"""DB access for ytvideo.

Helpers extracted from monolith's database.py that touch ytvideo-owned tables:
subscriptions, playlists, watch_history, watch_progress, video_ratings,
channel_ratings, categories, tags, feed_filters, feed_feedback,
takeout_import_jobs, channel_stats.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager

def _db_path() -> str:
    return os.environ.get("DB_PATH", "/opt/ytvideo/data/ytvideo.db")


@contextmanager
def get_db():
    con = sqlite3.connect(_db_path())
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    try:
        yield con
        con.commit()
    finally:
        con.close()


# ── channel_stats (ytvideo owns these for subscription browse) ──────────────

def _init_channel_stats():
    with get_db() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS channel_stats (
                channel_id TEXT PRIMARY KEY,
                channel_name TEXT,
                thumbnail TEXT,
                sub_count INTEGER,
                video_count INTEGER,
                last_upload_at REAL,
                avg_interval_days REAL,
                pattern TEXT,
                themes TEXT,
                recent_videos TEXT,
                fetched_at REAL
            )
        """)
        # Migration: separate "last successful fetch" (fetched_at, drives feed
        # freshness) from "last attempt" (last_attempt_at) + consecutive failures
        # (fail_count), so failing channels back off instead of being re-fetched
        # every tick. See get_stale_channel_ids() for the backoff gate.
        cols = {r[1] for r in con.execute("PRAGMA table_info(channel_stats)")}
        if "last_attempt_at" not in cols:
            con.execute("ALTER TABLE channel_stats ADD COLUMN last_attempt_at REAL")
        if "fail_count" not in cols:
            con.execute("ALTER TABLE channel_stats ADD COLUMN fail_count INTEGER DEFAULT 0")


# ── Subscriptions ──────────────────────────────────────────────────────────

def get_subscriptions() -> list[dict]:
    with get_db() as con:
        rows = con.execute(
            "SELECT * FROM subscriptions ORDER BY channel_name COLLATE NOCASE"
        ).fetchall()
    return [dict(r) for r in rows]


def add_subscription(channel_id: str, channel_name: str, thumbnail: str | None = None):
    with get_db() as con:
        con.execute(
            "INSERT OR REPLACE INTO subscriptions (channel_id, channel_name, thumbnail, added_at) VALUES (?,?,?,?)",
            (channel_id, channel_name, thumbnail, time.time()),
        )


def remove_subscription(channel_id: str):
    with get_db() as con:
        con.execute("DELETE FROM subscriptions WHERE channel_id = ?", (channel_id,))


def is_subscribed(channel_id: str) -> bool:
    with get_db() as con:
        row = con.execute(
            "SELECT 1 FROM subscriptions WHERE channel_id = ?", (channel_id,)
        ).fetchone()
    return row is not None


def save_channel_stats(
    channel_id: str,
    channel_name: str,
    thumbnail: str | None,
    sub_count,
    video_count,
    last_upload_at,
    avg_interval_days,
    pattern: str,
    themes: list,
    recent_videos: list,
):
    with get_db() as con:
        con.execute(
            """
            INSERT OR REPLACE INTO channel_stats
            (channel_id, channel_name, thumbnail, sub_count, video_count, last_upload_at,
             avg_interval_days, pattern, themes, recent_videos, fetched_at,
             last_attempt_at, fail_count)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0)
            """,
            (
                channel_id, channel_name, thumbnail, sub_count, video_count,
                last_upload_at, avg_interval_days, pattern,
                ",".join(themes), json.dumps(recent_videos), time.time(),
                time.time(),
            ),
        )


def touch_channel_attempt(channel_id: str, channel_name: str | None = None) -> None:
    """Record a *failed* refresh attempt: bump last_attempt_at + fail_count without
    touching fetched_at/recent_videos. This is what makes failing channels back off
    (see get_stale_channel_ids) instead of being re-fetched on every worker tick."""
    now = time.time()
    with get_db() as con:
        cur = con.execute(
            "UPDATE channel_stats SET last_attempt_at = ?, "
            "fail_count = COALESCE(fail_count, 0) + 1 WHERE channel_id = ?",
            (now, channel_id),
        )
        if cur.rowcount == 0:
            # Never-fetched channel: create a stub so it also backs off.
            con.execute(
                "INSERT OR IGNORE INTO channel_stats "
                "(channel_id, channel_name, fetched_at, last_attempt_at, fail_count) "
                "VALUES (?,?,?,?,1)",
                (channel_id, channel_name, None, now),
            )


# Retry backoff for channels whose refresh keeps failing. A channel is eligible
# for a refresh when its data is stale AND it hasn't been *attempted* within
# min(MAX, BASE * 2**fail_count). This stops the runaway loop where a failing
# fetch never updates fetched_at and so gets re-fetched on every 60s tick.
STALE_RETRY_BASE_SEC = 1800.0      # 30 min after the first failure
STALE_RETRY_MAX_SEC = 86400.0      # capped at 24h between attempts


def get_stale_channel_ids(max_age_hours: int = 48) -> list[dict]:
    now = time.time()
    cutoff = now - max_age_hours * 3600
    with get_db() as con:
        rows = con.execute(
            """
            SELECT s.channel_id, s.channel_name FROM subscriptions s
            LEFT JOIN channel_stats cs ON cs.channel_id = s.channel_id
            WHERE (cs.channel_id IS NULL OR cs.fetched_at IS NULL OR cs.fetched_at < ?)
              AND (
                    cs.last_attempt_at IS NULL
                    OR (? - cs.last_attempt_at) >=
                       min(?, ? * (1 << min(COALESCE(cs.fail_count, 0), 20)))
                  )
            ORDER BY COALESCE(cs.last_attempt_at, 0) ASC
            """,
            (cutoff, now, STALE_RETRY_MAX_SEC, STALE_RETRY_BASE_SEC),
        ).fetchall()
    return [dict(r) for r in rows]


