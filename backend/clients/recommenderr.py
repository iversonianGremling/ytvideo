"""Thin client wrapping calls to the recommenderr service.

All external-world fetches in ytvideo route through this module so the call
site stays free of URL/auth plumbing.
"""
from __future__ import annotations

import os
from typing import Any

import httpx

RECOMMENDERR_URL = os.environ.get("RECOMMENDERR_URL", "http://127.0.0.1:9001")
RECOMMENDERR_TOKEN = os.environ.get("RECOMMENDERR_TOKEN", "")
TIMEOUT = httpx.Timeout(10.0, connect=2.0)


def _headers() -> dict[str, str]:
    if RECOMMENDERR_TOKEN:
        return {"Authorization": f"Bearer {RECOMMENDERR_TOKEN}"}
    return {}


async def get(path: str, **params: Any) -> dict:
    async with httpx.AsyncClient(base_url=RECOMMENDERR_URL, timeout=TIMEOUT) as client:
        r = await client.get(path, params=params or None, headers=_headers())
        r.raise_for_status()
        return r.json()


async def post(path: str, json: dict | None = None) -> dict:
    async with httpx.AsyncClient(base_url=RECOMMENDERR_URL, timeout=TIMEOUT) as client:
        r = await client.post(path, json=json, headers=_headers())
        r.raise_for_status()
        return r.json()


async def delete(path: str) -> dict:
    async with httpx.AsyncClient(base_url=RECOMMENDERR_URL, timeout=TIMEOUT) as client:
        r = await client.delete(path, headers=_headers())
        r.raise_for_status()
        return r.json()


# Convenience wrappers for common endpoints:
async def search(q: str, page: int = 1, type: str = "video") -> dict:
    return await get("/v1/invidious/search", q=q, page=page, type=type)


async def video_info(video_id: str) -> dict:
    return await get(f"/v1/video/{video_id}/info")


async def video_formats(video_id: str) -> dict:
    return await get(f"/v1/video/{video_id}/formats")


async def recognize(video_id: str) -> dict:
    return await get(f"/v1/video/{video_id}/recognize")


async def enqueue_crawl(video_id: str) -> dict:
    return await post("/v1/crawl/enqueue", json={"video_id": video_id})


async def ppr_feed(seeds: list[str], limit: int = 100) -> dict:
    return await post("/v1/feed/videos", json={"seeds": seeds, "limit": limit})
