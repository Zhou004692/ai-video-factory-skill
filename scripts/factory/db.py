"""SQLite 持久层。任何 step 都通过这里读写状态。"""
import sqlite3
from . import DATA_DIR

DDL = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id          TEXT PRIMARY KEY,
    topic_id        TEXT NOT NULL,
    title           TEXT NOT NULL,
    track           TEXT,
    style           TEXT,
    target_audience TEXT,
    priority        INTEGER DEFAULT 3,
    batch_id        TEXT,
    state           TEXT NOT NULL DEFAULT 'PENDING',
    storyboard      TEXT,
    character_img   TEXT,
    voice_path      TEXT,
    bgm_path        TEXT,
    final_path      TEXT,
    publish_url     TEXT,
    cost_cents      INTEGER DEFAULT 0,
    error           TEXT,
    retries         INTEGER DEFAULT 0,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS shots (
    shot_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id        TEXT NOT NULL,
    idx           INTEGER NOT NULL,
    duration_sec  REAL,
    shot_type     TEXT,
    camera        TEXT,
    prompt        TEXT,
    voiceover     TEXT,
    subtitle      TEXT,
    video_path    TEXT,
    state         TEXT DEFAULT 'PENDING',
    provider      TEXT,
    cost_cents    INTEGER DEFAULT 0,
    error         TEXT,
    FOREIGN KEY(job_id) REFERENCES jobs(job_id),
    UNIQUE(job_id, idx)
);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id  TEXT,
    step    TEXT,
    level   TEXT,
    message TEXT,
    ts      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS provider_calls (
    task_id      TEXT PRIMARY KEY,
    job_id       TEXT NOT NULL,
    shot_idx     INTEGER NOT NULL,
    provider     TEXT NOT NULL,
    intent       TEXT,
    cost_cents   INTEGER NOT NULL DEFAULT 0,
    usage_json   TEXT,
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_jobs_state  ON jobs(state);
CREATE INDEX IF NOT EXISTS idx_shots_job   ON shots(job_id);
CREATE INDEX IF NOT EXISTS idx_events_job  ON events(job_id);
CREATE INDEX IF NOT EXISTS idx_provider_calls_job
    ON provider_calls(job_id, shot_idx);

CREATE TABLE IF NOT EXISTS media_assets (
    asset_id         TEXT PRIMARY KEY,
    media_type       TEXT NOT NULL DEFAULT 'video',
    path             TEXT NOT NULL UNIQUE,
    tags_json        TEXT NOT NULL DEFAULT '[]',
    duration_sec     REAL NOT NULL,
    orientation      TEXT NOT NULL DEFAULT '9:16',
    rights_status    TEXT NOT NULL DEFAULT 'owned',
    approval_status  TEXT NOT NULL DEFAULT 'approved',
    use_count        INTEGER NOT NULL DEFAULT 0,
    created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS asset_usages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id    TEXT NOT NULL,
    job_id      TEXT NOT NULL,
    master_idx  INTEGER NOT NULL,
    match_score REAL NOT NULL DEFAULT 0,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(asset_id, job_id, master_idx),
    FOREIGN KEY(asset_id) REFERENCES media_assets(asset_id)
);

CREATE INDEX IF NOT EXISTS idx_media_assets_approval
    ON media_assets(media_type, approval_status, rights_status);
CREATE INDEX IF NOT EXISTS idx_asset_usages_job
    ON asset_usages(job_id, master_idx);

CREATE TABLE IF NOT EXISTS review_events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id         TEXT NOT NULL,
    decision       TEXT NOT NULL,
    manual_minutes REAL NOT NULL DEFAULT 0,
    first_pass     INTEGER,
    notes          TEXT,
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(job_id) REFERENCES jobs(job_id)
);

CREATE INDEX IF NOT EXISTS idx_review_events_job
    ON review_events(job_id, decision);

