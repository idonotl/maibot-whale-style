"""表达学习的数据层：素材表与规则表。"""

from __future__ import annotations

from contextlib import contextmanager

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List

SCENES = (
    "被夸",
    "被逗",
    "安慰",
    "日常分享",
    "表观点",
    "被冷落",
    "求帮忙",
    "被关心",
    "吐槽",
    "自嘲",
    "接梗",
)

DDL = """
CREATE TABLE IF NOT EXISTS samples(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bvid TEXT DEFAULT '',
    source TEXT DEFAULT '',
    scene TEXT DEFAULT '',
    excerpt TEXT DEFAULT '',
    weight REAL DEFAULT 1.0,
    created_utc REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS rules(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scene TEXT NOT NULL,
    behavior TEXT NOT NULL,
    example TEXT DEFAULT '',
    confidence REAL DEFAULT 0.4,
    weight REAL DEFAULT 1.0,
    uses INTEGER DEFAULT 0,
    source_bvid TEXT DEFAULT '',
    source TEXT DEFAULT '',
    status TEXT DEFAULT 'active',
    created_utc REAL NOT NULL,
    updated_utc REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_rules ON rules(scene, behavior);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""


class StyleStore:
    """SQLite 仓储，负责素材、规则与统计。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(DDL)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def reserve_auto(self, source: str, daily: int, cooldown_minutes: int, now: float | None = None) -> bool:
        now=time.time() if now is None else now
        day=time.strftime('%Y-%m-%d',time.localtime(now))
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            key='auto:'+source
            row=db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
            prior=json.loads(row['value']) if row else {}
            used=int(prior.get('used',0)) if prior.get('day')==day else 0
            if used>=daily or now-float(prior.get('last',0))<cooldown_minutes*60:
                return False
            value=json.dumps(dict(day=day,used=used+1,last=now))
            db.execute('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)',(key,value))
            return True

    def add_sample(self, bvid: str, source: str, scene: str, excerpt: str, weight: float) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO samples(bvid,source,scene,excerpt,weight,created_utc) VALUES (?,?,?,?,?,?)",
                (bvid, source, scene, excerpt[:400], weight, time.time()),
            )

    def upsert_rule(
        self,
        scene: str,
        behavior: str,
        example: str,
        confidence: float,
        weight: float,
        source_bvid: str,
        source: str,
    ) -> int:
        now = time.time()
        with self.connect() as db:
            row = db.execute(
                "SELECT id, confidence FROM rules WHERE scene=? AND behavior=?", (scene, behavior)
            ).fetchone()
            if row:
                merged = min(1.0, max(row["confidence"], confidence) + 0.05)
                db.execute(
                    "UPDATE rules SET confidence=?, example=?, updated_utc=? WHERE id=?",
                    (merged, example[:200], now, row["id"]),
                )
                return int(row["id"])
            cursor = db.execute(
                "INSERT INTO rules(scene,behavior,example,confidence,weight,source_bvid,source,"
                "created_utc,updated_utc) VALUES (?,?,?,?,?,?,?,?,?)",
                (scene, behavior, example[:200], confidence, weight, source_bvid, source, now, now),
            )
            return int(cursor.lastrowid)

    def rules_for_scene(self, scene: str, limit: int, min_confidence: float) -> List[Dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM rules WHERE status='active' AND scene=? AND confidence>=? "
                "ORDER BY confidence DESC, uses ASC LIMIT ?",
                (scene, min_confidence, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def top_rules(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM rules WHERE status='active' ORDER BY confidence DESC, updated_utc DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_used(self, ids: List[int]) -> None:
        if not ids:
            return
        with self.connect() as db:
            db.executemany("UPDATE rules SET uses=uses+1, updated_utc=? WHERE id=?", [(time.time(), i) for i in ids])

    def stats(self) -> Dict[str, Any]:
        with self.connect() as db:
            rules = db.execute("SELECT COUNT(*) FROM rules WHERE status='active'").fetchone()[0]
            samples = db.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
            used = db.execute("SELECT COALESCE(SUM(uses),0) FROM rules").fetchone()[0]
            last = db.execute("SELECT COALESCE(MAX(updated_utc),0) FROM rules").fetchone()[0]
        return {"rules": rules, "samples": samples, "uses": used, "last_update": last}

    def learned_rules(self, limit: int = 50) -> List[Dict[str, Any]]:
        return self.top_rules(limit)

    def dump_json(self) -> str:
        return json.dumps(self.stats(), ensure_ascii=False)
