"""Feed router for ytvideo.

GET /feed — calls recommenderr's /v1/ppr/feed with subscription seeds;
             local feed_feedback/feed_filters are applied as post-filters.
Feed feedback and filters are stored locally in ytvideo.db.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from backend.db import (
    get_subscriptions,
    get_feed_filters,
    add_feed_filter,
    delete_feed_filter,
    get_feed_feedback,
    get_disliked_video_ids,
    set_feed_feedback,
    delete_feed_feedback,
    ensure_channel_in_music_section,
)
from backend.clients import recommenderr as rec_client
from backend.services.subscription_rss import invalidate_subscription_feed_cache

router = APIRouter()

# recommenderr graph id for the video feed ("Videos").
VIDEO_GRAPH_ID = 3

DISLIKE_REASON_VALUES = frozenset({
    "already_watched", "too_much_channel", "not_interesting", "its_music"
})


def get_feed_categories():
    """Return categories present in local feed_feedback."""
    from backend.db import get_db
    with get_db() as con:
        rows = con.execute(
            "SELECT DISTINCT category FROM feed_feedback WHERE category != '' ORDER BY category"
        ).fetchall()
    return [r["category"] for r in rows]


@router.get("/feed")
async def feed(
    limit: int = Query(100),
    offset: int = Query(0),
    category: Optional[str] = Query(None),
    sort: str = Query("score"),
    max_spam_mass: float = Query(1.0),
):
    """Call recommenderr PPR feed with subscription channel seeds, apply local filters."""
    subs = get_subscriptions()
    seeds = [s["channel_id"] for s in subs]

    try:
        result = await rec_client.ppr_feed(seeds=seeds, limit=limit + offset + 50)
        videos = result.get("videos") or result.get("items") or []
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"recommenderr unavailable: {exc}")

    # Drop videos the user has explicitly disliked. recommenderr zeroes them as
    # PPR *seeds*, but they can still surface as PPR *targets* of other seeds, so
    # filter them out at serve time too.
    disliked = get_disliked_video_ids()
    if disliked:
        videos = [v for v in videos if v.get("video_id") not in disliked]

    # Apply local feed_filters
    filters = get_feed_filters()
    if filters:
        kw_filters = [f["match_value"] for f in filters if f["filter_type"] == "keyword"]
        cid_filters = {f["match_value"] for f in filters if f["filter_type"] == "channel_id"}
        cname_filters = [f["match_value"] for f in filters if f["filter_type"] == "channel_name"]

        def _passes(v: dict) -> bool:
            title = (v.get("title") or "").lower()
            author = (v.get("author") or "").lower()
            author_id = v.get("author_id") or v.get("authorId") or ""
            if any(kw in title for kw in kw_filters):
                return False
            if author_id in cid_filters:
                return False
            if any(cn in author for cn in cname_filters):
                return False
            return True

        videos = [v for v in videos if _passes(v)]

    # Apply category filter if given
    if category:
        videos = [v for v in videos if (v.get("category") or "").startswith(category)]

    total = len(videos)
    return {
        "videos": videos[offset: offset + limit],
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + limit < total,
    }


@router.get("/feed/categories")
async def feed_categories():
    return await run_in_threadpool(get_feed_categories)


@router.get("/feed/version")
async def feed_version():
    """Current recommenderr feed generation for the video graph. The frontend
    polls this cheaply; when it changes, recommenderr has recomputed and the
    local feed-batch cache should be dropped and re-warmed."""
    try:
        data = await rec_client.get("/v1/feed/generations")
        gen = (data.get("by_name") or {}).get("videos", 0)
        return {"generation": gen}
    except Exception as exc:
        return {"generation": None, "error": str(exc)}


@router.post("/feed/recompute")
async def feed_recompute():
    """Signal recommenderr to recompute PPR (fire-and-forget)."""
    try:
        subs = get_subscriptions()
        seeds = [s["channel_id"] for s in subs]
        await rec_client.ppr_feed(seeds=seeds, limit=1)
    except Exception:
        pass
    return {"ok": True}


@router.delete("/feed")
async def feed_clear():
    """Clear local feed state (feedback and filters) — recommenderr data is immutable from here."""
    from backend.db import get_db
    with get_db() as con:
        con.execute("DELETE FROM feed_feedback")
    return {"ok": True}


@router.get("/feed/{video_id}/keywords")
async def feed_video_keywords(video_id: str):
    """The video's YouTube tags, for the dislike panel's 'stop recommending …' chips."""
    try:
        return await rec_client.get(f"/v1/ppr/video-keywords/{video_id}", graph_id=VIDEO_GRAPH_ID)
    except Exception as exc:
        return {"video_id": video_id, "keywords": [], "filtered": [], "error": str(exc)}


class SuppressKeywordRequest(BaseModel):
    keyword: str


@router.post("/feed/suppress-keyword")
async def feed_suppress_keyword(body: SuppressKeywordRequest):
    """Add a recommenderr keyword filter so videos tagged with this keyword stop
    appearing in the feed (matches title OR YouTube tags)."""
    kw = body.keyword.strip().lower()
    if not kw:
        raise HTTPException(status_code=400, detail="keyword required")
    try:
        await rec_client.post("/v1/ppr/feed-filters", json={
            "filter_type": "keyword", "match_value": kw, "graph_id": VIDEO_GRAPH_ID,
        })
        return {"ok": True, "keyword": kw}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/feed/{video_id}/why")
async def feed_why(video_id: str):
    """Explain why a video appeared — delegate to recommenderr."""
    try:
        return await rec_client.get(f"/v1/ppr/why/{video_id}")
    except Exception as exc:
        return {"video_id": video_id, "reason": f"unavailable: {exc}"}


class FeedFeedbackRequest(BaseModel):
    category: str = ""
    feedback: int
    author_id: Optional[str] = None
    dislike_reason: Optional[str] = None


@router.get("/feed/{video_id}/feedback")
async def get_feedback(video_id: str, category: str = Query("")):
    return get_feed_feedback(video_id, category)


@router.post("/feed/{video_id}/feedback")
async def set_feedback(video_id: str, body: FeedFeedbackRequest):
    if body.feedback not in (1, -1):
        raise HTTPException(status_code=400, detail="feedback must be 1 or -1")
    if body.feedback == -1 and body.dislike_reason is not None and body.dislike_reason not in DISLIKE_REASON_VALUES:
        raise HTTPException(
            status_code=400,
            detail=f"dislike_reason must be one of: {', '.join(sorted(DISLIKE_REASON_VALUES))}",
        )
    if body.feedback != -1 and body.dislike_reason:
        raise HTTPException(status_code=400, detail="dislike_reason is only valid when feedback is -1")
    set_feed_feedback(video_id, body.category, body.feedback, body.author_id or None,
                      body.dislike_reason if body.feedback == -1 else None)
    if body.feedback == -1 and body.dislike_reason == "its_music" and body.author_id:
        if ensure_channel_in_music_section(body.author_id):
            invalidate_subscription_feed_cache()
    # "Not interesting" → fetch the video's recommended neighbours (once) so the
    # recommenderr negative-centroid scorer can suppress the whole semantic
    # cluster, not just this one video.  The crawl respects foreground
    # backpressure on recommenderr, so this stays gentle on egress.
    if body.feedback == -1 and body.dislike_reason == "not_interesting":
        try:
            await rec_client.enqueue_crawl(video_id)
        except Exception:
            pass  # suppression still works from already-cached edges; best-effort
    return {"ok": True}


@router.delete("/feed/{video_id}/feedback")
async def remove_feedback(video_id: str, category: str = Query("")):
    delete_feed_feedback(video_id, category)
    return {"ok": True}


@router.get("/feed/for-source")
async def feed_for_source(video_id: str = Query(...), limit: int = Query(24)):
    """Ask recommenderr for related videos for a source video."""
    lim = max(1, min(int(limit), 48))
    try:
        return await rec_client.get(f"/v1/ppr/for-source/{video_id}", limit=lim)
    except Exception as exc:
        return {"videos": [], "error": str(exc)}


@router.post("/feed/save-recs")
async def feed_save_recs(video_id: str = Query(...), title: str = Query("")):
    """Ask recommenderr to crawl recommendations for a watched video."""
    try:
        await rec_client.enqueue_crawl(video_id)
        return {"ok": True}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


class ExploreSeed(BaseModel):
    type: str
    id: str
    label: Optional[str] = None


class ExploreRequest(BaseModel):
    seeds: list[ExploreSeed]
    limit: int = 50


class ExploreSaveRequest(BaseModel):
    name: str
    video_ids: list[str]


@router.post("/explore")
async def explore(body: ExploreRequest):
    seeds = [{"type": s.type, "id": s.id} for s in body.seeds]
    try:
        return await rec_client.post("/v1/ppr/explore", json={"seeds": seeds, "limit": body.limit})
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.post("/explore/save-category")
async def explore_save_category(body: ExploreSaveRequest):
    from backend.db import get_db
    import time
    with get_db() as con:
        con.execute(
            "INSERT OR REPLACE INTO custom_categories (name, keywords, created_at) VALUES (?,?,?)",
            (body.name, "", time.time()),
        )
    return {"ok": True, "category": body.name, "count": len(body.video_ids)}


# ── Feed Filters ───────────────────────────────────────────

class FeedFilterRequest(BaseModel):
    filter_type: str
    match_value: str


@router.get("/feed-filters")
async def list_feed_filters():
    return get_feed_filters()


@router.post("/feed-filters")
async def create_feed_filter(body: FeedFilterRequest):
    if body.filter_type not in ("keyword", "channel_id", "channel_name"):
        raise HTTPException(status_code=400, detail="Invalid filter_type")
    if not body.match_value.strip():
        raise HTTPException(status_code=400, detail="match_value required")
    add_feed_filter(body.filter_type, body.match_value)
    return {"ok": True}


@router.delete("/feed-filters/{filter_id}")
async def remove_feed_filter(filter_id: int):
    delete_feed_filter(filter_id)
    return {"ok": True}


# ── Weight Rules (proxied to recommenderr) ─────────────────────────────────

class WeightRuleRequest(BaseModel):
    rule_type: str
    match_value: str
    multiplier: float


@router.get("/weight-rules")
async def list_weight_rules():
    try:
        return await rec_client.get("/v1/ppr/weight-rules")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.post("/weight-rules")
async def add_weight_rule(body: WeightRuleRequest):
    try:
        return await rec_client.post("/v1/ppr/weight-rules", json={
            "rule_type": body.rule_type,
            "match_value": body.match_value,
            "multiplier": body.multiplier,
        })
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.delete("/weight-rules/{rule_id}")
async def remove_weight_rule(rule_id: int):
    try:
        return await rec_client.delete(f"/v1/ppr/weight-rules/{rule_id}")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
