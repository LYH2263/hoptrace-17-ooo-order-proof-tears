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


# ---------------------------------------------------------------------------
# 序撕裂核证实体 + 三方核证闸门：成功 / 失败对照（乱序到达夹具）
# ---------------------------------------------------------------------------

def _run_ooo(client):
    resp = client.post("/api/ingest/run", json={"fixture_name": "ingest_batch_ooo.json"})
    assert resp.json()["ok"] is True
    return resp.json()["report"]


def test_attestation_entity_written_with_ordered_hops_hash_projection_batch(client):
    """成功装载落序撕裂核证：运单/有序 hop id/新旧内容哈希/投影/批次键/结论齐全且结论一致。"""
    report = _run_ooo(client)
    assert {a["tear_verdict"] for a in report["attestations"]} == {"一致"}
    assert all(a["aligned"] is True for a in report["attestations"])

    with connect() as conn:
        att = ooo_hop_insert.latest_attestation(conn, "WB-1001")
        assert att is not None
        assert att["waybill_id"] == "WB-1001"
        assert att["batch_id"] == "B-OOO-02"
        assert att["tear_verdict"] == "一致"
        assert att["aligned"] is True
        expected_ids = ["H-1", "H-2", "H-8", "H-3", "H-9", "H-7"]
        assert att["axis_hop_ids"] == expected_ids
        # 核证里证明序必须与竖轴序完全相同（同实体互证）
        assert att["proof_hop_ids"] == expected_ids
        assert att["axis_content_hash"] == att["proof_content_hash"]
        assert att["last_hop_id"] == "H-7"
        assert att["proj_value"] == "已签收"
        assert len(att["axis_content_hash"]) == 64
        # 旧序哈希（装载前 H-1,H-2,H-3 的指纹）与新序哈希都留痕
        seed_proof = conn.execute(
            "SELECT content_hash FROM order_proofs "
            "WHERE waybill_id='WB-1001' AND batch_id='SEED' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert att["prior_content_hash"] == seed_proof["content_hash"]
        assert att["prior_content_hash"] != att["axis_content_hash"]
        # 核证检查项全部成立
        assert all(att["detail"]["checks"].values())


def test_attestation_read_apis_by_waybill_and_batch(client):
    """核证只读接口：按运单 / 批次键 / 批次子路由三种入口同读一张核证表。"""
    _run_ooo(client)

    by_batch = client.get("/api/order-tear-attestations?batch_id=B-OOO-02").json()
    assert {r["waybill_id"] for r in by_batch} == {"WB-1001", "WB-1002"}
    assert all(r["batch_id"] == "B-OOO-02" for r in by_batch)

    by_wb = client.get("/api/order-tear-attestations?waybill_id=WB-1002").json()
    assert by_wb[0]["axis_hop_ids"] == ["H-4", "H-11", "H-6"]
    assert by_wb[0]["proj_value"] == "派送中"

    by_route = client.get("/api/batches/B-OOO-02/order-tear-attestations").json()
    assert {r["id"] for r in by_route} == {r["id"] for r in by_batch}


def test_fault_proof_in_arrival_order_tears_and_whole_batch_rolls_back(client):
    """失败对照①：证明退回写入序 → 核证闸门判撕裂，回执失败且整批回滚无半截态。"""
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
        before_atts = conn.execute(
            "SELECT COUNT(*) AS c FROM order_tear_attestations WHERE waybill_id='WB-1001'"
        ).fetchone()["c"]

    ooo_hop_insert.install_fault(proof_arrival_order=True)
    try:
        resp = client.post(
            "/api/ingest/run", json={"fixture_name": "ingest_batch_ooo.json"}
        ).json()
    finally:
        ooo_hop_insert.clear_fault()

    # 撕裂时禁止回执成功
    assert resp["ok"] is False
    assert resp.get("torn") is True
    assert "序撕裂核证未通过" in resp["error"]

    with connect() as conn:
        # 竖轴、投影、证明、核证全部停在批前状态——无半截重排对外可查
        after_ids = [
            r["hop_id"] for r in conn.execute(
                "SELECT hop_id FROM timeline_points WHERE waybill_id='WB-1001' ORDER BY ord_position"
            ).fetchall()
        ]
        assert after_ids == before_ids
        assert "H-8" not in after_ids
        assert conn.execute(
            "SELECT status_proj FROM waybills WHERE id='WB-1001'"
        ).fetchone()["status_proj"] == before_proj
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM order_proofs WHERE waybill_id='WB-1001'"
        ).fetchone()["c"] == before_proofs
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM order_tear_attestations WHERE waybill_id='WB-1001'"
        ).fetchone()["c"] == before_atts
        # 故障批的原始点同样回滚，重放成功批仍可全部写入
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM hops WHERE waybill_id='WB-1001'"
        ).fetchone()["c"] == len(before_ids)

    # 闸门解除后同一乱序夹具可成功，证明序与竖轴序一致
    ok = client.post(
        "/api/ingest/run", json={"fixture_name": "ingest_batch_ooo.json"}
    ).json()
    assert ok["ok"] is True
    with connect() as conn:
        aligned, detail = ooo_hop_insert.verify_alignment(conn, "WB-1001")
        assert aligned is True
        assert detail["checks"]["axis_eq_proof_ids"] is True


