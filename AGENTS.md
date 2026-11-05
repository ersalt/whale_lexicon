# AGENTS.md · whale_lexicon

群黑话自学习插件。本仓库是**插件开发源**；主项目 `D:\Dev\qq-bot-lite\plugins\whale_lexicon`
是随项目分发的完整内置副本（含 tests/docs，供学习和一起跑测试）。

## 两个位置

```text
D:\Dev\whale_lexicon                       插件源码（主要开发、测试、发版）
D:\Dev\qq-bot-lite\plugins\whale_lexicon   主项目内置副本（机器人实际加载）
```

- 主要开发在本仓库进行；
- 完成后把运行时源码 + tests + docs 同步到主项目副本；
- **不要直接改主项目**。宿主缺能力先写 HOST-REQUEST，交给用户转给主项目 Agent；
- 不再维护 `tools/deploy-to-bot.ps1` 这类部署脚本，也不写 `.deployed.json`。

协作规则见主项目 `docs/plugin-integration.md`；接口契约见主项目 `docs/plugin.md`；
UI 规范见主项目 `docs/ui-spec.md`。

## 仓库长什么样

```text
plugin.py           清单解析、钩子、调度、面板接口、命令（入口，保证 setup(ctx) 在）
core/miner.py       攒聊天 → 模型提取 + 自审 → 落库（纯逻辑，不 import app.*）
core/store.py       SQLite：词条、缓冲、群名、梗百科
core/service.py     去重、入库、审核、分页、匹配
core/index.py       关键词索引
core/matcher.py     命中的提示词拼装
core/wiki.py        梗百科同步
pages/index.html    网页面板（单文件，引主程序的 /static/basecoat.css + theme.css + style.css + ui.js）
tools/              运维脚本：import_from_astrbot.py（从 AstrBot 搬数据）
tests/              pytest（清单、核心逻辑）
docs/               面板截图
```

## 校验

```powershell
# 在本仓库
python -m pytest -q
python -m ruff check .

# 用主项目的 venv 跑同一套
D:\Dev\qq-bot-lite\.venv\Scripts\python.exe -m pytest tests -q
D:\Dev\qq-bot-lite\.venv\Scripts\ruff.exe check .
```

主项目 CI 也会跑内置副本的测试；本仓库的 GitHub Actions 保留作独立校验。

## 契约要点

- `plugin.toml` 里 `api = 1`（本插件没有 model-facing tools，只有钩子/命令/面板）。
- `[[config]]` 声明设置项；`settings_fields(ctx, fields)` 可运行时改写（例如从 LM Studio 拉模型列表）。
- 注入走 `user_suffix`，**不要**塞 `system_prompt`：system 一变，KV 缓存整段失效。
- 钩子必须快进快出，网络/模型调用一律丢 `ctx.spawn` 的后台任务。
- 日志用 `ctx.log`（自动加 `[鲸语汇]` 前缀，logger 名 `plugin.whale_lexicon`），别手写前缀。
- 面板接口用**相对路径**（`./list`），返回统一 `{"ok": bool, "message": str, ...}`。
- 设置全在页面里时，`settings_fields` 返回 `[]`，主面板就不显示通用表单。

## 皮肤与主题

面板引的是主程序静态资源，组件库是 Basecoat（本地内置）。骨架用主程序的
`.topbar` / `.page` / `.head` / `.tabbar` / `.card` / `.modal`；弹窗**不要**用
`.dialog`（Basecoat 自带同名组件，会把面板压到 448px）。按钮用
`class="btn"` + `data-variant="outline|ghost|destructive"` + `data-size="sm"`；
默认态不要写 `data-variant="default"`（匹配不到规则，按钮会变透明）。
下拉框引 `/static/ui.js` 的 `uiSelect`，不要手写原生 select。
通知用 `.toast-stack` / `.toast-item`，主题跟随 `data-theme`，动效只碰
transform/opacity，`prefers-reduced-motion` 下静止。

**宿主样式变化会影响插件面板**（例如 `.tabbtn` 改成铺满）；这是复用主项目皮肤的结果。
如果插件需要独立布局，只在自己的 `<style>` 里覆盖插件局部类，并先按
`docs/plugin-integration.md` 发交接。

## 发版与同步

- 版本只写 `plugin.toml` 的 `version`，CHANGELOG 按 `## x.y.z` 记。
- 只有 `main` 分支，无 develop；发版 = 改版本 + CHANGELOG → commit → 附注 tag → GitHub Release。
- 当前：`v0.6.0`。
- 同步到主项目副本时，运行时 + tests + docs 一起带；主项目再跑内置插件测试。

## 截图与隐私

`docs/panel*.png` 用演示数据重拍：群号固定 `123456789` / `987654321`，群名用「摸鱼大本营」
「二次元研究所」。**不要**把真实群号截进去；截图里的机器人 QQ 号用 DOM 替换成 `20002`。

## 当前状态（2026-11-05）

- 面板按主程序 `docs/ui-spec.md` 对齐：sticky 顶栏 + 页头 + 页内页签 + 单张白卡行式；
  分类是页签，群范围收在一张白卡里，卡片用 `.entity` + `.entity-ink`，下拉框用 `uiSelect`。
- 动效（页签 ink、面板切换、主题圆形揭示、顶栏滚动、弹窗/toast）复用主项目的令牌和曲线。
- 弹窗是原生 `<dialog class="modal">`，没有右上角 X，关闭走 footer / 遮罩 / Esc。
- 本机数据在 `D:\Dev\qq-bot-lite\data\plugin_data\whale_lexicon\jargon.db`
  （从 AstrBot 实例导入：17 手动 / 99 自动 / 199 待审 / 4207 梗百科）。
