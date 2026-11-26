"""按届换码（目录版号）端到端测试。

覆盖：改名/两码互换、版号校验、上一版补报转码、核定批次号保留、
换码/换表/补报交错的顺序一致性、查询返回所读数据版号。
"""
import json
import os
import sqlite3

import pytest
from fastapi.testclient import TestClient

CSV_12 = """from_major,to_major,count
计算机,计算机,52
计算机,数学,8
数学,数学,45
数学,计算机,5
物理,物理,40
物理,数学,10
"""

CSV_23 = """from_major,to_major,count
计算机,计算机,50
计算机,物理,7
数学,数学,55
数学,计算机,8
物理,物理,38
物理,数学,2
"""

# 换码后（计算机 -> 计科）的两份去向表，用于“换表只收当前版”
CSV_12_V2 = CSV_12.replace("计算机", "计科")
CSV_23_V2 = CSV_23.replace("计算机", "计科")

RENAME = {"sem1": {"计算机": "计科"},
          "sem2": {"计算机": "计科"},
          "sem3": {"计算机": "计科"}}
SWAP = {"sem1": {"计算机": "数学", "数学": "计算机"},
        "sem2": {"计算机": "数学", "数学": "计算机"},
        "sem3": {"计算机": "数学", "数学": "计算机"}}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    from app.main import app
    with TestClient(app) as c:
        yield c


def upload_flows(client, cohort="2023", csv12=CSV_12, csv23=CSV_23,
                 version=None):
    params = {} if version is None else {"catalog_version": version}
    return client.post(
        f"/api/cohorts/{cohort}/flows", params=params,
        files={"file_1_2": ("flows_1_2.csv", csv12.encode(), "text/csv"),
               "file_2_3": ("flows_2_3.csv", csv23.encode(), "text/csv")})


def post_supps(client, items, cohort="2023", version=None):
    body = {"items": items}
    if version is not None:
        body["catalog_version"] = version
    return client.post(f"/api/cohorts/{cohort}/supplements", json=body)


def recode(client, mappings, base_version=1, cohort="2023"):
    return client.post(f"/api/cohorts/{cohort}/recodes",
                       json={"base_version": base_version,
                             "mappings": mappings})


def detail(r):
    return json.dumps(r.json()["detail"], ensure_ascii=False)


# ---------- 换码：改名与互换 ----------

def test_recode_rename_switches_everything(client):
    upload_flows(client)
    r = post_supps(client, [{"from_major": "计算机", "to_major": "计算机",
                             "count": 50}])
    assert r.status_code == 201
    r = recode(client, RENAME)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["base_version"] == 1 and body["catalog_version"] == 2
    assert body["majors"]["sem1"] == ["数学", "物理", "计科"]
    assert body["supplements_renamed"] == 1

    data = client.get("/api/cohorts/2023").json()
    assert data["catalog_version"] == 2
    assert data["flows_s1_s2"]["计科 -> 计科"] == 52
    assert data["flows_s2_s3"]["计科 -> 计科"] == 50
    assert data["supplements"] == {"计科 -> 计科": 50}
    # 查询返回所读数据的版号，且换码后仍可求解
    r = client.get("/api/cohorts/2023/bounds",
                   params={"from_major": "计科", "to_major": "计科"})
    assert r.status_code == 200
    assert r.json()["catalog_version"] == 2
    assert r.json()["lower"]["value"] == r.json()["upper"]["value"] == 50
    assert client.get("/api/cohorts/2023/table"
                      ).json()["catalog_version"] == 2


def test_recode_swap_two_codes(client):
    upload_flows(client)
    post_supps(client, [{"from_major": "计算机", "to_major": "计算机",
                         "count": 50}])
    r = recode(client, SWAP)
    assert r.status_code == 201, r.text
    # 互换后专业集合不变，数据按对照换位
    data = client.get("/api/cohorts/2023").json()
    assert data["majors"]["sem1"] == ["数学", "物理", "计算机"]
    assert data["flows_s1_s2"]["数学 -> 数学"] == 52   # 原 计算机->计算机
    assert data["flows_s1_s2"]["计算机 -> 计算机"] == 45  # 原 数学->数学
    assert data["flows_s2_s3"]["数学 -> 数学"] == 50   # 原 计算机->计算机
    assert data["supplements"] == {"数学 -> 数学": 50}
    # 被钉住的对随互换迁移
    r = client.get("/api/cohorts/2023/bounds",
                   params={"from_major": "数学", "to_major": "数学"})
    assert r.json()["lower"]["value"] == r.json()["upper"]["value"] == 50


