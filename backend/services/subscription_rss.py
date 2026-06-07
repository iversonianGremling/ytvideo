"""Subscription feed builder from cached channel stats.

Adapted from monolith: replaces monolith database imports with backend.db imports.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from backend.db import (
    get_db,
    get_subscriptions,
    get_ratings_for_video_ids,
    get_music_labeled_channel_ids,
)

RSS_CACHE_TTL_SECONDS = 300
DEFAULT_CHANNEL_RATING = 5
DEFAULT_INTERVAL_DAYS = 7.0
MAX_FEED_AGE_DAYS = 45.0
MIN_INTERVAL_DAYS = 0.5

logger = logging.getLogger(__name__)
_cache: dict[str, Any] = {"signature": "", "fetched_at": 0.0, "videos": [], "represented_channels": 0}
_cache_music: dict[str, Any] = {"signature": "", "fetched_at": 0.0, "videos": [], "represented_channels": 0}


def invalidate_subscription_feed_cache() -> None:
    for c in (_cache, _cache_music):
        c["signature"] = ""
        c["fetched_at"] = 0.0
        c["videos"] = []
        c["represented_channels"] = 0


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float | None = None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _load_channel_meta() -> dict[str, dict[str, Any]]:
    with get_db() as con:
        rows = con.execute(
            """
            SELECT s.channel_id,
                   s.channel_name,
                   COALESCE(CAST(cr.rating AS INTEGER), ?) AS rating,
                   cs.avg_interval_days,
                   cs.last_upload_at,
                   cs.fetched_at,
                   cs.recent_videos
            FROM subscriptions s
            LEFT JOIN channel_ratings cr ON cr.channel_id = s.channel_id
            LEFT JOIN channel_stats cs ON cs.channel_id = s.channel_id
            """,
            (DEFAULT_CHANNEL_RATING,),
        ).fetchall()

    meta: dict[str, dict[str, Any]] = {}
    for row in rows:
        recent_videos: list[dict[str, Any]] = []
        raw_recent = row["recent_videos"]
        if raw_recent:
            try:
                parsed = json.loads(raw_recent)
                if isinstance(parsed, list):
                    recent_videos = [item for item in parsed if isinstance(item, dict)]
            except json.JSONDecodeError:
                pass

        meta[row["channel_id"]] = {
            "channel_name": row["channel_name"],
            "rating": _as_int(row["rating"], DEFAULT_CHANNEL_RATING),
            "avg_interval_days": _as_float(row["avg_interval_days"], DEFAULT_INTERVAL_DAYS),
            "last_upload_at": _as_int(row["last_upload_at"], 0),
            "fetched_at": _as_int(row["fetched_at"], 0),
            "recent_videos": recent_videos,
        }
    return meta


def _uploads_per_week(avg_interval_days: float | None) -> float:
    if avg_interval_days is None:
        return 1.0
    return 7.0 / max(avg_interval_days, MIN_INTERVAL_DAYS)


def _channel_video_cap(rating: int, avg_interval_days: float | None) -> int:
    upw = _uploads_per_week(avg_interval_days)
    if rating >= 9:
        return max(3, int(upw * 1.5))
    if rating >= 7:
        return max(2, int(upw))
    if rating >= 5:
        return max(1, int(upw * 0.5))
    return 1


def _score_video(raw: dict, channel_rating: int, now: float) -> float:
    pub = _as_int(raw.get("published"), 0)
    if pub <= 0:
        return 0.0
    age_days = (now - pub) / 86400.0
    if age_days > MAX_FEED_AGE_DAYS:
        return 0.0
    freshness = max(0.0, 1.0 - age_days / MAX_FEED_AGE_DAYS)
    rating_boost = max(0.5, channel_rating / 5.0)
    return freshness * rating_boost


async def get_subscription_feed(
    limit: int = 12,
    offset: int = 0,
    music_labeled_only: bool = False,
) -> dict[str, Any]:
    target_cache = _cache_music if music_labeled_only else _cache
    now = time.time()

    subs = get_subscriptions()
    if not subs:
        return {"videos": [], "total": 0, "limit": limit, "offset": offset, "has_more": False, "represented_channels": 0}

    sig = ":".join(sorted(s["channel_id"] for s in subs))
    if (
        target_cache["signature"] == sig
        and (now - target_cache["fetched_at"]) < RSS_CACHE_TTL_SECONDS
        and target_cache["videos"]
    ):
        cached = target_cache["videos"]
        page = cached[offset: offset + limit]
        return {
            "videos": page,
            "total": len(cached),
            "limit": limit,
            "offset": offset,
            "has_more": offset + limit < len(cached),
            "represented_channels": target_cache["represented_channels"],
        }

    channel_meta = _load_channel_meta()
    music_channel_ids = get_music_labeled_channel_ids() if music_labeled_only else set()

    scored: list[tuple[float, dict]] = []
    seen_video_ids: set[str] = set()
    channel_caps: dict[str, int] = {}
    channel_counts: dict[str, int] = {}

    for sub in subs:
        channel_id = sub["channel_id"]
        is_music = channel_id in music_channel_ids
        if music_labeled_only and not is_music:
            continue
        if not music_labeled_only and is_music:
            continue

        meta = channel_meta.get(channel_id, {})
        rating = meta.get("rating", DEFAULT_CHANNEL_RATING)
        avg_interval = meta.get("avg_interval_days")
        cap = _channel_video_cap(rating, avg_interval)
        channel_caps[channel_id] = cap
        channel_counts[channel_id] = 0

        recent = list(meta.get("recent_videos") or [])
        for raw in recent:
            video_id = str(raw.get("video_id") or raw.get("videoId") or "").strip()
            if not video_id or video_id in seen_video_ids:
                continue
            score = _score_video(raw, rating, now)
            if score <= 0:
                continue
            seen_video_ids.add(video_id)
            thumb = raw.get("thumbnail") or f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
            video = {
                "videoId": video_id,
                "title": raw.get("title") or video_id,
                "author": meta.get("channel_name") or sub["channel_name"],
                "authorId": channel_id,
                "lengthSeconds": _as_int(raw.get("length_seconds") or raw.get("lengthSeconds")) or None,
                "viewCount": _as_int(raw.get("view_count") or raw.get("viewCount")) or None,
                "published": _as_int(raw.get("published")) or None,
                "videoThumbnails": [{"url": thumb}],
                "_channel_id": channel_id,
                "_score": score,
            }
            scored.append((score, video))

    scored.sort(key=lambda t: t[0], reverse=True)

    all_video_ids = [v["videoId"] for _, v in scored]
    ratings_map = get_ratings_for_video_ids(all_video_ids) if all_video_ids else {}

    results: list[dict] = []
    represented: set[str] = set()
    for score, video in scored:
        cid = video.get("_channel_id", "")
        cap = channel_caps.get(cid, 2)
        cnt = channel_counts.get(cid, 0)
        if cnt >= cap:
            continue
        vid = video["videoId"]
        r = ratings_map.get(vid)
        if r is not None and int(r) <= 1:
            continue
        channel_counts[cid] = cnt + 1
        represented.add(cid)
        out = {k: v for k, v in video.items() if not k.startswith("_")}
        if r is not None:
            out["my_rating"] = int(r)
        results.append(out)

    target_cache["signature"] = sig
    target_cache["fetched_at"] = now
    target_cache["videos"] = results
    target_cache["represented_channels"] = len(represented)

    page = results[offset: offset + limit]
    return {
        "videos": page,
        "total": len(results),
        "limit": limit,
        "offset": offset,
        "has_more": offset + limit < len(results),
        "represented_channels": len(represented),
    }
