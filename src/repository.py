import json
import sqlite3
from datetime import datetime, timezone

from .domain import ConflictError, NotFoundError


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SQLiteRepository:
    def __init__(self, path):
        self.path = str(path)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self):
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS entities (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_entities_kind_status
                    ON entities(kind, status);
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    actor_role TEXT NOT NULL,
                    action TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_entity
                    ON audit_log(entity_id, id);
                CREATE TABLE IF NOT EXISTS idempotency (
                    actor_id TEXT NOT NULL,
                    idem_key TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(actor_id, idem_key)
                );
                CREATE TABLE IF NOT EXISTS certificate_revisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    certificate_no TEXT NOT NULL,
                    port TEXT NOT NULL,
                    facility_id TEXT,
                    base_revision_id INTEGER,
                    changes TEXT NOT NULL,
                    submitted_at TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_revisions_certificate
                    ON certificate_revisions(certificate_no, id);
                CREATE TABLE IF NOT EXISTS certificate_conflicts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    certificate_no TEXT NOT NULL,
                    left_revision_id INTEGER NOT NULL,
                    right_revision_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(certificate_no, left_revision_id, right_revision_id)
                );
                CREATE TABLE IF NOT EXISTS backfill_failures (
                    code TEXT PRIMARY KEY,
                    certificate_no TEXT,
                    reason TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
            """)

    @staticmethod
    def _entity_from_row(row):
        return {
            "id": row["id"],
            "kind": row["kind"],
            "status": row["status"],
            "version": int(row["version"]),
            "data": json.loads(row["data"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def create_entity(self, entity_id, kind, status, data, actor_id):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
                (entity_id, kind, status, payload, actor_id, now, now),
            )
        return self.get_entity(entity_id)

    def get_entity(self, entity_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
        return self._entity_from_row(row) if row else None

    def list_entities(self, kind=None, status=None):
        clauses = []
        params = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM entities" + where + " ORDER BY created_at, id", params
            ).fetchall()
        return [self._entity_from_row(row) for row in rows]

    def find_entities(self, kind, field, value):
        return [
            entity
            for entity in self.list_entities(kind=kind)
            if (entity["id"] == value if field == "id" else entity["data"].get(field) == value)
        ]

    def update_entity(self, entity_id, expected_version, status, data):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT version FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("entity not found: " + entity_id)
            current_version = int(row["version"])
            if expected_version is not None and current_version != int(expected_version):
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected_version, current_version)
                )
            connection.execute(
                "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
                "WHERE id = ? AND version = ?",
                (status, payload, now, entity_id, current_version),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(entity_id)

    def append_audit(self, entity_id, actor_id, actor_role, action, from_status, to_status, detail):
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status, to_status, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entity_id,
                    actor_id,
                    actor_role,
                    action,
                    from_status,
                    to_status,
                    json.dumps(detail, ensure_ascii=False, sort_keys=True),
                    utcnow(),
                ),
            )

    def list_audit(self, entity_id=None):
        with self._connect() as connection:
            if entity_id:
                rows = connection.execute(
                    "SELECT * FROM audit_log WHERE entity_id = ? ORDER BY id", (entity_id,)
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        return [
            {
                "id": row["id"],
                "entity_id": row["entity_id"],
                "actor_id": row["actor_id"],
                "actor_role": row["actor_role"],
                "action": row["action"],
                "from_status": row["from_status"],
                "to_status": row["to_status"],
                "detail": json.loads(row["detail"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def get_idempotency(self, actor_id, idem_key):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT entity_id FROM idempotency WHERE actor_id = ? AND idem_key = ?",
                (actor_id, idem_key),
            ).fetchone()
        return row["entity_id"] if row else None

    def save_idempotency(self, actor_id, idem_key, entity_id):
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO idempotency(actor_id, idem_key, entity_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (actor_id, idem_key, entity_id, utcnow()),
            )

    # ---- 凭证多版本与冲突 -------------------------------------------------

    @staticmethod
    def _revision_from_row(row):
        return {
            "id": row["id"],
            "certificate_no": row["certificate_no"],
            "port": row["port"],
            "facility_id": row["facility_id"],
            "base_revision_id": row["base_revision_id"],
            "changes": json.loads(row["changes"]),
            "submitted_at": row["submitted_at"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
        }

    def add_certificate_revision(
        self, certificate_no, port, facility_id, base_revision_id, changes, submitted_at, created_by
    ):
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO certificate_revisions"
                "(certificate_no, port, facility_id, base_revision_id, changes, submitted_at, created_by, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    certificate_no,
                    port,
                    facility_id,
                    base_revision_id,
                    json.dumps(changes, ensure_ascii=False, sort_keys=True),
                    submitted_at,
                    created_by,
                    utcnow(),
                ),
            )
            revision_id = cursor.lastrowid
        return self.get_certificate_revision(revision_id)

    def get_certificate_revision(self, revision_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM certificate_revisions WHERE id = ?", (revision_id,)
            ).fetchone()
        return self._revision_from_row(row) if row else None

    def list_certificate_revisions(self, certificate_no=None):
        with self._connect() as connection:
            if certificate_no:
                rows = connection.execute(
                    "SELECT * FROM certificate_revisions WHERE certificate_no = ? ORDER BY id",
                    (certificate_no,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM certificate_revisions ORDER BY certificate_no, id"
                ).fetchall()
        return [self._revision_from_row(row) for row in rows]

    def add_certificate_conflict(self, certificate_no, left_revision_id, right_revision_id):
        first, second = sorted((left_revision_id, right_revision_id))
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO certificate_conflicts"
                "(certificate_no, left_revision_id, right_revision_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (certificate_no, first, second, utcnow()),
            )

    def _conflict_from_row(self, row, revisions):
        ordered = sorted(
            (revisions[row["left_revision_id"]], revisions[row["right_revision_id"]]),
            key=lambda item: (item["submitted_at"], item.get("facility_id") or ""),
        )
        return {
            "id": row["id"],
            "certificate_no": row["certificate_no"],
            # 按提交时间、再按种植点排列的两版
            "versions": ordered,
            "created_at": row["created_at"],
        }

    def list_certificate_conflicts(self, certificate_no=None):
        with self._connect() as connection:
            if certificate_no:
                rows = connection.execute(
                    "SELECT * FROM certificate_conflicts WHERE certificate_no = ? ORDER BY id",
                    (certificate_no,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM certificate_conflicts ORDER BY certificate_no, id"
                ).fetchall()
        revisions = {
            item["id"]: item
            for item in self.list_certificate_revisions(certificate_no)
        }
        return [self._conflict_from_row(row, revisions) for row in rows]

    # ---- 旧数据凭证号回填失败记录 -----------------------------------------

    def record_backfill_failure(self, code, certificate_no, reason):
        now = utcnow()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO backfill_failures(code, certificate_no, reason, attempts, status, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, 'pending', ?, ?) "
                "ON CONFLICT(code) DO UPDATE SET "
                "certificate_no = excluded.certificate_no, reason = excluded.reason, "
                "attempts = attempts + 1, status = 'pending', updated_at = excluded.updated_at",
                (code, certificate_no, reason, now, now),
            )

    def resolve_backfill_failure(self, code):
        with self._connect() as connection:
            connection.execute(
                "UPDATE backfill_failures SET status = 'resolved', updated_at = ? WHERE code = ?",
                (utcnow(), code),
            )

    @staticmethod
    def _backfill_failure_from_row(row):
        return {
            "code": row["code"],
            "certificate_no": row["certificate_no"],
            "reason": row["reason"],
            "attempts": int(row["attempts"]),
            "status": row["status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def list_backfill_failures(self, status=None):
        with self._connect() as connection:
            if status:
                rows = connection.execute(
                    "SELECT * FROM backfill_failures WHERE status = ? ORDER BY code",
                    (status,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM backfill_failures ORDER BY status, code"
                ).fetchall()
        return [self._backfill_failure_from_row(row) for row in rows]

    def get_backfill_failure(self, code):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM backfill_failures WHERE code = ?", (code,)
            ).fetchone()
        return self._backfill_failure_from_row(row) if row else None

    def ping(self):
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return True
