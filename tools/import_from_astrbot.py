#!/usr/bin/env python3
"""把 AstrBot 版群语汇（astrbot_plugin_slang）的数据搬进本插件。

两边的表结构本来就同源，差别只有两处：

- 作用域：AstrBot 存的是纯群号（`123456789`），本插件存的是会话键（`group:123456789`）
- 群名：AstrBot 在 `data_v4.db` 的 `umo_aliases` 里，本插件在 `groups` 表

用法（先停掉机器人，跑完再开）：

    python tools/import_from_astrbot.py \
        --source "C:/.../plugin_data/astrbot_plugin_slang/jargon.db" \
        --target "../qq-bot-lite/data/plugin_data/whale_lexicon/jargon.db" \
        --aliases "C:/.../data_v4.db" \
        --replace

`--replace` 会先清空目标库里的词条和梗百科；不加就是往现有数据上合并（同名同作用域跳过）。
梗百科和群名默认都搬，用 `--no-wiki` / `--no-aliases` 关掉。
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.store import TABLES, JargonStore  # noqa: E402

LEGACY = ("jargon", "pending")
SCOPE_RE = re.compile(r"^(?P<platform>[^:]*):(?P<kind>GroupMessage|FriendMessage|Group|Friend):(?P<id>\d+)$")


def map_scope(raw: str) -> str:
    """AstrBot 的作用域 → 本插件的作用域。"""
    raw = (raw or "").strip()
    if not raw:
        return ""
    if raw.startswith(("group:", "private:")):
        return raw
    if raw.isdigit():
        return f"group:{raw}"
    match = SCOPE_RE.match(raw)
    if match:
        kind = "group" if "Group" in match.group("kind") else "private"
        return f"{kind}:{match.group('id')}"
    return raw


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def source_rows(conn: sqlite3.Connection):
    """返回 {目标表: [行]}，兼容四表结构和 1.x 的两表结构。"""
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    out: dict[str, list[dict]] = {name: [] for name in TABLES}

    if all(name in tables for name in TABLES):
        for name in TABLES:
            for row in conn.execute(f"SELECT * FROM {name}"):
                out[name].append(dict(row))
        return out, "四张表"

    if "jargon" in tables or "pending" in tables:
        for old in LEGACY:
            if old not in tables:
                continue
            cols = table_columns(conn, old)
            for row in conn.execute(f"SELECT * FROM {old}"):
                row = dict(row)
                source = str(row.get("source") or "").strip()
                if source not in ("manual", "ai"):
                    source = "manual" if row.get("category") == "manual" else "ai"
                approved = old == "jargon"
                if "review_manual" in cols and not row.get("review_manual") and old == "jargon":
                    approved = False
                if approved:
                    target = "manual" if source == "manual" else "auto"
                else:
                    target = "manual_pending" if source == "manual" else "auto_pending"
                scope = row.get("scope") or ""
                if row.get("category") == "global":
                    scope = ""
                out[target].append({**row, "scope": scope})
        return out, "1.x 的 jargon/pending"

    raise SystemExit("源库里既没有四张表，也没有 jargon/pending，认不出来")


def import_rows(store: JargonStore, rows_by_table: dict[str, list[dict]], dry: bool) -> dict[str, int]:
    counts = {name: 0 for name in TABLES}
    for table, rows in rows_by_table.items():
        for row in rows:
            name = str(row.get("name") or "").strip()[:32]
            meaning = str(row.get("meaning") or "").strip()
            if not name or not meaning:
                continue
            scope = map_scope(row.get("scope"))
            if dry:
                counts[table] += 1
                continue
            try:
                now = time.time()
                store.conn.execute(
                    f"""INSERT OR IGNORE INTO {table}
                        (name, aliases, keywords, meaning, scope, count, auto_note,
                         evidence, contributor, created_at, updated_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (name, str(row.get("aliases") or ""),
                     str(row.get("keywords") or row.get("aliases") or ""),
                     meaning[:1000], scope, int(row.get("count") or 1),
                     str(row.get("auto_note") or ""), str(row.get("evidence") or "[]"),
                     str(row.get("contributor") or ""),
                     float(row.get("created_at") or 0) or now,
                     float(row.get("updated_at") or 0) or now),
                )
                counts[table] += 1
            except sqlite3.Error as exc:
                print(f"  ! {table} {name}: {exc}")
    if not dry:
        store.conn.commit()
    return counts


