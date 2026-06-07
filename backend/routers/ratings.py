"""Video and channel ratings router for ytvideo."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from backend.db import (
    get_rating,
    set_rating,
    delete_rating,
    get_liked_videos,
    get_channel_rating,
    set_channel_rating,
    delete_channel_rating,
)
from backend.services.subscription_rss import invalidate_subscription_feed_cache

router = APIRouter()


class RatingRequest(BaseModel):
    rating: int


class ChannelRatingRequest(BaseModel):
    rating: int
    channel_name: str = ""


@router.get("/liked")
async def liked_videos(limit: int = Query(200), offset: int = Query(0), sort: str = Query("rating")):
    return get_liked_videos(limit, offset, sort)


@router.get("/ratings/{video_id}")
async def get_video_rating(video_id: str):
    rating = get_rating(video_id)
    return {"rating": rating}


@router.post("/ratings/{video_id}")
async def rate_video(video_id: str, body: RatingRequest):
    if not (1 <= body.rating <= 10):
        raise HTTPException(status_code=400, detail="Rating must be between 1 and 10")
    set_rating(video_id, body.rating)
    return {"ok": True, "rating": body.rating}


@router.delete("/ratings/{video_id}")
async def remove_video_rating(video_id: str):
    delete_rating(video_id)
    return {"ok": True}


@router.get("/ratings/channel/{channel_id}")
async def get_channel_rating_ep(channel_id: str):
    result = get_channel_rating(channel_id)
    return result or {"rating": None}


@router.post("/ratings/channel/{channel_id}")
async def set_channel_rating_ep(channel_id: str, body: ChannelRatingRequest):
    if not (1 <= body.rating <= 10):
        raise HTTPException(status_code=400, detail="Rating must be between 1 and 10")
    set_channel_rating(channel_id, body.channel_name, body.rating)
    invalidate_subscription_feed_cache()
    return {"ok": True, "rating": body.rating}


@router.delete("/ratings/channel/{channel_id}")
async def delete_channel_rating_ep(channel_id: str):
    delete_channel_rating(channel_id)
    invalidate_subscription_feed_cache()
    return {"ok": True}
