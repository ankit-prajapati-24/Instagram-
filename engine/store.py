"""SQLite store.

Schema mirrors the Postgres design in the feasibility report, so moving to a
server database later is a driver swap rather than a rewrite.

Two tables carry more weight than the rest:
  ``assets`` — provider, licence, source URL and checksum per asset. This table
               *is* the copyright defence.
  ``costs``  — fed straight from the X-OmniRoute-* response headers, so
               per-video true cost needs no separate accounting.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from engine.contract import ReelPlan
from engine.omniroute import CostRecord

SCHEMA = """
CREATE TABLE IF NOT EXISTS topics (
  slug TEXT PRIMARY KEY,
  raw TEXT NOT NULL,
  dedupe_hash TEXT NOT NULL,
  discovered_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plans (
  plan_id TEXT PRIMARY KEY,
  slug TEXT NOT NULL,
  dedupe_hash TEXT NOT NULL,
  status TEXT NOT NULL,
  plan_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_plans_hash ON plans(dedupe_hash);
CREATE INDEX IF NOT EXISTS idx_plans_status ON plans(status);

CREATE TABLE IF NOT EXISTS claims (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  plan_id TEXT NOT NULL,
  beat_id TEXT,
  text TEXT NOT NULL,
  source_url TEXT,
  confidence TEXT
);
CREATE TABLE IF NOT EXISTS assets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  plan_id TEXT NOT NULL,
  beat_id TEXT,
  kind TEXT NOT NULL,
  provider TEXT,
  path TEXT,
  source_url TEXT,
  checksum TEXT,
  licence TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sticker_choices (
  plan_id TEXT NOT NULL,
  beat_id TEXT NOT NULL,
  slug TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (plan_id, beat_id)
);
CREATE TABLE IF NOT EXISTS renders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  plan_id TEXT NOT NULL,
  idempotency_key TEXT UNIQUE,
  status TEXT NOT NULL,
  attempts INTEGER DEFAULT 0,
  output_path TEXT,
  duration_s REAL,
  error_log TEXT,
  attribution TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS publications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  plan_id TEXT NOT NULL,
  platform TEXT NOT NULL,
  external_id TEXT,
  published_at TEXT,
  synthetic_flag_set INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS metrics_daily (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  plan_id TEXT NOT NULL,
  platform TEXT NOT NULL,
  date TEXT NOT NULL,
  views INTEGER, saves INTEGER, shares INTEGER, comments INTEGER,
  avg_view_pct REAL,
  UNIQUE(plan_id, platform, date)
);
CREATE TABLE IF NOT EXISTS costs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  plan_id TEXT,
  stage TEXT NOT NULL,
  provider TEXT,
  model TEXT,
  usd REAL NOT NULL DEFAULT 0,
  fallback_attempts INTEGER DEFAULT 0,
  latency_ms INTEGER DEFAULT 0,
  at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_costs_at ON costs(at);
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  idempotency_key TEXT UNIQUE,
  type TEXT NOT NULL,
  payload TEXT,
  status TEXT NOT NULL,
  attempts INTEGER DEFAULT 0,
  next_retry_at TEXT,
  last_error TEXT
);
CREATE TABLE IF NOT EXISTS embeddings (
  plan_id TEXT PRIMARY KEY,
  vector TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entities (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  plan_id TEXT NOT NULL,
  entity TEXT NOT NULL,
  seen_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entities_entity ON entities(entity);
"""


# Columns added to a table after this database first existed. ``CREATE TABLE
# IF NOT EXISTS`` does nothing to a table that is already there, so every
# clone that has ever been rendered against still carries the old shape and
# something has to widen it. Guarded by ``PRAGMA table_info`` rather than by
# swallowing sqlite's duplicate-column error, so it runs exactly once and is a
# no-op on a fresh database and on every later start.
#
# Every one of these must be nullable and must mean something sane when NULL,
# because the rows written before the column existed keep that NULL forever
# and are deliberately never backfilled: a render from before ``attribution``
# was recorded genuinely does not know what it used, and guessing on its
# behalf is the bug this column exists to remove.
ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("renders", "attribution", "TEXT"),
)


def _apply_added_columns(conn: sqlite3.Connection) -> None:
    for table, column, decl in ADDED_COLUMNS:
        present = {row["name"]
                   for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in present:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


# ``sticker_choices`` shipped once keyed by ``(plan_id, trigger)`` before
# this plan re-keyed it to ``(plan_id, beat_id)``. ``CREATE TABLE IF NOT
# EXISTS`` in ``SCHEMA`` is a no-op on a database where the old-shaped table
# already exists, and SQLite cannot ``ALTER TABLE`` a primary key -- so
# unlike ``ADDED_COLUMNS`` above, this needs its own step that drops and
# recreates the table rather than widening it in place.
#
# The old rows genuinely cannot be migrated: a trigger name does not
# identify a beat. Discarding them is correct; doing it silently at startup
# is not, so this prints what it threw away rather than swallowing it.
def _fix_table_shapes(conn: sqlite3.Connection) -> None:
    columns = {row["name"]
               for row in conn.execute("PRAGMA table_info(sticker_choices)")}
    if "trigger" not in columns:
        return
    count = conn.execute(
        "SELECT COUNT(*) AS n FROM sticker_choices").fetchone()["n"]
    conn.execute("DROP TABLE sticker_choices")
    conn.execute(
        "CREATE TABLE sticker_choices ("
        "  plan_id TEXT NOT NULL,"
        "  beat_id TEXT NOT NULL,"
        "  slug TEXT NOT NULL,"
        "  created_at TEXT NOT NULL,"
        "  PRIMARY KEY (plan_id, beat_id)"
        ")")
    print(f"[store] sticker_choices was keyed by trigger; discarded "
          f"{count} pick(s) that cannot be mapped to a beat",
          file=sys.stderr, flush=True)


# A plan only blocks its own topic once it actually became something. Gate
# rejections stay in the table for the audit trail without poisoning retries.
COUNTED_STATUSES = ("approved", "produced", "published")
COUNTED_PLACEHOLDERS = ",".join("?" * len(COUNTED_STATUSES))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def init(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            _apply_added_columns(conn)
            _fix_table_shapes(conn)

    # -- plans ------------------------------------------------------------
    def save_plan(self, plan: ReelPlan, status: str = "draft") -> None:
        now = _now()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO topics(slug, raw, dedupe_hash, discovered_at) "
                "VALUES(?,?,?,?) ON CONFLICT(slug) DO NOTHING",
                (plan.topic.slug, plan.topic.raw, plan.topic.dedupe_hash, now))
            conn.execute(
                "INSERT INTO plans(plan_id, slug, dedupe_hash, status, "
                "plan_json, created_at, updated_at) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(plan_id) DO UPDATE SET "
                "status=excluded.status, plan_json=excluded.plan_json, "
                "updated_at=excluded.updated_at",
                (plan.plan_id, plan.topic.slug, plan.topic.dedupe_hash,
                 status, plan.model_dump_json(), now, now))
            conn.execute("DELETE FROM claims WHERE plan_id=?", (plan.plan_id,))
            for claim in plan.provenance.claims:
                conn.execute(
                    "INSERT INTO claims(plan_id, beat_id, text, source_url, "
                    "confidence) VALUES(?,?,?,?,?)",
                    (plan.plan_id, claim.beat_id, claim.text,
                     claim.source_url, claim.confidence))

    def get_plan(self, plan_id: str) -> ReelPlan | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT plan_json FROM plans WHERE plan_id=?",
                (plan_id,)).fetchone()
        return ReelPlan.model_validate_json(row["plan_json"]) if row else None

    def set_status(self, plan_id: str, status: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE plans SET status=?, updated_at=? WHERE plan_id=?",
                (status, _now(), plan_id))

    def list_plans(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT p.plan_id, p.slug, p.status, p.created_at, "
                "t.raw AS topic, "
                "(SELECT COALESCE(SUM(usd),0) FROM costs c "
                " WHERE c.plan_id=p.plan_id) AS usd, "
                "(SELECT output_path FROM renders r WHERE r.plan_id=p.plan_id "
                " ORDER BY r.id DESC LIMIT 1) AS video "
                "FROM plans p LEFT JOIN topics t ON t.slug=p.slug "
                "ORDER BY p.created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def plan_status(self, plan_id: str) -> str | None:
        with self._conn() as conn:
            row = conn.execute("SELECT status FROM plans WHERE plan_id=?",
                               (plan_id,)).fetchone()
        return row["status"] if row else None

    def hash_exists(self, dedupe_hash: str) -> bool:
        """Was this exact topic ever carried through to a video?

        Deliberately restricted to COUNTED_STATUSES. Rejected plans are kept
        for the audit trail, but counting them made a topic that failed the
        moderation gate permanently un-retryable, reported as the misleading
        "slug already produced".
        """
        with self._conn() as conn:
            row = conn.execute(
                f"SELECT 1 FROM plans WHERE dedupe_hash=? "
                f"AND status IN ({COUNTED_PLACEHOLDERS}) LIMIT 1",
                (dedupe_hash, *COUNTED_STATUSES)).fetchone()
        return row is not None

    def published_slugs(self, limit: int = 400) -> list[str]:
        """Slugs that reached production, newest first."""
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT slug FROM plans WHERE status IN "
                f"({COUNTED_PLACEHOLDERS}) ORDER BY created_at DESC LIMIT ?",
                (*COUNTED_STATUSES, limit)).fetchall()
        return [r["slug"] for r in rows]

    # -- costs ------------------------------------------------------------
    def record_cost(self, plan_id: str | None, stage: str,
                    cost: CostRecord) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO costs(plan_id, stage, provider, model, usd, "
                "fallback_attempts, latency_ms, at) VALUES(?,?,?,?,?,?,?,?)",
                (plan_id, stage, cost.provider, cost.model, cost.usd,
                 cost.fallback_attempts, cost.latency_ms, _now()))

    def plan_cost(self, plan_id: str) -> float:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(usd),0) AS s FROM costs WHERE plan_id=?",
                (plan_id,)).fetchone()
        return float(row["s"])

    def today_usd(self) -> float:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(usd),0) AS s FROM costs "
                "WHERE date(at)=date('now')").fetchone()
        return float(row["s"])

    # -- assets -----------------------------------------------------------
    def save_asset(self, plan_id: str, beat_id: str | None, kind: str,
                   provider: str, path: str, source_url: str | None = None,
                   checksum: str | None = None,
                   licence: str = "ai-generated") -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO assets(plan_id, beat_id, kind, provider, path, "
                "source_url, checksum, licence, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (plan_id, beat_id, kind, provider, path, source_url,
                 checksum, licence, _now()))

    def plan_assets(self, plan_id: str) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM assets WHERE plan_id=? ORDER BY id",
                (plan_id,)).fetchall()
        return [dict(r) for r in rows]

    # -- renders ----------------------------------------------------------
    def record_render(self, plan_id: str, idempotency_key: str, status: str,
                      output_path: str | None = None,
                      duration_s: float | None = None,
                      error_log: str | None = None,
                      attribution: str | None = None) -> None:
        """Write what this render did. ``attribution`` is the credit line the
        stickers it actually composited oblige us to print.

        It is stored rather than worked out later because the only honest
        answer to "does this MP4 owe Lordicon a credit?" is what was on the
        timeline when ffmpeg ran. Re-deriving it at publish time reads
        today's settings and today's baked art against a file that may be
        days old, which gets it wrong in both directions: a credit on a reel
        made before any art existed, and — the licence-breaking one — no
        credit on a reel full of it after ``RAHASYA_STICKERS`` was turned off
        or the bake was re-made at another size.

        ``None`` means "this render owes no credit", and a row written before
        this column existed means the same thing, which is why it is nullable
        and why nothing backfills it.
        """
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO renders(plan_id, idempotency_key, status, "
                "attempts, output_path, duration_s, error_log, attribution, "
                "created_at) VALUES(?,?,?,1,?,?,?,?,?) "
                "ON CONFLICT(idempotency_key) DO UPDATE SET "
                "status=excluded.status, attempts=renders.attempts+1, "
                "output_path=excluded.output_path, "
                "duration_s=excluded.duration_s, "
                "error_log=excluded.error_log, "
                "attribution=excluded.attribution",
                (plan_id, idempotency_key, status, output_path, duration_s,
                 error_log, attribution, _now()))

    def render_duration(self, plan_id: str) -> float | None:
        """Probed length of the latest successful render, if there is one."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT duration_s FROM renders WHERE plan_id=? "
                "AND status='done' AND duration_s IS NOT NULL "
                "ORDER BY id DESC LIMIT 1", (plan_id,)).fetchone()
        return float(row["duration_s"]) if row else None

    def render_attribution(self, plan_id: str) -> str | None:
        """The credit line the latest successful render recorded, if any.

        NULL — no row, a row from before the column existed, or a render that
        composited no designed art — all mean the same thing: this video owes
        no credit. Publishing must not look any further than this.
        """
        with self._conn() as conn:
            row = conn.execute(
                "SELECT attribution FROM renders WHERE plan_id=? "
                "AND status='done' ORDER BY id DESC LIMIT 1",
                (plan_id,)).fetchone()
        return row["attribution"] if row else None

    # -- sticker choices --------------------------------------------------
    def choose_sticker(self, plan_id: str, beat_id: str, slug: str) -> None:
        """Record which Lordicon icon this reel should use at this beat.

        Per beat, not per trigger: ``find_cues`` already allows one sticker
        per beat, so the beat id is the cue's identity on both rungs. Keyed
        by trigger name, a reel whose script said "water" twice could only
        ever wear one icon for both.

        Per plan on purpose: the same beat can wear different art in
        different reels, the way clips already do. Replacing rather than
        appending, because there is one answer per beat per reel and a
        history of rejected picks would only have to be filtered out again.
        """
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO sticker_choices(plan_id, beat_id, slug, "
                "created_at) VALUES(?,?,?,?) "
                "ON CONFLICT(plan_id, beat_id) DO UPDATE SET "
                "slug=excluded.slug, created_at=excluded.created_at",
                (plan_id, beat_id, slug, _now()))

    def sticker_choices(self, plan_id: str) -> dict[str, str]:
        """``{beat_id: slug}`` for this plan, empty when nothing was chosen."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT beat_id, slug FROM sticker_choices WHERE plan_id=?",
                (plan_id,)).fetchall()
        return {row["beat_id"]: row["slug"] for row in rows}

    # -- dedup support ----------------------------------------------------
    def save_embedding(self, plan_id: str, vector: list[float]) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO embeddings(plan_id, vector, created_at) "
                "VALUES(?,?,?) ON CONFLICT(plan_id) DO UPDATE SET "
                "vector=excluded.vector",
                (plan_id, json.dumps(vector), _now()))

    def recent_embeddings(self, limit: int = 400
                          ) -> list[tuple[str, list[float]]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT plan_id, vector FROM embeddings "
                "ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [(r["plan_id"], json.loads(r["vector"])) for r in rows]

    def record_entities(self, plan_id: str, entities: Iterable[str]) -> None:
        now = _now()
        with self._conn() as conn:
            for entity in entities:
                cleaned = (entity or "").strip().lower()
                if cleaned:
                    conn.execute(
                        "INSERT INTO entities(plan_id, entity, seen_at) "
                        "VALUES(?,?,?)", (plan_id, cleaned, now))

    def entity_last_seen(self, entity: str) -> datetime | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT MAX(seen_at) AS s FROM entities WHERE entity=?",
                ((entity or "").strip().lower(),)).fetchone()
        if not row or not row["s"]:
            return None
        return datetime.fromisoformat(row["s"])

    def entities_in_cooldown(self, entities: Iterable[str],
                             days: int) -> list[str]:
        return [name for name, _ in self.cooldown_detail(entities, days)]

    def cooldown_detail(self, entities: Iterable[str],
                        days: int) -> list[tuple[str, int]]:
        """Blocked entities with how many days are left on each.

        The bare name list was not actionable: it named an entity without
        saying when it frees up, so the only recourse was to guess.
        """
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=days)
        blocked: list[tuple[str, int]] = []
        for entity in entities:
            seen = self.entity_last_seen(entity)
            if seen and seen > cutoff:
                remaining = days - (now - seen).days
                blocked.append((entity, max(remaining, 1)))
        return blocked

    def clear_entities(self, plan_id: str | None = None) -> int:
        """Forget recorded entities. Returns how many rows went.

        Needed because a bad research call can write entities that block
        unrelated topics, and there was otherwise no way to undo that.
        """
        with self._conn() as conn:
            if plan_id:
                cur = conn.execute("DELETE FROM entities WHERE plan_id=?",
                                   (plan_id,))
            else:
                cur = conn.execute("DELETE FROM entities")
            return cur.rowcount
