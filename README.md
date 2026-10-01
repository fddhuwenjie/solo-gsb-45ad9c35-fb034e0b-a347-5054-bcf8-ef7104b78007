# 微孔板样本谱系追踪 API

基于 **Python · FastAPI · SQLite** 的微孔板谱系台账服务：

- 登记孔位中的初始样本与体积；
- 一批移液操作**按提交顺序在单个事务中原子执行**：每次吸取按来源孔的组成占比带走各初始样本，目标孔按现有液体与新增液体重新混合；**任一步体积不足，整批回滚**；
- 孔位可设**可选最大容量**（默认不限，既有数据自动视为不限）：补液与批次移液在**每一步**按目标孔当时体积校验上限，超限即 409（批次整批回滚）；撤销前同样核对快照恢复后的各孔体积，任一孔超过当前上限即整次拒绝；容量设置/取消与补液、批次、撤销串行；
- 按孔查询当前体积、各初始样本占比，以及涉及该孔的完整批次记录；
- **孔位异常隔离**：操作员可对一组**已登记孔**凭 `case_id` / `reason` / `request_key` 原子建立隔离——同一事务确认全部孔尚未隔离后才落记录，任一孔已被（其他案件或同案件）占用即整次拒绝，不留部分状态；同键同目标重放返回首次结果、同键换案件/换孔集合返回 `409`；隔离孔不得补液、作为批次来源或目标、修改容量，也不得撤销触及它的已提交批次（冲突时液体、批次与审计均不变）；解除隔离同样携带 `case_id` 与 `request_key`，批量解除中任一孔未隔离或案件不符即整体拒绝；申请与解除审计永久保留，孔位与批次查询返回当前隔离信息；
- **误操作批次撤销**：成功提交的批次在同一事务保存其首次触及各孔的批前快照，可原子恢复；仅当批次已提交、尚未撤销且之后没有补液或后续批次触及这些孔时允许撤销，原批次保留并追加审计记录；
- 所有体积运算使用 `Decimal`（统一量化到小数点后 9 位），不使用 float；
- 通过 `request_key` 幂等去重：相同键重复提交不会重复执行，直接返回首个结果（失败批次同样被记录并消耗该键）；批次撤销同样以 `request_key` 幂等：同键同目标返回首次结果，同键换目标返回 `409`。

## 快速启动（Docker，唯一需要的命令）

```bash
docker compose up --build
```

启动后在本机访问：

| 地址 | 说明 |
| --- | --- |
| http://localhost:8000/health | 健康检查 |
| http://localhost:8000/docs | Swagger 交互式文档（可直接试调） |
| http://localhost:8000/redoc | ReDoc 文档 |

SQLite 数据落在命名卷 `lineage-data` 中，容器重启不丢失；`docker compose down -v` 可清空。

## 本地开发

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --port 8000   # 同样监听本机 8000 端口
pytest                                       # 运行测试
```

数据库文件默认在 `./data/lineage.db`，可用环境变量 `LINEAGE_DB` 指定其他路径。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/wells` | 登记孔位初始样本与体积（同孔重复登记视为补液混匀） |
| `PUT` | `/wells/{well}/capacity` | 设置或取消孔位最大容量（`null` 为不限；新上限低于当前体积返回 409） |
| `GET` | `/wells/{well}` | 孔位当前体积 + 容量上限 + 各初始样本的量与占比 |
| `GET` | `/wells/{well}/batches` | 涉及该孔的完整批次记录 |
| `POST` | `/batches` | 原子执行一批移液（`request_key` 幂等） |
| `GET` | `/batches` / `/batches/{id}` | 全部 / 单个批次记录（含失败批次） |
| `POST` | `/batches/{id}/reversals` | 撤销已提交批次：原子恢复批前快照（`request_key` 幂等） |
| `POST` | `/quarantines` | 按案件原子隔离一组已登记孔（`case_id` / `reason` / `request_key` 幂等） |
| `POST` | `/quarantines/releases` | 按案件原子解除一组隔离孔（`case_id` / `request_key` 幂等） |

孔位标签为「字母 + 数字」（如 `A1`、`B12`），不区分大小写；本服务按单块板建模。

## 调用示例

