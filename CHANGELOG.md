# 鲸语汇

## 0.1.0

首个可运行版本：

- 从 `astrbot_plugin_slang` 移植 `store` / `service` / `matcher` / `index` 四个纯逻辑模块
- 重写 `miner`：改用 qq-bot-lite 的 LLM 客户端，保留原提示词与两道过滤
- 新的对接层 `plugin.py`：缓冲群聊、按批学习、命中注入
- 精简面板接口：data / list / tree / approve / learn
