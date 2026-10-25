# 第三方许可与来源声明

本插件的 `sidecar/vendor/` 目录包含来自第三方项目的代码。全部为 MIT 许可，
其版权声明与许可条款随副本一并保留。

## 1. Our Free Model（上游 dsh 插件）

- 项目：https://github.com/Ebony-Vinyl/dsh-our-free-model
- 版权：Copyright (c) 2026 Ebony-Vinyl
- 许可：MIT — 全文见 [`sidecar/vendor/UPSTREAM-LICENSE.txt`](sidecar/vendor/UPSTREAM-LICENSE.txt)
- 移植时的版本：v2.5.9（commit `f3ecb7abcf5a0351645cfcbd638b9c7fbbcb3e97`），
  完整记录在 [`sidecar/vendor/VENDORED.json`](sidecar/vendor/VENDORED.json)

搬移的内容是上游 `packages/standalone` 的**导入闭包**（65 个模块）与其运行时
资源：匿名免费车道、Kilo 免密池、EAC 车道的上游协议实现、账号渠道的业务代码、
以及内置管理页面的构建产物。上游那些依赖 dsh 宿主的模块（插件入口、
adapter/kernel、自更新、公告流、宿主凭据库对接）**没有**被搬移 ——
vendoring 脚本在遇到任何 `@deepseek-ai/*` 导入时会直接失败。

`scripts/standalone-third-party.txt`（落成
`sidecar/vendor/UPSTREAM-THIRD-PARTY.txt`）里另含 React、React DOM、Scheduler
等构建期依赖的许可全文：`sidecar/vendor/packages/standalone/web/` 是 React 的
编译产物，即使其源码目录 `frontend/` 被裁掉，这份声明仍然随附。

## 2. channel-pack（被上游吸收的账号渠道实现）

- 上游作者：Jet（iJetLi）— https://gitee.com/iJetLi
- 版权：Copyright (c) 2026 Jet
- 许可：MIT — 全文见
  [`sidecar/vendor/vendor/channel-pack/LICENSE`](sidecar/vendor/vendor/channel-pack/LICENSE)
- 来源说明与裁剪记录见
  [`sidecar/vendor/vendor/channel-pack/NOTICE.md`](sidecar/vendor/vendor/channel-pack/NOTICE.md)
- 吸收的提交：`345f0a07b22713c0ae189ca7d8b97ec4f64626c6`（2026-10-06）

`qoder-auth-wasm.wasm` 沿用上游文件，由 Qoder 渠道按 `import.meta.url` 定位，
必须与其所在目录一同保留。

## 3. 本插件自身

`plugin.py`、`ofm_bridge/`、`sidecar/bootstrap.mjs`、`sidecar/vendor.mjs`
为本插件原创，MIT 许可，见根目录 [`LICENSE`](LICENSE)。

## 4. 与 MaiBot 的关系

MaiBot 与其插件 SDK（https://github.com/Mai-with-u/maibot-plugin-sdk）仅在
运行时被导入，不随本插件分发，因此不构成本声明的组成部分。

## 重新生成 vendored 代码

```bash
node sidecar/vendor.mjs ../dsh-our-free-model
```

脚本从上游检出计算导入闭包，并核对许可文件齐全。上游更新后重跑即可，
不要手工编辑 `sidecar/vendor/` 里的任何文件。
