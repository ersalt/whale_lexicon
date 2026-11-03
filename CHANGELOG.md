# 鲸语汇

## 0.2.0

面板按原插件（`astrbot_plugin_slang`）的结构重做，另外补上梗百科：

- 侧栏：待审 / 黑话库 / 手动 / 梗百科四个分类，各自带条数，可按群展开
  （群名从消息里顺手记下来，没有群名就显示群号）
- 主区：排序（最新 / 次数）、搜索（`/` 聚焦）、手动录入、批量通过/删除、分页
- 词条行：名称 | 释义 + 关键词 + 来源/群/次数/时间 | 通过、修改、删除
  （删除是行内二次确认，不弹窗）
- 录入 / 修改弹窗：词条名、关键词、内容（带字数上限）、作用域
- 梗百科：移植 `wikinow.py`，拉 cnmeme.wiki 公开词条，只读浏览，手动同步
- 设置改成跳主面板的插件设置，不再重复造一个配置编辑器

接口：`ping` / `tree` / `list` / `lookup` / `add` / `update` / `delete` /
`approve` / `bulk` / `learn` / `wiki/sync`。

## 0.1.0

首个可运行版本：

- 从 `astrbot_plugin_slang` 移植 `store` / `service` / `matcher` / `index` 四个纯逻辑模块
- 重写 `miner`：改用 qq-bot-lite 的 LLM 客户端，保留原提示词与两道过滤
- 新的对接层 `plugin.py`：缓冲群聊、按批学习、命中注入
- 精简面板接口：data / list / tree / approve / learn
