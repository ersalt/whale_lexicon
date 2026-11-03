"""从聊天里提取黑话，再用一次模型自审。

两段提示词沿用原插件（`astrbot_plugin_slang`）打磨过的版本：
先让模型按「真实出现过、只有这个群才懂」提取，再逐条判断「含义是否具体」
和「用法是否稳定」，两道都过才收。
"""

from __future__ import annotations

import json
import re

EXTRACT_PROMPT = """你在整理一个 QQ 群的黑话表。下面是从这个群抓的一段聊天。

只输出 JSON 数组，不要解释，不要 markdown 代码块。没有黑话就输出 []。
每个元素三个字段：
{"name":"黑话本身","aliases":"同义叫法，逗号分隔，没有就空字符串","meaning":"它在这个群里指什么，一句话","evidence":"原文里最能说明用法的一句，20 字内"}

要求：
- name 2~12 个字，必须是原文里真实出现过的
- 只挑群里自造、或只有这个群才懂的词；普通词、人名、@、表情不要
- 一次性的玩笑、口误、临时造词不要收
- 如果是群友的昵称或 ID，含义就写「群友 XXX 的 ID」，不要写「某个人的代称」
- 最多 {n} 条

聊天：
{chat}

输出："""

REVIEW_PROMPT = """判断下面每条值不值得收录成「群黑话」。两个标准都要过：

一、含义要具体
- 具体：说得出它指的人/物/事/动作，例如「指群主的小号」「指把图 P 得很糊」
- 空泛：同义反复或泛指，例如「某种特定事物」「群内的代称」「一种说法」「有冲击力的风格」

二、用法要稳定
- 稳定：群里会反复用，脱离这句话也懂
- 一次性：只是这次聊天里的玩笑、口误、临时造词

只要有一条不达标，certain 就是 false。
注意：「群友 XXX 的 ID」「指群主的小号」这种算具体，可以通过；「某个人的代称」不算。

只输出 JSON 数组，不要解释：[{{"name":"X","certain":true,"reason":"不超过20字"}}]

条目：
{items}

输出："""

_FENCE = re.compile(r"```(?:json)?\s*|\s*```")

# 出现这些词基本等于没解释，直接判不通过（不依赖模型自觉）
VAGUE_WORDS = (
    "特定事物",
    "某种特定",
    "某种东西",
    "群内的代称",
    "一类说法",
    "特定的代称",
    "某些事物",
    "之类的说法",
    "某种冲击",
    "一种感觉",
)

_ID_WORDS = r"(?:QQ\s*)?(?:ID|id|Id|账号|号码|号)"


def parse_json(text: str):
    if not text:
        return None
    cleaned = _FENCE.sub("", text).strip()
    for opener, closer in (("[", "]"), ("{", "}")):
        start, end = cleaned.find(opener), cleaned.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                continue
    return None


def clean_name(value: str) -> str:
    return (value or "").strip().strip("「」\"'“”《》")[:12]


def clean_meaning(value: str, limit: int) -> str:
    text = " ".join((value or "").split())
    if len(text) > limit:
        text = text[:limit].rstrip("，。；; ") + "…"
    return text


def parse_words(raw) -> set[str]:
    if not raw:
        return set()
    return {part.strip().casefold() for part in re.split(r"[,，]+", str(raw)) if part.strip()}


def is_low_value(name: str, meaning: str) -> bool:
    """「群友 XXX 的 ID」这类同义反复，没有新信息。"""
    name = (name or "").strip()
    text = re.sub(r"\s+", " ", (meaning or "").strip())
    if not name or not text:
        return False
    escaped = re.escape(name)
    patterns = [
        rf"^(?:群友\s*)?{escaped}\s*(?:[（(][^）)]*[）)])?\s*(?:的\s*)?{_ID_WORDS}$",
        rf"^群友\s*{escaped}\s*$",
    ]
    return any(re.match(pattern, text) for pattern in patterns)


class Miner:
    def __init__(self, llm, settings: dict):
        self.llm = llm
        self.max_meaning = int(settings.get("max_meaning_chars") or 200)
        self.extract_max = int(settings.get("extract_max") or 5)
        self.reject_words = parse_words(settings.get("reject_words") or "")

    async def _chat(self, prompt: str, timeout: float | None = None) -> str:
        messages = [
            {"role": "system", "content": "你是中文群聊语料整理助手，只输出 JSON。"},
            {"role": "user", "content": prompt},
        ]
        return await self.llm.complete(messages)

    async def learn(self, messages: list[dict]) -> list[dict]:
        """提取 + 自审，返回可以直接入库的条目。"""
        chat = "\n".join(f"{item['sender']}: {item['text']}" for item in messages)[-4000:]
        if not chat.strip():
            return []
        raw = await self._chat(EXTRACT_PROMPT.replace("{n}", str(self.extract_max)).replace("{chat}", chat))
        data = parse_json(raw)
        if not isinstance(data, list):
            return []

        candidates: list[dict] = []
        seen: set[str] = set()
        for item in data:
            if not isinstance(item, dict):
                continue
            name = clean_name(item.get("name"))
            meaning = clean_meaning(item.get("meaning"), self.max_meaning)
            if not name or not meaning:
                continue
            low = name.casefold()
            if not (2 <= len(name) <= 12) or low in seen or low in self.reject_words:
                continue
            if low not in chat.casefold():
                continue  # 原文里没出现过，模型编的
            if is_low_value(name, meaning) or any(word in meaning for word in VAGUE_WORDS):
                continue
            seen.add(low)
            candidates.append(
                {
                    "name": name,
                    "aliases": clean_meaning(item.get("aliases", ""), 60),
                    "meaning": meaning,
                    "evidence": clean_meaning(item.get("evidence", ""), 40),
                }
            )
        if not candidates:
            return []

        # 第二遍：让模型自己按「具体 + 稳定」过一遍
        lines = json.dumps(
            [{"name": c["name"], "meaning": c["meaning"]} for c in candidates], ensure_ascii=False
        )
        verdict = parse_json(await self._chat(REVIEW_PROMPT.replace("{items}", lines)))
        if not isinstance(verdict, list):
            return candidates[:2]
        certain = {
            str(item.get("name", "")).casefold()
            for item in verdict
            if isinstance(item, dict) and item.get("certain")
        }
        kept = [c for c in candidates if c["name"].casefold() in certain]
        return kept[: self.extract_max]
