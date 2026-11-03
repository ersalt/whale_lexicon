"""鲸语汇：群黑话自学习插件（qq-bot-lite 版）。

同目录的 core/ 是从原 AstrBot 插件（astrbot_plugin_slang）搬过来的纯逻辑模块，
不依赖任何平台 API。这里做三件事：攒消息、定期调模型学词、命中时把释义注入提示词，
外加一套给网页面板用的 HTTP 接口。
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.sdk import Command, CommandContext, Extension, HookContext, PluginContext

from .core import wiki as wiki_sync
from .core.matcher import format_hint
from .core.miner import Miner
from .core.service import SlangError, SlangService
from .core.store import TABLES, JargonStore

log = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
VERSION = "0.2.0"

router = APIRouter()
_CONTEXT: PluginContext | None = None
_STORE: JargonStore | None = None
_SERVICE: SlangService | None = None
_MINER: Miner | None = None
_BOT: "LexiconPlugin | None" = None


def _settings() -> dict:
    return _CONTEXT.settings if _CONTEXT else {}


def _limit() -> int:
    try:
        return max(20, int(_settings().get("max_meaning_chars", 200) or 200))
    except (TypeError, ValueError):
        return 200


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


# ---------------- 面板接口 ----------------
# 统一返回 {ok, message, ...}；页面对 4xx 也会解析 body，不吞错误。


@router.get("/ping")
async def ping() -> JSONResponse:
    if _SERVICE is None or _STORE is None:
        return _not_ready()
    stats = _SERVICE.stats()
    return JSONResponse({
        "ok": True,
        "rows": stats["total"],
        "manual": stats["manual"],
        "auto": stats["auto"],
        "pending": stats["pending"],
        "wiki": _STORE.wiki_count(),
        "enabled": bool(_settings().get("enabled", True)),
        "auto_learn": bool(_settings().get("auto_learn", True)),
        "limit": _limit(),
        "version": VERSION,
        "wiki_synced_at": _STORE.meta_get("wiki_synced_at") or "",
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
    buckets = [
        {"key": "pending", "label": "待审", "count": pending,
         "subs": subs(_SERVICE.pending_scopes())},
        {"key": "auto", "label": "黑话库", "count": auto,
         "subs": subs(_SERVICE.scopes_of("auto"))},
        {"key": "manual", "label": "手动", "count": manual,
         "subs": subs(_SERVICE.scopes_of("manual"))},
    ]
    if _settings().get("wiki_enable", True):
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
        if not _settings().get("wiki_enable", True):
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
    """手动学一批：把缓冲区里攒的聊天立刻送去提取。"""
    if _BOT is None:
        return _not_ready()
    scope = _clean_scope((body or {}).get("scope"))
    added = await _BOT.learn_once(scope or None, force=True)
    message = f"学完了，新增 {added} 条待审" if added else "这批不够数，没学到新词"
    return JSONResponse({"ok": True, "added": added, "message": message})


@router.post("/wiki/sync")
async def wiki_sync_now(body: dict | None = None) -> JSONResponse:
    """同步梗百科。第一次（库里空）自动全量，之后按水位增量。"""
    if _STORE is None:
        return _not_ready()
    if not _settings().get("wiki_enable", True):
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
    message = f"梗百科 {res['total']} 条（本次 {'全量' if full else '增量'} +{res['updated']}，{res['pages']} 页）"
    return JSONResponse({"ok": True, "message": message, "wiki": res["total"],
                         "updated": res["updated"], "pages": res["pages"]})


# ---------------- 插件本体 ----------------


class LexiconPlugin(Extension):
    name = "whale_lexicon"
    # 后台学习的巡检间隔（秒）：一分钟看一次有没有攒够批
    loop_seconds = 60.0

    def __init__(self) -> None:
        self.started = time.time()
        self._last_run: dict[str, float] = {}
        self._named: set[str] = set()

    # ---------- 生命周期 ----------

    async def on_start(self) -> None:
        """加载完成后起一个后台学习循环（受 ctx.spawn 管理，退出时会被取消）。"""
        self.ctx.spawn(self._background())

    async def _background(self) -> None:
        while True:
            await asyncio.sleep(self.loop_seconds)
            if not _settings().get("auto_learn", True):
                continue
            try:
                await self.learn_once(None)
            except Exception:  # noqa: BLE001
                log.exception("[鲸语汇] 自动学习失败")

    # ---------- 钩子 ----------

    async def on_message(self, ctx: HookContext) -> None:
        if not _settings().get("enabled", True) or _STORE is None:
            return
        text = (ctx.event.content.text or "").strip()
        if not text or text.startswith("/"):
            return
        self._note_scope(ctx)
        speaker = getattr(ctx.event, "sender_name", None) or str(ctx.event.user_id or "群友")
        try:
            _STORE.buffer_add(ctx.session_key, speaker, text)
        except Exception:  # noqa: BLE001
            log.warning("[鲸语汇] 写缓冲失败", exc_info=True)

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

    async def system_prompt(self, ctx: HookContext) -> str | None:
        if not _settings().get("enabled", True) or _SERVICE is None:
            return None
        limit = int(_settings().get("inject_max", 3) or 0)
        if limit <= 0:
            return None
        text = (ctx.event.content.text or "").strip()
        if not text:
            return None
        hits = _SERVICE.match(text, ctx.session_key, limit=limit)
        if not hits:
            return None
        hint = format_hint(hits, max_chars=_limit())
        return hint or None

    # ---------- 学习 ----------

    async def learn_once(self, scope: str | None, force: bool = False) -> int:
        """扫一遍缓冲区，够数就学一批。返回新增的待审条目数。"""
        if _STORE is None or _MINER is None or _SERVICE is None:
            return 0
        if not _settings().get("enabled", True):
            return 0

        batch = max(5, int(_settings().get("learn_batch_size", 20) or 20))
        gap = max(30, int(_settings().get("learn_frequency", 300) or 300))
        scopes = [scope] if scope else [item["scope"] for item in _STORE.buffer_scopes()]
        added = 0
        for name in scopes:
            if not name:
                continue
            if not force and time.time() - self._last_run.get(name, 0) < gap:
                continue
            count = _STORE.buffer_count(name)
            if count < batch:
                continue
            rows = _STORE.buffer_take(name, batch)
            if not rows:
                continue
            try:
                entries = await _MINER.learn(rows)
            except Exception:  # noqa: BLE001
                log.warning("[鲸语汇] 提取失败 %s", name, exc_info=True)
                continue
            for entry in entries:
                try:
                    _SERVICE.submit(
                        entry["name"],
                        entry["meaning"],
                        scope=name,
                        aliases=entry.get("aliases", ""),
                        evidence=entry.get("evidence", ""),
                    )
                    added += 1
                except Exception:  # noqa: BLE001 - 单条失败不影响整批
                    log.debug("[鲸语汇] 入库失败 %s", entry.get("name"), exc_info=True)
            _STORE.buffer_delete([row["id"] for row in rows])
            self._last_run[name] = time.time()
            log.info("[鲸语汇] %s 学了一批：%d 条进待审", name, added)
        return added


def setup(ctx: PluginContext) -> LexiconPlugin | None:
    global _CONTEXT, _STORE, _SERVICE, _MINER, _BOT
    _CONTEXT = ctx

    llm = ctx.services.get("llm")
    if llm is None:
        log.error("[鲸语汇] 缺少 llm 服务，无法学习")
        return None

    db_path = (ctx.data_dir or HERE) / "jargon.db"
    _STORE = JargonStore(str(db_path))
    _SERVICE = SlangService(_STORE)
    _MINER = Miner(llm, ctx.settings)

    plugin = LexiconPlugin()
    _BOT = plugin
    log.info("[鲸语汇] 已就绪，数据库 %s", db_path)
    return plugin


# ---------------- 命令：/yh xxx ----------------

HELP = (
    "鲸语汇：\n"
    "/yh list [关键词]  看待审词条\n"
    "/yh approve 名字   通过（管理员）\n"
    "/yh del 名字       删除（管理员）\n"
    "/yh learn          立刻学一批\n"
    "/yh stats          看统计"
)


async def _handle_yh(cmd: CommandContext) -> str:
    if _SERVICE is None or _STORE is None:
        return "鲸语汇还没就绪"
    sub = (cmd.args[0] if cmd.args else "").lower()
    rest = cmd.args[1:]

    if sub in ("", "help"):
        return HELP

    if sub == "stats":
        data = _SERVICE.stats() or {}
        return (f"词条 {data.get('total', 0)} 条（手动 {data.get('manual', 0)} / "
                f"自动 {data.get('auto', 0)}），待审 {data.get('pending', 0)}")

    if sub == "list":
        search = " ".join(rest)
        total, items = _SERVICE.list_entries("pending", search=search)
        if not items:
            return "待审池是空的"
        lines = [f"待审 {total} 条："]
        for item in items[:10]:
            lines.append(f"· {item['content']}：{item['meaning'][:24]}")
        if total > 10:
            lines.append(f"……还有 {total - 10} 条，去面板看")
        return "\n".join(lines)

    if sub in ("approve", "deny", "del", "delete"):
        if not cmd.is_admin:
            return "这条命令只有管理员能用"
        name = rest[0] if rest else ""
        if not name:
            return f"用法：/yh {sub} 名字"
        for table in ("auto_pending", "manual_pending", "auto", "manual"):
            try:
                if sub == "approve":
                    _SERVICE.approve(table, name, "")
                    return f"已通过「{name}」"
                _SERVICE.delete(table, name, "")
                return f"已删除「{name}」"
            except Exception:  # noqa: BLE001 - 表里没有就试下一张
                continue
        return f"没找到「{name}」"

    if sub == "learn":
        if _BOT is None:
            return "鲸语汇还没就绪"
        added = await _BOT.learn_once(None, force=True)
        return f"学完了，新增 {added} 条待审" if added else "这批不够数，没学到新词"

    return HELP


COMMANDS = [Command("yh", handler=_handle_yh, help="群语汇：/yh help")]
