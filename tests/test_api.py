"""API 端到端测试（FastAPI TestClient + 临时 SQLite）。"""
import io
import json
import os
from concurrent.futures import ThreadPoolExecutor

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


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    from app.main import app
    with TestClient(app) as c:
        yield c


def upload_flows(client, cohort="2023", csv12=CSV_12, csv23=CSV_23):
    return client.post(
        f"/api/cohorts/{cohort}/flows",
        files={"file_1_2": ("flows_1_2.csv", csv12.encode(), "text/csv"),
               "file_2_3": ("flows_2_3.csv", csv23.encode(), "text/csv")})


def post_supps(client, items, cohort="2023"):
    return client.post(f"/api/cohorts/{cohort}/supplements",
                       json={"items": items})


def check_witness(client, table, cohort="2023"):
    """见证表的二维汇总必须等于当前已生效的两张旧表与全部核定数。"""
    data = client.get(f"/api/cohorts/{cohort}").json()
    a, b, z = {}, {}, {}
    total = 0
    for e in table["entries"]:
        assert isinstance(e["count"], int) and e["count"] >= 0
        total += e["count"]
        k12 = f'{e["sem1"]} -> {e["sem2"]}'
        k23 = f'{e["sem2"]} -> {e["sem3"]}'
        k13 = f'{e["sem1"]} -> {e["sem3"]}'
        a[k12] = a.get(k12, 0) + e["count"]
        b[k23] = b.get(k23, 0) + e["count"]
        z[k13] = z.get(k13, 0) + e["count"]
    for key, want in data["flows_s1_s2"].items():
        assert a.get(key, 0) == want, f"一二学期表 {key}"
    for key, want in data["flows_s2_s3"].items():
        assert b.get(key, 0) == want, f"二三学期表 {key}"
    for key, want in data["supplements"].items():
        assert z.get(key, 0) == want, f"核定数 {key}"
    assert total == data["total_students"]


# ---------- 去向表录入 ----------

def test_upload_flows_ok(client):
    r = upload_flows(client)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["total_students"] == 160
    assert body["majors"]["sem1"] == ["数学", "物理", "计算机"]
    assert body["majors"]["sem3"] == ["数学", "物理", "计算机"]


def test_upload_flows_sem2_mismatch_rejected(client):
    bad23 = CSV_23.replace("计算机,计算机,50", "计算机,计算机,51")
    r = upload_flows(client, csv23=bad23)
    assert r.status_code == 409
    assert "计算机" in json.dumps(r.json()["detail"], ensure_ascii=False)
    assert client.get("/api/cohorts/2023").status_code == 404


@pytest.mark.parametrize("bad_csv,msg", [
    ("f,t,c\n计算机,计算机,1\n", "表头"),
    ("from_major,to_major,count\n计算机,计算机,-1\n", "负数"),
    ("from_major,to_major,count\n计算机,计算机,x\n", "整数"),
    ("from_major,to_major,count\n计算机,计算机,1\n计算机,计算机,2\n", "重复"),
])
def test_upload_flows_csv_errors(client, bad_csv, msg):
    r = upload_flows(client, csv12=bad_csv)
    assert r.status_code == 422
    assert msg in json.dumps(r.json()["detail"], ensure_ascii=False)


def test_upload_flows_too_many_majors(client):
    rows = "\n".join(f"专业{i},专业{i},1" for i in range(7))
    csv7 = f"from_major,to_major,count\n{rows}\n"
    r = upload_flows(client, csv12=csv7, csv23=csv7)
    assert r.status_code == 409
    assert "超过上限" in json.dumps(r.json()["detail"], ensure_ascii=False)


# ---------- 核定数补报 ----------

def test_supplements_before_flows_404(client):
    r = post_supps(client, [{"from_major": "计算机", "to_major": "计算机",
                             "count": 1}])
    assert r.status_code == 404


def test_supplements_accept_and_correct(client):
    upload_flows(client)
    r = post_supps(client, [{"from_major": "计算机", "to_major": "计算机",
                             "count": 50},
                            {"from_major": "数学", "to_major": "数学",
                             "count": 40}])
    assert r.status_code == 201, r.text
    # 更正：同一首末对覆盖
    r = post_supps(client, [{"from_major": "计算机", "to_major": "计算机",
                             "count": 52}])
    assert r.status_code == 201, r.text
    data = client.get("/api/cohorts/2023").json()
    assert data["supplements"]["计算机 -> 计算机"] == 52
    assert data["supplements"]["数学 -> 数学"] == 40


def test_supplements_rejected_atomically(client):
    upload_flows(client)
    post_supps(client, [{"from_major": "计算机", "to_major": "计算机",
                         "count": 50}])
    # 一条越界 + 一条合法：整批退回，合法那条也不写入
    r = post_supps(client, [{"from_major": "数学", "to_major": "数学",
                             "count": 40},
                            {"from_major": "计算机", "to_major": "计算机",
                             "count": 59}])
    assert r.status_code == 409
    detail = json.dumps(r.json()["detail"], ensure_ascii=False)
    assert "[45, 58]" in detail  # 简易界提示
    data = client.get("/api/cohorts/2023").json()
    assert data["supplements"] == {"计算机 -> 计算机": 50}  # 原数据不变


