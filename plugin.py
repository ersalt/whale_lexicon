"""鲸语汇：群黑话自学习插件（qq-bot-lite 版）。

同目录的 store / service / matcher / miner 是从原 AstrBot 插件
（astrbot_plugin_slang）里搬过来的纯逻辑模块，不依赖任何平台 API。
这里只做三件事：攒消息、定期调模型学词、命中时把释义注入提示词。
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import logging
import sys
import time
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.pluginkit.manifest import PluginContext
from app.pluginkit.registry import Extension, HookContext

log = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent


def _load_core():
    """core 是个真包，按包加载，内部用相对导入。"""
    spec = importlib.util.spec_from_file_location(
        "whale_lexicon_core",
        HERE / "core" / "__init__.py",
        submodule_search_locations=[str(HERE / "core")],
    )
    if spec is None or spec.loader is None:
        raise ImportError("加载 core 包失败")
    module = importlib.util.module_from_spec(spec)
    sys.modules["whale_lexicon_core"] = module
    spec.loader.exec_module(module)
    return module

_core = _load_core()
_store_mod = importlib.import_module("whale_lexicon_core.store")
_service_mod = importlib.import_module("whale_lexicon_core.service")
_matcher_mod = importlib.import_module("whale_lexicon_core.matcher")
_miner_mod = importlib.import_module("whale_lexicon_core.miner")

JargonStore = _store_mod.JargonStore
SlangService = _service_mod.SlangService
format_hint = _matcher_mod.format_hint
Miner = _miner_mod.Miner

router = APIRouter()
_CONTEXT: PluginContext | None = None
_STORE: JargonStore | None = None
_SERVICE: SlangService | None = None
_MINER: Miner | None = None
_BOT: "LexiconPlugin | None" = None


def _settings() -> dict:
    return _CONTEXT.settings if _CONTEXT else {}


@router.get("/data")
async def data() -> JSONResponse:
    if _SERVICE is None:
        return JSONResponse({"ok": False, "message": "插件未就绪"})
    return JSONResponse({"ok": True, **(_SERVICE.stats() or {})})


@router.get("/list")
async def list_entries(bucket: str = "pending", scope: str = "", search: str = "") -> JSONResponse:
    if _SERVICE is None:
        return JSONResponse({"ok": False, "message": "插件未就绪"})
    items = _SERVICE.list_entries(bucket, search=search, scope=scope)
    return JSONResponse({"ok": True, "items": items})


@router.get("/tree")
async def tree() -> JSONResponse:
    if _SERVICE is None:
        return JSONResponse({"ok": False, "message": "插件未就绪"})
    return JSONResponse({"ok": True, "counts": _SERVICE.tree_counts(), "scopes": _SERVICE.pending_scopes()})


@router.post("/approve")
async def approve(body: dict) -> JSONResponse:
    if _SERVICE is None:
        return JSONResponse({"ok": False, "message": "插件未就绪"})
    table = body.get("table") or ""
    name = body.get("name") or ""
    scope = body.get("scope") or ""
    candidates = [table] if table in ("auto_pending", "manual_pending", "auto", "manual") else ["auto_pending", "manual_pending"]
    try:
        where = ""
        for candidate in candidates:
            try:
                where = _SERVICE.approve(candidate, name, scope)
                break
            except Exception:
                continue
        else:
            raise ValueError(f"没找到「{name}」")
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "where": where, **(_SERVICE.stats() or {})})


@router.post("/learn")
async def learn_now(body: dict | None = None) -> JSONResponse:
    """手动学一批：把缓冲区里攒的聊天立刻送去提取。"""
    if _BOT is None:
        return JSONResponse({"ok": False, "message": "插件未就绪"})
    scope = (body or {}).get("scope") or ""
    added = await _BOT.learn_once(scope or None, force=True)
    return JSONResponse({"ok": True, "added": added})


class LexiconPlugin(Extension):
    name = "whale_lexicon"

    def __init__(self) -> None:
        self.started = time.time()
        self._last_run: dict[str, float] = {}
        self._task: asyncio.Task | None = None

    # ---------- 生命周期 ----------

    def boot(self) -> None:
        """setup() 里调：建库、建服务、把后台学习循环跑起来。"""
        self.loop_seconds = 60.0
        try:
            self._task = asyncio.get_event_loop().create_task(self._background())
        except RuntimeError:
            # 没有正在运行的事件循环（极少见），下次收到消息再补
            self._task = None

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
        speaker = getattr(ctx.event, "sender_name", None) or str(ctx.event.user_id or "群友")
        try:
            _STORE.buffer_add(ctx.session_key, speaker, text)
        except Exception:  # noqa: BLE001
            log.warning("[鲸语汇] 写缓冲失败", exc_info=True)

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
        hint = format_hint(hits, max_chars=int(_settings().get("max_meaning_chars", 200) or 200))
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
    plugin.boot()
    log.info("[鲸语汇] 已就绪，数据库 %s", db_path)
    return plugin



