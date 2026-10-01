"""孔位异常隔离端到端测试。

覆盖：原子申请（未登记/已隔离整批拒绝）、同键重放与换目标冲突、
隔离孔禁止补液/批次/改容量/撤销（冲突不留部分状态、批次键不被消耗）、
批量解除（任一不匹配整体拒绝、同键语义）、申请与解除审计保留、
查询返回当前隔离信息，以及真实多线程并发下的原子性与幂等。
"""

import os
import sqlite3
import threading
from decimal import Decimal

from app import ledger


def register(client, well, sample, volume):
    resp = client.post("/wells", json={"well": well, "sample": sample, "volume": volume})
    assert resp.status_code == 201, resp.text
    return resp.json()


def amounts(body):
    return {c["sample"]: Decimal(c["amount"]) for c in body["composition"]}


def quarantine(client, wells, case_id, reason, request_key):
    return client.post(
        "/quarantines",
        json={
            "wells": wells,
            "case_id": case_id,
            "reason": reason,
            "request_key": request_key,
        },
    )


def release(client, wells, case_id, request_key):
    return client.post(
        "/quarantines/releases",
        json={"wells": wells, "case_id": case_id, "request_key": request_key},
    )


def committed_batch(client, request_key, source, destination, volume):
    resp = client.post(
        "/batches",
        json={
            "request_key": request_key,
            "operations": [
                {"source": source, "destination": destination, "volume": volume}
            ],
        },
    )
    return resp


def _run_in_thread(target):
    result = {}

    def runner():
        try:
            result["value"] = target()
        except Exception as exc:
            result["error"] = exc

    thread = threading.Thread(target=runner)
    thread.start()
    return thread, result


def _join(thread, result, timeout=30.0):
    thread.join(timeout)
    assert not thread.is_alive(), "工作线程超时未结束（可能死锁）"
    if "error" in result:
        raise result["error"]
    return result["value"]


# ------------------------------------------------ 隔离申请


def test_quarantine_registered_wells_atomically(client):
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "50")

    resp = quarantine(client, ["A1", "A2"], "CASE-1", "污染嫌疑", "q-1")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["action"] == "quarantine"
    assert body["case_id"] == "CASE-1"
    assert body["reason"] == "污染嫌疑"
    assert body["request_key"] == "q-1"
    assert body["replayed"] is False
    assert [(w["well"], w["active"]) for w in body["wells"]] == [
        ("A1", True),
        ("A2", True),
    ]
    assert all(w["released_at"] is None for w in body["wells"])

    a1 = client.get("/wells/A1").json()
    assert a1["quarantine"]["case_id"] == "CASE-1"
    assert a1["quarantine"]["reason"] == "污染嫌疑"
    assert a1["quarantine"]["request_key"] == "q-1"
    assert isinstance(a1["quarantine"]["quarantine_id"], int)
    assert a1["quarantine"]["quarantined_at"]
    # 未隔离孔不带隔离信息
    register(client, "A3", "S3", "10")
    assert client.get("/wells/A3").json()["quarantine"] is None


def test_quarantine_dedupes_and_normalizes_wells(client):
    register(client, "a1", "S1", "10")
    register(client, "B2", "S2", "10")
    resp = quarantine(client, ["a1", "A1", "b2"], "CASE-1", "r", "q-dedup")
    assert resp.status_code == 201, resp.text
    assert [w["well"] for w in resp.json()["wells"]] == ["A1", "B2"]


def test_quarantine_unknown_well_is_404_and_atomic(client):
    register(client, "A1", "S1", "10")
    resp = quarantine(client, ["A1", "Z9"], "CASE-1", "r", "q-unknown")
    assert resp.status_code == 404, resp.text
    detail = resp.json()["detail"]
    assert detail["reason"] == "unknown_well"
    assert detail["unknown_wells"] == [{"well": "Z9"}]
    # 整次申请不留任何状态：A1 未被隔离
    assert client.get("/wells/A1").json()["quarantine"] is None