def get_channel_browse_page(
    page: int = 1,
    per_page: int = 20,
    pattern: str = "",
    hide_rated: bool = False,
    sort: str = "name",
    search: str = "",
    hide_categorized: bool = False,
) -> dict:
    where = []
    params: list = []

    if search:
        where.append("s.channel_name LIKE ?")
        params.append(f"%{search}%")
    if pattern:
        where.append("COALESCE(cs.pattern,'unknown') = ?")
        params.append(pattern)
    if hide_rated:
        where.append("cr.rating IS NULL")
    if hide_categorized:
        where.append("cca.category_id IS NULL")

    where_clause = ("WHERE " + " AND ".join(where)) if where else ""

    order_map = {
        "name":        "s.channel_name COLLATE NOCASE ASC",
        "last_upload": "cs.last_upload_at DESC NULLS LAST",
        "rating":      "CAST(cr.rating AS INTEGER) DESC NULLS LAST",
        "rating_date": "CAST(cr.rating AS INTEGER) DESC NULLS LAST, cs.last_upload_at DESC NULLS LAST",
        "video_count": "cs.video_count DESC NULLS LAST",
        "pattern":     'COALESCE(cs.pattern,"zzz") ASC',
    }
    order = order_map.get(sort, order_map["name"])

    with get_db() as con:
        total = con.execute(
            f"""
            SELECT COUNT(*) FROM subscriptions s
            LEFT JOIN channel_stats cs ON cs.channel_id = s.channel_id
            LEFT JOIN channel_ratings cr ON cr.channel_id = s.channel_id
            LEFT JOIN channel_category_assignments cca ON cca.channel_id = s.channel_id
            {where_clause}
            """,
            params,
        ).fetchone()[0]

        offset = (page - 1) * per_page
        rows = con.execute(
            f"""
            SELECT s.channel_id, s.channel_name,
                   COALESCE(cs.thumbnail, s.thumbnail) as thumbnail,
                   cs.sub_count, cs.video_count,
                   cs.last_upload_at, cs.avg_interval_days, cs.pattern,
                   cs.themes, cs.recent_videos, cs.fetched_at,
                   cr.rating,
                   cat.name as category_name, cat.id as category_id
            FROM subscriptions s
            LEFT JOIN channel_stats cs ON cs.channel_id = s.channel_id
            LEFT JOIN channel_ratings cr ON cr.channel_id = s.channel_id
            LEFT JOIN channel_category_assignments cca ON cca.channel_id = s.channel_id
            LEFT JOIN categories cat ON cat.id = cca.category_id
            {where_clause}
            ORDER BY {order}
            LIMIT ? OFFSET ?
            """,
            params + [per_page, offset],
        ).fetchall()

        all_video_ids = []
        items = []
        for r in rows:
            d = dict(r)
            videos = []
            if d.get("recent_videos"):
                try:
                    videos = json.loads(d["recent_videos"])
                except Exception:
                    videos = []
            d["recent_videos"] = videos
            d["themes"] = [t.strip() for t in (d.get("themes") or "").split(",") if t.strip()]
            all_video_ids.extend(v.get("video_id") for v in videos if v.get("video_id"))
            items.append(d)

        video_ratings: dict = {}
        if all_video_ids:
            ph = ",".join("?" * len(all_video_ids))
            rating_rows = con.execute(
                f"SELECT video_id, rating FROM video_ratings WHERE video_id IN ({ph})",
                all_video_ids,
            ).fetchall()
            video_ratings = {r["video_id"]: r["rating"] for r in rating_rows}

    for item in items:
        for v in item["recent_videos"]:
            vid = v.get("video_id")
            if vid and vid in video_ratings:
                v["my_rating"] = video_ratings[vid]

    return {"items": items, "total": total, "page": page, "per_page": per_page}


def get_channel_stats_summary() -> dict:
    with get_db() as con:
        total = con.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0]
        cached = con.execute("SELECT COUNT(*) FROM channel_stats").fetchone()[0]
        patterns = con.execute(
            """
            SELECT COALESCE(cs.pattern, 'uncached') as pattern, COUNT(*) as cnt
            FROM subscriptions s
            LEFT JOIN channel_stats cs ON cs.channel_id = s.channel_id
            GROUP BY pattern
            """
        ).fetchall()
    return {
        "total": total,
        "cached": cached,
        "patterns": {r["pattern"]: r["cnt"] for r in patterns},
    }


# ── Playlists ──────────────────────────────────────────────────────────────

def get_playlists() -> list[dict]:
    with get_db() as con:
        rows = con.execute(
            """
            SELECT p.*, COUNT(pv.id) as video_count,
                   (SELECT pv2.thumbnail FROM playlist_videos pv2
                    WHERE pv2.playlist_id = p.id
                    ORDER BY pv2.added_at DESC, pv2.position DESC, pv2.id DESC
                    LIMIT 1) as first_thumb
            FROM playlists p
            LEFT JOIN playlist_videos pv ON pv.playlist_id = p.id
            GROUP BY p.id
            ORDER BY p.updated_at DESC
            """
        ).fetchall()
    return [dict(r) for r in rows]


def create_playlist(title: str, description: str = "") -> int:
    now = time.time()
    with get_db() as con:
        cur = con.execute(
            "INSERT INTO playlists (title, description, created_at, updated_at) VALUES (?,?,?,?)",
            (title, description, now, now),
        )
        return cur.lastrowid


def delete_playlist(playlist_id: int):
    with get_db() as con:
        con.execute("DELETE FROM playlists WHERE id = ?", (playlist_id,))


def update_playlist(playlist_id: int, title: str | None = None, description: str | None = None):
    with get_db() as con:
        if title is not None:
            con.execute(
                "UPDATE playlists SET title=?, updated_at=? WHERE id=?",
                (title, time.time(), playlist_id),
            )
        if description is not None:
            con.execute(
                "UPDATE playlists SET description=?, updated_at=? WHERE id=?",
                (description, time.time(), playlist_id),
            )


def get_playlist(playlist_id: int, limit: int | None = None, offset: int = 0) -> dict | None:
    with get_db() as con:
        pl = con.execute("SELECT * FROM playlists WHERE id = ?", (playlist_id,)).fetchone()
        if not pl:
            return None
        total = con.execute(
            "SELECT COUNT(*) FROM playlist_videos WHERE playlist_id = ?", (playlist_id,)
        ).fetchone()[0]
        query = "SELECT * FROM playlist_videos WHERE playlist_id = ? ORDER BY added_at DESC, position DESC"
        qparams: list = [playlist_id]
        if limit is not None:
            query += " LIMIT ? OFFSET ?"
            qparams.extend([limit, offset])
        videos = con.execute(query, qparams).fetchall()

    result = dict(pl)
    result["video_count"] = total
    result["videos"] = [dict(v) for v in videos]
    return result


