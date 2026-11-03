# AGENTS.md · whale_lexicon

群黑话自学习插件。这个仓库是**源码**；机器人加载的是部署副本，两者不是同一份。

## 两个位置

```
D:\Dev\whale_lexicon                         源码仓库（改代码、发版、跑测试）
D:\Dev\qq-bot-lite\plugins\whale_lexicon     部署副本（机器人实际加载，没有 .git）
```

改完代码要同步过去，再重启机器人：

```powershell
powershell -ExecutionPolicy Bypass -File tools\deploy-to-bot.ps1
```

脚本用 `robocopy /MIR`，排除 `.git`、`tests/`、`tools/`、`docs/` 和缓存，并在副本里写
`.deployed.json`（版本 / 提交 / 时间）。**部署脚本不会重启机器人**——插件是加载时导入的，
不重启就不生效。

## 仓库长什么样

```
plugin.py           清单解析、钩子、调度、面板接口、命令（入口，保证 setup(ctx) 在）
core/miner.py       攒聊天 → 模型提取 + 自审 → 落库（纯逻辑，不 import app.*）
pages/index.html    网页面板（单文件，引主程序的 /static/basecoat.css + style.css + theme.css）
tools/              运维脚本：import_from_astrbot.py（从 AstrBot 实例搬数据）、deploy-to-bot.ps1
tests/              pytest（清单、核心逻辑）
docs/               面板截图
```

## 校验

```powershell
python -m pytest -q
python -m ruff check .
# 用主程序的 venv 也行：
# D:\Dev\qq-bot-lite\.venv\Scripts\python.exe -m pytest -q
```

CI：GitHub Actions，3.12/3.13 × Ubuntu/Windows，`ruff check` + `pytest`。

## 契约要点（主程序 docs/plugin.md 是完整版）

- `plugin.toml` 里 `api = 1`；`[[config]]` 声明设置项，`settings_fields(ctx, fields)` 可在运行时改写
  （比如把 `learn_model` 换成从 LM Studio 拉到的模型列表）。
- 注入走 `user_suffix`，**不要**塞 `system_prompt`：system 一变，KV 缓存整段失效。
- 钩子必须快进快出，网络/模型调用一律丢 `ctx.spawn` 的后台任务。
- 日志用 `ctx.log`（自动加 `[鲸语汇]` 前缀，logger 名 `plugin.whale_lexicon`），别手写前缀。
- 面板接口用**相对路径**（`./list`），返回统一 `{"ok": bool, "message": str, ...}`。
- 设置全在页面里时，`settings_fields` 返回 `[]`，主面板就不显示通用表单。

## 皮肤与主题

面板引的是主程序的静态资源，组件库是 Basecoat（本地内置）。按钮用
`class="btn"` + `data-variant="outline|ghost|destructive"` + `data-size="sm"`；
默认态不要写 `data-variant="default"`（匹配不到规则，按钮会变透明）。
主题跟随 `data-theme`，同时同步 `.dark` 类。

## 发版

- 版本只写 `plugin.toml` 的 `version`，CHANGELOG 按 `## x.y.z` 记。
- 只有 `main` 分支，无 develop；发版 = 改版本 + CHANGELOG → commit → 附注 tag → GitHub Release。
- 当前：`v0.5.0`（2026-11-04）。

## 截图与隐私

`docs/panel*.png` 用演示数据重拍：群号固定 `123456789` / `987654321`，群名用「摸鱼大本营」
「二次元研究所」。**不要**把真实群号截进去；截图里的机器人 QQ 号用 DOM 替换成 `20002`。

## 当前状态（2026-11-04）

- 面板已迁 Basecoat，主题按钮在右上角，弹窗是原生 `<dialog>`。
- 本机数据在 `D:\Dev\qq-bot-lite\data\plugin_data\whale_lexicon\jargon.db`（从 AstrBot 实例导入：
  17 手动 / 67 自动 / 131 待审 / 4207 梗百科）。
