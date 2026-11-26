"""SQLite 存储层。

- WAL 模式：快照读不阻塞写入，补报在查询求解期间仍可入库。
- 所有写操作（录表、补报、换码）经 _write_lock 串行化，并在单个事务内
  完成，保证“校验通过才写入，有矛盾整批退回，仍可按原数据查询”，
  交错请求的结果与按入库顺序办理一致。
- 目录版号：每届自 v1 起（已有库迁移时视为首版），换码整体生效后 +1；
  recodes 表记录每次换码的对照与换码前专业清单，供上一版补报转码。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager

_DEFAULT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "..", "data", "app.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS cohorts (
  cohort_id       TEXT PRIMARY KEY,
  catalog_version INTEGER NOT NULL DEFAULT 1,
  created_at      TEXT NOT NULL DEFAULT (datetime('now'))
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
  kind       TEXT NOT NULL CHECK (kind IN ('flows', 'supplements', 'recode')),
  n_rows     INTEGER NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS recodes (
  cohort_id     TEXT NOT NULL REFERENCES cohorts(cohort_id),
  version       INTEGER NOT NULL,
  mappings      TEXT NOT NULL,
  majors_before TEXT NOT NULL,
  batch_id      TEXT NOT NULL,
  created_at    TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (cohort_id, version)
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
        _migrate(conn)
        conn.commit()
    finally:
        conn.close()


def _migrate(conn) -> None:
    """已有库视为首版：补 catalog_version 列（默认 1）、放宽 batches 类别。"""
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(cohorts)")]
    if "catalog_version" not in cols:
        conn.execute("ALTER TABLE cohorts "
                     "ADD COLUMN catalog_version INTEGER NOT NULL DEFAULT 1")
    sql = conn.execute("SELECT sql FROM sqlite_master "
                       "WHERE type='table' AND name='batches'").fetchone()
    if sql and "'recode'" not in sql["sql"]:
        conn.execute("ALTER TABLE batches RENAME TO batches_old")
        conn.execute("""
            CREATE TABLE batches (
              batch_id   TEXT PRIMARY KEY,
              cohort_id  TEXT NOT NULL,
              kind       TEXT NOT NULL
                         CHECK (kind IN ('flows', 'supplements', 'recode')),
              n_rows     INTEGER NOT NULL,
              created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )""")
        conn.execute("INSERT INTO batches SELECT * FROM batches_old")
        conn.execute("DROP TABLE batches_old")


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
        "SELECT cohort_id, catalog_version FROM cohorts WHERE cohort_id=?",
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
    return {"cohort_id": cohort_id, "catalog_version": row["catalog_version"],
            "flows": flows, "supplements": supplements}


def snapshot(cohort_id: str):
    """在单个读事务内读取某届全部已生效数据（WAL 下为一致性快照）。

    返回 None 表示该届不存在；否则返回 dict：
    {cohort_id, catalog_version, flows: {stage: {(from,to): count}},
     supplements: {(from,to): count}}
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


def apply_recode(conn, cohort_id: str,
                 mappings: dict[str, dict[str, str]],
                 majors_before: dict[str, list[str]]) -> tuple[int, str]:
    """在写事务内按对照换码：两份去向表、全部核定数与版号一起切换。

    核定数保留原批次号与录入时间；换码对照与换码前专业清单存入
    recodes 表（供上一版补报转码）。返回 (新版号, 批次号)。
    调用方须已完成全部校验；本函数不抛业务异常，失败由事务整体回滚。
    """
    stage_maps = {"s1_s2": (mappings["sem1"], mappings["sem2"]),
                  "s2_s3": (mappings["sem2"], mappings["sem3"])}
    flows_rows = conn.execute(
        "SELECT stage, from_major, to_major, count FROM flows "
        "WHERE cohort_id=?", (cohort_id,)).fetchall()
    conn.execute("DELETE FROM flows WHERE cohort_id=?", (cohort_id,))
    conn.executemany(
        "INSERT INTO flows(cohort_id, stage, from_major, to_major, count)"
        " VALUES (?,?,?,?,?)",
        [(cohort_id, r["stage"],
          stage_maps[r["stage"]][0].get(r["from_major"], r["from_major"]),
          stage_maps[r["stage"]][1].get(r["to_major"], r["to_major"]),
          r["count"]) for r in flows_rows])
    supp_rows = conn.execute(
        "SELECT from_major, to_major, count, batch_id, updated_at "
        "FROM supplements WHERE cohort_id=?", (cohort_id,)).fetchall()
    conn.execute("DELETE FROM supplements WHERE cohort_id=?", (cohort_id,))
    conn.executemany(
        "INSERT INTO supplements(cohort_id, from_major, to_major, count,"
        " batch_id, updated_at) VALUES (?,?,?,?,?,?)",
        [(cohort_id,
          mappings["sem1"].get(r["from_major"], r["from_major"]),
          mappings["sem3"].get(r["to_major"], r["to_major"]),
          r["count"], r["batch_id"], r["updated_at"]) for r in supp_rows])
    conn.execute("UPDATE cohorts SET catalog_version = catalog_version + 1"
                 " WHERE cohort_id=?", (cohort_id,))
    new_version = conn.execute(
        "SELECT catalog_version FROM cohorts WHERE cohort_id=?",
        (cohort_id,)).fetchone()["catalog_version"]
    batch_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO recodes(cohort_id, version, mappings, majors_before,"
        " batch_id) VALUES (?,?,?,?,?)",
        (cohort_id, new_version,
         json.dumps(mappings, ensure_ascii=False, sort_keys=True),
         json.dumps(majors_before, ensure_ascii=False), batch_id))
    conn.execute(
        "INSERT INTO batches(batch_id, cohort_id, kind, n_rows)"
        " VALUES (?,?,?,?)",
        (batch_id, cohort_id, "recode", len(flows_rows) + len(supp_rows)))
    return new_version, batch_id


def get_recode(conn, cohort_id: str, version: int):
    """读取某届换出指定版号的那次换码档案；不存在返回 None。"""
    r = conn.execute(
        "SELECT mappings, majors_before, batch_id, created_at FROM recodes"
        " WHERE cohort_id=? AND version=?", (cohort_id, version)).fetchone()
    if r is None:
        return None
    return {"mappings": json.loads(r["mappings"]),
            "majors_before": json.loads(r["majors_before"]),
            "batch_id": r["batch_id"], "created_at": r["created_at"]}


def list_cohorts() -> list[dict]:
    conn = connect()
    try:
        return [{"cohort_id": r["cohort_id"],
                 "catalog_version": r["catalog_version"]}
                for r in conn.execute(
                    "SELECT cohort_id, catalog_version FROM cohorts"
                    " ORDER BY cohort_id")]
    finally:
        conn.close()
