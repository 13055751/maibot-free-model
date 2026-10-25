"""本地 sidecar 网关的 HTTP 客户端（纯标准库）。

## 为什么不用 httpx / aiohttp

MaiBot 主程序装了 httpx，但插件跑在 **Runner 子进程**里，那里由 SDK 的
依赖清单决定环境——只有 `pydantic` 与 `msgpack`。把主程序的依赖当成
自己的依赖，会在依赖裁剪或独立 venv 部署时炸掉，所以这里只用标准库。

## 为什么一定要读流式

sidecar 的 `/v1/chat/completions` 在非流式分支里**丢掉了思考内容**：
`src/forward.js` 的 JSON 分支只回填 `content` 与 `tool_calls`，
`outcome.thinking` 没有落点（实测：`kilo-auto/free` 非流式响应里没有
`reasoning_content`，流式则有 `delta.reasoning`）。MaiBot 的
`ReasoningItem` 需要这份内容，所以插件内部强制 `stream: true`，
把 SSE 收拢成一个完整结果再交给 Host——Host 侧的流式钩子对插件
Provider 根本不开放，收拢是唯一的办法。

## 流里的失败也必须当成失败

网关在流已开始后才发现问题时，会往流里写一条 `{"error": {...}}`
（状态行早已是 200）。这时候绝不能返回一个「正常完成但正文为空」的
结果——那会让 Host 把失败当成功记账。看到 error 事件就抛。
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

#: 一次收流的默认上限（秒）。思考模型可能长时间只出 reasoning。
DEFAULT_TIMEOUT = 180.0

#: 从错误响应体里截取多少字符进消息，够诊断又不至于刷爆日志。
_ERROR_BODY_LIMIT = 400


class GatewayError(RuntimeError):
    """一次网关调用的失败。

    Args:
        message: 面向日志与 Host 的失败说明。
        code: 归一化的失败类别（`transport` / `timeout` / `status` /
            `upstream` / `malformed` / `aborted`）。
        status: HTTP 状态码，若有。
        retry_after_ms: 上游 `Retry-After` 折算的毫秒，若有。
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "transport",
        status: int | None = None,
        retry_after_ms: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.retry_after_ms = retry_after_ms


@dataclass
class ChatResult:
    """一次收拢后的完整响应。"""

    content: str = ""
    reasoning: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] | None = None
    finish_reason: str | None = None
    model: str | None = None
    response_id: str | None = None


@dataclass
class _ToolSlot:
    """按 `index` 归并的一路工具调用增量。"""

    call_id: str = ""
    name: str = ""
    arguments: list[str] = field(default_factory=list)

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.call_id,
            "type": "function",
            "function": {"name": self.name, "arguments": "".join(self.arguments)},
        }


def _classify_http_error(status: int, body_text: str, retry_after: str | None) -> GatewayError:
    """把网关的错误信封翻译成一个带类别的失败。

    网关沿用 OpenAI 的 `{error:{message,type,code}}`，`code` 是上游语义
    （`RATE_LIMIT` / `LANE_LOCKED` / …）。保留 `code` 很重要：Host 的重试
    策略按它区分「值得再试」与「再试只是烧配额」。
    """
    message = body_text.strip() or f"HTTP {status}"
    code: str | None = None
    try:
        payload = json.loads(body_text)
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            message = str(error.get("message") or message)
            candidate = error.get("code") or error.get("type")
            code = str(candidate) if candidate is not None else None
        elif isinstance(error, str):
            message = error
    except ValueError:
        pass
    retry_ms: int | None = None
    try:
        if retry_after:
            retry_ms = max(0, int(float(retry_after)) * 1000)
    except ValueError:
        retry_ms = None
    if status == 401 or status == 403:
        kind = "unauthorized"
    elif status == 404:
        kind = "not_found"
    elif status == 429:
        kind = "rate_limit"
    elif status >= 500:
        kind = "server"
    else:
        kind = "client"
    return GatewayError(
        f"{code + ': ' if code else ''}{message[:_ERROR_BODY_LIMIT]}" if code else message[:_ERROR_BODY_LIMIT],
        code=kind,
        status=status,
        retry_after_ms=retry_ms,
    )


