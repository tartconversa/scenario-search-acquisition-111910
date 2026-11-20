"""SQLite 持久层。

设计原则：
1. 来源原文（raw_response）与归并身份（requirement / observation）分表，
   原文不可变，归并结果可重算；
2. 每条需求的每次出现都有 observation 指向具体 query_run，
   可追查“通过哪些查询观察到”；
3. 限速时间、查询版本、覆盖判定都落盘，中断恢复只认真实版本与覆盖依据，
   不记裸页码；
4. 项目经理保存的筛选名单以快照表单独保存，永不被后续轮次改写。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS run(
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    trigger TEXT NOT NULL,
    note TEXT
);

CREATE TABLE IF NOT EXISTS unit_run(
    run_id TEXT NOT NULL,
    unit TEXT NOT NULL,
    planned_version INTEGER,
    observed_version INTEGER,
    verdict TEXT NOT NULL,
    reason TEXT,
    blocked_until REAL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    PRIMARY KEY(run_id, unit)
);

CREATE TABLE IF NOT EXISTS query_run(
    query_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    unit TEXT NOT NULL,
    industry TEXT,
    year INTEGER,
    page INTEGER NOT NULL,
    version INTEGER NOT NULL,
    total INTEGER,
    cap INTEGER,
    truncated INTEGER,
    next_page INTEGER,
    status TEXT NOT NULL,
    scope_status TEXT,
    error_code TEXT,
    retry_after REAL,
    response_digest TEXT,
    observed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_response(
    response_digest TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    body TEXT NOT NULL,
    first_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scope_coverage(
    run_id TEXT NOT NULL,
    unit TEXT NOT NULL,
    industry TEXT,
    year INTEGER,
    total INTEGER NOT NULL,
    observed_count INTEGER NOT NULL,
    complete INTEGER NOT NULL,
    gap_count INTEGER NOT NULL,
    pages_fetched INTEGER NOT NULL,
    truncated INTEGER NOT NULL,
    basis TEXT NOT NULL,
    PRIMARY KEY(run_id, unit, industry, year)
);

CREATE TABLE IF NOT EXISTS requirement(
    unit TEXT NOT NULL,
    id TEXT NOT NULL,
    first_seen_run TEXT NOT NULL,
    latest_run TEXT NOT NULL,
    latest_revision INTEGER,
    title TEXT,
    industries_json TEXT NOT NULL,
    year INTEGER,
    status TEXT NOT NULL,
    state_reason TEXT,
    PRIMARY KEY(unit, id)
);

CREATE TABLE IF NOT EXISTS observation(
    unit TEXT NOT NULL,
    id TEXT NOT NULL,
    query_id INTEGER NOT NULL,
    run_id TEXT NOT NULL,
    response_digest TEXT NOT NULL,
    observed_revision INTEGER NOT NULL,
    observed_industries_json TEXT NOT NULL,
    observed_year INTEGER,
    observed_status TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    PRIMARY KEY(unit, id, query_id),
    FOREIGN KEY(query_id) REFERENCES query_run(query_id)
);

CREATE TABLE IF NOT EXISTS requirement_change(
    change_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    unit TEXT NOT NULL,
    id TEXT NOT NULL,
    change TEXT NOT NULL,
    detail TEXT NOT NULL,
    at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS selection_snapshot(
    unit TEXT NOT NULL,
    id TEXT NOT NULL,
    observed_revision INTEGER NOT NULL,
    saved_at TEXT NOT NULL,
    PRIMARY KEY(unit, id)
);

CREATE TABLE IF NOT EXISTS list_snapshot(
    run_id TEXT NOT NULL,
    unit TEXT NOT NULL,
    id TEXT NOT NULL,
    state TEXT NOT NULL,
    contactable INTEGER NOT NULL,
    detail TEXT NOT NULL,
    PRIMARY KEY(run_id, unit, id)
);

CREATE TABLE IF NOT EXISTS kv(
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_query_run_unit ON query_run(run_id, unit);
CREATE INDEX IF NOT EXISTS idx_observation_req ON observation(unit, id);
CREATE INDEX IF NOT EXISTS idx_observation_run ON observation(run_id);
CREATE INDEX IF NOT EXISTS idx_change_req ON requirement_change(unit, id);
"""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # ---- 运行 ----------------------------------------------------------
    def start_run(self, run_id: str, trigger: str, note: Optional[str] = None) -> None:
        self.conn.execute(
            "INSERT INTO run(run_id, started_at, trigger, note) VALUES(?,?,?,?)",
            (run_id, utc_now_iso(), trigger, note),
        )

    def finish_run(self, run_id: str) -> None:
        self.conn.execute("UPDATE run SET finished_at=? WHERE run_id=?", (utc_now_iso(), run_id))

    def start_unit_run(self, run_id: str, unit: str, planned_version: Optional[int]) -> None:
        self.conn.execute(
            "INSERT INTO unit_run(run_id, unit, planned_version, verdict, started_at) "
            "VALUES(?,?,?,'running',?)",
            (run_id, unit, planned_version, utc_now_iso()),
        )

    def finish_unit_run(
        self,
        run_id: str,
        unit: str,
        verdict: str,
        reason: Optional[str],
        observed_version: Optional[int],
        blocked_until: Optional[float] = None,
    ) -> None:
        self.conn.execute(
            "UPDATE unit_run SET verdict=?, reason=?, observed_version=?, blocked_until=?, finished_at=? "
            "WHERE run_id=? AND unit=?",
            (verdict, reason, observed_version, blocked_until, utc_now_iso(), run_id, unit),
        )

    def get_unit_run(self, run_id: str, unit: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM unit_run WHERE run_id=? AND unit=?", (run_id, unit)
        ).fetchone()

    def latest_finished_unit_run(self, unit: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM unit_run WHERE unit=? AND verdict != 'running' "
            "ORDER BY finished_at DESC LIMIT 1",
            (unit,),
        ).fetchone()

    # ---- 限速（单位级，跨进程持久化） ----------------------------------
    def set_blocked_until(self, unit: str, blocked_until: float) -> None:
        self.conn.execute(
            "INSERT INTO kv(k,v) VALUES('blocked:'||?,?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (unit, str(blocked_until)),
        )

    def get_blocked_until(self, unit: str) -> float:
        row = self.conn.execute("SELECT v FROM kv WHERE k=?", ("blocked:" + unit,)).fetchone()
        return float(row["v"]) if row else 0.0

    def clear_blocked(self, unit: str) -> None:
        self.conn.execute("DELETE FROM kv WHERE k=?", ("blocked:" + unit,))

    # ---- 原文 ----------------------------------------------------------
    def save_raw(self, url: str, status_code: int, body: bytes, digest: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO raw_response(response_digest, url, status_code, body, first_seen_at) "
            "VALUES(?,?,?,?,?)",
            (digest, url, status_code, body.decode("utf-8", "replace"), utc_now_iso()),
        )

    def save_query(
        self,
        run_id: str,
        unit: str,
        industry: Optional[str],
        year: Optional[int],
        page: int,
        version: int,
        status: str,
        *,
        total: Optional[int] = None,
        cap: Optional[int] = None,
        truncated: Optional[bool] = None,
        next_page: Optional[bool] = None,
        scope_status: Optional[str] = None,
        error_code: Optional[str] = None,
        retry_after: Optional[float] = None,
        response_digest: Optional[str] = None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO query_run(run_id, unit, industry, year, page, version, total, cap, truncated, "
            "next_page, status, scope_status, error_code, retry_after, response_digest, observed_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id, unit, industry, year, page, version, total, cap,
                None if truncated is None else int(truncated),
                None if next_page is None else int(next_page),
                status, scope_status, error_code, retry_after, response_digest, utc_now_iso(),
            ),
        )
        return int(cur.lastrowid)

    def successful_queries(self, run_id: str, unit: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM query_run WHERE run_id=? AND unit=? AND status='ok' ORDER BY industry, year, page",
            (run_id, unit),
        ))

    def save_observation(
        self,
        unit: str,
        item_id: str,
        query_id: int,
        run_id: str,
        digest: str,
        item: dict[str, Any],
    ) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO observation(unit, id, query_id, run_id, response_digest, "
            "observed_revision, observed_industries_json, observed_year, observed_status, observed_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                unit, item_id, query_id, run_id, digest,
                int(item.get("revision", 0)),
                json.dumps(item.get("industries", []), ensure_ascii=False),
                item.get("year"),
                item.get("status", "open"),
                utc_now_iso(),
            ),
        )

    # ---- 覆盖 ----------------------------------------------------------
    def save_scope_coverage(self, run_id: str, unit: str, rows: Iterable[dict[str, Any]]) -> None:
        self.conn.executemany(
            "INSERT OR REPLACE INTO scope_coverage(run_id, unit, industry, year, total, observed_count, "
            "complete, gap_count, pages_fetched, truncated, basis) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    run_id, unit, r["industry"], r["year"], r["total"], r["observed_count"],
                    int(r["complete"]), r["gap_count"], r["pages_fetched"], int(r["truncated"]), r["basis"],
                )
                for r in rows
            ],
        )

    def scope_coverage(self, run_id: str, unit: Optional[str] = None) -> list[sqlite3.Row]:
        if unit is None:
            return list(self.conn.execute(
                "SELECT * FROM scope_coverage WHERE run_id=? ORDER BY unit, industry, year", (run_id,)
            ))
        return list(self.conn.execute(
            "SELECT * FROM scope_coverage WHERE run_id=? AND unit=? ORDER BY industry, year", (run_id, unit)
        ))

    # ---- 需求归并 ------------------------------------------------------
    def get_requirement(self, unit: str, item_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM requirement WHERE unit=? AND id=?", (unit, item_id)
        ).fetchone()

    def upsert_requirement(self, run_id: str, unit: str, item_id: str, item: dict[str, Any],
                           state: str, reason: str) -> None:
        existing = self.get_requirement(unit, item_id)
        first_seen = existing["first_seen_run"] if existing else run_id
        self.conn.execute(
            "INSERT INTO requirement(unit, id, first_seen_run, latest_run, latest_revision, title, "
            "industries_json, year, status, state_reason) VALUES(?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(unit,id) DO UPDATE SET latest_run=excluded.latest_run, "
            "latest_revision=excluded.latest_revision, title=excluded.title, "
            "industries_json=excluded.industries_json, year=excluded.year, status=excluded.status, "
            "state_reason=excluded.state_reason",
            (
                unit, item_id, first_seen, run_id, int(item.get("revision", 0)),
                item.get("title"),
                json.dumps(item.get("industries", []), ensure_ascii=False),
                item.get("year"), state, reason,
            ),
        )

    def mark_requirement_state(self, unit: str, item_id: str, run_id: str, state: str, reason: str) -> None:
        self.conn.execute(
            "UPDATE requirement SET status=?, state_reason=?, latest_run=? WHERE unit=? AND id=?",
            (state, reason, run_id, unit, item_id),
        )

    def all_requirements(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM requirement ORDER BY unit, id"))

    def requirements_of_unit(self, unit: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM requirement WHERE unit=? ORDER BY id", (unit,)
        ))

    def record_change(self, run_id: str, unit: str, item_id: str, change: str, detail: str) -> None:
        self.conn.execute(
            "INSERT INTO requirement_change(run_id, unit, id, change, detail, at) VALUES(?,?,?,?,?,?)",
            (run_id, unit, item_id, change, detail, utc_now_iso()),
        )

    def changes(self, run_id: Optional[str] = None) -> list[sqlite3.Row]:
        if run_id:
            return list(self.conn.execute(
                "SELECT * FROM requirement_change WHERE run_id=? ORDER BY change_id", (run_id,)
            ))
        return list(self.conn.execute("SELECT * FROM requirement_change ORDER BY change_id"))

    def observations_of(self, unit: str, item_id: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT q.industry, q.year, q.page, q.run_id, q.version, q.response_digest, o.observed_at "
            "FROM observation o JOIN query_run q ON q.query_id=o.query_id "
            "WHERE o.unit=? AND o.id=? ORDER BY o.observed_at",
            (unit, item_id),
        ))

    # ---- 名单 ----------------------------------------------------------
    def save_selection_snapshot(self, entries: Iterable[dict[str, Any]]) -> int:
        n = 0
        for e in entries:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO selection_snapshot(unit, id, observed_revision, saved_at) "
                "VALUES(?,?,?,?)",
                (e["unit"], e["id"], int(e["observed_revision"]), utc_now_iso()),
            )
            n += cur.rowcount
        return n

    def selection_snapshot(self) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM selection_snapshot ORDER BY unit, id"
        ))

    def save_list_snapshot(self, run_id: str, rows: Iterable[dict[str, Any]]) -> None:
        self.conn.executemany(
            "INSERT OR REPLACE INTO list_snapshot(run_id, unit, id, state, contactable, detail) "
            "VALUES(?,?,?,?,?,?)",
            [
                (run_id, r["unit"], r["id"], r["state"], int(r["contactable"]), r["detail"])
                for r in rows
            ],
        )

    def list_snapshot(self, run_id: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM list_snapshot WHERE run_id=? ORDER BY unit, id", (run_id,)
        ))

    # ---- 调试导出 ------------------------------------------------------
    def query_runs(self, run_id: str, unit: Optional[str] = None) -> list[sqlite3.Row]:
        if unit is None:
            return list(self.conn.execute(
                "SELECT * FROM query_run WHERE run_id=? ORDER BY unit, industry, year, page", (run_id,)
            ))
        return list(self.conn.execute(
            "SELECT * FROM query_run WHERE run_id=? AND unit=? ORDER BY industry, year, page",
            (run_id, unit),
        ))

    def runs(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM run ORDER BY started_at"))