def test_supplements_row_total_violation(client):
    upload_flows(client)
    # 计算机第一学期共 60 人，两条核定各自过简易界、合计 62：联合矛盾
    r = post_supps(client, [{"from_major": "计算机", "to_major": "计算机",
                             "count": 55},
                            {"from_major": "计算机", "to_major": "物理",
                             "count": 7}])
    assert r.status_code == 409
    assert "超过该专业总人数" in json.dumps(r.json()["detail"],
                                          ensure_ascii=False)


def test_supplements_unknown_major(client):
    upload_flows(client)
    r = post_supps(client, [{"from_major": "化学", "to_major": "计算机",
                             "count": 1}])
    assert r.status_code == 409
    assert "化学" in json.dumps(r.json()["detail"], ensure_ascii=False)


def test_flows_reupload_must_stay_consistent(client):
    upload_flows(client)
    post_supps(client, [{"from_major": "计算机", "to_major": "计算机",
                         "count": 50}])
    # 新表使核定数不可行（计算机->计算机 第三学期最多 40+8=48 < 50）：整批退回，旧表仍在
    bad23 = CSV_23.replace("计算机,计算机,50\n计算机,物理,7",
                           "计算机,计算机,40\n计算机,物理,17")
    r = upload_flows(client, csv23=bad23)
    assert r.status_code == 409
    data = client.get("/api/cohorts/2023").json()
    assert data["flows_s2_s3"]["计算机 -> 计算机"] == 50


# ---------- 上下界查询 ----------

def test_bounds_without_supplements(client):
    upload_flows(client)
    r = client.get("/api/cohorts/2023/bounds",
                   params={"from_major": "计算机", "to_major": "计算机"})
    assert r.status_code == 200, r.text
    body = r.json()
    lo, hi = body["lower"]["value"], body["upper"]["value"]
    assert (lo, hi) == (45, 58)
    assert body["verified_value"] is None
    check_witness(client, body["lower"]["table"])
    check_witness(client, body["upper"]["table"])
    z = body["upper"]["table"]["margins"]["s1_s3"]
    assert z["计算机 -> 计算机"] == hi


def test_bounds_with_supplement(client):
    upload_flows(client)
    post_supps(client, [{"from_major": "计算机", "to_major": "计算机",
                         "count": 50}])
    r = client.get("/api/cohorts/2023/bounds",
                   params={"from_major": "计算机", "to_major": "计算机"})
    body = r.json()
    assert body["lower"]["value"] == body["upper"]["value"] == 50
    assert body["verified_value"] == 50
    check_witness(client, body["lower"]["table"])
    # 另一未核定对的界随之收窄但仍可达成
    r2 = client.get("/api/cohorts/2023/bounds",
                    params={"from_major": "数学", "to_major": "数学"})
    assert r2.json()["lower"]["value"] <= r2.json()["upper"]["value"]
    check_witness(client, r2.json()["upper"]["table"])


def test_bounds_unknown_major(client):
    upload_flows(client)
    r = client.get("/api/cohorts/2023/bounds",
                   params={"from_major": "化学", "to_major": "计算机"})
    assert r.status_code == 404


def test_bounds_unknown_cohort(client):
    assert client.get("/api/cohorts/1999/bounds",
                      params={"from_major": "a", "to_major": "b"}
                      ).status_code == 404


def test_feasible_table_endpoint(client):
    upload_flows(client)
    post_supps(client, [{"from_major": "计算机", "to_major": "计算机",
                         "count": 50}])
    r = client.get("/api/cohorts/2023/table")
    assert r.status_code == 200
    check_witness(client, r.json()["table"])


# ---------- 并发：求解期间补报仍可入库 ----------

def test_bounds_query_does_not_block_writes(client):
    upload_flows(client)
    post_supps(client, [{"from_major": "计算机", "to_major": "计算机",
                         "count": 50}])

    def query_bounds(_):
        r = client.get("/api/cohorts/2023/bounds",
                       params={"from_major": "物理", "to_major": "数学"})
        assert r.status_code == 200
        return r.json()["lower"]["value"]

    def write_supp(n):
        r = post_supps(client, [{"from_major": "数学", "to_major": "数学",
                                 "count": 40 + n}])
        assert r.status_code == 201

    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = [ex.submit(query_bounds, i) for i in range(6)]
        futs += [ex.submit(write_supp, n) for n in range(3)]
        for f in futs:
            f.result()
    data = client.get("/api/cohorts/2023").json()
    # 三次更正并发，最终值是其中之一（顺序不定），但必须已生效
    assert data["supplements"]["数学 -> 数学"] in {40, 41, 42}
