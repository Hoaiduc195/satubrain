"""SQLite storage, scoped full-text retrieval and idempotent snapshot merging."""

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import unicodedata
from uuid import uuid4

from .privacy import check_secrets
from .scope import label

KINDS = ("note", "decision", "handoff", "message")
ROLES = ("user", "assistant")
MAX_TEXT = 24_000
MAX_QUERY = 400
MAX_ENTRIES = 250_000
MAX_SNAPSHOT = 64 * 1024 * 1024
MAX_BODY = MAX_TEXT * 4 + 1024
HEX = re.compile(r"[a-f0-9]{64}")
DEVICE = re.compile(r"[a-f0-9]{32}")
WORDS = re.compile(r"[^\W_]+", re.UNICODE)
FIELDS = ("id", "project", "task", "branch", "kind", "text", "agent", "session",
          "position", "role", "created_at")
SCHEMA = """
CREATE TABLE IF NOT EXISTS memory (
    id TEXT NOT NULL UNIQUE, project TEXT NOT NULL, task TEXT NOT NULL, branch TEXT NOT NULL,
    kind TEXT NOT NULL, text TEXT NOT NULL, agent TEXT NOT NULL, session TEXT NOT NULL,
    position INTEGER NOT NULL, role TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS memory_scope
    ON memory(project, task, branch, kind, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS memory_recent
    ON memory(project, task, branch, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS memory_timeline
    ON memory(project, task, branch, session, kind, position, id);
CREATE TABLE IF NOT EXISTS tombstone (id TEXT NOT NULL PRIMARY KEY);
CREATE TABLE IF NOT EXISTS metadata (key TEXT NOT NULL PRIMARY KEY, value TEXT NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    text, content='memory', content_rowid='rowid', tokenize='unicode61'
);

"""


def default_db():
    if path := os.environ.get("SATUBRAIN_DB"):
        return Path(path).expanduser()
    return Path.home() / ".satubrain" / "memory.sqlite3"


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def memory_id(row):
    text = row["text"]
    if row["kind"] in {"note", "decision"}:
        text = " ".join(unicodedata.normalize("NFKC", text).casefold().split())
    key = [row[field] for field in ("project", "task", "branch", "kind")]
    if row["kind"] == "handoff":
        key.extend(row[field] for field in ("agent", "session"))
    elif row["kind"] == "message":
        key.extend(row[field] for field in ("session", "position"))
        text = ""
    return hashlib.sha256(json_bytes([*key, text])).hexdigest()


def record(*, project, task, branch, kind, text, agent="human", session="",
           position=0, role="", created_at=None):
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT or "\x00" in text:
        raise ValueError(f"memory must contain 1–{MAX_TEXT} characters without null bytes")
    if kind not in KINDS:
        raise ValueError("unknown memory kind")
    if type(position) is not int or not 0 <= position <= 2**31 - 1:
        raise ValueError("position must be a non-negative 32-bit integer")
    if kind == "message":
        if not session or role not in ROLES:
            raise ValueError("messages require a session ID and a user or assistant role")
    elif position or role:
        raise ValueError("position and role apply to messages only")
    created_at = utc_now() if created_at is None else created_at
    if not isinstance(created_at, str) or len(created_at) > 40:
        raise ValueError("invalid timestamp")
    try:
        parsed = datetime.fromisoformat(created_at)
        if parsed.utcoffset() != timezone.utc.utcoffset(None):
            raise ValueError
    except ValueError:
        raise ValueError("expected an ISO 8601 timestamp in UTC") from None
    row = {
        "project": label(project, "project"), "task": label(task, "task"),
        "branch": label(branch, "branch"), "kind": kind, "text": text.strip(),
        "agent": label(agent, "agent"), "session": label(session, "session") if session else "",
        "position": position, "role": role, "created_at": parsed.isoformat(timespec="microseconds"),
    }
    check_secrets(*(row[field] for field in ("project", "task", "branch", "text", "agent", "session")))
    return {"id": memory_id(row), **row}