```bash
# 1. 登记初始样本：A1 加入 100 µL 样本 S1，A2 加入 60 µL 样本 S2
curl -s -X POST http://localhost:8000/wells \
  -H 'Content-Type: application/json' \
  -d '{"well": "A1", "sample": "S1", "volume": "100"}'
curl -s -X POST http://localhost:8000/wells \
  -H 'Content-Type: application/json' \
  -d '{"well": "A2", "sample": "S2", "volume": "60"}'

# 2. 原子批量移液：A1 -> B1 40，A2 -> B1 30（B1 混合为 S1:S2 = 4:3）
curl -s -X POST http://localhost:8000/batches \
  -H 'Content-Type: application/json' \
  -d '{
        "request_key": "run-2026-09-22-001",
        "operations": [
          {"source": "A1", "destination": "B1", "volume": "40"},
          {"source": "A2", "destination": "B1", "volume": "30"}
        ]
      }'

# 3. 查询 B1 的体积与谱系占比
curl -s http://localhost:8000/wells/B1
# => {"well":"B1","volume":"70","composition":[
#      {"sample":"S1","amount":"40","fraction":"0.571428571"},
#      {"sample":"S2","amount":"30","fraction":"0.428571429"}]}

# 4. 体积不足 -> 整批回滚，返回 409，状态不变
curl -s -X POST http://localhost:8000/batches \
  -H 'Content-Type: application/json' \
  -d '{
        "request_key": "run-2026-09-22-002",
        "operations": [
          {"source": "B1", "destination": "C1", "volume": "50"},
          {"source": "B1", "destination": "D1", "volume": "50"}
        ]
      }'
# => 409 {"detail":{"message":"孔位 B1 体积不足：可用 20，请求吸取 50", ...}}

# 5. 重复提交同一 request_key -> 200 且 replayed=true，不会重复执行
# 6. 查询涉及 B1 的全部批次
curl -s http://localhost:8000/wells/B1/batches

# 7. 撤销误操作批次（假设批次 id 为 1）：原子恢复批前快照，原批次保留
curl -s -X POST http://localhost:8000/batches/1/reversals \
  -H 'Content-Type: application/json' \
  -d '{"request_key": "undo-run-2026-09-22-001"}'
# 之后再有补液或后续批次触及该批孔位 -> 409 且 blocking_wells 列出后续操作
```

### 容量上限与冲突

```bash
# 8. 给 B1（当前 70 µL）设置上限 100；未设置过的孔位 max_capacity 为 null（不限）
curl -s -X PUT http://localhost:8000/wells/B1/capacity \
  -H 'Content-Type: application/json' \
  -d '{"max_capacity": "100"}'
# => {"well":"B1","volume":"70","max_capacity":"100","composition":[...]}

# 9. 补液超限 -> 409，返回超限孔位/当时体积/拟加入量/上限，液体不变
curl -s -X POST http://localhost:8000/wells \
  -H 'Content-Type: application/json' \
  -d '{"well": "B1", "sample": "S1", "volume": "50"}'
# => 409 {"detail":{"reason":"capacity_exceeded","well":"B1",
#         "volume":"70","adding":"50","max_capacity":"100", ...}}

# 10. 批次中某一步使目标孔超限 -> 整批回滚，同样返回上述四元组（失败批次落库并消耗 request_key）
curl -s -X POST http://localhost:8000/batches \
  -H 'Content-Type: application/json' \
  -d '{
        "request_key": "run-2026-09-30-001",
        "operations": [
          {"source": "A1", "destination": "B1", "volume": "40"}
        ]
      }'
# => 409 {"detail":{"reason":"capacity_exceeded","well":"B1",
#         "volume":"70","adding":"40","max_capacity":"100","batch_id":...}}
# 同一批次「先移出再移入」按每步当时体积校验：先 B1->C1 移出 40，再 A1->B1 移入 40 即可通过

# 11. 把上限调到当前体积（70）以下 -> 409 capacity_below_volume，配置不变
curl -s -X PUT http://localhost:8000/wells/B1/capacity \
  -H 'Content-Type: application/json' \
  -d '{"max_capacity": "10"}'
# => 409 {"detail":{"reason":"capacity_below_volume","well":"B1",
#         "volume":"70","max_capacity":"10", ...}}

# 12. 取消上限（恢复不限）
curl -s -X PUT http://localhost:8000/wells/B1/capacity \
  -H 'Content-Type: application/json' \
  -d '{"max_capacity": null}'

# 13. 撤销也核对容量：若某快照孔恢复后的体积超过其当前上限
#     -> 409 capacity_exceeded，exceeding_wells 逐孔列出恢复后体积与上限，
#        液体与审计记录不变；调大/取消上限后同 request_key 可重试
```

