"""提取 + 自审：原插件 `astrbot_plugin_slang/miner.py` 的移植版。

流程和原版一致：
1. 把待学消息按 `learn_batch_size` 切批，逐批提取（批间留出间隔，别把本地模型打满）
2. 合并同名候选、合并关键词
3. 本地过一遍硬规则：拒绝词、空泛释义、同义反复的「群友 ID」
4. 一次自审：模型逐条判「含义具体 + 用法稳定」
5. 本批出现次数 < 2 的一律不算确定；确定的直接进黑话库，其余进待审并写下理由

`learn()` 返回入库条数；全部批次都失败时返回 None，让上层保留缓冲稍后重试。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

log = logging.getLogger(__name__)

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

SYSTEM_PROMPT = "你是中文群聊语料整理助手，只输出 JSON。"

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
    """拒绝词表：只按中英文逗号分隔，空格和顿号保留在词里。"""
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


def merge_keywords(a: str, b: str) -> str:
    out: list[str] = []
    for raw in (a, b):
        for part in re.split(r"[,，、;；\s]+", raw or ""):
            part = part.strip()
            if part and part not in out:
                out.append(part)
    return ",".join(out)[:300]


class Miner:
    """一次学习 = 分批提取 + 合并 + 本地过滤 + 一次自审 + 落库。"""

    def __init__(self, llm, settings: dict, service):
        self.llm = llm
        self.svc = service
        self.max_meaning = max(20, int(settings.get("max_meaning_chars") or 200))
        self.extract_max = max(1, int(settings.get("extract_max") or 5))
        self.reject_words = parse_words(settings.get("reject_words") or "")
        self.batch_size = max(3, int(settings.get("learn_batch_size") or 15))
        self.batch_delay = max(0, int(settings.get("learn_batch_delay_ms") or 600)) / 1000
        self.batch_timeout = max(30, int(settings.get("learn_batch_timeout") or 300))
        self.model = str(settings.get("learn_model") or "").strip()
        # 提取默认关思考、自审默认保持思考，和原插件一致
        hint = self._model_hint()
        self.extract_mode = _think_mode(settings.get("extract_thinking"), "auto")
        self.review_mode = _think_mode(settings.get("review_thinking"), "off")
        self.extract_extra = resolve_thinking(
            self.extract_mode, settings.get("extract_extra_body"), hint)
        self.review_extra = resolve_thinking(
            self.review_mode, settings.get("review_extra_body"), hint)
        self._no_think_blocked: set[str] = set()

    # ---------- 模型 ----------

    def _model_hint(self) -> str:
        cfg = getattr(self.llm, "cfg", None)
        return " ".join(str(getattr(cfg, key, "") or "") for key in ("model", "base_url"))

    def _override(self, extra: dict) -> dict | None:
        data = dict(extra or {})
        if self.model:
            data["model"] = self.model  # 学习单独用一个小模型
        return data or None

    # ---------- 调模型 ----------

    async def _call(self, prompt: str, extra: dict) -> str:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
        text = await asyncio.wait_for(
            self.llm.complete(messages, override=self._override(extra),
                              timeout=self.batch_timeout),
            timeout=self.batch_timeout + 5,
        )
        return (text or "").strip()

    async def _chat(self, prompt: str, extra: dict | None, mode: str) -> str:
        try:
            return await self._call(prompt, extra or {})
        except asyncio.TimeoutError:
            log.warning("[鲸语汇] 模型调用超时（%ss），本批跳过", self.batch_timeout)
            return ""
        except Exception as exc:  # noqa: BLE001
            # 关思考的参数后端不认：去掉重试一次，之后不再试
            if extra and mode not in self._no_think_blocked:
                self._no_think_blocked.add(mode)
                log.info("[鲸语汇] 模型不认「%s」的关思考参数，回退普通请求：%s", mode, exc)
                try:
                    return await self._call(prompt, {})
                except asyncio.TimeoutError:
                    log.warning("[鲸语汇] 模型调用超时（%ss），本批跳过", self.batch_timeout)
                except Exception as retry:
                    log.warning("[鲸语汇] 模型调用失败：%s", retry)
                return ""
            log.warning("[鲸语汇] 模型调用失败：%s", exc)
            return ""

    # ---------- 主流程 ----------

    async def learn(self, scope: str, messages: list[dict], on_progress=None) -> int | None:
        if not messages:
            return 0
        batches = [messages[i : i + self.batch_size]
                   for i in range(0, len(messages), self.batch_size)]
        candidates: dict[str, dict] = {}
        failed = 0
        log.info("[鲸语汇] %s 开始学习：%d 条 / %d 批", scope, len(messages), len(batches))

        for index, batch in enumerate(batches):
            text = "\n".join(f"{m['sender']}: {m['text']}" for m in batch)[-4000:]
            raw = await self._chat(
                EXTRACT_PROMPT.replace("{n}", str(self.extract_max)).replace("{chat}", text),
                self.extract_extra, self.extract_mode)
            items = parse_json(raw)
            if not isinstance(items, list):
                failed += 1
                log.warning("[鲸语汇] %s 第 %d/%d 批解析失败", scope, index + 1, len(batches))
            else:
                self._merge_candidates(candidates, items)
            if index < len(batches) - 1 and self.batch_delay:
                await asyncio.sleep(self.batch_delay)
            if on_progress is not None:
                on_progress(index + 1, len(batches))

        if failed and failed == len(batches):
            log.warning("[鲸语汇] %s 全部 %d 批失败，保留缓冲稍后重试", scope, len(batches))
            return None
        if not candidates:
            log.info("[鲸语汇] %s 本轮没提取到条目", scope)
            return 0

        kept = self._prefilter(scope, list(candidates.values()))
        if not kept:
            return 0

        chat = "\n".join(f"{m['sender']}: {m['text']}" for m in messages)[-4000:]
        verdicts = await self._review(kept)
        saved = 0
        for item in kept[: self.extract_max]:
            certain, reason = verdicts.get(item["name"], (False, "未自审"))
            freq = chat.count(item["name"])
            if freq < 2:
                certain, reason = False, f"仅出现 {freq} 次"
            elif certain and any(word in item["meaning"] for word in VAGUE_WORDS):
                certain, reason = False, "含义空泛（同义反复）"
            self.svc.submit(
                item["name"], item["meaning"], scope=scope,
                keywords=item["keywords"], evidence=item["evidence"],
                auto_note=reason, approved=certain,
                contributor=self._contributor(messages, item["name"]),
            )
            saved += 1
            log.info("[鲸语汇] %s %s：%s = %s",
                     scope, "直接入库" if certain else "进待审", item["name"], item["meaning"])
        log.info("[鲸语汇] %s 学习完成：候选 %d，入库 %d，失败批 %d",
                 scope, len(candidates), saved, failed)
        return saved

    # ---------- 内部 ----------

    def _merge_candidates(self, candidates: dict[str, dict], items) -> None:
        for item in list(items)[: self.extract_max]:
            if not isinstance(item, dict):
                continue
            name = clean_name(item.get("name"))
            meaning = clean_meaning(item.get("meaning"), self.max_meaning)
            if not name or not meaning:
                continue
            entry = {
                "name": name,
                "meaning": meaning,
                "keywords": clean_meaning(item.get("aliases"), 60),
                "evidence": clean_meaning(item.get("evidence"), 60),
            }
            current = candidates.get(name)
            if current is None:
                candidates[name] = entry
            else:
                current["keywords"] = merge_keywords(current["keywords"], entry["keywords"])
                if not current["evidence"]:
                    current["evidence"] = entry["evidence"]

    def _prefilter(self, scope: str, items: list[dict]) -> list[dict]:
        kept = []
        for item in items:
            if not (2 <= len(item["name"]) <= 12):
                continue
            if item["name"].casefold() in self.reject_words:
                log.info("[鲸语汇] %s 命中拒绝词，丢弃：%s", scope, item["name"])
                continue
            if is_low_value(item["name"], item["meaning"]):
                log.info("[鲸语汇] %s 过滤无信息量 ID 词条：%s", scope, item["name"])
                continue
            kept.append(item)
        return kept

    async def _review(self, items: list[dict]) -> dict[str, tuple[bool, str]]:
        lines = "\n".join(f"- {item['name']}：{item['meaning']}" for item in items)
        raw = await self._chat(
            REVIEW_PROMPT.replace("{items}", lines), self.review_extra, self.review_mode)
        data = parse_json(raw)
        out: dict[str, tuple[bool, str]] = {}
        if isinstance(data, list):
            for row in data:
                if not isinstance(row, dict):
                    continue
                name = clean_name(row.get("name"))
                if not name:
                    continue
                out[name] = (bool(row.get("certain")), clean_meaning(row.get("reason"), 30))
        if not out:
            log.warning("[鲸语汇] 自审结果解析失败，全部进待审")
        return out

    @staticmethod
    def _contributor(messages: list[dict], name: str) -> str:
        """最早提到这个词的群友。"""
        for message in messages:
            if name and name in (message.get("text") or ""):
                return str(message.get("sender") or "")[:32]
        return ""


THINK_MODES = ("auto", "off", "openai", "glm", "gemini", "deepseek", "custom")


def _think_mode(raw, default: str) -> str:
    mode = str(raw or default).strip().lower()
    return mode if mode in THINK_MODES else default


def resolve_thinking(mode: str, custom, model_hint: str) -> dict:
    """把「怎么关思考」翻译成请求体里那几个参数。

    跟原插件同一套映射：auto 按模型名猜，猜不到就按 OpenAI 兼容处理，
    真不认这个参数的话 `Miner._chat` 会去掉重试一次。
    """
    if mode == "off":
        return {}
    if mode == "custom":
        return _parse_extra(custom)
    if mode == "glm":
        return {"thinking": {"type": "disabled"}}
    if mode == "gemini":
        return {"gm_thinking_config": {"budget": 0}}
    if mode in ("openai", "deepseek"):
        return {"reasoning_effort": "none"}
    low = (model_hint or "").lower()
    if "glm" in low or "zhipu" in low:
        return {"thinking": {"type": "disabled"}}
    if "gemini" in low:
        return {"gm_thinking_config": {"budget": 0}}
    return {"reasoning_effort": "none"}


def _parse_extra(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        log.warning("[鲸语汇] 自定义请求参数不是合法 JSON，忽略：%s", text)
        return {}
    return value if isinstance(value, dict) else {}
