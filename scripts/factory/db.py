"""SQLite 持久层。任何 step 都通过这里读写状态。"""
import os
from pathlib import Path
import sqlite3
from . import DATA_DIR, SKILL_ROOT

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
    failure_code    TEXT,
    retries         INTEGER DEFAULT 0,
    production_worker_pid INTEGER,
    production_started_at TIMESTAMP,
    production_heartbeat_at TEXT,
    worker_lease_id TEXT,
    recovery_attempts INTEGER NOT NULL DEFAULT 0,
    creative_ir_json TEXT,
    creative_ir_version INTEGER NOT NULL DEFAULT 0,
    production_spec_json TEXT,
    production_spec_hash TEXT,
    production_spec_version INTEGER NOT NULL DEFAULT 0,
    execution_engine TEXT NOT NULL DEFAULT 'legacy',
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
    video_model   TEXT,
    native_audio  INTEGER DEFAULT 0,
    video_qa      TEXT,
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

CREATE TABLE IF NOT EXISTS task_doctor_runs (
    run_id                 TEXT PRIMARY KEY,
    entity_type            TEXT NOT NULL,
    entity_id              TEXT NOT NULL,
    requested_actions_json TEXT NOT NULL DEFAULT '[]',
    applied_actions_json   TEXT NOT NULL DEFAULT '[]',
    before_json            TEXT NOT NULL DEFAULT '{}',
    after_json             TEXT NOT NULL DEFAULT '{}',
    status                 TEXT NOT NULL,
    created_at             TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_task_doctor_runs_entity
    ON task_doctor_runs(entity_type, entity_id, created_at);

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

CREATE TABLE IF NOT EXISTS production_nodes (
    job_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    node_type TEXT NOT NULL,
    paid INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'PENDING',
    spec_hash TEXT NOT NULL,
    input_json TEXT NOT NULL DEFAULT '{}',
    output_json TEXT NOT NULL DEFAULT '{}',
    idempotency_key TEXT NOT NULL,
    provider_task_id TEXT,
    estimated_cost_cents INTEGER NOT NULL DEFAULT 0,
    actual_cost_cents INTEGER NOT NULL DEFAULT 0,
    lease_id TEXT,
    heartbeat_at TEXT,
    error_code TEXT,
    next_action TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(job_id, node_id),
    UNIQUE(idempotency_key),
    FOREIGN KEY(job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS production_edges (
    job_id TEXT NOT NULL,
    from_node_id TEXT NOT NULL,
    to_node_id TEXT NOT NULL,
    edge_type TEXT NOT NULL,
    PRIMARY KEY(job_id, from_node_id, to_node_id),
    FOREIGN KEY(job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS production_node_events (
    job_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    event TEXT NOT NULL,
    from_state TEXT NOT NULL,
    to_state TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(job_id, event_id),
    FOREIGN KEY(job_id, node_id) REFERENCES production_nodes(job_id, node_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_production_nodes_state
    ON production_nodes(job_id, state, node_type);
CREATE INDEX IF NOT EXISTS idx_production_edges_target
    ON production_edges(job_id, to_node_id);
CREATE INDEX IF NOT EXISTS idx_production_node_events_node
    ON production_node_events(job_id, node_id, created_at);

CREATE TABLE IF NOT EXISTS production_compilation_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    status TEXT NOT NULL,
    v1_spec_hash TEXT,
    v2_spec_hash TEXT,
    diff_codes_json TEXT NOT NULL DEFAULT '[]',
    blockers_json TEXT NOT NULL DEFAULT '[]',
    mode TEXT,
    error TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_production_compilation_runs_job
    ON production_compilation_runs(job_id, created_at);

CREATE TABLE IF NOT EXISTS production_interventions (
    intervention_id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    intervention_type TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_production_interventions_job
    ON production_interventions(job_id, created_at);

CREATE TABLE IF NOT EXISTS provider_runtime (
    provider       TEXT PRIMARY KEY,
    status         TEXT NOT NULL DEFAULT 'available'
                   CHECK(status IN ('available','action_required')),
    reason_code    TEXT,
    reason_message TEXT,
    blocked_at     TIMESTAMP,
    cleared_at     TIMESTAMP
);

CREATE TABLE IF NOT EXISTS job_transitions (
    transition_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id       TEXT NOT NULL UNIQUE,
    job_id         TEXT NOT NULL,
    event          TEXT NOT NULL,
    from_state     TEXT NOT NULL,
    to_state       TEXT NOT NULL,
    payload_json   TEXT NOT NULL DEFAULT '{}',
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(job_id) REFERENCES jobs(job_id)
);

CREATE INDEX IF NOT EXISTS idx_job_transitions_job
    ON job_transitions(job_id, transition_id);

CREATE TABLE IF NOT EXISTS provider_capacity (
    provider         TEXT PRIMARY KEY,
    configured_limit INTEGER NOT NULL DEFAULT 1,
    learned_limit    INTEGER NOT NULL DEFAULT 1,
    success_streak   INTEGER NOT NULL DEFAULT 0,
    cooldown_until   TEXT,
    last_reason      TEXT,
    updated_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS character_asset_bindings (
    character_id   TEXT PRIMARY KEY,
    display_name   TEXT NOT NULL,
    identity_type  TEXT NOT NULL DEFAULT 'real_person',
    provider       TEXT NOT NULL DEFAULT 'volcengine_ark',
    asset_id       TEXT NOT NULL,
    asset_uri      TEXT NOT NULL,
    asset_group_id TEXT,
    source         TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'Active',
    background_description TEXT NOT NULL DEFAULT '',
    background_description_source TEXT NOT NULL DEFAULT '',
    metadata_json  TEXT NOT NULL DEFAULT '{}',
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_character_asset_id
    ON character_asset_bindings(asset_id);

CREATE TABLE IF NOT EXISTS character_voice_bindings (
    character_id   TEXT PRIMARY KEY,
    display_name   TEXT NOT NULL,
    provider       TEXT NOT NULL DEFAULT 'volcengine_tts',
    resource_id    TEXT NOT NULL DEFAULT 'seed-tts-2.0',
    voice_type     TEXT NOT NULL,
    voice_name     TEXT NOT NULL DEFAULT '',
    preview_url    TEXT NOT NULL DEFAULT '',
    source         TEXT NOT NULL CHECK(source IN ('auto','manual')),
    match_score    INTEGER NOT NULL DEFAULT 0,
    match_reasons_json TEXT NOT NULL DEFAULT '[]',
    profile_json   TEXT NOT NULL DEFAULT '{}',
    status         TEXT NOT NULL DEFAULT 'Active',
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_character_voice_type
    ON character_voice_bindings(voice_type);

CREATE TABLE IF NOT EXISTS character_reference_images (
    image_id       TEXT PRIMARY KEY,
    character_id   TEXT NOT NULL,
    relative_path  TEXT NOT NULL UNIQUE,
    original_name  TEXT NOT NULL,
    sort_order     INTEGER NOT NULL DEFAULT 0,
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(character_id) REFERENCES character_asset_bindings(character_id)
);

CREATE INDEX IF NOT EXISTS idx_character_reference_images_character
    ON character_reference_images(character_id, sort_order, created_at);

CREATE TABLE IF NOT EXISTS character_identity_anchors (
    job_id        TEXT NOT NULL,
    identity_key  TEXT NOT NULL,
    character_id  TEXT NOT NULL,
    display_name  TEXT NOT NULL,
    source_type   TEXT NOT NULL,
    state         TEXT NOT NULL DEFAULT 'PENDING',
    asset_uri     TEXT,
    anchor_mode   TEXT NOT NULL DEFAULT 'dynamic_video',
    image_path    TEXT,
    image_url     TEXT,
    video_path    TEXT,
    video_url     TEXT,
    preview_path  TEXT,
    duration_sec  REAL NOT NULL DEFAULT 0,
    cost_cents    INTEGER NOT NULL DEFAULT 0,
    approval_state TEXT NOT NULL DEFAULT 'PENDING_REVIEW',
    approved_at   TIMESTAMP,
    review_note   TEXT NOT NULL DEFAULT '',
    generator     TEXT NOT NULL DEFAULT '',
    model         TEXT NOT NULL DEFAULT '',
    error         TEXT,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(job_id, identity_key),
    FOREIGN KEY(job_id) REFERENCES jobs(job_id)
);

CREATE INDEX IF NOT EXISTS idx_character_identity_anchors_job
    ON character_identity_anchors(job_id, state);

CREATE TABLE IF NOT EXISTS scene_visual_anchors (
    anchor_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          TEXT NOT NULL,
    anchor_key      TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    master_indexes  TEXT NOT NULL DEFAULT '[]',
    state           TEXT NOT NULL DEFAULT 'PENDING',
    image_path      TEXT,
    image_url       TEXT,
    preview_path    TEXT,
    video_path      TEXT,
    video_url       TEXT,
    cost_cents      INTEGER NOT NULL DEFAULT 0,
    generator       TEXT NOT NULL DEFAULT '',
    model           TEXT NOT NULL DEFAULT '',
    approval_state  TEXT NOT NULL DEFAULT 'PENDING_REVIEW',
    approved_at     TIMESTAMP,
    review_note     TEXT NOT NULL DEFAULT '',
    error           TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(job_id, anchor_key),
    FOREIGN KEY(job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_scene_visual_anchors_job
    ON scene_visual_anchors(job_id, state);

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
    checks_json    TEXT NOT NULL DEFAULT '{}',
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

CREATE TABLE IF NOT EXISTS source_feeds (
    feed_id             TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    feed_type           TEXT NOT NULL,
    endpoint            TEXT NOT NULL,
    source_kind         TEXT NOT NULL DEFAULT 'hotspot',
    enabled             INTEGER NOT NULL DEFAULT 1,
    cursor              TEXT,
    last_collected_at   TIMESTAMP,
    last_error          TEXT NOT NULL DEFAULT '',
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS source_candidates (
    candidate_id    TEXT PRIMARY KEY,
    feed_id         TEXT NOT NULL,
    external_id     TEXT NOT NULL,
    title           TEXT NOT NULL,
    source_url      TEXT NOT NULL DEFAULT '',
    excerpt         TEXT NOT NULL DEFAULT '',
    published_at    TEXT,
    status          TEXT NOT NULL DEFAULT 'candidate',
    source_id       TEXT,
    raw_json        TEXT NOT NULL DEFAULT '{}',
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(feed_id, external_id),
    FOREIGN KEY(feed_id) REFERENCES source_feeds(feed_id),
    FOREIGN KEY(source_id) REFERENCES script_sources(source_id)
);

CREATE INDEX IF NOT EXISTS idx_source_candidates_status
    ON source_candidates(status, published_at, created_at);
CREATE INDEX IF NOT EXISTS idx_source_candidates_feed
    ON source_candidates(feed_id, created_at);

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
    revision            INTEGER NOT NULL DEFAULT 1,
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
    revision             INTEGER NOT NULL DEFAULT 1,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(brief_id, request_fingerprint, variant_index),
    FOREIGN KEY(brief_id) REFERENCES topic_briefs(brief_id),
    FOREIGN KEY(approved_job_id) REFERENCES jobs(job_id)
);

CREATE INDEX IF NOT EXISTS idx_script_drafts_brief
    ON script_drafts(brief_id, status, created_at);

CREATE TABLE IF NOT EXISTS content_revisions (
    revision_id  TEXT PRIMARY KEY,
    entity_type  TEXT NOT NULL,
    entity_id    TEXT NOT NULL,
    revision     INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    change_note  TEXT NOT NULL DEFAULT '',
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(entity_type, entity_id, revision)
);

CREATE INDEX IF NOT EXISTS idx_content_revisions_entity
    ON content_revisions(entity_type, entity_id, revision DESC);

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

CREATE TABLE IF NOT EXISTS platform_submissions (
    submission_id TEXT PRIMARY KEY,
    job_id         TEXT NOT NULL,
    platform       TEXT NOT NULL,
    state          TEXT NOT NULL DEFAULT 'draft'
                   CHECK(state IN ('draft','uploading','submitted','pending_review','published','rejected')),
    upload_id      TEXT,
    item_id        TEXT,
    title          TEXT,
    cover_tsp      REAL,
    last_error     TEXT NOT NULL DEFAULT '',
    metadata_json  TEXT NOT NULL DEFAULT '{}',
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(job_id, platform),
    FOREIGN KEY(job_id) REFERENCES jobs(job_id)
);

CREATE INDEX IF NOT EXISTS idx_platform_submissions_state
    ON platform_submissions(platform, state, updated_at);

CREATE TABLE IF NOT EXISTS platform_accounts (
    platform           TEXT PRIMARY KEY,
    account_id         TEXT NOT NULL,
    access_token       TEXT NOT NULL,
    refresh_token      TEXT NOT NULL,
    scope              TEXT NOT NULL DEFAULT '',
    access_expires_at  INTEGER NOT NULL,
    refresh_expires_at INTEGER NOT NULL,
    status             TEXT NOT NULL DEFAULT 'active',
    metadata_json      TEXT NOT NULL DEFAULT '{}',
    created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS platform_oauth_states (
    state        TEXT PRIMARY KEY,
    platform     TEXT NOT NULL,
    redirect_uri TEXT NOT NULL,
    expires_at   INTEGER NOT NULL,
    used_at      TIMESTAMP,
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_platform_oauth_states_platform
    ON platform_oauth_states(platform, expires_at);

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


def database_path() -> Path:
    """Resolve the database path, allowing isolated test/runtime instances."""
    override = os.environ.get("FACTORY_DB_PATH", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    runtime_data = os.environ.get("FACTORY_DATA_DIR", "").strip()
    if runtime_data:
        data_dir = Path(runtime_data).expanduser()
        if not data_dir.is_absolute():
            data_dir = SKILL_ROOT / data_dir
        return (data_dir / "factory.db").resolve()
    return Path(_DB_PATH)


def conn() -> sqlite3.Connection:
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(DDL)

    # 增量迁移:多角色 + dialogue/broll + 每镜音频
    _add_cols_if_missing(db, "jobs", [
        ("characters_json", "TEXT"),     # [{id,name,gender,image_path,image_url}]
        ("batch_id", "TEXT"),
        ("failure_code", "TEXT"),
        ("production_worker_pid", "INTEGER"),
        ("production_started_at", "TIMESTAMP"),
        ("production_heartbeat_at", "TEXT"),
        ("worker_lease_id", "TEXT"),
        ("recovery_attempts", "INTEGER NOT NULL DEFAULT 0"),
        ("creative_ir_json", "TEXT"),
        ("creative_ir_version", "INTEGER NOT NULL DEFAULT 0"),
        ("production_spec_json", "TEXT"),
        ("production_spec_hash", "TEXT"),
        ("production_spec_version", "INTEGER NOT NULL DEFAULT 0"),
        ("execution_engine", "TEXT NOT NULL DEFAULT 'legacy'"),
    ])
    _add_cols_if_missing(db, "production_nodes", [
        ("paid", "INTEGER NOT NULL DEFAULT 0"),
    ])
    _add_cols_if_missing(db, "character_asset_bindings", [
        ("identity_type", "TEXT NOT NULL DEFAULT 'real_person'"),
        ("background_description", "TEXT NOT NULL DEFAULT ''"),
        ("background_description_source", "TEXT NOT NULL DEFAULT ''"),
    ])
    _add_cols_if_missing(db, "character_identity_anchors", [
        ("anchor_mode", "TEXT NOT NULL DEFAULT 'dynamic_video'"),
        ("image_path", "TEXT"),
        ("image_url", "TEXT"),
        ("approval_state", "TEXT NOT NULL DEFAULT 'PENDING_REVIEW'"),
        ("approved_at", "TIMESTAMP"),
        ("review_note", "TEXT NOT NULL DEFAULT ''"),
        ("generator", "TEXT NOT NULL DEFAULT ''"),
        ("model", "TEXT NOT NULL DEFAULT ''"),
    ])
    _add_cols_if_missing(db, "scene_visual_anchors", [
        ("approval_state", "TEXT NOT NULL DEFAULT 'PENDING_REVIEW'"),
        ("approved_at", "TIMESTAMP"),
        ("review_note", "TEXT NOT NULL DEFAULT ''"),
        ("video_path", "TEXT"),
        ("video_url", "TEXT"),
        ("generator", "TEXT NOT NULL DEFAULT ''"),
        ("model", "TEXT NOT NULL DEFAULT ''"),
    ])
    db.execute(
        "UPDATE character_identity_anchors SET approval_state='APPROVED' "
        "WHERE source_type='volc_asset' AND state='READY'"
    )
    _add_cols_if_missing(db, "review_events", [
        ("first_pass", "INTEGER"),
        ("checks_json", "TEXT NOT NULL DEFAULT '{}'"),
    ])
    _add_cols_if_missing(db, "topic_briefs", [
        ("request_fingerprint", "TEXT NOT NULL DEFAULT ''"),
        ("approved_angle_index", "INTEGER"),
        ("revision", "INTEGER NOT NULL DEFAULT 1"),
    ])
    _add_cols_if_missing(db, "script_drafts", [
        ("revision", "INTEGER NOT NULL DEFAULT 1"),
    ])
    _add_cols_if_missing(db, "platform_publications", [
        ("published_at", "TEXT"),
        ("metadata_json", "TEXT NOT NULL DEFAULT '{}'"),
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
