"""清点火唯一出口（甲路，当班封死）。

纪要：docs/清点火纪要.md

对账、探头、温谱、批次、游标五处的"残留尖峰 / 游标态"一律从本模块
``batch_state()`` 取数，任何服务或页面不得自行判定。

甲路口径：
- 批次内仍有残留超温点（温度点 > 阈值）→ 正式游标零增、批次态零改、拒开；
- 残留清零后才允许正式开放；
- 禁止乙路"拟开/影子拟开"记录，也不做事后晋升；
- 发现拟开影子记录一律视为偏航，相关状态不得报绿。
"""
from __future__ import annotations

import json

MEMO_PATH = "docs/清点火纪要.md"
MEMO_PATH_LABEL = "甲"
SHADOW_STATUSES = ("拟开", "影子拟开")
DEFAULT_MARGIN = 2.0


def _value(point) -> float:
    return float(point.get("c", point.get("temp", 0)))


def points_of(batch) -> list:
    return json.loads(batch["temp_points"] or "[]")


def default_threshold(chamber) -> float:
    return float(chamber["target_max"]) + DEFAULT_MARGIN


def residual(points: list, threshold: float) -> dict:
    offenders = [p for p in points if p and _value(p) > threshold]
    peak = max((_value(p) for p in offenders), default=None)
    observed = max((_value(p) for p in points if p), default=None)
    return {
        "threshold": float(threshold),
        "residual_peak": peak,
        "residual_count": len(offenders),
        "residual_times": [p.get("t", p.get("time")) for p in offenders],
        "observed_peak": observed,
        "spike_cleared": peak is None,
    }


def _latest_formal_cursor(conn, batch_id: int):
    placeholders = ",".join("?" for _ in SHADOW_STATUSES)
    return conn.execute(
        f"""SELECT * FROM excursion_cursors
            WHERE batch_id=? AND status NOT IN ({placeholders})
            ORDER BY id DESC LIMIT 1""",
        (batch_id, *SHADOW_STATUSES),
    ).fetchone()


def batch_state(conn, batch, threshold: float | None = None) -> dict:
    """五处同源的唯一清点火判定。"""
    chamber = conn.execute(
        "SELECT * FROM cold_chambers WHERE id=?", (batch["chamber_id"],)
    ).fetchone()
    if threshold is not None:
        thr = float(threshold)
    else:
        cursor = _latest_formal_cursor(conn, batch["id"])
        thr = (
            float(cursor["threshold"])
            if cursor is not None
            else default_threshold(chamber)
        )
    res = residual(points_of(batch), thr)
    placeholders = ",".join("?" for _ in SHADOW_STATUSES)
    open_cursors = conn.execute(
        """SELECT COUNT(*) c FROM excursion_cursors
           WHERE batch_id=? AND status='开放'""",
        (batch["id"],),
    ).fetchone()["c"]
    shadow_cursors = conn.execute(
        f"""SELECT COUNT(*) c FROM excursion_cursors
            WHERE batch_id=? AND status IN ({placeholders})""",
        (batch["id"], *SHADOW_STATUSES),
    ).fetchone()["c"]
    status = batch["status"]
    # 尖峰未清即不一致；出现拟开/影子记录一律视为偏离纪要，不得报绿。
    consistent = res["spike_cleared"] and shadow_cursors == 0
    openable = (
        res["spike_cleared"]
        and open_cursors == 0
        and shadow_cursors == 0
        and status not in ("已封批", "草稿")
    )
    return {
        "memo": MEMO_PATH,
        "path": MEMO_PATH_LABEL,
        "batch_id": batch["id"],
        "batch_code": batch["batch_code"],
        "status": status,
        "threshold": thr,
        "residual_peak": res["residual_peak"],
        "residual_count": res["residual_count"],
        "residual_times": res["residual_times"],
        "observed_peak": res["observed_peak"],
        "spike_cleared": res["spike_cleared"],
        "open_cursors": open_cursors,
        "shadow_cursors": shadow_cursors,
        "cursor_spike_consistent": consistent,
        "openable": openable,
    }


def probe_state(conn, probe_id: int) -> dict:
    rows = conn.execute(
        "SELECT * FROM temp_batches WHERE probe_id=? ORDER BY id", (probe_id,)
    ).fetchall()
    states = [batch_state(conn, row) for row in rows]
    return {
        "memo": MEMO_PATH,
        "path": MEMO_PATH_LABEL,
        "batch_count": len(states),
        "residual_batches": sum(1 for s in states if not s["spike_cleared"]),
        "open_cursors": sum(s["open_cursors"] for s in states),
        "shadow_cursors": sum(s["shadow_cursors"] for s in states),
        "cursor_spike_consistent": all(
            s["cursor_spike_consistent"] for s in states
        ),
        "batches": states,
    }


def chamber_state(conn, chamber_id: int) -> dict:
    rows = conn.execute(
        "SELECT * FROM temp_batches WHERE chamber_id=? ORDER BY id", (chamber_id,)
    ).fetchall()
    states = [batch_state(conn, row) for row in rows]
    return {
        "memo": MEMO_PATH,
        "path": MEMO_PATH_LABEL,
        "batch_count": len(states),
        "residual_batches": sum(1 for s in states if not s["spike_cleared"]),
        "open_cursors": sum(s["open_cursors"] for s in states),
        "shadow_cursors": sum(s["shadow_cursors"] for s in states),
        "cursor_spike_consistent": all(
            s["cursor_spike_consistent"] for s in states
        ),
    }
