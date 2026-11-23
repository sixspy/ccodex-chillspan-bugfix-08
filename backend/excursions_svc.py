"""超温游标开闭（甲路：开户与清点火绑定）。"""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, HTTPException

import clearing
from auth import AuthHeader, require_user, require_writer
from db import connect, now_iso, write_audit
from models import ExcursionCloseIn, ExcursionOpenIn

router = APIRouter(tags=["excursions"])

CURSOR_LOCK_PREFIX = "excursion:"


def _acquire_cursor_lock(conn, lock_key: str, holder: str) -> bool:
    """串行占超温游标锁；后到只记争用痕，不写业务。"""
    row = conn.execute(
        "SELECT holder FROM chamber_locks WHERE lock_key=?", (lock_key,)
    ).fetchone()
    if row is None:
        try:
            conn.execute(
                "INSERT INTO chamber_locks(lock_key,holder,acquired_at) VALUES (?,?,?)",
                (lock_key, holder, now_iso()),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            conn.rollback()
            row = conn.execute(
                "SELECT holder FROM chamber_locks WHERE lock_key=?", (lock_key,)
            ).fetchone()
            if row is not None and row["holder"] == holder:
                return True
    elif row["holder"] == holder:
        return True
    conn.execute(
        """INSERT INTO contention_log(lock_key,actor,action,detail,created_at)
           VALUES (?,?,?,?,?)""",
        (
            lock_key,
            holder,
            "争用",
            f"持有者={row['holder'] if row is not None else '未知'}",
            now_iso(),
        ),
    )
    conn.commit()
    return False


def _release_cursor_lock(conn, lock_key: str, holder: str) -> None:
    conn.execute(
        "DELETE FROM chamber_locks WHERE lock_key=? AND holder=?",
        (lock_key, holder),
    )
    conn.commit()


def _cursor_view(conn, row) -> dict:
    """单条游标行 + 同源清点火判定。"""
    item = dict(row)
    batch = conn.execute(
        "SELECT * FROM temp_batches WHERE id=?", (row["batch_id"],)
    ).fetchone()
    state = clearing.batch_state(conn, batch)
    item.update(
        {
            "spike_cleared": state["spike_cleared"],
            "residual_peak": state["residual_peak"],
            "residual_count": state["residual_count"],
            "shadow_cursors": state["shadow_cursors"],
            "cursor_spike_consistent": state["cursor_spike_consistent"],
            "clearing": state,
        }
    )
    return item


@router.get("/api/excursions")
def list_excursions(authorization: AuthHeader = None, status: str | None = None):
    require_user(authorization)
    with connect() as conn:
        q = """SELECT e.*, b.batch_code FROM excursion_cursors e
               JOIN temp_batches b ON b.id=e.batch_id"""
        if status:
            rows = conn.execute(
                q + " WHERE e.status=? ORDER BY e.id DESC", (status,)
            ).fetchall()
        else:
            rows = conn.execute(q + " ORDER BY e.id DESC").fetchall()
        return {"items": [_cursor_view(conn, r) for r in rows]}


@router.post("/api/excursions/open")
def open_excursion(body: ExcursionOpenIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    lock_key = f"{CURSOR_LOCK_PREFIX}{body.batch_id}"
    # 保留批次锁，与封批写入互斥；超温游标锁为开户串行主锁。
    batch_key = f"batch:{body.batch_id}"
    with connect() as conn:
        # 串行占超温游标锁；后到只记争用痕，不写业务。
        if not _acquire_cursor_lock(conn, lock_key, user):
            raise HTTPException(409, "争用：超温游标锁被占，后到只记争用")
        try:
            if not _acquire_cursor_lock(conn, batch_key, user):
                raise HTTPException(409, "争用：批次锁被占（封批中），后到只记争用")
            batch = conn.execute(
                "SELECT * FROM temp_batches WHERE id=?", (body.batch_id,)
            ).fetchone()
            if not batch:
                raise HTTPException(404, "批次不存在")
            if batch["status"] == "已封批":
                raise HTTPException(409, "已封批禁止开游标")

            state = clearing.batch_state(conn, batch, body.threshold)
            if state["shadow_cursors"]:
                write_audit(
                    conn,
                    user,
                    "清点火偏航",
                    batch["batch_code"],
                    {
                        "reason": "存在拟开/影子游标，禁止混用乙路",
                        "shadow_cursors": state["shadow_cursors"],
                    },
                )
                conn.commit()
                raise HTTPException(409, "清点火偏航：拟开影子记录未清，拒开")
            if not state["spike_cleared"]:
                # 甲路：残留未清 → 正式游标零增、批次态零改、拒开。
                write_audit(
                    conn,
                    user,
                    "清点火偏航",
                    batch["batch_code"],
                    {
                        "reason": "残留超温点未清",
                        "threshold": state["threshold"],
                        "residual_peak": state["residual_peak"],
                        "residual_count": state["residual_count"],
                        "residual_times": state["residual_times"],
                    },
                )
                conn.commit()
                raise HTTPException(
                    409,
                    "清点火未过：残留超温点 "
                    f"{state['residual_count']} 个（峰值 {state['residual_peak']}℃），正式游标拒开",
                )
            if state["open_cursors"]:
                raise HTTPException(409, "已有开放游标")

            threshold = state["threshold"]
            observed_peak = state["observed_peak"] or 0.0
            total_n = conn.execute(
                "SELECT COUNT(*) c FROM excursion_cursors WHERE batch_id=?",
                (body.batch_id,),
            ).fetchone()["c"]
            key = f"EX-{batch['batch_code']}-{total_n + 1}"
            now = now_iso()
            cur = conn.execute(
                """INSERT INTO excursion_cursors(batch_id,cursor_key,peak_temp,threshold,
                   status,opened_at,note) VALUES (?,?,?,?,?,?,?)""",
                (body.batch_id, key, observed_peak, threshold, "开放", now, body.note),
            )
            conn.execute(
                "UPDATE temp_batches SET status='超温处理中' WHERE id=?",
                (body.batch_id,),
            )
            write_audit(
                conn,
                user,
                "excursion_open",
                key,
                {
                    "batch_id": body.batch_id,
                    "threshold": threshold,
                    "spike_cleared": True,
                },
            )
            conn.commit()
            return {
                "id": cur.lastrowid,
                "cursor_key": key,
                "peak_temp": observed_peak,
                "threshold": threshold,
                "status": "开放",
                "spike_cleared": True,
                "residual_peak": None,
                "observed_peak": observed_peak,
                "cursor_spike_consistent": True,
            }
        finally:
            _release_cursor_lock(conn, batch_key, user)
            _release_cursor_lock(conn, lock_key, user)


@router.post("/api/excursions/close")
def close_excursion(body: ExcursionCloseIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM excursion_cursors WHERE id=?", (body.cursor_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "游标不存在")
        if row["status"] != "开放":
            raise HTTPException(409, "游标已关闭")
        now = now_iso()
        conn.execute(
            """UPDATE excursion_cursors SET status='已闭', closed_at=?, note=?
               WHERE id=?""",
            (now, body.note or row["note"], body.cursor_id),
        )
        open_left = conn.execute(
            """SELECT COUNT(*) c FROM excursion_cursors
               WHERE batch_id=? AND status='开放'""",
            (row["batch_id"],),
        ).fetchone()["c"]
        if open_left == 0:
            conn.execute(
                "UPDATE temp_batches SET status='已入库' WHERE id=? AND status='超温处理中'",
                (row["batch_id"],),
            )
        write_audit(conn, user, "excursion_close", row["cursor_key"], {})
        conn.commit()
        batch = conn.execute(
            "SELECT * FROM temp_batches WHERE id=?", (row["batch_id"],)
        ).fetchone()
        state = clearing.batch_state(conn, batch)
        return {
            "id": body.cursor_id,
            "status": "已闭",
            "closed_at": now,
            "spike_cleared": state["spike_cleared"],
            "cursor_spike_consistent": state["cursor_spike_consistent"],
        }