def validate_snapshot(payload):
    if not isinstance(payload, dict) or type(payload.get("version")) is not int or payload["version"] != 1:
        raise ValueError("unsupported snapshot format")
    entries, tombstones = payload.get("entries"), payload.get("tombstones")
    if not isinstance(entries, list) or not isinstance(tombstones, list):
        raise ValueError("snapshot requires entries and tombstones arrays")
    if len(entries) + len(tombstones) > MAX_ENTRIES:
        raise ValueError("snapshot exceeds the entry limit")
    ids = set()
    validated = []
    for row in entries:
        if not isinstance(row, dict) or set(row) != set(FIELDS):
            raise ValueError("invalid memory record")
        if not isinstance(row["created_at"], str):
            raise ValueError("snapshot record requires a timestamp")
        entry = record(**{key: row[key] for key in FIELDS if key != "id"})
        if entry["id"] != row["id"] or entry["id"] in ids:
            raise ValueError("snapshot contains a modified or repeated record ID")
        ids.add(entry["id"])
        validated.append(entry)
    for identifier in tombstones:
        if not isinstance(identifier, str) or not HEX.fullmatch(identifier) or identifier in ids:
            raise ValueError("snapshot contains an invalid or repeated tombstone")
        ids.add(identifier)
    return validated, tombstones


