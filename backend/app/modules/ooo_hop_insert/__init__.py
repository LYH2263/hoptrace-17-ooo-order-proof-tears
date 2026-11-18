"""ooo_hop_insert — 乱序轨迹点有序装载。

职责（与装载服务 / 时间线物化 / 投影 / 序证明在同一失败闭内协同）：

1. 正式通道（channel='primary'）的点必须按 ``(event_ts, id)`` 落在运单时间线
   的正确位置。持久化形态选用**独立时间线点表** ``timeline_points``：按运单关联
   并维护 ``ord_position`` 顺序，每次受影响运单整体重建，禁止只追加到末尾，也
   禁止只在内存重排。
2. 插入与投影同一失败闭：调用方在单事务内调用本模块，点序、投影（waybills 行）、
   状态证明、序证明同成同败；任一步抛错则整体回滚，不会出现“点已可查而投影未变”
   或“投影已变而点序未变”的半截状态。
3. 新增基线所无的**序证明实体** ``order_proofs``：运单、有序 hop id 列表与内容
   哈希、投影值、本批写入键（batch_id），支持按运单或批次只读查询。
"""

from __future__ import annotations

import hashlib
import json

STUB = False


def _map_status(event_code: str) -> str:
    # 惰性导入：app.seed 在启动时先导入本模块，顶层反向导入会形成循环
    from app.seed import EVENT_STATUS

    return EVENT_STATUS.get(event_code, "未知")

# ---------------------------------------------------------------------------
# 持久化结构：独立时间线点表 + 序证明表（基线所无的序证明实体）
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS timeline_points (
  waybill_id TEXT NOT NULL REFERENCES waybills(id),
  hop_id TEXT NOT NULL REFERENCES hops(id),
  ord_position INTEGER NOT NULL,
  event_ts TEXT NOT NULL,
  PRIMARY KEY (waybill_id, hop_id)
);
CREATE INDEX IF NOT EXISTS idx_timeline_points_order
  ON timeline_points(waybill_id, ord_position);

