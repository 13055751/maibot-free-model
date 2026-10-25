"""LLM Provider 的实现：把 MaiBot 的请求转成一次 sidecar 调用。

## 一条请求的旅程

    Host (PluginLLMClient)
      → operation="response", request={context_items, tool_options, …}
      → 本模块：投影成 OpenAI Chat 形状 → 强制 stream=true → 收 SSE
      → 返回 {output_items, item_schema_version, usage}

## 为什么每次请求都要 ensure_running()

sidecar 可能被外部杀掉，也可能在热重载后还没起来。endpoint 不是一个
"启动一次就永远有效"的东西：请求侧必须自己确认它还活着。`ensure_running()`
在无主时重启，在有主时只做一次 `poll()`，代价可忽略。

## 失败怎么交给 Host

Host 会把 RPC 里的 error 包成 `RespParseException`，再由 MaiBot 的重试与
模型回退策略接管。所以这里的异常信息要**可诊断**（带上车道、状态码、
是配额还是网络），而不是笼统一句"调用失败"。绝不返回一个半截的成功结果——
那会让麦麦把失败当成一次正常回答记进上下文。
"""

from __future__ import annotations

from typing import Any

from maibot_sdk import LLMProviderBase

from . import gateway, protocol
from .catalog import build_catalog, lane_label
from .manager import SidecarEndpoint, SidecarError, SidecarManager

#: 一次 turn 的默认上限（秒）。与生成的 TOML 里的 `timeout` 对齐。
DEFAULT_TURN_TIMEOUT = 300.0

#: `max_tokens` 的兜底：Host 没给、模型信息也没给时用这个值。
FALLBACK_MAX_TOKENS = 8192

#: 免费车道里撞配额是常态，给一条人类可读的解释。
_QUOTA_HINT = (
    "这条免费车道的共享配额已用完（上游按会话计数，非你的账号问题）。"
    "可换一个模型、稍后再试，或登录账号渠道（见 /ofm help）。"
)


class ProviderUnavailable(RuntimeError):
    """sidecar 起不来，本次请求无法发起。"""