### 孔位异常隔离与冲突

```bash
# 14. 把一组已登记孔按案件原子隔离（reason 记录异常原因）
curl -s -X POST http://localhost:8000/quarantines \
  -H 'Content-Type: application/json' \
  -d '{
        "wells": ["B1", "C1"],
        "case_id": "CASE-2026-10-01-07",
        "reason": "移液器吸头疑似触碰孔壁，等待复测",
        "request_key": "quarantine-2026-10-01-001"
      }'
# => 201 {"action":"quarantine","quarantine_id":1,"case_id":"CASE-2026-10-01-07",
#         "wells":[{"well":"B1","active":true,"released_at":null,
#                   "release_request_key":null}, ...], ...}

# 15. 隔离孔禁止补液 / 批次（来源或目标）/ 改容量 / 撤销触及它的批次
#     -> 409 well_quarantined，逐个列出隔离孔、案件与隔离申请 request_key；
#        液体、批次与审计均不变，且批次 request_key 不被消耗
curl -s -X POST http://localhost:8000/wells \
  -H 'Content-Type: application/json' \
  -d '{"well": "B1", "sample": "S1", "volume": "5"}'
# => 409 {"detail":{"reason":"well_quarantined",
#         "quarantined_wells":[{"well":"B1","case_id":"CASE-2026-10-01-07",
#                              "quarantine_id":1,"request_key":"quarantine-2026-10-01-001"}], ...}}

# 16. 任一孔已被其他案件（或同一案件）隔离 -> 整次申请 409，同批其他孔不留部分状态
#     未登记孔则返回 404 unknown_well
curl -s -X POST http://localhost:8000/quarantines \
  -H 'Content-Type: application/json' \
  -d '{"wells": ["A1", "B1"], "case_id": "CASE-OTHER",
       "reason": "另一案件", "request_key": "quarantine-2026-10-01-002"}'
# => 409 {"detail":{"reason":"already_quarantined",
#         "quarantined_wells":[{"well":"B1","case_id":"CASE-2026-10-01-07", ...}]}}

# 17. 同键同目标重放 -> 200 replayed=true；同键换案件或换孔集合 -> 409
curl -s -X POST http://localhost:8000/quarantines \
  -H 'Content-Type: application/json' \
  -d '{"wells": ["B1"], "case_id": "CASE-OTHER",
       "reason": "换目标", "request_key": "quarantine-2026-10-01-001"}'
# => 409 {"detail":{"reason":"request_key_conflict","action":"quarantine",
#         "existing_case_id":"CASE-2026-10-01-07","existing_wells":["B1","C1"]}}

# 18. 查询孔位/批次即返回当前隔离信息（GET /wells/B1 的 "quarantine" 字段；
#     GET /wells/B1/batches 顶层 "quarantine" 与每个批次的 "quarantined_wells"）
curl -s http://localhost:8000/wells/B1
# => {"well":"B1","volume":"70",...,"quarantine":{"case_id":"CASE-2026-10-01-07",
#         "reason":"...","quarantine_id":1,"request_key":"quarantine-2026-10-01-001",
#         "quarantined_at":"2026-10-01T..."}}

# 19. 解除隔离：每个孔都必须当前由该 case_id 隔离；任一不匹配整批拒绝（409，
#     mismatched_wells 逐孔给出 not_quarantined / case_mismatch）
curl -s -X POST http://localhost:8000/quarantines/releases \
  -H 'Content-Type: application/json' \
  -d '{"wells": ["B1", "C1"], "case_id": "CASE-2026-10-01-07",
       "request_key": "release-2026-10-01-001"}'
# => 201 {"action":"release","release_id":1,"wells":[{"well":"B1",
#         "quarantine_request_key":"quarantine-2026-10-01-001","reason":"...", ...}]}
# 解除同样幂等：同键同目标 -> 200 replayed=true；换目标 -> 409 request_key_conflict。
# 成功后孔位/批次查询不再返回隔离信息；申请与解除审计均保留（重放申请可看到
# released_at 与 release_request_key），解除后可被新案件重新隔离。
```

