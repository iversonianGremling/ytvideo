"""ytvideo — video frontend backend.

Owns video-mode user state (subscriptions, video playlists, watch history,
ratings, categories, tags). Calls recommenderr for all external data.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI

load_dotenv()

DB_PATH = os.environ.get("DB_PATH", "/opt/ytvideo/data/ytvideo.db")
SCHEMA_PATH = Path(__file__).parent / "schema.sql"
LISTEN_HOST = os.environ.get("LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "9002"))
RECOMMENDERR_URL = os.environ.get("RECOMMENDERR_URL", "http://127.0.0.1:9001")
RECOMMENDERR_TOKEN = os.environ.get("RECOMMENDERR_TOKEN", "")
SCHEMA_VERSION = 1


def init_db() -> None:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA foreign_keys=ON")
        con.executescript(SCHEMA_PATH.read_text())
        con.execute(
            "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION, time.time()),
        )
        con.commit()
    finally:
        con.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # Ensure db module's channel_stats table also exists
    from backend.db import _init_channel_stats
    _init_channel_stats()

    # Start takeout import background worker
    from backend.services.takeout_imports import takeout_import_worker
    import_task = asyncio.create_task(takeout_import_worker())

    # Start subscription stats background worker
    from backend.routers.subscriptions import subscription_stats_background_worker
    sub_worker_task = asyncio.create_task(subscription_stats_background_worker())

    yield

    import_task.cancel()
    sub_worker_task.cancel()
    try:
        await import_task
    except asyncio.CancelledError:
        pass
    try:
        await sub_worker_task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="ytvideo", lifespan=lifespan)


@app.get("/health")
def health() -> dict:
    return {
        "service": "ytvideo",
        "status": "ok",
        "schema_version": SCHEMA_VERSION,
        "recommenderr_url": RECOMMENDERR_URL,
    }


# Routers
from backend.routers import subscriptions, playlists, history, ratings, categories, tags, feed, imports, mpv, internal

app.include_router(subscriptions.router)
app.include_router(playlists.router)
app.include_router(history.router)
app.include_router(ratings.router)
app.include_router(categories.router)
app.include_router(tags.router)
app.include_router(feed.router)
app.include_router(imports.router)
app.include_router(mpv.router, prefix="/mpv")
app.include_router(internal.router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=LISTEN_HOST, port=LISTEN_PORT)