def test_recode_preserves_supplement_batch_id(client):
    upload_flows(client)
    batch_id = post_supps(client, [
        {"from_major": "计算机", "to_major": "计算机", "count": 50},
        {"from_major": "数学", "to_major": "数学", "count": 40},
    ]).json()["batch_id"]
    recode(client, RENAME)
    conn = sqlite3.connect(os.environ["DB_PATH"])
    try:
        rows = conn.execute(
            "SELECT from_major, to_major, count, batch_id FROM supplements"
            " WHERE cohort_id='2023' ORDER BY from_major").fetchall()
    finally:
        conn.close()
    assert [(f, t, n) for f, t, n, _ in rows] == [
        ("数学", "数学", 40), ("计科", "计科", 50)]
    assert all(b == batch_id for _, _, _, b in rows)  # 保留原核定批次号


# ---------- 换码：校验失败沿用原版 ----------

def test_recode_version_mismatch_keeps_old(client):
    upload_flows(client)
    r = recode(client, RENAME, base_version=2)
    assert r.status_code == 409
    assert "版号不符" in detail(r)
    data = client.get("/api/cohorts/2023").json()
    assert data["catalog_version"] == 1
    assert "计算机 -> 计算机" in data["flows_s1_s2"]


def test_recode_unknown_old_code(client):
    upload_flows(client)
    r = recode(client, {"sem1": {"化学": "化学工程"}, "sem2": {}, "sem3": {}})
    assert r.status_code == 409
    assert "化学" in detail(r) and "无旧码" in detail(r)
    assert client.get("/api/cohorts/2023").json()["catalog_version"] == 1


def test_recode_duplicate_rejected_unless_swap(client):
    upload_flows(client)
    # 只改一边：换完与沿用的「数学」重码
    r = recode(client, {"sem1": {"计算机": "数学"}, "sem2": {}, "sem3": {}})
    assert r.status_code == 409
    assert "重码" in detail(r)
    assert client.get("/api/cohorts/2023").json()["catalog_version"] == 1


def test_recode_unknown_cohort(client):
    assert recode(client, RENAME, cohort="1999").status_code == 404


# ---------- 换表/补报的版号口径 ----------

def test_flows_only_accepts_current_version(client):
    upload_flows(client)
    recode(client, RENAME)
    # 无版号：非首版不受理
    r = upload_flows(client, csv12=CSV_12_V2, csv23=CSV_23_V2)
    assert r.status_code == 409 and "首版" in detail(r)
    # 旧版号：不受理
    r = upload_flows(client, csv12=CSV_12_V2, csv23=CSV_23_V2, version=1)
    assert r.status_code == 409 and "版号不符" in detail(r)
    # 当前版：受理
    r = upload_flows(client, csv12=CSV_12_V2, csv23=CSV_23_V2, version=2)
    assert r.status_code == 201, r.text
    assert r.json()["catalog_version"] == 2
    data = client.get("/api/cohorts/2023").json()
    assert data["flows_s1_s2"]["计科 -> 计科"] == 52


def test_supplements_version_rules(client):
    upload_flows(client)
    recode(client, RENAME)
    item_old = {"from_major": "计算机", "to_major": "计算机", "count": 50}
    item_new = {"from_major": "计科", "to_major": "计科", "count": 50}
    # 无版号：非首版不受理
    assert post_supps(client, [item_new]).status_code == 409
    # 更旧的版号：不受理
    r = post_supps(client, [item_new], version=3)
    assert r.status_code == 409 and "版号不符" in detail(r)
    # 当前版：按现行码
    r = post_supps(client, [item_new], version=2)
    assert r.status_code == 201, r.text
    assert r.json()["catalog_version"] == 2
    assert "converted_from_version" not in r.json()
    # 上一版：旧码转现行码后覆盖同一对
    r = post_supps(client, [dict(item_old, count=52)], version=1)
    assert r.status_code == 201, r.text
    assert r.json()["converted_from_version"] == 1
    data = client.get("/api/cohorts/2023").json()
    assert data["supplements"] == {"计科 -> 计科": 52}


