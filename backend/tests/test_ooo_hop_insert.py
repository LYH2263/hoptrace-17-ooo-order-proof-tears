"""ooo_hop_insert：乱序有序装载 / 持久化 / 失败闭 / 序证明 端到端测试。"""
from app.db import connect
from app.modules import ooo_hop_insert
from app.seed import init_db


def _paths(client):
    import app.db as db_mod
    return db_mod.DB_PATH


def test_seed_builds_ordered_persistent_timeline_and_proof(client):
    """种子装载即建持久点表与序证明，且序按 (event_ts, id)。"""
    with connect() as conn:
        pts = ooo_hop_insert.list_timeline_points(conn, "WB-1001")
        assert [p["id"] for p in pts] == ["H-1", "H-2", "H-3"]
        assert [p["ord_position"] for p in pts] == [0, 1, 2]
        proof = ooo_hop_insert.latest_order_proof(conn, "WB-1001")
        assert proof["ordered_hop_ids"] == ["H-1", "H-2", "H-3"]
        assert proof["proj_value"] == "到站"
        assert proof["last_hop_id"] == "H-3"
        assert proof["batch_id"] == "SEED"
        assert len(proof["content_hash"]) == 64
        # 投影与证明末点一致
        wb = conn.execute("SELECT status_proj FROM waybills WHERE id='WB-1001'").fetchone()
        assert wb["status_proj"] == "到站"


def test_out_of_order_batch_inserts_into_correct_positions(client):
    """乱序到达：更早点必须插入正确位置，禁止追加末尾；末点投影回到真实末事件。"""
    resp = client.post("/api/ingest/run", json={"fixture_name": "ingest_batch_ooo.json"})
    assert resp.json()["ok"] is True

    with connect() as conn:
        pts = ooo_hop_insert.list_timeline_points(conn, "WB-1001")
        ids = [p["id"] for p in pts]
        # H-8(15:00) 插到 H-2(12:40) 与 H-3(18:05) 之间；
        # H-9 与 H-3 同为 18:05，按 id 决胜 H-3 在 H-9 前；H-7(20:10) 居末
        assert ids == ["H-1", "H-2", "H-8", "H-3", "H-9", "H-7"]
        ts = [p["event_ts"] for p in pts]
        assert ts == sorted(ts)  # event_ts 单调不减
        # 到达序（落库先后）与时间线序不一致 —— 证明是“插入”而非“追加”
        late = {p["id"]: p["late_insert"] for p in pts}
        assert late["H-8"] is True
        assert late["H-9"] is True
        assert late["H-7"] is False  # 最末点虽最后到达，但本就属于末尾

        proof = ooo_hop_insert.latest_order_proof(conn, "WB-1001")
        assert proof["batch_id"] == "B-OOO-02"
        assert proof["ordered_hop_ids"] == ids
        assert proof["last_hop_id"] == "H-7"
        assert proof["proj_value"] == "已签收"
        wb = conn.execute("SELECT status_proj FROM waybills WHERE id='WB-1001'").fetchone()
        assert wb["status_proj"] == "已签收"


def test_order_survives_reopen_and_is_in_table_not_memory(client):
    """刷新/重开连接后顺序仍在：持久化在 timeline_points，而非内存重排。"""
    client.post("/api/ingest/run", json={"fixture_name": "ingest_batch_ooo.json"})
    db_path = _paths(client)

    # 全新连接，且不做任何重排，直接读点表
    import sqlite3
    fresh = sqlite3.connect(db_path)
    fresh.row_factory = sqlite3.Row
    rows = fresh.execute(
        "SELECT hop_id FROM timeline_points WHERE waybill_id='WB-1001' ORDER BY ord_position"
    ).fetchall()
    assert [r["hop_id"] for r in rows] == ["H-1", "H-2", "H-8", "H-3", "H-9", "H-7"]
    # hops 原始表的物理落库顺序仍是乱序（证明序来自点表维护，而非追加）
    physical = fresh.execute(
        "SELECT id FROM hops WHERE waybill_id='WB-1001' ORDER BY rowid"
    ).fetchall()
    physical_ids = [r["id"] for r in physical]
    assert physical_ids[-3:] == ["H-7", "H-9", "H-8"]
    fresh.close()