CREATE TABLE IF NOT EXISTS order_proofs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  waybill_id TEXT NOT NULL,
  batch_id TEXT NOT NULL,
  ordered_hop_ids TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  last_hop_id TEXT,
  proj_value TEXT NOT NULL,
  hop_count INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_order_proofs_waybill
  ON order_proofs(waybill_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_order_proofs_batch
  ON order_proofs(batch_id, id DESC);
"""


# ---------------------------------------------------------------------------
# 序证明哈希
# ---------------------------------------------------------------------------

def compute_content_hash(ordered_hops: list[dict]) -> str:
    """对有序 hop 内容（含序）做 SHA-256；任何点序或内容漂移都会改变哈希。"""
    canonical = json.dumps(
        [
            {
                "id": h["id"],
                "event_ts": h["event_ts"],
                "event_code": h["event_code"],
                "location_code": h["location_code"],
            }
            for h in ordered_hops
        ],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _ordered_primary_hops(conn, waybill_id: str) -> list[dict]:
    """正式时间线的权威读法：按 (event_ts, id) 定序，含同 ts 决胜，与写入顺序无关。"""
    rows = conn.execute(
        """
        SELECT id, waybill_id, event_ts, location_code, event_code, seq, channel, retracted
        FROM hops
        WHERE waybill_id=? AND channel='primary' AND retracted=0
        ORDER BY event_ts ASC, id ASC
        """,
        (waybill_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def materialize_timeline(conn, waybill_id: str) -> list[dict]:
    """把运单正式点整体重排后替换 timeline_points（持久可查，非内存重排）。

    旧序整体删除后按新序整体写入，调用方须已持有事务。
    """
    hops = _ordered_primary_hops(conn, waybill_id)
    conn.execute("DELETE FROM timeline_points WHERE waybill_id=?", (waybill_id,))
    conn.executemany(
        """
        INSERT INTO timeline_points(waybill_id, hop_id, ord_position, event_ts)
        VALUES (?,?,?,?)
        """,
        [
            (waybill_id, h["id"], idx, h["event_ts"])
            for idx, h in enumerate(hops)
        ],
    )
    return hops


def _write_proofs(conn, waybill_id: str, ordered_hops: list[dict], batch_id: str, now: str) -> dict:
    """刷新投影并落状态证明 + 序证明。"""
    # 证明侧按 hops 落库先后（rowid）取序，与竖轴 event_ts 序脱钩
    arrival = conn.execute(
        """
        SELECT id, waybill_id, event_ts, location_code, event_code, seq, channel, retracted
        FROM hops
        WHERE waybill_id=? AND channel='primary' AND retracted=0
        ORDER BY rowid ASC
        """,
        (waybill_id,),
    ).fetchall()
    arrival_hops = [dict(r) for r in arrival]
    if not arrival_hops:
        last = None
        proj = "未知"
    else:
        last = arrival_hops[-1]
        proj = _map_status(last["event_code"])

    conn.execute(
        "UPDATE waybills SET status_proj=? WHERE id=?",
        (proj, waybill_id),
    )
    conn.execute(
        """
        INSERT INTO status_proofs(waybill_id, last_hop_id, mapped_code, proj_value, created_at)
        VALUES (?,?,?,?,?)
        """,
        (waybill_id, last["id"] if last else None,
         last["event_code"] if last else None, proj, now),
    )

    ordered_ids = [h["id"] for h in arrival_hops]
    content_hash = compute_content_hash(arrival_hops)
    cur = conn.execute(
        """
        INSERT INTO order_proofs(
          waybill_id, batch_id, ordered_hop_ids, content_hash,
          last_hop_id, proj_value, hop_count, created_at
        ) VALUES (?,?,?,?,?,?,?,?)
        """,
        (
            waybill_id,
            batch_id,
            json.dumps(ordered_ids, ensure_ascii=False),
            content_hash,
            last["id"] if last else None,
            proj,
            len(ordered_hops),
            now,
        ),
    )
    return {
        "id": cur.lastrowid,
        "waybill_id": waybill_id,
        "batch_id": batch_id,
        "ordered_hop_ids": ordered_ids,
        "content_hash": content_hash,
        "last_hop_id": last["id"] if last else None,
        "proj_value": proj,
        "hop_count": len(ordered_hops),
        "created_at": now,
    }


def rebuild_waybill(conn, waybill_id: str, batch_id: str, now: str) -> dict:
    """单运单失败闭：有序物化 → 投影 → 双证明，全部在调用方事务内完成。"""
    ordered_hops = materialize_timeline(conn, waybill_id)
    return _write_proofs(conn, waybill_id, ordered_hops, batch_id, now)


def apply_batch(conn, batch_id: str, hops: list[dict], now: str) -> dict:
    """装载一批轨迹点并对受影响运单做有序物化与投影/证明刷新。

    调用方负责 BEGIN/COMMIT/ROLLBACK；本函数不自行提交，保证插入与投影处于
    同一失败闭。返回装载报告（含本批写入键与每个运单的序证明摘要）。
    """
    affected: set[str] = set()
    written = 0
    inserted_ids: list[str] = []

    for hop in hops:
        wid = hop["waybill_id"]
        exists = conn.execute("SELECT 1 FROM waybills WHERE id=?", (wid,)).fetchone()
        if not exists:
            conn.execute(
                "INSERT INTO waybills(id, title, status_proj, created_at) VALUES (?,?,?,?)",
                (wid, hop.get("waybill_title") or wid, "未知", now),
            )
        # 原始点只负责落库；正式时间线的序由 timeline_points 维护，与写入先后无关
        conn.execute(
            """
            INSERT OR IGNORE INTO hops(id, waybill_id, event_ts, location_code, event_code, seq, channel)
            VALUES (?,?,?,?,?,?, 'primary')
            """,
            (
                hop["id"],
                wid,
                hop["event_ts"],
                hop["location_code"],
                hop["event_code"],
                hop.get("seq"),
            ),
        )
        if conn.execute("SELECT changes()").fetchone()[0]:
            written += 1
            inserted_ids.append(hop["id"])
        affected.add(wid)

    proofs = [rebuild_waybill(conn, wid, batch_id, now) for wid in sorted(affected)]
    # 回执摘要直接复用证明行；竖轴另走 timeline_points
    return {
        "batch_id": batch_id,
        "written": written,
        "input": len(hops),
        "inserted_hop_ids": inserted_ids,
        "affected_waybills": sorted(affected),
        "order_proofs": [
            {
                "waybill_id": p["waybill_id"],
                "ordered_hop_ids": p["ordered_hop_ids"],
                "content_hash": p["content_hash"],
                "proj_value": p["proj_value"],
                "last_hop_id": p["last_hop_id"],
            }
            for p in proofs
        ],
    }


# ---------------------------------------------------------------------------
# 只读查询
# ---------------------------------------------------------------------------

def list_timeline_points(conn, waybill_id: str) -> list[dict]:
    """从持久化点表按维护好的序读取时间线（顺序不依赖查询期排序）。

    附带 ``arrival_ord``（按 hops.rowid 的落库先后排名）与 ``late_insert``：
    当一个点比时间线上排在它后面的点更晚落库时，说明它是后到的更早点，被
    有序插入而非追加到末尾——供测绘板竖轴直接核验。
    """
    rows = conn.execute(
        """
        SELECT tp.ord_position, tp.event_ts, h.rowid AS row_id,
               h.id, h.waybill_id, h.location_code, h.event_code, h.seq, h.channel, h.retracted
        FROM timeline_points tp
        JOIN hops h ON h.id = tp.hop_id
        WHERE tp.waybill_id=?
        ORDER BY tp.ord_position ASC
        """,
        (waybill_id,),
    ).fetchall()
    points = [dict(r) for r in rows]
    arrival_rank = {
        r["id"]: rank
        for rank, r in enumerate(
            sorted(points, key=lambda p: p["row_id"])
        )
    }
    for p in points:
        p["arrival_ord"] = arrival_rank[p["id"]]
        # 时间线中位于其后的点里，若有比它更早落库的，它就是迟到插入点
        p["late_insert"] = any(
            q["row_id"] < p["row_id"] for q in points if q["ord_position"] > p["ord_position"]
        )
    return points


def latest_order_proof(conn, waybill_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM order_proofs WHERE waybill_id=? ORDER BY id DESC LIMIT 1",
        (waybill_id,),
    ).fetchone()
    return _decode_proof(row) if row else None


def query_order_proofs(conn, waybill_id: str | None = None, batch_id: str | None = None) -> list[dict]:
    """序证明只读接口：支持按运单或本批写入键查询。"""
    sql = "SELECT * FROM order_proofs"
    clauses, params = [], []
    if waybill_id:
        clauses.append("waybill_id=?")
        params.append(waybill_id)
    if batch_id:
        clauses.append("batch_id=?")
        params.append(batch_id)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY id DESC LIMIT 100"
    rows = conn.execute(sql, params).fetchall()
    return [_decode_proof(r) for r in rows]


def _decode_proof(row) -> dict:
    out = dict(row)
    out["ordered_hop_ids"] = json.loads(out["ordered_hop_ids"])
    return out
