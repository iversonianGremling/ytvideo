"""mpv streaming router for ytvideo."""
from __future__ import annotations

import logging
from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse
from backend.services.mpv_service import mpv

logger = logging.getLogger("mpv")
router = APIRouter()


@router.get("/stream")
async def stream(
    request: Request,
    v: str = Query(...),
    quality: int = Query(720),
    start: float = Query(0),
):
    """Stream video via a single mpv process (h264+aac fMP4)."""
    proc = await mpv.start(v, quality, start)

    async def generate():
        try:
            while True:
                if await request.is_disconnected():
                    break
                chunk = await proc.stdout.read(65536)
                if not chunk:
                    break
                yield chunk
        finally:
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                await proc.wait()

    return StreamingResponse(
        generate(),
        media_type="video/mp4",
        headers={"Cache-Control": "no-cache", "Accept-Ranges": "none"},
    )
