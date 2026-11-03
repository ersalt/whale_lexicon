# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
"""梗百科同步：拉 cnmeme.wiki 的公开词条，进 wiki 表给面板只读浏览。

接口文档 https://cnmeme.wiki/developers/api
- 无需鉴权，只读公开词条
- limit 最大 30，按更新时间倒序
- 文档要求：限并发、别密集轮询、429 遵守 Retry-After、保留来源

同步是纯函数式的：给一个 store 就干活，不碰网络以外的全局状态。
调用方负责放到线程里跑（网络 IO 会阻塞事件循环）。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

BASE = "https://cnmeme.wiki/api/v1/public/memes"
UA = "qq-bot-lite-whale-lexicon/0.2 (+https://github.com/ersalt/qq-bot-lite)"


def _parse_ts(raw) -> float:
    if not raw:
        return 0.0
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _fetch(url: str, timeout: int = 15, retries: int = 2) -> dict | None:
    for attempt in range(retries + 1):
        req = urllib.request.Request(
            url, headers={"User-Agent": UA, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # 429 是「别刷了」，按 Retry-After 退避；其它错误直接放弃
            if exc.code == 429 and attempt < retries:
                wait = int(exc.headers.get("Retry-After") or 10)
                time.sleep(min(wait, 60))
                continue
            return None
        except Exception:  # noqa: BLE001 - 网络问题不重试太久
            time.sleep(1 + attempt)
    return None


def sync(store, limit: int = 30, sleep: float = 0.5, max_pages: int = 0,
         full: bool = False, log=print) -> dict:
    """同步到 store 的 wiki 表，返回统计。

    增量靠 meta.wiki_synced_at 水位；full=True 会先清空再全量拉。
    max_pages 给面板按钮用：一次别拉太久，按页数封顶。
    """
    limit = max(1, min(30, int(limit)))
    watermark = 0.0 if full else float(store.meta_get("wiki_synced_at") or 0)
    if full:
        store.wiki_clear()

    offset, pages, total, newest = 0, 0, 0, watermark
    while True:
        url = f"{BASE}?{urllib.parse.urlencode({'limit': limit, 'offset': offset})}"
        data = _fetch(url)
        if not data:
            if pages == 0:
                raise RuntimeError("梗百科接口没响应（可能被限流或断网）")
            log(f"第 {pages + 1} 页请求失败，先收下已拿到的")
            break
        items = data.get("items") or []
        if not items:
            break

        rows = []
        for it in items:
            title = (it.get("title") or "").strip()
            if not (2 <= len(title) <= 24):
                continue
            ts = _parse_ts(it.get("updated_at"))
            if watermark and ts and ts <= watermark:
                continue
            summary = " ".join((it.get("summary") or "").split())
            if not summary:
                continue
            rows.append({
                "title": title,
                "tags": "、".join(it.get("tags") or []),
                "summary": summary[:200],
                "updated_at": ts or time.time(),
            })
            newest = max(newest, ts)

        if rows:
            store.wiki_upsert_many(rows)
            total += len(rows)

        pages += 1
        offset += limit
        if not data.get("has_more"):
            break
        if max_pages and pages >= max_pages:
            break
        time.sleep(sleep)

    store.meta_set("wiki_synced_at", str(newest))
    return {"pages": pages, "updated": total, "total": store.wiki_count(),
            "watermark": newest, "capped": bool(max_pages and pages >= max_pages)}
