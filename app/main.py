"""教务处三学期专业去向汇总系统。

- POST /api/cohorts/{cohort_id}/flows        录入两份去向 CSV（整批替换）
- POST /api/cohorts/{cohort_id}/supplements  成批补录/更正首末专业对核定人数
- GET  /api/cohorts/{cohort_id}              查看当前已生效数据
- GET  /api/cohorts/{cohort_id}/table        任一满足全部生效汇总的三学期去向表
- GET  /api/cohorts/{cohort_id}/bounds       指定首末专业对的人数上下界（各附见证表）

不变式：任一时刻，库中两份去向表与全部核定数都能由同一张
非负整数三学期去向表汇总得到；任何会破坏该不变式的写入整批退回。
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from . import db, solver
from .csvio import CsvError, parse_flows_csv
from .domain import (DataError, Model, build_model, named_entries,
                     named_margins)


class SupplementItem(BaseModel):
    from_major: str = Field(min_length=1, description="第一学期专业")
    to_major: str = Field(min_length=1, description="第三学期专业")
    count: int = Field(ge=0, description="核定人数（非负整数）")


class SupplementBatch(BaseModel):
    items: list[SupplementItem] = Field(min_length=1)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(
    title="三学期专业去向汇总系统",
    version="1.0.0",
    lifespan=lifespan,
    description="录入两届间去向 CSV 与首末专业对核定人数，"
                "查询首末专业对人数的上下界及达到界值的三学期去向表。",
)


# ---------- 工具 ----------

def _err(status: int, message: str, problems: list[str] | None = None):
    detail = {"message": message}
    if problems:
        detail["problems"] = problems
    raise HTTPException(status_code=status, detail=detail)


def _model_or_409(cohort_id: str, flows12, flows23, supplements) -> Model:
    try:
        return build_model(cohort_id, flows12, flows23, supplements)
    except DataError as e:
        _err(409, "数据自身不一致，整批退回", e.problems)


def _diagnose(m: Model, merged_z_named: dict,
              new_items: list[SupplementItem]) -> list[str]:
    """补报被拒时给出逐条原因。"""
    problems: list[str] = []
    col_a = m.col_a()
    row_a = m.row_a()
    col_b = m.col_b()
    # 1) 逐条简易界检查 + 2) 按首/末专业汇总不超过该专业总人数
    for it in new_items:
        i, k = m.idx1(it.from_major), m.idx3(it.to_major)
        if i is None or k is None:
            continue
        lo, hi = solver.cheap_bounds(m.n2, m.a, m.b, col_a, i, k)
        if not (lo <= it.count <= hi):
            problems.append(
                f"{it.from_major} -> {it.to_major} 核定 {it.count} 人，"
                f"超出由两张去向表推出的可能范围 [{lo}, {hi}]")
    for i, name in enumerate(m.majors1):
        tot = sum(v for (s, _), v in merged_z_named.items()
                  if s == name)
        if tot > row_a.get(i, 0):
            problems.append(
                f"第一学期专业「{name}」核定人数合计 {tot}，"
                f"超过该专业总人数 {row_a.get(i, 0)}")
    for k, name in enumerate(m.majors3):
        tot = sum(v for (_, d), v in merged_z_named.items()
                  if d == name)
        if tot > col_b.get(k, 0):
            problems.append(
                f"第三学期专业「{name}」核定人数合计 {tot}，"
                f"超过该专业总人数 {col_b.get(k, 0)}")
    if problems:
        return problems
    # 3) 简易检查都过但整体不可行：逐条试算定位
    bad = []
    for it in new_items:
        z1 = dict(m.z)
        z1[(m.idx1(it.from_major), m.idx3(it.to_major))] = it.count
        try:
            solver.find_table(m.n1, m.n2, m.n3, m.a, m.b, z1)
        except solver.Infeasible:
            bad.append(f"{it.from_major} -> {it.to_major} = {it.count}")
    if bad:
        problems.append("以下条目单独即与已生效数据矛盾: " + "; ".join(bad))
    else:
        problems.append("各条目单独可行，但合并后与已生效数据联合矛盾"
                        "（不存在同时满足全部汇总的三学期去向表）")
    return problems


def _witness_json(m: Model, w: dict) -> dict:
    problems = solver.check_witness(w, m.a, m.b, m.z, m.n1, m.n2, m.n3)
    if problems:
        raise RuntimeError("内部校验失败: " + "; ".join(problems))
    return {"entries": named_entries(m, w),
            "total": sum(w.values()),
            "margins": named_margins(m, w)}


def _read_csv(upload: UploadFile, label: str) -> dict:
    raw = upload.file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        _err(422, f"{label} 不是有效的 UTF-8 文本")
    try:
        return parse_flows_csv(text)
    except CsvError as e:
        _err(422, f"{label} 解析失败: {e}")


# ---------- 接口 ----------

@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/cohorts")
def cohorts():
    return {"cohorts": db.list_cohorts()}


@app.post("/api/cohorts/{cohort_id}/flows", status_code=201)
def upload_flows(cohort_id: str,
                 file_1_2: UploadFile = File(
                     description="第一至第二学期去向 CSV"),
                 file_2_3: UploadFile = File(
                     description="第二至第三学期去向 CSV")):
    """录入/替换两份去向 CSV。缺行按零；第二学期各专业人数两表必须一致；
    若已有核定数，则新表须与全部核定数联合可行，否则整批退回。"""
    flows12 = _read_csv(file_1_2, "一至二学期表")
    flows23 = _read_csv(file_2_3, "二至三学期表")
    with db.write_txn() as conn:
        current = db.read_effective(conn, cohort_id)
        supplements = current["supplements"] if current else {}
        m = _model_or_409(cohort_id, flows12, flows23, supplements)
        if supplements:
            try:
                solver.find_table(m.n1, m.n2, m.n3, m.a, m.b, m.z)
            except solver.Infeasible:
                _err(409, "新去向表与已生效核定数矛盾，整批退回",
                     [f"核定 {s} -> {d} = {n} 与新表不可同时成立"
                      for (s, d), n in supplements.items()])
        batch_id = db.replace_flows(conn, cohort_id, flows12, flows23)
    return {"cohort_id": cohort_id, "batch_id": batch_id,
            "majors": {"sem1": m.majors1, "sem2": m.majors2,
                       "sem3": m.majors3},
            "total_students": m.total_students,
            "rows": {"s1_s2": len(flows12), "s2_s3": len(flows23)}}


@app.post("/api/cohorts/{cohort_id}/supplements", status_code=201)
def upload_supplements(cohort_id: str, batch: SupplementBatch):
    """成批补录/更正首末专业对核定人数。同一首末对重复时后者覆盖前者；
    整批与已生效数据联合可行才写入，否则整批退回。"""
    seen: set[tuple[str, str]] = set()
    for it in batch.items:
        key = (it.from_major, it.to_major)
        if key in seen:
            _err(422, f"批次内重复的首末专业对: {it.from_major} -> "
                      f"{it.to_major}")
        seen.add(key)
    with db.write_txn() as conn:
        current = db.read_effective(conn, cohort_id)
        if current is None or not current["flows"]["s1_s2"]:
            _err(404, f"届别 {cohort_id} 尚未录入两份去向 CSV")
        merged = dict(current["supplements"])
        for it in batch.items:
            merged[(it.from_major, it.to_major)] = it.count
        m = _model_or_409(cohort_id, current["flows"]["s1_s2"],
                          current["flows"]["s2_s3"], merged)
        try:
            solver.find_table(m.n1, m.n2, m.n3, m.a, m.b, m.z)
        except solver.Infeasible:
            _err(409, "补报与已生效数据矛盾，整批退回，仍可按原数据查询",
                 _diagnose(m, merged, batch.items))
        items = [(it.from_major, it.to_major, it.count)
                 for it in batch.items]
        batch_id = db.upsert_supplements(conn, cohort_id, items)
    return {"cohort_id": cohort_id, "batch_id": batch_id,
            "accepted": len(items),
            "effective_supplements": len(merged)}


@app.get("/api/cohorts/{cohort_id}")
def get_cohort(cohort_id: str):
    snap = db.snapshot(cohort_id)
    if snap is None:
        _err(404, f"届别 {cohort_id} 不存在")
    m = _model_or_409(cohort_id, snap["flows"]["s1_s2"],
                      snap["flows"]["s2_s3"], snap["supplements"])
    fmt = lambda d: {f"{s} -> {d_}": n for (s, d_), n in sorted(d.items())}
    return {
        "cohort_id": cohort_id,
        "majors": {"sem1": m.majors1, "sem2": m.majors2,
                   "sem3": m.majors3},
        "total_students": m.total_students,
        "flows_s1_s2": fmt(snap["flows"]["s1_s2"]),
        "flows_s2_s3": fmt(snap["flows"]["s2_s3"]),
        "supplements": fmt(snap["supplements"]),
    }


@app.get("/api/cohorts/{cohort_id}/table")
def get_table(cohort_id: str):
    """任一满足全部生效汇总的三学期去向表及其三张二维汇总。"""
    snap = db.snapshot(cohort_id)
    if snap is None:
        _err(404, f"届别 {cohort_id} 不存在")
    m = _model_or_409(cohort_id, snap["flows"]["s1_s2"],
                      snap["flows"]["s2_s3"], snap["supplements"])
    try:
        w = solver.find_table(m.n1, m.n2, m.n3, m.a, m.b, m.z)
    except solver.Infeasible:
        _err(409, "已生效数据不可行（不应发生，请检查写入路径）")
    return {"cohort_id": cohort_id, "table": _witness_json(m, w)}


@app.get("/api/cohorts/{cohort_id}/bounds")
def get_bounds(cohort_id: str,
               from_major: str = Query(description="第一学期专业"),
               to_major: str = Query(description="第三学期专业")):
    """指定首末专业对的人数上下界，各附一张达到界值、
    且符合全部生效汇总的三学期去向表。

    整个回答基于同一次快照（同一套已生效数据）计算；
    求解在快照读取后进行，不阻塞补报入库。
    """
    snap = db.snapshot(cohort_id)   # 一次快照，之后求解不占库锁
    if snap is None:
        _err(404, f"届别 {cohort_id} 不存在")
    m = _model_or_409(cohort_id, snap["flows"]["s1_s2"],
                      snap["flows"]["s2_s3"], snap["supplements"])
    i0, k0 = m.idx1(from_major), m.idx3(to_major)
    if i0 is None:
        _err(404, f"「{from_major}」不是第一学期专业",
             [f"可选: {', '.join(m.majors1)}"])
    if k0 is None:
        _err(404, f"「{to_major}」不是第三学期专业",
             [f"可选: {', '.join(m.majors3)}"])
    try:
        lo, w_lo, hi, w_hi = solver.bounds_for_pair(
            m.n1, m.n2, m.n3, m.a, m.b, m.z, (i0, k0))
    except solver.Infeasible:
        _err(409, "已生效数据不可行（不应发生，请检查写入路径）")
    fixed = snap["supplements"].get((from_major, to_major))
    return {
        "cohort_id": cohort_id,
        "pair": {"from_major": from_major, "to_major": to_major},
        "snapshot": {"total_students": m.total_students,
                     "n_supplements": len(m.z)},
        "verified_value": fixed,
        "lower": {"value": lo, "table": _witness_json(m, w_lo)},
        "upper": {"value": hi, "table": _witness_json(m, w_hi)},
    }
