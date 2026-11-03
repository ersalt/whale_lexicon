# 鲸语汇（whale_lexicon）

群黑话自学习插件，移植自 AstrBot 插件 `astrbot_plugin_slang`，适配 `qq-bot-lite`。

从群聊里攒消息 → 定期调本地模型提取黑话 → 模型自审（含义要具体、用法要稳定）
→ 进待审池 → 审核通过后，命中时注入到 system 提示词里。

## 为什么单开一个项目

词库、提取、审核这套逻辑跟具体机器人框架无关，塞进主仓库只会让两边都难维护。
这里是独立的 git 仓库，主项目通过 `[extensions] dirs` 登记这个目录来加载。

## 结构

```
whale_lexicon/
  plugin.toml        元数据与设置项（面板里可改）
  plugin.py          与 qq-bot-lite 对接的胶水：攒消息、调模型、注入提示词、面板接口
  core/              纯逻辑，不依赖任何机器人 API
    store.py         SQLite：词条表、缓冲区、梗百科
    service.py       业务：去重、入库、审核、匹配
    matcher.py       命中后的提示词拼装
    index.py         按首字符分桶的关键词索引
    miner.py         提取 + 自审（提示词沿用原插件打磨过的版本）
```

## 已实现

- 群聊缓冲、按批触发学习、按群隔离
- 提取 → 自审两道过滤（含「原文里没出现过就丢」「空泛释义直接判否」）
- 命中注入 system 提示词
- 面板接口：`/plugins/whale_lexicon/data|list|tree|approve|learn`

## 待办

- 网页面板（原插件的 `pages/yh/index.html` 还没迁）
- `/yh` 命令（本项目暂无命令分发）
- 梗百科同步

## 许可

MIT，沿用原作者（贰叔）的 LICENSE。
