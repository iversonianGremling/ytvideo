"""Watch history and progress router for ytvideo."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel

from backend.db import (
    add_to_history,
    get_history,
    get_history_genres,
    delete_history_item,
    clear_history,
    save_watch_progress,
    get_watch_progress,
    delete_watch_progress,
    save_playlist_progress,
    get_playlist_progress,
    delete_playlist_progress,
    get_continue_watching,
)

router = APIRouter()


class HistoryRequest(BaseModel):
    video_id: str
    title: str
    thumbnail: Optional[str] = None
    duration: Optional[int] = None
    author: Optional[str] = None
    author_id: Optional[str] = None


class WatchProgressRequest(BaseModel):
    position: float
    title: Optional[str] = None
    thumbnail: Optional[str] = None
    duration: Optional[int] = None
    author: Optional[str] = None
    author_id: Optional[str] = None
    media_type: str = "video"


class PlaylistProgressRequest(BaseModel):
    title: Optional[str] = None
    thumbnail: Optional[str] = None
    author: Optional[str] = None
    author_id: Optional[str] = None
    current_video_id: Optional[str] = None
    current_video_title: Optional[str] = None
    current_video_thumbnail: Optional[str] = None
    current_video_position: float
    current_video_duration: Optional[int] = None
    queue_index: int = 0
    total_items: Optional[int] = None
    kind: str = "playlist"


# Watch history

@router.post("/history")
async def history_add(body: HistoryRequest):
    add_to_history(body.video_id, body.title, body.thumbnail, body.duration, body.author, body.author_id)
    return {"ok": True}


@router.get("/history/genres")
async def history_genres():
    return get_history_genres()


@router.get("/history")
async def history_list(
    limit: int = Query(100),
    offset: int = Query(0),
    search: str = Query(""),
    category: str = Query(""),
    liked_only: bool = Query(False),
    order_by: str = Query("watched_at"),
):
    return get_history(limit, offset, search, category, liked_only, order_by)


@router.delete("/history")
async def history_clear():
    clear_history()
    return {"ok": True}


@router.delete("/history/{video_id}")
async def history_remove(video_id: str):
    delete_history_item(video_id)
    return {"ok": True}


# Watch progress

@router.post("/history/{video_id}/progress")
async def save_progress(video_id: str, body: WatchProgressRequest):
    save_watch_progress(
        video_id, body.title, body.thumbnail, body.duration,
        body.author, body.author_id, body.position, body.media_type,
    )
    return {"ok": True}


@router.get("/history/{video_id}/progress")
async def get_progress(video_id: str):
    p = get_watch_progress(video_id)
    return p if p else {"video_id": video_id, "position": 0}


@router.delete("/history/{video_id}/progress")
async def remove_progress(video_id: str):
    delete_watch_progress(video_id)
    return {"ok": True}


# Playlist progress

@router.post("/playlists/progress/{playlist_id}")
async def save_playlist_progress_ep(playlist_id: str, body: PlaylistProgressRequest):
    save_playlist_progress(
        playlist_id, body.title, body.thumbnail, body.author, body.author_id,
        body.current_video_id, body.current_video_title, body.current_video_thumbnail,
        body.current_video_position, body.current_video_duration,
        body.queue_index, body.total_items, body.kind,
    )
    return {"ok": True}


@router.get("/playlists/progress/{playlist_id}")
async def get_playlist_progress_ep(playlist_id: str):
    p = get_playlist_progress(playlist_id)
    return p if p else {"playlist_id": playlist_id, "queue_index": 0, "current_video_position": 0}


@router.delete("/playlists/progress/{playlist_id}")
async def remove_playlist_progress_ep(playlist_id: str):
    delete_playlist_progress(playlist_id)
    return {"ok": True}


@router.get("/continue-watching")
async def continue_watching(limit: int = Query(20)):
    return get_continue_watching(limit)