def test_failure_closure_rolls_back_timeline_projection_and_proof(client):
    """同一失败闭：物化后任一步失败，点序、投影、证明全部回滚，无半截状态。"""
    with connect() as conn:
        before_ids = [
            r["hop_id"] for r in conn.execute(
                "SELECT hop_id FROM timeline_points WHERE waybill_id='WB-1001' ORDER BY ord_position"
            ).fetchall()
        ]
        before_proj = conn.execute(
            "SELECT status_proj FROM waybills WHERE id='WB-1001'"
        ).fetchone()["status_proj"]
        before_proofs = conn.execute(
            "SELECT COUNT(*) AS c FROM order_proofs WHERE waybill_id='WB-1001'"
        ).fetchone()["c"]

    hops = [
        {
            "id": "H-50", "waybill_id": "WB-1001",
            "event_ts": "2026-11-17T13:00:00Z",
            "location_code": "X", "event_code": "IN_TRANSIT",
        }
    ]
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        ooo_hop_insert.apply_batch(conn, "B-FAIL", hops, "2026-11-17T21:00:00Z")
        # 物化与证明已在事务内写入，此时模拟后续失败
        raise RuntimeError("boom: 模拟提交前失败")
    except RuntimeError:
        conn.rollback()
    finally:
        conn.close()

    with connect() as conn:
        after_ids = [
            r["hop_id"] for r in conn.execute(
                "SELECT hop_id FROM timeline_points WHERE waybill_id='WB-1001' ORDER BY ord_position"
            ).fetchall()
        ]
        after_proj = conn.execute(
            "SELECT status_proj FROM waybills WHERE id='WB-1001'"
        ).fetchone()["status_proj"]
        after_proofs = conn.execute(
            "SELECT COUNT(*) AS c FROM order_proofs WHERE waybill_id='WB-1001'"
        ).fetchone()["c"]
        assert after_ids == before_ids
        assert "H-50" not in after_ids
        assert after_proj == before_proj
        assert after_proofs == before_proofs


def test_idempotent_replay_does_not_duplicate_or_reorder(client):
    """重复回放同一批：INSERT OR IGNORE 生效，序与投影保持稳定。"""
    payload = {"fixture_name": "ingest_batch_ooo.json"}
    first = client.post("/api/ingest/run", json=payload).json()["report"]
    second = client.post("/api/ingest/run", json=payload).json()["report"]
    assert first["written"] == 5
    assert second["written"] == 0  # 全部幂等忽略

    with connect() as conn:
        pts = ooo_hop_insert.list_timeline_points(conn, "WB-1001")
        ids = [p["id"] for p in pts]
        assert ids == ["H-1", "H-2", "H-8", "H-3", "H-9", "H-7"]
        # hops 表无重复行
        dup = conn.execute(
            "SELECT COUNT(*) AS c FROM hops WHERE waybill_id='WB-1001'"
        ).fetchone()["c"]
        assert dup == len(ids)


def test_order_proof_read_apis_by_waybill_and_batch(client):
    """序证明只读接口：按运单 / 批次查询，且内容哈希对序敏感。"""
    client.post("/api/ingest/run", json={"fixture_name": "ingest_batch_ooo.json"})

    by_batch = client.get("/api/order-proofs?batch_id=B-OOO-02").json()
    assert {r["waybill_id"] for r in by_batch} == {"WB-1001", "WB-1002"}
    assert all(r["batch_id"] == "B-OOO-02" for r in by_batch)

    by_wb = client.get("/api/order-proofs?waybill_id=WB-1002").json()
    assert by_wb[0]["ordered_hop_ids"] == ["H-4", "H-11", "H-6"]
    assert by_wb[0]["proj_value"] == "派送中"

    by_route = client.get("/api/batches/B-OOO-02/order-proofs").json()
    assert len(by_route) == len(by_batch)

    # 哈希对序敏感：交换两个点的位置哈希必须改变
    with connect() as conn:
        base = ooo_hop_insert.latest_order_proof(conn, "WB-1001")
        pts = ooo_hop_insert._ordered_primary_hops(conn, "WB-1001")
        pts[0], pts[1] = pts[1], pts[0]
        swapped_hash = ooo_hop_insert.compute_content_hash(pts)
        assert swapped_hash != base["content_hash"]


def test_timeline_endpoint_carries_order_proof(client):
    """时间线接口返回持久点表序与序证明，供测绘板竖轴/检查器渲染。"""
    client.post("/api/ingest/run", json={"fixture_name": "ingest_batch_ooo.json"})
    body = client.get("/api/waybills/WB-1001/timeline").json()
    assert [h["id"] for h in body["hops"]] == [
        "H-1", "H-2", "H-8", "H-3", "H-9", "H-7"
    ]
    assert body["hops"][2]["late_insert"] is True  # H-8 后到先插
    assert body["order_proof"]["batch_id"] == "B-OOO-02"
    assert body["order_proof"]["last_hop_id"] == "H-7"
    assert body["waybill"]["status_proj"] == "已签收"


def test_backfill_existing_db_without_timeline_table(client):
    """旧库升级路径：timeline_points 为空时 init_db 回填有序时间线与证明。"""
    # 清空点表模拟旧库，再触发 init_db
    with connect() as conn:
        conn.execute("DELETE FROM timeline_points")
        conn.execute("DELETE FROM order_proofs")
        conn.commit()
    init_db()
    with connect() as conn:
        pts = ooo_hop_insert.list_timeline_points(conn, "WB-1001")
        assert [p["id"] for p in pts] == ["H-1", "H-2", "H-3"]
        proof = ooo_hop_insert.latest_order_proof(conn, "WB-1001")
        assert proof["batch_id"] == "BACKFILL"