## 设计要点

- **Decimal 精度**：体积与组分量在库中以 TEXT 存 Decimal 字符串，运算全部走 `decimal.Decimal`，结果统一量化到 9 位小数（`ROUND_HALF_UP`），接口返回值中的数字均为字符串以避免 JSON 精度损失。
- **按比例吸取（整数单位最大余量法）**：从孔中吸取体积 `v`（孔总量 `V`，均以 9 位小数的最小计量单位 QUANTUM 计）时，每个初始样本先分得比例份额 `amount × v/V` 的整数单位，剩余单位按小数余量从大到小分配；余量相同按样本 ID 升序。任何样本分得的单位数不超过其原有量，且各样本带走量之和恰好等于 `v`——保证每一步之后来源孔与目标孔都满足「组分量之和 = 体积」；目标孔将各组分量累加，即视为立即完美混合。
- **原子性**：一个批次的所有操作在单个 SQLite 事务中按序执行，任一步 `InsufficientVolume` 或 `CapacityExceeded` 即回滚；失败批次以 `status="failed"` 落库（操作明细随回滚清空），方便审计。
- **容量上限**：`wells.max_capacity` 以 TEXT 存 Decimal 字符串，`NULL` 表示不限——老库启动时经 `ALTER TABLE` 就地补列，既有数据一律为 `NULL`，行为完全保留。`PUT /wells/{well}/capacity` 设置（正数）或取消（`null`）上限；新上限低于当前体积返回 `409 capacity_below_volume` 且配置不变。补液与批次移液在**每一步**按目标孔当时体积校验上限：同一批次内「先移出再移入」因此可行，任一步超限整批回滚，409 载荷含超限孔位、当时体积、拟加入量与上限（批次场景失败批次同样落库并消耗 `request_key`）。撤销在恢复快照前核对各快照孔恢复后的体积，任一孔超过其**当前**上限即整次拒绝（`409 capacity_exceeded`，`exceeding_wells` 逐孔列出恢复后体积与上限），液体与审计记录不变；容量是孔位配置而非快照内容，恢复快照不改变上限。
- **并发串行（`BEGIN IMMEDIATE`）**：补液、批次提交、撤销、容量变更四个变更端点从事务起点（任何校验读之前）即取 SQLite 保留写锁，使「校验 → 写入 → 提交」全程互斥：后到的变更在取锁处等待，先到者提交后才读库，因此必然看到已提交的最新状态（先提交者决定后续校验看到的状态）。这消除了撤销的丢失更新——撤销完成阻塞检查后、恢复快照前若有补液先提交，撤销在锁内重检到该补液事件并返回 `409 blocked`（列出阻塞孔位与补液操作），不会以旧快照覆盖；反之撤销先提交时，补液在锁上等待，随后叠加在恢复后的状态上（含撤销中被删除、随后补液重建的孔）。两种顺序均不丢失组分。容量变更同理：补液先提交则容量校验看到补液后的体积，容量先提交则补液/批次/撤销看到新上限。
- **幂等**：`batches.request_key` 有唯一约束。重复提交返回首个批次记录（`replayed=true`，HTTP 200）；并发下同键请求先在锁内复核、再由唯一约束兜底，不会执行两次。
- **批次撤销**：每个成功提交的批次在**同一事务**内对其首次触及的每个孔（来源与目标去重）保存批前快照——孔体积与各初始样本量，孔不存在则记 `NULL`。撤销时（`POST /batches/{id}/reversals`，请求体携带独立的 `request_key`）：
  - 仅当批次 `committed`、尚未撤销、存在快照，该批之后没有补液或已提交批次触及任一快照孔，且快照恢复后的各孔体积不超过其当前容量上限时才允许；条件不符返回 `409`，`reason` 取 `not_committed` / `already_reversed` / `no_snapshot` / `blocked` / `capacity_exceeded` / `request_key_conflict`；`blocked` 时在 `blocking_wells` 中逐孔列出后续操作（`top_up` 含样本与体积，`batch` 含后续批次 id 与 request_key），`capacity_exceeded` 时在 `exceeding_wells` 中逐孔列出恢复后体积与当前上限，**液体状态保持不变**；
  - 撤销在单事务内把所有快照孔恢复到批前状态（批次中新建的孔整体删除，先删组成再删孔以满足外键），并向 `reversals` 追加一条关联原批次的审计记录；**原批次与其操作明细不删除**，批次记录新增 `reversed` / `reversed_at` 字段；
  - 撤销 `request_key` 幂等：同键同目标返回首次撤销结果（`replayed=true`，HTTP 200），同键换目标返回 `409`；
  - 上线前已提交、没有快照的批次明确返回 `409 no_snapshot`，不可撤销。
  - 「后续操作」的判定基于孔位事件表 `well_events`（补液与批次提交各记一条，自增 id 即发生顺序）：以**本批次在该孔上的事件 id** 为基准，只统计之后的事件。
