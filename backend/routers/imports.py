"""Takeout imports router for ytvideo."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from pydantic import BaseModel

from backend.services.takeout_imports import (
    create_import_job,
    get_import_job,
    list_import_jobs,
    safe_upload_path,
    submit_import_job,
)

router = APIRouter()


def _detect_takeout_upload(files: list[UploadFile]) -> tuple[str, str]:
    if not files:
        raise HTTPException(status_code=400, detail="Upload at least one Google Takeout file")

    zip_uploads = [upload for upload in files if (upload.filename or "").lower().endswith(".zip")]
    if zip_uploads:
        if len(zip_uploads) != 1 or len(files) != 1:
            raise HTTPException(
                status_code=400,
                detail="Upload either one zip archive or a set of extracted files",
            )
        return "zip", zip_uploads[0].filename or "takeout.zip"

    first_name = (files[0].filename or "").replace("\\", "/")
    if "/" in first_name:
        top_level = first_name.split("/", 1)[0] or "Google Takeout"
        return "folder", top_level

    if len(files) == 1:
        return "files", files[0].filename or "takeout-file"
    return "files", f"{len(files)} takeout files"


async def _store_takeout_upload(upload: UploadFile, payload_root: Path) -> None:
    relative_path = safe_upload_path(upload.filename or "upload.bin")
    destination = payload_root / relative_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as handle:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            handle.write(chunk)
    await upload.close()


@router.get("/imports/takeout/jobs")
async def takeout_jobs():
    return list_import_jobs()


@router.get("/imports/takeout/jobs/{job_id}")
async def takeout_job(job_id: int):
    job = get_import_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Import job not found")
    return job


@router.post("/imports/takeout/jobs")
async def create_takeout_job(
    files: list[UploadFile] = File(...),
    fetch_metadata: bool = Form(False),
):
    source_kind, source_name = _detect_takeout_upload(files)
    job = create_import_job(source_name, source_kind, fetch_metadata)
    payload_root = Path(job["storage_path"]) / "payload"

    for upload in files:
        await _store_takeout_upload(upload, payload_root)

    await submit_import_job(int(job["id"]))
    created = get_import_job(int(job["id"]))
    if not created:
        raise HTTPException(status_code=500, detail="Import job was created but could not be loaded")
    return created