def import_wiki(source: sqlite3.Connection, store: JargonStore, dry: bool) -> int:
    tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "wiki" not in tables:
        return 0
    rows = source.execute("SELECT title, tags, summary, updated_at FROM wiki").fetchall()
    if dry:
        return len(rows)
    store.wiki_upsert_many([
        {"title": r[0], "tags": r[1] or "", "summary": r[2] or "", "updated_at": r[3] or 0}
        for r in rows
    ])
    synced = source.execute("SELECT v FROM meta WHERE k='wiki_synced_at'").fetchone() \
        if "meta" in tables else None
    if synced:
        store.meta_set("wiki_synced_at", synced[0])
    return len(rows)


def import_aliases(alias_db: Path, store: JargonStore, dry: bool) -> int:
    if not alias_db.exists():
        print(f"  ! 找不到 {alias_db}，跳过群名")
        return 0
    conn = sqlite3.connect(f"file:{alias_db}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT umo, auto_name, user_alias FROM umo_aliases").fetchall()
    except sqlite3.Error:
        return 0
    finally:
        conn.close()
    count = 0
    for umo, auto_name, user_alias in rows:
        scope = map_scope(umo)
        name = (user_alias or auto_name or "").strip()
        if not scope or not name:
            continue
        if not dry:
            store.note_scope(scope, name)
        count += 1
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description="从 AstrBot 的群语汇搬数据进鲸语汇")
    parser.add_argument("--source", required=True, help="AstrBot 的 jargon.db")
    parser.add_argument("--target", required=True, help="本插件的 jargon.db")
    parser.add_argument("--aliases", default="", help="AstrBot 的 data_v4.db（用来搬群名）")
    parser.add_argument("--replace", action="store_true", help="先清空目标的词条（梗百科也重搬）")
    parser.add_argument("--no-wiki", action="store_true", help="不搬梗百科")
    parser.add_argument("--no-aliases", action="store_true", help="不搬群名")
    parser.add_argument("--dry-run", action="store_true", help="只统计不写库")
    args = parser.parse_args()

    source_path = Path(args.source)
    if not source_path.exists():
        raise SystemExit(f"源库不存在：{source_path}")
    source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row

    rows_by_table, shape = source_rows(source)
    print(f"源库：{source_path}（{shape}）")
    for table, rows in rows_by_table.items():
        print(f"  {table}: {len(rows)} 行")

    store = JargonStore(str(args.target))
    if args.replace and not args.dry_run:
        for table in TABLES:
            store.conn.execute(f"DELETE FROM {table}")
        if not args.no_wiki:
            store.conn.execute("DELETE FROM wiki")
            store.conn.execute("DELETE FROM meta WHERE k='wiki_synced_at'")
        store.conn.commit()
        print("已清空目标词条" + ("和梗百科" if not args.no_wiki else ""))

    counts = import_rows(store, rows_by_table, args.dry_run)
    print("导入词条：", {k: v for k, v in counts.items() if v})

    if not args.no_wiki:
        n = import_wiki(source, store, args.dry_run)
        print(f"导入梗百科：{n} 条")

    if args.aliases and not args.no_aliases:
        n = import_aliases(Path(args.aliases), store, args.dry_run)
        print(f"导入群名：{n} 个")

    if not args.dry_run:
        store.conn.commit()
        total = sum(
            store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in TABLES
        )
        print("目标统计：", total, "条词条，", store.wiki_count(), "条梗百科")
    store.close()
    source.close()


if __name__ == "__main__":
    main()
