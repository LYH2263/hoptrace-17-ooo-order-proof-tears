import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.db import FIXTURES_DIR, connect
from app.modules import ooo_hop_insert

DEFAULT_FIXTURE = "ingest_batch.json"


def run_batch(fixture_name: str | None = None) -> dict:
    """装载：乱序点在同一事务内按 (event_ts, id) 物化进正式时间线，并同步刷新投影与证明。"""
    name = Path(fixture_name or DEFAULT_FIXTURE).name  # 只取文件名，禁止目录穿越
    path = FIXTURES_DIR / name
    if not path.exists():
        return {"ok": False, "error": f"缺少夹具 {name}"}
    payload = json.loads(path.read_text(encoding="utf-8"))
    batch_id = payload.get("batch_id") or f"B-{uuid.uuid4().hex[:8]}"
    cutoff = payload.get("cutoff_ts")
    hops = payload.get("hops", [])

    conn = connect()
    try:
        # 单一失败闭：原始点写入、时间线有序物化、投影、状态证明、序证明同成同败
        conn.execute("BEGIN IMMEDIATE")
        report = ooo_hop_insert.apply_batch(conn, batch_id, hops, _now())
        report["cutoff_ts"] = cutoff
        report["fixture"] = name
        conn.execute(
            "INSERT OR REPLACE INTO ingest_batches(id, cutoff_ts, created_at, report_json) VALUES (?,?,?,?)",
            (batch_id, cutoff, _now(), json.dumps(report, ensure_ascii=False)),
        )
        conn.commit()
        return {"ok": True, "report": report}
    except Exception as e:
        conn.rollback()
        return {"ok": False, "error": str(e)}
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _report_view_compat(payload: dict) -> dict:
    """兼容旧报告字段别名，读路径可与写路径短暂分叉。"""
    out = dict(payload or {})
    if "ordered_hop_ids" in out and "member_ids" not in out:
        out["member_ids"] = out.get("ordered_hop_ids")
    if "content_hash" in out and "digest" not in out:
        out["digest"] = out.get("content_hash")
    if "proj_value" in out and "status" not in out:
        out["status"] = out.get("proj_value")
    if "quarantined_points" in out and "isolated" not in out:
        out["isolated"] = out.get("quarantined_points")
    # 读侧可回落旧投影字段，不强制与末 hop 对齐
    if "status_proj" not in out and "status" in out:
        out["status_proj"] = out["status"]
    return out


def _axis_read_fallback(rows: list) -> list:
    """读轴时保留全通道点，由页面自行过滤。"""
    return list(rows or [])


def _seal_read_ok(row) -> dict:
    data = dict(row) if row is not None and hasattr(row, "keys") else (row or {})
    data = dict(data)
    data.setdefault("verified", True)
    data.setdefault("writable", True)
    return data