def _consume_stream(response: Any, result: ChatResult) -> ChatResult:
    """逐行吃 SSE，把增量并进 `result`。

    `response` 是 `urllib` 的 HTTPResponse（二进制）。SSE 帧以空行分隔，
    但只按 `data:` 前缀取载荷更稳：注释行（`: KILO PROCESSING`）与
    `event:` 行都直接跳过。
    """
    slots: dict[int, _ToolSlot] = {}

    for raw_line in response:
        line = raw_line.decode("utf-8", "replace").strip()
        if not line or not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except ValueError:
            # 一行坏帧不该毁掉整轮已收到的正文。
            continue
        if not isinstance(chunk, dict):
            continue

        # 网关在 200 之后才发现失败时的错误帧。
        error = chunk.get("error")
        if isinstance(error, dict):
            raise GatewayError(
                str(error.get("message") or "stream failed")[:_ERROR_BODY_LIMIT],
                code=str(error.get("code") or error.get("type") or "upstream"),
            )

        if chunk.get("id") and result.response_id is None:
            result.response_id = str(chunk["id"])
        if chunk.get("model"):
            result.model = str(chunk["model"])
        usage = chunk.get("usage")
        if isinstance(usage, dict):
            result.usage = usage

        for choice in chunk.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            if choice.get("finish_reason"):
                result.finish_reason = str(choice["finish_reason"])
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                continue
            if isinstance(delta.get("content"), str):
                result.content += delta["content"]
            # 思考增量：网关用 `reasoning`，兼容 OpenAI 风格的
            # `reasoning_content` / `reasoning_summary_text`。
            for key in ("reasoning", "reasoning_content", "thinking"):
                if isinstance(delta.get(key), str):
                    result.reasoning += delta[key]
            for call in delta.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                index = int(call.get("index") or 0)
                slot = slots.setdefault(index, _ToolSlot())
                if isinstance(call.get("id"), str) and call["id"]:
                    slot.call_id = call["id"]
                function = call.get("function")
                if isinstance(function, dict):
                    if isinstance(function.get("name"), str):
                        slot.name += function["name"]
                    if isinstance(function.get("arguments"), str):
                        slot.arguments.append(function["arguments"])

    if slots:
        result.tool_calls = [slots[index].snapshot() for index in sorted(slots)]
    return result


def _request_blocking(
    base_url: str,
    api_key: str,
    payload: dict[str, Any],
    timeout: float,
) -> ChatResult:
    """同步发起一次流式请求并收拢结果（在工作线程里跑）。"""
    url = f"{base_url.rstrip('/')}/chat/completions"
    body = json.dumps({**payload, "stream": True}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("content-type", "application/json")
    request.add_header("accept", "text/event-stream")
    request.add_header("authorization", f"Bearer {api_key}")

    try:
        response = urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        try:
            text = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - 错误体读不出来时保留状态码即可
            text = ""
        raise _classify_http_error(exc.code, text, exc.headers.get("retry-after") if exc.headers else None) from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, TimeoutError):
            raise GatewayError(f"gateway request timed out after {timeout:.0f}s", code="timeout") from exc
        raise GatewayError(f"gateway unreachable: {reason}", code="transport") from exc
    except TimeoutError as exc:
        raise GatewayError(f"gateway request timed out after {timeout:.0f}s", code="timeout") from exc

    with response:
        content_type = str(response.headers.get("content-type") or "")
        # 网关对 JSON 请求偶尔整份回一个非流文档（例如管理接口的错误信封）。
        if "text/event-stream" not in content_type:
            text = response.read().decode("utf-8", "replace")
            try:
                document = json.loads(text)
            except ValueError as exc:
                raise GatewayError(f"unexpected non-stream response: {text[:_ERROR_BODY_LIMIT]}", code="malformed") from exc
            if isinstance(document, dict) and isinstance(document.get("error"), dict):
                raise _classify_http_error(200, text, None)
            return _from_document(document)
        return _consume_stream(response, ChatResult())