def add_video_to_playlist(
    playlist_id: int, video_id: str, title: str,
    thumbnail: str | None = None, duration: int | None = None,
    author: str | None = None, author_id: str | None = None,
) -> bool:
    now = time.time()
    with get_db() as con:
        row = con.execute(
            "SELECT MAX(position) as mx FROM playlist_videos WHERE playlist_id = ?", (playlist_id,)
        ).fetchone()
        pos = (row["mx"] or 0) + 1
        try:
            con.execute(
                "DELETE FROM playlist_video_overrides WHERE playlist_id = ? AND video_id = ?",
                (playlist_id, video_id),
            )
            con.execute(
                "INSERT INTO playlist_videos "
                "(playlist_id, video_id, title, thumbnail, duration, author, author_id, position, added_at, source_managed) "
                "VALUES (?,?,?,?,?,?,?,?,?,0)",
                (playlist_id, video_id, title, thumbnail, duration, author, author_id, pos, now),
            )
            con.execute(
                "UPDATE playlists SET updated_at=? WHERE id=?", (now, playlist_id)
            )
            return True
        except sqlite3.IntegrityError:
            con.rollback()
            return False


def remove_video_from_playlist(playlist_id: int, video_id: str):
    now = time.time()
    with get_db() as con:
        row = con.execute(
            """
            SELECT pv.source_managed, p.source_playlist_id, p.source_updated_at
            FROM playlist_videos pv
            JOIN playlists p ON p.id = pv.playlist_id
            WHERE pv.playlist_id = ? AND pv.video_id = ?
            """,
            (playlist_id, video_id),
        ).fetchone()
        if row and row["source_managed"] and (
            row["source_playlist_id"] is not None or row["source_updated_at"] is not None
        ):
            con.execute(
                """
                INSERT INTO playlist_video_overrides (playlist_id, video_id, is_deleted, updated_at)
                VALUES (?,?,1,?)
                ON CONFLICT(playlist_id, video_id) DO UPDATE SET
                    is_deleted=excluded.is_deleted, updated_at=excluded.updated_at
                """,
                (playlist_id, video_id, now),
            )
        else:
            con.execute(
                "DELETE FROM playlist_video_overrides WHERE playlist_id=? AND video_id=?",
                (playlist_id, video_id),
            )
        con.execute(
            "DELETE FROM playlist_videos WHERE playlist_id=? AND video_id=?",
            (playlist_id, video_id),
        )
        con.execute("UPDATE playlists SET updated_at=? WHERE id=?", (now, playlist_id))


# ── Watch Progress + History ───────────────────────────────────────────────

def save_watch_progress(
    video_id: str, title: str | None, thumbnail: str | None,
    duration: int | None, author: str | None, author_id: str | None,
    position: int, media_type: str = "video",
):
    with get_db() as con:
        con.execute(
            """
            INSERT INTO watch_progress
                (video_id, title, thumbnail, duration, author, author_id, media_type, position, last_updated)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(video_id) DO UPDATE SET
                title=COALESCE(excluded.title, title),
                thumbnail=COALESCE(excluded.thumbnail, thumbnail),
                duration=COALESCE(excluded.duration, duration),
                author=COALESCE(excluded.author, author),
                author_id=COALESCE(excluded.author_id, author_id),
                media_type=COALESCE(excluded.media_type, media_type),
                position=excluded.position,
                last_updated=excluded.last_updated
            """,
            (video_id, title, thumbnail, duration, author, author_id, media_type, position, time.time()),
        )


def get_watch_progress(video_id: str) -> dict | None:
    with get_db() as con:
        row = con.execute(
            "SELECT * FROM watch_progress WHERE video_id = ?", (video_id,)
        ).fetchone()
    return dict(row) if row else None


def delete_watch_progress(video_id: str):
    with get_db() as con:
        con.execute("DELETE FROM watch_progress WHERE video_id = ?", (video_id,))


def save_playlist_progress(
    playlist_id: str, title, thumbnail, author, author_id,
    current_video_id, current_video_title, current_video_thumbnail,
    current_video_position, current_video_duration, queue_index,
    total_items, kind="playlist",
):
    with get_db() as con:
        con.execute(
            """
            INSERT INTO playlist_progress
                (playlist_id, title, thumbnail, author, author_id,
                 current_video_id, current_video_title, current_video_thumbnail,
                 current_video_position, current_video_duration, queue_index,
                 total_items, kind, last_updated)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(playlist_id) DO UPDATE SET
                title=COALESCE(excluded.title, title),
                thumbnail=COALESCE(excluded.thumbnail, thumbnail),
                author=COALESCE(excluded.author, author),
                author_id=COALESCE(excluded.author_id, author_id),
                current_video_id=excluded.current_video_id,
                current_video_title=COALESCE(excluded.current_video_title, current_video_title),
                current_video_thumbnail=COALESCE(excluded.current_video_thumbnail, current_video_thumbnail),
                current_video_position=excluded.current_video_position,
                current_video_duration=COALESCE(excluded.current_video_duration, current_video_duration),
                queue_index=excluded.queue_index,
                total_items=COALESCE(excluded.total_items, total_items),
                kind=COALESCE(excluded.kind, kind),
                last_updated=excluded.last_updated
            """,
            (
                playlist_id, title, thumbnail, author, author_id,
                current_video_id, current_video_title, current_video_thumbnail,
                current_video_position, current_video_duration, queue_index,
                total_items, kind, time.time(),
            ),
        )


def get_playlist_progress(playlist_id: str) -> dict | None:
    with get_db() as con:
        row = con.execute(
            "SELECT * FROM playlist_progress WHERE playlist_id = ?", (playlist_id,)
        ).fetchone()
    return dict(row) if row else None


def delete_playlist_progress(playlist_id: str):
    with get_db() as con:
        con.execute(
            "DELETE FROM playlist_progress WHERE playlist_id = ?", (playlist_id,)
        )