class Store:
    def __init__(self, path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        # Pre-create with owner-only permissions. SQLite uses its mode for WAL sidecars.
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA trusted_schema = OFF")
            db.execute("PRAGMA secure_delete = ON")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                db.execute("PRAGMA journal_mode = WAL")
                db.executescript(f"BEGIN IMMEDIATE; {SCHEMA} PRAGMA user_version = 1; COMMIT;")
            elif version != 1:
                raise ValueError("unsupported database version")
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self, write=True):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @staticmethod
    def _remove(db, identifier):
        row = db.execute("SELECT rowid, text FROM memory WHERE id = ?", (identifier,)).fetchone()
        if row is None:
            return
        db.execute("INSERT INTO memory_fts(memory_fts, rowid, text) VALUES ('delete', ?, ?)",
                   (row["rowid"], row["text"]))
        db.execute("DELETE FROM memory WHERE id = ?", (identifier,))

    @staticmethod
    def _put(db, rows):
        columns = ", ".join(FIELDS)
        placeholders = ", ".join("?" for _ in FIELDS)
        inserted = 0
        for row in rows:
            values = tuple(row[key] for key in FIELDS)
            if db.execute("SELECT 1 FROM tombstone WHERE id = ?", (row["id"],)).fetchone():
                continue
            # Stable earliest record wins on all devices, including independent duplicates.
            existing = db.execute("SELECT * FROM memory WHERE id = ?", (row["id"],)).fetchone()
            if existing:
                incoming = (row["created_at"], json_bytes(row))
                current = (existing["created_at"], json_bytes(dict(existing)))
                if incoming >= current:
                    continue
                Store._remove(db, row["id"])
            inserted_row = db.execute(f"INSERT INTO memory ({columns}) VALUES ({placeholders})", values)
            db.execute("INSERT INTO memory_fts(rowid, text) VALUES (?, ?)",
                       (inserted_row.lastrowid, row["text"]))
            inserted += int(existing is None)
        return inserted

    def save(self, **fields):
        entry = record(**fields)
        with self.transaction() as db:
            added = self._put(db, [entry])
            deleted = db.execute("SELECT 1 FROM tombstone WHERE id = ?", (entry["id"],)).fetchone()
        return {"id": entry["id"], "status": "saved" if added else "deleted" if deleted else "duplicate"}

    def save_batch(self, entries):
        rows = [record(**entry) for entry in entries]
        with self.transaction() as db:
            return self._put(db, rows)

    def delete(self, project, task, branch, identifier):
        if not isinstance(identifier, str) or not HEX.fullmatch(identifier):
            raise ValueError("invalid memory ID")
        with self.transaction() as db:
            exists = db.execute(
                "SELECT 1 FROM memory WHERE project = ? AND task = ? AND branch = ? AND id = ?",
                (project, task, branch, identifier),
            ).fetchone()
            if exists:
                self._remove(db, identifier)
                db.execute("INSERT INTO tombstone VALUES (?)", (identifier,))
        return bool(exists)

    @staticmethod
    def _search(db, project, task, branch, query, limit, kind):
        if not isinstance(query, str) or len(query) > MAX_QUERY:
            raise ValueError(f"query must contain at most {MAX_QUERY} characters")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if kind is not None and kind not in KINDS:
            raise ValueError("unknown memory kind")
        args = [project, task, branch]
        conditions = "m.project = ? AND m.task = ? AND m.branch = ?"
        if kind:
            conditions += " AND m.kind = ?"
            args.append(kind)
        tokens = list(dict.fromkeys(WORDS.findall(query.casefold())))[:16]
        if tokens:
            match = " OR ".join(f'"{word}"' for word in tokens)
            sql = f"""SELECT m.* FROM memory_fts JOIN memory m ON m.rowid = memory_fts.rowid
                      WHERE memory_fts MATCH ? AND {conditions}
                      ORDER BY bm25(memory_fts), m.created_at DESC, m.id DESC LIMIT ?"""
            args.insert(0, match)
        elif query.strip():
            return []
        else:
            sql = f"""SELECT m.* FROM memory m WHERE {conditions}
                      ORDER BY m.created_at DESC, m.id DESC LIMIT ?"""
        return [dict(row) for row in db.execute(sql, (*args, limit))]

    def search(self, project, task, branch, query="", limit=20, kind=None):
        with self.connect() as db:
            return self._search(db, project, task, branch, query, limit, kind)

    def history(self, project, task, branch, session, after=-1, limit=100):
        if type(after) is not int or not -1 <= after <= 2**31 - 1:
            raise ValueError("invalid history cursor")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                """SELECT * FROM memory WHERE project = ? AND task = ? AND branch = ?
                   AND session = ? AND kind = 'message' AND position > ?
                   ORDER BY position, id LIMIT ?""",
                (project, task, branch, label(session, "session"), after, limit),
            )]

    def get(self, project, task, branch, identifier):
        if not isinstance(identifier, str) or not HEX.fullmatch(identifier):
            raise ValueError("invalid memory ID")
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM memory WHERE project = ? AND task = ? AND branch = ? AND id = ?",
                (project, task, branch, identifier),
            ).fetchone()
        return dict(row) if row else None

    def context(self, project, task, branch, query="", budget=1600):
        if type(budget) is not int or not 64 <= budget <= 32000:
            raise ValueError("budget must be between 64 and 32000")
        with self.transaction(write=False) as db:
            latest = self._search(db, project, task, branch, "", 1, "handoff")
            relevant = self._search(db, project, task, branch, query, 30, None)
        seen = set()
        items = []
        used = 2  # JSON array delimiters.
        for row in latest + relevant:
            if row["id"] in seen:
                continue
            seen.add(row["id"])
            item = {"kind": row["kind"], "agent": row["agent"], "text": row["text"]}
            remaining = budget - used - int(bool(items))
            if remaining < 48:
                break
            # Keep an oversized checkpoint from consuming the entire retrieval budget.
            allocation = min(remaining, max(64, budget // 2))
            if len(json_bytes(item)) > allocation:
                low, high = 0, len(item["text"])
                while low < high:
                    middle = (low + high + 1) // 2
                    candidate = {**item, "text": item["text"][:middle] + "…"}
                    if len(json_bytes(candidate)) <= allocation:
                        low = middle
                    else:
                        high = middle - 1
                if low == 0:
                    continue
                item["text"] = item["text"][:low] + "…"
            used += len(json_bytes(item)) + int(bool(items))
            items.append(item)
        return {"items": items, "context_bytes": used, "budget": budget}

    def scopes(self, project=None):
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                """SELECT project, task, branch, count(*) AS entries, max(created_at) AS updated
                   FROM memory WHERE (? IS NULL OR project = ?) GROUP BY project, task, branch
                   ORDER BY updated DESC, project, task, branch LIMIT 1000""", (project, project)
            )]

    @staticmethod
    def snapshot(db):
        rows = []
        size = 64
        for row in db.execute("SELECT * FROM memory ORDER BY id"):
            entry = dict(row)
            size += len(json_bytes(entry)) + 1
            if size > MAX_SNAPSHOT or len(rows) >= MAX_ENTRIES:
                raise ValueError("database exceeds the snapshot limit")
            rows.append(entry)
        tombstones = []
        for row in db.execute("SELECT id FROM tombstone ORDER BY id"):
            size += 67
            if size > MAX_SNAPSHOT or len(rows) + len(tombstones) >= MAX_ENTRIES:
                raise ValueError("database exceeds the snapshot limit")
            tombstones.append(row[0])
        return {"version": 1, "entries": rows, "tombstones": tombstones}

    def export(self):
        with self.transaction(write=False) as db:
            return self.snapshot(db)

    @staticmethod
    def _merge(db, entries, tombstones):
        db.executemany("INSERT OR IGNORE INTO tombstone VALUES (?)", ((key,) for key in tombstones))
        for identifier in tombstones:
            Store._remove(db, identifier)
        return Store._put(db, entries)

    def merge(self, payload):
        entries, tombstones = validate_snapshot(payload)
        with self.transaction() as db:
            added = self._merge(db, entries, tombstones)
        return {"added": added, "tombstones": len(tombstones)}

    @staticmethod
    def device(db):
        db.execute("INSERT OR IGNORE INTO metadata VALUES ('device', ?)", (uuid4().hex,))
        identifier = db.execute("SELECT value FROM metadata WHERE key = 'device'").fetchone()[0]
        if not DEVICE.fullmatch(identifier):
            raise ValueError("invalid device ID in database")
        return identifier