def test_fault_stale_projection_tears_and_rolls_back(client):
    """失败对照②：投影不随末有效 hop 变 → proj_follows_last 不成立，整批回滚。"""
    ooo_hop_insert.install_fault(stale_projection=True)
    try:
        resp = client.post(
            "/api/ingest/run", json={"fixture_name": "ingest_batch_ooo.json"}
        ).json()
    finally:
        ooo_hop_insert.clear_fault()

    assert resp["ok"] is False
    assert resp.get("torn") is True

    with connect() as conn:
        # 投影仍是种子末点（到站），未被污染成半新半旧
        assert conn.execute(
            "SELECT status_proj FROM waybills WHERE id='WB-1001'"
        ).fetchone()["status_proj"] == "到站"
        # 竖轴也回滚，未出现“轴已新、投影仍旧”的半截态
        ids = [
            r["hop_id"] for r in conn.execute(
                "SELECT hop_id FROM timeline_points WHERE waybill_id='WB-1001' ORDER BY ord_position"
            ).fetchall()
        ]
        assert ids == ["H-1", "H-2", "H-3"]


def test_fault_missing_proof_row_tears_and_rolls_back(client):
    """失败对照③：点序已变而序证明行缺失 → proof_row_present 不成立，禁止回执成功。"""
    ooo_hop_insert.install_fault(skip_proof_row=True)
    try:
        resp = client.post(
            "/api/ingest/run", json={"fixture_name": "ingest_batch_ooo.json"}
        ).json()
    finally:
        ooo_hop_insert.clear_fault()

    assert resp["ok"] is False
    assert resp.get("torn") is True

    with connect() as conn:
        # 仅保留种子证明行，没有故障批留下的半截证明/竖轴
        rows = conn.execute(
            "SELECT batch_id FROM order_proofs WHERE waybill_id='WB-1001'"
        ).fetchall()
        assert {r["batch_id"] for r in rows} == {"SEED"}
        ids = [
            r["hop_id"] for r in conn.execute(
                "SELECT hop_id FROM timeline_points WHERE waybill_id='WB-1001' ORDER BY ord_position"
            ).fetchall()
        ]
        assert ids == ["H-1", "H-2", "H-3"]


def test_gate_error_type_rolls_back_under_direct_transaction(client):
    """核证闸门抛 OrderTearError（RuntimeError 子类），直接走事务时同样整体回滚。"""
    ooo_hop_insert.install_fault(proof_arrival_order=True)
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            ooo_hop_insert.apply_batch(
                conn,
                "B-DIRECT-TEAR",
                [
                    {
                        "id": "H-60",
                        "waybill_id": "WB-1001",
                        "event_ts": "2026-11-17T07:00:00Z",
                        "location_code": "X",
                        "event_code": "PICKED",
                    }
                ],
                "2026-11-17T21:00:00Z",
            )
            raised = False
        except ooo_hop_insert.OrderTearError:
            raised = True
        conn.rollback()
    finally:
        ooo_hop_insert.clear_fault()
        conn.close()
    assert raised is True

    with connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM hops WHERE id='H-60'"
        ).fetchone()["c"] == 0


