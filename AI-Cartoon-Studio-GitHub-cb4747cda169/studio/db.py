import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


def encode(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Store:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "studio.sqlite3"
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL,
                    executor TEXT, priority INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    created REAL NOT NULL, available REAL NOT NULL, lease_until REAL,
                    lease_token TEXT, payload TEXT NOT NULL, result TEXT, error TEXT,
                    dedupe TEXT UNIQUE NOT NULL, revision INTEGER NOT NULL DEFAULT 1
                );
                CREATE INDEX IF NOT EXISTS task_ready ON tasks(status,kind,available,priority);
                CREATE TABLE IF NOT EXISTS objects (
                    category TEXT, id TEXT, revision INTEGER, body TEXT, updated REAL,
                    PRIMARY KEY(category,id)
                );
                CREATE TABLE IF NOT EXISTS history (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, category TEXT, id TEXT,
                    revision INTEGER, body TEXT, created REAL
                );
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, body TEXT, created REAL
                );
                CREATE TABLE IF NOT EXISTS archive (
                    season_title TEXT NOT NULL, episode_title TEXT NOT NULL,
                    full_script TEXT NOT NULL, PRIMARY KEY(season_title,episode_title)
                );
                CREATE TABLE IF NOT EXISTS quota (
                    provider TEXT, day TEXT, used INTEGER, PRIMARY KEY(provider,day)
                );
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def event(self, kind: str, body: dict):
        with self.connect() as db:
            db.execute("INSERT INTO events(kind,body,created) VALUES(?,?,?)", (kind, encode(body), time.time()))

    def put(self, category: str, id: str, body: dict, expected: int | None = None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM objects WHERE category=? AND id=?", (category, id)).fetchone()
            revision = old["revision"] if old else 0
            if expected is not None and revision != expected:
                raise ValueError("Объект уже изменён; обновите данные")
            now = time.time()
            db.execute("INSERT INTO history(category,id,revision,body,created) VALUES(?,?,?,?,?)",
                       (category, id, revision + 1, encode(body), now))
            db.execute("INSERT INTO objects VALUES(?,?,?,?,?) ON CONFLICT(category,id) DO UPDATE SET revision=excluded.revision,body=excluded.body,updated=excluded.updated",
                       (category, id, revision + 1, encode(body), now))
        return revision + 1

    def get(self, category: str, id: str, default=None):
        with self.connect() as db:
            row = db.execute("SELECT * FROM objects WHERE category=? AND id=?", (category, id)).fetchone()
        return json.loads(row["body"]) if row else default

    def objects(self, category: str):
        with self.connect() as db:
            rows = db.execute("SELECT id,revision,body FROM objects WHERE category=? ORDER BY updated DESC", (category,)).fetchall()
        return [{"id": r["id"], "revision": r["revision"], "body": json.loads(r["body"])} for r in rows]

    def restore(self, category: str, id: str, revision: int):
        with self.connect() as db:
            row = db.execute("SELECT body FROM history WHERE category=? AND id=? AND revision=?", (category, id, revision)).fetchone()
        if not row:
            raise ValueError("Версия не найдена")
        return self.put(category, id, json.loads(row["body"]))

    def enqueue(self, kind: str, payload: dict, dedupe: str, priority=0):
        id = uuid.uuid4().hex
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO tasks(id,kind,status,priority,created,available,payload,dedupe) VALUES(?,?,'pending',?,?,?,?,?)",
                       (id, kind, priority, time.time(), time.time(), encode(payload), dedupe))
            return db.execute("SELECT id FROM tasks WHERE dedupe=?", (dedupe,)).fetchone()[0]

    def claim(self, kinds: list[str], executor: str, ttl=120, require_render_notice=False):
        if not kinds:
            return None
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            # Просроченный lease нельзя использовать повторно: старый Worker теряет право записи.
            db.execute("UPDATE tasks SET status=CASE WHEN attempts>=3 THEN 'failed' ELSE 'pending' END, executor=NULL,lease_token=NULL,lease_until=NULL WHERE status='running' AND lease_until<?", (now,))
            marks = ",".join("?" for _ in kinds)
            notice = " AND (kind!='render' OR EXISTS (SELECT 1 FROM objects o WHERE o.category='render_warning' AND o.id=tasks.id AND json_extract(o.body,'$.status')='sent' AND json_extract(o.body,'$.ready_at')<=?))" if require_render_notice else ""
            row = db.execute(f"SELECT * FROM tasks WHERE status='pending' AND attempts<3 AND available<=? AND kind IN ({marks}){notice} ORDER BY priority DESC,created LIMIT 1", (now, *kinds, *([now] if require_render_notice else []))).fetchone()
            if row is None:
                return None
            token = uuid.uuid4().hex
            db.execute("UPDATE tasks SET status='running',executor=?,attempts=attempts+1,lease_until=?,lease_token=? WHERE id=?", (executor, now + ttl, token, row["id"]))
            result = dict(db.execute("SELECT * FROM tasks WHERE id=?", (row["id"],)).fetchone())
        result["payload"] = json.loads(result["payload"])
        return result

    def heartbeat(self, id: str, token: str, ttl=120):
        with self.connect() as db:
            count = db.execute("UPDATE tasks SET lease_until=? WHERE id=? AND lease_token=? AND status='running' AND lease_until>?", (time.time()+ttl, id, token, time.time())).rowcount
        if not count:
            raise ValueError("Аренда задачи истекла или передана другому исполнителю")

    def finish(self, id: str, token: str, result=None, error=None, wait=False):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM tasks WHERE id=? AND lease_token=? AND status='running' AND lease_until>?", (id, token, time.time())).fetchone()
            if not row:
                raise ValueError("Результат устаревшего исполнителя отклонён")
            status = "pending" if wait else ("failed" if error and row["attempts"] >= 3 else "pending" if error else "done")
            db.execute("UPDATE tasks SET status=?,result=?,error=?,available=?,lease_until=NULL,lease_token=NULL,attempts=attempts-? WHERE id=?",
                       (status, encode(result) if result is not None else None, error, time.time() + (3600 if wait else 30 if error else 0), int(wait), id))
        self.event("task."+status, {"id": id, "error": error})

    def tasks(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT id,kind,status,executor,priority,attempts,created,error FROM tasks ORDER BY created DESC LIMIT 200")]

    def task(self, id: str):
        with self.connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (id,)).fetchone()
        return dict(row) if row else None

    def quota(self, provider: str, day: str, limit: int):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO quota VALUES(?,?,0)", (provider, day))
            used = db.execute("SELECT used FROM quota WHERE provider=? AND day=?", (provider, day)).fetchone()[0]
            if used >= limit:
                return False
            db.execute("UPDATE quota SET used=used+1 WHERE provider=? AND day=?", (provider, day))
        return True

    def backup(self):
        folder = self.root / "backups"
        folder.mkdir(exist_ok=True)
        path = folder / f"studio-{time.time_ns()}.sqlite3"
        with self.connect() as source, sqlite3.connect(path) as target:
            source.backup(target)
        return path