class FreeModelProvider(LLMProviderBase):
    """MaiBot `@LLMProvider` 背后真正的实现。

    继承 SDK 的 `LLMProviderBase`（文档对"逻辑较多时"的推荐做法），
    `get_response()` 正是基类的抽象方法；`dispatch()` 被本类覆盖，
    因为基版只会抛一句光秃秃的 `NotImplementedError`，而这里需要告诉用户
    embedding 该去哪里配。

    Args:
        manager: sidecar 生命周期管理器。
        logger: 宿主日志（`self.ctx.logger`）。
        turn_timeout: 单次请求的收流上限秒数。
        default_effort: 模型有思考菜单但 Host 没指定时用的档位。
    """

    def __init__(
        self,
        manager: SidecarManager,
        *,
        logger: Any = None,
        turn_timeout: float = DEFAULT_TURN_TIMEOUT,
        default_effort: str | None = None,
    ) -> None:
        self.manager = manager
        self.logger = logger
        self.turn_timeout = turn_timeout
        self.default_effort = default_effort

    # ---------------------------------------------------------------- dispatch

    async def dispatch(self, operation: str, request: dict[str, Any]) -> dict[str, Any]:
        """按 Host 的 operation 分发。

        `embedding` / `audio_transcription` 明确不支持：这些上游车道只提供
        对话补全。MaiBot 的记忆检索需要 embedding 模型，必须另外配一个
        真的 embedding 提供商——让它在这里静默返回一个假向量，比报错更糟。
        """
        if operation == "response":
            return await self.get_response(request)
        if operation == "embedding":
            raise NotImplementedError(
                "Our Free Model 车道不提供 embedding；请在 model_config 里为 embedding 任务另配提供商。"
            )
        if operation == "audio_transcription":
            raise NotImplementedError("Our Free Model 车道不提供语音识别。")
        raise ValueError(f"不支持的 LLM Provider 操作类型: {operation}")

    # ---------------------------------------------------------------- response

    async def get_response(self, request: dict[str, Any]) -> dict[str, Any]:
        """一次对话补全：投影 → 调用 → 还原成 Context Item。"""
        endpoint = await self._endpoint()

        model_info = request.get("model_info") if isinstance(request.get("model_info"), dict) else {}
        model_id = str(model_info.get("model_identifier") or model_info.get("name") or "").strip()
        if model_id == "":
            raise protocol.ProtocolError("请求没有 model_identifier，无法选路")

        visual = bool(model_info.get("visual"))
        system_prompt, messages, warnings = protocol.project_context_items(
            request.get("context_items"),
            allow_images=visual,
        )
        for note in warnings[:4]:
            self._log("warning", f"{model_id}: 上下文投影 {note}")
        if messages == []:
            raise protocol.ProtocolError("上下文里没有可发送的消息（可能全被判定为无效片段）")

        payload: dict[str, Any] = {
            "model": model_id,
            "messages": messages,
            "max_tokens": _resolve_max_tokens(request, model_info),
        }
        # 系统提示走「messages 首行」，不用顶层 `system` 字段：sidecar 的三条
        # 上游线（chat / claude / responses）都能正确抬起首行 system，但只有
        # chat 线读顶层 `system`——两个都给会在 chat 模型上把同一条指令发两遍。
        if system_prompt is not None:
            payload["messages"] = [{"role": "system", "content": system_prompt}, *messages]

        tools = protocol.project_tools(request.get("tool_options"))
        if tools:
            payload["tools"] = tools
            choice = request.get("tool_choice")
            # 免费车道的上游指纹门禁会把 `tool_choice:"none"` 判成非法；
            # 需要压制工具调用时由车道侧用提示词完成，这里只透传有效值。
            if isinstance(choice, str) and choice in ("auto", "required"):
                payload["tool_choice"] = choice
            elif isinstance(choice, dict):
                payload["tool_choice"] = choice

        temperature = _resolve_temperature(request, model_info)
        if temperature is not None:
            payload["temperature"] = temperature

        response_format = protocol.project_response_format(request.get("response_format"))
        if response_format is not None:
            payload["response_format"] = response_format

        effort = _resolve_effort(request, model_info, self.default_effort)
        if effort is not None:
            payload["reasoning_effort"] = effort

        try:
            result = await gateway.chat_completion(
                endpoint.base_url,
                endpoint.api_key,
                payload,
                timeout=self.turn_timeout,
            )
        except gateway.GatewayError as exc:
            raise self._explain(exc, model_id, endpoint) from None

        items = protocol.build_output_items(
            content=result.content,
            reasoning=result.reasoning,
            tool_calls=result.tool_calls,
            logical_turn_id=request.get("logical_turn_id"),
        )
        if items == []:
            # 上游回了一个 200 但什么都没有。返回空 items 会让麦麦安静地
            # 不回话，用户无从判断是模型懒还是车道坏了，所以显式报错。
            raise protocol.ProtocolError(f"{model_id} 返回空响应（无正文、无思考、无工具调用）")

        usage = protocol.map_usage(result.usage)
        self._log(
            "info",
            f"{model_id} 完成：正文 {len(result.content)} 字，思考 {len(result.reasoning)} 字，"
            f"工具 {len(result.tool_calls)} 个，"
            + (f"token {usage['total_tokens']}" if usage else "无用量回报"),
        )
        response: dict[str, Any] = {
            "output_items": items,
            "item_schema_version": protocol.ITEM_SCHEMA_VERSION,
            "raw_data": {
                "model": result.model,
                "finish_reason": result.finish_reason,
                "id": result.response_id,
                "provider": "our-free-model-sidecar",
            },
        }
        if usage is not None:
            response["usage"] = usage
        if result.response_id:
            response["response_id"] = result.response_id
        if result.finish_reason == "length":
            # 让 Host 的诊断里看得见"被 max_tokens 截断"这件事。
            response["status"] = "incomplete"
        return response

    # ------------------------------------------------------------------ catalog

    async def list_models(self) -> list:
        """实时清单（给 `/ofm models`、`/ofm toml` 用）。

        两份数据源合并：`/v1/models` 给出可路由集合与思考档位，管理摘要给出
        车道、视觉能力与真实名字。摘要拿不到就退化成只用清单（车道按 id 推断、
        visual 保守为假），不因一次额外请求失败而让清单整体不可用。
        """
        endpoint = await self._endpoint()
        rows = await gateway.list_models(endpoint.base_url, endpoint.api_key)
        rich = await gateway.management_summary(endpoint.base_url, endpoint.api_key)
        return build_catalog(rows, rich)

    async def probe(self) -> dict[str, Any]:
        """一次不含生成成本的连通性检查，返回可视化摘要。

        这个方法**从不抛异常**：它被 `/ofm status`、`/ofm restart` 与后台
        预启动共用，那些调用点只想要一份"现在怎么样了"的报告，不该因为
        sidecar 起不来而把一条命令变成一次异常回溯。失败信息进返回值。
        """
        try:
            endpoint = await self._endpoint()
        except (SidecarError, ProviderUnavailable, OSError) as exc:
            return {"ok": False, "error": str(exc), "base_url": None, "models": 0, "lanes": {}, "visual": 0}
        try:
            models = await self.list_models()
        except gateway.GatewayError as exc:
            return {
                "ok": False,
                "error": str(exc),
                "base_url": endpoint.base_url,
                "sidecar_version": endpoint.version,
                "models": 0,
                "lanes": {},
                "visual": 0,
            }
        lanes: dict[str, int] = {}
        for model in models:
            key = lane_label(model.lane)
            lanes[key] = lanes.get(key, 0) + 1
        return {
            "ok": True,
            "base_url": endpoint.base_url,
            "sidecar_version": endpoint.version,
            "models": len(models),
            "visual": sum(1 for model in models if model.visual),
            "lanes": lanes,
            "management_url": endpoint.management_url,
        }

    # ------------------------------------------------------------------ helpers

    async def _endpoint(self) -> SidecarEndpoint:
        try:
            return await self.manager.ensure_running()
        except (SidecarError, OSError) as exc:
            raise ProviderUnavailable(str(exc)) from exc

    def _explain(self, exc: gateway.GatewayError, model_id: str, endpoint: SidecarEndpoint) -> protocol.ProtocolError:
        """把网关错误翻译成一条用户看得懂、也值得重试/不值得重试的说明。"""
        code = (exc.code or "").lower()
        detail = str(exc)
        if code == "rate_limit":
            return protocol.ProtocolError(f"{model_id} 配额耗尽：{detail}｜{_QUOTA_HINT}")
        if code in ("unauthorized", "not_found"):
            return protocol.ProtocolError(
                f"{model_id} 不可用：{detail}｜该 id 可能已被上游撤下，用 `/ofm models` 看当前清单。"
            )
        if code == "transport":
            return protocol.ProtocolError(
                f"本地 sidecar 不可达（{endpoint.base_url}）：{detail}｜用 `/ofm restart` 重启。"
            )
        if code == "timeout":
            return protocol.ProtocolError(f"{model_id} 超时：{detail}｜可把 turnTimeout 调大。")
        return protocol.ProtocolError(f"{model_id} 请求失败（{code}）：{detail}")

    def _log(self, level: str, message: str) -> None:
        handler = getattr(self.logger, level, None) if self.logger is not None else None
        if callable(handler):
            handler(message)


