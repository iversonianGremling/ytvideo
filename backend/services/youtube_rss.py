"""Fetch YouTube channel uploads via the official Atom feed (RSS).

Adapted from monolith: replaces invidious_client with recommenderr client's
base URL for the Invidious-proxied feed fallback.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional
from xml.etree import ElementTree as ET

import httpx

logger = logging.getLogger(__name__)

YOUTUBE_RSS_URL = "https://www.youtube.com/feeds/videos.xml"
RSS_TIMEOUT_SECONDS = 15.0
RECOMMENDERR_URL = os.environ.get("RECOMMENDERR_URL", "http://127.0.0.1:9001")
INVIDIOUS_URL = os.environ.get("INVIDIOUS_URL", "http://127.0.0.1:3000")

_RSS_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}

_client: Optional[httpx.AsyncClient] = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=RSS_TIMEOUT_SECONDS)
    return _client


def _parse_timestamp(value: str) -> int:
    if not value:
        return 0
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except Exception:
        return 0


def _parse_feed_xml(text: str, channel_id: str, default_channel_name: str):
    root = ET.fromstring(text)
    feed_title = root.findtext("atom:title", default=default_channel_name, namespaces=_RSS_NS) or default_channel_name
    videos = []

    for entry in root.findall("atom:entry", _RSS_NS):
        video_id = entry.findtext("yt:videoId", default="", namespaces=_RSS_NS)
        if not video_id:
            continue

        thumb_el = entry.find("media:group/media:thumbnail", _RSS_NS)
        thumb_url = thumb_el.attrib.get("url") if thumb_el is not None else ""
        if not thumb_url:
            thumb_url = f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"

        published = _parse_timestamp(
            entry.findtext("atom:published", default="", namespaces=_RSS_NS)
            or entry.findtext("atom:updated", default="", namespaces=_RSS_NS)
        )

        videos.append({
            "videoId": video_id,
            "title": entry.findtext("atom:title", default=video_id, namespaces=_RSS_NS),
            "published": published,
            "videoThumbnails": [{"url": thumb_url}],
            "viewCount": None,
            "lengthSeconds": None,
        })

    return feed_title, videos


async def _fetch_rss_xml(
    channel_id: str, default_channel_name: str, url: str, *, params: dict | None = None
) -> tuple[dict[str, Any] | None, BaseException | None]:
    try:
        resp = await _get_client().get(url, params=params or {})
        resp.raise_for_status()
    except Exception as exc:
        return None, exc

    try:
        channel_title, videos = _parse_feed_xml(resp.text, channel_id, default_channel_name)
    except ET.ParseError as exc:
        return None, exc

    thumb: str | None = None
    if videos:
        vt = videos[0].get("videoThumbnails") or []
        if vt:
            thumb = vt[0].get("url")

    return ({"channel_name": channel_title, "thumbnail": thumb, "videos": videos}, None)


async def _fetch_invidious_api(channel_id: str, default_channel_name: str) -> tuple[dict[str, Any] | None, BaseException | None]:
    """Fetch channel uploads via Invidious /api/v1/channels/{id}/videos (InnerTube-based)."""
    url = f"{INVIDIOUS_URL.rstrip('/')}/api/v1/channels/{channel_id}/videos"
    try:
        resp = await _get_client().get(url, params={"sort_by": "newest"})
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        return None, exc

    raw_videos = data.get("videos") or []
    if not raw_videos:
        return None, Exception("empty video list")

    channel_name = (raw_videos[0].get("author") or default_channel_name) if raw_videos else default_channel_name
    videos = []
    for v in raw_videos:
        video_id = v.get("videoId") or ""
        if not video_id:
            continue
        thumbs = v.get("videoThumbnails") or []
        thumb_url = thumbs[0].get("url", "") if thumbs else f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
        videos.append({
            "videoId": video_id,
            "title": v.get("title") or video_id,
            "published": v.get("published") or 0,
            "videoThumbnails": [{"url": thumb_url}],
            "viewCount": v.get("viewCount"),
            "lengthSeconds": v.get("lengthSeconds"),
        })

    thumb = videos[0]["videoThumbnails"][0]["url"] if videos else None
    return {"channel_name": channel_name, "thumbnail": thumb, "videos": videos}, None


async def fetch_channel_videos_rss(channel_id: str, default_channel_name: str) -> dict[str, Any] | None:
    """Fetch channel uploads. Tries YouTube RSS first; falls back to Invidious RSS, then Invidious API."""
    def n_v(d: dict | None) -> int:
        return len((d or {}).get("videos") or [])

    # Primary: direct YouTube RSS (no proxy, fast)
    yt_data, yt_err = await _fetch_rss_xml(
        channel_id, default_channel_name, YOUTUBE_RSS_URL,
        params={"channel_id": channel_id},
    )
    if yt_err:
        logger.debug("YouTube RSS failed for %s: %s", channel_id, yt_err)
    if yt_data and n_v(yt_data) > 0:
        return yt_data

    # Fallback: Invidious RSS (proxied, only if YouTube RSS failed/empty)
    inv_url = f"{INVIDIOUS_URL.rstrip('/')}/feed/channel/{channel_id}"
    iv_data, iv_err = await _fetch_rss_xml(channel_id, default_channel_name, inv_url)
    if iv_err:
        logger.debug("Invidious RSS failed for %s: %s", channel_id, iv_err)
    if iv_data and n_v(iv_data) > 0:
        return iv_data

    # Last resort: Invidious InnerTube API
    api_data, api_err = await _fetch_invidious_api(channel_id, default_channel_name)
    if api_err:
        logger.info("Invidious API fallback failed for %s: %s", channel_id, api_err)
    if api_data and n_v(api_data) > 0:
        return api_data

    return yt_data or iv_data or None