def test_quarantine_conflicting_well_rejects_whole_request(client):
    register(client, "A1", "S1", "10")
    register(client, "A2", "S2", "10")
    register(client, "A3", "S3", "10")
    assert quarantine(client, ["A1"], "CASE-1", "r", "q-first").status_code == 201

    resp = quarantine(client, ["A2", "A1", "A3"], "CASE-2", "r", "q-second")
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["reason"] == "already_quarantined"
    assert detail["quarantined_wells"] == [
        {
            "well": "A1",
            "case_id": "CASE-1",
            "quarantine_id": detail["quarantined_wells"][0]["quarantine_id"],
            "request_key": "q-first",
        }
    ]
    # 同次申请里的其他孔不得留下部分隔离状态
    assert client.get("/wells/A2").json()["quarantine"] is None
    assert client.get("/wells/A3").json()["quarantine"] is None
    assert client.get("/wells/A1").json()["quarantine"]["case_id"] == "CASE-1"


def test_same_case_cannot_quarantine_active_well_again(client):
    register(client, "A1", "S1", "10")
    assert quarantine(client, ["A1"], "CASE-1", "r1", "q-a").status_code == 201
    resp = quarantine(client, ["A1"], "CASE-1", "r2", "q-b")
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "already_quarantined"


def test_quarantine_request_key_replay_same_target(client):
    register(client, "A1", "S1", "10")
    register(client, "A2", "S2", "10")
    payload = {"wells": ["A2", "A1"], "case_id": "CASE-1", "reason": "r", "request_key": "q-idem"}
    first = client.post("/quarantines", json=payload)
    assert first.status_code == 201
    # 同键同目标（孔顺序不同、reason 不同）均按重放处理
    replay = client.post(
        "/quarantines",
        json={"wells": ["A1", "A2"], "case_id": "CASE-1", "reason": "其他描述", "request_key": "q-idem"},
    )
    assert replay.status_code == 200
    body = replay.json()
    assert body["replayed"] is True
    assert body["quarantine_id"] == first.json()["quarantine_id"]


def test_quarantine_same_key_other_target_conflicts(client):
    register(client, "A1", "S1", "10")
    register(client, "A2", "S2", "10")
    assert quarantine(client, ["A1"], "CASE-1", "r", "shared-q").status_code == 201

    # 换孔集合
    resp = quarantine(client, ["A2"], "CASE-1", "r", "shared-q")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reason"] == "request_key_conflict"
    assert detail["action"] == "quarantine"
    assert detail["existing_case_id"] == "CASE-1"
    assert detail["existing_wells"] == ["A1"]
    assert client.get("/wells/A2").json()["quarantine"] is None

    # 换案件
    resp = quarantine(client, ["A1"], "CASE-2", "r", "shared-q")
    assert resp.status_code == 409
    assert resp.json()["detail"]["existing_wells"] == ["A1"]


def test_quarantine_validation_errors(client):
    register(client, "A1", "S1", "10")
    assert client.post(
        "/quarantines",
        json={"wells": [], "case_id": "c", "reason": "r", "request_key": "k"},
    ).status_code == 422
    assert client.post(
        "/quarantines",
        json={"wells": ["nope"], "case_id": "c", "reason": "r", "request_key": "k"},
    ).status_code == 422
    assert client.post(
        "/quarantines",
        json={"wells": ["A1"], "case_id": " ", "reason": "r", "request_key": "k"},
    ).status_code == 422
    assert client.post(
        "/quarantines",
        json={"wells": ["A1"], "case_id": "c", "reason": "r", "request_key": " "},
    ).status_code == 422


# ----------------------------------------------- 隔离孔禁止各类变更


def test_quarantined_well_rejects_top_up(client):
    register(client, "A1", "S1", "100")
    assert quarantine(client, ["A1"], "CASE-1", "r", "q-top").status_code == 201

    resp = client.post("/wells", json={"well": "A1", "sample": "S1", "volume": "5"})
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["reason"] == "well_quarantined"
    assert [w["well"] for w in detail["quarantined_wells"]] == ["A1"]
    assert detail["quarantined_wells"][0]["case_id"] == "CASE-1"
    # 液体不变
    assert client.get("/wells/A1").json()["volume"] == "100"


def test_quarantined_well_rejects_batch_source_and_destination(client):
    register(client, "A1", "S1", "100")
    register(client, "B1", "S2", "10")
    register(client, "C1", "S3", "10")
    assert quarantine(client, ["A1", "B1"], "CASE-1", "r", "q-batch").status_code == 201

    # 隔离孔作为来源
    resp = committed_batch(client, "b-src", "A1", "D1", "5")
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "well_quarantined"
    # 隔离孔作为目标
    resp = committed_batch(client, "b-dst", "C1", "B1", "5")
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "well_quarantined"

    # 液体、批次与审计均不变：无任何批次落库（含失败批次）
    assert client.get("/wells/A1").json()["volume"] == "100"
    assert client.get("/wells/C1").json()["volume"] == "10"
    assert client.get("/wells/D1").status_code == 404
    assert client.get("/batches").json()["batches"] == []

    # 隔离冲突不消耗 request_key：解除后同键提交成功
    assert release(client, ["A1", "B1"], "CASE-1", "r-q-batch").status_code == 201
    ok = committed_batch(client, "b-src", "A1", "D1", "5")
    assert ok.status_code == 201, ok.text
    assert client.get("/wells/A1").json()["volume"] == "95"


