"""Our Free Model 插件的内部桥接层。

职责边界：MaiBot 的请求形状 ↔ sidecar 的 OpenAI 形状。这一层不含上游协议
知识（那些都在 vendored 的 `sidecar/vendor/` 里，是原插件经过实测的实现），
也不含 Runner 生命周期（那在 `plugin.py`）。

- `protocol`：Context Item 双向投影
- `gateway` ：纯标准库的网关 HTTP/SSE 客户端
- `manager` ：sidecar 进程监管
- `provider`：把以上串成一个 LLM Provider
- `catalog` ：模型清单与 model_config TOML 生成
"""

__all__ = ["catalog", "gateway", "manager", "protocol", "provider"]
