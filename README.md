# 三学期专业去向汇总系统

教务处查同一届全程在籍学生的专业去向：录入**第一至第二学期**、**第二至第三学期**
两份去向 CSV，调阅学籍原件后可成批补录/更正**首末专业对**（第一学期 → 第三学期）
的核定人数，并查询任意首末专业对人数的**上下界**，各附一张达到该界值、且符合
全部已生效汇总的**三学期去向表**。

## 数据模型与不变式

设三学期去向表 `w[i][j][k]` 为第一学期专业 `i`、第二学期专业 `j`、第三学期
专业 `k` 的人数（非负整数）。系统维护的不变式：

> 任一时刻，库中两份去向表与全部核定数，都能由**同一张**非负整数三学期
> 去向表汇总得到：
> - `Σ_k w[i][j][k] = 一至二学期表[i][j]`（缺行按零）
> - `Σ_i w[i][j][k] = 二至三学期表[j][k]`（缺行按零）
> - `Σ_j w[i][j][k] = 核定数[i][k]`（仅已核定的首末对）

任何会破坏该不变式的写入（录表、补报）都**整批退回**，库中数据不变，
仍可按原数据查询。上下界即在该不变式约束下对 `Σ_j w[i0][j][k0]` 求
最小/最大值的整数规划（三向运输问题，规模 ≤6×6×6，用 HiGHS 经
`scipy.optimize.milp` 求解）。

## 快速启动

### Docker Compose（推荐）

```bash
docker compose up --build
```

服务监听 `http://localhost:8000`，SQLite 数据持久化在命名卷 `app-data`。

### 本地运行

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 运行测试

```bash
pip install pytest httpx
python -m pytest tests/ -q
```

## 在 Swagger 中试报和查数

打开 `http://localhost:8000/docs`：

1. **录表**：`POST /api/cohorts/{cohort_id}/flows`，届别填 `2023`，
   `file_1_2` 上传 `samples/flows_1_2.csv`，`file_2_3` 上传
   `samples/flows_2_3.csv`。返回三个学期的专业列表与总人数 160。
2. **补报**：`POST /api/cohorts/2023/supplements`，请求体贴
   `samples/supplements_ok.json` 的内容（计→计 50、数→数 40），应返回 201；
   再试 `samples/supplements_bad.json`（计→计 59），应返回 409 并附原因，
   且已生效数据不变。
3. **查数**：`GET /api/cohorts/2023/bounds?from_major=计算机&to_major=计算机`，
   返回上下界及各自的三学期去向见证表（含三张二维汇总，可核对与旧表一致）。
4. **看全表**：`GET /api/cohorts/2023/table` 返回任一满足全部生效汇总的
   三学期去向表；`GET /api/cohorts/2023` 查看当前已生效数据。

命令行等价操作：

```bash
curl -X POST http://localhost:8000/api/cohorts/2023/flows \
  -F "file_1_2=@samples/flows_1_2.csv" -F "file_2_3=@samples/flows_2_3.csv"
curl -X POST http://localhost:8000/api/cohorts/2023/supplements \
  -H "Content-Type: application/json" -d @samples/supplements_ok.json
curl "http://localhost:8000/api/cohorts/2023/bounds?from_major=计算机&to_major=计算机"
```

## CSV 格式

```csv
from_major,to_major,count
计算机,计算机,52
计算机,数学,8
```

- 表头固定为 `from_major,to_major,count`；UTF-8（可带 BOM）。
- `count` 为非负整数；同一 `(from,to)` 不得重复；缺行按零计。
- 每学期最多 6 个专业；两张表的第二学期各专业人数必须一致
  （同一届全程在籍学生）。

## 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/cohorts/{id}/flows` | 录入/替换两份去向 CSV（multipart），须与已生效核定数联合可行 |
| POST | `/api/cohorts/{id}/supplements` | 成批补录/更正首末专业对核定人数，同对覆盖，整批校验通过才写入 |
| GET | `/api/cohorts/{id}/bounds?from_major=&to_major=` | 该首末对人数上下界，各附达到界值的三学期去向见证表 |
| GET | `/api/cohorts/{id}/table` | 任一满足全部生效汇总的三学期去向表及其二维汇总 |
| GET | `/api/cohorts/{id}` | 当前已生效数据（专业、两张旧表、全部核定数） |
| GET | `/api/cohorts` / `/api/health` | 届别列表 / 健康检查 |

## 并发与一致性设计

- SQLite 开 WAL：读快照不阻塞写入。
- 上下界查询先在**单个读事务**内取一致性快照，随后在内存中求解——
  整份答案（下界表 + 上界表）来自同一套已生效数据，且求解期间补报仍可入库。
- 所有写操作经应用级写锁串行化，并在单事务内完成“校验 + 写入”：
  补报/换表与已生效数据联合可行才提交，否则整批回滚。
- 补报被拒时返回逐条诊断：简易上下界越界、首/末专业总人数超限，
  或逐条试算定位的矛盾条目。

## 目录结构

```
app/
  main.py     FastAPI 接口
  domain.py   校验与模型组装（6 专业上限、二学期人数衔接、已知专业）
  solver.py   三向运输问题整数求解（scipy/HiGHS）、见证表校验
  db.py       SQLite（WAL）、快照读、串行写事务
  csvio.py    去向 CSV 解析
samples/      可直接在 Swagger 试用的样例
tests/        求解器手算案例 + API 端到端测试（含并发）
```

## 待补事项

- 无鉴权与操作审计（批次已留痕于 `batches` 表，但未记录操作人）。
- 核定数暂不支持删除（可以 0 覆盖达到同等效果）。
- 上下界为精确整数规划解；专业数上限 6 时求解在毫秒级，若放宽上限
  需评估求解耗时（三向运输问题一般为 NP 难）。
