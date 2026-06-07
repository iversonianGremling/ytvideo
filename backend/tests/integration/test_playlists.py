"""Integration tests for playlists endpoints."""
from __future__ import annotations

import sqlite3
import pytest


def test_create_list_delete_playlist(client, tmp_db):
    # Create
    r = client.post("/playlists", json={"title": "My Playlist", "description": "Test"})
    assert r.status_code == 200
    body = r.json()
    pid = body["id"]
    assert pid is not None
    assert body["title"] == "My Playlist"

    # List
    r = client.get("/playlists")
    assert r.status_code == 200
    items = r.json()
    assert any(p["id"] == pid for p in items)

    # Get detail
    r = client.get(f"/playlists/{pid}")
    assert r.status_code == 200
    detail = r.json()
    assert detail["title"] == "My Playlist"
    assert detail["videos"] == []
    assert detail["video_count"] == 0

    # Add video
    r = client.post(f"/playlists/{pid}/videos", json={
        "video_id": "abc123",
        "title": "Test Video",
        "thumbnail": "https://example.com/t.jpg",
        "duration": 300,
        "author": "TestAuthor",
        "author_id": "UC_auth",
    })
    assert r.status_code == 200
    assert r.json()["added"] is True

    # Verify DB row
    con = sqlite3.connect(tmp_db)
    try:
        rows = con.execute("SELECT * FROM playlist_videos WHERE playlist_id=? AND video_id='abc123'", (pid,)).fetchall()
        assert len(rows) == 1
    finally:
        con.close()

    # Get detail with video
    r = client.get(f"/playlists/{pid}")
    detail = r.json()
    assert detail["video_count"] == 1
    assert detail["videos"][0]["video_id"] == "abc123"

    # Remove video
    r = client.delete(f"/playlists/{pid}/videos/abc123")
    assert r.status_code == 200

    # Delete playlist
    r = client.delete(f"/playlists/{pid}")
    assert r.status_code == 200

    # Verify gone
    r = client.get(f"/playlists/{pid}")
    assert r.status_code == 404


def test_update_playlist(client):
    r = client.post("/playlists", json={"title": "Old Title"})
    pid = r.json()["id"]

    r = client.put(f"/playlists/{pid}", json={"title": "New Title"})
    assert r.status_code == 200

    r = client.get(f"/playlists/{pid}")
    assert r.json()["title"] == "New Title"


def test_add_duplicate_video_to_playlist(client):
    r = client.post("/playlists", json={"title": "Dup Test"})
    pid = r.json()["id"]

    video = {"video_id": "dup111", "title": "Dup Video"}
    r1 = client.post(f"/playlists/{pid}/videos", json=video)
    assert r1.json()["added"] is True

    r2 = client.post(f"/playlists/{pid}/videos", json=video)
    assert r2.json()["added"] is False  # already in playlist
