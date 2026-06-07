"""Video tags router for ytvideo."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from backend.db import (
    get_all_tags,
    create_tag,
    get_tag_by_id,
    get_tag_items,
    delete_tag,
    get_videos_by_tag,
    add_video_tag,
    remove_video_tag,
)

router = APIRouter()


class CreateTagBody(BaseModel):
    name: str
    description: str = ""


class UpdateTagBody(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None


@router.get("/tags")
async def list_tags(limit: int = Query(500)):
    return await run_in_threadpool(get_all_tags, limit)


@router.get("/tags/{tag_id}/items")
async def tag_items_ep(tag_id: int, limit: int = Query(300)):
    return await run_in_threadpool(get_tag_items, tag_id, limit)


@router.post("/tags")
async def create_tag_ep(body: CreateTagBody):
    if not body.name.strip():
        raise HTTPException(400, "name required")
    tag_id = await run_in_threadpool(create_tag, body.name, body.description)
    return await run_in_threadpool(get_tag_by_id, tag_id)


@router.get("/tags/{tag_id}")
async def get_tag(tag_id: int):
    tag = await run_in_threadpool(get_tag_by_id, tag_id)
    if not tag:
        raise HTTPException(404, "Tag not found")
    return tag


@router.put("/tags/{tag_id}")
async def update_tag(tag_id: int, body: UpdateTagBody):
    from backend.db import get_db
    import time
    with get_db() as con:
        if body.name is not None:
            con.execute("UPDATE tags SET name=? WHERE id=?", (body.name.strip(), tag_id))
        if body.description is not None:
            con.execute("UPDATE tags SET description=? WHERE id=?", (body.description, tag_id))
    return await run_in_threadpool(get_tag_by_id, tag_id)


@router.delete("/tags/{tag_id}")
async def delete_tag_ep(tag_id: int):
    await run_in_threadpool(delete_tag, tag_id)
    return {"ok": True}


@router.get("/tags/{tag_id}/videos")
async def get_tag_videos(tag_id: int, limit: int = Query(100)):
    return await run_in_threadpool(get_videos_by_tag, tag_id, limit)


class VideoTagBody(BaseModel):
    video_id: str
    tag_id: int


@router.post("/video-tags")
async def add_video_tag_ep(body: VideoTagBody):
    await run_in_threadpool(add_video_tag, body.video_id, body.tag_id)
    return {"ok": True}


@router.delete("/video-tags/{video_id}/{tag_id}")
async def remove_video_tag_ep(video_id: str, tag_id: int):
    await run_in_threadpool(remove_video_tag, video_id, tag_id)
    return {"ok": True}
