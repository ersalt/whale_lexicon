"""鲸语汇：群黑话自学习插件（qq-bot-lite 版）。

同目录的 core/ 是从原 AstrBot 插件（astrbot_plugin_slang）搬过来的纯逻辑模块，
不依赖任何平台 API。这里做四件事：

- 攒消息：按群缓冲，攒够 learn_frequency 条才排队学习
- 学词：单 worker 串行，等机器人闲下来再跑，失败保留缓冲
- 注入：命中词条注入提示词；没命中就退回梗百科
- 面板：/plugins/whale_lexicon 的接口
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.sdk import Command, CommandContext, Extension, HookContext, PluginContext

from .core import wiki as wiki_sync
from .core.matcher import format_hint, format_wiki, match_wiki
from .core.miner import Miner
from .core.service import SlangError, SlangService
from .core.store import TABLES, JargonStore

log = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
VERSION = "0.3.1"

router = APIRouter()
_CONTEXT: PluginContext | None = None
_STORE: JargonStore | None = None
_SERVICE: SlangService | None = None
_BOT: "LexiconPlugin | None" = None
_MODELS: dict = {"at": 0.0, "items": []}


# ---------------- 配置读取 ----------------


def _settings() -> dict:
    return _CONTEXT.settings if _CONTEXT else {}


def _num(key: str, default: int, low: int = 0, high: int = 10**6) -> int:
    try:
        return max(low, min(high, int(_settings().get(key) or default)))
    except (TypeError, ValueError):
        return default


def _flag(key: str, default: bool = True) -> bool:
    value = _settings().get(key, default)
    return bool(default if value is None else value)


def _split_list(raw) -> list[str]:
    if not raw:
        return []
    items = raw if isinstance(raw, (list, tuple, set)) else re.split(r"[,，、;；\s]+", str(raw))
    out: list[str] = []
    for item in items:
        item = str(item).strip()
        if item and item not in out:
            out.append(item)
    return out


def _limit() -> int:
    return _num("max_meaning_chars", 200, 20, 1000)


def _fail(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "message": message}, status_code=status)


def _not_ready() -> JSONResponse:
    return _fail("插件还没就绪，看看启动日志", 503)


def _scope_label(scope: str, names: dict[str, str] | None = None) -> str:
    """作用域 → 人能看懂的名字：全局 / 群名 / 群 123456 / 私聊 789。"""
    if not scope:
        return "全局"
    names = names or {}
    if scope in names:
        return names[scope]
    kind, _, ident = scope.rpartition(":")
    if kind == "group":
        return f"群 {ident}"
    if kind == "private":
        return f"私聊 {ident}"
    return scope


def _clean_scope(value) -> str:
    scope = str(value or "").strip()
    return "" if scope in ("__global__", "global") else scope


async def _local_models(llm) -> list[str]:
    """模型服务上有哪些模型（下拉框选项）。60 秒缓存，连不上就返回空。"""
    if llm is None:
        return []
    now = time.time()
    if now - _MODELS["at"] < 60:
        return _MODELS["items"]
    try:
        items = await asyncio.wait_for(llm.list_models(), timeout=2.0)
        items = [str(item) for item in items][:50]
    except Exception:  # noqa: BLE001 - 连不上就当没有，字段退回输入框
        items = []
    _MODELS.update({"at": now, "items": items})
    return items


# ---------------- 面板接口 ----------------
# 统一返回 {ok, message, ...}；页面对 4xx 也会解析 body，不吞错误。


@router.get("/ping")
async def ping() -> JSONResponse:
    if _SERVICE is None or _STORE is None:
        return _not_ready()
    stats = _SERVICE.stats()
    bot = _BOT
    return JSONResponse({
        "ok": True,
        "rows": stats["total"],
        "manual": stats["manual"],
        "auto": stats["auto"],
        "pending": stats["pending"],
        "wiki": _STORE.wiki_count(),
        "buffer": sum(item["n"] for item in _STORE.buffer_scopes()),
        "enabled": _flag("enabled"),
        "auto_learn": _flag("auto_learn"),
        "learning": bool(bot and bot.busy),
        "limit": _limit(),
        "version": VERSION,
        "wiki_synced_at": _STORE.meta_get("wiki_synced_at") or "",
        "inject": dict(bot.inject_stats) if bot else {},
    })


@router.get("/tree")
async def tree() -> JSONResponse:
    if _SERVICE is None or _STORE is None:
        return _not_ready()
    names = _STORE.scope_names()

    def subs(scopes: list[dict]) -> list[dict]:
        return [{"scope": s["scope"], "name": _scope_label(s["scope"], names),
                 "count": s["count"]} for s in scopes]

    manual, auto, pending = _SERVICE.tree_counts()
    # 顺序跟原插件一致：手动（知识库）→ 黑话库（学来的）→ 待审（审查）→ 梗百科（兜底）
    buckets = [
        {"key": "manual", "label": "手动", "count": manual,
         "subs": subs(_SERVICE.scopes_of("manual"))},
        {"key": "auto", "label": "黑话库", "count": auto,
         "subs": subs(_SERVICE.scopes_of("auto"))},
        {"key": "pending", "label": "待审", "count": pending,
         "subs": subs(_SERVICE.pending_scopes())},
    ]
    if _flag("wiki_enable"):
        buckets.append({"key": "wiki", "label": "梗百科", "count": _STORE.wiki_count(),
                        "subs": [], "readonly": True})
    return JSONResponse({"ok": True, "total": manual + auto, "buckets": buckets})


@router.get("/list")
async def list_entries(
    bucket: str = "pending", search: str = "", scope: str = "", scoped: bool = False,
    page: int = 1, size: int = 50, sort: str = "recent",
) -> JSONResponse:
    if _SERVICE is None or _STORE is None:
        return _not_ready()
    page = max(1, int(page or 1))
    size = max(1, min(200, int(size or 50)))
    sort = "count" if sort == "count" else "recent"

    if bucket == "wiki":
        if not _flag("wiki_enable"):
            return JSONResponse({"ok": True, "total": 0, "page": 1, "size": size,
                                 "items": [], "stats": _SERVICE.stats()})
        total, rows = _STORE.wiki_query(search.strip(), page, size)
        items = [{
            "table": "wiki", "source": "wiki", "pending": False,
            "id": f"wiki:{r['id']}", "content": r["title"], "meaning": r["summary"],
            "count": 0, "scope": "", "is_global": True, "aliases": "",
            "keywords": "", "auto_note": "", "contributor": "", "evidence": "[]",
            "tags": r["tags"], "read_only": True, "updated_at": r["updated_at"],
        } for r in rows]
        return JSONResponse({"ok": True, "total": total, "page": page, "size": size,
                             "items": items, "stats": _SERVICE.stats()})

    if bucket not in ("pending", "auto", "manual"):
        return _fail(f"不认识的分类：{bucket}")
    total, items = _SERVICE.list_entries(
        bucket, search=search.strip(), scope=_clean_scope(scope), scoped=bool(scoped),
        page=page, size=size, sort=sort)
    return JSONResponse({"ok": True, "total": total, "page": page, "size": size,
                         "items": items, "stats": _SERVICE.stats()})


@router.post("/lookup")
async def lookup(body: dict | None = None) -> JSONResponse:
    """录入前查重：同一个名字在四张表里各是什么状态。"""
    if _SERVICE is None:
        return _not_ready()
    body = body or {}
    name = str(body.get("name") or "").strip()[:32]
    if not name:
        return _fail("词条名不能为空")
    scope = _clean_scope(body.get("scope"))
    return JSONResponse({"ok": True, "items": _SERVICE.lookup(name, scope)})


@router.post("/add")
async def add_entry(body: dict | None = None) -> JSONResponse:
    if _SERVICE is None or _STORE is None:
        return _not_ready()
    body = body or {}
    name = str(body.get("name") or body.get("content") or "").strip()[:32]
    meaning = str(body.get("meaning") or "").strip()
    scope = _clean_scope(body.get("scope"))
    keywords = str(body.get("keywords") or body.get("aliases") or "").strip()
    if not name or not meaning:
        return _fail("词条名和内容都要填")
    limit = _limit()
    if len(meaning) > limit:
        return _fail(f"内容 {len(meaning)}/{limit} 字，删减后再录入")
    try:
        _SERVICE.add_manual(name, meaning, scope=scope, keywords=keywords)
    except SlangError as exc:
        return _fail(str(exc))
    where = _scope_label(scope, _STORE.scope_names())
    message = f"「{name}」已录入（{where}）"
    if (_STORE.repo("auto").find(name, scope)
            or _STORE.repo("auto_pending").find(name, scope)):
        message += "；黑话库已有同名条目，未覆盖"
    return JSONResponse({"ok": True, "message": message, "stats": _SERVICE.stats()})


@router.post("/update")
async def update_entry(body: dict | None = None) -> JSONResponse:
    if _SERVICE is None:
        return _not_ready()
    body = body or {}
    old_name = str(body.get("old_name") or body.get("content") or "").strip()
    old_scope = _clean_scope(body.get("old_scope"))
    name = str(body.get("name") or "").strip()
    meaning = str(body.get("meaning") or "").strip()
    scope = _clean_scope(body["scope"]) if "scope" in body else None
    keywords = str(body.get("keywords") or "").strip() if "keywords" in body else None

    table = str(body.get("table") or "")
    if table not in TABLES:
        # 页面没带表名时按自然键找，唯一命中才改，避免改错表
        hits = _SERVICE.lookup(old_name, old_scope)
        if len(hits) != 1:
            return _fail("分不清要改哪张表，请刷新页面重试")
        table = hits[0]["table"]

    limit = _limit()
    if len(meaning) > limit:
        return _fail(f"内容 {len(meaning)}/{limit} 字，删减后再保存")
    try:
        rec = _SERVICE.update(table, old_name, old_scope, name, meaning, scope,
                              keywords=keywords)
    except SlangError as exc:
        return _fail(str(exc))
    return JSONResponse({"ok": True, "message": f"「{rec['name']}」已更新",
                         "stats": _SERVICE.stats()})


@router.post("/delete")
async def delete_entry(body: dict | None = None) -> JSONResponse:
    if _SERVICE is None:
        return _not_ready()
    body = body or {}
    name = str(body.get("name") or "").strip()
    scope = _clean_scope(body.get("scope"))
    table = str(body.get("table") or "")
    candidates = [table] if table in TABLES else list(TABLES)
    for candidate in candidates:
        try:
            _SERVICE.delete(candidate, name, scope)
        except SlangError:
            continue
        except Exception as exc:  # noqa: BLE001
            log.warning("[鲸语汇] 删除失败 %s", name, exc_info=True)
            return _fail(f"删除失败：{exc}", 500)
        return JSONResponse({"ok": True, "message": f"已删除「{name}」",
                             "stats": _SERVICE.stats()})
    return _fail(f"没找到「{name}」", 404)


@router.post("/approve")
async def approve(body: dict | None = None) -> JSONResponse:
    """待审 → 过审；已过审的再点一次就是驳回，移回待审。"""
    if _SERVICE is None:
        return _not_ready()
    body = body or {}
    name = str(body.get("name") or "").strip()
    scope = _clean_scope(body.get("scope"))
    table = str(body.get("table") or "")
    candidates = [table] if table in TABLES else ["auto_pending", "manual_pending"]
    for candidate in candidates:
        try:
            _SERVICE.approve(candidate, name, scope)
        except SlangError:
            continue
        except Exception as exc:  # noqa: BLE001
            log.warning("[鲸语汇] 审核失败 %s", name, exc_info=True)
            return _fail(f"审核失败：{exc}", 500)
        action = "通过" if candidate.endswith("_pending") else "驳回"
        return JSONResponse({"ok": True, "message": f"「{name}」已{action}",
                             "stats": _SERVICE.stats()})
    return _fail(f"没找到「{name}」", 404)


@router.post("/bulk")
async def bulk(body: dict | None = None) -> JSONResponse:
    if _SERVICE is None:
        return _not_ready()
    body = body or {}
    action = str(body.get("action") or "")
    items = body.get("items")
    if action not in ("approve", "delete") or not isinstance(items, list) or not items:
        return _fail("参数不对")
    res = _SERVICE.bulk(action, items[:200])
    done = res["approved"] if action == "approve" else res["deleted"]
    message = f"已{'通过' if action == 'approve' else '删除'} {done} 条"
    if res.get("skipped"):
        message += f"；跳过 {res['skipped']} 条（重名或参数无效）"
    return JSONResponse({"ok": True, "message": message, "result": res,
                         "stats": _SERVICE.stats()})


@router.post("/learn")
async def learn_now(body: dict | None = None) -> JSONResponse:
    """手动学一批：立刻把缓冲区里的聊天送去提取，不等自动调度。"""
    if _BOT is None:
        return _not_ready()
    if _BOT.busy:
        return _fail("正在学，等这一轮跑完")
    scope = _clean_scope((body or {}).get("scope"))
    added, groups = await _BOT.learn_now(scope or None)
    if not groups:
        return JSONResponse({"ok": True, "added": 0, "groups": 0,
                             "message": "缓冲区不够，没东西可学"})
    return JSONResponse({"ok": True, "added": added, "groups": groups,
                         "message": f"学了 {groups} 个群，新增 {added} 条"})


@router.post("/wiki/sync")
async def wiki_sync_now(body: dict | None = None) -> JSONResponse:
    """同步梗百科。第一次（库里空）自动全量，之后按水位增量。"""
    if _STORE is None:
        return _not_ready()
    if not _flag("wiki_enable"):
        return _fail("梗百科没开，去主面板的插件设置里打开")
    body = body or {}
    empty = _STORE.wiki_count() == 0
    full = bool(body.get("full")) or empty
    try:
        res = await asyncio.to_thread(
            wiki_sync.sync, _STORE, 30, 0.4, 20 if full else 40, full, log.info)
    except Exception as exc:  # noqa: BLE001 - 网络问题直接告诉用户
        log.warning("[鲸语汇] 梗百科同步失败", exc_info=True)
        return _fail(f"同步失败：{exc}", 502)
    if _BOT is not None:
        _BOT.reload_wiki_keys()
    message = (f"梗百科 {res['total']} 条（本次 {'全量' if full else '增量'} "
               f"+{res['updated']}，{res['pages']} 页）")
    return JSONResponse({"ok": True, "message": message, "wiki": res["total"],
                         "updated": res["updated"], "pages": res["pages"]})


# ---------------- 插件本体 ----------------


class LexiconPlugin(Extension):
    name = "whale_lexicon"
    # 调度器多久看一次有没有群可以学
    tick_seconds = 5.0

    def __init__(self) -> None:
        self.started = time.time()
        self._pending: dict[str, float] = {}
        self._learning: set[str] = set()
        self._cooldown: dict[str, float] = {}
        self._last_reply: dict[str, float] = {}
        self._named: set[str] = set()
        self._scheduler: asyncio.Task | None = None
        self._wiki_task: asyncio.Task | None = None
        self._wiki_running = False
        self._wiki_keys: list[tuple[str, str]] = []
        self.inject_stats = {"calls": 0, "hits": 0, "empty": 0,
                             "no_hit": 0, "blocked": 0, "last": ""}

    # ---------- 生命周期 ----------

    async def on_start(self) -> None:
        self.reload_wiki_keys()
        # 重启后把已经攒够的群重新排上队，不然要等新消息才动
        frequency = _num("learn_frequency", 50, 5, 10000)
        try:
            for row in _STORE.buffer_scopes():
                if row["n"] >= frequency:
                    self._pending[row["scope"]] = time.time()
        except Exception:  # noqa: BLE001
            log.debug("[鲸语汇] 扫描缓冲区失败", exc_info=True)
        if self._pending:
            self._ensure_scheduler()
        self._wiki_task = self._spawn(self._wiki_loop())

    async def on_stop(self) -> None:
        for task in (self._scheduler, self._wiki_task):
            if task is not None:
                task.cancel()
        self._scheduler = None
        self._wiki_task = None

    def _spawn(self, coro) -> asyncio.Task | None:
        ctx = self.ctx or _CONTEXT
        return ctx.spawn(coro) if ctx is not None else None

    # ---------- 梗百科定时同步 ----------

    async def _wiki_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(60)
                await self._maybe_sync_wiki()
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001
                log.warning("[鲸语汇] 梗百科定时任务异常", exc_info=True)

    async def _maybe_sync_wiki(self) -> None:
        if not _flag("wiki_sync_enable", False) or not _flag("wiki_enable"):
            return
        if self._wiki_running or _STORE is None:
            return
        wanted = str(_settings().get("wiki_sync_time") or "04:30").strip()
        if time.strftime("%H:%M") != wanted:
            return
        interval = _num("wiki_sync_interval_days", 7, 1, 365)
        last = float(_STORE.meta_get("wiki_synced_at") or 0)
        if last and time.time() - last < interval * 86400:
            return
        self._wiki_running = True
        try:
            res = await asyncio.to_thread(wiki_sync.sync, _STORE, 30, 0.5, 0, False, log.info)
            self.reload_wiki_keys()
            log.info("[鲸语汇] 梗百科自动同步完成：%s", res)
        finally:
            self._wiki_running = False

    # ---------- 收集 ----------

    def _blacklist(self) -> set[str]:
        return set(_split_list(_settings().get("blacklist_groups")))

    def _blocked(self, event) -> bool:
        """群消息看黑名单，私聊看 listen_private。"""
        group_id = getattr(event, "group_id", None)
        if getattr(event, "is_group", False) and group_id is not None:
            return str(group_id) in self._blacklist()
        return not _flag("listen_private", False)

    async def on_message(self, ctx: HookContext) -> None:
        if _STORE is None:
            return
        if not _flag("enabled") or not _flag("auto_learn"):
            return
        if self._blocked(ctx.event):
            return
        if str(ctx.event.user_id) == str(getattr(ctx.event, "self_id", "")):
            return  # 自己说的话不学
        text = (ctx.event.content.text or "").strip()
        if not text or text.startswith("/"):
            return
        speaker = getattr(ctx.event, "sender_name", None) or str(ctx.event.user_id or "群友")
        try:
            count = _STORE.buffer_add(ctx.session_key, speaker, text)
        except Exception:  # noqa: BLE001
            log.warning("[鲸语汇] 写缓冲失败", exc_info=True)
            return
        self._note_scope(ctx)
        frequency = _num("learn_frequency", 50, 5, 10000)
        if count and count >= frequency and ctx.session_key not in self._pending:
            self._pending[ctx.session_key] = time.time()
            self._ensure_scheduler()

    def _note_scope(self, ctx: HookContext) -> None:
        """顺手记下群名，面板侧栏才有名字可显示。每个会话只写一次。"""
        key = ctx.session_key
        if key in self._named:
            return
        name = getattr(ctx.event, "group_name", None)
        if not name or name == "未知群名":
            return
        try:
            _STORE.note_scope(key, name)
            self._named.add(key)
        except Exception:  # noqa: BLE001 - 记名字失败不影响主流程
            log.debug("[鲸语汇] 记群名失败 %s", key, exc_info=True)

    async def after_reply(self, ctx: HookContext, text: str) -> str:
        """记下最后一次回复时间：学习调度靠它判断这个群是不是闲下来了。"""
        self._last_reply[ctx.session_key] = time.time()
        return text

    # ---------- 设置面板的兜底渲染 ----------

    async def settings_fields(self, ctx, fields: list[dict]) -> list[dict] | None:
        """能连上模型服务时，「学习用的模型」从输入框变成下拉框。

        这就是插件设置面板的兜底接口：默认按 plugin.toml 里声明的字段渲染，
        想补动态选项/按状态改布局，在这里改字段就行；返回 None 表示不动。
        """
        models = await _local_models(ctx.services.get("llm"))
        if not models:
            return fields
        for field in fields:
            if field.get("key") != "learn_model":
                continue
            field["type"] = "options"
            field["options"] = [""] + models
            field["labels"] = ["跟主模型一致"] + models
            field["help"] = "留空 = 跟聊天用同一个模型；下拉列表来自当前连着的模型服务"
        return fields

    # ---------- 注入 ----------

    def reload_wiki_keys(self) -> None:
        try:
            self._wiki_keys = _STORE.wiki_keys() if _STORE else []
        except Exception:  # noqa: BLE001
            self._wiki_keys = []

    async def user_suffix(self, ctx: HookContext) -> str | None:
        """命中词条就追加到这条消息末尾。

        放用户消息而不是 system：system 要尽量静态，模型的 KV 缓存才能一直命中；
        注入内容每条都不一样，塞进 system 会把缓存从注入点断开。
        """
        stats = self.inject_stats
        stats["calls"] += 1
        if not _flag("enabled") or _SERVICE is None:
            stats["blocked"] += 1
            return None
        if self._blocked(ctx.event):
            stats["blocked"] += 1
            return None
        text = (ctx.event.content.text or "").strip()
        if not text:
            stats["empty"] += 1
            return None

        limit = _num("inject_max", 3, 1, 50)
        max_meaning = _limit()
        hint, tag = "", ""
        try:
            hits = _SERVICE.match(text, ctx.session_key, limit=limit)
        except Exception:  # noqa: BLE001
            log.warning("[鲸语汇] 索引匹配失败", exc_info=True)
            hits = []
        if hits:
            for hit in hits:
                hit["review_manual"] = 1 if hit.get("source") == "manual" else 0
            hint = format_hint(hits, max_meaning, budget=max_meaning * limit + 120)
            if hint:
                tag = "黑话 " + ", ".join(hit.get("name", "") for hit in hits)

        # 黑话没命中就退回梗百科：只认标题，命中一条就够
        if not hint and _flag("wiki_enable") and self._wiki_keys:
            try:
                titles = match_wiki(self._wiki_keys, text, limit=1)
                if titles:
                    hint = format_wiki(_STORE.wiki_get(titles[0])) if _STORE else ""
                    tag = f"梗 {titles[0]}"
            except Exception:  # noqa: BLE001
                log.warning("[鲸语汇] 梗库查询失败", exc_info=True)

        if not hint:
            stats["no_hit"] += 1
            return None
        stats["hits"] += 1
        stats["last"] = f"{tag} @ {time.strftime('%H:%M:%S')}"
        log.info("[鲸语汇] 注入 %s", tag)
        return hint

    # ---------- 学习调度 ----------

    @property
    def busy(self) -> bool:
        return bool(self._learning)

    def _ensure_scheduler(self) -> None:
        if self._scheduler is None or self._scheduler.done():
            self._scheduler = self._spawn(self._scheduler_loop())

    def _ready(self, now: float) -> list[tuple[float, str]]:
        """返回可以开学的群，按「闲得最久」排序。"""
        frequency = _num("learn_frequency", 50, 5, 10000)
        idle_seconds = _num("learn_idle_seconds", 300, 0, 3600)
        multiplier = _num("learn_force_multiplier", 2, 0, 10)
        ready: list[tuple[float, str]] = []
        for scope in list(self._pending):
            if now < self._cooldown.get(scope, 0):
                continue
            if scope in self._learning:
                continue
            count = _STORE.buffer_count(scope)
            if count < frequency:
                self._pending.pop(scope, None)
                continue
            idle = now - self._last_reply.get(scope, 0)
            forced = multiplier > 0 and count >= frequency * multiplier
            if idle < idle_seconds and not forced:
                continue
            ready.append((self._last_reply.get(scope, 0), scope))
        ready.sort()
        return ready

    async def _scheduler_loop(self) -> None:
        """单 worker 串行学习：同一时间只跑一个群，避免多群同时抢模型。"""
        gap = _num("learn_group_gap_seconds", 5, 0, 300)
        while self._pending:
            try:
                await asyncio.sleep(self.tick_seconds)
                if self.busy:  # 手动「学一批」正在跑，这一轮让开
                    continue
                ready = self._ready(time.time())
                if not ready:
                    continue
                _, scope = ready[0]
                self._pending.pop(scope, None)
                await self._learn(scope)
                await asyncio.sleep(gap)
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001
                log.exception("[鲸语汇] 学习调度异常")
                await asyncio.sleep(max(5, gap))

    async def _learn(self, scope: str, rows: list[dict] | None = None) -> int | None:
        """学一个群。返回入库条数；None 表示失败（调用方保留缓冲）。"""
        if _STORE is None or _SERVICE is None or _BOT is None:
            return 0
        frequency = _num("learn_frequency", 50, 5, 10000)
        rows = rows if rows is not None else _STORE.buffer_take(scope, frequency)
        if not rows:
            return 0
        self._learning.add(scope)
        try:
            miner = Miner(_CONTEXT.services.get("llm"), _settings(), _SERVICE)
            saved = await miner.learn(scope, rows)
        except Exception:  # noqa: BLE001 - 学失败保留缓冲，5 分钟后再试
            log.exception("[鲸语汇] %s 学习异常，缓冲保留", scope)
            self._cooldown[scope] = time.time() + 300
            return None
        finally:
            self._learning.discard(scope)

        if saved is None:
            self._cooldown[scope] = time.time() + 300
            log.warning("[鲸语汇] %s 学习未完成，缓冲保留，5 分钟后重试", scope)
            return None
        _STORE.buffer_delete([row["id"] for row in rows])
        log.info("[鲸语汇] %s 本轮入库 %d 条，清掉 %d 条缓冲", scope, saved, len(rows))
        if _STORE.buffer_count(scope) >= frequency:
            self._pending[scope] = time.time()
        return saved

    async def learn_now(self, scope: str | None = None) -> tuple[int, int]:
        """面板/命令里的「学一批」：不看空闲时间，直接学。返回 (入库, 群数)。"""
        if _STORE is None or self.busy:
            return 0, 0
        frequency = _num("learn_frequency", 50, 5, 10000)
        scopes = [scope] if scope else [row["scope"] for row in _STORE.buffer_scopes()]
        added = groups = 0
        for name in scopes:
            if not name or name in self._learning:
                continue
            rows = _STORE.buffer_take(name, frequency)
            if len(rows) < 3:
                continue
            saved = await self._learn(name, rows)
            if saved is None:
                continue
            added += saved
            groups += 1
        return added, groups


def setup(ctx: PluginContext) -> LexiconPlugin | None:
    global _CONTEXT, _STORE, _SERVICE, _BOT
    _CONTEXT = ctx

    llm = ctx.services.get("llm")
    if llm is None:
        log.error("[鲸语汇] 缺少 llm 服务，无法学习")
        return None

    db_path = (ctx.data_dir or HERE) / "jargon.db"
    _STORE = JargonStore(str(db_path))
    _SERVICE = SlangService(_STORE)

    plugin = LexiconPlugin()
    _BOT = plugin
    stats = _SERVICE.stats()
    log.info("[鲸语汇] 已就绪：词条 %d（手动 %d · 自动 %d），待审 %d，梗库 %d，数据库 %s",
             stats["total"], stats["manual"], stats["auto"], stats["pending"],
             _STORE.wiki_count(), db_path)
    return plugin


# ---------------- 命令：/yh xxx ----------------

HELP = (
    "鲸语汇：\n"
    "/yh list [页]       看已收录词条\n"
    "/yh pending        看待审\n"
    "/yh search 关键词   搜词条\n"
    "/yh add global|group 名字 含义\n"
    "/yh approve 名字    通过 / 驳回\n"
    "/yh del 名字        删除\n"
    "/yh promote 名字    升级为全局\n"
    "/yh check 一句话    看会注入什么\n"
    "/yh learn          立刻学一批\n"
    "/yh stat           看统计"
)

SIDE = {"manual": "手动", "auto": "黑话库", "manual_pending": "手动待审", "auto_pending": "待审"}


def _clip(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit].rstrip("，。；; ") + "…"


def _fmt(rec: dict) -> str:
    where = "全局" if rec.get("is_global") else _scope_label(rec.get("scope", ""))
    src = "手动" if rec.get("source") == "manual" else "自动"
    return f"{rec.get('content')}：{_clip(rec.get('meaning', ''), 40)} [{src} {where} ×{rec.get('count', 1)}]"


async def _handle_yh(cmd: CommandContext) -> str:
    if _SERVICE is None or _STORE is None or _BOT is None:
        return "鲸语汇还没就绪"
    sub = (cmd.args[0] if cmd.args else "").lower()
    rest = cmd.args[1:]

    if sub in ("", "help"):
        return HELP

    if sub in ("list", "pending"):
        page = int(rest[0]) if rest and rest[0].isdigit() else 1
        if sub == "list":
            rows = _SERVICE.all_approved()
            rows.sort(key=lambda r: -r["updated_at"])
            total = len(rows)
            page_rows = rows[(page - 1) * 8: page * 8]
            head = f"词条第 {page} 页（共 {total} 条）："
        else:
            total, page_rows = _SERVICE.list_entries("pending", page=page, size=8)
            head = f"待审 {total} 条（第 {page} 页）："
        if not page_rows:
            return "没有词条。" if sub == "list" else "没有待审的条目。"
        lines = [head] + [_fmt(rec) for rec in page_rows]
        if sub == "pending":
            lines.append("用 /yh approve 名称 或 /yh del 名称 处理")
        return "\n".join(lines)

    if sub == "search":
        if not rest:
            return "用法：/yh search 关键词"
        keyword = rest[0]
        rows = [r for r in _SERVICE.all_approved()
                if keyword in r["content"] or keyword in r["meaning"]]
        if not rows:
            return f"没找到包含「{keyword}」的已审词条。"
        return f"命中 {len(rows)} 条：\n" + "\n".join(_fmt(r) for r in rows[:10])

    if sub == "add":
        if len(rest) < 3:
            return "用法：/yh add global|group 名字 含义"
        where, name, meaning = rest[0], rest[1], " ".join(rest[2:])
        scope = "" if where == "global" else cmd.session_key
        try:
            _SERVICE.add_manual(name, meaning, scope=scope)
        except SlangError as exc:
            return str(exc)
        return f"已录入：{name}（{'全局' if not scope else '本群'}）"

    if sub in ("approve", "del", "delete", "promote"):
        found = _SERVICE.find_any(rest[0]) if rest else None
        if not rest:
            return f"用法：/yh {sub} 名字"
        if not found:
            return f"没找到「{rest[0]}」。"
        table, rec = found
        try:
            if sub == "approve":
                action = _SERVICE.approve(table, rec["name"], rec["scope"])
                return "已通过，移入黑话库。" if action == "approved" else "已驳回，移回待审。"
            if sub == "del" or sub == "delete":
                _SERVICE.delete(table, rec["name"], rec["scope"])
                return "已删除。"
            if not rec["scope"]:
                return "这条已经是全局词条了。"
            _SERVICE.update(table, rec["name"], rec["scope"], scope="")
            return "已提升为全局词条。"
        except SlangError as exc:
            return str(exc)

    if sub == "check":
        if not rest:
            return "用法：/yh check 一句话（看会注入什么）"
        text = " ".join(rest)
        hits = _SERVICE.match(text, cmd.session_key, _num("inject_max", 3, 1, 50))
        if not hits:
            return "这句话不会命中任何词条。"
        return (f"命中 {len(hits)} 条（聊天只显示前 40 字，注入按 max_meaning_chars 截断）：\n"
                + "\n".join(f"[{SIDE.get(h['table'], h['table'])}] "
                            f"{h['name']}：{_clip(h['meaning'], 40)}" for h in hits))

    if sub == "stat":
        stats = _SERVICE.stats()
        inject = _BOT.inject_stats
        buffer = sum(item["n"] for item in _STORE.buffer_scopes())
        return (f"词条 {stats['total']} 条（手动 {stats['manual']} · 自动 {stats['auto']}）\n"
                f"待审 {stats['pending']} · 梗库 {_STORE.wiki_count()} · 缓冲 {buffer}\n"
                f"注入：调用 {inject['calls']} · 命中 {inject['hits']} · 空文本 {inject['empty']}"
                f" · 无命中 {inject['no_hit']} · 跳过 {inject['blocked']}\n"
                f"最近：{inject['last'] or '无'}")

    if sub == "learn":
        # 命令层有 20 秒超时，学习可能跑一分钟，所以丢给后台，立刻回话
        if _BOT.busy:
            return "正在学，等这一轮跑完"
        _BOT._spawn(_BOT.learn_now(None))
        return "开始学了，几十秒后去面板看结果"

    return HELP


COMMANDS = [Command("yh", handler=_handle_yh, help="群语汇：/yh help", admin=True)]

