"""Internal bulk-export endpoints for recommenderr to sync user data."""
from __future__ import annotations

import os

from fastapi import APIRouter, Depends, Header, HTTPException, status

from backend.db import get_db

router = APIRouter(prefix="/internal")

_TOKEN = os.environ.get("RECOMMENDERR_TOKEN", "")


def _require_token(authorization: str | None = Header(default=None)) -> None:
    if not _TOKEN:
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing bearer token")
    if authorization.removeprefix("Bearer ").strip() != _TOKEN:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid bearer token")


@router.get("/history", dependencies=[Depends(_require_token)])
def export_history() -> list[dict]:
    with get_db() as con:
        rows = con.execute(
            "SELECT video_id, title, author_id, watched_at FROM watch_history"
        ).fetchall()
    return [dict(r) for r in rows]


@router.get("/playlists/videos", dependencies=[Depends(_require_token)])
def export_playlist_videos() -> list[dict]:
    with get_db() as con:
        rows = con.execute(
            "SELECT playlist_id, video_id, title, author_id FROM playlist_videos"
        ).fetchall()
    return [dict(r) for r in rows]


@router.get("/categories", dependencies=[Depends(_require_token)])
def export_categories() -> dict:
    """Full category snapshot so recommenderr can mirror it (IDs preserved) and
    run per-category recommendations against it."""
    with get_db() as con:
        categories = [
            dict(r) for r in con.execute(
                "SELECT id, name, parent_id, description FROM categories"
            ).fetchall()
        ]
        video_assignments = [
            dict(r) for r in con.execute(
                "SELECT video_id, category_id FROM video_category_assignments "
                "WHERE category_id IS NOT NULL"
            ).fetchall()
        ]
        channel_assignments = [
            dict(r) for r in con.execute(
                "SELECT channel_id, category_id FROM channel_category_assignments "
                "WHERE category_id IS NOT NULL"
            ).fetchall()
        ]
        category_tags = [
            dict(r) for r in con.execute(
                "SELECT category_id, tag_id FROM category_tags"
            ).fetchall()
        ]
        tags = [
            dict(r) for r in con.execute(
                "SELECT id, name FROM tags"
            ).fetchall()
        ]
        video_tags = [
            dict(r) for r in con.execute(
                "SELECT video_id, tag_id FROM video_tags"
            ).fetchall()
        ]
    return {
        "categories": categories,
        "video_assignments": video_assignments,
        "channel_assignments": channel_assignments,
        "category_tags": category_tags,
        "tags": tags,
        "video_tags": video_tags,
    }
