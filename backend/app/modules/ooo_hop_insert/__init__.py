"""ooo_hop_insert — 乱序轨迹点有序装载。

职责（装载服务 / 时间线物化 / 投影 / 序证明在同一失败闭内协同）：

1. 正式通道（channel='primary'）的点必须按 ``(event_ts, id)`` 落在运单时间线
   的正确位置。持久化形态选用**独立时间线点表** ``timeline_points``：按运单关联
   并维护 ``ord_position`` 顺序，每次受影响运单整体重建，禁止只追加到末尾，也
   禁止只在内存重排。
2. 插入与投影同一失败闭：调用方在单事务内调用本模块，点序、投影（waybills 行）、
   状态证明、序证明同成同败；任一步抛错则整体回滚，不会出现“点已可查而投影未变”
   或“投影已变而点序未变”的半截状态。
3. 序证明实体 ``order_proofs``：运单、有序 hop id 列表与内容哈希（对序敏感的
   SHA-256）、投影值、本批写入键（batch_id），支持按运单或批次只读查询。
4. 序撕裂核证实体 ``order_tear_attestations``（基线所无）：每次重建都把竖轴序、
   证明序、投影派生末点三方对照落库，记录旧序/新序内容哈希与撕裂结论。核证闸门
   在同一事务内回读持久态比对——竖轴序 ≠ 证明序、或投影不随末有效 hop、或证明
   行缺失，一律抛 :class:`OrderTearError`，由调用方整体回滚，装载回执失败，
   禁止半截重排对外可查。

提交形态：**同事务重排时间线并重算投影与序证明，缺一回滚**。
"""

from __future__ import annotations

import hashlib
import json

STUB = False


class OrderTearError(RuntimeError):
    """竖轴序 / 序证明 / 投影三方核证不一致。

    抛出时调用方必须回滚整个事务——任何一侧落后或超前都不得提交成功。
    """


# ---------------------------------------------------------------------------
# 测试故障注入：默认全关，仅失败对照测试显式打开，用于复现撕裂并验证闸门
# ---------------------------------------------------------------------------

_FAULT = {
    "proof_arrival_order": False,  # 证明侧退回写入序（rowid），模拟旧指纹撕裂
    "stale_projection": False,     # 投影停在末写入点，不随末有效 hop 变
    "skip_proof_row": False,       # 不写序证明行，模拟证明缺失
}


def install_fault(**flags) -> None:
    for key, value in flags.items():
        if key not in _FAULT:  # pragma: no cover - 防御误配
            raise KeyError(f"未知故障注入点: {key}")
        _FAULT[key] = bool(value)


def clear_fault() -> None:
    for key in _FAULT:
        _FAULT[key] = False


def _map_status(event_code: str) -> str:
    # 惰性导入：app.seed 在启动时先导入本模块，顶层反向导入会形成循环
    from app.seed import EVENT_STATUS

    return EVENT_STATUS.get(event_code, "未知")


# ---------------------------------------------------------------------------
# 持久化结构：独立时间线点表 + 序证明表 + 序撕裂核证表（基线所无的核证实体）
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

