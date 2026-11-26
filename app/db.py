"""SQLite 存储层。

- WAL 模式：快照读不阻塞写入，补报在查询求解期间仍可入库。
- 所有写操作经 _write_lock 串行化，并在单个事务内完成，保证
  “补报通过才写入，有矛盾整批退回，仍可按原数据查询”。
"""
from __future__ import annotations

import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager

_DEFAULT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "..", "data", "app.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS cohorts (
  cohort_id  TEXT PRIMARY KEY,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS flows (
  cohort_id  TEXT NOT NULL REFERENCES cohorts(cohort_id),
  stage      TEXT NOT NULL CHECK (stage IN ('s1_s2', 's2_s3')),
  from_major TEXT NOT NULL,
  to_major   TEXT NOT NULL,
  count      INTEGER NOT NULL CHECK (count >= 0),
  PRIMARY KEY (cohort_id, stage, from_major, to_major)
);
CREATE TABLE IF NOT EXISTS supplements (
  cohort_id  TEXT NOT NULL REFERENCES cohorts(cohort_id),
  from_major TEXT NOT NULL,
  to_major   TEXT NOT NULL,
  count      INTEGER NOT NULL CHECK (count >= 0),
  batch_id   TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (cohort_id, from_major, to_major)
);
CREATE TABLE IF NOT EXISTS batches (
  batch_id   TEXT PRIMARY KEY,
  cohort_id  TEXT NOT NULL,
  kind       TEXT NOT NULL CHECK (kind IN ('flows', 'supplements')),
  n_rows     INTEGER NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_write_lock = threading.Lock()


def db_path() -> str:
    return os.environ.get("DB_PATH", _DEFAULT_PATH)


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    path = db_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = connect()
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def write_txn():
    """串行化的写事务：进入即持应用级写锁与 SQLite IMMEDIATE 事务。"""
    with _write_lock:
        conn = connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()


def read_effective(conn, cohort_id: str):
    """在已有连接/事务内读取某届全部已生效数据；不存在返回 None。"""
    row = conn.execute(
        "SELECT cohort_id FROM cohorts WHERE cohort_id=?",
        (cohort_id,)).fetchone()
    if row is None:
        return None
    flows: dict[str, dict[tuple[str, str], int]] = {"s1_s2": {}, "s2_s3": {}}
    for r in conn.execute(
            "SELECT stage, from_major, to_major, count FROM flows "
            "WHERE cohort_id=?", (cohort_id,)):
        flows[r["stage"]][(r["from_major"], r["to_major"])] = r["count"]
    supplements = {
        (r["from_major"], r["to_major"]): r["count"]
        for r in conn.execute(
            "SELECT from_major, to_major, count FROM supplements "
            "WHERE cohort_id=?", (cohort_id,))
    }
    return {"cohort_id": cohort_id, "flows": flows,
            "supplements": supplements}


def snapshot(cohort_id: str):
    """在单个读事务内读取某届全部已生效数据（WAL 下为一致性快照）。

    返回 None 表示该届不存在；否则返回 dict：
    {cohort_id, flows: {stage: {(from,to): count}}, supplements: {(from,to): count}}
    """
    conn = connect()
    try:
        conn.execute("BEGIN")
        data = read_effective(conn, cohort_id)
        conn.commit()
        return data
    finally:
        conn.close()


def replace_flows(conn, cohort_id: str,
                  flows12: dict[tuple[str, str], int],
                  flows23: dict[tuple[str, str], int]) -> str:
    """在写事务内整批替换两份去向表。返回批次号。"""
    conn.execute(
        "INSERT INTO cohorts(cohort_id) VALUES(?) "
        "ON CONFLICT(cohort_id) DO NOTHING", (cohort_id,))
    conn.execute("DELETE FROM flows WHERE cohort_id=?", (cohort_id,))
    for stage, table in (("s1_s2", flows12), ("s2_s3", flows23)):
        conn.executemany(
            "INSERT INTO flows(cohort_id, stage, from_major, to_major, count)"
            " VALUES (?,?,?,?,?)",
            [(cohort_id, stage, s, d, n) for (s, d), n in table.items()])
    batch_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO batches(batch_id, cohort_id, kind, n_rows)"
        " VALUES (?,?,?,?)",
        (batch_id, cohort_id, "flows", len(flows12) + len(flows23)))
    return batch_id


def upsert_supplements(conn, cohort_id: str,
                       items: list[tuple[str, str, int]]) -> str:
    """在写事务内成批补录/更正核定人数（同对覆盖）。返回批次号。"""
    conn.execute(
        "INSERT INTO cohorts(cohort_id) VALUES(?) "
        "ON CONFLICT(cohort_id) DO NOTHING", (cohort_id,))
    batch_id = uuid.uuid4().hex
    conn.executemany(
        "INSERT INTO supplements(cohort_id, from_major, to_major, count,"
        " batch_id, updated_at) VALUES (?,?,?,?,?, datetime('now')) "
        "ON CONFLICT(cohort_id, from_major, to_major) "
        "DO UPDATE SET count=excluded.count, batch_id=excluded.batch_id,"
        " updated_at=excluded.updated_at",
        [(cohort_id, s, d, n, batch_id) for s, d, n in items])
    conn.execute(
        "INSERT INTO batches(batch_id, cohort_id, kind, n_rows)"
        " VALUES (?,?,?,?)",
        (batch_id, cohort_id, "supplements", len(items)))
    return batch_id


def list_cohorts() -> list[str]:
    conn = connect()
    try:
        return [r["cohort_id"] for r in conn.execute(
            "SELECT cohort_id FROM cohorts ORDER BY cohort_id")]
    finally:
        conn.close()
