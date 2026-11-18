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
- 序证明含运单、有序 hop id 列表、内容哈希（对序敏感的 SHA-256）、投影值、本批写入键，只读接口：
  - `GET /api/order-proofs?waybill_id=...` 或 `?batch_id=...`
  - `GET /api/batches/{batch_id}/order-proofs`
  - `GET /api/waybills/{id}/timeline` 同时返回 `order_proof` 与每点的 `late_insert` 迟到标记。
- 夹具：`fixtures/ingest_batch_ooo.json` 为乱序到达批次（到达顺序与 `event_ts` 相反，含同时间戳按 id 决胜）。装载页可分别触发基础/乱序批次。
- 后端测试：`cd backend && pytest`（`tests/test_ooo_hop_insert.py`，8 用例）。
