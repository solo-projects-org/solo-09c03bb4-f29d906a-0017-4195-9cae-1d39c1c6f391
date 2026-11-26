# 三学期专业去向汇总系统

教务处查同一届全程在籍学生的专业去向：录入**第一至第二学期**、**第二至第三学期**
两份去向 CSV，调阅学籍原件后可成批补录/更正**首末专业对**（第一学期 → 第三学期）
的核定人数，并查询任意首末专业对人数的**上下界**，各附一张达到该界值、且符合
全部已生效汇总的**三学期去向表**。

院系换专业代码时，可按届**换码**（目录版号 +1）：两份去向表、全部核定数
（保留原批次号）与版号一起切换；按旧目录核出的首末人数可凭**上一版**旧码补报，
系统自动转成现行码后再做整批可行性校验。

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

## 目录版号与换码

每届目录自 **v1** 起（已有库迁移时视为首版），每次换码整体生效后 **+1**：

- **换码** `POST /api/cohorts/{id}/recodes`：提交所见的目录版号
  （`base_version`，须等于当前版）与各学期旧码 → 新码对照。旧码须在该学期
  现行专业清单内；**允许两码互换**（成对列出即可）；未列出的专业码沿用；
  换完后同一学期不得重码。两份去向表、全部核定数（**保留原核定批次号**）
  与版号在同一事务内切换；任一校验失败整批退回，**沿用原版**。
- **换表只收当前版**：录表可带 `catalog_version` 查询参数，须等于当前版；
  **无版号的录表只在首版受理**。
- **补报可收当前版或上一版**：请求体可带 `catalog_version`。填当前版按
  现行码；填上一版则按**最近一次换码前的专业清单与对照**转成现行码，
  再用生效数据做整批可行性校验；**无版号的补报只在首版受理**。
  旧码无对应专业或版号不符时，说明原因并退回。
- **查询返回所读数据的版号**：`GET /api/cohorts/{id}`、`/table`、`/bounds`
  及届别列表均附 `catalog_version`。

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
   `samples/flows_2_3.csv`。返回三个学期的专业列表与总人数 160（首版 v1）。
2. **补报**：`POST /api/cohorts/2023/supplements`，请求体贴
   `samples/supplements_ok.json` 的内容（计→计 50、数→数 40），应返回 201；
   再试 `samples/supplements_bad.json`（计→计 59），应返回 409 并附原因，
   且已生效数据不变。
3. **查数**：`GET /api/cohorts/2023/bounds?from_major=计算机&to_major=计算机`，
   返回上下界及各自的三学期去向见证表（含三张二维汇总，可核对与旧表一致）。
4. **看全表**：`GET /api/cohorts/2023/table` 返回任一满足全部生效汇总的
   三学期去向表；`GET /api/cohorts/2023` 查看当前已生效数据与目录版号。

### 试换码（改名）

5. **换码**：`POST /api/cohorts/2023/recodes`，请求体贴
   `samples/recode_rename.json`（计算机 → 计算机科学与技术，
   `base_version: 1`），返回 201 且 `catalog_version` 变为 2；
   再 `GET /api/cohorts/2023` 可见两表与核定数均已换成新码、版号为 2。
6. **旧版补报**：换码后再贴 `samples/supplements_prev_version.json`
   （`catalog_version: 1`，旧码计→计 50），应返回 201 且
   `converted_from_version: 1`——旧码按换码前清单与对照转成现行码后入库；
   不带版号的补报/录表此时会被 409 拒绝（只在首版受理）。

### 试两码互换

7. 换一届（如 `2024`）重新录表，然后 `POST /api/cohorts/2024/recodes`
   贴 `samples/recode_swap.json`（计算机 ↔ 数学，三学期成对互换），
   应返回 201；`GET /api/cohorts/2024` 可见两码数据整体换位。
   只列单边（如仅 `计算机 → 数学`）会因换后重码被 409 拒绝。

命令行等价操作：

