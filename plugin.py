"""Our Free Model — MaiBot 插件入口。

把 dsh 的 `dsh-our-free-model` 免费模型能力接到麦麦上：一个
`@LLMProvider`，背后是随插件一起发布的 Node sidecar（vendored 的
`packages/standalone`），内含匿名免费车道、Kilo 免密池、十个账号渠道、
本地聚合与降级链。

## 为什么是 sidecar 而不是纯 Python 重写

上游那套车道逻辑有三万行 JavaScript，其中指纹门禁、验证码接力、wasm
签名（`qoder-auth-wasm.wasm`）都无法用 MaiBot 的插件 API 表达。而
`packages/standalone` 已经证明这些逻辑**不依赖 dsh 宿主**：只用 `node:*`
内置模块，零第三方依赖。所以正确的移植是复用那份经过实测的协议实现，
而不是把它重写成另一份未经验证的 Python。Python 侧只做 MaiBot 需要的
那层形状适配 —— 这正是 `ofm_bridge/protocol.py` 的全部职责。

## 生命周期

- `on_load`：不阻塞。真正启动 sidecar 的工作丢给后台任务，失败也不
  让插件加载失败（模型不可用 ≠ 插件坏了），错误只记日志，等到
  第一次请求时再重试。
- `on_unload`：杀掉 sidecar 进程组并释放 `service.lock`。
- 配置热更新：改了端口/数据目录这类必须重启才生效的值，就重启 sidecar。

## 模型清单的落地方式

MaiBot 的插件 Provider **不能**向 WebUI 广播模型清单，模型必须写在
`model_config.toml` 里。所以 `/ofm toml` 是这条能力链的关键出口：它读实时
清单，生成一段可直接粘贴的 TOML，同时落一份到数据目录。上游清单是动态的，
这段文本要能重生成 —— 别抄一次用一辈子。
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from pathlib import Path
from typing import Any, ClassVar, Iterable

from maibot_sdk import Command, Field, LLMProvider, LLMProviderBase, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType

#: Runner 用 `spec_from_file_location` 加载本文件，`sys.path` 里只有
#: 主程序 `src` 与插件的**父**目录，插件自己的目录不在其中——同级包
#: `ofm_bridge` 因此必须显式加路径。用插件目录而不是包名做标记，
#: 避免多个插件互相污染。
_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from ofm_bridge import catalog as catalog_module  # noqa: E402
from ofm_bridge import gateway  # noqa: E402
from ofm_bridge.manager import DEFAULT_PORT, SidecarError, SidecarManager  # noqa: E402
from ofm_bridge.provider import FreeModelProvider, ProviderUnavailable  # noqa: E402

CLIENT_TYPE = catalog_module.CLIENT_TYPE


class PluginSection(PluginConfigBase):
    """`[plugin]` 子节。

    形状照抄内置插件 `plugin_management`：根配置模型必须有一个名为
    `plugin` 的子节，且其中含 `config_version` —— SDK 的
    `extract_plugin_config_version()` 硬要求这两个名字，缺一个 Runner
    就报「插件配置文件缺少 [plugin] 配置节」并拒绝注入配置。
    """

    __ui_label__ = "免费模型"

    config_version: str = Field(default="1.0.0", description="配置版本号（Runner 用于判断是否需要迁移配置）")
    enabled: bool = Field(default=True, description="总开关。关闭后 Provider 直接拒绝请求，sidecar 也会停掉。")
    nodePath: str = Field(
        default="node",
        description="Node.js 可执行文件，需要 Node >= 22.19。GUI 启动的麦麦常拿不到 shell PATH，"
        "那种情况请填绝对路径，例如 ~/.nvm/versions/node/v24.20.0/bin/node。",
    )
    port: int = Field(
        default=DEFAULT_PORT,
        description="sidecar 监听端口。默认 18900，固定值才能把 base_url 写死于模型配置；"
        "0 = 交给系统分配（每次启动都会变，模型配置得跟着改）。",
        ge=0,
        le=65535,
    )
    dataDir: str = Field(
        default="",
        description="sidecar 数据目录（配置、账号凭据、用量统计）。留空则用插件自己的数据目录。",
    )
    autoStart: bool = Field(default=True, description="插件加载时就拉起 sidecar，而不是等第一次请求。")
    autoRefresh: bool = Field(default=True, description="允许 sidecar 刷新模型清单；关闭可省一次冷启动网络往返。")
    startTimeout: int = Field(
        default=120,
        description="等待 sidecar 就绪的秒数。首次启动要拉上游清单，慢一点是正常的。",
        ge=15,
        le=600,
    )
    turnTimeout: int = Field(
        default=300,
        description="单次请求的收流上限秒数。长思考模型一轮可能很久。",
        ge=30,
        le=1800,
    )
    defaultEffort: str = Field(
        default="",
        description="模型有思考档位菜单、但任务没指定时用哪一档。留空 = 交给上游默认。常见值 high / medium / low。",
    )
    providerName: str = Field(
        default=catalog_module.DEFAULT_PROVIDER_NAME,
        description="生成 TOML 时写进 api_provider 的服务商名，需与 model_config 里 [[api_providers]].name 一致。",
    )


class FreeModelConfig(PluginConfigBase):
    """插件根配置。真正的字段都在 `plugin` 子节里。"""

    plugin: PluginSection = Field(default_factory=PluginSection, description="免费模型插件设置")


class OurFreeModelPlugin(MaiBotPlugin):
    """一个 `@LLMProvider` + 一个本地 sidecar + 一组运维命令。"""

    config_model: ClassVar[type[PluginConfigBase] | None] = FreeModelConfig
    config_reload_subscriptions: ClassVar[Iterable[str]] = ()

    def __init__(self) -> None:
        super().__init__()
        self._manager: SidecarManager | None = None
        self._provider: FreeModelProvider | None = None
        self._startup: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------- lifecycle

    async def on_load(self) -> None:
        config = self._cfg()
        if not config.enabled:
            self.ctx.logger.warning("[free-model] 已配置为关闭（enabled=false），不会启动 sidecar。")
            return
        await self._build()
        if config.autoStart:
            # 后台启动，不阻塞 on_load：冷启动可能几十秒，而 Runner 对加载
            # 有超时；把「起不来」变成「第一次请求时再试」比让插件挂掉好。
            self._startup = asyncio.create_task(self._warm_up())

    async def on_unload(self) -> None:
        if self._startup is not None and not self._startup.done():
            self._startup.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._startup
        self._startup = None
        if self._manager is not None:
            await self._manager.shutdown()
            self._manager = None
            self._provider = None
        self.ctx.logger.info("[free-model] 插件已卸载，sidecar 已回收。")

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        if scope != "self":
            return
        self.ctx.logger.info("[free-model] 配置已更新（version=%s），按新配置重建 sidecar。", version)
        # 端口/数据目录/node 路径都是启动期参数，热改必须重启进程才生效。
        manager = self._manager
        self._manager = None
        self._provider = None
        if manager is not None:
            await manager.shutdown()
        if self._cfg().enabled:
            await self._build()
            if self._cfg().autoStart:
                self._startup = asyncio.create_task(self._warm_up())

    # ------------------------------------------------------------ internals

    def _cfg(self) -> PluginSection:
        """`self.config.plugin` 的短写。

        配置必须嵌在 `[plugin]` 子节里（见 `PluginSection` 的说明），
        每次写 `self.config.plugin.` 太啰嗦，容易漏。
        """
        return self.config.plugin

    async def _build(self) -> None:
        """按当前配置装配 manager 与 provider。"""
        config = self._cfg()
        data_dir = Path(config.dataDir).expanduser() if config.dataDir else self._default_data_dir()
        self._manager = SidecarManager(
            data_dir=data_dir,
            port=config.port,
            node_path=config.nodePath,
            logger=self.ctx.logger,
            auto_refresh=config.autoRefresh,
            ready_timeout=float(config.startTimeout),
        )
        self._provider = FreeModelProvider(
            self._manager,
            logger=self.ctx.logger,
            turn_timeout=float(config.turnTimeout),
            default_effort=(config.defaultEffort.strip() or None),
        )

    def _default_data_dir(self) -> Path:
        """插件专属数据目录；拿不到就退回插件目录下的 `.ofm-data`。"""
        paths = getattr(self.ctx, "paths", None)
        candidate = getattr(paths, "data_dir", None)
        if candidate:
            return Path(candidate) / "sidecar"
        return _PLUGIN_DIR / ".ofm-data"

    async def _warm_up(self) -> None:
        """后台把 sidecar 拉起来，只为让第一次对话不必等冷启动。"""
        assert self._provider is not None
        try:
            report = await self._provider.probe()
        except (SidecarError, ProviderUnavailable, gateway.GatewayError) as exc:
            self.ctx.logger.warning(
                "[free-model] sidecar 预启动失败：%s（第一次请求会自动重试；可用 /ofm restart 手动重试）",
                exc,
            )
            return
        except Exception as exc:  # noqa: BLE001 - 后台任务不能把异常抛回事件循环
            self.ctx.logger.error("[free-model] sidecar 预启动异常：%r", exc)
            return
        if report.get("ok"):
            self.ctx.logger.info(
                "[free-model] sidecar 就绪：%s，%s 个模型可用（车道 %s）。",
                report["base_url"],
                report["models"],
                "、".join(f"{lane}×{count}" for lane, count in sorted(report["lanes"].items())) or "无",
            )
        else:
            self.ctx.logger.warning("[free-model] sidecar 已启动但清单为空：%s", report.get("error"))

    async def _ready_provider(self) -> FreeModelProvider:
        """拿 provider；插件被关掉或还没装配时给出可操作的解释。"""
        if self._provider is None:
            async with self._lock:
                if self._provider is None:
                    if not self._cfg().enabled:
                        raise ProviderUnavailable("插件当前为关闭状态，请把配置里的 enabled 设为 true。")
                    await self._build()
        assert self._provider is not None
        return self._provider

    # -------------------------------------------------------------- provider

    @LLMProvider(CLIENT_TYPE, name="Our Free Model", description="免费模型车道：匿名池、Kilo 免密池与十个账号渠道")
    async def handle_llm(self, operation: str, request: dict[str, Any]) -> dict[str, Any]:
        """MaiBot 的 LLM 请求入口。

        Host 侧 `PluginLLMClient` 把 `ResponseRequest` 序列化成
        `context_items` 等字段后 RPC 过来；这里只负责转形状与收流。
        """
        provider = await self._ready_provider()
        return await provider.dispatch(operation, request)

    # -------------------------------------------------------------- commands

    @Command("ofm", description="免费模型运维：status / models / toml / restart / login / help", pattern=r"^/ofm\b")
    async def handle_ofm(self, **kwargs):
        stream_id = kwargs.get("stream_id")
        text = self._command_text(kwargs)
        action = self._command_action(text)

        if action in ("", "help", "h", "?"):
            return await self._reply(stream_id, self._help_text())
        if action == "status":
            return await self._reply(stream_id, await self._status_text())
        if action == "models":
            return await self._reply(stream_id, await self._models_text())
        if action == "toml":
            return await self._dispatch_toml(stream_id)
        if action in ("endpoint", "openai", "url"):
            return await self._reply(stream_id, await self._endpoint_text())
        if action in ("restart", "start"):
            return await self._reply(stream_id, await self._restart_text())
        if action == "login":
            return await self._reply(stream_id, await self._login_text())
        return await self._reply(stream_id, f"不认识的操作符 `{action}`，用 /ofm help 看可选项。")

    @staticmethod
    def _command_text(kwargs: dict[str, Any]) -> str:
        """从 Command 的 kwargs 里取原始消息文本。

        文档（plugin/commands.md）给的字段是 `raw_message`，另有
        `matched_groups`（命名捕获组）与 `stream_id`。按 `raw_message` →
        `matched_groups.args` → `text` 的优先级试探，兼容 Host 的字段差异。
        """
        raw = kwargs.get("raw_message")
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
        groups = kwargs.get("matched_groups")
        if isinstance(groups, dict):
            for key in ("args", "text", "action"):
                value = groups.get(key)
                if isinstance(value, str) and value.strip():
                    return f"/ofm {value.strip()}"
        for key in ("text", "message_str"):
            value = kwargs.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    @staticmethod
    def _command_action(text: str) -> str:
        """`/ofm models xxx` → `models`。"""
        parts = text.split(None, 2)
        return parts[1].lower() if len(parts) > 1 else ""

    async def _reply(self, stream_id: Any, message: str) -> tuple[bool, str, int]:
        """发一条文本并按 Command 契约返回三元组。

        返回值是 `(success, response, weight)`：weight 是命令优先级权重，
        **不是**「已回复」标记；Host 不会替我们把 response 发出去，所以
        消息必须自己 `send.text`。stream_id 缺失（例如从 Tool 侧进来）时
        只把文本作为 response 带回，不再尝试发送。
        """
        if stream_id is None:
            return True, message, 1
        await self.ctx.send.text(message, stream_id)
        return True, message, 1

    def _help_text(self) -> str:
        return (
            "Our Free Model｜免费模型车道\n"
            "/ofm endpoint — 输出本机 OpenAI 兼容端点（地址 + 密钥 + 可粘贴的 provider 块）\n"
            "/ofm status — sidecar 与车道状态\n"
            "/ofm models — 当前可用模型清单\n"
            "/ofm toml — 生成 [[models]] 段（配合上面二选一使用）\n"
            "/ofm restart — 重启 sidecar（改了端口/数据目录/Node 路径后用）\n"
            "/ofm login — 打开账号渠道登录页（Qoder/TRAE/Cline/Gemini 等十个）\n"
            "\n两条接入方式：\n"
            "  1) 省事：/ofm endpoint 拿 base_url + api_key，client_type 填 openai，"
            "直接用麦麦内置 OpenAI 客户端；\n"
            "  2) 完整：client_type 填 our_free_model 走本插件，连思考内容一起拿到。\n"
            "两种都还需要 [[models]] 段：插件 Provider 无法向 WebUI 广播模型清单。"
        )

    async def _status_text(self) -> str:
        if self._manager is None:
            enabled = self._cfg().enabled
            return (
                "sidecar 未装配。"
                if enabled
                else "插件当前为关闭状态（config.enabled = false）。"
            )
        report = await self._provider.probe() if self._provider is not None else {"ok": False}
        lines = [
            "【sidecar】",
            f"- 进程：{'运行中' if self._manager.is_running() else '未运行'}",
            f"- 数据目录：{self._manager.data_dir}",
            f"- Node：{self._manager.status().get('node_path') or self._cfg().nodePath}",
        ]
        if report.get("ok"):
            lanes = "、".join(f"{lane}×{count}" for lane, count in sorted(report["lanes"].items())) or "无"
            lines += [
                f"- 网关：{report['base_url']}（sidecar v{report['sidecar_version']}）",
                f"- 模型：{report['models']} 个（{lanes}）",
                f"- 登录页：{report['management_url']}",
            ]
        else:
            lines.append(f"- 错误：{report.get('error') or '尚未就绪，用 /ofm restart 重试'}")
        lines.append(
            "\n【提醒】免费车道由上游共享配额，撞 429 是常态而非故障；"
            "EAC 协付渠道依赖 dsh 桌面端宿主指纹，在麦麦上不可用。"
        )
        return "\n".join(lines)

    async def _models_text(self) -> str:
        provider = await self._ready_provider()
        try:
            models = await provider.list_models()
        except (SidecarError, ProviderUnavailable, gateway.GatewayError) as exc:
            return f"取清单失败：{exc}"
        if not models:
            return "清单为空：sidecar 可能没起来，或上游此刻不可达。试试 /ofm restart。"
        return "\n".join(["【可用模型】", *catalog_module.summary_lines(models)])

    async def _toml_parts(self, models: list) -> list[str]:
        """把配置切成能逐条发进聊天的若干段。

        用**精简版**而不是带注释的完整版：27 个模型的完整版约 7800 字符（8 段），
        精简版约 3000 字符（4 段）。两者都能被 `tomllib` 解析；注释是给人读的，
        不该用多刷四行消息换。带注释的完整版始终写进文件。
        """
        text = catalog_module.render_minimal_toml(models, provider_name=self._cfg().providerName)
        return catalog_module.split_toml(text, label=self._cfg().providerName)

    async def _handle_toml(self, stream_id: Any, models: list) -> tuple[bool, str, int]:
        text = catalog_module.render_toml(models, provider_name=self._cfg().providerName)
        saved: str | None = None
        manager = self._manager
        if manager is not None:
            with contextlib.suppress(OSError):
                manager.data_dir.mkdir(parents=True, exist_ok=True)
                target = manager.data_dir / "generated_models.toml"
                target.write_text(text, encoding="utf-8")
                saved = str(target)
        parts = await self._toml_parts(models)
        # 只讲各段头部没有的信息：粘完要重启、占位值不用改、完整版在哪。
        # 「共几段、按顺序粘」这类话每段头部已经写了，别在这里重复。
        intro = (
            f"【免费模型配置 · {len(models)} 个模型】全部粘进 model_config.toml 后**要重启麦麦**才生效。\n"
            "base_url / api_key 是占位值，不用改：真实端口与密钥由插件运行时向 sidecar 握手取得。"
            + (f"\n完整带注释版已写入：{saved}" if saved else "")
        )
        await self._reply(stream_id, intro)
        for part in parts:
            await self._reply(stream_id, part)
        return True, intro, 1

    async def _toml_text(self) -> str:
        """给 Tool / 无 stream_id 场景的一次性全文。"""
        provider = await self._ready_provider()
        try:
            models = await provider.list_models()
        except (SidecarError, ProviderUnavailable, gateway.GatewayError) as exc:
            return f"生成 TOML 失败：{exc}"
        if not models:
            return "清单为空，生成不出可用配置。试试 /ofm restart。"
        return catalog_module.render_minimal_toml(models, provider_name=self._cfg().providerName)

    async def _dispatch_toml(self, stream_id: Any) -> tuple[bool, str, int]:
        """`/ofm toml`：先取清单，再决定分段发还是直接报错。"""
        provider = await self._ready_provider()
        try:
            models = await provider.list_models()
        except (SidecarError, ProviderUnavailable, gateway.GatewayError) as exc:
            return await self._reply(stream_id, f"生成 TOML 失败：{exc}")
        if not models:
            return await self._reply(stream_id, "清单为空，生成不出可用配置。试试 /ofm restart。")
        return await self._handle_toml(stream_id, models)

    async def _restart_text(self) -> str:
        manager = self._manager
        if manager is None:
            await self._build()
            manager = self._manager
        assert manager is not None
        await manager.shutdown()
        self._provider = None
        await self._build()
        assert self._provider is not None
        try:
            report = await self._provider.probe()
        except (SidecarError, gateway.GatewayError) as exc:
            return f"重启失败：{exc}"
        if not report.get("ok"):
            return f"sidecar 起来了但清单为空：{report.get('error')}"
        return f"已重启：{report['base_url']}，{report['models']} 个模型可用。"

    async def _endpoint_text(self) -> str:
        """输出可作为纯 OpenAI 端点使用的地址、密钥与 provider 块。

        这条路不经过本插件的 `@LLMProvider`：麦麦用内置 `openai` 客户端直连
        sidecar，协议投影与流式解析都由麦麦自己做，兼容性最好。端口是固定的，
        所以这段配置写完就不用再动（除非 18900 被别的程序占了）。
        """
        provider = await self._ready_provider()
        try:
            endpoint = await provider.manager.ensure_running()
        except (SidecarError, ProviderUnavailable) as exc:
            return f"sidecar 未就绪：{exc}"
        block = catalog_module.openai_provider_block(
            endpoint.base_url,
            endpoint.api_key,
            provider_name=self._cfg().providerName,
            timeout=self._cfg().turnTimeout,
        )
        assigned = f"（默认 {DEFAULT_PORT}）" if endpoint.port == DEFAULT_PORT else (
            f"（注意：{DEFAULT_PORT} 被占用，实际用了 {endpoint.port}，以这里为准）"
        )
        return (
            "【本机 OpenAI 兼容端点】\n"
            f"base_url : {endpoint.base_url}{assigned}\n"
            f"api_key  : {endpoint.api_key}\n"
            f"模型列表 : {endpoint.base_url}/models\n"
            "\n把下面这段粘进 model_config.toml（client_type 是麦麦内置的 openai）：\n"
            f"{block}\n"
            "\n再补 [[models]] 段：/ofm toml 会逐段发来，或直接抄生成的"
            "generated_models.toml。"
        )

    async def _login_text(self) -> str:
        if self._provider is None:
            await self._ready_provider()
        assert self._provider is not None
        try:
            endpoint = await self._provider.manager.ensure_running()
        except (SidecarError, ProviderUnavailable) as exc:
            return f"sidecar 未就绪：{exc}"
        return (
            "账号渠道登录页（仅本机可开）：\n"
            f"{endpoint.management_url}\n"
            "链接是一次性的，10 分钟内有效。在里面给 Qoder / TRAE / Cline / "
            "Gemini / MiniMax / ZCode / Loomy / Raccoon / LobsterAI 等渠道登录，"
            "登录后对应模型就会出现在 /ofm models 里。"
        )

    # ----------------------------------------------------------------- tools

    @Tool(
        "free_model_status",
        brief_description="查询本地免费模型网关的状态与可用模型数",
        detailed_description=(
            "当需要判断免费模型车道是否健康、当前有多少模型可用、"
            "或某个车道（匿名池 / Kilo / 账号渠道）是否已登录时调用。"
            "不产生任何上游请求，只做一次本地清单读取。"
        ),
        parameters=[
            ToolParameterInfo(
                name="detail",
                param_type=ToolParamType.BOOLEAN,
                description="是否同时返回按车道聚合的模型数",
                required=False,
                default=False,
            )
        ],
    )
    async def handle_status_tool(self, detail: bool = False, **kwargs) -> dict[str, Any]:
        """给 LLM 用的结构化状态，省掉一次真实生成的花费。"""
        del kwargs
        if self._provider is None:
            await self._ready_provider()
        assert self._provider is not None
        report = await self._provider.probe()
        if not detail:
            report = {
                key: report.get(key)
                for key in ("ok", "base_url", "models", "sidecar_version", "error")
                if key in report
            }
        return report


def create_plugin() -> OurFreeModelPlugin:
    return OurFreeModelPlugin()
