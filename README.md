<div align="center">

# ytvideo

**The video frontend backend of [yt-platform](https://github.com/iversonianGremling/recommenderr) — owns your library, feed, and subscriptions.**

A small FastAPI service that holds *your* video state (watch history, playlists, ratings, tags, categories, subscriptions) and delegates every YouTube/Invidious fetch and all feed scoring to [`recommenderr`](https://github.com/iversonianGremling/recommenderr).

[![Backend](https://img.shields.io/badge/backend-FastAPI-009688)](https://fastapi.tiangolo.com/)
[![Python](https://img.shields.io/badge/python-3-3776ab)](https://www.python.org/)
[![Storage](https://img.shields.io/badge/storage-SQLite-003b57)](https://www.sqlite.org/)
[![Port](https://img.shields.io/badge/port-9002-blue)](#configuration)
[![Self-hosted](https://img.shields.io/badge/self--hosted-yes-success)](#deployment)

</div>

---

## What is this?

`ytvideo` is one of three services in **yt-platform**, a self-hosted YouTube & YouTube Music frontend with a transparent recommendation engine. It is the **video-mode backend**: it owns user state for watching videos and serves it to the React video SPA.

The design rule of the platform is a clean split of responsibilities:

- **`ytvideo` owns *your* state** — library, feed, watch history, playlists, ratings, tags, categories, subscriptions, mpv control.
- **[`recommenderr`](https://github.com/iversonianGremling/recommenderr) owns the *outside world*** — every Invidious/YouTube call, all third-party APIs, and the recommendation pipeline.

This keeps the player fast and resilient: if the recommender's external integrations hit a rate limit or break, the library and history still work.

> For the big picture (architecture diagram, the recommendation pipeline, routing, and deployment of all three services), see the **[yt-platform / recommenderr README](https://github.com/iversonianGremling/recommenderr)**.

## Features

- **Library & watch history** — what you've watched, when, and how far.
- **Video playlists** — create, reorder, and manage playlists.
- **Ratings & tags** — rate and tag videos; tags feed back into recommendations.
- **Categories** — group subscriptions/recommendations into named categories.
- **Subscriptions** — RSS-based channel subscriptions with a background stats worker.
- **Google Takeout import** — bulk-import history/subscriptions via a background import worker.
- **mpv control** — drive a local mpv player from the frontend (`/mpv`).
- **Feed** — reads the precomputed ranked feed from `recommenderr` and serves it to the SPA, detecting recomputes via the feed-generation counter.

## API surface

A FastAPI app exposing routers for each concern:

| Router          | Purpose                                          |
| --------------- | ------------------------------------------------ |
| `subscriptions` | RSS subscriptions + background stats worker      |
| `playlists`     | Video playlists                                  |
| `history`       | Watch history                                    |
| `ratings`       | Video ratings                                    |
| `categories`    | Category grouping                                |
| `tags`          | Video tags                                       |
| `feed`          | Personalized feed (proxied/scored by recommenderr) |
| `imports`       | Google Takeout import jobs                        |
| `mpv`           | Local mpv playback control (`/mpv` prefix)        |
| `internal`      | Internal endpoints for sibling services          |

`GET /health` reports service status, schema version, and the configured `recommenderr` URL.

## Repository layout

```
ytvideo/
├── backend/
│   ├── main.py            FastAPI app + lifespan (DB init, background workers)
│   ├── schema.sql         SQLite schema (WAL, foreign keys)
│   ├── routers/           subscriptions, playlists, history, ratings,
│   │                      categories, tags, feed, imports, mpv, internal
│   ├── services/          mpv_service, youtube_rss, subscription_rss, takeout_imports
│   ├── clients/           recommenderr client
│   ├── db/
│   └── tests/             unit + integration (pytest)
├── requirements.txt
├── pytest.ini
└── .env.example
```

## Configuration

Copy `.env.example` to `.env` and adjust. Key variables:

| Variable                            | Default                  | Purpose                                   |
| ----------------------------------- | ------------------------ | ----------------------------------------- |
| `LISTEN_HOST` / `LISTEN_PORT`       | `0.0.0.0` / `9002`       | Bind address                              |
| `DB_PATH`                           | `/opt/ytvideo/data/ytvideo.db` | SQLite database path                |
| `RECOMMENDERR_URL`                  | `http://127.0.0.1:9001`  | Where to reach the recommender            |
| `RECOMMENDERR_TOKEN`                | —                        | Bearer token shared with recommenderr     |
| `INVIDIOUS_URL`                     | —                        | Invidious instance (passed through)       |
| `SUBSCRIPTION_STATS_REFRESH_*`      | —                        | Subscription stats background worker       |
| `TAKEOUT_IMPORTS_ENABLED` / `_DIR`  | —                        | Google Takeout import worker               |

`.env` is gitignored — never commit secrets.

## Development

```bash
# from ytvideo/
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

python -m backend.main      # serves on :9002
python -m pytest            # run tests
```

## Deployment

Runs as a `systemd` service inside a Proxmox LXC (CT134), behind the shared nginx site, alongside `recommenderr` (`:9001`) and `ytmusic` (`:9003`).

```bash
#   ytvideo.service  →  python -m backend.main   (cwd /opt/ytvideo, :9002)
systemctl restart ytvideo
systemctl status  ytvideo
```

nginx routes `/api/local/` and `/api/mpv/` to this service; the video SPA — built from the
[`ytfrontend`](https://github.com/iversonianGremling/ytfrontend) repo (the platform's umbrella) — is
served at `/`.

## License

Personal / self-hosted project. No public license granted — adapt for your own use.