- **孔位异常隔离**：三张表——`quarantine_requests`（申请：`request_key` 唯一、案件、原因）、`quarantine_releases`（解除：`request_key` 唯一、案件）、`quarantines`（每孔一条明细，`released_at IS NULL` 即隔离中，解除只置 `released_at`/`release_id` 不删行；`well` 外键 `ON DELETE CASCADE`，使撤销批次删除批次新建孔时其隔离明细在同一撤销事务内随孔清除）。
  - 申请（`POST /quarantines`）在单个 `BEGIN IMMEDIATE` 事务内：先确认全部孔已登记（否则 `404 unknown_well`）、全部孔当前无有效隔离（否则 `409 already_quarantined`，`quarantined_wells` 列出占用孔与其案件/隔离键），再插入申请行与每孔明细——任一条件不符整事务回滚，**不留下申请行或部分孔隔离**，`request_key` 也不被消耗；
  - 解除（`POST /quarantines/releases`）同事务核对每个孔当前有效隔离且案件匹配：未隔离/未登记为 `not_quarantined`、案件不符为 `case_mismatch`（`mismatched_wells` 逐孔列出），任一不匹配整体回滚，任何孔都不解除；解除可横跨同一案件的多次申请；
  - 幂等语义与撤销一致：隔离/解除键各自独立命名空间，同键同案件且**孔集合相同**（与顺序无关）返回首次结果（`replayed=true`，HTTP 200），换案件或换孔集合返回 `409 request_key_conflict`（载荷带原案件与原孔集合）；冲突回滚不消耗键；
  - 隔离孔在补液、批次（执行前对全部来源/目标孔一次性校验，先于批次行写入与快照保存）、容量变更、批次撤销四处被拒绝（`409 well_quarantined`，`quarantined_wells` 列出涉及孔）：冲突事务整体回滚，液体、批次、隔离与审计均不变；批次场景**不**落失败批次、**不**消耗批次 `request_key`，撤销场景同键在隔离解除后可重试；
  - 查询：`GET /wells/{well}` 返回 `quarantine`（案件、原因、隔离键、隔离时间）或 `null`；`GET /wells/{well}/batches` 顶层带 `quarantine`，批次载荷新增 `quarantined_wells`（仅列当前仍隔离的相关孔），解除后这些字段同步消失。申请与解除审计永久保留（解除后重放申请可见 `active=false`、`released_at` 与 `release_request_key`），解除后孔可被新案件重新隔离；
  - 隔离/解除与补液、批次、撤销、容量变更共用同一把保留写锁串行：先提交者决定后提交者看到的隔离状态。
- **状态码**：登记/批次成功/撤销成功/隔离申请成功/解除成功 `201`；容量设置/取消成功 `200`；幂等重放 `200`；体积不足、容量超限、撤销冲突或触及隔离孔 `409`；孔位/批次不存在（含隔离未登记孔）`404`；参数非法 `422`。

## 项目结构

```
app/
  main.py     # FastAPI 路由
  ledger.py   # 谱系台账核心逻辑（Decimal 运算、事务内批量执行）
  db.py       # SQLite 连接与建表
  schemas.py  # 请求体验证
tests/        # pytest 端到端测试
Dockerfile / compose.yaml
```
