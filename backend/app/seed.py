import json
from datetime import datetime, timezone
from pathlib import Path

from app.db import FIXTURES_DIR, connect
from app.modules import ooo_hop_insert


SCHEMA = """
CREATE TABLE IF NOT EXISTS waybills (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  status_proj TEXT NOT NULL DEFAULT '未知',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hops (
  id TEXT PRIMARY KEY,
  waybill_id TEXT NOT NULL REFERENCES waybills(id),
  event_ts TEXT NOT NULL,
  location_code TEXT NOT NULL,
  event_code TEXT NOT NULL,
  seq INTEGER,
  channel TEXT NOT NULL DEFAULT 'primary',
  retracted INTEGER NOT NULL DEFAULT 0,
  UNIQUE(waybill_id, event_ts, location_code, event_code)
);

CREATE TABLE IF NOT EXISTS quarantine (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  waybill_id TEXT NOT NULL,
  reason TEXT NOT NULL,
  member_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingest_batches (
  id TEXT PRIMARY KEY,
  cutoff_ts TEXT,
  created_at TEXT NOT NULL,
  report_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS status_proofs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  waybill_id TEXT NOT NULL,
  last_hop_id TEXT,
  mapped_code TEXT,
  proj_value TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""

EVENT_STATUS = {
    "PICKED": "已揽收",
    "IN_TRANSIT": "运输中",
    "ARRIVED_HUB": "到站",
    "OUT_FOR_DELIVERY": "派送中",
    "DELIVERED": "已签收",
    "EXCEPTION": "异常",
}


def init_db():
    conn = connect()
    conn.executescript(SCHEMA)
    # 功能题（ooo_hop_insert）持久结构：独立时间线点表 + 序证明表
    conn.executescript(ooo_hop_insert.SCHEMA)
    n = conn.execute("SELECT COUNT(*) AS c FROM waybills").fetchone()["c"]
    if n == 0:
        _seed_fixtures(conn)
        conn.commit()
    elif conn.execute("SELECT COUNT(*) AS c FROM timeline_points").fetchone()["c"] == 0:
        # 既有库升级：按 (event_ts, id) 回填持久时间线与序证明，刷新后不乱序
        _backfill(conn)
        conn.commit()
    conn.close()


def _seed_fixtures(conn):
    fixture = FIXTURES_DIR / "seed_hops.json"
    if not fixture.exists():
        return
    data = json.loads(fixture.read_text(encoding="utf-8"))
    for wb in data.get("waybills", []):
        conn.execute(
            "INSERT INTO waybills(id, title, status_proj, created_at) VALUES (?,?,?,?)",
            (wb["id"], wb["title"], wb.get("status_proj", "未知"), wb["created_at"]),
        )
    for hop in data.get("hops", []):
        conn.execute(
            "INSERT INTO hops(id, waybill_id, event_ts, location_code, event_code, seq, channel) VALUES (?,?,?,?,?,?,?)",
            (
                hop["id"],
                hop["waybill_id"],
                hop["event_ts"],
                hop["location_code"],
                hop["event_code"],
                hop.get("seq"),
                hop.get("channel", "primary"),
            ),
        )
    now = _now()
    waybill_ids = [
        r["id"]
        for r in conn.execute("SELECT DISTINCT waybill_id AS id FROM hops ORDER BY waybill_id").fetchall()
    ]
    # 种子也走同一失败闭：有序物化 → 投影 → 状态证明 / 序证明
    for wid in waybill_ids:
        ooo_hop_insert.rebuild_waybill(conn, wid, "SEED", now)


def _backfill(conn):
    now = _now()
    waybill_ids = [
        r["id"]
        for r in conn.execute(
            "SELECT DISTINCT waybill_id AS id FROM hops "
            "WHERE channel='primary' AND retracted=0 ORDER BY waybill_id"
        ).fetchall()
    ]
    for wid in waybill_ids:
        ooo_hop_insert.rebuild_waybill(conn, wid, "BACKFILL", now)


def map_status(event_code: str) -> str:
    return EVENT_STATUS.get(event_code, "未知")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
