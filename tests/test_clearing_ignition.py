"""清点火（BUGFIX-08）失败测：甲路拒开、乙路禁影、双窗争用。

口径见 docs/清点火纪要.md：游标开户与清点火绑定，只走甲路。
本文件先写红：修前残留尖峰仍可开户 / 拟开影子仍报绿 / 争用不占超温游标锁。
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import db
from app import app
from db import init_db


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "t.db")
    init_db()
    yield


def _auth(c: TestClient, user: str = "admin") -> dict:
    tok = c.post(
        "/api/login", json={"username": user, "password": "123456"}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _batch(c: TestClient, h: dict, code: str) -> dict:
    items = c.get("/api/batches", headers=h).json()["items"]
    return next(b for b in items if b["batch_code"] == code)


def _cursor_rows(batch_id: int) -> list:
    with db.connect() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM excursion_cursors WHERE batch_id=? ORDER BY id",
                (batch_id,),
            ).fetchall()
        ]


def test_a_residual_spike_must_block_formal_open():
    """失败测甲：残留超温点未清仍开户——必须拒开、零增、批次态零改。"""
    c = TestClient(app)
    h = _auth(c)
    batch = _batch(c, h, "TB-CK-XN-04-001")  # 种子夹具：2.6℃ > 默认阈 2.0℃
    assert batch["clearing"]["spike_cleared"] is False
    before_status = batch["status"]
    before_cursors = _cursor_rows(batch["id"])

    r = c.post(
        "/api/excursions/open",
        headers=h,
        json={"batch_id": batch["id"]},
    )
    assert r.status_code == 409, r.text
    assert "残留" in r.json()["detail"] or "清点火" in r.json()["detail"]

    # 正式游标零增
    assert _cursor_rows(batch["id"]) == before_cursors
    # 批次游标态零改
    after = _batch(c, h, "TB-CK-XN-04-001")
    assert after["status"] == before_status
    # 拒写须记清点火偏航
    audit = c.get("/api/audit", headers=h).json()["items"]
    assert any(
        a["action"] == "清点火偏航"
        and a["target"] == "TB-CK-XN-04-001"
        for a in audit
    )

    # 拒开期间任何视图都不得报绿
    proof = c.get("/api/proof/batch/TB-CK-XN-04-001", headers=h).json()
    assert proof["spike_cleared"] is False
    assert proof["cursor_spike_consistent"] is False

    # 点火清零后才允许正式开放
    with db.connect() as conn:
        clean = [{"t": "07:00", "c": -2.0}, {"t": "08:00", "c": -1.0}]
        conn.execute(
            """UPDATE temp_batches SET temp_points=?, min_observed=?, max_observed=?
               WHERE id=?""",
            (json.dumps(clean, ensure_ascii=False), -2.0, -1.0, batch["id"]),
        )
        conn.commit()
    ok = c.post(
        "/api/excursions/open",
        headers=h,
        json={"batch_id": batch["id"]},
    )
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["status"] == "开放"
    assert body["spike_cleared"] is True
    assert body["cursor_spike_consistent"] is True
    assert body["residual_peak"] is None


def test_b_pending_shadow_never_reads_green():
    """失败测乙：拟开影子未晋升却游标已绿——必须禁影且一律不绿。"""
    c = TestClient(app)
    h = _auth(c)
    batch = _batch(c, h, "TB-CK-XN-04-001")
    with db.connect() as conn:
        cur = conn.execute(
            """INSERT INTO excursion_cursors(batch_id,cursor_key,peak_temp,threshold,
               status,opened_at,note) VALUES (?,?,?,?,?,?,?)""",
            (
                batch["id"],
                "EX-SHADOW-01",
                2.6,
                2.0,
                "拟开",
                db.now_iso(),
                "影子拟开",
            ),
        )
        shadow_id = cur.lastrowid
        conn.commit()

    proof = c.get("/api/proof/batch/TB-CK-XN-04-001", headers=h).json()
    assert proof["spike_cleared"] is False
    assert proof["shadow_cursors"] == 1
    assert proof["excursion_closed"] is False
    assert proof["cursor_spike_consistent"] is False

    rows = c.get("/api/excursions", headers=h).json()["items"]
    shadow = next(x for x in rows if x["id"] == shadow_id)
    assert shadow["status"] == "拟开"
    assert shadow["cursor_spike_consistent"] is False
    assert shadow["spike_cleared"] is False

    # 影子记录不得被自动晋升
    with db.connect() as conn:
        still = conn.execute(
            "SELECT status FROM excursion_cursors WHERE id=?", (shadow_id,)
        ).fetchone()
    assert still["status"] == "拟开"


def test_c_contention_serializes_on_excursion_cursor_lock():
    """争用失败测：双窗同批抢开须串行占超温游标锁，后到只记痕。"""
    c1 = TestClient(app)
    c2 = TestClient(app)
    h_admin = _auth(c1, "admin")
    h_ops = _auth(c2, "ops")
    batch = _batch(c1, h_admin, "TB-CK-HD-01-001")
    assert batch["clearing"]["spike_cleared"] is True
    lock_key = f"excursion:{batch['id']}"
    before = _cursor_rows(batch["id"])

    held = c1.post(
        "/api/locks/acquire", headers=h_admin, json={"lock_key": lock_key}
    )
    assert held.status_code == 200

    # 第二窗口同批抢开：必须被超温游标锁挡下
    r = c2.post(
        "/api/excursions/open",
        headers=h_ops,
        json={"batch_id": batch["id"]},
    )
    assert r.status_code == 409, r.text
    assert _cursor_rows(batch["id"]) == before

    contention = c2.get("/api/contention", headers=h_admin).json()["items"]
    assert any(
        x["lock_key"] == lock_key and x["actor"] == "ops" for x in contention
    )

    # 串行后单窗口开户成功，且同批只多一条正式游标
    c1.post(
        "/api/locks/release", headers=h_admin, json={"lock_key": lock_key}
    )
    ok = c1.post(
        "/api/excursions/open",
        headers=h_admin,
        json={"batch_id": batch["id"]},
    )
    assert ok.status_code == 200, ok.text
    assert len(_cursor_rows(batch["id"])) == len(before) + 1


def test_d_double_window_race_never_double_opens():
    """争用补测：双窗同批无预置锁抢开，只许一个成功、只留一条正式游标。"""
    import threading

    c1 = TestClient(app)
    c2 = TestClient(app)
    h1 = _auth(c1, "admin")
    h2 = _auth(c2, "ops")
    batch = _batch(c1, h1, "TB-CK-HD-01-001")
    assert batch["clearing"]["spike_cleared"] is True
    before = len(_cursor_rows(batch["id"]))

    barrier = threading.Barrier(2)
    results: dict[str, int] = {}

    def _race(name: str, client: TestClient, headers: dict) -> None:
        barrier.wait()
        r = client.post(
            "/api/excursions/open",
            headers=headers,
            json={"batch_id": batch["id"]},
        )
        results[name] = r.status_code

    t1 = threading.Thread(target=_race, args=("admin", c1, h1))
    t2 = threading.Thread(target=_race, args=("ops", c2, h2))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert sorted(results.values()) == [200, 409], results
    rows = _cursor_rows(batch["id"])
    assert len(rows) == before + 1
    assert sum(1 for r in rows if r["status"] == "开放") == 1


def test_e_two_officers_on_same_residual_batch_both_blocked():
    """双人同批（残留超温）同时点开户：串行占超温游标锁，两人都拒开、零增、态零改。"""
    import threading

    c1 = TestClient(app)
    c2 = TestClient(app)
    h1 = _auth(c1, "admin")
    h2 = _auth(c2, "ops")
    batch = _batch(c1, h1, "TB-CK-XN-04-001")
    assert batch["clearing"]["spike_cleared"] is False
    before_status = batch["status"]
    before_cursors = _cursor_rows(batch["id"])

    barrier = threading.Barrier(2)
    results: dict[str, int] = {}

    def _race(name: str, client: TestClient, headers: dict) -> None:
        barrier.wait()
        r = client.post(
            "/api/excursions/open",
            headers=headers,
            json={"batch_id": batch["id"]},
        )
        results[name] = r.status_code

    t1 = threading.Thread(target=_race, args=("admin", c1, h1))
    t2 = threading.Thread(target=_race, args=("ops", c2, h2))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert results == {"admin": 409, "ops": 409}, results
    assert _cursor_rows(batch["id"]) == before_cursors
    assert _batch(c1, h1, "TB-CK-XN-04-001")["status"] == before_status
    # 持有锁的一方必须落清点火偏航痕；争用方只记争用。
    audit = c1.get("/api/audit", headers=h1).json()["items"]
    assert any(
        a["action"] == "清点火偏航" and a["target"] == "TB-CK-XN-04-001"
        for a in audit
    )


def test_g_spectrum_export_refuses_residual_sealed_batch():
    """温谱导出同源：已封批但曲线残留尖峰仍未清，导出一律拒写。"""
    c = TestClient(app)
    h = _auth(c)
    with db.connect() as conn:
        batch = conn.execute(
            "SELECT * FROM temp_batches WHERE batch_code='TB-CK-HD-01-002'"
        ).fetchone()
        noisy = [{"t": "12:00", "c": -19.5}, {"t": "13:00", "c": -14.0}]
        conn.execute(
            "UPDATE temp_batches SET temp_points=? WHERE id=?",
            (json.dumps(noisy, ensure_ascii=False), batch["id"]),
        )
        conn.commit()
    before = c.get("/api/spectrum", headers=h).json()["items"]
    r = c.post(
        "/api/spectrum/export", headers=h, json={"batch_id": batch["id"]}
    )
    assert r.status_code == 409, r.text
    assert c.get("/api/spectrum", headers=h).json()["items"] == before
    audit = c.get("/api/audit", headers=h).json()["items"]
    assert any(
        a["action"] == "清点火偏航" and a["target"] == "TB-CK-HD-01-002"
        for a in audit
    )


def test_five_views_share_one_clearing_source():
    """对账页、探头页、温谱页、批次页、游标页五处尖峰与游标同源一致。"""
    c = TestClient(app)
    h = _auth(c)
    code = "TB-CK-HB-02-001"  # 种子：开放游标 -18℃ 阈，曲线仍留 -15℃ 尖峰

    exc = c.get("/api/excursions", headers=h).json()["items"]
    cursor = next(x for x in exc if x["batch_code"] == code and x["status"] == "开放")
    assert cursor["spike_cleared"] is False
    assert cursor["cursor_spike_consistent"] is False
    assert cursor["residual_peak"] == -15.0

    batch = _batch(c, h, code)
    assert batch["clearing"]["spike_cleared"] is False
    assert batch["clearing"]["cursor_spike_consistent"] is False
    assert batch["clearing"]["residual_peak"] == -15.0

    probes = c.get("/api/probes", headers=h).json()["items"]
    probe = next(p for p in probes if p["probe_code"] == "PR-HB-B1")
    assert probe["clearing"]["cursor_spike_consistent"] is False

    recon = c.get("/api/reconcile/CK-HB-02", headers=h).json()
    assert recon["clearing"]["cursor_spike_consistent"] is False
    assert recon["clearing"]["residual_batches"] >= 1

    proof = c.get(f"/api/proof/batch/{code}", headers=h).json()
    assert proof["spike_cleared"] is False
    assert proof["cursor_spike_consistent"] is False
    assert proof["clearing"]["residual_peak"] == -15.0

    # 温谱页同源：导出一条干净封批温谱，其清点火判定与批次页一致
    sealed = _batch(c, h, "TB-CK-HD-01-002")
    sp = c.post(
        "/api/spectrum/export", headers=h, json={"batch_id": sealed["id"]}
    )
    assert sp.status_code == 200, sp.text
    ref = sp.json()["export_ref"]
    exports = c.get("/api/spectrum", headers=h).json()["items"]
    export = next(x for x in exports if x["export_ref"] == ref)
    assert export["clearing"]["spike_cleared"] is True
    assert export["clearing"]["cursor_spike_consistent"] is True
    one = c.get(f"/api/spectrum/{ref}", headers=h).json()
    assert one["clearing"]["spike_cleared"] is True
    sealed_now = _batch(c, h, "TB-CK-HD-01-002")
    assert (
        one["clearing"]["residual_peak"]
        == sealed_now["clearing"]["residual_peak"]
    )