def test_timeline_endpoint_carries_attestation_and_live_alignment(client):
    """时间线接口同时给竖轴、序证明与核证实体，且实时核证一致——测绘板与核对页同源。"""
    _run_ooo(client)
    body = client.get("/api/waybills/WB-1001/timeline").json()
    assert body["attestation"]["batch_id"] == "B-OOO-02"
    assert body["attestation"]["tear_verdict"] == "一致"
    assert body["attestation"]["axis_hop_ids"] == [
        h["id"] for h in body["hops"]
    ]
    assert body["attestation"]["proof_hop_ids"] == [
        h["id"] for h in body["hops"]
    ]
    assert body["live_alignment"]["aligned"] is True
    assert body["live_alignment"]["detail"]["checks"]["proj_follows_last"] is True
    # 序证明与核证指纹互证
    assert body["order_proof"]["content_hash"] == body["attestation"]["proof_content_hash"]


def test_replay_after_success_keeps_single_attestation_per_rebuild(client):
    """幂等回放仍重算并核证：每运单每批追加一条一致核证，结论恒为一致、无撕裂行。"""
    _run_ooo(client)
    replay = client.post(
        "/api/ingest/run", json={"fixture_name": "ingest_batch_ooo.json"}
    ).json()
    assert replay["ok"] is True
    with connect() as conn:
        rows = ooo_hop_insert.query_attestations(conn, batch_id="B-OOO-02")
        assert len(rows) == 4  # 两运单各重建两次
        assert all(r["aligned"] for r in rows)
        torn = conn.execute(
            "SELECT COUNT(*) AS c FROM order_tear_attestations WHERE aligned=0"
        ).fetchone()["c"]
        assert torn == 0


def test_attestation_writer_can_express_tear_verdict_in_draft(client):
    """核证实体必须能表达“撕裂”结论（草稿区用法）：不一致明细落出 aligned=0/撕裂。"""
    with connect() as conn:
        # 手工构造竖轴序与证明序不一致的明细，验证结论判定分支
        ok_detail = {
            "axis_hop_ids": ["H-1", "H-2"],
            "axis_content_hash": "a" * 64,
            "checks": {
                "proof_row_present": True,
                "axis_eq_proof_ids": True,
                "axis_eq_proof_hash": True,
                "last_hop_matches": True,
                "proj_follows_last": True,
            },
            "proof_hop_ids": ["H-1", "H-2"],
            "proof_content_hash": "a" * 64,
            "last_hop_id": "H-2",
            "proj_value": "运输中",
        }
        ok_row = ooo_hop_insert._write_attestation(
            conn, "WB-1001", "B-DRAFT-OK", None, ok_detail, "2026-11-18T00:00:00Z"
        )
        assert ok_row["aligned"] is True
        assert ok_row["tear_verdict"] == "一致"

        torn_detail = dict(
            ok_detail,
            checks=dict(
                ok_detail["checks"],
                axis_eq_proof_ids=False,
                axis_eq_proof_hash=False,
            ),
            proof_hop_ids=["H-2", "H-1"],
            proof_content_hash="b" * 64,
        )
        torn_row = ooo_hop_insert._write_attestation(
            conn, "WB-1001", "B-DRAFT-TORN", "c" * 64, torn_detail,
            "2026-11-18T00:01:00Z",
        )
        assert torn_row["aligned"] is False
        assert torn_row["tear_verdict"] == "撕裂"
        assert torn_row["prior_content_hash"] == "c" * 64
        conn.rollback()  # 草稿核证不进入正式态


def test_seed_and_backfill_also_produce_attestations(client):
    """种子与旧库回填路径同样过核证闸门并留核证实体。"""
    with connect() as conn:
        seed_att = ooo_hop_insert.latest_attestation(conn, "WB-1001")
        assert seed_att["batch_id"] == "SEED"
        assert seed_att["tear_verdict"] == "一致"
        assert seed_att["axis_hop_ids"] == ["H-1", "H-2", "H-3"]

        conn.execute("DELETE FROM order_tear_attestations")
        conn.execute("DELETE FROM timeline_points")
        conn.execute("DELETE FROM order_proofs")
        conn.commit()
    init_db()
    with connect() as conn:
        att = ooo_hop_insert.latest_attestation(conn, "WB-1001")
        assert att["batch_id"] == "BACKFILL"
        assert att["aligned"] is True
        aligned, _ = ooo_hop_insert.verify_alignment(conn, "WB-1001")
        assert aligned is True
