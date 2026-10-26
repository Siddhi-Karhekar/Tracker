-- TeamPulse storage. Everything the agents read comes from here.
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- Team members and their identity on each platform (from team.toml)
CREATE TABLE IF NOT EXISTS members (
    handle          TEXT PRIMARY KEY,          -- stable internal id, e.g. "priya"
    display_name    TEXT NOT NULL,
    github_login    TEXT,
    notion_user_id  TEXT,
    discord_id      TEXT,
    discord_name    TEXT,
    opted_in        INTEGER NOT NULL DEFAULT 1
);

-- Master task list: GitHub issues + Notion task pages, linked when Notion has a "GitHub Issue" number
CREATE TABLE IF NOT EXISTS tasks (
    task_id         TEXT PRIMARY KEY,          -- "T-<issue#>" or "N-<first 8 of notion page id>"
    title           TEXT NOT NULL,
    github_issue    INTEGER,
    notion_page_id  TEXT,
    updated_at      TEXT NOT NULL
);

-- Every raw item from every source, normalised. One row per issue, PR, review, commit,
-- comment, Notion page version, Discord message.
CREATE TABLE IF NOT EXISTS events (
    event_id    TEXT PRIMARY KEY,              -- "<source>:<type>:<native id>"
    source      TEXT NOT NULL CHECK (source IN ('github','notion','discord')),
    type        TEXT NOT NULL,                 -- issue, pull_request, review, commit, issue_comment, notion_page, message
    native_id   TEXT NOT NULL,
    url         TEXT,
    author      TEXT,                          -- member handle when known, else raw platform name
    author_raw  TEXT,                          -- platform login / id / name as seen
    timestamp   TEXT NOT NULL,                 -- ISO-8601 UTC
    channel     TEXT,                          -- Discord channel or thread name
    parent_id   TEXT,                          -- event_id this replies to / belongs to
    title       TEXT,
    body        TEXT,
    task_ids    TEXT,                          -- JSON list; filled by deterministic links (Closes #n, Notion relation)
    payload     TEXT,                          -- JSON with source-specific fields
    deleted     INTEGER NOT NULL DEFAULT 0,
    ingested_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_source_time ON events (source, timestamp);
CREATE INDEX IF NOT EXISTS idx_events_author ON events (author);

-- The common format all three features compare. Structured sources (GitHub, Notion) produce
-- claims at ingestion; Discord claims come from the Blocker Agent / entity linking (Weeks 5–6).
CREATE TABLE IF NOT EXISTS claims (
    claim_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL,
    attribute   TEXT NOT NULL CHECK (attribute IN ('status','owner','deadline')),
    value       TEXT NOT NULL,                 -- status: todo|in_progress|done|open|dropped ; owner: handle ; deadline: YYYY-MM-DD
    source      TEXT NOT NULL,
    source_ref  TEXT NOT NULL,                 -- event_id the claim was read from
    author      TEXT,
    timestamp   TEXT NOT NULL,                 -- when the source said it
    confidence  REAL NOT NULL DEFAULT 1.0,
    extractor   TEXT NOT NULL,                 -- "rule:github", "rule:notion", "llm:<model>"
    UNIQUE (task_id, attribute, value, source, source_ref)
);
CREATE INDEX IF NOT EXISTS idx_claims_task ON claims (task_id, attribute, timestamp);

-- Incremental sync cursors (per repo, per Notion data source, per Discord channel)
CREATE TABLE IF NOT EXISTS sync_state (
    source      TEXT NOT NULL,
    scope       TEXT NOT NULL,
    cursor      TEXT,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (source, scope)
);

-- Long-term memory: explicit facts only, re-verified against live data before use
CREATE TABLE IF NOT EXISTS memory (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    fact          TEXT NOT NULL,
    source        TEXT,
    created_at    TEXT NOT NULL,
    last_verified TEXT
);

-- Privacy: members who opted out. Their Discord messages are not stored and past ones are purged.
CREATE TABLE IF NOT EXISTS optouts (
    handle       TEXT PRIMARY KEY,
    requested_at TEXT NOT NULL
);
