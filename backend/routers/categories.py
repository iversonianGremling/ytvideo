"""Categories v2 router for ytvideo."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from backend.db import (
    create_category,
    update_category,
    delete_category,
    get_category_by_id,
    get_categories_tree,
    search_categories,
    get_category_videos,
    get_category_channels,
    create_tag,
    delete_tag,
    get_all_tags,
    search_tags,
    check_tag_category_conflicts,
    add_category_tag,
    remove_category_tag,
    set_video_category_v2,
    remove_video_category_v2,
    get_video_assignment,
    add_video_tag,
    remove_video_tag,
    set_channel_category_v2,
    remove_channel_category_v2,
    get_channel_assignment,
    add_channel_tag,
    remove_channel_tag,
    suggest_tags,
    get_tag_by_id,
    get_category_descendant_ids,
    get_db,
)
from backend.services.subscription_rss import invalidate_subscription_feed_cache
from backend.clients import recommenderr

router = APIRouter()


# ── Categories ─────────────────────────────────────────────

@router.get("/categories")
async def list_categories_tree():
    return await run_in_threadpool(get_categories_tree)


@router.get("/categories/search")
async def search_cats(q: str = Query(""), limit: int = Query(20)):
    return await run_in_threadpool(search_categories, q, limit)


class CreateCategoryBody(BaseModel):
    name: str
    parent_id: Optional[int] = None
    description: str = ""


@router.post("/categories")
async def create_cat(body: CreateCategoryBody):
    if not body.name.strip():
        raise HTTPException(400, "name required")
    cat_id = await run_in_threadpool(create_category, body.name, body.parent_id, body.description)
    return await run_in_threadpool(get_category_by_id, cat_id)


@router.get("/categories/{cat_id}")
async def get_cat(cat_id: int):
    cat = await run_in_threadpool(get_category_by_id, cat_id)
    if not cat:
        raise HTTPException(404, "Not found")
    return cat


class UpdateCategoryBody(BaseModel):
    name: Optional[str] = None
    parent_id: Optional[int] = None
    description: Optional[str] = None
    clear_parent: bool = False


@router.put("/categories/{cat_id}")
async def update_cat(cat_id: int, body: UpdateCategoryBody):
    cat = await run_in_threadpool(get_category_by_id, cat_id)
    if not cat:
        raise HTTPException(404, "Not found")
    if body.parent_id and body.parent_id == cat_id:
        raise HTTPException(400, "Category cannot be its own parent")
    await run_in_threadpool(update_category, cat_id, body.name, body.parent_id, body.description, body.clear_parent)
    return await run_in_threadpool(get_category_by_id, cat_id)


@router.delete("/categories/{cat_id}")
async def delete_cat(cat_id: int):
    cat = await run_in_threadpool(get_category_by_id, cat_id)
    if not cat:
        raise HTTPException(404, "Not found")
    await run_in_threadpool(delete_category, cat_id)
    return {"ok": True}


@router.get("/categories/{cat_id}/videos")
async def cat_videos(cat_id: int, include_children: bool = Query(True), limit: int = Query(50), offset: int = Query(0)):
    return await run_in_threadpool(get_category_videos, cat_id, include_children, limit, offset)


@router.get("/categories/{cat_id}/channels")
async def cat_channels(cat_id: int, include_children: bool = Query(True), limit: int = Query(50), offset: int = Query(0)):
    return await run_in_threadpool(get_category_channels, cat_id, include_children, limit, offset)


class CategoryTagBody(BaseModel):
    tag_id: int


@router.post("/categories/{cat_id}/tags")
async def add_cat_tag(cat_id: int, body: CategoryTagBody):
    await run_in_threadpool(add_category_tag, cat_id, body.tag_id)
    return {"ok": True}


@router.delete("/categories/{cat_id}/tags/{tag_id}")
async def remove_cat_tag(cat_id: int, tag_id: int):
    await run_in_threadpool(remove_category_tag, cat_id, tag_id)
    return {"ok": True}


@router.get("/categories/{cat_id}/video-ids")
async def cat_video_ids(cat_id: int, include_children: bool = Query(True)):
    with get_db() as con:
        ids = get_category_descendant_ids(con, cat_id) if include_children else [cat_id]
        ph = ",".join("?" * len(ids))
        rows = con.execute(
            f"SELECT video_id FROM video_category_assignments WHERE category_id IN ({ph})", ids
        ).fetchall()
    return [r["video_id"] for r in rows]


@router.get("/categories/{cat_id}/channel-ids")
async def cat_channel_ids(cat_id: int, include_children: bool = Query(True)):
    with get_db() as con:
        ids = get_category_descendant_ids(con, cat_id) if include_children else [cat_id]
        ph = ",".join("?" * len(ids))
        rows = con.execute(
            f"SELECT channel_id FROM channel_category_assignments WHERE category_id IN ({ph})", ids
        ).fetchall()
    return [r["channel_id"] for r in rows]


# ── Recommendations (proxied to recommenderr) ──────────────
# Category definitions/assignments are mirrored into recommenderr via its
# user_data_sync; the category_recs worker there ranks them. We just proxy.

@router.get("/categories/{cat_id}/recommendations")
async def cat_recommendations(cat_id: int, limit: int = Query(30)):
    try:
        return await recommenderr.get(f"/v1/categories/{cat_id}/recommendations", limit=limit)
    except Exception:
        # Engine unreachable / not yet synced — surface an empty "computing" state
        # rather than a 500 so the UI can keep polling.
        return {"status": "computing", "items": [], "last_run_at": None}


@router.post("/categories/{cat_id}/recompute")
async def cat_recompute(cat_id: int):
    cat = await run_in_threadpool(get_category_by_id, cat_id)
    if not cat:
        raise HTTPException(404, "Not found")
    try:
        return await recommenderr.post(f"/v1/categories/{cat_id}/recompute")
    except Exception as exc:
        raise HTTPException(502, f"recommenderr unreachable: {exc}")


@router.get("/categories/{cat_id}/playlist-overlap")
async def cat_playlist_overlap(cat_id: int, include_children: bool = Query(True)):
    # Playlists live in ytvideo's own DB, so this stays local.
    with get_db() as con:
        ids = get_category_descendant_ids(con, cat_id) if include_children else [cat_id]
        ph = ",".join("?" * len(ids))
        rows = con.execute(f"""
            SELECT p.id, p.title,
                   COUNT(DISTINCT vca.video_id) as overlap_count,
                   COUNT(DISTINCT pv.video_id) as total_count
            FROM playlists p
            JOIN playlist_videos pv ON pv.playlist_id = p.id
            LEFT JOIN video_category_assignments vca
                ON vca.video_id = pv.video_id AND vca.category_id IN ({ph})
            GROUP BY p.id
            HAVING overlap_count > 0
            ORDER BY overlap_count DESC
        """, ids).fetchall()
    return [dict(r) for r in rows]


# ── Tags ───────────────────────────────────────────────────

@router.get("/tags")
async def list_tags(limit: int = Query(500)):
    return await run_in_threadpool(get_all_tags, limit)


@router.get("/tags/search")
async def search_tags_ep(q: str = Query(""), limit: int = Query(15)):
    return await run_in_threadpool(search_tags, q, limit)


@router.get("/tags/suggest")
async def suggest_tags_ep(
    q: str = Query(""),
    category_id: Optional[int] = Query(None),
    video_id: Optional[str] = Query(None),
    channel_id: Optional[str] = Query(None),
    limit: int = Query(10),
):
    return await run_in_threadpool(suggest_tags, q, category_id, video_id, channel_id, limit)


@router.get("/tags/conflicts")
async def tag_conflicts(name: str = Query(...)):
    return {"conflicts": await run_in_threadpool(check_tag_category_conflicts, name)}


class CreateTagBody(BaseModel):
    name: str
    description: str = ""


@router.post("/tags")
async def create_tag_ep(body: CreateTagBody):
    if not body.name.strip():
        raise HTTPException(400, "name required")
    conflicts = await run_in_threadpool(check_tag_category_conflicts, body.name)
    tag_id = await run_in_threadpool(create_tag, body.name, body.description)
    tag = await run_in_threadpool(get_tag_by_id, tag_id)
    return {**tag, "conflicts": conflicts}


@router.delete("/tags/{tag_id}")
async def delete_tag_ep(tag_id: int):
    await run_in_threadpool(delete_tag, tag_id)
    return {"ok": True}


# ── Video assignments ──────────────────────────────────────

@router.get("/videos/{video_id}/assignment")
async def get_video_assign(video_id: str):
    return await run_in_threadpool(get_video_assignment, video_id)


class SetCategoryBody(BaseModel):
    category_id: int


@router.put("/videos/{video_id}/category")
async def set_video_cat(video_id: str, body: SetCategoryBody):
    await run_in_threadpool(set_video_category_v2, video_id, body.category_id)
    return await run_in_threadpool(get_video_assignment, video_id)


@router.delete("/videos/{video_id}/category")
async def remove_video_cat(video_id: str):
    await run_in_threadpool(remove_video_category_v2, video_id)
    return {"ok": True}


class VideoTagBody(BaseModel):
    tag_id: int


@router.post("/videos/{video_id}/tags")
async def add_vtag(video_id: str, body: VideoTagBody):
    await run_in_threadpool(add_video_tag, video_id, body.tag_id)
    return await run_in_threadpool(get_video_assignment, video_id)


@router.delete("/videos/{video_id}/tags/{tag_id}")
async def remove_vtag(video_id: str, tag_id: int):
    await run_in_threadpool(remove_video_tag, video_id, tag_id)
    return {"ok": True}


# ── Channel assignments ────────────────────────────────────

@router.get("/channels/{channel_id}/assignment")
async def get_channel_assign(channel_id: str):
    return await run_in_threadpool(get_channel_assignment, channel_id)


@router.put("/channels/{channel_id}/category")
async def set_channel_cat(channel_id: str, body: SetCategoryBody):
    await run_in_threadpool(set_channel_category_v2, channel_id, body.category_id)
    invalidate_subscription_feed_cache()
    return await run_in_threadpool(get_channel_assignment, channel_id)


@router.delete("/channels/{channel_id}/category")
async def remove_channel_cat(channel_id: str):
    await run_in_threadpool(remove_channel_category_v2, channel_id)
    invalidate_subscription_feed_cache()
    return {"ok": True}


class ChannelTagBody(BaseModel):
    tag_id: int


@router.post("/channels/{channel_id}/tags")
async def add_ctag(channel_id: str, body: ChannelTagBody):
    await run_in_threadpool(add_channel_tag, channel_id, body.tag_id)
    invalidate_subscription_feed_cache()
    return await run_in_threadpool(get_channel_assignment, channel_id)


@router.delete("/channels/{channel_id}/tags/{tag_id}")
async def remove_ctag(channel_id: str, tag_id: int):
    await run_in_threadpool(remove_channel_tag, channel_id, tag_id)
    invalidate_subscription_feed_cache()
    return {"ok": True}
