"""Subscriptions router for ytvideo."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from backend.db import (
    get_db,
    get_subscriptions,
    add_subscription,
    remove_subscription,
    is_subscribed,
    save_channel_stats,
    touch_channel_attempt,
    get_stale_channel_ids,
    get_channel_browse_page,
    get_channel_stats_summary,
)
from backend.services.subscription_rss import (
    get_subscription_feed,
    invalidate_subscription_feed_cache,
)
from backend.services.youtube_rss import fetch_channel_videos_rss

router = APIRouter()
_log = logging.getLogger(__name__)

# ── Browse helpers ─────────────────────────────────────────────────────────

_BROWSE_STOP = {
    "the","a","an","and","or","of","in","on","at","to","for","with","by","from",
    "is","it","its","this","that","was","are","be","been","have","has","had",
    "i","my","you","your","we","our","they","their","he","she","his","her",
    "how","what","why","when","where","which","who","not","no","all","more",
    "but","so","if","as","up","do","did","get","got","can","will","just","also",
    "about","after","into","over","out","than","then","s","t","re","ve","d",
    "1","2","3","4","5","6","7","8","9","10","100",
    "ft","feat","official","video","audio","new","vs","ep","full","live",
    "part","best","top","most","mr","dr","st","episode","series","season",
    "vol","mix","remix","cover","reaction","review","trailer","clip","short",
}


def _analyze_upload_pattern(videos: list) -> dict:
    import time as _t
    timestamps = sorted([v.get("published", 0) for v in videos if v.get("published")], reverse=True)
    if not timestamps:
        return {"pattern": "unknown", "avg_interval_days": None, "last_upload_at": None}

    last_upload = timestamps[0]
    now = _t.time()
    days_since = (now - last_upload) / 86400

    if days_since > 365:
        return {"pattern": "inactive", "avg_interval_days": None, "last_upload_at": last_upload}

    if len(timestamps) < 3:
        pattern = "inactive" if days_since > 180 else "unknown"
        return {"pattern": pattern, "avg_interval_days": None, "last_upload_at": last_upload}

    recent = timestamps[:20]
    intervals = [(recent[i] - recent[i + 1]) / 86400 for i in range(len(recent) - 1)]
    avg = sum(intervals) / len(intervals)
    variance = sum((x - avg) ** 2 for x in intervals) / len(intervals)
    std_dev = math.sqrt(variance)
    cv = std_dev / avg if avg > 0 else 0

    if days_since > 180:
        pattern = "inactive"
    elif cv > 1.8:
        pattern = "bursty"
    elif avg < 1.5:
        pattern = "daily"
    elif avg < 5:
        pattern = "frequent"
    elif avg < 12:
        pattern = "weekly"
    elif avg < 30:
        pattern = "biweekly"
    elif avg < 65:
        pattern = "monthly"
    elif avg < 150:
        pattern = "sporadic"
    else:
        pattern = "rare"

    return {"pattern": pattern, "avg_interval_days": round(avg, 1), "last_upload_at": last_upload}


def _extract_themes(videos: list, top_n: int = 6) -> list:
    from collections import Counter
    counter: Counter = Counter()
    for v in videos:
        words = re.findall(r"[a-z]{4,}", (v.get("title") or "").lower())
        for w in words:
            if w not in _BROWSE_STOP:
                counter[w] += 1
    return [w for w, c in counter.most_common(top_n * 2) if c >= 2][:top_n]


def _build_recent_videos(videos: list) -> list:
    recent = []
    for v in videos[:20]:
        vt = v.get("videoThumbnails") or []
        thumb = vt[0]["url"] if vt else None
        recent.append({
            "video_id": v.get("videoId"),
            "title": v.get("title"),
            "thumbnail": thumb,
            "published": v.get("published"),
            "view_count": v.get("viewCount"),
            "length_seconds": v.get("lengthSeconds"),
        })
    return recent


async def _refresh_channel_rss(channel_id: str, channel_name: str) -> bool:
    """Lightweight RSS-only refresh."""
    try:
        rss_data = await fetch_channel_videos_rss(channel_id, channel_name)
        if not rss_data:
            # All upstream sources failed/empty. Record the failed attempt so this
            # channel backs off instead of being re-fetched every worker tick.
            touch_channel_attempt(channel_id, channel_name)
            return False

        videos = rss_data.get("videos") or []
        resolved_name = rss_data.get("channel_name") or channel_name
        thumbnail = rss_data.get("thumbnail")

        pattern_info = _analyze_upload_pattern(videos)
        themes = _extract_themes(videos)

        save_channel_stats(
            channel_id=channel_id,
            channel_name=resolved_name,
            thumbnail=thumbnail,
            sub_count=None,
            video_count=len(videos),
            last_upload_at=pattern_info.get("last_upload_at"),
            avg_interval_days=pattern_info.get("avg_interval_days"),
            pattern=pattern_info.get("pattern"),
            themes=themes,
            recent_videos=_build_recent_videos(videos),
        )
        return True
    except Exception as exc:
        _log.warning("RSS refresh failed for %s: %s", channel_id, exc)
        touch_channel_attempt(channel_id, channel_name)
        return False


# ── Endpoints ──────────────────────────────────────────────────────────────

class SubscribeRequest(BaseModel):
    channel_id: str
    channel_name: str
    thumbnail: Optional[str] = None


@router.get("/subscriptions")
async def list_subscriptions():
    return get_subscriptions()


@router.get("/subscriptions/{channel_id}/check")
async def check_subscription(channel_id: str):
    return {"subscribed": is_subscribed(channel_id)}


@router.post("/subscriptions")
async def subscribe(body: SubscribeRequest):
    add_subscription(body.channel_id, body.channel_name, body.thumbnail)
    invalidate_subscription_feed_cache()
    return {"ok": True}


@router.delete("/subscriptions/{channel_id}")
async def unsubscribe(channel_id: str):
    remove_subscription(channel_id)
    invalidate_subscription_feed_cache()
    return {"ok": True}


@router.get("/subscriptions/feed")
async def subscription_feed(
    page: Optional[int] = Query(None),
    per_page: Optional[int] = Query(None),
    limit: int = Query(12),
    offset: int = Query(0),
    music_labeled_only: bool = Query(False),
):
    if page is not None or per_page is not None:
        effective_limit = max(1, per_page or limit)
        effective_offset = max(0, ((page or 1) - 1) * effective_limit)
        return await get_subscription_feed(
            limit=effective_limit, offset=effective_offset, music_labeled_only=music_labeled_only
        )
    return await get_subscription_feed(limit=limit, offset=offset, music_labeled_only=music_labeled_only)


@router.get("/subscriptions/recent-uploads")
async def subscription_recent_uploads(limit: int = Query(36), offset: int = Query(0)):
    """Return all recently fetched subscription videos sorted chronologically."""
    import time

    def _run():
        subscriptions = get_subscriptions()
        if not subscriptions:
            return {"videos": [], "total": 0, "limit": limit, "offset": offset, "has_more": False}

        from backend.services.subscription_rss import _load_channel_meta
        channel_meta = _load_channel_meta()
        now = time.time()
        max_age_seconds = 45 * 86400
        seen: set[str] = set()
        videos: list[dict] = []

        for sub in subscriptions:
            channel_id = sub["channel_id"]
            meta = channel_meta.get(channel_id, {})
            recent = list(meta.get("recent_videos") or [])
            for raw in recent:
                video_id = str(raw.get("video_id") or raw.get("videoId") or "").strip()
                if not video_id or video_id in seen:
                    continue
                published = int(raw.get("published") or 0)
                if published and (now - published) > max_age_seconds:
                    continue
                seen.add(video_id)
                thumb = raw.get("thumbnail") or f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
                videos.append({
                    "videoId": video_id,
                    "title": raw.get("title") or video_id,
                    "author": meta.get("channel_name") or sub["channel_name"],
                    "authorId": channel_id,
                    "lengthSeconds": int(raw.get("length_seconds") or raw.get("lengthSeconds") or 0) or None,
                    "viewCount": int(raw.get("view_count") or raw.get("viewCount") or 0) or None,
                    "published": published or None,
                    "videoThumbnails": [{"url": thumb}],
                })

        videos.sort(key=lambda v: v.get("published") or 0, reverse=True)
        safe_limit = max(1, min(limit, 100))
        safe_offset = max(0, offset)
        return {
            "videos": videos[safe_offset:safe_offset + safe_limit],
            "total": len(videos),
            "limit": safe_limit,
            "offset": safe_offset,
            "has_more": safe_offset + safe_limit < len(videos),
        }

    return await run_in_threadpool(_run)


@router.get("/subscriptions/browse")
async def browse_subscriptions(
    page: int = Query(1),
    per_page: int = Query(20),
    pattern: str = Query(""),
    hide_rated: bool = Query(False),
    sort: str = Query("name"),
    search: str = Query(""),
    hide_categorized: bool = Query(False),
):
    return await run_in_threadpool(
        get_channel_browse_page, page, per_page, pattern, hide_rated, sort, search, hide_categorized
    )


@router.get("/subscriptions/browse/summary")
async def browse_summary():
    return await run_in_threadpool(get_channel_stats_summary)


@router.post("/subscriptions/refresh-stats")
async def refresh_stats_batch(batch: int = Query(10)):
    stale = get_stale_channel_ids(max_age_hours=48)[:batch]
    await asyncio.gather(*[_refresh_channel_rss(ch["channel_id"], ch["channel_name"]) for ch in stale])
    invalidate_subscription_feed_cache()
    return {"ok": True, "refreshed": len(stale), "remaining": max(0, len(get_stale_channel_ids(48)))}


@router.post("/subscriptions/{channel_id}/refresh-stats")
async def refresh_one_stats(channel_id: str):
    subs = get_subscriptions()
    ch = next((s for s in subs if s["channel_id"] == channel_id), None)
    if not ch:
        raise HTTPException(status_code=404, detail="Not subscribed to this channel")
    await _refresh_channel_rss(channel_id, ch["channel_name"])
    invalidate_subscription_feed_cache()
    return {"ok": True}


# ── RSS scan ───────────────────────────────────────────────────────────────

_rss_scan_state: dict = {"running": False, "total": 0, "done": 0, "ok": 0, "failed": 0}
_rss_scan_task = None
RSS_SCAN_CONCURRENT = 20


async def _run_rss_scan():
    global _rss_scan_state
    subs = get_subscriptions()
    _rss_scan_state.update({"total": len(subs), "done": 0, "ok": 0, "failed": 0})

    for i in range(0, len(subs), RSS_SCAN_CONCURRENT):
        if not _rss_scan_state["running"]:
            break
        chunk = subs[i: i + RSS_SCAN_CONCURRENT]
        results = await asyncio.gather(*[
            _refresh_channel_rss(ch["channel_id"], ch["channel_name"]) for ch in chunk
        ])
        _rss_scan_state["done"] += len(chunk)
        _rss_scan_state["ok"] += sum(1 for r in results if r)
        _rss_scan_state["failed"] += sum(1 for r in results if not r)

    invalidate_subscription_feed_cache()
    _log.info("RSS scan complete: %d/%d channels updated", _rss_scan_state["ok"], _rss_scan_state["total"])
    _rss_scan_state["running"] = False


@router.post("/subscriptions/rss-scan/start")
async def rss_scan_start():
    global _rss_scan_task, _rss_scan_state
    if _rss_scan_state["running"]:
        return {"ok": False, "reason": "Already running"}
    _rss_scan_state["running"] = True
    _rss_scan_task = asyncio.create_task(_run_rss_scan())
    return {"ok": True, "total": len(get_subscriptions())}


@router.post("/subscriptions/rss-scan/stop")
async def rss_scan_stop():
    _rss_scan_state["running"] = False
    return {"ok": True}


@router.get("/subscriptions/rss-scan/status")
async def rss_scan_status():
    return dict(_rss_scan_state)


# ── Autoscan ───────────────────────────────────────────────────────────────

_autoscan_state: dict = {"running": False, "total": 0, "done": 0, "current_channel": None, "errors": 0}
_autoscan_task = None


async def _run_autoscan(skip_fresh_hours: int = 0):
    global _autoscan_state
    import random as _random
    import time as _time
    _autoscan_state["errors"] = 0
    _autoscan_state["done"] = 0

    subs = get_subscriptions()
    if not subs:
        _autoscan_state["running"] = False
        return

    with get_db() as con:
        cutoff = _time.time() - skip_fresh_hours * 3600
        fresh = {r["channel_id"] for r in con.execute(
            "SELECT channel_id FROM channel_stats WHERE fetched_at > ?", (cutoff,)
        ).fetchall()}

    to_scan = [s for s in subs if s["channel_id"] not in fresh]
    _autoscan_state["total"] = len(to_scan)

    MAX_RETRIES = 3
    BASE_RETRY_DELAY = 15.0

    for sub in to_scan:
        if not _autoscan_state["running"]:
            break
        success = False
        for attempt in range(MAX_RETRIES + 1):
            if not _autoscan_state["running"]:
                break
            if attempt == 0:
                _autoscan_state["current_channel"] = sub["channel_name"]
            else:
                retry_wait = BASE_RETRY_DELAY * attempt + _random.uniform(0, 5.0)
                _autoscan_state["current_channel"] = f"{sub['channel_name']} (retry {attempt}/{MAX_RETRIES}, wait {retry_wait:.0f}s)"
                await asyncio.sleep(retry_wait)
                if not _autoscan_state["running"]:
                    break
            try:
                await _refresh_channel_rss(sub["channel_id"], sub["channel_name"])
                success = True
                break
            except Exception as exc:
                _log.warning("Autoscan error %s attempt %d: %s", sub["channel_id"], attempt, exc)
                if attempt == MAX_RETRIES:
                    _autoscan_state["errors"] += 1
        _autoscan_state["done"] += 1
        if _autoscan_state["running"]:
            await asyncio.sleep(3.0 + _random.uniform(0, 2.0))

    _autoscan_state["running"] = False
    _autoscan_state["current_channel"] = None


@router.get("/subscriptions/autoscan/status")
async def autoscan_status():
    return dict(_autoscan_state)


@router.post("/subscriptions/autoscan/start")
async def autoscan_start(skip_fresh_hours: int = Query(0), force_all: bool = Query(False)):
    global _autoscan_task, _autoscan_state
    if _autoscan_state["running"]:
        return {"ok": False, "reason": "Already running"}
    _autoscan_state["running"] = True
    _autoscan_task = asyncio.create_task(_run_autoscan(0 if force_all else skip_fresh_hours))
    return {"ok": True}


@router.post("/subscriptions/autoscan/stop")
async def autoscan_stop():
    global _autoscan_state
    _autoscan_state["running"] = False
    return {"ok": True}


# ── Background worker ──────────────────────────────────────────────────────

async def subscription_stats_background_worker() -> None:
    """Refresh stale subscribed channels via RSS on a regular interval."""
    if os.getenv("SUBSCRIPTION_STATS_REFRESH_ENABLED", "1").strip().lower() in ("0", "false", "no", "off"):
        _log.info("subscription stats background refresh disabled")
        return

    interval = float(os.getenv("SUBSCRIPTION_STATS_REFRESH_INTERVAL_SEC", "60"))
    batch = max(1, int(os.getenv("SUBSCRIPTION_STATS_REFRESH_BATCH", "30")))
    stale_hours = max(1, int(os.getenv("SUBSCRIPTION_STATS_STALE_HOURS", "6")))
    _log.info(
        "subscription stats background refresh: every %.0fs, batch=%s, stale>%sh (RSS-only)",
        interval, batch, stale_hours,
    )

    await asyncio.sleep(30.0)

    while True:
        try:
            stale = get_stale_channel_ids(max_age_hours=stale_hours)[:batch]
            if stale:
                results = await asyncio.gather(*[
                    _refresh_channel_rss(ch["channel_id"], ch["channel_name"]) for ch in stale
                ])
                saved = sum(1 for r in results if r)
                invalidate_subscription_feed_cache()
                _log.info("subscription stats refresh: %d/%d channel(s) updated via RSS", saved, len(stale))
            else:
                _log.debug("subscription stats refresh: all channels fresh")
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("subscription stats background refresh tick failed")
        await asyncio.sleep(max(10.0, interval))
