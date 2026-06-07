"""Takeout import job management for ytvideo.

Adapted from monolith's takeout_imports.py: imports into ytvideo.db using the
import_takeout library, and references backend.db instead of services.database.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import time
import zipfile
from pathlib import Path, PurePosixPath

from backend.db import get_db
from backend.services.subscription_rss import invalidate_subscription_feed_cache

_job_queue: asyncio.Queue[int] = asyncio.Queue()

DEFAULT_DB_PATH = Path(os.getenv("DB_PATH", "/opt/ytvideo/data/ytvideo.db")).resolve()
IMPORT_ROOT = Path(os.getenv("TAKEOUT_IMPORTS_DIR", str(DEFAULT_DB_PATH.parent / "import-jobs"))).resolve()


def ensure_import_root() -> None:
    IMPORT_ROOT.mkdir(parents=True, exist_ok=True)


def create_import_job(source_name: str, source_kind: str, fetch_metadata: bool) -> dict:
    ensure_import_root()
    now = time.time()
    with get_db() as con:
        cur = con.execute(
            """
            INSERT INTO takeout_import_jobs
                (source_name, source_kind, status, phase, message, storage_path, fetch_metadata, total_steps,
                 completed_steps, subscriptions_imported, playlists_imported, history_imported, created_at, updated_at)
            VALUES (?, ?, 'uploading', 'upload', 'Receiving upload', '', ?, 3, 0, 0, 0, 0, ?, ?)
            """,
            (source_name, source_kind, 1 if fetch_metadata else 0, now, now),
        )
        job_id = cur.lastrowid
        storage_path = IMPORT_ROOT / f"job-{job_id}"
        storage_path.mkdir(parents=True, exist_ok=True)
        (storage_path / "payload").mkdir(parents=True, exist_ok=True)
        con.execute(
            "UPDATE takeout_import_jobs SET storage_path=?, updated_at=? WHERE id=?",
            (str(storage_path), now, job_id),
        )
    return {"id": job_id, "storage_path": str(storage_path)}


def list_import_jobs(limit: int = 20) -> list[dict]:
    with get_db() as con:
        rows = con.execute(
            "SELECT * FROM takeout_import_jobs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [_serialize_job(row) for row in rows]


def get_import_job(job_id: int) -> dict | None:
    with get_db() as con:
        row = con.execute(
            "SELECT * FROM takeout_import_jobs WHERE id=?", (job_id,)
        ).fetchone()
    return _serialize_job(row) if row else None


async def submit_import_job(job_id: int) -> None:
    _update_job(job_id, status="pending", phase="queued", message="Queued for import")
    await _job_queue.put(job_id)


async def takeout_import_worker() -> None:
    import os
    if os.getenv("TAKEOUT_IMPORTS_ENABLED", "1").strip().lower() in ("0", "false", "no", "off"):
        return
    ensure_import_root()
    with get_db() as con:
        stale = con.execute(
            "SELECT id FROM takeout_import_jobs WHERE status IN ('pending', 'running') ORDER BY created_at ASC"
        ).fetchall()

    for row in stale:
        await _job_queue.put(int(row["id"]))

    while True:
        try:
            job_id = await _job_queue.get()
            await asyncio.to_thread(_run_job_sync, job_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass


def _serialize_job(row) -> dict:
    if not row:
        return {}
    item = dict(row)
    item["fetch_metadata"] = bool(item.get("fetch_metadata"))
    item.pop("storage_path", None)
    return item


def _update_job(job_id: int, **fields) -> None:
    if not fields:
        return
    payload = {**fields, "updated_at": time.time()}
    assignments = ", ".join(f"{key}=?" for key in payload)
    with get_db() as con:
        con.execute(
            f"UPDATE takeout_import_jobs SET {assignments} WHERE id=?",
            [*payload.values(), job_id],
        )


def _load_job_row(job_id: int):
    with get_db() as con:
        return con.execute(
            "SELECT * FROM takeout_import_jobs WHERE id=?", (job_id,)
        ).fetchone()


def _payload_root(job_row) -> Path:
    return Path(job_row["storage_path"]) / "payload"


def safe_upload_path(name: str) -> Path:
    parts = [
        part for part in PurePosixPath((name or "").replace("\\", "/")).parts
        if part not in ("", ".", "..")
    ]
    if not parts:
        raise ValueError("Invalid upload path")
    return Path(*parts)


def _extract_archive(archive_path: Path, target_root: Path) -> None:
    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.infolist():
            raw_name = member.filename.replace("\\", "/")
            if not raw_name or raw_name.endswith("/"):
                continue
            relative_path = safe_upload_path(raw_name)
            destination = target_root / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, destination.open("wb") as handle:
                shutil.copyfileobj(source, handle)


def _resolve_input_root(job_row) -> Path:
    payload_root = _payload_root(job_row)
    if job_row["source_kind"] != "zip":
        return payload_root

    extracted_root = Path(job_row["storage_path"]) / "extracted"
    if extracted_root.exists():
        shutil.rmtree(extracted_root)
    extracted_root.mkdir(parents=True, exist_ok=True)

    archives = sorted(payload_root.glob("*.zip"))
    if not archives:
        raise FileNotFoundError("No zip archive was uploaded")
    _extract_archive(archives[0], extracted_root)
    return extracted_root


def _run_job_sync(job_id: int) -> None:
    job = _load_job_row(job_id)
    if not job:
        return

    try:
        # Try to import using import_takeout if available
        try:
            import import_takeout
            _run_with_import_takeout(job_id, job, import_takeout)
        except ImportError:
            _run_job_manual(job_id, job)
    except Exception as exc:
        _update_job(
            job_id,
            status="failed",
            phase="failed",
            message="Import failed",
            error_text=str(exc),
            finished_at=time.time(),
        )


def _run_with_import_takeout(job_id: int, job, import_takeout) -> None:
    input_root = _resolve_input_root(job)
    fetch_metadata = bool(job["fetch_metadata"])

    _update_job(
        job_id,
        status="running",
        phase="subscriptions",
        message="Importing subscriptions",
        error_text=None,
        completed_steps=0,
        finished_at=None,
    )

    conn = import_takeout.get_db()
    try:
        import_takeout.init_db(conn)

        subscriptions_imported = int(import_takeout.import_subscriptions(conn, input_root, fetch_metadata, False) or 0)
        _update_job(job_id, phase="playlists", message="Importing playlists", completed_steps=1, subscriptions_imported=subscriptions_imported)

        playlists_imported = int(import_takeout.import_playlists(conn, input_root, fetch_metadata, False) or 0)
        _update_job(job_id, phase="history", message="Importing history", completed_steps=2, playlists_imported=playlists_imported)

        history_imported = int(import_takeout.import_history(conn, input_root, fetch_metadata, False) or 0)
        invalidate_subscription_feed_cache()

        _update_job(
            job_id,
            status="done",
            phase="complete",
            message="Import complete",
            completed_steps=3,
            history_imported=history_imported,
            finished_at=time.time(),
        )
    finally:
        conn.close()


def _run_job_manual(job_id: int, job) -> None:
    """Fallback: mark as failed if import_takeout not available."""
    _update_job(
        job_id,
        status="failed",
        phase="failed",
        message="import_takeout library not available",
        error_text="import_takeout not installed",
        finished_at=time.time(),
    )