```bash
curl -X POST http://localhost:8000/api/cohorts/2023/flows \
  -F "file_1_2=@samples/flows_1_2.csv" -F "file_2_3=@samples/flows_2_3.csv"
curl -X POST http://localhost:8000/api/cohorts/2023/supplements \
  -H "Content-Type: application/json" -d @samples/supplements_ok.json
curl -X POST http://localhost:8000/api/cohorts/2023/recodes \
  -H "Content-Type: application/json" -d @samples/recode_rename.json
curl -X POST http://localhost:8000/api/cohorts/2023/supplements \
  -H "Content-Type: application/json" -d @samples/supplements_prev_version.json
curl "http://localhost:8000/api/cohorts/2023/bounds" \
  --get --data-urlencode "from_major=计算机科学与技术" \
  --data-urlencode "to_major=计算机科学与技术"
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
| POST | `/api/cohorts/{id}/flows` | 录入/替换两份去向 CSV（multipart），须与已生效核定数联合可行；`catalog_version` 只收当前版，缺省仅首版受理 |
| POST | `/api/cohorts/{id}/supplements` | 成批补录/更正首末专业对核定人数，同对覆盖，整批校验通过才写入；`catalog_version` 可填当前版或上一版（旧码自动转现行码），缺省仅首版受理 |
| POST | `/api/cohorts/{id}/recodes` | 按届换码：`base_version` 须等于当前版；两表、核定数（保留原批次号）与版号同事务切换，失败沿用原版 |
| GET | `/api/cohorts/{id}/bounds?from_major=&to_major=` | 该首末对人数上下界，各附达到界值的三学期去向见证表（附目录版号） |
| GET | `/api/cohorts/{id}/table` | 任一满足全部生效汇总的三学期去向表及其二维汇总（附目录版号） |
| GET | `/api/cohorts/{id}` | 当前已生效数据（专业、两张旧表、全部核定数、目录版号） |
| GET | `/api/cohorts` / `/api/health` | 届别列表（含各届目录版号）/ 健康检查 |

## 并发与一致性设计

- SQLite 开 WAL：读快照不阻塞写入。
- 上下界查询先在**单个读事务**内取一致性快照，随后在内存中求解——
  整份答案（下界表 + 上界表）来自同一套已生效数据，且求解期间补报仍可入库。
- 所有写操作（录表、补报、换码）经应用级写锁串行化，并在单事务内完成
  “校验 + 写入”：换码、换表与补报交错时，结果与按入库顺序办理一致；
  与已生效数据联合可行才提交，否则整批回滚。
- 版号校验在写事务内读取当前版后进行，不存在“看到 v1、写入时已是 v2”
  的竞态；换码失败回滚，查询始终读到完整的某一版。
- 补报被拒时返回逐条诊断：简易上下界越界、首/末专业总人数超限，
  或逐条试算定位的矛盾条目。

## 目录结构

```
app/
  main.py     FastAPI 接口（录表、补报、换码、查询）
  domain.py   校验与模型组装（6 专业上限、二学期人数衔接、换码对照校验与转换）
  solver.py   三向运输问题整数求解（scipy/HiGHS）、见证表校验
  db.py       SQLite（WAL）、快照读、串行写事务、目录版号迁移与换码落库
  csvio.py    去向 CSV 解析
samples/      可直接在 Swagger 试用的样例（含换码改名、两码互换、旧版补报）
tests/        求解器手算案例 + API 端到端测试（含换码、版号口径与并发）
```

## 待补事项

- 无鉴权与操作审计（批次已留痕于 `batches` 表，但未记录操作人）。
- 核定数暂不支持删除（可以 0 覆盖达到同等效果）。
- 上一版补报只认**最近一次**换码的对照（更早版本的旧码不予受理，
  需先自行换算到上一版或现行码）。
- 换码档案（`recodes` 表）暂无查询接口，仅用于上一版补报转码。
- 上下界为精确整数规划解；专业数上限 6 时求解在毫秒级，若放宽上限
  需评估求解耗时（三向运输问题一般为 NP 难）。