CREATE TABLE IF NOT EXISTS order_tear_attestations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  waybill_id TEXT NOT NULL,
  batch_id TEXT NOT NULL,
  axis_hop_ids TEXT NOT NULL,
  axis_content_hash TEXT NOT NULL,
  proof_hop_ids TEXT NOT NULL,
  proof_content_hash TEXT NOT NULL,
  prior_content_hash TEXT,
  last_hop_id TEXT,
  proj_value TEXT NOT NULL,
  aligned INTEGER NOT NULL,
  tear_verdict TEXT NOT NULL,
  detail_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attest_waybill
  ON order_tear_attestations(waybill_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_attest_batch
  ON order_tear_attestations(batch_id, id DESC);
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
    """基于竖轴同一派生源（event_ts 有序 hops）刷新投影并落状态证明 + 序证明。

    证明序、内容哈希、投影末点全部取自 ``ordered_hops``——即物化进
    ``timeline_points`` 的同一份有序结果，禁止再按写入序（rowid）重查。
    """
    # 故障注入：证明侧若被改回写入序，核证闸门必须拦下并回滚
    if _FAULT["proof_arrival_order"]:
        arrival = conn.execute(
            """
            SELECT id, waybill_id, event_ts, location_code, event_code, seq, channel, retracted
            FROM hops
            WHERE waybill_id=? AND channel='primary' AND retracted=0
            ORDER BY rowid ASC
            """,
            (waybill_id,),
        ).fetchall()
        proof_hops = [dict(r) for r in arrival]
    else:
        proof_hops = ordered_hops

    if not proof_hops:
        last = None
        proj = "未知"
    else:
        last = proof_hops[-1]
        proj = _map_status(last["event_code"])

    # 故障注入：投影停留旧值，不随末有效 hop 变
    if _FAULT["stale_projection"]:
        old = conn.execute(
            "SELECT status_proj FROM waybills WHERE id=?", (waybill_id,)
        ).fetchone()
        proj = old["status_proj"] if old else "未知"

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

    if _FAULT["skip_proof_row"]:
        return {
            "id": None,
            "waybill_id": waybill_id,
            "batch_id": batch_id,
            "ordered_hop_ids": [h["id"] for h in proof_hops],
            "content_hash": compute_content_hash(proof_hops),
            "last_hop_id": last["id"] if last else None,
            "proj_value": proj,
            "hop_count": len(proof_hops),
            "created_at": now,
        }

    ordered_ids = [h["id"] for h in proof_hops]
    content_hash = compute_content_hash(proof_hops)
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


# ---------------------------------------------------------------------------
# 核证闸门：回读持久态，验证 竖轴序 = 证明序 = 投影派生源
# ---------------------------------------------------------------------------

def verify_alignment(conn, waybill_id: str) -> tuple[bool, dict]:
    """对已落库的竖轴 / 序证明 / 投影做三方核证（只读，不写）。

    返回 ``(是否一致, 核证明细)``。任一检查项不成立即判定撕裂：

    * ``axis_eq_proof_ids``  竖轴有序 hop id 序列与序证明完全相同
    * ``axis_eq_proof_hash`` 竖轴重算内容哈希与证明指纹相同
    * ``last_hop_matches``   证明末点为竖轴末有效 hop
    * ``proj_follows_last``  投影 = 证明投影 = 末有效 hop 的状态映射
    * ``proof_row_present``  序证明行存在（禁止点序已变而证明行缺失）
    """
    axis_rows = conn.execute(
        """
        SELECT h.id, h.event_ts, h.location_code, h.event_code
        FROM timeline_points tp
        JOIN hops h ON h.id = tp.hop_id
        WHERE tp.waybill_id=?
        ORDER BY tp.ord_position ASC
        """,
        (waybill_id,),
    ).fetchall()
    axis_hops = [dict(r) for r in axis_rows]
    axis_ids = [h["id"] for h in axis_hops]
    axis_hash = compute_content_hash(axis_hops)

    proof_row = conn.execute(
        "SELECT * FROM order_proofs WHERE waybill_id=? ORDER BY id DESC LIMIT 1",
        (waybill_id,),
    ).fetchone()
    wb = conn.execute(
        "SELECT status_proj FROM waybills WHERE id=?", (waybill_id,)
    ).fetchone()

    detail = {
        "waybill_id": waybill_id,
        "axis_hop_ids": axis_ids,
        "axis_content_hash": axis_hash,
        "proof_row_present": proof_row is not None,
    }
    if proof_row is None:
        detail["checks"] = {
            "axis_eq_proof_ids": False,
            "axis_eq_proof_hash": False,
            "last_hop_matches": False,
            "proj_follows_last": False,
            "proof_row_present": False,
        }
        return False, detail

    proof = _decode_proof(proof_row)
    expected_last = axis_hops[-1]["id"] if axis_hops else None
    expected_proj = (
        _map_status(axis_hops[-1]["event_code"]) if axis_hops else "未知"
    )
    checks = {
        "proof_row_present": True,
        "axis_eq_proof_ids": axis_ids == proof["ordered_hop_ids"],
        "axis_eq_proof_hash": axis_hash == proof["content_hash"],
        "last_hop_matches": proof["last_hop_id"] == expected_last,
        "proj_follows_last": (
            wb["status_proj"] == proof["proj_value"] == expected_proj
        ),
    }
    detail.update(
        {
            "proof_hop_ids": proof["ordered_hop_ids"],
            "proof_content_hash": proof["content_hash"],
            "last_hop_id": expected_last,
            "proj_value": wb["status_proj"],
            "checks": checks,
        }
    )
    return all(checks.values()), detail


def _write_attestation(
    conn,
    waybill_id: str,
    batch_id: str,
    prior_hash: str | None,
    detail: dict,
    now: str,
) -> dict:
    """落序撕裂核证行：旧序/新序哈希、三轴对照与撕裂结论，只追加不改写。"""
    aligned = detail["checks"]["proof_row_present"] and all(
        v for k, v in detail["checks"].items() if k != "proof_row_present"
    )
    verdict = "一致" if aligned else "撕裂"
    cur = conn.execute(
        """
        INSERT INTO order_tear_attestations(
          waybill_id, batch_id, axis_hop_ids, axis_content_hash,
          proof_hop_ids, proof_content_hash, prior_content_hash,
          last_hop_id, proj_value, aligned, tear_verdict, detail_json, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            waybill_id,
            batch_id,
            json.dumps(detail["axis_hop_ids"], ensure_ascii=False),
            detail["axis_content_hash"],
            json.dumps(detail.get("proof_hop_ids", []), ensure_ascii=False),
            detail.get("proof_content_hash", ""),
            prior_hash,
            detail.get("last_hop_id"),
            detail.get("proj_value", "未知"),
            1 if aligned else 0,
            verdict,
            json.dumps(detail, ensure_ascii=False),
            now,
        ),
    )
    return {
        "id": cur.lastrowid,
        "waybill_id": waybill_id,
        "batch_id": batch_id,
        "axis_hop_ids": detail["axis_hop_ids"],
        "axis_content_hash": detail["axis_content_hash"],
        "proof_hop_ids": detail.get("proof_hop_ids", []),
        "proof_content_hash": detail.get("proof_content_hash", ""),
        "prior_content_hash": prior_hash,
        "last_hop_id": detail.get("last_hop_id"),
        "proj_value": detail.get("proj_value", "未知"),
        "aligned": aligned,
        "tear_verdict": verdict,
        "detail": detail,
        "created_at": now,
    }


def rebuild_waybill(conn, waybill_id: str, batch_id: str, now: str) -> dict:
    """单运单失败闭：有序物化 → 投影/双证明 → 三方核证 → 核证实体，全在调用方事务内。

    核证不通过抛 :class:`OrderTearError`，调用方必须回滚——竖轴、投影、序证明
    任一侧落后或超前都不得提交。
    """
    prior = latest_order_proof(conn, waybill_id)
    prior_hash = prior["content_hash"] if prior else None

    ordered_hops = materialize_timeline(conn, waybill_id)
    _write_proofs(conn, waybill_id, ordered_hops, batch_id, now)

    aligned, detail = verify_alignment(conn, waybill_id)
    if not aligned:
        # 不写核证行、不提交：撕裂态对外完全不可查，整事务随异常回滚
        raise OrderTearError(
            f"序撕裂核证未通过 waybill={waybill_id} batch={batch_id}: "
            f"{json.dumps(detail['checks'], ensure_ascii=False)}"
        )

    attestation = _write_attestation(
        conn, waybill_id, batch_id, prior_hash, detail, now
    )
    proof = latest_order_proof(conn, waybill_id)
    return {"proof": proof, "attestation": attestation}


def apply_batch(conn, batch_id: str, hops: list[dict], now: str) -> dict:
    """装载一批轨迹点并对受影响运单做有序物化与投影/证明刷新。

    调用方负责 BEGIN/COMMIT/ROLLBACK；本函数不自行提交，保证插入与投影处于
    同一失败闭。核证闸门抛 :class:`OrderTearError` 时整批回滚，回执失败。
    返回装载报告（含本批写入键、序证明与序撕裂核证摘要）。
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

    rebuilt = [rebuild_waybill(conn, wid, batch_id, now) for wid in sorted(affected)]
    # 回执摘要与证明行、核证行同源；竖轴另走 timeline_points，三方在事务内核证一致
    return {
        "batch_id": batch_id,
        "written": written,
        "input": len(hops),
        "inserted_hop_ids": inserted_ids,
        "affected_waybills": sorted(affected),
        "order_proofs": [
            {
                "waybill_id": item["proof"]["waybill_id"],
                "ordered_hop_ids": item["proof"]["ordered_hop_ids"],
                "content_hash": item["proof"]["content_hash"],
                "proj_value": item["proof"]["proj_value"],
                "last_hop_id": item["proof"]["last_hop_id"],
            }
            for item in rebuilt
        ],
        "attestations": [
            {
                "waybill_id": item["attestation"]["waybill_id"],
                "tear_verdict": item["attestation"]["tear_verdict"],
                "aligned": item["attestation"]["aligned"],
                "axis_content_hash": item["attestation"]["axis_content_hash"],
                "proof_content_hash": item["attestation"]["proof_content_hash"],
                "prior_content_hash": item["attestation"]["prior_content_hash"],
                "proj_value": item["attestation"]["proj_value"],
                "last_hop_id": item["attestation"]["last_hop_id"],
            }
            for item in rebuilt
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


def latest_attestation(conn, waybill_id: str) -> dict | None:
    """运单最新一版序撕裂核证（测绘板竖轴与核对页读同一实体）。"""
    row = conn.execute(
        "SELECT * FROM order_tear_attestations WHERE waybill_id=? ORDER BY id DESC LIMIT 1",
        (waybill_id,),
    ).fetchone()
    return _decode_attestation(row) if row else None


def query_attestations(
    conn, waybill_id: str | None = None, batch_id: str | None = None
) -> list[dict]:
    """序撕裂核证只读接口：支持按运单或批次键查询。"""
    sql = "SELECT * FROM order_tear_attestations"
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
    return [_decode_attestation(r) for r in rows]


def _decode_proof(row) -> dict:
    out = dict(row)
    out["ordered_hop_ids"] = json.loads(out["ordered_hop_ids"])
    return out


def _decode_attestation(row) -> dict:
    out = dict(row)
    out["axis_hop_ids"] = json.loads(out["axis_hop_ids"])
    out["proof_hop_ids"] = json.loads(out["proof_hop_ids"])
    out["detail"] = json.loads(out.pop("detail_json"))
    out["aligned"] = bool(out["aligned"])
    return out