def _from_document(document: Any) -> ChatResult:
    """把一个非流式的 chat.completion 文档折成同一个结果形状。"""
    result = ChatResult()
    if not isinstance(document, dict):
        return result
    result.response_id = str(document["id"]) if document.get("id") else None
    result.model = str(document["model"]) if document.get("model") else None
    usage = document.get("usage")
    if isinstance(usage, dict):
        result.usage = usage
    choices = document.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return result
    first = choices[0]
    result.finish_reason = str(first["finish_reason"]) if first.get("finish_reason") else None
    message = first.get("message")
    if not isinstance(message, dict):
        return result
    content = message.get("content")
    if isinstance(content, str):
        result.content = content
    reasoning = message.get("reasoning_content") or message.get("reasoning")
    if isinstance(reasoning, str):
        result.reasoning = reasoning
    calls = message.get("tool_calls")
    if isinstance(calls, list):
        result.tool_calls = [call for call in calls if isinstance(call, dict)]
    return result


async def chat_completion(
    base_url: str,
    api_key: str,
    payload: dict[str, Any],
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> ChatResult:
    """异步包装：在工作线程里跑标准库的阻塞请求。

    Args:
        base_url: 形如 `http://127.0.0.1:18900/v1`。
        api_key: sidecar 的 `forwardKey`。
        payload: OpenAI Chat Completions 请求体（`stream` 会被强制为 true）。
        timeout: 整个收流过程的上限秒数。

    Returns:
        ChatResult: 收拢后的完整结果。

    Raises:
        GatewayError: 传输、状态码或流内失败。
    """
    return await asyncio.wait_for(
        asyncio.to_thread(_request_blocking, base_url, api_key, payload, timeout),
        timeout=timeout + 15.0,
    )


async def management_summary(base_url: str, api_key: str, *, timeout: float = 25.0) -> dict[str, Any]:
    """读独立端的管理摘要，取回 `catalog` 的富信息行。

    为什么需要它：网关的 `/v1/models` 把所有车道的 `owned_by` 统一压成
    `our-free-model`，也不输出模态；而管理摘要的每行带 `channel`（车道）、
    `vision`（能否看图）、`reasoning`、真实 `name` 与 `routable`。
    鉴权方式与推理接口相同（Bearer forwardKey），实测可用。

    返回 `{}` 表示拿不到（旧版 sidecar 或路由变更），调用方必须能降级。
    """
    root = base_url.rstrip("/")
    origin = root[: -len("/v1")] if root.endswith("/v1") else root
    try:
        document = await asyncio.wait_for(
            asyncio.to_thread(_get_json, f"{origin}/api/management/summary", api_key),
            timeout=timeout + 5.0,
        )
    except (GatewayError, asyncio.TimeoutError):
        return {}
    catalog_rows = document.get("catalog") if isinstance(document, dict) else None
    if not isinstance(catalog_rows, list):
        return {}
    return {
        str(row.get("id")): row
        for row in catalog_rows
        if isinstance(row, dict) and row.get("id")
    }


async def list_models(base_url: str, api_key: str, *, timeout: float = 25.0) -> list[dict[str, Any]]:
    """拉取 `/models`，返回原始模型行（含 `x_ofm_efforts` 等扩展字段）。"""
    document = await asyncio.wait_for(
        asyncio.to_thread(_get_json, f"{base_url.rstrip('/')}/models", api_key),
        timeout=timeout + 5.0,
    )
    rows = document.get("data") if isinstance(document, dict) else None
    return [row for row in rows or [] if isinstance(row, dict)]


def _get_json(url: str, api_key: str) -> dict[str, Any]:
    request = urllib.request.Request(url, method="GET")
    request.add_header("accept", "application/json")
    request.add_header("authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            return json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        text = ""
        try:
            text = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            pass
        raise _classify_http_error(exc.code, text, None) from exc
    except urllib.error.URLError as exc:
        raise GatewayError(f"gateway unreachable: {getattr(exc, 'reason', exc)}", code="transport") from exc
    except ValueError as exc:
        raise GatewayError(f"gateway returned a non-JSON document: {exc}", code="malformed") from exc