def test_supplements_previous_version_unknown_old_code(client):
    upload_flows(client)
    recode(client, RENAME)
    r = post_supps(client, [{"from_major": "化学", "to_major": "计算机",
                             "count": 1}], version=1)
    assert r.status_code == 409
    assert "化学" in detail(r) and "无对应专业" in detail(r)


def test_supplements_previous_version_infeasible_rejected(client):
    upload_flows(client)
    recode(client, RENAME)
    # 上一版旧码 + 越界人数：转码后整批校验，仍退回并说明原因
    r = post_supps(client, [{"from_major": "计算机", "to_major": "计算机",
                             "count": 59}], version=1)
    assert r.status_code == 409
    assert "[45, 58]" in detail(r)
    assert client.get("/api/cohorts/2023").json()["supplements"] == {}


def test_second_recode_only_latest_mapping_used(client):
    upload_flows(client)
    recode(client, RENAME)                       # v1 -> v2：计算机->计科
    recode(client, {"sem1": {"数学": "高等数学"},
                    "sem2": {"数学": "高等数学"},
                    "sem3": {"数学": "高等数学"}}, base_version=2)  # v2 -> v3
    # v1 已不可补报
    r = post_supps(client, [{"from_major": "计算机", "to_major": "计算机",
                             "count": 50}], version=1)
    assert r.status_code == 409 and "版号不符" in detail(r)
    # 上一版（v2）按最近一次换码对照转换：数学->高等数学，计科沿用
    r = post_supps(client, [{"from_major": "数学", "to_major": "数学",
                             "count": 40},
                            {"from_major": "计科", "to_major": "计科",
                             "count": 50}], version=2)
    assert r.status_code == 201, r.text
    data = client.get("/api/cohorts/2023").json()
    assert data["catalog_version"] == 3
    assert data["supplements"] == {"高等数学 -> 高等数学": 40,
                                   "计科 -> 计科": 50}


# ---------- 交错顺序一致性 ----------

def test_interleaved_writes_match_arrival_order(client):
    """换码 -> 旧版补报 -> 当前版换表 -> 当前版补报，逐步生效。"""
    upload_flows(client)
    recode(client, RENAME)                                    # -> v2
    r = post_supps(client, [{"from_major": "计算机",
                             "to_major": "计算机", "count": 50}], version=1)
    assert r.status_code == 201                               # 转成 计科->计科
    r = upload_flows(client, csv12=CSV_12_V2, csv23=CSV_23_V2, version=2)
    assert r.status_code == 201                               # 换表只收当前版
    r = post_supps(client, [{"from_major": "计科", "to_major": "计科",
                             "count": 52}], version=2)
    assert r.status_code == 201                               # 同对覆盖
    data = client.get("/api/cohorts/2023").json()
    assert data["catalog_version"] == 2
    assert data["supplements"] == {"计科 -> 计科": 52}
    assert data["flows_s1_s2"]["计科 -> 计科"] == 52


def test_cohorts_list_includes_version(client):
    upload_flows(client)
    recode(client, RENAME)
    cohorts = client.get("/api/cohorts").json()["cohorts"]
    assert cohorts == [{"cohort_id": "2023", "catalog_version": 2}]


# ---------- 首版兼容：无版号照常受理 ----------

def test_versionless_writes_ok_on_first_version(client):
    assert upload_flows(client).status_code == 201
    r = post_supps(client, [{"from_major": "计算机", "to_major": "计算机",
                             "count": 50}])
    assert r.status_code == 201
    assert r.json()["catalog_version"] == 1
    # 首版上显式带 v1 也受理
    assert post_supps(client, [{"from_major": "数学", "to_major": "数学",
                                "count": 40}], version=1).status_code == 201
