-- ytvideo.db
-- Owns: video-mode user state (subscriptions, playlists, watch history, ratings, categories).
-- Cross-DB references to recommenderr (video metadata, PPR, category_recommendations) are
-- fetched via REST; this DB only stores user-set facts.

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at REAL NOT NULL
);

-- ----- Subscriptions -----

CREATE TABLE IF NOT EXISTS subscriptions (
    channel_id TEXT PRIMARY KEY,
    channel_name TEXT NOT NULL,
    thumbnail TEXT,
    added_at REAL NOT NULL
);

-- ----- Playlists (video-only; music playlists live in ytmusic.db) -----

CREATE TABLE IF NOT EXISTS playlists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    source_playlist_id TEXT,
    source_updated_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_playlists_source_playlist_id
    ON playlists(source_playlist_id) WHERE source_playlist_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS playlist_videos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    video_id TEXT NOT NULL,
    title TEXT NOT NULL,
    thumbnail TEXT,
    duration INTEGER,
    author TEXT,
    author_id TEXT,
    position INTEGER NOT NULL,
    added_at REAL NOT NULL,
    source_managed INTEGER NOT NULL DEFAULT 0,
    UNIQUE(playlist_id, video_id)
);
CREATE INDEX IF NOT EXISTS idx_pv_playlist ON playlist_videos(playlist_id, position);

CREATE TABLE IF NOT EXISTS playlist_video_overrides (
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    video_id TEXT NOT NULL,
    is_deleted INTEGER NOT NULL DEFAULT 1,
    updated_at REAL NOT NULL,
    PRIMARY KEY (playlist_id, video_id)
);

CREATE TABLE IF NOT EXISTS playlist_progress (
    playlist_id TEXT PRIMARY KEY,
    title TEXT,
    thumbnail TEXT,
    author TEXT,
    author_id TEXT,
    current_video_id TEXT,
    current_video_title TEXT,
    current_video_thumbnail TEXT,
    current_video_position INTEGER NOT NULL,
    current_video_duration INTEGER,
    queue_index INTEGER NOT NULL DEFAULT 0,
    total_items INTEGER,
    kind TEXT NOT NULL DEFAULT 'playlist',
    last_updated REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_playlist_progress_updated
    ON playlist_progress(last_updated DESC);

-- ----- Watch progress + history (video rows only; ytmusic owns music rows) -----

CREATE TABLE IF NOT EXISTS watch_progress (
    video_id TEXT PRIMARY KEY,
    title TEXT,
    thumbnail TEXT,
    duration INTEGER,
    author TEXT,
    author_id TEXT,
    position INTEGER NOT NULL,
    last_updated REAL NOT NULL,
    media_type TEXT NOT NULL DEFAULT 'video'
);

CREATE TABLE IF NOT EXISTS watch_history (
    video_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    thumbnail TEXT,
    duration INTEGER,
    author TEXT,
    author_id TEXT,
    watched_at REAL NOT NULL,
    listen_count INTEGER NOT NULL DEFAULT 1,
    first_listened_at REAL
);
CREATE INDEX IF NOT EXISTS idx_history_watched ON watch_history(watched_at DESC);

-- ----- Ratings -----

CREATE TABLE IF NOT EXISTS video_ratings (
    video_id TEXT PRIMARY KEY,
    rating TEXT NOT NULL,
    rated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS channel_ratings (
    channel_id TEXT PRIMARY KEY,
    channel_name TEXT,
    rating TEXT NOT NULL,
    rated_at REAL NOT NULL
);

-- ----- Categories (canonical tree; recommenderr stores opaque category_ids) -----

CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    parent_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    description TEXT DEFAULT '',
    created_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_cat_name_parent
    ON categories(name, COALESCE(parent_id, -1));

CREATE TABLE IF NOT EXISTS custom_categories (
    name TEXT PRIMARY KEY,
    keywords TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS video_categories (
    video_id TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'auto',
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vc_category ON video_categories(category);

CREATE TABLE IF NOT EXISTS video_category_assignments (
    video_id TEXT PRIMARY KEY,
    category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    source TEXT NOT NULL DEFAULT 'user',
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vca_cat ON video_category_assignments(category_id);

CREATE TABLE IF NOT EXISTS channel_category_assignments (
    channel_id TEXT PRIMARY KEY,
    category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    source TEXT NOT NULL DEFAULT 'user',
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cca_cat ON channel_category_assignments(category_id);

-- ----- Tags -----

CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    description TEXT DEFAULT '',
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS category_tags (
    category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (category_id, tag_id)
);
CREATE INDEX IF NOT EXISTS idx_cattag_cat ON category_tags(category_id);

CREATE TABLE IF NOT EXISTS video_tags (
    video_id TEXT NOT NULL,
    tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (video_id, tag_id)
);
CREATE INDEX IF NOT EXISTS idx_vtag_tag ON video_tags(tag_id);

CREATE TABLE IF NOT EXISTS channel_tags (
    channel_id TEXT NOT NULL,
    tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (channel_id, tag_id)
);
CREATE INDEX IF NOT EXISTS idx_ctag_tag ON channel_tags(tag_id);

-- ----- Feed customisation -----

CREATE TABLE IF NOT EXISTS feed_filters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filter_type TEXT NOT NULL,
    match_value TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE(filter_type, match_value)
);

CREATE TABLE IF NOT EXISTS feed_feedback (
    video_id TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT '',
    feedback INTEGER NOT NULL,
    author_id TEXT,
    created_at REAL NOT NULL,
    dislike_reason TEXT,
    PRIMARY KEY (video_id, category)
);
CREATE INDEX IF NOT EXISTS idx_ff_category ON feed_feedback(category);
CREATE INDEX IF NOT EXISTS idx_ff_author ON feed_feedback(author_id);

-- ----- Takeout imports -----

CREATE TABLE IF NOT EXISTS takeout_import_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_name TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    phase TEXT,
    message TEXT,
    error_text TEXT,
    storage_path TEXT NOT NULL,
    fetch_metadata INTEGER NOT NULL DEFAULT 0,
    total_steps INTEGER NOT NULL DEFAULT 3,
    completed_steps INTEGER NOT NULL DEFAULT 0,
    subscriptions_imported INTEGER NOT NULL DEFAULT 0,
    playlists_imported INTEGER NOT NULL DEFAULT 0,
    history_imported INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    finished_at REAL
);
CREATE INDEX IF NOT EXISTS idx_takeout_import_jobs_created
    ON takeout_import_jobs(created_at DESC);
