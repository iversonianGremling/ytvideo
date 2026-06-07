"""Playlists router for ytvideo."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from backend.db import (
    get_db,
    get_playlists,
    create_playlist,
    delete_playlist,
    update_playlist,
    get_playlist,
    add_video_to_playlist,
    remove_video_from_playlist,
)

router = APIRouter()


class CreatePlaylistRequest(BaseModel):
    title: str
    description: Optional[str] = ""


class UpdatePlaylistRequest(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None


class AddVideoRequest(BaseModel):
    video_id: str
    title: Optional[str] = None
    thumbnail: Optional[str] = None
    duration: Optional[int] = None
    author: Optional[str] = None
    author_id: Optional[str] = None


class CreateCategoryFromPlaylistRequest(BaseModel):
    category_name: str = ""
    parent: str = ""
    boost: float = 2.0


@router.get("/playlists")
async def list_playlists():
    return get_playlists()


@router.post("/playlists")
async def create_pl(body: CreatePlaylistRequest):
    pid = create_playlist(body.title, body.description or "")
    return {"id": pid, "title": body.title}


@router.get("/playlists/{playlist_id}")
async def get_pl(playlist_id: int, limit: int = Query(100), offset: int = Query(0)):
    pl = get_playlist(playlist_id, limit, offset)
    if not pl:
        raise HTTPException(status_code=404, detail="Playlist not found")
    return pl


@router.put("/playlists/{playlist_id}")
async def update_pl(playlist_id: int, body: UpdatePlaylistRequest):
    update_playlist(playlist_id, body.title, body.description)
    return {"ok": True}


@router.delete("/playlists/{playlist_id}")
async def delete_pl(playlist_id: int):
    delete_playlist(playlist_id)
    return {"ok": True}


@router.post("/playlists/{playlist_id}/videos")
async def add_video(playlist_id: int, body: AddVideoRequest):
    with get_db() as con:
        row = con.execute("SELECT id FROM playlists WHERE id = ?", (playlist_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Playlist not found")
    added = add_video_to_playlist(
        playlist_id, body.video_id, body.title or body.video_id,
        body.thumbnail, body.duration, body.author, body.author_id,
    )
    return {"ok": True, "added": added}


@router.delete("/playlists/{playlist_id}/videos/{video_id}")
async def remove_video(playlist_id: int, video_id: str):
    remove_video_from_playlist(playlist_id, video_id)
    return {"ok": True}


@router.post("/playlists/{playlist_id}/create-category")
async def playlist_create_category(playlist_id: int, body: CreateCategoryFromPlaylistRequest):
    """Create a custom category from playlist videos."""
    import re
    from collections import Counter

    pl = get_playlist(playlist_id, limit=500, offset=0)
    if not pl:
        raise HTTPException(status_code=404, detail="Playlist not found")

    videos = pl.get("videos", [])
    if not videos:
        raise HTTPException(status_code=400, detail="Playlist is empty")

    cat_name = (body.category_name.strip() or pl["title"]).strip()
    full_cat = f"{body.parent}/{cat_name}" if body.parent else cat_name

    STOP = {
        "the","a","an","and","or","of","in","on","at","to","for","with","by","from",
        "is","it","its","this","that","was","are","be","been","have","has","had",
        "i","my","you","your","we","our","they","their","he","she","his","her",
        "how","what","why","when","where","which","who","not","no","all","more",
        "but","so","if","as","up","do","did","get","got","can","will","just",
        "about","after","into","over","out","than","then","s","t","re","ve","d",
        "1","2","3","4","5","10","100","ft","feat","official","video","audio",
        "new","vs","ep","full","live","part","best","top","most",
    }

    with get_db() as con:
        video_ids = [v["video_id"] for v in videos]
        ph = ",".join("?" * len(video_ids))
        kw_counter: Counter = Counter()
        for v in videos:
            words = re.findall(r"[a-z]{3,}", (v.get("title") or "").lower())
            for w in words:
                if w not in STOP:
                    kw_counter[w] += 1

    author_counter: Counter = Counter(v["author"].lower() for v in videos if v.get("author"))
    total = len(videos)
    dominant_authors = [a for a, c in author_counter.items() if c / total >= 0.2]
    top_keywords = [kw for kw, cnt in kw_counter.most_common(20) if cnt >= 2][:15]
    all_keywords = top_keywords + dominant_authors

    # Store as custom_category
    with get_db() as con:
        import time
        con.execute(
            "INSERT OR REPLACE INTO custom_categories (name, keywords, created_at) VALUES (?,?,?)",
            (full_cat, ", ".join(all_keywords), time.time()),
        )

    return {
        "ok": True,
        "category": full_cat,
        "keywords": top_keywords,
        "dominant_authors": dominant_authors,
        "videos_assigned": len(videos),
        "boost": body.boost,
    }
