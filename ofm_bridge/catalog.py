"""模型清单：把网关的 `/v1/models` 变成 MaiBot 能用的东西。

## MaiBot 的限制

内置 Provider 能向 WebUI 广播模型清单，**插件 Provider 不能**：模型必须
写在 `model_config.toml` 的 `[[models]]` 里，`api_provider` 指向某个
`[[api_providers]]` 的名字。所以这个插件必须解决"用户怎么知道该粘什么"。

答案是 `catalog toml` 命令：读实时清单，产出一段可直接粘贴的 TOML，
包括 provider 块与每个模型块。清单是动态的（上游随时增删模型），
所以这段 TOML 要能重生成，而不是抄一次用一辈子。

## 网关模型行的扩展字段

sidecar 的 `/v1/models` 除了 OpenAI 标准字段，还带：

- `context_window` — 上游真实窗口；
- `x_ofm_efforts` — 该模型声明的思考档位菜单；
- `x_ofm_effort_default` — 默认档；
- `input` — 输入模态（含 `image` 就是 VLM，可当 MaiBot 的 `visual` 模型）。

`owned_by` **不用**：网关把所有车道的它统一压成 `our-free-model`，
车道要靠 id 形状推断（见 `infer_lane`）。

MaiBot 的 `model_config` 不认这些，所以它们只用来生成正确的 TOML 字段：
`visual = true`、`max_tokens`、以及 `extra_params` 里的思考档位默认值。

## 命名规则

上游 id 里有 `/` 与 `:`（`nvidia/nemotron-3.5-super-vl:free`）。MaiBot 的
`model_identifier` 会原样发给 API，所以保留；但 `name` 是用户在
`model_task_config` 里引用的名字，必须短且不含歧义，这里压成
`ofm-<slug>`。带思考档位的模型另发 `<name>(<effort>)` 变体的说法**不**采用——
网关的 `(level)` 后缀解析是插件侧行为，写进 MaiBot 配置会让人困惑，
统一走 `extra_params.reasoning_effort` 更直白。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

#: 生成 TOML 时使用的 provider 块名字，用户在 [[models]] 的 api_provider 里引用它。
#: 这是 MaiBot 侧的标签，用户可以改成别的名字（改配置 providerName 即可）；
#: 默认取插件名，纯粹是为了在 model_config 里一眼认出来源。
DEFAULT_PROVIDER_NAME = "our-free-model"

#: 插件声明的 client_type，必须与 _manifest.json / @LLMProvider 一致。
CLIENT_TYPE = "our_free_model"

#: 单模型默认输出上限（MaiBot 的 max_tokens 语义）。
DEFAULT_MAX_TOKENS = 8192

#: 无法从清单拿到窗口时的兜底。
FALLBACK_CONTEXT_WINDOW = 131072


@dataclass(frozen=True)
class ModelRow:
    """一行规整化的模型清单。"""

    identifier: str
    name: str
    lane: str
    context_window: int
    max_output: int | None
    visual: bool
    #: 人类可读名（来自管理摘要，如 "Big Pickle"）；仅用于列表展示。
    #: `name` 才是写进 TOML 的 slug，因为 `model_task_config` 要一个
    #: 不含空格、可安全引用的键。
    display_name: str = ""
    efforts: tuple[str, ...] = ()
    effort_default: str | None = None
    description: str | None = None
    free: bool = True

    def toml_block(self, provider_name: str) -> str:
        """一个 `[[models]]` 块。

        `price_in` / `price_out` 一律 0：这些车道不收钱，填了会让 MaiBot
        的用量统计出一个永远为零的成本，反而误导。
        """
        lines = [
            "[[models]]",
            f'model_identifier = "{self.identifier}"',
            f'name = "{self.name}"',
            f'api_provider = "{provider_name}"',
            "price_in = 0.0",
            "price_out = 0.0",
            "cache_price_in = 0.0",
            f"max_tokens = {self.max_output or DEFAULT_MAX_TOKENS}",
            f"visual = {'true' if self.visual else 'false'}",
        ]
        extra: dict[str, str] = {}
        if self.effort_default is not None:
            extra["reasoning_effort"] = f'"{self.effort_default}"'
        if extra:
            rendered = ", ".join(f'{key} = {value}' for key, value in extra.items())
            lines.append(f"extra_params = {{{rendered}}}")
        return "\n".join(lines)


def _slug(text: str) -> str:
    """把上游 id 压成一个安全的 MaiBot 模型名。"""
    cleaned = re.sub(r"[^0-9A-Za-z]+", "-", text).strip("-").lower()
    return cleaned or "model"


def infer_lane(identifier: str) -> str:
    """按 id 形状推断模型属于哪条车道。

    不读 `owned_by`：网关的 `/v1/models` 把所有车道的 `owned_by` **统一压成
    `our-free-model`**（实测 27 行全是同一个值），拿它当车道等于把账号渠道
    全标成匿名池。车道信息只留在 id 形状里。

    - `combo:` 前缀 → 降级链（虚拟模型，背后是若干真实车道）；
    - 不含 `/` → 匿名免费车道（该车道自己 mint 的 id 从不带斜杠）；
    - 含 `/` 且带 `:free` / `/free` → Kilo 免密池；
    - 其余含 `/` → 账号渠道或聚合车道（`provider/model` 形状）。
    """
    if identifier.startswith("combo:"):
        return "combo"
    if "/" not in identifier:
        return "free-lane"
    if ":free" in identifier or identifier.endswith("/free"):
        return "kilo"
    # 含斜杠且不带 free 标记 → `provider/model` 形状，即账号渠道或聚合车道。
    # 不回落到 owned_by：它已被网关统一压平，用它当车道会把渠道标成匿名池。
    return "channel"


_LANE_LABELS = {
    "free-lane": "匿名免费池",
    "kilo": "Kilo 免密池",
    "combo": "降级链",
    "channel": "账号渠道/聚合",
}


def lane_label(lane: str) -> str:
    return _LANE_LABELS.get(lane, lane)


def _is_free(identifier: str, lane: str) -> bool:
    """这条车道不需要用户自备 Key 或登录。"""
    if lane == "free-lane" or lane == "kilo" or lane == "combo":
        return True
    return ":free" in identifier or identifier.endswith("/free")


#: 管理摘要的 `channel` → 本模块的车道标记。上游用 `anonymous` 表达
#: 免密免费车道，这里统一成 `free-lane` 以保持与 `infer_lane` 同一套词。
_CHANNEL_LANES = {"anonymous": "free-lane", "kilo": "kilo", "combo": "combo"}


def normalize(row: dict[str, Any], *, rich: dict[str, Any] | None = None) -> ModelRow | None:
    """把一行清单规整成 `ModelRow`，无效行返回 None。

    Args:
        row: `/v1/models` 的一行 —— 提供可路由集合与思考档位（`x_ofm_efforts`）。
        rich: 管理摘要里同 id 的那一行 —— 提供 `channel` / `vision` / 真实
            `name`。两份数据各有对方没有的字段，所以必须合并；拿不到 rich 时
            降级为按 id 推断车道，`visual` 只能退回 `input` 字段（实测网关
            不发这个键，因此会保守地判为不支持图片——宁可少标一个 VLM，
            也不要标错让用户发图后被上游拒）。
    """
    identifier = str(row.get("id") or "").strip()
    if identifier == "":
        return None
    detail = rich or {}

    channel = str(detail.get("channel") or "").strip().lower()
    lane = _CHANNEL_LANES.get(channel, "channel") if channel else infer_lane(identifier)

    if "vision" in detail:
        visual = detail["vision"] is True
    else:
        modalities = row.get("input")
        visual = isinstance(modalities, list) and "image" in modalities

    efforts_value = row.get("x_ofm_efforts")
    efforts = tuple(str(item) for item in efforts_value) if isinstance(efforts_value, list) else ()
    default = row.get("x_ofm_effort_default")
    context = detail.get("contextWindow") if isinstance(detail.get("contextWindow"), (int, float)) else row.get("context_window")
    max_output = detail.get("maxOutput") if isinstance(detail.get("maxOutput"), (int, float)) else row.get("max_tokens")
    description = row.get("description")
    return ModelRow(
        identifier=identifier,
        name=f"ofm-{_slug(identifier)}",
        display_name=_display_name(detail.get("name"), identifier),
        lane=lane,
        context_window=int(context) if isinstance(context, (int, float)) and context > 0 else FALLBACK_CONTEXT_WINDOW,
        max_output=int(max_output) if isinstance(max_output, (int, float)) and max_output > 0 else None,
        visual=visual,
        efforts=efforts,
        effort_default=str(default) if isinstance(default, str) and default else None,
        description=str(description) if isinstance(description, str) else None,
        free=_is_free(identifier, lane),
    )


def _display_name(value: Any, identifier: str) -> str:
    """模型的人类可读名（仅用于列表展示，不进 TOML 的 `name`）。

    TOML 里的 `name` 必须是 MaiBot 能安全引用的 slug —— 摘要给的名字带空格
    和大小写（"Big Pickle"），用作 `model_task_config` 的键会添麻烦。
    """
    text = str(value or "").strip()
    return text or identifier


def build_catalog(
    rows: list[dict[str, Any]],
    rich_rows: list[dict[str, Any]] | dict[str, dict[str, Any]] | None = None,
) -> list[ModelRow]:
    """规整化并按车道排序，同名模型只留第一个。

    `routable` 为 False 的行会被剔除：那是上游清单已声明、但网关明确拒绝
    路由的模型，列出去只会在第一次调用时撞墙。
    """
    if isinstance(rich_rows, list):
        index = {str(item.get("id")): item for item in rich_rows if isinstance(item, dict) and item.get("id")}
    else:
        index = rich_rows or {}

    seen: set[str] = set()
    catalog: list[ModelRow] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        identifier = str(row.get("id") or "")
        rich = index.get(identifier)
        # 摘要里的 routable 是权威判定；清单行本身来自 /v1/models，
        # 那里出现的 id 已经是可路由的，所以只在拿到 rich 时用它去伪。
        if rich is not None and rich.get("routable") is False:
            continue
        model = normalize(row, rich=rich)
        if model is None or model.identifier in seen:
            continue
        seen.add(model.identifier)
        catalog.append(model)
    order = {"free-lane": 0, "kilo": 1, "channel": 2, "combo": 3}
    catalog.sort(key=lambda item: (order.get(item.lane, 2), item.lane, item.identifier))
    return catalog


#: 一条聊天消息的安全长度。麦麦自身的回复分段约 512 字符 × 3 条，
#: 超出的尾部会被静默丢掉 —— 所以长配置必须自己切段，而不是指望宿主。
_SPLIT_LIMIT = 900


def _minimal_header(provider_name: str) -> str:
    return "\n".join([
        "# Our Free Model 精简配置：base_url / api_key 是占位值，运行时由插件向 sidecar 握手取得。",
        "[[api_providers]]",
        f'name = "{provider_name}"',
        'base_url = "http://127.0.0.1:0/v1"',
        'api_key = "sidecar"',
        f'client_type = "{CLIENT_TYPE}"',
        'auth_type = "none"',
        "timeout = 300",
    ])


def minimal_block(model: ModelRow, provider_name: str) -> str:
    """精简的 `[[models]]` 块：只留必填项，用于要发进聊天的场合。

    `price_*` 交给 MaiBot 默认（文档标注可选）。`visual` 只在真支持图时写，
    因为默认就是 false，写出来纯属噪音。
    """
    lines = [
        "[[models]]",
        f'model_identifier = "{model.identifier}"',
        f'name = "{model.name}"',
        f'api_provider = "{provider_name}"',
    ]
    if model.max_output:
        lines.append(f"max_tokens = {model.max_output}")
    if model.visual:
        lines.append("visual = true")
    if model.effort_default:
        lines.append(f'extra_params = {{reasoning_effort = "{model.effort_default}"}}')
    return "\n".join(lines)


def render_minimal_toml(catalog: list[ModelRow], *, provider_name: str = DEFAULT_PROVIDER_NAME) -> str:
    """全车道、零注释的最小可解析配置。"""
    blocks = [minimal_block(model, provider_name) for model in catalog]
    return "\n\n".join([_minimal_header(provider_name), *blocks]) + "\n"


def split_toml(text: str, *, limit: int = _SPLIT_LIMIT, label: str = "our-free-model") -> list[str]:
    """把一段配置切成能逐条发进聊天的多段。

    麦麦的回复分段器上限约 512 字符 × 3 条，而 27 个模型的配置有数千字符 ——
    当成一条消息发出去必然被截，用户拿到半份配置还以为成功了。所以这里按
    `[[models]]` 的块边界切（绝不切断一个块），保证**每段都是合法 TOML**，
    段与段拼起来也是合法 TOML。

    段序号只标在每段开头一行，不重复说明文字：27 个模型要 7 段，
    每段都塞同一段提醒等于多刷 7 行废话。

    ⚠️ 这里**不截断**。曾经加过「最多发 5 段」的上限，实测会把 27 个模型
    砍成 20 个 —— 半份配置粘进 model_config 后，缺的那几个模型在任务里
    直接找不到，比让用户手动翻文件更难排查。宁可多发两段。
    """
    chunks: list[str] = []
    current = ""
    for block in text.split("\n\n"):
        piece = f"{block}\n\n"
        # provider 块与后续模型块都要保证整块不跨段。
        if current and len(current) + len(piece) > limit:
            chunks.append(current.rstrip("\n"))
            current = piece
        else:
            current += piece
    if current.strip():
        chunks.append(current.rstrip("\n"))
    if len(chunks) == 1:
        return chunks
    total = len(chunks)
    marked = [f"── 第 {index}/{total} 段 ──\n{chunk}" for index, chunk in enumerate(chunks, start=1)]
    marked[0] = f"【{label} 配置 · 共 {total} 段，按顺序整段粘贴，勿调换勿漏段】\n{marked[0]}"
    return marked


def provider_block(provider_name: str, *, timeout: int = 300) -> str:
    """`[[api_providers]]` 块。

    `base_url` 与 `api_key` 是**占位**——真正的地址与密钥由插件在运行时
    从 sidecar 握手信息里取，`api_providers` 里的这两项只是 MaiBot 的
    必填字段。这一点必须在生成的注释里写清楚，否则用户会以为要手填。

    说明一律**单独成行**放在键上方，不做行尾注释：行尾对齐要数空格，
    而中文是全角宽度，怎么补都会在等宽字体里错位。
    """
    return "\n".join([
        "[[api_providers]]",
        "# base_url / api_key 是占位值：真正的端口与密钥由插件在运行时向 sidecar 握手取得，不用手填。",
        f'name = "{provider_name}"',
        'base_url = "http://127.0.0.1:0/v1"',
        'api_key = "sidecar"',
        "# client_type 必须与插件声明一致，改了插件就接不到请求。",
        f'client_type = "{CLIENT_TYPE}"',
        "# 鉴权由插件内部完成，这里保持 none。",
        'auth_type = "none"',
        "# 思考模型一轮可能很久。",
        f"timeout = {timeout}",
        "max_retry = 2",
        "# 思考内容按原生字段解析，不靠标签猜测。",
        'reasoning_parse_mode = "native"',
    ])


def render_toml(
    catalog: list[ModelRow],
    *,
    provider_name: str = DEFAULT_PROVIDER_NAME,
    only_free: bool = False,
    include_visual_hint: bool = True,
) -> str:
    """生成一整段可粘贴的 TOML。

    Args:
        catalog: `build_catalog()` 的结果。
        provider_name: 写进 `api_provider` 的服务商名。
        only_free: 只要不收 Key 的车道（匿名免费 + Kilo + 已登录的账号渠道）。
        include_visual_hint: 给 VLM 行加注释，方便用户挑 `vlm` 任务模型。
    """
    rows = [model for model in catalog if not only_free or model.free]
    header = "\n".join([
        "# ── Our Free Model（MaiBot 插件）自动生成 ─────────────────────",
        "# 粘贴到 model_config.toml。base_url / api_key 是占位值：请求由插件",
        "# 转发给本地 sidecar，真正的端口与密钥在运行时握手取得。",
        "# 清单是动态的，上游增删模型后重新运行 `/ofm toml` 覆盖即可。",
        f"# 共 {len(rows)} 个模型。",
        "",
        provider_block(provider_name),
    ])
    blocks = []
    for model in rows:
        note = f"# ── {lane_label(model.lane)} ｜窗口 {model.context_window}"
        if model.efforts:
            note += f" ｜思考档 {'/'.join(model.efforts)}"
        if include_visual_hint and model.visual:
            note += " ｜VLM，可作 vlm 任务模型"
        blocks.append(f"{note}\n{model.toml_block(provider_name)}")
    return "\n\n".join([header, *blocks]) + "\n"


def summary_lines(catalog: list[ModelRow], *, limit: int = 40) -> list[str]:
    """给聊天窗口用的紧凑清单（TOML 太长，不适合直接刷屏）。"""
    lines: list[str] = []
    for model in catalog[:limit]:
        flags = []
        if model.visual:
            flags.append("图")
        if model.effort_default:
            flags.append(f"思考:{model.effort_default}")
        if not model.free:
            flags.append("需Key")
        suffix = f"  [{', '.join(flags)}]" if flags else ""
        shown = model.display_name or model.identifier
        lines.append(f"{model.name}  ← {shown}  ({lane_label(model.lane)}){suffix}")
    if len(catalog) > limit:
        lines.append(f"… 另有 {len(catalog) - limit} 个，用 `/ofm toml` 取完整配置")
    return lines


@dataclass
class LaneHealth:
    """按车道聚合的可用性视图，用于 `status` 输出。"""

    counts: dict[str, int] = field(default_factory=dict)
    visual: int = 0
    needs_key: int = 0

    @classmethod
    def of(cls, catalog: list[ModelRow]) -> "LaneHealth":
        health = cls()
        for model in catalog:
            health.counts[model.lane] = health.counts.get(model.lane, 0) + 1
            health.visual += 1 if model.visual else 0
            health.needs_key += 0 if model.free else 1
        return health

    def describe(self) -> str:
        if not self.counts:
            return "清单为空"
        parts = [f"{lane}×{count}" for lane, count in sorted(self.counts.items(), key=lambda item: -item[1])]
        return f"{sum(self.counts.values())} 个模型：" + "、".join(parts)