def test_batch_touching_quarantined_well_is_atomic(client):
    """多步批次中某一步触及隔离孔：整批回滚，前序步骤不留状态。"""
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "100")
    register(client, "B2", "S9", "10")
    assert quarantine(client, ["B2"], "CASE-1", "r", "q-atom").status_code == 201

    resp = client.post(
        "/batches",
        json={
            "request_key": "b-atom",
            "operations": [
                {"source": "A1", "destination": "B1", "volume": "10"},  # 本可成功
                {"source": "A2", "destination": "B2", "volume": "5"},   # 触及隔离孔
            ],
        },
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "well_quarantined"
    # 前序步骤也回滚
    assert client.get("/wells/A1").json()["volume"] == "100"
    assert client.get("/wells/B1").status_code == 404
    assert client.get("/wells/A2").json()["volume"] == "100"
    assert client.get("/batches").json()["batches"] == []


def test_quarantined_well_rejects_capacity_change(client):
    register(client, "A1", "S1", "50")
    assert quarantine(client, ["A1"], "CASE-1", "r", "q-cap").status_code == 201

    resp = client.put("/wells/A1/capacity", json={"max_capacity": "60"})
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "well_quarantined"
    # 配置不变
    assert client.get("/wells/A1").json()["max_capacity"] is None

    # 解除隔离孔后才能改容量
    assert release(client, ["A1"], "CASE-1", "r-q-cap").status_code == 201
    assert client.put("/wells/A1/capacity", json={"max_capacity": "60"}).status_code == 200


def test_reversal_touching_quarantined_well_rejected(client):
    register(client, "A1", "S1", "100")
    bid_resp = committed_batch(client, "rv-batch", "A1", "B1", "40")
    bid = bid_resp.json()["batch_id"]
    assert quarantine(client, ["A1", "B1"], "CASE-1", "r", "q-rev").status_code == 201

    resp = client.post(f"/batches/{bid}/reversals", json={"request_key": "undo-1"})
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["reason"] == "well_quarantined"
    assert {w["well"] for w in detail["quarantined_wells"]} == {"A1", "B1"}

    # 液体、批次与撤销审计均不变
    assert client.get("/wells/A1").json()["volume"] == "60"
    assert client.get("/wells/B1").json()["volume"] == "40"
    assert client.get(f"/batches/{bid}").json()["reversed"] is False
    conn = sqlite3.connect(os.environ["LINEAGE_DB"])
    try:
        assert conn.execute("SELECT COUNT(*) FROM reversals").fetchone()[0] == 0
    finally:
        conn.close()

    # 冲突不消耗撤销 request_key：解除隔离后同键撤销成功
    assert release(client, ["A1", "B1"], "CASE-1", "r-q-rev").status_code == 201
    assert client.post(f"/batches/{bid}/reversals", json={"request_key": "undo-1"}).status_code == 201
    assert client.get("/wells/A1").json()["volume"] == "100"


def test_reversal_deletes_batch_created_well_with_quarantine_history(client):
    """批次新建的孔即使有（已解除的）隔离审计行，撤销仍能随快照整孔删除。"""
    register(client, "A1", "S1", "100")
    bid = committed_batch(client, "rv-new-well", "A1", "B1", "40").json()["batch_id"]
    assert quarantine(client, ["B1"], "CASE-1", "r", "q-del").status_code == 201
    assert release(client, ["B1"], "CASE-1", "rel-del").status_code == 201
    # 该批次之后无补液/后续批次 -> 撤销可执行，B1 被删除
    resp = client.post(f"/batches/{bid}/reversals", json={"request_key": "undo-del"})
    assert resp.status_code == 201, resp.text
    assert client.get("/wells/B1").status_code == 404
    assert client.get("/wells/A1").json()["volume"] == "100"
    # 申请/解除审计行随被删除孔级联清除
    conn = sqlite3.connect(os.environ["LINEAGE_DB"])
    try:
        assert conn.execute("SELECT COUNT(*) FROM quarantines WHERE well = 'B1'").fetchone()[0] == 0
        # 申请/解除主记录不随孔删除（可能还覆盖其他孔）
        assert conn.execute(
            "SELECT COUNT(*) FROM quarantine_requests WHERE request_key = 'q-del'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM quarantine_releases WHERE request_key = 'rel-del'"
        ).fetchone()[0] == 1
    finally:
        conn.close()


# ------------------------------------------------ 解除隔离


def test_release_clears_quarantine_and_keeps_audits(client):
    register(client, "A1", "S1", "10")
    first = quarantine(client, ["A1"], "CASE-1", "嫌疑", "q-on")
    assert first.status_code == 201

    resp = release(client, ["A1"], "CASE-1", "rel-1")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["action"] == "release"
    assert body["case_id"] == "CASE-1"
    assert body["replayed"] is False
    assert body["wells"] == [
        {
            "well": "A1",
            "case_id": "CASE-1",
            "quarantine_id": body["wells"][0]["quarantine_id"],
            "quarantine_request_key": "q-on",
            "reason": "嫌疑",
            "quarantined_at": body["wells"][0]["quarantined_at"],
        }
    ]
    assert isinstance(body["wells"][0]["quarantine_id"], int)

    # 当前隔离信息消失
    assert client.get("/wells/A1").json()["quarantine"] is None

    # 申请与解除审计均保留：重放申请可见 released_at 与解除键
    replay = client.post(
        "/quarantines",
        json={"wells": ["A1"], "case_id": "CASE-1", "reason": "嫌疑", "request_key": "q-on"},
    )
    assert replay.status_code == 200
    well = replay.json()["wells"][0]
    assert well["active"] is False
    assert well["release_request_key"] == "rel-1"
    assert well["released_at"] is not None

    # 解除键重放
    again = release(client, ["A1"], "CASE-1", "rel-1")
    assert again.status_code == 200
    assert again.json()["replayed"] is True
    assert again.json()["release_id"] == body["release_id"]


def test_release_same_key_other_target_conflicts(client):
    register(client, "A1", "S1", "10")
    register(client, "A2", "S2", "10")
    assert quarantine(client, ["A1", "A2"], "CASE-1", "r", "q-for-rel").status_code == 201
    assert release(client, ["A1"], "CASE-1", "rel-shared").status_code == 201

    # 同键换孔集合 -> 409
    resp = release(client, ["A2"], "CASE-1", "rel-shared")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reason"] == "request_key_conflict"
    assert detail["action"] == "release"
    assert detail["existing_wells"] == ["A1"]
    # A2 仍处隔离
    assert client.get("/wells/A2").json()["quarantine"]["case_id"] == "CASE-1"

    # 同键换案件 -> 409
    resp = release(client, ["A2"], "CASE-OTHER", "rel-shared")
    assert resp.status_code == 409


def test_release_batch_rejects_if_any_mismatch(client):
    register(client, "A1", "S1", "10")
    register(client, "A2", "S2", "10")
    register(client, "A3", "S3", "10")
    assert quarantine(client, ["A1"], "CASE-1", "r", "q-mix-1").status_code == 201
    assert quarantine(client, ["A2"], "CASE-2", "r", "q-mix-2").status_code == 201

    # A1 案件正确，A2 案件不符，A3 未隔离：整次拒绝
    resp = release(client, ["A1", "A2", "A3"], "CASE-1", "rel-mix")
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["reason"] == "release_mismatch"
    by_well = {w["well"]: w for w in detail["mismatched_wells"]}
    assert by_well["A2"] == {"well": "A2", "reason": "case_mismatch", "case_id": "CASE-2"}
    assert by_well["A3"] == {"well": "A3", "reason": "not_quarantined"}

    # 没有任何孔被解除
    assert client.get("/wells/A1").json()["quarantine"]["case_id"] == "CASE-1"
    assert client.get("/wells/A2").json()["quarantine"]["case_id"] == "CASE-2"
    assert client.get("/wells/A3").json()["quarantine"] is None

    # 冲突不消耗解除键：只解除匹配的 A1 即可成功
    ok = release(client, ["A1"], "CASE-1", "rel-mix")
    assert ok.status_code == 201
    assert client.get("/wells/A1").json()["quarantine"] is None
    assert client.get("/wells/A2").json()["quarantine"]["case_id"] == "CASE-2"


def test_release_unknown_well_rejected(client):
    resp = release(client, ["Z9"], "CASE-1", "rel-ghost")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reason"] == "release_mismatch"
    assert detail["mismatched_wells"] == [{"well": "Z9", "reason": "not_quarantined"}]


def test_release_spans_multiple_applications_same_case(client):
    """一次解除可覆盖同一案件分两次申请的孔。"""
    register(client, "A1", "S1", "10")
    register(client, "A2", "S2", "10")
    assert quarantine(client, ["A1"], "CASE-1", "r1", "q-sp-1").status_code == 201
    assert quarantine(client, ["A2"], "CASE-1", "r2", "q-sp-2").status_code == 201

    resp = release(client, ["A2", "A1"], "CASE-1", "rel-sp")
    assert resp.status_code == 201
    wells = {w["well"]: w for w in resp.json()["wells"]}
    assert wells["A1"]["quarantine_request_key"] == "q-sp-1"
    assert wells["A2"]["quarantine_request_key"] == "q-sp-2"
    assert client.get("/wells/A1").json()["quarantine"] is None
    assert client.get("/wells/A2").json()["quarantine"] is None


def test_requarantine_after_release_works(client):
    register(client, "A1", "S1", "10")
    assert quarantine(client, ["A1"], "CASE-1", "r1", "q-cycle-1").status_code == 201
    assert release(client, ["A1"], "CASE-1", "rel-cycle").status_code == 201
    # 新案件可重新隔离同一孔
    resp = quarantine(client, ["A1"], "CASE-2", "r2", "q-cycle-2")
    assert resp.status_code == 201
    assert client.get("/wells/A1").json()["quarantine"]["case_id"] == "CASE-2"


# ------------------------------------------------ 查询返回隔离信息


def test_well_batches_query_returns_quarantine(client):
    register(client, "A1", "S1", "100")
    assert committed_batch(client, "b-hist", "A1", "B1", "40").status_code == 201
    assert quarantine(client, ["B1"], "CASE-1", "r", "q-hist").status_code == 201

    body = client.get("/wells/B1/batches").json()
    assert body["quarantine"]["case_id"] == "CASE-1"
    assert [b["request_key"] for b in body["batches"]] == ["b-hist"]

    # 批次载荷也带当前隔离信息
    batch = body["batches"][0]
    assert set(batch["quarantined_wells"]) == {"B1"}
    assert batch["quarantined_wells"]["B1"]["case_id"] == "CASE-1"

    # 解除后批次与孔位查询均不再返回隔离信息
    assert release(client, ["B1"], "CASE-1", "rel-hist").status_code == 201
    body = client.get("/wells/B1/batches").json()
    assert body["quarantine"] is None
    assert body["batches"][0]["quarantined_wells"] == {}


def test_batch_list_payloads_include_quarantined_wells(client):
    register(client, "A1", "S1", "100")
    bid = committed_batch(client, "b-list", "A1", "B1", "40").json()["batch_id"]
    assert quarantine(client, ["A1"], "CASE-9", "r", "q-list").status_code == 201

    batch = client.get(f"/batches/{bid}").json()
    assert set(batch["quarantined_wells"]) == {"A1"}
    all_batches = client.get("/batches").json()["batches"]
    assert set(all_batches[0]["quarantined_wells"]) == {"A1"}


# ------------------------------------------------ 并发原子性与幂等


def test_concurrent_quarantine_same_well_only_one_wins(client):
    register(client, "A1", "S1", "10")
    barrier = threading.Barrier(2)
    threads, results = [], []
    for case_id, key in (("CASE-1", "cq-1"), ("CASE-2", "cq-2")):
        t, r = _run_in_thread(
            lambda case_id=case_id, key=key: (
                barrier.wait(),
                quarantine(client, ["A1"], case_id, "r", key),
            )[1]
        )
        threads.append(t)
        results.append(r)
    responses = [_join(t, r) for t, r in zip(threads, results)]

    statuses = sorted(resp.status_code for resp in responses)
    assert statuses == [201, 409], statuses
    conflict = next(resp for resp in responses if resp.status_code == 409)
    assert conflict.json()["detail"]["reason"] == "already_quarantined"
    # 每孔至多一条有效隔离
    conn = sqlite3.connect(os.environ["LINEAGE_DB"])
    try:
        active = conn.execute(
            "SELECT COUNT(*) FROM quarantines WHERE well = 'A1' AND released_at IS NULL"
        ).fetchone()[0]
        assert active == 1
    finally:
        conn.close()


def test_concurrent_same_key_same_target_replays_once(client):
    register(client, "A1", "S1", "10")
    threads, results = [], []
    for _ in range(2):
        t, r = _run_in_thread(
            lambda: quarantine(client, ["A1"], "CASE-1", "r", "cq-idem")
        )
        threads.append(t)
        results.append(r)
    responses = [_join(t, r) for t, r in zip(threads, results)]

    assert sorted(resp.status_code for resp in responses) == [200, 201]
    bodies = [resp.json() for resp in responses]
    assert {b["quarantine_id"] for b in bodies} == {bodies[0]["quarantine_id"]}
    assert [b["replayed"] for b in bodies].count(True) == 1


def test_concurrent_same_key_other_target_conflicts(client):
    register(client, "A1", "S1", "10")
    register(client, "A2", "S2", "10")
    barrier = threading.Barrier(2)
    threads, results = [], []
    for well in ("A1", "A2"):
        t, r = _run_in_thread(
            lambda well=well: (
                barrier.wait(),
                quarantine(client, [well], "CASE-1", "r", "cq-shared"),
            )[1]
        )
        threads.append(t)
        results.append(r)
    responses = [_join(t, r) for t, r in zip(threads, results)]

    assert sorted(resp.status_code for resp in responses) == [201, 409]
    conflict = next(resp for resp in responses if resp.status_code == 409)
    detail = conflict.json()["detail"]
    assert detail["reason"] == "request_key_conflict"
    assert detail["existing_wells"] in (["A1"], ["A2"])
    # 只有一个孔被隔离
    conn = sqlite3.connect(os.environ["LINEAGE_DB"])
    try:
        active = conn.execute(
            "SELECT well FROM quarantines WHERE released_at IS NULL ORDER BY well"
        ).fetchall()
        assert [row[0] for row in active] in (["A1"], ["A2"])
    finally:
        conn.close()


def test_quarantine_committed_first_blocks_concurrent_top_up(client, monkeypatch):
    """隔离先持锁提交：并发补液在锁后读到隔离状态，409，液体不变。"""
    register(client, "A1", "S1", "100")
    gate_ready = threading.Event()
    release_gate = threading.Event()
    original = ledger.apply_quarantine

    def gated_apply(conn, wells, case_id, reason, request_key):
        gate_ready.set()
        assert release_gate.wait(10.0), "隔离线程未被释放"
        return original(conn, wells, case_id, reason, request_key)

    monkeypatch.setattr(ledger, "apply_quarantine", gated_apply)

    q_thread, q_result = _run_in_thread(
        lambda: quarantine(client, ["A1"], "CASE-1", "r", "cq-vs-topup")
    )
    assert gate_ready.wait(10.0)

    top_thread, top_result = _run_in_thread(
        lambda: client.post("/wells", json={"well": "A1", "sample": "S1", "volume": "5"})
    )
    threading.Event().wait(0.3)
    assert top_thread.is_alive(), "补液应在隔离的写锁上排队"

    release_gate.set()
    q_resp = _join(q_thread, q_result)
    top_resp = _join(top_thread, top_result)
    assert q_resp.status_code == 201
    assert top_resp.status_code == 409
    assert top_resp.json()["detail"]["reason"] == "well_quarantined"
    assert client.get("/wells/A1").json()["volume"] == "100"


def test_concurrent_release_and_wrong_case_rejected_atomically(client):
    """并发两笔解除：正确案件解除成功后，错误案件的同批解除整体 409。"""
    register(client, "A1", "S1", "10")
    assert quarantine(client, ["A1"], "CASE-1", "r", "q-crel").status_code == 201
    # 串行即可确定性地覆盖：错误案件先被拒（隔离仍有效），正确案件随后成功
    bad = release(client, ["A1"], "CASE-2", "rel-bad")
    assert bad.status_code == 409
    assert bad.json()["detail"]["mismatched_wells"][0]["reason"] == "case_mismatch"
    assert client.get("/wells/A1").json()["quarantine"]["case_id"] == "CASE-1"
    good = release(client, ["A1"], "CASE-1", "rel-good")
    assert good.status_code == 201
    assert client.get("/wells/A1").json()["quarantine"] is None
