"""微孔板样本谱系追踪 API。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Response

from . import db as db_module
from . import ledger
from .schemas import (
    BatchCreate,
    CapacityUpdate,
    QuarantineCreate,
    QuarantineReleaseCreate,
    ReversalCreate,
    WellCreate,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    db_module.init_db()
    yield


app = FastAPI(
    title="微孔板样本谱系追踪 API",
    version="1.0.0",
    description="登记孔位初始样本，原子执行批量移液，追踪各初始样本在每个孔位中的占比。",
    lifespan=lifespan,
)


def get_db() -> Iterator[sqlite3.Connection]:
    conn = db_module.connect()
    try:
        yield conn
    finally:
        conn.close()


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


# ---------------------------------------------------------------- 孔位

def _capacity_exceeded_detail(exc: ledger.CapacityExceeded, **extra) -> dict:
    """容量超限 409 的统一载荷：超限孔位、当时体积、拟加入量与上限。"""
    return {
        "message": str(exc),
        "reason": "capacity_exceeded",
        "well": exc.well,
        "volume": ledger.dec_str(exc.volume),
        "adding": ledger.dec_str(exc.adding),
        "max_capacity": ledger.dec_str(exc.max_capacity),
        **extra,
    }


def _well_quarantined_detail(exc: ledger.WellQuarantined, **extra) -> dict:
    """触及隔离孔 409 的统一载荷：逐个列出隔离孔、案件与隔离申请。"""
    return {
        "message": str(exc),
        "reason": "well_quarantined",
        "quarantined_wells": exc.wells,
        **extra,
    }


@app.post("/wells", status_code=201)
def register_well(payload: WellCreate, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    """登记孔位中的初始样本与体积；同一孔位重复登记视为补液混匀。

    孔位设有容量上限时，补液后体积超过上限返回 409，液体状态不变。
    孔位处于异常隔离中时返回 409，禁止补液。
    """
    try:
        with db_module.write_transaction(conn):
            # 立即取写锁：读改写与并发撤销/批次/容量变更串行，补液不会被旧快照覆盖
            ledger.register_initial_sample(conn, payload.well, payload.sample, payload.volume)
    except ledger.CapacityExceeded as exc:
        raise HTTPException(status_code=409, detail=_capacity_exceeded_detail(exc))
    except ledger.WellQuarantined as exc:
        raise HTTPException(status_code=409, detail=_well_quarantined_detail(exc))
    return ledger.well_state(conn, payload.well)


@app.put("/wells/{well}/capacity")
def set_capacity(
    well: str,
    payload: CapacityUpdate,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """设置或取消孔位的最大容量（"max_capacity": null 表示取消，恢复不限）。

    新上限低于孔位当前体积时返回 409 且配置不变。容量变更与补液、批次、
    撤销、隔离共用同一把写锁串行：先提交者决定后续校验看到的状态。
    隔离中的孔位不得修改容量，返回 409 且配置不变。
    """
    well = well.strip().upper()
    try:
        with db_module.write_transaction(conn):
            ledger.set_well_capacity(conn, well, payload.max_capacity)
    except ledger.WellNotFound:
        raise HTTPException(status_code=404, detail=f"孔位 {well} 尚未登记")
    except ledger.WellQuarantined as exc:
        raise HTTPException(status_code=409, detail=_well_quarantined_detail(exc))
    except ledger.CapacityConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": str(exc),
                "reason": "capacity_below_volume",
                "well": exc.well,
                "volume": ledger.dec_str(exc.volume),
                "max_capacity": ledger.dec_str(exc.max_capacity),
            },
        )
    return ledger.well_state(conn, well)


@app.get("/wells/{well}")
def get_well(well: str, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    """查询孔位当前体积与各初始样本占比。"""
    state = ledger.well_state(conn, well.strip().upper())
    if state is None:
        raise HTTPException(status_code=404, detail=f"孔位 {well} 尚未登记")
    return state


@app.get("/wells/{well}/batches")
def get_well_batches(well: str, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    """查询涉及该孔位（作为来源或目标）的完整批次记录。"""
    well = well.strip().upper()
    if ledger.get_well_volume(conn, well) is None:
        raise HTTPException(status_code=404, detail=f"孔位 {well} 尚未登记")
    rows = conn.execute(
        "SELECT DISTINCT batch_id FROM operations WHERE source = ? OR destination = ? "
        "ORDER BY batch_id",
        (well, well),
    ).fetchall()
    return {
        "well": well,
        "quarantine": ledger.quarantine_payload(conn, well),
        "batches": [ledger.batch_payload(conn, row["batch_id"]) for row in rows],
    }


# ---------------------------------------------------------- 孔位异常隔离

def _quarantine_key_conflict_detail(
    action: str, request_key: str, existing_case_id: str, existing_wells: list[str]
) -> dict:
    """隔离/解除 request_key 换目标的 409 载荷：列出键原有案件与孔集合。"""
    return {
        "message": (
            f"request_key {request_key} 已用于案件 {existing_case_id} 的"
            f"孔集合 {existing_wells}，不能换目标重用"
        ),
        "reason": "request_key_conflict",
        "action": action,
        "request_key": request_key,
        "existing_case_id": existing_case_id,
        "existing_wells": existing_wells,
    }


def _request_wells(conn: sqlite3.Connection, request_id: int) -> list[str]:
    return [
        r["well"]
        for r in conn.execute(
            "SELECT well FROM quarantines WHERE request_id = ? ORDER BY well",
            (request_id,),
        ).fetchall()
    ]


def _release_wells(conn: sqlite3.Connection, release_id: int) -> list[str]:
    return [
        r["well"]
        for r in conn.execute(
            "SELECT well FROM quarantines WHERE release_id = ? ORDER BY well",
            (release_id,),
        ).fetchall()
    ]


@app.post("/quarantines", status_code=201)
def create_quarantine(
    payload: QuarantineCreate,
    response: Response,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """把一组已登记孔按案件原子隔离。

    同一事务内先确认全部孔已登记且尚未隔离，再建立隔离记录；任一孔未登记
    （404）或已被（其他案件或本案件的）隔离占用（409），整次申请回滚，
    不留下任何部分状态。request_key 幂等：同键同案件同孔集合重放返回首次
    结果（200，replayed=true）；同键换案件或换孔集合返回 409。
    """
    # 事务外快速路径：无并发时直接给出重放/换目标结论，免取写锁。
    existing = ledger.find_quarantine_request_by_key(conn, payload.request_key)
    if existing is not None:
        existing_wells = _request_wells(conn, int(existing["id"]))
        if existing["case_id"] != payload.case_id or set(existing_wells) != set(payload.wells):
            raise HTTPException(
                status_code=409,
                detail=_quarantine_key_conflict_detail(
                    "quarantine", payload.request_key, existing["case_id"], existing_wells
                ),
            )
        response.status_code = 200
        return ledger.get_quarantine_request(conn, int(existing["id"]), replayed=True)

    try:
        # BEGIN IMMEDIATE 立即取锁：幂等复核、孔位/隔离校验与隔离行写入
        # 持同一把锁完成，与并发补液/批次/撤销/解除串行。
        with db_module.write_transaction(conn):
            row = ledger.find_quarantine_request_by_key(conn, payload.request_key)
            if row is not None:
                existing_wells = _request_wells(conn, int(row["id"]))
                if row["case_id"] != payload.case_id or set(existing_wells) != set(payload.wells):
                    raise HTTPException(
                        status_code=409,
                        detail=_quarantine_key_conflict_detail(
                            "quarantine", payload.request_key, row["case_id"], existing_wells
                        ),
                    )
                response.status_code = 200
                return ledger.get_quarantine_request(conn, int(row["id"]), replayed=True)
            request_id = ledger.apply_quarantine(
                conn, payload.wells, payload.case_id, payload.reason, payload.request_key
            )
    except ledger.QuarantineConflict as exc:
        # 冲突随事务整体回滚：无隔离行、无申请行，request_key 不被消耗
        status_code = 404 if exc.reason == "unknown_well" else 409
        field = "unknown_wells" if exc.reason == "unknown_well" else "quarantined_wells"
        raise HTTPException(
            status_code=status_code,
            detail={"message": exc.message, "reason": exc.reason, field: exc.wells},
        )
    except sqlite3.IntegrityError:
        # 兜底：极端并发下 request_key 唯一约束拦截（正常已被锁内复核覆盖）
        row = ledger.find_quarantine_request_by_key(conn, payload.request_key)
        existing_wells = _request_wells(conn, int(row["id"]))
        if row["case_id"] != payload.case_id or set(existing_wells) != set(payload.wells):
            raise HTTPException(
                status_code=409,
                detail=_quarantine_key_conflict_detail(
                    "quarantine", payload.request_key, row["case_id"], existing_wells
                ),
            )
        response.status_code = 200
        return ledger.get_quarantine_request(conn, int(row["id"]), replayed=True)

    response.status_code = 201
    return ledger.get_quarantine_request(conn, request_id)


@app.post("/quarantines/releases", status_code=201)
def release_quarantine_endpoint(
    payload: QuarantineReleaseCreate,
    response: Response,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """原子解除一组当前由同一案件隔离的孔。

    每个孔都必须当前处于该 case_id 的隔离中；任一孔未隔离或案件不符，
    整次解除回滚（409），任何孔都不解除。request_key 幂等：同键同案件
    同孔集合重放返回首次结果（200）；同键换案件或换孔集合返回 409。
    申请与解除审计均保留。
    """
    existing = ledger.find_quarantine_release_by_key(conn, payload.request_key)
    if existing is not None:
        existing_wells = _release_wells(conn, int(existing["id"]))
        if existing["case_id"] != payload.case_id or set(existing_wells) != set(payload.wells):
            raise HTTPException(
                status_code=409,
                detail=_quarantine_key_conflict_detail(
                    "release", payload.request_key, existing["case_id"], existing_wells
                ),
            )
        response.status_code = 200
        return ledger.get_quarantine_release(conn, int(existing["id"]), replayed=True)

    try:
        with db_module.write_transaction(conn):
            row = ledger.find_quarantine_release_by_key(conn, payload.request_key)
            if row is not None:
                existing_wells = _release_wells(conn, int(row["id"]))
                if row["case_id"] != payload.case_id or set(existing_wells) != set(payload.wells):
                    raise HTTPException(
                        status_code=409,
                        detail=_quarantine_key_conflict_detail(
                            "release", payload.request_key, row["case_id"], existing_wells
                        ),
                    )
                response.status_code = 200
                return ledger.get_quarantine_release(conn, int(row["id"]), replayed=True)
            release_id = ledger.release_quarantine(
                conn, payload.wells, payload.case_id, payload.request_key
            )
    except ledger.QuarantineConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={"message": exc.message, "reason": exc.reason, "mismatched_wells": exc.wells},
        )
    except sqlite3.IntegrityError:
        row = ledger.find_quarantine_release_by_key(conn, payload.request_key)
        existing_wells = _release_wells(conn, int(row["id"]))
        if row["case_id"] != payload.case_id or set(existing_wells) != set(payload.wells):
            raise HTTPException(
                status_code=409,
                detail=_quarantine_key_conflict_detail(
                    "release", payload.request_key, row["case_id"], existing_wells
                ),
            )
        response.status_code = 200
        return ledger.get_quarantine_release(conn, int(row["id"]), replayed=True)

    response.status_code = 201
    return ledger.get_quarantine_release(conn, release_id)


# ---------------------------------------------------------------- 批次

@app.post("/batches")
def submit_batch(
    payload: BatchCreate,
    response: Response,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """提交一批移液操作：单事务按序执行，任一步体积不足或目标孔容量超限则整批回滚。

    request_key 幂等：相同键重复提交不会重复执行，直接返回首个结果。
    """
    existing = ledger.find_batch_by_key(conn, payload.request_key)
    if existing is not None:
        response.status_code = 200
        return ledger.batch_payload(conn, existing["id"], replayed=True)

    try:
        # 立即取写事务：幂等检查、体积校验与全部写入持锁完成，与并发
        # 补液/撤销/批次从校验开始串行
        with db_module.write_transaction(conn):
            batch_id = ledger.execute_batch(conn, payload.request_key, payload.operations)
    except ledger.WellQuarantined as exc:
        # 隔离冲突发生在批次行写入之前：事务整体回滚，不留失败批次、
        # 不消耗 request_key（冲突排除后同键可正常提交），液体与审计均不变。
        raise HTTPException(status_code=409, detail=_well_quarantined_detail(exc))
    except ledger.InsufficientVolume as exc:
        # 状态已回滚；把失败批次也记录下来，消耗掉这个 request_key
        try:
            with db_module.write_transaction(conn):
                batch_id = ledger.insert_batch(conn, payload.request_key, "failed", error=str(exc))
        except sqlite3.IntegrityError:
            row = ledger.find_batch_by_key(conn, payload.request_key)
            response.status_code = 200
            return ledger.batch_payload(conn, row["id"], replayed=True)
        raise HTTPException(
            status_code=409,
            detail={
                "message": str(exc),
                "batch_id": batch_id,
                "well": exc.well,
                "available": ledger.dec_str(exc.available),
                "requested": ledger.dec_str(exc.requested),
            },
        )
    except ledger.CapacityExceeded as exc:
        # 与体积不足同理：状态已回滚；失败批次落库并消耗 request_key
        try:
            with db_module.write_transaction(conn):
                batch_id = ledger.insert_batch(conn, payload.request_key, "failed", error=str(exc))
        except sqlite3.IntegrityError:
            row = ledger.find_batch_by_key(conn, payload.request_key)
            response.status_code = 200
            return ledger.batch_payload(conn, row["id"], replayed=True)
        raise HTTPException(
            status_code=409,
            detail=_capacity_exceeded_detail(exc, batch_id=batch_id),
        )
    except sqlite3.IntegrityError:
        # 并发下同一 request_key 已被写入：按重放处理，不重复执行
        row = ledger.find_batch_by_key(conn, payload.request_key)
        response.status_code = 200
        return ledger.batch_payload(conn, row["id"], replayed=True)

    response.status_code = 201
    return ledger.batch_payload(conn, batch_id)


@app.get("/batches")
def list_batches(conn: sqlite3.Connection = Depends(get_db)) -> dict:
    """全部批次记录（含失败批次）。"""
    rows = conn.execute("SELECT id FROM batches ORDER BY id").fetchall()
    return {"batches": [ledger.batch_payload(conn, row["id"]) for row in rows]}


@app.get("/batches/{batch_id}")
def get_batch(batch_id: int, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    """单个批次的完整记录。"""
    row = conn.execute("SELECT id FROM batches WHERE id = ?", (batch_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"批次 {batch_id} 不存在")
    return ledger.batch_payload(conn, batch_id)


def _request_key_conflict_detail(request_key: str, batch_id: int, existing_batch_id: int) -> dict:
    return {
        "message": (
            f"request_key {request_key} 已用于撤销批次 "
            f"{existing_batch_id}，不能再用于批次 {batch_id}"
        ),
        "reason": "request_key_conflict",
        "batch_id": batch_id,
        "existing_batch_id": existing_batch_id,
    }


@app.post("/batches/{batch_id}/reversals")
def reverse_batch(
    batch_id: int,
    payload: ReversalCreate,
    response: Response,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """撤销一个已提交批次：原子恢复其批前快照并追加审计记录，不删除原批次。

    仅当目标批次已提交、尚未撤销、保存了批前快照，之后没有补液或已提交
    批次触及相关孔位，且快照恢复后的各孔体积不超过其当前容量上限时可撤销；
    条件不符返回 409 并列出阻塞孔位或超限孔位，液体状态保持不变。
    request_key 幂等：同键同目标返回首次结果（200），同键换目标返回 409。
    """
    # 事务外快速路径：无并发时直接给出重放/换目标结论，免取写锁。
    existing = ledger.find_reversal_by_key(conn, payload.request_key)
    if existing is not None:
        if int(existing["batch_id"]) != batch_id:
            raise HTTPException(
                status_code=409,
                detail=_request_key_conflict_detail(
                    payload.request_key, batch_id, int(existing["batch_id"])
                ),
            )
        response.status_code = 200
        return ledger.get_reversal(conn, int(existing["id"]), replayed=True)

    try:
        # BEGIN IMMEDIATE 立即取锁：幂等复核、阻塞检查与快照恢复持同一把
        # 锁完成，期间并发补液/批次只能在锁上等待；先提交的补液/批次必然
        # 已对本事务可见，阻塞检查据此返回 409，液体状态不被旧快照覆盖。
        with db_module.write_transaction(conn):
            # 目标批次是否已被撤销（排除当前事务尚未提交的自见行——同键首次
            # 尝试若因冲突回滚、键未消耗而重试，会走到这里，此时 INSERT 尚未
            # 在本次事务执行）。被另一个键撤销是硬冲突，必须先于键复核判定，
            # 否则同键换目标会在这里误命中自己即将插入的键。
            reversal = conn.execute(
                "SELECT id FROM reversals WHERE batch_id = ? AND request_key != ?",
                (batch_id, payload.request_key),
            ).fetchone()
            if reversal is not None:
                raise ledger.ReversalConflict(
                    "already_reversed", f"批次 {batch_id} 已经撤销，不能重复撤销"
                )
            # 并发下另一同键撤销可能已在此期间提交：持锁后做键复核，保证
            # 同键同目标仍按重放（200）处理，而非误判为重复撤销。
            row = ledger.find_reversal_by_key(conn, payload.request_key)
            if row is not None:
                if int(row["batch_id"]) != batch_id:
                    raise HTTPException(
                        status_code=409,
                        detail=_request_key_conflict_detail(
                            payload.request_key, batch_id, int(row["batch_id"])
                        ),
                    )
                response.status_code = 200
                return ledger.get_reversal(conn, int(row["id"]), replayed=True)
            reversal_id = ledger.apply_reversal(conn, batch_id, payload.request_key)
    except ledger.ReversalConflict as exc:
        if exc.reason == "not_found":
            raise HTTPException(status_code=404, detail=exc.message)
        detail: dict = {"message": exc.message, "reason": exc.reason, "batch_id": batch_id}
        if exc.blockers:
            detail["blocking_wells"] = exc.blockers
        if exc.exceeding:
            detail["exceeding_wells"] = exc.exceeding
        if exc.quarantined:
            detail["quarantined_wells"] = exc.quarantined
        raise HTTPException(status_code=409, detail=detail)
    except sqlite3.IntegrityError:
        # 兜底：极端并发下 request_key 唯一约束拦截（正常已被锁内复核覆盖）
        row = ledger.find_reversal_by_key(conn, payload.request_key)
        if row is not None:
            if int(row["batch_id"]) != batch_id:
                raise HTTPException(
                    status_code=409,
                    detail=_request_key_conflict_detail(
                        payload.request_key, batch_id, int(row["batch_id"])
                    ),
                )
            response.status_code = 200
            return ledger.get_reversal(conn, int(row["id"]), replayed=True)
        raise HTTPException(
            status_code=409,
            detail={
                "message": f"批次 {batch_id} 已经撤销，不能重复撤销",
                "reason": "already_reversed",
                "batch_id": batch_id,
            },
        )

    response.status_code = 201
    return ledger.get_reversal(conn, reversal_id)