LONG_MUSIC_PROGRESS_SECONDS = 15 * 60


def get_continue_watching(limit: int = 20) -> list[dict]:
    with get_db() as con:
        video_rows = con.execute(
            """
            SELECT *, 'video' AS kind FROM watch_progress
            WHERE position > 30
              AND (duration IS NULL OR position < duration - 30)
              AND (COALESCE(media_type, 'video') != 'music' OR COALESCE(duration, 0) >= ?)
              AND video_id NOT IN (
                  SELECT current_video_id FROM playlist_progress
                  WHERE current_video_id IS NOT NULL
              )
            """,
            (LONG_MUSIC_PROGRESS_SECONDS,),
        ).fetchall()
        playlist_rows = con.execute(
            """
            SELECT * FROM playlist_progress
            WHERE current_video_position > 30
            ORDER BY last_updated DESC
            """
        ).fetchall()

    items = [dict(r) for r in video_rows]
    items.extend(dict(r) for r in playlist_rows)
    items.sort(key=lambda item: item.get("last_updated", 0), reverse=True)
    return items[:limit]


def add_to_history(
    video_id: str, title: str, thumbnail: str | None = None,
    duration: int | None = None, author: str | None = None, author_id: str | None = None,
):
    now = time.time()
    with get_db() as con:
        con.execute(
            """
            INSERT INTO watch_history
                (video_id, title, thumbnail, duration, author, author_id,
                 listen_count, first_listened_at, watched_at)
            VALUES (?,?,?,?,?,?,1,?,?)
            ON CONFLICT(video_id) DO UPDATE SET
                title=excluded.title,
                thumbnail=excluded.thumbnail,
                duration=excluded.duration,
                author=excluded.author,
                author_id=excluded.author_id,
                listen_count=COALESCE(watch_history.listen_count, 1) + 1,
                first_listened_at=COALESCE(watch_history.first_listened_at, excluded.first_listened_at),
                watched_at=excluded.watched_at
            """,
            (video_id, title, thumbnail, duration, author, author_id, now, now),
        )


