"""MaiBot Context Item 与 OpenAI Chat Completions 之间的双向投影。

这个模块只处理数据形状，不做任何网络请求。

## 为什么不能照文档写

MaiBot 官方插件文档写的是 `message_list`，但那已经落后于实现。Host 侧
`src/llm_models/model_client/plugin_client.py` 把请求交给
`serialize_response_request_snapshot()`，实际发到 Runner 的字段是
`context_items` + `item_schema_version`。以文档为准会拿到空消息列表，
并且不会报错——静默失败最难查，所以这里以主仓库源码为准。

## 出站（Host → 上游）

请求侧的 Item 类型见 `payload_content/context_snapshot.py`：

- `SystemMessageItem` / `UserMessageItem` / `AssistantMessageItem`：`parts`
  里是 `{type:text|image|refusal}` 的片段；
- `ReasoningItem`：模型自己的思考，回放下游没有对应槽位，丢弃；
- `FunctionCallItem`：一次工具调用，`tool_call.args` 是**字典**；
- `FunctionCallOutputItem`：一次工具结果，靠 `call_id` 关联；
- `ProviderActivityItem` / `ProviderOpaqueItem`：宿主侧活动投影，上游不认，丢弃。

OpenAI 形状要求把连续的多个 `FunctionCallItem` 合并进**同一条**
assistant 消息的 `tool_calls` 数组，工具结果则是各自独立的
`{"role":"tool"}` 消息。顺序错了、孤儿结果没剔掉，严格的上游直接 400。

## 入站（上游 → Host）

返回两种形状都可以，但**只有 `output_items` 是长期的**：Host 见到旧的
`content`/`tool_calls` 标量形状会打一条 warning，并声明下一个 Item schema
版本就删除该兼容入口。所以这里直接产出规范 Item 数组，附带
`item_schema_version`——缺这个或版本不对会抛 `RespParseException`。

`tool_calls[].function.arguments` 从网关拿到的是 **JSON 字符串**，而 Host 的
`ContextToolCall` 要求 `args` 是**字典**（`materialize_args()` 直接
`json.loads`，非字典就抛「工具参数必须是 JSON object」）。不转换的话
参数会静默变空，模型收到空调用结果，整轮逻辑跑偏——这是最容易踩的坑。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

#: Host 当前的 Item schema 版本（`context_item.CONTEXT_ITEM_SCHEMA_VERSION`）。
#: 必须逐字回传给 Host，否则 `RespParseException`。
ITEM_SCHEMA_VERSION = 1

#: `ContextImagePart` 接受的格式白名单（`context_item.SUPPORTED_IMAGE_FORMATS`）。
SUPPORTED_IMAGE_FORMATS = ("jpg", "jpeg", "png", "webp", "gif")

TEXT_PART = "text"
IMAGE_PART = "image"
REFUSAL_PART = "refusal"


class ProtocolError(ValueError):
    """请求形状不合法，或上游返回无法投影成合法 Item。"""


def _now_iso() -> str:
    """Host 用 `datetime.fromisoformat` 解析，必须是非空 ISO 字符串。"""
    return datetime.now().isoformat()


def _meta(logical_turn_id: str | None) -> dict[str, Any]:
    """Item 关系元数据。

    `logical_turn_id` 的键**必须存在**（可为 null）：Host 的
    `_deserialize_item_meta` 对缺键直接报错，对 null 才放行。
    """
    return {
        "item_id": uuid.uuid4().hex,
        "logical_turn_id": logical_turn_id,
        "timestamp": _now_iso(),
    }


def _parts_text(parts: Any) -> str:
    """把 parts 里的文本片段拼成一段字符串。"""
    chunks: list[str] = []
    for part in parts or []:
        if not isinstance(part, dict):
            continue
        if part.get("type") == TEXT_PART and isinstance(part.get("text"), str):
            chunks.append(part["text"])
        elif part.get("type") == REFUSAL_PART and isinstance(part.get("refusal"), str):
            # 拒答文本是模型上一轮的输出内容，回放时按正文处理，
            # 否则历史里会出现一条完全空的消息。
            chunks.append(part["refusal"])
    return "".join(chunks)


def _parts_images(parts: Any) -> list[dict[str, Any]]:
    """把 parts 里的图片片段转成 OpenAI 的 `image_url` 数据 URI。"""
    images: list[dict[str, Any]] = []
    for part in parts or []:
        if not isinstance(part, dict) or part.get("type") != IMAGE_PART:
            continue
        raw = part.get("image_base64")
        fmt = str(part.get("image_format") or "").strip().lower()
        if not isinstance(raw, str) or raw == "":
            continue
        if fmt == "jpg":
            fmt = "jpeg"
        if fmt not in SUPPORTED_IMAGE_FORMATS:
            # Host 的白名单外格式进不了 ContextImagePart，上游更不认；
            # 跳过并留痕，比让整轮请求 400 好。
            continue
        images.append({"type": "image_url", "image_url": {"url": f"data:image/{fmt};base64,{raw}"}})
    return images


def _user_content(text: str, images: list[dict[str, Any]]) -> Any:
    """用户消息内容：纯文本给字符串，带图给多模态数组。

    空文本 + 有图时必须只给图；两者都空的消息上游会拒。
    """
    if not images:
        return text
    blocks: list[dict[str, Any]] = []
    if text:
        blocks.append({"type": "text", "text": text})
    blocks.extend(images)
    return blocks


def project_context_items(
    items: Any,
    *,
    allow_images: bool = True,
) -> tuple[str | None, list[dict[str, Any]], list[str]]:
    """把 Host 的 `context_items` 投影成 OpenAI Chat 请求形状。

    Args:
        items: `request["context_items"]`，一个 Item 快照列表。
        allow_images: 目标模型是否接受图片输入。为假时剔除图片片段，
            并在 warnings 里留痕（比整轮 400 好）。

    Returns:
        (system_prompt, messages, warnings)
        system_prompt 为拼接后的系统提示（无则 None）；messages 不含
        system 角色，避免同一条提示在两个位置重复出现。
    """
    if items is None:
        raise ProtocolError("请求缺少 context_items")
    if not isinstance(items, list):
        raise ProtocolError(f"context_items 必须是列表，实际是 {type(items).__name__}")

    systems: list[str] = []
    messages: list[dict[str, Any]] = []
    warnings: list[str] = []
    # 已写出的调用 id，用于识别「有结果、无调用」的孤儿工具输出。
    issued: set[str] = set()
    # 正在累积 tool_calls 的 assistant 消息；遇到任何别的角色就封箱。
    open_call_frame: dict[str, Any] | None = None
    # 待落地的 assistant 正文。Hold 住不立即发出，是因为同一轮里它可能紧跟
    # 一个 FunctionCallItem —— OpenAI 要求正文与 tool_calls 落在**同一条**
    # 消息里，先发出去就会拆成两条（严格上游按两条历史处理时报 400）。
    pending_assistant: str | None = None

    def flush_assistant() -> None:
        """把待落地正文并入已打开的调用帧，或单独发一条消息。"""
        nonlocal pending_assistant, open_call_frame
        if pending_assistant is None:
            return
        text, pending_assistant = pending_assistant, None
        if open_call_frame is not None:
            existing = open_call_frame.get("content") or ""
            open_call_frame["content"] = f"{existing}\n{text}".strip() if existing else text
            return
        if text.strip():
            messages.append({"role": "assistant", "content": text})

    def seal_frame() -> None:
        nonlocal open_call_frame
        if open_call_frame is not None:
            messages.append(open_call_frame)
            open_call_frame = None

    def image_blocks(parts: Any) -> list[dict[str, Any]]:
        found = _parts_images(parts)
        if not found:
            return []
        if not allow_images:
            warnings.append(f"dropped {len(found)} image part(s): model has no vision input")
            return []
        return found

    for raw in items:
        if not isinstance(raw, dict):
            warnings.append(f"skipped non-object context item: {type(raw).__name__}")
            continue
        item_type = str(raw.get("item_type") or "")

        if item_type == "SystemMessageItem":
            flush_assistant()
            seal_frame()
            text = _parts_text(raw.get("parts")).strip()
            if text:
                systems.append(text)
            continue

        if item_type == "UserMessageItem":
            flush_assistant()
            seal_frame()
            text = _parts_text(raw.get("parts"))
            blocks = image_blocks(raw.get("parts"))
            content = _user_content(text, blocks)
            if content == "" or content == []:
                warnings.append("dropped empty user item")
                continue
            messages.append({"role": "user", "content": content})
            continue

        if item_type == "AssistantMessageItem":
            text = _parts_text(raw.get("parts"))
            if text.strip() == "":
                warnings.append("dropped empty assistant item")
                continue
            if pending_assistant is not None:
                pending_assistant = f"{pending_assistant}\n{text}"
            else:
                pending_assistant = text
            continue

        if item_type == "ReasoningItem":
            # 模型的历史思考不回放给上游：三条上游线都没有承接它的字段，
            # 而且思考本身不是可执行的历史。
            continue

        if item_type == "FunctionCallItem":
            call = raw.get("tool_call")
            if not isinstance(call, dict):
                warnings.append("dropped function call item without tool_call")
                continue
            call_id = str(call.get("call_id") or "")
            name = str(call.get("func_name") or "")
            if call_id == "" or name == "":
                warnings.append("dropped function call item missing call_id or func_name")
                continue
            args = call.get("args")
            if args is None:
                args = {}
            if not isinstance(args, dict):
                warnings.append(f"coerced non-object args for {name}: {type(args).__name__}")
                args = {}
            if open_call_frame is None:
                open_call_frame = {"role": "assistant", "content": None, "tool_calls": []}
                # 帧一开就把待落地的正文放进来，保证同轮正文只有一条。
                flush_assistant()
            open_call_frame["tool_calls"].append({
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
            })
            issued.add(call_id)
            continue

        if item_type == "FunctionCallOutputItem":
            flush_assistant()
            seal_frame()
            call_id = str(raw.get("call_id") or "")
            if call_id == "":
                warnings.append("dropped tool output without call_id")
                continue
            if call_id not in issued:
                # 孤儿结果：调用方把历史截断时会出现。严格上游对
                # 「没有对应调用的 tool 消息」直接 400，只能剔掉。
                warnings.append(f"dropped orphan tool output for {call_id}")
                continue
            output = raw.get("output")
            messages.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": output if isinstance(output, str) and output != "" else "(no output)",
            })
            continue

        if item_type in ("ProviderActivityItem", "ProviderOpaqueItem"):
            # 宿主侧活动投影（内置工具、不透明续接），上游模型看不见这些。
            continue

        warnings.append(f"dropped unknown context item: {item_type or '(no item_type)'}")

    flush_assistant()
    seal_frame()
    system_prompt = "\n\n".join(systems) if systems else None
    return system_prompt, messages, warnings


def project_tools(tool_options: Any) -> list[dict[str, Any]]:
    """把 Host 的 `tool_options` 转成 OpenAI `tools`。

    Host 的 `serialize_tool_options_snapshot()` 已经输出
    `{"type":"function","function":{name,description,parameters}}`，
    与 OpenAI 一致；这里只做防御性的形状清洗，因为工具定义会被
    上游逐字比对（免费车道的指纹门禁尤其严格）。
    """
    if not tool_options:
        return []
    if not isinstance(tool_options, list):
        raise ProtocolError("tool_options 必须是列表")

    tools: list[dict[str, Any]] = []
    for option in tool_options:
        if not isinstance(option, dict):
            continue
        function = option.get("function") if isinstance(option.get("function"), dict) else None
        if function is None:
            # 扁平形状（`{type,function,name,description,parameters}` 平铺）：
            # 网关侧的 Responses 线会用这种，兼容收下。
            if isinstance(option.get("name"), str):
                function = option
            else:
                continue
        name = str(function.get("name") or "").strip()
        if name == "":
            continue
        schema = function.get("parameters")
        if not isinstance(schema, dict) or schema.get("type") != "object":
            # 空 schema 会让部分上游拒绝整份工具定义。
            schema = {"type": "object", "properties": {}}
        tools.append({
            "type": "function",
            "function": {
                "name": name,
                "description": str(function.get("description") or ""),
                "parameters": schema,
            },
        })
    return tools


def project_response_format(response_format: Any) -> dict[str, Any] | None:
    """Host 的 `RespFormat.to_dict()` → OpenAI `response_format`。

    Host 内部用的是 `{"format_type": ..., "schema": {...}}`，不是 OpenAI 的
    `{"type": ...}`。直接透传会被上游当成无效格式。
    """
    if not isinstance(response_format, dict):
        return None
    kind = str(response_format.get("format_type") or response_format.get("type") or "").strip().lower()
    if kind in ("", "text"):
        return None
    if kind == "json_object":
        return {"type": "json_object"}
    if kind == "json_schema":
        schema = response_format.get("schema")
        if not isinstance(schema, dict):
            return None
        return {
            "type": "json_schema",
            "json_schema": {
                "name": str(schema.get("name") or "response"),
                "schema": schema.get("schema") if isinstance(schema.get("schema"), dict) else {},
                **({"description": schema["description"]} if isinstance(schema.get("description"), str) else {}),
                "strict": bool(schema.get("strict", False)),
            },
        }
    return None


def _parse_arguments(raw: Any) -> dict[str, Any]:
    """网关的 `arguments`（JSON 字符串）→ Host 要求的字典。

    解析失败退回空字典并留痕，绝不要把原始字符串塞进 `args`：Host 的
    `materialize_args()` 只认字典，塞字符串会让整轮请求在响应解析阶段炸掉。
    """
    if isinstance(raw, dict):
        return raw
    if raw is None or raw == "":
        return {}
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _message_of(message: Any) -> tuple[str | None, list[dict[str, Any]]]:
    """从一条上游响应里取（正文, 工具调用列表）。"""
    if not isinstance(message, dict):
        return None, []
    content = message.get("content")
    if isinstance(content, list):
        # 少数上游把正文拆成 parts 数组返回。
        content = "".join(
            str(part.get("text") or "") for part in content if isinstance(part, dict) and part.get("type") == TEXT_PART
        ) or None
    calls = message.get("tool_calls")
    return (content if isinstance(content, str) else None), (calls if isinstance(calls, list) else [])


def build_output_items(
    *,
    content: str | None,
    reasoning: str | None,
    tool_calls: Any,
    logical_turn_id: str | None,
) -> list[dict[str, Any]]:
    """把一次网关响应整理成 Host 的 `output_items`。

    顺序固定为 思考 → 正文 → 工具调用，与 Host 自己的
    `build_portable_output_items()` 一致：`FunctionCallItem` 的
    `logical_turn_id` 必须非空，正文/思考在关系校验里允许为空。
    全部为空时返回 `[]`，交给上层决定是否算失败响应。
    """
    turn = logical_turn_id or uuid.uuid4().hex
    items: list[dict[str, Any]] = []

    thinking = str(reasoning or "").strip()
    if thinking:
        items.append({
            "item_type": "ReasoningItem",
            "meta": _meta(turn),
            "representation": "raw_text",
            "summary_parts": [],
            "text_parts": [thinking],
        })

    body = str(content or "").strip()
    if body:
        items.append({
            "item_type": "AssistantMessageItem",
            "meta": _meta(turn),
            "parts": [{"type": TEXT_PART, "text": body}],
        })

    if isinstance(tool_calls, list):
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            function = call.get("function") if isinstance(call.get("function"), dict) else {}
            name = str(function.get("name") or call.get("name") or "").strip()
            if name == "":
                continue
            call_id = str(call.get("id") or call.get("call_id") or "").strip() or f"call_{uuid.uuid4().hex[:12]}"
            items.append({
                "item_type": "FunctionCallItem",
                "meta": _meta(turn),
                "tool_call": {
                    "call_id": call_id,
                    "func_name": name,
                    "args": _parse_arguments(function.get("arguments", call.get("arguments"))),
                },
            })
    return items


def map_usage(raw_usage: Any) -> dict[str, Any] | None:
    """网关 usage → Host `UsageRecord` 认的字段。

    Host 读 `prompt_tokens` / `completion_tokens` / `total_tokens` /
    `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`。网关给的是
    OpenAI 形状，缓存命中在 `prompt_tokens_details.cached_tokens`，
    未命中要用总量去减。
    """
    if not isinstance(raw_usage, dict):
        return None
    prompt = int(raw_usage.get("prompt_tokens") or 0)
    completion = int(raw_usage.get("completion_tokens") or 0)
    total = int(raw_usage.get("total_tokens") or 0) or (prompt + completion)
    details = raw_usage.get("prompt_tokens_details")
    cached = int(details.get("cached_tokens") or 0) if isinstance(details, dict) else 0
    usage: dict[str, Any] = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
    }
    if cached:
        usage["prompt_cache_hit_tokens"] = cached
        usage["prompt_cache_miss_tokens"] = max(0, prompt - cached)
    return usage
