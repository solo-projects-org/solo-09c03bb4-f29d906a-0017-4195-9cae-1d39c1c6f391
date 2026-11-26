"""教务处三学期专业去向汇总系统。

- POST /api/cohorts/{cohort_id}/flows        录入两份去向 CSV（整批替换，只收当前版）
- POST /api/cohorts/{cohort_id}/supplements  成批补录/更正首末专业对核定人数（当前版或上一版）
- POST /api/cohorts/{cohort_id}/recodes      按届换码（两表、核定数与版号一起切换）
- GET  /api/cohorts/{cohort_id}              查看当前已生效数据（附目录版号）
- GET  /api/cohorts/{cohort_id}/table        任一满足全部生效汇总的三学期去向表
- GET  /api/cohorts/{cohort_id}/bounds       指定首末专业对的人数上下界（各附见证表）

不变式：任一时刻，库中两份去向表与全部核定数都能由同一张
非负整数三学期去向表汇总得到；任何会破坏该不变式的写入整批退回。

目录版号：每届自 v1 起（已有库视为首版）；换码整体生效后 +1。
无版号的录表/补报只在首版受理；换表只收当前版；补报可收当前版或
上一版（按最近一次换码前的专业清单与对照转成现行码后整批校验）；
查询返回所读数据的版号。
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, ConfigDict, Field

from . import db, solver
from .csvio import CsvError, parse_flows_csv
from .domain import (DataError, Model, apply_mappings, build_model,
                     check_recode, convert_previous_version, majors_of_flows,
                     named_entries, named_margins)


class SupplementItem(BaseModel):
    from_major: str = Field(min_length=1, description="第一学期专业")
    to_major: str = Field(min_length=1, description="第三学期专业")
    count: int = Field(ge=0, description="核定人数（非负整数）")


class SupplementBatch(BaseModel):
    catalog_version: int | None = Field(
        default=None, ge=1,
        description="条目所用目录版号：缺省仅首版受理；可填当前版（现行码）"
                    "或上一版（旧码，按最近一次换码对照转成现行码）")
    items: list[SupplementItem] = Field(min_length=1)

    model_config = {
        "json_schema_extra": {
            "examples": [
                {"items": [{"from_major": "计算机", "to_major": "计算机",
                            "count": 50},
                           {"from_major": "数学", "to_major": "数学",
                            "count": 40}]},
                {"catalog_version": 1,
                 "items": [{"from_major": "计算机", "to_major": "计算机",
                            "count": 50}]},
            ]
        }
    }


class RecodeMappings(BaseModel):
    sem1: dict[str, str] = Field(default_factory=dict,
                                 description="第一学期 旧码 -> 新码")
    sem2: dict[str, str] = Field(default_factory=dict,
                                 description="第二学期 旧码 -> 新码")
    sem3: dict[str, str] = Field(default_factory=dict,
                                 description="第三学期 旧码 -> 新码")
    model_config = ConfigDict(extra="forbid")


class RecodeRequest(BaseModel):
    base_version: int = Field(
        ge=1, description="提交方所见的目录版号（须等于当前版才受理）")
    mappings: RecodeMappings = Field(
        description="各学期旧码 -> 新码对照；未列出的专业码沿用；"
                    "允许两码互换（成对列出）")

    model_config = {
        "json_schema_extra": {
            "examples": [
                {"base_version": 1,
                 "mappings": {
                     "sem1": {"计算机": "计算机科学与技术"},
                     "sem2": {"计算机": "计算机科学与技术"},
                     "sem3": {"计算机": "计算机科学与技术"}}},
                {"base_version": 1,
                 "mappings": {
                     "sem1": {"计算机": "数学", "数学": "计算机"},
                     "sem2": {"计算机": "数学", "数学": "计算机"},
                     "sem3": {"计算机": "数学", "数学": "计算机"}}},
            ]
        }
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(
    title="三学期专业去向汇总系统",
    version="1.1.0",
    lifespan=lifespan,
    description="录入两届间去向 CSV 与首末专业对核定人数，"
                "查询首末专业对人数的上下界及达到界值的三学期去向表。"
                "支持按届换码（目录版号）：换表只收当前版，补报可收当前版或上一版。",
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
                     description="第二至第三学期去向 CSV"),
                 catalog_version: int | None = Query(
                     default=None, ge=1,
                     description="目录版号：缺省仅首版受理；换表只收当前版")):
    """录入/替换两份去向 CSV（按现行专业码，只收当前版）。

    缺行按零；第二学期各专业人数两表必须一致；若已有核定数，则新表须与
    全部核定数联合可行，否则整批退回。无版号的录表只在首版受理。
    """
    flows12 = _read_csv(file_1_2, "一至二学期表")
    flows23 = _read_csv(file_2_3, "二至三学期表")
    with db.write_txn() as conn:
        current = db.read_effective(conn, cohort_id)
        cur_ver = current["catalog_version"] if current else 1
        if catalog_version is None:
            if cur_ver != 1:
                _err(409, f"该届目录已换码（当前版 v{cur_ver}），无版号的录表"
                          f"只在首版受理；请带 catalog_version={cur_ver}")
        elif catalog_version != cur_ver:
            _err(409, f"版号不符：提交 v{catalog_version}，当前版 v{cur_ver}；"
                      f"换表只收当前版，整批退回，沿用原版")
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
            "catalog_version": cur_ver,
            "majors": {"sem1": m.majors1, "sem2": m.majors2,
                       "sem3": m.majors3},
            "total_students": m.total_students,
            "rows": {"s1_s2": len(flows12), "s2_s3": len(flows23)}}


@app.post("/api/cohorts/{cohort_id}/supplements", status_code=201)
def upload_supplements(cohort_id: str, batch: SupplementBatch):
    """成批补录/更正首末专业对核定人数。同一首末对重复时后者覆盖前者；
    整批与已生效数据联合可行才写入，否则整批退回。

    目录版号（catalog_version）：缺省仅首版受理；填当前版按现行码；
    填上一版则按最近一次换码前的专业清单与对照转成现行码后，
    再用生效数据做整批可行性校验。旧码无对应专业或版号不符时退回。
    """
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
        cur_ver = current["catalog_version"]
        submitted = batch.catalog_version
        items = [(it.from_major, it.to_major, it.count)
                 for it in batch.items]
        if submitted is None:
            if cur_ver != 1:
                _err(409, f"该届目录已换码（当前版 v{cur_ver}），无版号的补报"
                          f"只在首版受理；请带 catalog_version"
                          f"（当前版 v{cur_ver} 或上一版 v{cur_ver - 1}）")
        elif submitted == cur_ver:
            pass
        elif submitted == cur_ver - 1:
            recode = db.get_recode(conn, cohort_id, cur_ver)
            items, problems = convert_previous_version(items, recode)
            if problems:
                _err(409, "上一版补报含无对应专业的旧码，整批退回", problems)
        else:
            accept = f"当前版 v{cur_ver}" + (
                f" 或上一版 v{cur_ver - 1}" if cur_ver > 1 else "")
            _err(409, f"版号不符：提交 v{submitted}，可接受{accept}，"
                      f"整批退回，仍按 v{cur_ver} 数据服务")
        merged = dict(current["supplements"])
        for f, t, n in items:
            merged[(f, t)] = n
        m = _model_or_409(cohort_id, current["flows"]["s1_s2"],
                          current["flows"]["s2_s3"], merged)
        try:
            solver.find_table(m.n1, m.n2, m.n3, m.a, m.b, m.z)
        except solver.Infeasible:
            note = (f"（条目已按上一版 v{submitted} 对照转成现行码）"
                    if submitted not in (None, cur_ver) else "")
            new_items = [SupplementItem(from_major=f, to_major=t, count=n)
                         for f, t, n in items]
            _err(409, f"补报与已生效数据矛盾{note}，整批退回，仍可按原数据查询",
                 _diagnose(m, merged, new_items))
        batch_id = db.upsert_supplements(conn, cohort_id, items)
    resp = {"cohort_id": cohort_id, "batch_id": batch_id,
            "accepted": len(items),
            "effective_supplements": len(merged),
            "catalog_version": cur_ver}
    if submitted not in (None, cur_ver):
        resp["converted_from_version"] = submitted
    return resp


@app.post("/api/cohorts/{cohort_id}/recodes", status_code=201)
def recode_catalog(cohort_id: str, req: RecodeRequest):
    """按届换码：提交所见的目录版号与各学期旧码 -> 新码对照。

    - 旧码须在该学期现行专业清单内；允许两码互换（成对列出）；
      未列出的专业码沿用；换完后同一学期不得重码。
    - 两份去向表、全部核定数（保留原批次号）与版号在同一事务内切换；
      任一校验失败整批退回，沿用原版。
    """
    mappings = {sem: {k.strip(): v.strip()
                      for k, v in getattr(req.mappings, sem).items()}
                for sem in ("sem1", "sem2", "sem3")}
    with db.write_txn() as conn:
        current = db.read_effective(conn, cohort_id)
        if current is None or not current["flows"]["s1_s2"]:
            _err(404, f"届别 {cohort_id} 尚未录入两份去向 CSV")
        cur_ver = current["catalog_version"]
        if req.base_version != cur_ver:
            _err(409, f"版号不符：提交所见 v{req.base_version}，"
                      f"当前版 v{cur_ver}，整批退回，沿用原版")
        majors = majors_of_flows(current["flows"]["s1_s2"],
                                 current["flows"]["s2_s3"])
        problems = check_recode(mappings, majors)
        if problems:
            _err(409, "换码对照不合法，整批退回，沿用原版", problems)
        new_version, batch_id = db.apply_recode(conn, cohort_id, mappings,
                                                majors)
        flows12, flows23, _ = apply_mappings(current["flows"]["s1_s2"],
                                             current["flows"]["s2_s3"],
                                             current["supplements"], mappings)
        majors_after = majors_of_flows(flows12, flows23)
    return {"cohort_id": cohort_id, "base_version": cur_ver,
            "catalog_version": new_version, "batch_id": batch_id,
            "renamed": mappings,
            "majors_before": majors, "majors": majors_after,
            "supplements_renamed": len(current["supplements"])}


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
        "catalog_version": snap["catalog_version"],
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
    return {"cohort_id": cohort_id,
            "catalog_version": snap["catalog_version"],
            "table": _witness_json(m, w)}


@app.get("/api/cohorts/{cohort_id}/bounds")
def get_bounds(cohort_id: str,
               from_major: str = Query(description="第一学期专业"),
               to_major: str = Query(description="第三学期专业")):
    """指定首末专业对的人数上下界，各附一张达到界值、
    且符合全部生效汇总的三学期去向表。

    整个回答基于同一次快照（同一套已生效数据、同一目录版号）计算；
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
        "catalog_version": snap["catalog_version"],
        "pair": {"from_major": from_major, "to_major": to_major},
        "snapshot": {"total_students": m.total_students,
                     "n_supplements": len(m.z)},
        "verified_value": fixed,
        "lower": {"value": lo, "table": _witness_json(m, w_lo)},
        "upper": {"value": hi, "table": _witness_json(m, w_hi)},
    }
