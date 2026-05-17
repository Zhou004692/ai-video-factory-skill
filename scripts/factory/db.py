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

CREATE INDEX IF NOT EXISTS idx_jobs_state  ON jobs(state);
CREATE INDEX IF NOT EXISTS idx_shots_job   ON shots(job_id);
CREATE INDEX IF NOT EXISTS idx_events_job  ON events(job_id);
"""

_DB_PATH = DATA_DIR / "factory.db"


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
    ])
    _add_cols_if_missing(db, "shots", [
        ("narrative_type",    "TEXT"),   # dialogue | broll
        ("speaker_id",        "TEXT"),
        ("audio_path",        "TEXT"),
        ("audio_url",         "TEXT"),
        ("video_url",         "TEXT"),
        ("anchor_character",  "TEXT"),   # broll 可选,用 character 图做 Seedance 首帧
        ("character_in_scene_path", "TEXT"),  # dialogue 镜头自己的 in-scene 图
        ("character_in_scene_url",  "TEXT"),
    ])
    db.commit()
    return db


def log(db: sqlite3.Connection, job_id: str, step: str, level: str, msg: str) -> None:
    db.execute(
        "INSERT INTO events(job_id, step, level, message) VALUES(?,?,?,?)",
        (job_id, step, level, msg[:2000])
    )
    db.commit()
