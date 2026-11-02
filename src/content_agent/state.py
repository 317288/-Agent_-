import json
import sqlite3
from pathlib import Path

from .schemas import dumps


class Store:
    """Single-process snapshots; plan + completed state commit together."""
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path))
        self.db.execute("CREATE TABLE IF NOT EXISTS tasks (task_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, snapshot TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS plans (task_id TEXT PRIMARY KEY, plan_id TEXT UNIQUE NOT NULL, document TEXT NOT NULL)")
        self.db.commit()

    def get(self, task_id):
        row = self.db.execute("SELECT snapshot FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def create(self, state):
        with self.db:
            self.db.execute("INSERT INTO tasks VALUES (?,?,?)", (state["task"]["task_id"], state["fingerprint"], dumps(state)))

    def persist(self, state, plan=None):
        with self.db:
            if plan is not None:
                self.db.execute("INSERT INTO plans VALUES (?,?,?)", (state["task"]["task_id"], plan["saved_plan_id"], dumps(plan)))
            self.db.execute("UPDATE tasks SET snapshot=? WHERE task_id=?", (dumps(state), state["task"]["task_id"]))

    def get_plan(self, task_id):
        row = self.db.execute("SELECT document FROM plans WHERE task_id=?", (task_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def close(self):
        self.db.close()
