"""sidecar 进程的生命周期管理。

## 拓扑

MaiBot 插件（Python，Runner 子进程）→ 启动 → Node sidecar
（`sidecar/bootstrap.mjs`，跑 vendored standalone 服务）→ 提供
`http://127.0.0.1:<port>/v1` 的 OpenAI 兼容网关。

sidecar 自己已经内含：匿名免费车道（含 v2 指纹门禁）、Kilo 免密池、
EAC 协付车道、十个账号渠道，以及本地聚合与降级链。这些都以
`node:*` 内置模块实现，零第三方依赖，所以整包是可搬運的。

## 就绪握手

`bootstrap.mjs` 在服务起来后往 stdout 打一行
`OFM_READY {json}`，里面是 `url` / `apiKey` / `managementUrl` / `version`。
只认这一行，不解析任何中文提示——上游的日志措辞会变，机器可读行不会。

读管道只用**一个**工作线程泵，把片段送进 `asyncio.Queue`；握手从队列
另一头取。不能「每次 wait_for 起一个读线程」：超时的线程仍阻塞在
`read()` 上，和新线程同一个 fd 争抢，会静默丢字节、握手卡到超时。

## 不留孤儿

Runner 子进程被强杀时不会执行 `on_unload`，直接遗下 sidecar。所以：

- POSIX：sidecar 用独立会话（`start_new_session`）起进程组，停止时先
  `SIGTERM` 整个进程组（让 vendored 的 `close()` 正常释放 `service.lock`），
  超时再 `SIGKILL` 整个进程组；
- Windows：`CREATE_NEW_PROCESS_GROUP` 起，停止时 `taskkill /PID /T /F`
  连子进程（渠道 Worker）一起端掉；
- 另起一个看门狗协程：sidecar 意外退出时清空 endpoint，让下一次请求走
  「重启」而不是对着死端口重试。

## 数据目录

sidecar 把配置、账号凭据、用量统计存在一个数据目录里。默认放在插件自己的
`ctx.paths.data_dir` 下——跨重启保留，且不碰 vendored 服务原配的
`~/.our-free-model`。
"""

from __future__ import annotations

import asyncio
import contextlib
import glob as _glob
import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

#: 就绪等待上限（秒）。首次启动要拉模型清单，冷启动偏慢。
READY_TIMEOUT = 120.0

#: 优雅退出等待（秒），超过就升级成强杀。
GRACEFUL_TIMEOUT = 8.0

#: 握手行前缀，与 `sidecar/bootstrap.mjs` 严格对齐。
READY_PREFIX = "OFM_READY "

#: 网关默认端口。0 表示交给系统分配，避免与用户本机已有服务抢端口。
DEFAULT_PORT = 0

#: 一次从管道取多少字节。
_CHUNK = 4096

_EOF = object()


class SidecarError(RuntimeError):
    """sidecar 起不来、意外退出或握手失败。"""


def _plugin_root() -> Path:
    """插件根目录（本文件在 `<root>/ofm_bridge/manager.py`）。"""
    return Path(__file__).resolve().parent.parent


def _blocking_read(fd: int) -> bytes | None:
    """阻塞读一个片段；对端关闭返回空 bytes，出错返回 None。

    必须用 `os.read` 而不是 `open(fd).read(n)`：后者是 BufferedReader，
    给了 size 就**凑满 n 字节才返回**。握手行只有一百多字节，凑不满就一直
    读不到 `OFM_READY`——实测两次冷启动全部卡死到超时。`os.read` 是
    底层 read(2)，管道里有数据就立刻返回。

    在工作线程里调用，绝不阻塞事件循环。
    """
    try:
        return os.read(fd, _CHUNK)
    except OSError:
        return None


class SidecarEndpoint:
    """一次就绪的 sidecar 连接信息。"""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.url = str(payload["url"]).rstrip("/")
        self.api_key = str(payload.get("apiKey") or "")
        self.management_url = str(payload.get("managementUrl") or "")
        self.version = str(payload.get("version") or "")
        self.data_dir = str(payload.get("dataDir") or "")
        self.port = int(payload.get("port") or 0)

    @property
    def base_url(self) -> str:
        return f"{self.url}/v1"

    def __repr__(self) -> str:  # 绝不把 api_key 带进 repr
        return f"<SidecarEndpoint {self.base_url} v{self.version}>"


