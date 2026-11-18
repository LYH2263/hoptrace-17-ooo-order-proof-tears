from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.db import connect
from app.modules import ooo_hop_insert
from app.services import ingest as ingest_svc
from app.services import timeline as timeline_svc

router = APIRouter(prefix="/api")


class IngestRequest(BaseModel):
    fixture_name: str | None = None


@router.get("/overview")
def overview():
    conn = connect()
    try:
        return {
            "waybills": conn.execute("SELECT COUNT(*) AS c FROM waybills").fetchone()["c"],
            "hops_primary": conn.execute(
                "SELECT COUNT(*) AS c FROM hops WHERE channel='primary' AND retracted=0"
            ).fetchone()["c"],
            "hops_sidecar": conn.execute(
                "SELECT COUNT(*) AS c FROM hops WHERE channel='sidecar'"
            ).fetchone()["c"],
            "quarantine": conn.execute("SELECT COUNT(*) AS c FROM quarantine").fetchone()["c"],
        }
    finally:
        conn.close()


@router.get("/waybills")
def list_waybills():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, title, status_proj, created_at FROM waybills ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/waybills/{waybill_id}/timeline")
def waybill_timeline(waybill_id: str):
    conn = connect()
    try:
        wb = conn.execute("SELECT * FROM waybills WHERE id=?", (waybill_id,)).fetchone()
        if not wb:
            raise HTTPException(404, "运单不存在")
        hops = timeline_svc.list_primary(conn, waybill_id)
        proof = conn.execute(
            "SELECT * FROM status_proofs WHERE waybill_id=? ORDER BY id DESC LIMIT 1",
            (waybill_id,),
        ).fetchone()
        order_proof = ooo_hop_insert.latest_order_proof(conn, waybill_id)
        # 序撕裂核证与测绘板竖轴/证明核对页读同一实体
        attestation = ooo_hop_insert.latest_attestation(conn, waybill_id)
        aligned, live_detail = ooo_hop_insert.verify_alignment(conn, waybill_id)
        return {
            "waybill": dict(wb),
            "hops": hops,
            "proof": dict(proof) if proof else None,
            "order_proof": order_proof,
            "attestation": attestation,
            "live_alignment": {"aligned": aligned, "detail": live_detail},
        }
    finally:
        conn.close()


@router.get("/quarantine")
def list_quarantine():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, waybill_id, reason, member_json, created_at FROM quarantine ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/sidecar")
def list_sidecar():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, waybill_id, event_ts, location_code, event_code, channel FROM hops WHERE channel='sidecar' ORDER BY event_ts"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.post("/ingest/run")
def run_ingest(req: IngestRequest | None = None):
    return ingest_svc.run_batch(req.fixture_name if req else None)


@router.get("/status-proofs")
def status_proofs(waybill_id: str | None = None):
    conn = connect()
    try:
        if waybill_id:
            rows = conn.execute(
                "SELECT * FROM status_proofs WHERE waybill_id=? ORDER BY id DESC",
                (waybill_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM status_proofs ORDER BY id DESC LIMIT 50"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/order-proofs")
def order_proofs(waybill_id: str | None = None, batch_id: str | None = None):
    """序证明只读接口：可按运单或本批写入键查询。"""
    conn = connect()
    try:
        return ooo_hop_insert.query_order_proofs(conn, waybill_id=waybill_id, batch_id=batch_id)
    finally:
        conn.close()


@router.get("/batches/{batch_id}/order-proofs")
def batch_order_proofs(batch_id: str):
    """按本批写入键查询序证明（批次视角的只读入口）。"""
    conn = connect()
    try:
        return ooo_hop_insert.query_order_proofs(conn, batch_id=batch_id)
    finally:
        conn.close()


@router.get("/order-tear-attestations")
def order_tear_attestations(waybill_id: str | None = None, batch_id: str | None = None):
    """序撕裂核证只读接口：运单、有序 hop id、新旧内容哈希、投影、批次键、撕裂结论。"""
    conn = connect()
    try:
        return ooo_hop_insert.query_attestations(
            conn, waybill_id=waybill_id, batch_id=batch_id
        )
    finally:
        conn.close()


@router.get("/batches/{batch_id}/order-tear-attestations")
def batch_order_tear_attestations(batch_id: str):
    """按本批写入键查询序撕裂核证（批次视角）。"""
    conn = connect()
    try:
        return ooo_hop_insert.query_attestations(conn, batch_id=batch_id)
    finally:
        conn.close()
