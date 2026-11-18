# 途迹（Hoptrace）

轨迹点装载 → 运单时间线物化 → 迟到旁路与脏点隔离 → 状态证明互证。

**不是** Feed 归并、CDC 游标 Sink、质检签发、数仓分层、交班过户、正冲净额。  
**也不是** 电商订单 / 仓库库存 / 外卖工单 CRUD——去皮后是事件装载与有序物化。

## 启动

```bash
# 后端
cd backend
pip install -r requirements.txt
set DATA_DIR=%CD%\data
set FIXTURES_DIR=%CD%\..\fixtures
uvicorn app.main:app --host 127.0.0.1 --port 10800

# 前端
cd frontend
npm install
npm run dev
```

| 入口 | 地址 |
| --- | --- |
| 前端 | http://127.0.0.1:5800 |
| 接口 | http://127.0.0.1:10800 |

## UI

三栏测绘板：顶品牌条 + 中竖轴时间线 + 右检查器 + 底 hop 点阵。  
不使用左轨编号导航、不使用顶栏流程 ribbon。

## 乱序装载（ooo_hop_insert）

- 正式点按 `(event_ts, id)` 物化进持久点表 `timeline_points`（独立时间线点表，按运单维护 `ord_position`），刷新/重开不乱序，不追加末尾。
- 点序、投影 `waybills.status_proj`、状态证明、序证明 `order_proofs` 在同一事务内同成同败。
- 提交形态：**同事务重排时间线并重算投影与序证明，缺一回滚**。提交前回读持久态跑三方核证闸门——竖轴序 = 证明序 = 投影派生源（末有效 hop 映射），任一检查不成立即抛 `OrderTearError` 整体回滚，装载回执 `ok=false, torn=true`，禁止半截重排对外可查。
- 序撕裂核证实体 `order_tear_attestations`（基线所无）：运单、竖轴/证明有序 hop id、新旧序内容哈希、投影、批次键、撕裂结论（一致/撕裂）与逐项检查明细，只追加不改写。只读接口：
  - `GET /api/order-tear-attestations?waybill_id=...` 或 `?batch_id=...`
  - `GET /api/batches/{batch_id}/order-tear-attestations`
  - `GET /api/waybills/{id}/timeline` 同时返回 `order_proof`、`attestation` 与实时核证 `live_alignment`。
- 序证明含运单、有序 hop id 列表、内容哈希（对序敏感的 SHA-256）、投影值、本批写入键，只读接口：
  - `GET /api/order-proofs?waybill_id=...` 或 `?batch_id=...`
  - `GET /api/batches/{batch_id}/order-proofs`
- 测绘板竖轴（`Board.vue`）与证明核对页（`Proofs.vue`）同读 `order_tear_attestations`，并在前端本地再核竖轴/证明/核证三方同序。
- 夹具：`fixtures/ingest_batch_ooo.json` 为乱序到达批次（到达顺序与 `event_ts` 相反，含同时间戳按 id 决胜）。装载页可分别触发基础/乱序批次。
- 后端测试：`cd backend && pytest`（`tests/test_ooo_hop_insert.py`，17 用例，含乱序夹具成功对照与三类撕裂故障注入失败对照：证明写写入序、投影停滞、证明行缺失）。