def _resolve_max_tokens(request: dict[str, Any], model_info: dict[str, Any]) -> int:
    """请求级 > 模型级 > 兜底。

    MaiBot 的语义是 `max_tokens` 覆盖任务配置，两个来源都可能是 None。
    """
    for candidate in (request.get("max_tokens"), model_info.get("max_tokens")):
        if isinstance(candidate, (int, float)) and candidate > 0:
            return int(candidate)
    return FALLBACK_MAX_TOKENS


def _resolve_temperature(request: dict[str, Any], model_info: dict[str, Any]) -> float | None:
    """`send_temperature = false` 时一个温度都不下发。

    部分上游模型不接受该参数，带了会 400；MaiBot 用这个开关表达这件事。
    """
    if model_info.get("send_temperature") is False:
        return None
    for candidate in (model_info.get("temperature"), request.get("temperature")):
        if isinstance(candidate, (int, float)):
            return float(candidate)
    return None


def _resolve_effort(request: dict[str, Any], model_info: dict[str, Any], fallback: str | None) -> str | None:
    """思考档位：显式 `extra_params` 优先，其次模型默认，最后插件默认。

    Host 把 `[[models]].extra_params` 同时放在 `model_info.extra_params` 与
    `request.extra_params`，两处都读，请求级赢。
    """
    for source in (request.get("extra_params"), model_info.get("extra_params")):
        if isinstance(source, dict):
            for key in ("reasoning_effort", "effort", "reasoning"):
                value = source.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
                if isinstance(value, dict) and isinstance(value.get("effort"), str) and value["effort"].strip():
                    return value["effort"].strip()
    return fallback