CREATE TABLE IF NOT EXISTS script_sources (
    source_id      TEXT PRIMARY KEY,
    fingerprint    TEXT NOT NULL UNIQUE,
    source_type    TEXT NOT NULL,
    title          TEXT,
    source_uri     TEXT,
    content        TEXT NOT NULL,
    metadata_json  TEXT NOT NULL DEFAULT '{}',
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS topic_script_sources (
    topic_id     TEXT NOT NULL,
    source_id    TEXT NOT NULL,
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(topic_id, source_id),
    FOREIGN KEY(source_id) REFERENCES script_sources(source_id)
);

CREATE INDEX IF NOT EXISTS idx_topic_script_sources_topic
    ON topic_script_sources(topic_id);

CREATE TABLE IF NOT EXISTS topic_briefs (
    brief_id           TEXT PRIMARY KEY,
    request_fingerprint TEXT NOT NULL DEFAULT '',
    status             TEXT NOT NULL DEFAULT 'candidate',
    recommended_title  TEXT NOT NULL,
    brief_json         TEXT NOT NULL,
    duplicate_json     TEXT,
    input_tokens       INTEGER NOT NULL DEFAULT 0,
    output_tokens      INTEGER NOT NULL DEFAULT 0,
    approved_topic_id  TEXT,
    approved_job_id    TEXT,
    approved_angle_index INTEGER,
    created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS topic_brief_sources (
    brief_id    TEXT NOT NULL,
    source_id   TEXT NOT NULL,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(brief_id, source_id),
    FOREIGN KEY(brief_id) REFERENCES topic_briefs(brief_id),
    FOREIGN KEY(source_id) REFERENCES script_sources(source_id)
);

CREATE INDEX IF NOT EXISTS idx_topic_briefs_status
    ON topic_briefs(status, created_at);

CREATE TABLE IF NOT EXISTS script_drafts (
    draft_id            TEXT PRIMARY KEY,
    brief_id            TEXT NOT NULL,
    variant_index       INTEGER NOT NULL,
    status              TEXT NOT NULL DEFAULT 'candidate',
    request_fingerprint TEXT NOT NULL,
    script_json         TEXT NOT NULL,
    recommended_index   INTEGER NOT NULL DEFAULT 0,
    recommendation_reason TEXT,
    input_tokens        INTEGER NOT NULL DEFAULT 0,
    output_tokens       INTEGER NOT NULL DEFAULT 0,
    approved_job_id     TEXT,
    storyboard_json     TEXT,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(brief_id, request_fingerprint, variant_index),
    FOREIGN KEY(brief_id) REFERENCES topic_briefs(brief_id),
    FOREIGN KEY(approved_job_id) REFERENCES jobs(job_id)
);

CREATE INDEX IF NOT EXISTS idx_script_drafts_brief
    ON script_drafts(brief_id, status, created_at);

CREATE TABLE IF NOT EXISTS performance_snapshots (
    snapshot_id       TEXT PRIMARY KEY,
    job_id            TEXT NOT NULL,
    platform          TEXT NOT NULL,
    captured_at       TEXT NOT NULL,
    views             INTEGER NOT NULL DEFAULT 0,
    completed_views   INTEGER,
    completion_rate   REAL,
    average_watch_sec REAL,
    likes             INTEGER NOT NULL DEFAULT 0,
    comments          INTEGER NOT NULL DEFAULT 0,
    shares            INTEGER NOT NULL DEFAULT 0,
    saves              INTEGER NOT NULL DEFAULT 0,
    follows            INTEGER NOT NULL DEFAULT 0,
    leads              INTEGER NOT NULL DEFAULT 0,
    metadata_json      TEXT NOT NULL DEFAULT '{}',
    created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(job_id, platform, captured_at),
    FOREIGN KEY(job_id) REFERENCES jobs(job_id)
);

CREATE INDEX IF NOT EXISTS idx_performance_job_platform
    ON performance_snapshots(job_id, platform, captured_at);

CREATE TABLE IF NOT EXISTS platform_publications (
    publication_id TEXT PRIMARY KEY,
    job_id          TEXT NOT NULL,
    platform        TEXT NOT NULL,
    item_id         TEXT NOT NULL,
    item_url        TEXT,
    title           TEXT,
    published_at    TEXT,
    metadata_json   TEXT NOT NULL DEFAULT '{}',
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(platform, item_id),
    UNIQUE(job_id, platform),
    FOREIGN KEY(job_id) REFERENCES jobs(job_id)
);

CREATE INDEX IF NOT EXISTS idx_platform_publications_job
    ON platform_publications(job_id, platform);

CREATE TABLE IF NOT EXISTS creative_angle_tags (
    brief_id          TEXT NOT NULL,
    angle_index       INTEGER NOT NULL,
    hook_type         TEXT NOT NULL,
    narrative_pattern TEXT NOT NULL,
    proof_type        TEXT NOT NULL,
    cta_type          TEXT NOT NULL,
    notes             TEXT,
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(brief_id, angle_index),
    FOREIGN KEY(brief_id) REFERENCES topic_briefs(brief_id)
);

CREATE TABLE IF NOT EXISTS creative_experiments (
    experiment_id       TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'draft',
    platform            TEXT NOT NULL,
    window_hours        REAL NOT NULL,
    min_window_coverage REAL NOT NULL DEFAULT 0.8,
    variable_dimension  TEXT NOT NULL,
    control_value       TEXT NOT NULL,
    treatment_value     TEXT NOT NULL,
    primary_metric      TEXT NOT NULL,
    hypothesis          TEXT NOT NULL,
    audience            TEXT NOT NULL,
    production_profile  TEXT NOT NULL,
    min_views           INTEGER NOT NULL DEFAULT 100,
    min_jobs_per_arm    INTEGER NOT NULL DEFAULT 2,
    planned_jobs_per_arm INTEGER NOT NULL DEFAULT 2,
    minimum_effect      REAL NOT NULL DEFAULT 0,
    fixed_tags_json     TEXT,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS creative_experiment_jobs (
    experiment_id TEXT NOT NULL,
    job_id         TEXT NOT NULL,
    arm            TEXT NOT NULL,
    assignment_method TEXT NOT NULL DEFAULT 'manual',
    allocation_token  TEXT,
    assigned_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(experiment_id, job_id),
    FOREIGN KEY(experiment_id) REFERENCES creative_experiments(experiment_id),
    FOREIGN KEY(job_id) REFERENCES jobs(job_id)
);

CREATE INDEX IF NOT EXISTS idx_creative_experiment_jobs_arm
    ON creative_experiment_jobs(experiment_id, arm);

CREATE TABLE IF NOT EXISTS creative_experiment_slots (
    slot_id           TEXT PRIMARY KEY,
    experiment_id     TEXT NOT NULL,
    subject_key       TEXT NOT NULL,
    arm               TEXT NOT NULL,
    allocation_method TEXT NOT NULL DEFAULT 'system_balanced',
    allocation_hash   TEXT NOT NULL,
    bound_job_id      TEXT,
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(experiment_id, subject_key),
    UNIQUE(experiment_id, bound_job_id),
    FOREIGN KEY(experiment_id) REFERENCES creative_experiments(experiment_id),
    FOREIGN KEY(bound_job_id) REFERENCES jobs(job_id)
);

CREATE INDEX IF NOT EXISTS idx_creative_experiment_slots_arm
    ON creative_experiment_slots(experiment_id, arm);

"""

_DB_PATH = DATA_DIR / "factory.db"

SHOT_OBSERVABILITY_COLUMNS = [
    ("video_model", "TEXT"),
    ("native_audio", "INTEGER DEFAULT 0"),
    ("video_qa", "TEXT"),
]


def _add_cols_if_missing(db: sqlite3.Connection, table: str,
                         cols: list[tuple[str, str]]) -> None:
    existing = {row[1] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, ddl in cols:
        if name not in existing:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def conn() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(_DB_PATH), check_same_thread=False, timeout=30)
    db.row_factory = sqlite3.Row
    db.executescript(DDL)

    # 增量迁移:多角色 + dialogue/broll + 每镜音频
    _add_cols_if_missing(db, "jobs", [
        ("characters_json", "TEXT"),     # [{id,name,gender,image_path,image_url}]
        ("batch_id", "TEXT"),
    ])
    _add_cols_if_missing(db, "review_events", [
        ("first_pass", "INTEGER"),
    ])
    _add_cols_if_missing(db, "topic_briefs", [
        ("request_fingerprint", "TEXT NOT NULL DEFAULT ''"),
        ("approved_angle_index", "INTEGER"),
    ])
    _add_cols_if_missing(db, "platform_publications", [
        ("published_at", "TEXT"),
    ])
    _add_cols_if_missing(db, "creative_experiment_jobs", [
        ("assignment_method", "TEXT NOT NULL DEFAULT 'manual'"),
        ("allocation_token", "TEXT"),
    ])
    _add_cols_if_missing(db, "creative_experiments", [
        ("planned_jobs_per_arm", "INTEGER NOT NULL DEFAULT 2"),
    ])
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_topic_briefs_request "
        "ON topic_briefs(request_fingerprint, created_at)"
    )
    _add_cols_if_missing(db, "shots", [
        ("narrative_type",    "TEXT"),   # dialogue | broll
        ("speaker_id",        "TEXT"),
        ("audio_path",        "TEXT"),
        ("audio_url",         "TEXT"),
        ("video_url",         "TEXT"),
        ("anchor_character",  "TEXT"),   # broll 可选,用 character 图做 Seedance 首帧
        ("character_in_scene_path", "TEXT"),  # dialogue 镜头自己的 in-scene 图
        ("character_in_scene_url",  "TEXT"),
    ] + SHOT_OBSERVABILITY_COLUMNS)
    db.commit()
    return db


def log(db: sqlite3.Connection, job_id: str, step: str, level: str, msg: str) -> None:
    db.execute(
        "INSERT INTO events(job_id, step, level, message) VALUES(?,?,?,?)",
        (job_id, step, level, msg[:2000])
    )
    db.commit()