class SidecarManager:
    """负责 sidecar 的启动、探活、重启与退出回收。"""

    def __init__(
        self,
        *,
        data_dir: Path | str,
        port: int = DEFAULT_PORT,
        node_path: str = "node",
        logger: Any = None,
        auto_refresh: bool = True,
        ready_timeout: float = READY_TIMEOUT,
    ) -> None:
        self.data_dir = Path(data_dir).expanduser()
        if not self.data_dir.is_absolute():
            self.data_dir = _plugin_root() / self.data_dir
        self.port = port
        self.node_path = node_path
        self.auto_refresh = auto_refresh
        # 可调而非硬编码：冷启动要拉上游清单，慢网络下 120s 也可能不够。
        self.ready_timeout = float(ready_timeout)
        self._logger = logger
        self._process: subprocess.Popen[bytes] | None = None
        self._endpoint: SidecarEndpoint | None = None
        self._lock = asyncio.Lock()
        self._pumps: list[asyncio.Task[None]] = []
        self._ready: asyncio.Future[SidecarEndpoint] | None = None
        self._stopping = False

    # ------------------------------------------------------------------ helpers

    def _log(self, level: str, message: str) -> None:
        """日志优先走宿主注入的 logger，退化为 stderr。

        Runner 子进程的 stdout 是 RPC 通道，**绝对不能**往那儿 print，
        所以退化目标只能是 stderr。
        """
        handler = getattr(self._logger, level, None) if self._logger is not None else None
        if callable(handler):
            handler(f"[free-model] {message}")
            return
        with contextlib.suppress(Exception):
            print(f"[free-model] {level}: {message}", file=sys.stderr)

    @property
    def script_path(self) -> Path:
        return _plugin_root() / "sidecar" / "bootstrap.mjs"

    @property
    def endpoint(self) -> SidecarEndpoint | None:
        return self._endpoint

    def status(self) -> dict[str, Any]:
        """给命令/Tool 用的状态摘要（不含密钥）。"""
        process = self._process
        return {
            "running": self.is_running(),
            "base_url": self._endpoint.base_url if self._endpoint else None,
            "management_url": self._endpoint.management_url if self._endpoint else None,
            "sidecar_version": self._endpoint.version if self._endpoint else None,
            "exit_code": process.poll() if process is not None else None,
            "data_dir": str(self.data_dir),
            "node_path": self._cached_node,
        }

    def is_running(self) -> bool:
        process = self._process
        return process is not None and process.poll() is None and self._endpoint is not None

    # ------------------------------------------------------------------- start

    async def ensure_running(self) -> SidecarEndpoint:
        """返回就绪的 endpoint，必要时启动或重启 sidecar。

        并发调用共享同一把锁：vendored 服务对同一数据目录只允许一个实例
        （`service.lock`），两个 Runner 同时启动必然有一个被 EEXIST 挡下。
        """
        if self._endpoint is not None and self.is_running():
            return self._endpoint
        async with self._lock:
            if self._endpoint is not None and self.is_running():
                return self._endpoint
            return await self._start_locked()

    def _build_command(self, node: str) -> list[str]:
        args = [node, str(self.script_path), "--data-dir", str(self.data_dir), "--port", str(self.port)]
        if not self.auto_refresh:
            args += ["--no-refresh", "true"]
        return args

    _cached_node: str | None = None

    def _resolve_node(self) -> str:
        """把配置的 node 命令解析成可执行文件绝对路径。

        GUI 启动的 MaiBot 常常拿不到 shell 的 PATH（nvm 装的 node 尤其如此），
        所以除了 `which`，再试一批常见安装位置。
        """
        configured = (self.node_path or "node").strip() or "node"
        found = shutil.which(configured)
        if found:
            self._cached_node = found
            return found
        if os.path.isabs(configured) and os.access(configured, os.X_OK):
            self._cached_node = configured
            return configured

        patterns = [
            str(Path.home() / ".nvm" / "versions" / "node" / "*" / "bin" / "node"),
            "/usr/local/bin/node",
            "/usr/bin/node",
            "/opt/homebrew/bin/node",
            str(Path(os.environ.get("NVM_BIN", "/nonexistent")) / "node"),
            str(Path(os.environ.get("HOMEBREW_PREFIX", "/nonexistent")) / "bin" / "node"),
        ]
        if os.name == "nt":
            patterns = [
                r"C:\Program Files\nodejs\node.exe",
                str(Path(os.environ.get("APPDATA", "/nonexistent")) / "npm" / "node.exe"),
            ]
        for pattern in patterns:
            for hit in sorted(_glob.glob(pattern), reverse=True):
                if os.path.isfile(hit):
                    self._cached_node = hit
                    return hit
        raise SidecarError(
            f"找不到 Node.js 可执行文件（配置值 {configured!r}）。"
            "sidecar 需要 Node >= 22.19；请把插件配置里的 nodePath 指向绝对路径。"
        )

    async def _start_locked(self) -> SidecarEndpoint:
        if not self.script_path.exists():
            raise SidecarError(f"缺少 sidecar 入口：{self.script_path}")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        # 上一轮的残骸先清掉，否则新实例会撞 service.lock。
        await self._cleanup_existing()

        node = self._resolve_node()
        command = self._build_command(node)
        self._log("info", f"启动 sidecar（{node}），端口 {self.port or '系统分配'}，数据目录 {self.data_dir}")

        try:
            process = subprocess.Popen(  # noqa: S603 - 命令与参数均来自受控配置
                command,
                cwd=str(self.script_path.parent),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=False,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
                start_new_session=(os.name != "nt"),
            )
        except OSError as exc:
            raise SidecarError(f"sidecar 启动失败：{exc}") from exc

        self._process = process
        self._endpoint = None
        assert process.stdout is not None and process.stderr is not None
        ready: asyncio.Future[SidecarEndpoint] = asyncio.get_running_loop().create_future()
        # 握手超时（`asyncio.shield` 保住 future）之后，stdout 泵仍可能补一个
        # 异常进来。没人 retrieve 就会打 "Future exception was never retrieved"，
        # 把真正的失败原因淹在噪音里，所以这里主动消费掉。
        ready.add_done_callback(lambda fut: fut.cancelled() or fut.exception())
        self._ready = ready
        self._pumps = [
            asyncio.create_task(self._pump_stdout(process.stdout.fileno(), ready)),
            asyncio.create_task(self._pump_stderr(process.stderr.fileno())),
            asyncio.create_task(self._watch_exit(process)),
        ]

        try:
            endpoint = await asyncio.wait_for(asyncio.shield(ready), timeout=self.ready_timeout)
        except asyncio.TimeoutError:
            await self._cleanup_existing()
            raise SidecarError(
                f"sidecar 在 {self.ready_timeout:.0f}s 内没有就绪。首次启动需要拉取模型清单，"
                "也可能被网络出口挡住；可把 startTimeout 调大后再试。"
            ) from None
        except Exception:
            await self._cleanup_existing()
            raise
        finally:
            self._ready = None

        if not ready.done() or ready.cancelled():
            await self._cleanup_existing()
            raise SidecarError("sidecar 就绪等待被打断")

        self._endpoint = endpoint
        self._log(
            "info",
            f"sidecar 就绪：{endpoint.base_url}（v{endpoint.version}）"
            f"｜账号渠道登录页 {endpoint.management_url}",
        )
        return endpoint

    async def _pump_stdout(self, fd: int, ready: asyncio.Future[SidecarEndpoint]) -> None:
        """单线程泵：把 stdout 逐行分发成握手信号或普通日志。"""
        pending = b""
        resolved = False
        while True:
            chunk = await asyncio.to_thread(_blocking_read, fd)
            if chunk is None or chunk == b"":
                break
            pending += chunk
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                text = line.decode("utf-8", "replace").strip()
                if not text:
                    continue
                if text.startswith(READY_PREFIX) and not resolved:
                    resolved = True
                    self._resolve_ready(ready, text[len(READY_PREFIX):])
                    continue
                self._log("info", f"sidecar: {text}")
        if pending.strip():
            self._log("info", f"sidecar: {pending.decode('utf-8', 'replace').strip()}")
        if not resolved and not ready.done():
            ready.set_exception(
                SidecarError("sidecar 的 stdout 已关闭且未输出就绪行；请查看 sidecar 日志转录。")
            )

    def _resolve_ready(self, ready: asyncio.Future[SidecarEndpoint], payload_text: str) -> None:
        try:
            payload = json.loads(payload_text)
        except ValueError as exc:
            if not ready.done():
                ready.set_exception(SidecarError(f"就绪行 JSON 解析失败：{exc}"))
            return
        if not isinstance(payload, dict) or not payload.get("url"):
            if not ready.done():
                ready.set_exception(SidecarError("就绪行缺少 url 字段"))
            return
        if not ready.done():
            ready.set_result(SidecarEndpoint(payload))

    async def _pump_stderr(self, fd: int) -> None:
        """把 sidecar 的 stderr 逐行转成宿主日志。

        vendored 服务把 warn/info 都写在 stderr，这既是诊断的主要来源，
        也必须持续排空——管道填满会把 sidecar 卡死。
        """
        pending = b""
        while True:
            chunk = await asyncio.to_thread(_blocking_read, fd)
            if chunk is None or chunk == b"":
                break
            pending += chunk
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                text = line.decode("utf-8", "replace").strip()
                if text:
                    self._classify_log(text)
        if pending.strip():
            self._classify_log(pending.decode("utf-8", "replace").strip())

    def _classify_log(self, text: str) -> None:
        """vendored 服务的 `[warn]`/`[error]` 前缀映射到宿主级别。"""
        lowered = text.lower()
        if lowered.startswith("[error]"):
            self._log("error", text)
        elif lowered.startswith("[warn]"):
            self._log("warning", text)
        else:
            self._log("info", text)

    async def _watch_exit(self, process: subprocess.Popen[bytes]) -> None:
        """sidecar 意外退出时清空 endpoint，下一次请求就会重启它。"""
        while True:
            await asyncio.sleep(2.0)
            if process.poll() is None:
                continue
            if not self._stopping and self._endpoint is not None:
                self._log(
                    "warning",
                    f"sidecar 进程退出（退出码 {process.returncode}），下一次请求会重新启动它。",
                )
            self._endpoint = None
            return

    # --------------------------------------------------------------------- stop

    async def shutdown(self) -> None:
        """停止 sidecar，释放 service.lock。幂等。"""
        async with self._lock:
            self._stopping = True
            try:
                await self._cleanup_existing()
            finally:
                self._stopping = False

    async def _cleanup_existing(self) -> None:
        """杀掉当前 sidecar 进程组、收干任务、清掉残留锁文件。"""
        self._endpoint = None
        process = self._process
        self._process = None

        if process is not None:
            await self._terminate(process)
        tasks, self._pumps = self._pumps, []
        # 进程退出后管道对端关闭，读线程自然拿到 EOF；仍卡住的（EOF 前
        # 还有子进程持有写端，比如渠道 Worker）就取消任务并**有界等待**。
        # 不去 close() 那两个 fd：与工作线程正在 read 的 fd 号存在复用竞态。
        for task in tasks:
            task.cancel()
        if tasks:
            _done, pending = await asyncio.wait(tasks, timeout=3.0)
            if pending:
                self._log("warning", f"{len(pending)} 个 sidecar 日志泵线程未退出，已交由解释器回收。")

        ready = self._ready
        if ready is not None and not ready.done():
            ready.cancel()

        with contextlib.suppress(OSError):
            (self.data_dir / "service.lock").unlink()

    async def _terminate(self, process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        self._log("info", "正在停止 sidecar…")
        if os.name == "nt":
            with contextlib.suppress(Exception):
                process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            with contextlib.suppress(Exception):
                self._signal_process_group(process, signal.SIGTERM)
        if await self._wait_exit(process, GRACEFUL_TIMEOUT):
            return

        self._log("warning", "sidecar 未在优雅期内退出，强杀整个进程组。")
        if os.name == "nt":
            with contextlib.suppress(Exception):
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True,
                    timeout=15,
                    check=False,
                )
        else:
            with contextlib.suppress(Exception):
                self._signal_process_group(process, signal.SIGKILL)
        if not await self._wait_exit(process, 5.0):
            with contextlib.suppress(Exception):
                process.kill()
            await self._wait_exit(process, 5.0)

    @staticmethod
    def _signal_process_group(process: subprocess.Popen[bytes], sig: signal.Signals) -> None:
        try:
            os.killpg(os.getpgid(process.pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            with contextlib.suppress(Exception):
                process.send_signal(sig)

    @staticmethod
    async def _wait_exit(process: subprocess.Popen[bytes], seconds: float) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
        while loop.time() < deadline:
            if process.poll() is not None:
                return True
            await asyncio.sleep(0.15)
        return process.poll() is not None
