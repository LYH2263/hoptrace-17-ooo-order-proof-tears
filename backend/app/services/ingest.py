import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.db import FIXTURES_DIR, connect
from app.modules import ooo_hop_insert

DEFAULT_FIXTURE = "ingest_batch.json"


def run_batch(fixture_name: str | None = None) -> dict:
    """装载：乱序点在同一事务内按 (event_ts, id) 物化进正式时间线，并同步刷新投影与证明。

    竖轴点序、投影、序证明、撕裂核证处于同一失败闭：核证闸门抛
    ``OrderTearError`` 或任一步出错都整体回滚，回执 ``ok=False``，
    禁止半截重排对外可查。
    """
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
        # 单一失败闭：原始点写入、时间线有序物化、投影、状态证明、序证明、撕裂核证同成同败
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
    except ooo_hop_insert.OrderTearError as e:
        conn.rollback()
        # 序证明与正式轴/投影撕裂：整批回滚后显式失败，不得回执成功
        return {"ok": False, "error": str(e), "torn": True}
    except Exception as e:
        conn.rollback()
        return {"ok": False, "error": str(e)}
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
