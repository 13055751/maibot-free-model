# Our Free Model — MaiBot 插件

把 [dsh-our-free-model](https://github.com/Ebony-Vinyl/dsh-our-free-model) 的免费模型能力接到麦麦上：装上插件就多出一条 LLM Provider，不用注册、不用填 Key，就能用上 DeepSeek V4.1 Flash、Kimi K3 这类前沿模型。

**这是一次移植，不是一次重写。** 上游那三万行车道逻辑（含 v2 指纹门禁、十个账号渠道的登录与积分、Qoder 的 wasm 签名）以它原本的 JavaScript 实现运行在一个本机子进程里；插件只做 MaiBot 需要的形状适配。理由见文末「架构取舍」。

---

## 你需要什么

| 依赖 | 版本 | 说明 |
| --- | --- | --- |
| MaiBot | ≥ 1.3.0 | 需要插件系统的 `@LLMProvider` 能力 |
| maibot-plugin-sdk | ≥ 2.0.0 | Runner 自带 |
| **Node.js** | **≥ 22.19** | sidecar 的运行时，这是唯一的额外硬依赖 |
| Python | ≥ 3.10 | 插件本体，**只用标准库**，不装任何第三方包 |

Node 缺失是最常见的失败原因。GUI 启动的 MaiBot 常常拿不到 shell 的 PATH（nvm 装的那份尤其如此），这种情况下请在插件配置里把 `nodePath` 填成绝对路径，例如 `~/.nvm/versions/node/v24.20.0/bin/node`。

## 安装

1. 把本目录整个放进 MaiBot 的 `plugins/`：

   ```
   MaiBot/plugins/our-free-model/
     ├─ _manifest.json
     ├─ plugin.py
     ├─ ofm_bridge/
     └─ sidecar/
   ```

2. 启动 MaiBot。插件会在后台拉起 sidecar，日志里出现这一行就算通了：

   ```
   [free-model] sidecar 就绪：http://127.0.0.1:<端口>/v1（v2.5.4），27 个模型可用
   ```

3. 在聊天里发 `/ofm toml`。配置会被**切成若干段**发出（27 个模型约 7 段），按顺序整段粘进 `model_config.toml`，然后**重启 MaiBot**。

   嫌刷屏就抄文件：同一条命令还会把完整的带注释版写成 `generated_models.toml`（路径打在回复里，默认在 `data/plugins/<插件ID>/sidecar/`），复制文件内容比逐条抄消息快得多。

   这一步不能省：麦麦的插件 Provider **不能**向 WebUI 广播模型清单，模型必须写在配置文件里才能被任务引用。`base_url` 与 `api_key` 在生成的配置里是占位值 —— 真正的端口和密钥由插件在运行时向 sidecar 握手取得，你不需要（也不应该）手填。

4. 验证：`/ofm status` 看车道分布，随便挑一个 `ofm-*` 模型对话。

## 命令

| 命令 | 作用 |
| --- | --- |
| `/ofm status` | sidecar 进程、网关地址、模型数与车道分布 |
| `/ofm models` | 当前可用模型清单（含车道与思考档位） |
| `/ofm toml` | 生成可粘贴的 `model_config.toml` 配置段 |
| `/ofm restart` | 重启 sidecar（改了端口/数据目录/Node 路径之后用） |
| `/ofm login` | 打开账号渠道登录页（本机管理界面） |

清单是动态的：上游增删模型后，重跑一次 `/ofm toml` 覆盖旧配置即可。

## 能力边界（重要）

这些不是 bug，是移植的物理边界，装之前就该知道：

- **匿名免费车道随时可能撞墙。** 那条车道靠上游共享配额，`429 RATE_LIMIT` 与 `LANE_LOCKED`（出口 IP 被判滥用）是常态而非故障。换网络、换 VPN、或登录账号渠道都能绕开。**开发/验证时不要把成功断言压在它身上。**
- **不支持 embedding 与语音识别。** 这些上游车道只提供对话补全。麦麦的记忆检索需要 embedding 模型，请在 `model_config.toml` 里**另外**配一个真的 embedding 提供商。
- **EAC 协付渠道在麦麦上不存在。** 上游用它自己的宿主指纹闸门 + 桌面端密封凭据，麦麦没有对应物。已明确不移植。
- **插件内部走流式。** 麦麦的插件 Provider 不开放 Host 侧流式钩子；而且实测 sidecar 的非流式分支会丢掉思考内容，所以插件强制 `stream: true` 并把 SSE 收拢成完整结果再返回。思考内容因此是可见的。
- **WebUI 里没有本插件的设置页。** 配置走 `config.toml` / 插件配置面板的字段，运维走 `/ofm` 命令。上游那套十一页毛玻璃看板属于 dsh 宿主生态，没有搬。

## 数据与隐私

- sidecar 只监听 **127.0.0.1**，端口默认由系统分配。
- 账号渠道的凭据存在 sidecar 数据目录里的 `channel-credentials.json`，权限 `0600`。**是本机明文**，不是系统密钥库 —— 上游插件用 dsh 的宿主凭据库，独立版没有这个依赖，所以退化成受控文件权限。别把这台机器的目录分享给别人。
- 数据目录默认在插件自己的 `data/plugins/<插件ID>/sidecar/`，可在配置里改 `dataDir`。
- 用量统计留在本机，不上传。
- **Kilo 免密池**由上游免费提供，其模型卡声明 prompt 可能被记录用于改进服务。**别往这条车道发敏感内容。** 匿名车道同理。

## 一句实话

匿名免费车道的工作方式，是伪装成官方客户端去通过上游的客户端指纹校验。上游许可条款怎么看这种行为，我不知道；源仓库是公开的 MIT，实现也是公开的。**风险归使用它的人**，我不替上游做辩护，也不建议把它当成生产依赖。想要稳定，就登录账号渠道，或者用聚合车道接自己的 Key。

## 架构取舍

**为什么不把 JavaScript 重写成 Python？** 上游的渠道层是 147 个 TypeScript 文件，包含验证码接力、账号池轮换、每日积分领取和一个 wasm 签名模块。用 MaiBot 的插件 API 表达这些，等于重写一遍并且没有任何一份实现经过验证。

而上游自己已经给出了答案：仓库里的 `packages/standalone` 是一个**不依赖 dsh 宿主**的独立服务，只用 `node:*` 内置模块，零第三方依赖，已经把所有车道统一挂在一个本机 OpenAI 兼容网关上。所以正确的移植是：

```
MaiBot Runner (Python)
   └─ @LLMProvider ── ofm_bridge/  ← 只做形状适配（约 2000 行 Python）
                          │
                          └─ sidecar/ ← vendored 上游 standalone（65 个模块）
                              Node 子进程，127.0.0.1
```

`sidecar/vendor/` 由 `sidecar/vendor.mjs` 从上游检出计算 import 闭包生成。上游更新后重跑：

```bash
node sidecar/vendor.mjs ../dsh-our-free-model
```

脚本遇到任何 `@deepseek-ai/*` 导入会直接失败 —— 宿主依赖泄漏进 sidecar 是被硬性拒绝的。**不要手工编辑 `sidecar/vendor/` 里的文件。**

## 验收方式

这个插件的测试不靠自己定义"合规"，而是让 **MaiBot 主仓库的原代码**来判定：把 `context_snapshot.py` / `context_protocol.py` / `context_item.py` 原样取来，用 Host 自己的 `deserialize_context_item_snapshot()` + `validate_context_items(MODEL_OUTPUT)` 解析插件的返回值，它不点头就是失败。`_manifest.json` 也过主仓库的 `ManifestValidator`，生命周期也调 Loader 的 `_validate_sdk_plugin_contract`。

覆盖范围：协议投影与真实推理往返、病态输入（孤儿工具结果、非字典参数、未知 item 类型）、`arguments` 字符串→字典、冷启动与热复用、命令与工具、配置热更新、卸载后不留孤儿进程。

## 许可

本插件 MIT。`sidecar/vendor/` 内含第三方 MIT 代码，声明齐全见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