def get_history(
    limit: int = 100, offset: int = 0, search: str = "",
    category: str = "", liked_only: bool = False, order_by: str = "watched_at",
) -> list[dict]:
    where_clauses = []
    params: list = []

    if search:
        where_clauses.append("(h.title LIKE ? OR h.author LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%"])

    if category:
        where_clauses.append(
            "(EXISTS (SELECT 1 FROM video_categories vc WHERE vc.video_id = h.video_id AND LOWER(vc.category) LIKE ?))"
        )
        params.append(f"%{category.lower()}%")

    if liked_only:
        where_clauses.append("CAST(vr.rating AS INTEGER) >= 7")

    valid_orders = {
        "watched_at": "h.watched_at DESC",
        "score": "CASE WHEN vr.rating IS NULL THEN 1 ELSE 0 END, CAST(vr.rating AS INTEGER) DESC, h.watched_at DESC",
        "title": 'LOWER(COALESCE(h.title, "")) ASC, h.watched_at DESC',
        "channel": 'LOWER(COALESCE(h.author, "")) ASC, h.watched_at DESC',
        "duration": "COALESCE(h.duration, 0) DESC, h.watched_at DESC",
    }
    order_clause = valid_orders.get(order_by, valid_orders["watched_at"])

    where = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    params.extend([limit, offset])

    with get_db() as con:
        rows = con.execute(
            f"""
            SELECT h.*,
                   CAST(vr.rating AS INTEGER) as rating,
                   (SELECT vc.category FROM video_categories vc WHERE vc.video_id = h.video_id LIMIT 1) as category
            FROM watch_history h
            LEFT JOIN video_ratings vr ON vr.video_id = h.video_id
            {where}
            ORDER BY {order_clause}
            LIMIT ? OFFSET ?
            """,
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def get_history_genres() -> list[str]:
    with get_db() as con:
        categories = con.execute(
            """
            SELECT DISTINCT vc.category
            FROM watch_history h
            JOIN video_categories vc ON vc.video_id = h.video_id
            WHERE vc.category IS NOT NULL AND vc.category != ''
            ORDER BY vc.category
            """
        ).fetchall()
    return [r["category"] for r in categories]


def delete_history_item(video_id: str):
    with get_db() as con:
        con.execute("DELETE FROM watch_history WHERE video_id = ?", (video_id,))


def clear_history():
    with get_db() as con:
        con.execute("DELETE FROM watch_history")


# ── Video Ratings ──────────────────────────────────────────────────────────

def get_rating(video_id: str) -> int | None:
    with get_db() as con:
        row = con.execute(
            "SELECT rating FROM video_ratings WHERE video_id = ?", (video_id,)
        ).fetchone()
    return int(row["rating"]) if row else None


def set_rating(video_id: str, rating: int):
    with get_db() as con:
        con.execute(
            "INSERT OR REPLACE INTO video_ratings (video_id, rating, rated_at) VALUES (?,?,?)",
            (video_id, int(rating), time.time()),
        )


def delete_rating(video_id: str):
    with get_db() as con:
        con.execute("DELETE FROM video_ratings WHERE video_id = ?", (video_id,))


def get_liked_videos(limit: int = 200, offset: int = 0, sort: str = "rating") -> list[dict]:
    valid_orders = {
        "rating": "vr.rating DESC, vr.rated_at DESC",
        "date":   "vr.rated_at DESC",
        "title":  "LOWER(COALESCE(wh.title, '')) ASC",
        "channel": "LOWER(COALESCE(wh.author, '')) ASC",
        "duration": "COALESCE(wh.duration, 0) DESC",
    }
    order_clause = valid_orders.get(sort, valid_orders["rating"])
    with get_db() as con:
        rows = con.execute(
            f"""
            SELECT vr.video_id, vr.rating, vr.rated_at,
                   wh.title, wh.thumbnail, wh.duration, wh.author, wh.author_id
            FROM video_ratings vr
            LEFT JOIN watch_history wh ON wh.video_id = vr.video_id
            WHERE CAST(vr.rating AS INTEGER) >= 7
            ORDER BY {order_clause}
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
    return [dict(r) for r in rows]


def get_ratings_for_video_ids(video_ids: list[str]) -> dict[str, int]:
    if not video_ids:
        return {}
    with get_db() as con:
        ph = ",".join("?" * len(video_ids))
        rows = con.execute(
            f"SELECT video_id, CAST(rating AS INTEGER) AS rating FROM video_ratings WHERE video_id IN ({ph})",
            video_ids,
        ).fetchall()
    return {str(r["video_id"]): int(r["rating"]) for r in rows}


# ── Channel Ratings ────────────────────────────────────────────────────────

def get_channel_rating(channel_id: str) -> dict | None:
    with get_db() as con:
        row = con.execute(
            "SELECT rating, channel_name FROM channel_ratings WHERE channel_id = ?", (channel_id,)
        ).fetchone()
    return {"rating": int(row["rating"]), "channel_name": row["channel_name"]} if row else None


def set_channel_rating(channel_id: str, channel_name: str, rating: int):
    with get_db() as con:
        con.execute(
            "INSERT OR REPLACE INTO channel_ratings (channel_id, channel_name, rating, rated_at) VALUES (?,?,?,?)",
            (channel_id, channel_name, int(rating), time.time()),
        )


def delete_channel_rating(channel_id: str):
    with get_db() as con:
        con.execute("DELETE FROM channel_ratings WHERE channel_id = ?", (channel_id,))


# ── Feed Filters ───────────────────────────────────────────────────────────

def get_feed_filters() -> list[dict]:
    with get_db() as con:
        rows = con.execute(
            "SELECT id, filter_type, match_value, created_at FROM feed_filters ORDER BY created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def add_feed_filter(filter_type: str, match_value: str):
    with get_db() as con:
        con.execute(
            "INSERT OR IGNORE INTO feed_filters (filter_type, match_value, created_at) VALUES (?,?,?)",
            (filter_type, match_value.lower().strip(), time.time()),
        )


def delete_feed_filter(filter_id: int):
    with get_db() as con:
        con.execute("DELETE FROM feed_filters WHERE id = ?", (filter_id,))


# ── Feed Feedback ──────────────────────────────────────────────────────────

def get_feed_feedback(video_id: str, category: str = "") -> dict:
    with get_db() as con:
        if category:
            row = con.execute(
                "SELECT feedback, category, dislike_reason FROM feed_feedback WHERE video_id=? AND category=?",
                (video_id, category),
            ).fetchone()
            if row:
                return {"feedback": int(row["feedback"]), "category": row["category"], "dislike_reason": row["dislike_reason"]}

        row = con.execute(
            "SELECT feedback, category, dislike_reason FROM feed_feedback WHERE video_id=? AND category=''",
            (video_id,),
        ).fetchone()
    if row:
        return {"feedback": int(row["feedback"]), "category": row["category"], "dislike_reason": row["dislike_reason"]}
    return {"feedback": None, "category": category, "dislike_reason": None}


def set_feed_feedback(
    video_id: str, category: str, feedback: int,
    author_id: str | None = None, dislike_reason: str | None = None,
):
    reason = dislike_reason if feedback == -1 else None
    with get_db() as con:
        con.execute(
            "INSERT OR REPLACE INTO feed_feedback (video_id, category, feedback, author_id, dislike_reason, created_at) VALUES (?,?,?,?,?,?)",
            (video_id, category, feedback, author_id, reason, time.time()),
        )


def delete_feed_feedback(video_id: str, category: str = ""):
    with get_db() as con:
        con.execute(
            "DELETE FROM feed_feedback WHERE video_id=? AND category=?",
            (video_id, category),
        )


# ── Categories (v2) ───────────────────────────────────────────────────────

def _get_category_path(con, category_id: int) -> str:
    parts = []
    cur_id = category_id
    visited: set = set()
    while cur_id is not None:
        if cur_id in visited:
            break
        visited.add(cur_id)
        row = con.execute(
            "SELECT name, parent_id FROM categories WHERE id=?", (cur_id,)
        ).fetchone()
        if not row:
            break
        parts.append(row["name"])
        cur_id = row["parent_id"]
    parts.reverse()
    return "/".join(parts)


def _build_category_tree(con, parent_id=None):
    rows = con.execute(
        "SELECT id, name, description FROM categories WHERE parent_id IS ? ORDER BY name COLLATE NOCASE",
        (parent_id,),
    ).fetchall()
    result = []
    for r in rows:
        vid_count = con.execute(
            "SELECT COUNT(*) FROM video_category_assignments WHERE category_id=?", (r["id"],)
        ).fetchone()[0]
        ch_count = con.execute(
            "SELECT COUNT(*) FROM channel_category_assignments WHERE category_id=?", (r["id"],)
        ).fetchone()[0]
        tags = con.execute(
            "SELECT t.id, t.name FROM category_tags ct JOIN tags t ON t.id=ct.tag_id WHERE ct.category_id=? ORDER BY t.name COLLATE NOCASE",
            (r["id"],),
        ).fetchall()
        children = _build_category_tree(con, r["id"])
        result.append({
            "id": r["id"], "name": r["name"], "description": r["description"] or "",
            "video_count": vid_count, "channel_count": ch_count,
            "tags": [dict(t) for t in tags], "children": children,
        })
    return result


def create_category(name: str, parent_id=None, description: str = "") -> int:
    with get_db() as con:
        cur = con.execute(
            "INSERT INTO categories (name, parent_id, description, created_at) VALUES (?,?,?,?)",
            (name.strip(), parent_id, description, time.time()),
        )
        return cur.lastrowid


def update_category(cat_id: int, name: str | None = None, parent_id=None, description: str | None = None, clear_parent: bool = False):
    with get_db() as con:
        if name is not None:
            con.execute("UPDATE categories SET name=? WHERE id=?", (name.strip(), cat_id))
        if description is not None:
            con.execute("UPDATE categories SET description=? WHERE id=?", (description, cat_id))
        if clear_parent:
            con.execute("UPDATE categories SET parent_id=NULL WHERE id=?", (cat_id,))
        elif parent_id is not None:
            con.execute("UPDATE categories SET parent_id=? WHERE id=?", (parent_id, cat_id))


def delete_category(cat_id: int):
    with get_db() as con:
        row = con.execute("SELECT parent_id FROM categories WHERE id=?", (cat_id,)).fetchone()
        if row:
            parent_id = row["parent_id"]
            con.execute("UPDATE categories SET parent_id=? WHERE parent_id=?", (parent_id, cat_id))
            con.execute("UPDATE video_category_assignments SET category_id=NULL WHERE category_id=?", (cat_id,))
            con.execute("UPDATE channel_category_assignments SET category_id=NULL WHERE category_id=?", (cat_id,))
            con.execute("DELETE FROM categories WHERE id=?", (cat_id,))


def get_category_by_id(cat_id: int) -> dict | None:
    with get_db() as con:
        row = con.execute(
            "SELECT id, name, parent_id, description, created_at FROM categories WHERE id=?", (cat_id,)
        ).fetchone()
        if not row:
            return None
        path = _get_category_path(con, cat_id)
        tags = con.execute(
            "SELECT t.id, t.name, t.description FROM category_tags ct JOIN tags t ON t.id=ct.tag_id WHERE ct.category_id=? ORDER BY t.name COLLATE NOCASE",
            (cat_id,),
        ).fetchall()
        vc = con.execute("SELECT COUNT(*) FROM video_category_assignments WHERE category_id=?", (cat_id,)).fetchone()[0]
        cc = con.execute("SELECT COUNT(*) FROM channel_category_assignments WHERE category_id=?", (cat_id,)).fetchone()[0]
        parent = None
        if row["parent_id"]:
            pr = con.execute("SELECT id, name FROM categories WHERE id=?", (row["parent_id"],)).fetchone()
            if pr:
                parent = {"id": pr["id"], "name": pr["name"], "path": _get_category_path(con, pr["id"])}
    return {
        "id": row["id"], "name": row["name"], "parent_id": row["parent_id"],
        "parent": parent, "description": row["description"] or "",
        "path": path, "created_at": row["created_at"],
        "tags": [dict(t) for t in tags], "video_count": vc, "channel_count": cc,
    }


def get_categories_tree() -> list:
    with get_db() as con:
        return _build_category_tree(con)


def search_categories(q: str, limit: int = 20) -> list:
    with get_db() as con:
        rows = con.execute(
            "SELECT id FROM categories WHERE name LIKE ? ORDER BY name COLLATE NOCASE LIMIT ?",
            (f"%{q}%", limit),
        ).fetchall()
    results = []
    for r in rows:
        cat = get_category_by_id(r["id"])
        if cat:
            results.append(cat)
    return results


def get_category_descendant_ids(con, cat_id: int) -> list[int]:
    ids = [cat_id]
    queue = [cat_id]
    while queue:
        cur = queue.pop()
        children = con.execute("SELECT id FROM categories WHERE parent_id=?", (cur,)).fetchall()
        for c in children:
            ids.append(c["id"])
            queue.append(c["id"])
    return ids


def get_category_videos(cat_id: int, include_children: bool = True, limit: int = 50, offset: int = 0) -> list[dict]:
    with get_db() as con:
        cat_ids = get_category_descendant_ids(con, cat_id) if include_children else [cat_id]
        ph = ",".join("?" * len(cat_ids))
        rows = con.execute(
            f"""
            SELECT vca.video_id, vca.source, vca.updated_at,
                   wh.title, wh.thumbnail, wh.duration, wh.author, wh.author_id
            FROM video_category_assignments vca
            LEFT JOIN watch_history wh ON wh.video_id = vca.video_id
            WHERE vca.category_id IN ({ph})
            ORDER BY vca.updated_at DESC LIMIT ? OFFSET ?
            """,
            (*cat_ids, limit, offset),
        ).fetchall()
    return [dict(r) for r in rows]


def get_category_channels(cat_id: int, include_children: bool = True, limit: int = 50, offset: int = 0) -> list[dict]:
    with get_db() as con:
        cat_ids = get_category_descendant_ids(con, cat_id) if include_children else [cat_id]
        ph = ",".join("?" * len(cat_ids))
        rows = con.execute(
            f"""
            SELECT cca.channel_id, cca.source, cca.updated_at,
                   COALESCE(s.channel_name, cs.channel_name) as channel_name,
                   COALESCE(s.thumbnail, cs.thumbnail) as thumbnail
            FROM channel_category_assignments cca
            LEFT JOIN subscriptions s ON s.channel_id = cca.channel_id
            LEFT JOIN channel_stats cs ON cs.channel_id = cca.channel_id
            WHERE cca.category_id IN ({ph})
            ORDER BY cca.updated_at DESC LIMIT ? OFFSET ?
            """,
            (*cat_ids, limit, offset),
        ).fetchall()
    return [dict(r) for r in rows]


# ── Tags ───────────────────────────────────────────────────────────────────

def create_tag(name: str, description: str = "") -> int:
    with get_db() as con:
        cur = con.execute(
            "INSERT INTO tags (name, description, created_at) VALUES (?,?,?)",
            (name.strip(), description, time.time()),
        )
        return cur.lastrowid


def delete_tag(tag_id: int):
    with get_db() as con:
        con.execute("DELETE FROM tags WHERE id=?", (tag_id,))


def get_all_tags(limit: int = 500) -> list[dict]:
    with get_db() as con:
        rows = con.execute(
            """
            SELECT t.id, t.name, t.description,
                   (SELECT COUNT(*) FROM video_tags WHERE tag_id=t.id) as video_count,
                   (SELECT COUNT(*) FROM channel_tags WHERE tag_id=t.id) as channel_count,
                   (SELECT COUNT(*) FROM category_tags WHERE tag_id=t.id) as category_count
            FROM tags t ORDER BY t.name COLLATE NOCASE LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def search_tags(q: str, limit: int = 15) -> list[dict]:
    with get_db() as con:
        rows = con.execute(
            """
            SELECT t.id, t.name, t.description,
                   (SELECT COUNT(*) FROM video_tags WHERE tag_id=t.id) +
                   (SELECT COUNT(*) FROM channel_tags WHERE tag_id=t.id) as uses
            FROM tags t WHERE t.name LIKE ?
            ORDER BY uses DESC, t.name COLLATE NOCASE LIMIT ?
            """,
            (f"%{q}%", limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_tag_by_id(tag_id: int) -> dict | None:
    with get_db() as con:
        row = con.execute(
            "SELECT id, name, description, created_at FROM tags WHERE id=?", (tag_id,)
        ).fetchone()
        if not row:
            return None
        vc = con.execute("SELECT COUNT(*) FROM video_tags WHERE tag_id=?", (tag_id,)).fetchone()[0]
        cc = con.execute("SELECT COUNT(*) FROM channel_tags WHERE tag_id=?", (tag_id,)).fetchone()[0]
    return {**dict(row), "video_count": vc, "channel_count": cc}


def get_tag_items(tag_id: int, limit: int = 300) -> dict:
    """Full breakdown of everything carrying a tag: videos, channels, categories."""
    with get_db() as con:
        videos = [dict(r) for r in con.execute("""
            SELECT vt.video_id,
                   COALESCE(NULLIF(wh.title, ''), vt.video_id) AS title,
                   wh.thumbnail, wh.author, wh.author_id
            FROM video_tags vt
            LEFT JOIN watch_history wh ON wh.video_id = vt.video_id
            WHERE vt.tag_id = ?
            ORDER BY (wh.title IS NULL), wh.title COLLATE NOCASE
            LIMIT ?
        """, (tag_id, limit)).fetchall()]
        channels = [dict(r) for r in con.execute("""
            SELECT ct.channel_id,
                   COALESCE(NULLIF(s.channel_name, ''), NULLIF(cs.channel_name, ''),
                            NULLIF(cr.channel_name, ''), ct.channel_id) AS channel_name,
                   COALESCE(s.thumbnail, cs.thumbnail) AS thumbnail
            FROM channel_tags ct
            LEFT JOIN subscriptions s ON s.channel_id = ct.channel_id
            LEFT JOIN channel_stats cs ON cs.channel_id = ct.channel_id
            LEFT JOIN channel_ratings cr ON cr.channel_id = ct.channel_id
            WHERE ct.tag_id = ?
            ORDER BY channel_name COLLATE NOCASE
            LIMIT ?
        """, (tag_id, limit)).fetchall()]
        categories = [dict(r) for r in con.execute("""
            SELECT c.id, c.name
            FROM category_tags cat
            JOIN categories c ON c.id = cat.category_id
            WHERE cat.tag_id = ?
            ORDER BY c.name COLLATE NOCASE
            LIMIT ?
        """, (tag_id, limit)).fetchall()]
    return {"videos": videos, "channels": channels, "categories": categories}


def check_tag_category_conflicts(tag_name: str) -> list[str]:
    words = {w.lower() for w in tag_name.split() if len(w) > 2}
    if not words:
        return []
    with get_db() as con:
        cats = con.execute("SELECT name FROM categories").fetchall()
    conflicts = []
    for c in cats:
        cat_words = {w.lower() for w in c["name"].split() if len(w) > 2}
        if words & cat_words:
            conflicts.append(c["name"])
    return conflicts


def add_category_tag(category_id: int, tag_id: int):
    with get_db() as con:
        con.execute(
            "INSERT OR IGNORE INTO category_tags (category_id, tag_id) VALUES (?,?)",
            (category_id, tag_id),
        )


def remove_category_tag(category_id: int, tag_id: int):
    with get_db() as con:
        con.execute(
            "DELETE FROM category_tags WHERE category_id=? AND tag_id=?",
            (category_id, tag_id),
        )


def set_video_category_v2(video_id: str, category_id: int, source: str = "user"):
    now = time.time()
    with get_db() as con:
        con.execute(
            "INSERT OR REPLACE INTO video_category_assignments (video_id, category_id, source, updated_at) VALUES (?,?,?,?)",
            (video_id, category_id, source, now),
        )
        path = _get_category_path(con, category_id)
        if path:
            con.execute(
                "INSERT OR REPLACE INTO video_categories (video_id, category, source, updated_at) VALUES (?,?,?,?)",
                (video_id, path, source, now),
            )


def remove_video_category_v2(video_id: str):
    with get_db() as con:
        con.execute(
            "UPDATE video_category_assignments SET category_id=NULL WHERE video_id=?", (video_id,)
        )


def get_video_assignment(video_id: str) -> dict:
    with get_db() as con:
        row = con.execute(
            "SELECT category_id, source FROM video_category_assignments WHERE video_id=?", (video_id,)
        ).fetchone()
        category = None
        if row and row["category_id"]:
            cr = con.execute("SELECT id, name FROM categories WHERE id=?", (row["category_id"],)).fetchone()
            if cr:
                category = {"id": cr["id"], "name": cr["name"], "path": _get_category_path(con, cr["id"])}
        tags = con.execute(
            "SELECT t.id, t.name FROM video_tags vt JOIN tags t ON t.id=vt.tag_id WHERE vt.video_id=? ORDER BY t.name COLLATE NOCASE",
            (video_id,),
        ).fetchall()
    return {"category": category, "tags": [dict(t) for t in tags], "source": row["source"] if row else None}


def add_video_tag(video_id: str, tag_id: int):
    with get_db() as con:
        con.execute(
            "INSERT OR IGNORE INTO video_tags (video_id, tag_id) VALUES (?,?)", (video_id, tag_id)
        )


def remove_video_tag(video_id: str, tag_id: int):
    with get_db() as con:
        con.execute(
            "DELETE FROM video_tags WHERE video_id=? AND tag_id=?", (video_id, tag_id)
        )


def get_videos_by_tag(tag_id: int, limit: int = 100) -> list[dict]:
    with get_db() as con:
        rows = con.execute(
            """
            SELECT vt.video_id, wh.title, wh.thumbnail, wh.duration, wh.author, wh.author_id
            FROM video_tags vt
            LEFT JOIN watch_history wh ON wh.video_id = vt.video_id
            WHERE vt.tag_id=?
            ORDER BY wh.watched_at DESC NULLS LAST
            LIMIT ?
            """,
            (tag_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def set_channel_category_v2(channel_id: str, category_id: int, source: str = "user"):
    now = time.time()
    with get_db() as con:
        con.execute(
            "INSERT OR REPLACE INTO channel_category_assignments (channel_id, category_id, source, updated_at) VALUES (?,?,?,?)",
            (channel_id, category_id, source, now),
        )


def remove_channel_category_v2(channel_id: str):
    with get_db() as con:
        con.execute(
            "UPDATE channel_category_assignments SET category_id=NULL WHERE channel_id=?", (channel_id,)
        )


def get_channel_assignment(channel_id: str) -> dict:
    with get_db() as con:
        row = con.execute(
            "SELECT category_id, source FROM channel_category_assignments WHERE channel_id=?", (channel_id,)
        ).fetchone()
        category = None
        if row and row["category_id"]:
            cr = con.execute("SELECT id, name FROM categories WHERE id=?", (row["category_id"],)).fetchone()
            if cr:
                category = {"id": cr["id"], "name": cr["name"], "path": _get_category_path(con, cr["id"])}
        tags = con.execute(
            "SELECT t.id, t.name FROM channel_tags ct JOIN tags t ON t.id=ct.tag_id WHERE ct.channel_id=? ORDER BY t.name COLLATE NOCASE",
            (channel_id,),
        ).fetchall()
    return {"category": category, "tags": [dict(t) for t in tags], "source": row["source"] if row else None}


def add_channel_tag(channel_id: str, tag_id: int):
    with get_db() as con:
        con.execute(
            "INSERT OR IGNORE INTO channel_tags (channel_id, tag_id) VALUES (?,?)", (channel_id, tag_id)
        )


def remove_channel_tag(channel_id: str, tag_id: int):
    with get_db() as con:
        con.execute(
            "DELETE FROM channel_tags WHERE channel_id=? AND tag_id=?", (channel_id, tag_id)
        )


def ensure_channel_in_music_section(channel_id: str | None) -> bool:
    if not channel_id or not str(channel_id).strip():
        return False
    cid = str(channel_id).strip()
    with get_db() as con:
        row = con.execute("SELECT id FROM tags WHERE LOWER(name)='music' LIMIT 1").fetchone()
        if row:
            tag_id = int(row["id"])
        else:
            cur = con.execute(
                "INSERT INTO tags (name, description, created_at) VALUES (?,?,?)",
                ("music", "Music section / subscriptions", time.time()),
            )
            tag_id = int(cur.lastrowid)
        con.execute(
            "INSERT OR IGNORE INTO channel_tags (channel_id, tag_id) VALUES (?,?)", (cid, tag_id)
        )
    return True


def get_music_labeled_channel_ids() -> set[str]:
    with get_db() as con:
        ids: set[str] = set()
        for r in con.execute(
            "SELECT ct.channel_id FROM channel_tags ct INNER JOIN tags t ON t.id=ct.tag_id WHERE LOWER(t.name)='music'"
        ).fetchall():
            ids.add(str(r["channel_id"]))
        for r in con.execute(
            """
            SELECT cca.channel_id FROM channel_category_assignments cca
            WHERE cca.category_id IS NOT NULL
            AND EXISTS (
                WITH RECURSIVE cat_anc AS (
                    SELECT id, name, parent_id FROM categories WHERE id = cca.category_id
                    UNION ALL
                    SELECT c.id, c.name, c.parent_id FROM categories c INNER JOIN cat_anc ON c.id = cat_anc.parent_id
                )
                SELECT 1 FROM cat_anc WHERE LOWER(name) = 'music' LIMIT 1
            )
            """
        ).fetchall():
            ids.add(str(r["channel_id"]))
    return ids


def suggest_tags(q: str = "", category_id: int | None = None, video_id: str | None = None, channel_id: str | None = None, limit: int = 10) -> list[dict]:
    with get_db() as con:
        excluded: set = set()
        if video_id:
            excluded = {r["tag_id"] for r in con.execute("SELECT tag_id FROM video_tags WHERE video_id=?", (video_id,)).fetchall()}
        elif channel_id:
            excluded = {r["tag_id"] for r in con.execute("SELECT tag_id FROM channel_tags WHERE channel_id=?", (channel_id,)).fetchall()}

        suggestions = []
        seen: set = set()

        if category_id:
            for t in con.execute(
                "SELECT t.id, t.name, t.description, 'category' as source FROM category_tags ct JOIN tags t ON t.id=ct.tag_id WHERE ct.category_id=? AND (? = '' OR t.name LIKE ?) ORDER BY t.name COLLATE NOCASE",
                (category_id, q, f"%{q}%"),
            ).fetchall():
                if t["id"] not in excluded and t["id"] not in seen:
                    suggestions.append(dict(t)); seen.add(t["id"])

        for t in con.execute(
            "SELECT t.id, t.name, t.description, (SELECT COUNT(*) FROM video_tags WHERE tag_id=t.id) + (SELECT COUNT(*) FROM channel_tags WHERE tag_id=t.id) as uses, 'global' as source FROM tags t WHERE (? = '' OR t.name LIKE ?) ORDER BY uses DESC, t.name COLLATE NOCASE LIMIT ?",
            (q, f"%{q}%", limit * 2),
        ).fetchall():
            if t["id"] not in excluded and t["id"] not in seen:
                suggestions.append(dict(t)); seen.add(t["id"])

    return suggestions[:limit]


# ── Takeout imports ────────────────────────────────────────────────────────

def get_playlists_for_video_ids(video_ids: list[str], per_video_limit: int = 6) -> dict[str, list[dict]]:
    if not video_ids:
        return {}
    with get_db() as con:
        ph = ",".join("?" * len(video_ids))
        rows = con.execute(
            f"""
            SELECT pv.video_id, p.id as playlist_id, p.title as playlist_title
            FROM playlist_videos pv
            JOIN playlists p ON p.id = pv.playlist_id
            WHERE pv.video_id IN ({ph})
            """,
            video_ids,
        ).fetchall()
    buckets: dict[str, list] = {}
    for r in rows:
        vid = str(r["video_id"])
        buckets.setdefault(vid, []).append((int(r["playlist_id"]), r["playlist_title"] or f"Playlist {r['playlist_id']}"))
    per: dict[str, list[dict]] = {}
    for vid, pairs in buckets.items():
        pairs.sort(key=lambda t: (t[1].lower(), t[0]))
        per[vid] = [{"id": pid, "title": title} for pid, title in pairs[:per_video_limit]]
    return per


# Ensure channel_stats table exists on import
_init_channel_stats()
