"""谱系台账核心逻辑：所有体积运算一律使用 Decimal，不用 float。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

# 统一量化到小数点后 9 位，避免 1/3 这类无限循环小数在库中无限膨胀。
QUANTUM = Decimal("0.000000001")
ZERO = Decimal("0")


class InsufficientVolume(Exception):
    """某一步吸取体积超过来源孔当前体积，触发整批回滚。"""

    def __init__(self, well: str, available: Decimal, requested: Decimal) -> None:
        self.well = well
        self.available = available
        self.requested = requested
        super().__init__(f"孔位 {well} 体积不足：可用 {available}，请求吸取 {requested}")


class WellNotFound(Exception):
    """孔位尚未登记。"""

    def __init__(self, well: str) -> None:
        self.well = well
        super().__init__(f"孔位 {well} 尚未登记")


class CapacityExceeded(Exception):
    """补液/移液将使目标孔超过其容量上限：补液整体拒绝，批次整批回滚。"""

    def __init__(
        self, well: str, volume: Decimal, adding: Decimal, max_capacity: Decimal
    ) -> None:
        self.well = well
        self.volume = volume  # 加入之前的当时体积
        self.adding = adding  # 拟加入量
        self.max_capacity = max_capacity
        super().__init__(
            f"孔位 {well} 容量超限：当前体积 {volume}，拟加入 {adding}，上限 {max_capacity}"
        )


class CapacityConflict(Exception):
    """新容量上限低于孔位当前体积：拒绝变更，容量配置保持不变。"""

    def __init__(self, well: str, volume: Decimal, max_capacity: Decimal) -> None:
        self.well = well
        self.volume = volume
        self.max_capacity = max_capacity
        super().__init__(
            f"孔位 {well} 当前体积 {volume} 高于新容量上限 {max_capacity}，容量配置不变"
        )


class WellQuarantined(Exception):
    """操作触及当前处于异常隔离的孔：补液/批次/改容量/撤销整次拒绝，状态不变。"""

    def __init__(self, wells: list[dict]) -> None:
        # wells: [{well, case_id, quarantine_id, request_key}]
        self.wells = wells
        labels = "、".join(f"{w['well']}（案件 {w['case_id']}）" for w in wells)
        super().__init__(f"孔位已隔离，禁止补液/移液/改容量/撤销：{labels}")


class QuarantineConflict(Exception):
    """隔离申请/解除条件不满足：孔未登记、已在隔离中，或解除时孔与案件不匹配。

    由调用方随事务整体回滚：隔离行、解除行与液体/批次状态均不变。
    """

    def __init__(self, reason: str, message: str, wells: list[dict] | None = None) -> None:
        self.reason = reason
        self.message = message
        self.wells = wells or []
        super().__init__(message)


class QuarantineRequestConflict(Exception):
    """隔离/解除 request_key 已被用于不同目标（案件或孔集合不同）。"""

    def __init__(
        self,
        action: str,
        request_key: str,
        existing_case_id: str,
        existing_wells: list[str],
    ) -> None:
        self.action = action  # "quarantine" / "release"
        self.request_key = request_key
        self.existing_case_id = existing_case_id
        self.existing_wells = existing_wells
        super().__init__(
            f"request_key {request_key} 已用于{existing_case_id} 案件的"
            f"孔集合 {existing_wells}，不能换目标重用"
        )


def q(value: Decimal) -> Decimal:
    """量化到统一精度。quantize 单调不减，组分量不会被放大到超过原值。"""
    return value.quantize(QUANTUM, rounding=ROUND_HALF_UP)


def dec_str(value: Decimal) -> str:
    """Decimal -> 紧凑字符串（去掉尾随 0，避免科学计数法）。"""
    return format(value.normalize(), "f")


# ---------------------------------------------------------------- 孔位与组成

def ensure_sample(conn: sqlite3.Connection, name: str) -> int:
    conn.execute("INSERT OR IGNORE INTO samples (name) VALUES (?)", (name,))
    row = conn.execute("SELECT id FROM samples WHERE name = ?", (name,)).fetchone()
    return int(row["id"])


def ensure_well(conn: sqlite3.Connection, well: str) -> None:
    conn.execute("INSERT OR IGNORE INTO wells (well, volume) VALUES (?, '0')", (well,))


def get_well_volume(conn: sqlite3.Connection, well: str) -> Decimal | None:
    row = conn.execute("SELECT volume FROM wells WHERE well = ?", (well,)).fetchone()
    return None if row is None else Decimal(row["volume"])


def get_well_capacity(conn: sqlite3.Connection, well: str) -> Decimal | None:
    """孔位容量上限；孔未登记或不限（NULL）时返回 None。"""
    row = conn.execute("SELECT max_capacity FROM wells WHERE well = ?", (well,)).fetchone()
    if row is None or row["max_capacity"] is None:
        return None
    return Decimal(row["max_capacity"])


def set_well_capacity(conn: sqlite3.Connection, well: str, capacity: Decimal | None) -> None:
    """设置（capacity）或取消（None）孔位容量上限。

    新上限低于当前体积时抛 CapacityConflict，配置保持不变；孔未登记抛
    WellNotFound；孔处于隔离中时抛 WellQuarantined。调用方须在写事务内
    调用，使容量变更与补液/批次/撤销/隔离串行。
    """
    row = conn.execute("SELECT volume FROM wells WHERE well = ?", (well,)).fetchone()
    if row is None:
        raise WellNotFound(well)
    blocked = active_quarantine_briefs(conn, [well])
    if blocked:
        raise WellQuarantined(blocked)
    volume = Decimal(row["volume"])
    if capacity is not None and capacity < volume:
        raise CapacityConflict(well, volume, capacity)
    conn.execute(
        "UPDATE wells SET max_capacity = ? WHERE well = ?",
        (None if capacity is None else dec_str(capacity), well),
    )


def _composition_amount(conn: sqlite3.Connection, well: str, sample_id: int) -> Decimal:
    row = conn.execute(
        "SELECT amount FROM well_composition WHERE well = ? AND sample_id = ?",
        (well, sample_id),
    ).fetchone()
    return Decimal(row["amount"]) if row else ZERO


def _set_composition(conn: sqlite3.Connection, well: str, sample_id: int, amount: Decimal) -> None:
    if amount == 0:
        conn.execute(
            "DELETE FROM well_composition WHERE well = ? AND sample_id = ?",
            (well, sample_id),
        )
    else:
        conn.execute(
            "INSERT INTO well_composition (well, sample_id, amount) VALUES (?, ?, ?) "
            "ON CONFLICT(well, sample_id) DO UPDATE SET amount = excluded.amount",
            (well, sample_id, dec_str(amount)),
        )


def _set_volume(conn: sqlite3.Connection, well: str, volume: Decimal) -> None:
    conn.execute("UPDATE wells SET volume = ? WHERE well = ?", (dec_str(volume), well))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record_event(
    conn: sqlite3.Connection,
    well: str,
    event_type: str,
    *,
    batch_id: int | None = None,
    sample: str | None = None,
    volume: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO well_events (well, type, batch_id, sample, volume, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (well, event_type, batch_id, sample, volume, _now()),
    )


def register_initial_sample(conn: sqlite3.Connection, well: str, sample: str, volume: Decimal) -> None:
    """登记初始样本；孔位已有液体时视为补液并立即混匀。

    孔位处于隔离中时抛 WellQuarantined，不做任何变更。
    孔位设有容量上限时，补液后的体积超过上限即抛 CapacityExceeded，
    由调用方回滚，液体状态不变。
    """
    ensure_well(conn, well)
    sample_id = ensure_sample(conn, sample)
    blocked = active_quarantine_briefs(conn, [well])
    if blocked:
        raise WellQuarantined(blocked)
    current = get_well_volume(conn, well)
    added = q(volume)
    capacity = get_well_capacity(conn, well)
    if capacity is not None and current + added > capacity:
        raise CapacityExceeded(well, current, added, capacity)
    _set_volume(conn, well, current + added)
    _set_composition(conn, well, sample_id, q(_composition_amount(conn, well, sample_id) + volume))
    _record_event(conn, well, "top_up", sample=sample, volume=dec_str(added))


def _allocate_move(
    amounts: list[tuple[int, Decimal]], move_units: int
) -> dict[int, int]:
    """按比例把 move_units 个最小计量单位（QUANTUM）分配给来源孔的各样本。

    先取比例份额 amount_i * move_units / total 的整数单位，再把剩余单位
    按小数余量从大到小分配；余量相同时按样本 ID 升序。各样本分得的单位数
    必然不超过其原有单位数：若 floor(x_i) 已等于 units_i，则 x_i 是整数，
    余量为 0，不会再分到额外单位。
    """
    total_units = sum(int(amount / QUANTUM) for _, amount in amounts)
    scaled = [
        (sample_id, int(amount / QUANTUM) * move_units) for sample_id, amount in amounts
    ]
    allocated = {sample_id: value // total_units for sample_id, value in scaled}
    remaining = move_units - sum(allocated.values())
    if remaining:
        ordered = sorted(scaled, key=lambda item: (-(item[1] % total_units), item[0]))
        for sample_id, _ in ordered[:remaining]:
            allocated[sample_id] += 1
    return allocated


def apply_transfer(conn: sqlite3.Connection, source: str, destination: str, volume: Decimal) -> None:
    """执行一次移液：按来源组成占比，以最小计量单位（QUANTUM）整数取走各样本。

    用最大余量法分配整数单位，保证实际带走的组分量之和恰好等于请求体积，
    从而来源孔和目标孔在每一步都满足「组分量之和 = 体积」。
    """
    volume = q(volume)
    src_volume = get_well_volume(conn, source) or ZERO
    if src_volume < volume:
        raise InsufficientVolume(source, src_volume, volume)

    ensure_well(conn, destination)
    rows = conn.execute(
        "SELECT sample_id, amount FROM well_composition WHERE well = ? ORDER BY sample_id",
        (source,),
    ).fetchall()
    amounts = [(row["sample_id"], Decimal(row["amount"])) for row in rows]

    move_units = int(volume / QUANTUM)
    moved_units = _allocate_move(amounts, move_units) if move_units else {}

    moved_total = ZERO
    for sample_id, amount in amounts:
        moved = QUANTUM * moved_units.get(sample_id, 0)
        moved_total += moved
        _set_composition(conn, source, sample_id, amount - moved)
        if moved:
            _set_composition(
                conn,
                destination,
                sample_id,
                _composition_amount(conn, destination, sample_id) + moved,
            )

    # 目标孔容量按本步执行时的当时体积校验：同一批次先移出再移入因此可行；
    # 超限抛 CapacityExceeded，由调用方回滚整批。
    dst_volume = get_well_volume(conn, destination)
    capacity = get_well_capacity(conn, destination)
    if capacity is not None and dst_volume + moved_total > capacity:
        raise CapacityExceeded(destination, dst_volume, moved_total, capacity)

    _set_volume(conn, source, src_volume - moved_total)
    _set_volume(conn, destination, dst_volume + moved_total)


def well_state(conn: sqlite3.Connection, well: str) -> dict | None:
    """孔位当前体积、容量上限（None 为不限）与各初始样本的量与占比。"""
    row = conn.execute(
        "SELECT volume, max_capacity FROM wells WHERE well = ?", (well,)
    ).fetchone()
    if row is None:
        return None
    volume = Decimal(row["volume"])
    rows = conn.execute(
        "SELECT s.name AS sample, wc.amount AS amount "
        "FROM well_composition wc JOIN samples s ON s.id = wc.sample_id "
        "WHERE wc.well = ? ORDER BY s.name",
        (well,),
    ).fetchall()
    composition = []
    for row2 in rows:
        amount = Decimal(row2["amount"])
        fraction = q(amount / volume) if volume > 0 else ZERO
        composition.append(
            {"sample": row2["sample"], "amount": dec_str(amount), "fraction": dec_str(fraction)}
        )
    return {
        "well": well,
        "volume": dec_str(volume),
        "max_capacity": (
            None if row["max_capacity"] is None else dec_str(Decimal(row["max_capacity"]))
        ),
        "composition": composition,
        "quarantine": quarantine_payload(conn, well),
    }


# ---------------------------------------------------------------- 孔位异常隔离

def active_quarantine_rows(
    conn: sqlite3.Connection, wells: list[str]
) -> dict[str, sqlite3.Row]:
    """返回这些孔当前有效（未解除）的隔离行，按孔索引；每孔至多一条。"""
    if not wells:
        return {}
    placeholders = ",".join("?" for _ in wells)
    rows = conn.execute(
        f"SELECT * FROM quarantines WHERE released_at IS NULL "
        f"AND well IN ({placeholders})",
        wells,
    ).fetchall()
    return {r["well"]: r for r in rows}


def active_quarantine_briefs(conn: sqlite3.Connection, wells: list[str]) -> list[dict]:
    """给定孔中处于隔离中的孔的摘要（按 wells 顺序去重），供拒绝载荷使用。"""
    if not wells:
        return []
    placeholders = ",".join("?" for _ in wells)
    rows = conn.execute(
        f"SELECT q.id AS id, q.well AS well, q.case_id AS case_id, r.request_key AS request_key "
        f"FROM quarantines q JOIN quarantine_requests r ON r.id = q.request_id "
        f"WHERE q.released_at IS NULL AND q.well IN ({placeholders})",
        list(dict.fromkeys(wells)),
    ).fetchall()
    by_well = {r["well"]: r for r in rows}
    return [
        {
            "well": well,
            "case_id": by_well[well]["case_id"],
            "quarantine_id": int(by_well[well]["id"]),
            "request_key": by_well[well]["request_key"],
        }
        for well in dict.fromkeys(wells)
        if well in by_well
    ]


def quarantine_payload(conn: sqlite3.Connection, well: str) -> dict | None:
    """孔位当前隔离信息；未隔离（或隔离已解除）返回 None。"""
    rows = active_quarantine_rows(conn, [well])
    row = rows.get(well)
    if row is None:
        return None
    request = conn.execute(
        "SELECT request_key, reason, created_at FROM quarantine_requests WHERE id = ?",
        (row["request_id"],),
    ).fetchone()
    return {
        "case_id": row["case_id"],
        "reason": request["reason"],
        "quarantine_id": int(row["id"]),
        "request_key": request["request_key"],
        "quarantined_at": row["created_at"],
    }


def find_quarantine_request_by_key(
    conn: sqlite3.Connection, request_key: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM quarantine_requests WHERE request_key = ?", (request_key,)
    ).fetchone()


def find_quarantine_release_by_key(
    conn: sqlite3.Connection, request_key: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM quarantine_releases WHERE request_key = ?", (request_key,)
    ).fetchone()


def apply_quarantine(
    conn: sqlite3.Connection, wells: list[str], case_id: str, reason: str, request_key: str
) -> int:
    """在调用方事务内原子隔离一组孔，返回 quarantine_requests.id。

    - 任一孔未登记：QuarantineConflict("unknown_well")，整次回滚；
    - 任一孔已被（任意案件，含同一案件）隔离：QuarantineConflict("already_quarantined")，
      整次回滚，不留下任何隔离行；
    - request_key 唯一冲突由调用方在锁内先做同目标/换目标判定，
      最终由唯一约束兜底（IntegrityError）。
    """
    unknown = [
        well
        for well in wells
        if conn.execute("SELECT 1 FROM wells WHERE well = ?", (well,)).fetchone() is None
    ]
    if unknown:
        raise QuarantineConflict(
            "unknown_well",
            f"孔位尚未登记，不能隔离：{', '.join(unknown)}",
            [{"well": well} for well in unknown],
        )
    blocked = active_quarantine_briefs(conn, wells)
    if blocked:
        raise QuarantineConflict(
            "already_quarantined",
            "部分孔位已被其他案件隔离，整次申请不生效",
            blocked,
        )
    now = _now()
    cur = conn.execute(
        "INSERT INTO quarantine_requests (request_key, case_id, reason, created_at) "
        "VALUES (?, ?, ?, ?)",
        (request_key, case_id, reason, now),
    )
    request_id = int(cur.lastrowid)
    conn.executemany(
        "INSERT INTO quarantines (request_id, well, case_id, created_at) "
        "VALUES (?, ?, ?, ?)",
        [(request_id, well, case_id, now) for well in wells],
    )
    return request_id


def release_quarantine(
    conn: sqlite3.Connection, wells: list[str], case_id: str, request_key: str
) -> int:
    """在调用方事务内原子解除一组孔的隔离，返回 quarantine_releases.id。

    每个孔都必须当前有效隔离且 case_id 与请求一致；任一不匹配（未隔离/
    未登记/案件不符）即 QuarantineConflict，整次回滚，任何孔都不解除。
    """
    active = active_quarantine_rows(conn, wells)
    mismatches: list[dict] = []
    for well in wells:
        row = active.get(well)
        if row is None:
            mismatches.append({"well": well, "reason": "not_quarantined"})
        elif row["case_id"] != case_id:
            mismatches.append(
                {
                    "well": well,
                    "reason": "case_mismatch",
                    "case_id": row["case_id"],
                }
            )
    if mismatches:
        raise QuarantineConflict(
            "release_mismatch",
            "部分孔位当前未由该案件隔离，整次解除不生效",
            mismatches,
        )
    now = _now()
    cur = conn.execute(
        "INSERT INTO quarantine_releases (request_key, case_id, created_at) "
        "VALUES (?, ?, ?)",
        (request_key, case_id, now),
    )
    release_id = int(cur.lastrowid)
    conn.executemany(
        "UPDATE quarantines SET released_at = ?, release_id = ? "
        "WHERE well = ? AND released_at IS NULL",
        [(now, release_id, well) for well in wells],
    )
    return release_id


def get_quarantine_request(
    conn: sqlite3.Connection, request_id: int, replayed: bool = False
) -> dict | None:
    row = conn.execute(
        "SELECT * FROM quarantine_requests WHERE id = ?", (request_id,)
    ).fetchone()
    if row is None:
        return None
    qrows = conn.execute(
        "SELECT * FROM quarantines WHERE request_id = ? ORDER BY well", (request_id,)
    ).fetchall()
    well_payloads = []
    for q in qrows:
        release_key = None
        released_at = q["released_at"]
        if q["release_id"] is not None:
            release_key = conn.execute(
                "SELECT request_key FROM quarantine_releases WHERE id = ?",
                (q["release_id"],),
            ).fetchone()["request_key"]
        well_payloads.append(
            {
                "well": q["well"],
                "active": released_at is None,
                "released_at": released_at,
                "release_request_key": release_key,
            }
        )
    return {
        "action": "quarantine",
        "quarantine_id": row["id"],
        "request_key": row["request_key"],
        "case_id": row["case_id"],
        "reason": row["reason"],
        "created_at": row["created_at"],
        "replayed": replayed,
        "wells": well_payloads,
    }


def get_quarantine_release(
    conn: sqlite3.Connection, release_id: int, replayed: bool = False
) -> dict | None:
    row = conn.execute(
        "SELECT * FROM quarantine_releases WHERE id = ?", (release_id,)
    ).fetchone()
    if row is None:
        return None
    qrows = conn.execute(
        "SELECT * FROM quarantines WHERE release_id = ? ORDER BY well", (release_id,)
    ).fetchall()
    # 解除可横跨同一案件的多次申请：按申请分组保留申请审计
    requests = {
        r["request_id"]: conn.execute(
            "SELECT * FROM quarantine_requests WHERE id = ?", (r["request_id"],)
        ).fetchone()
        for r in qrows
    }
    well_payloads = []
    for q in qrows:
        req = requests[q["request_id"]]
        well_payloads.append(
            {
                "well": q["well"],
                "case_id": q["case_id"],
                "quarantine_id": int(q["id"]),
                "quarantine_request_key": req["request_key"],
                "reason": req["reason"],
                "quarantined_at": q["created_at"],
            }
        )
    return {
        "action": "release",
        "release_id": row["id"],
        "request_key": row["request_key"],
        "case_id": row["case_id"],
        "released_at": row["created_at"],
        "replayed": replayed,
        "wells": well_payloads,
    }


# ---------------------------------------------------------------- 批次

def find_batch_by_key(conn: sqlite3.Connection, request_key: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM batches WHERE request_key = ?", (request_key,)).fetchone()


def insert_batch(conn: sqlite3.Connection, request_key: str, status: str, error: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO batches (request_key, status, error, created_at) VALUES (?, ?, ?, ?)",
        (request_key, status, error, _now()),
    )
    return int(cur.lastrowid)


def _save_pre_batch_snapshot(conn: sqlite3.Connection, batch_id: int, wells: list[str]) -> None:
    """保存该批次首次触及各孔在任何移液发生前的完整快照（体积 + 各样本量）。"""
    for well in wells:
        row = conn.execute("SELECT volume FROM wells WHERE well = ?", (well,)).fetchone()
        volume = None if row is None else row["volume"]
        conn.execute(
            "INSERT INTO batch_well_snapshots (batch_id, well, volume) VALUES (?, ?, ?)",
            (batch_id, well, volume),
        )
        comp_rows = conn.execute(
            "SELECT sample_id, amount FROM well_composition WHERE well = ?",
            (well,),
        ).fetchall()
        conn.executemany(
            "INSERT INTO batch_snapshot_composition (batch_id, well, sample_id, amount) "
            "VALUES (?, ?, ?, ?)",
            [(batch_id, well, r["sample_id"], r["amount"]) for r in comp_rows],
        )


def execute_batch(conn: sqlite3.Connection, request_key: str, operations: list) -> int:
    """在调用方的事务内按提交顺序执行；任一步抛 InsufficientVolume 由调用方回滚。"""
    touched_wells = list(dict.fromkeys(
        well
        for op in operations
        for well in (op.source, op.destination)
    ))
    # 隔离孔不得作为批次来源或目标：任何移液发生之前整体拒绝，批次行也不落库。
    blocked = active_quarantine_briefs(conn, touched_wells)
    if blocked:
        raise WellQuarantined(blocked)
    batch_id = insert_batch(conn, request_key, "committed")
    # 先于任何移液保存批前快照，保证与最终提交同事务、可整体回滚。
    _save_pre_batch_snapshot(conn, batch_id, touched_wells)
    for step, op in enumerate(operations, start=1):
        volume = q(op.volume)
        apply_transfer(conn, op.source, op.destination, volume)
        conn.execute(
            "INSERT INTO operations (batch_id, step, source, destination, volume) "
            "VALUES (?, ?, ?, ?, ?)",
            (batch_id, step, op.source, op.destination, dec_str(volume)),
        )
    for well in touched_wells:
        _record_event(conn, well, "batch", batch_id=batch_id)
    return batch_id


def batch_payload(conn: sqlite3.Connection, batch_id: int, replayed: bool = False) -> dict:
    row = conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
    ops = conn.execute(
        "SELECT step, source, destination, volume FROM operations "
        "WHERE batch_id = ? ORDER BY step",
        (batch_id,),
    ).fetchall()
    reversal = conn.execute(
        "SELECT reversed_at FROM reversals WHERE batch_id = ?",
        (batch_id,),
    ).fetchone()
    well_rows = conn.execute(
        "SELECT DISTINCT well FROM ("
        "  SELECT source AS well FROM operations WHERE batch_id = ?"
        "  UNION"
        "  SELECT destination AS well FROM operations WHERE batch_id = ?"
        ")",
        (batch_id, batch_id),
    ).fetchall()
    quarantined_wells = {
        well: payload
        for well in (r["well"] for r in well_rows)
        if (payload := quarantine_payload(conn, well)) is not None
    }
    return {
        "batch_id": row["id"],
        "request_key": row["request_key"],
        "status": row["status"],
        "error": row["error"],
        "created_at": row["created_at"],
        "replayed": replayed,
        "reversed": reversal is not None,
        "reversed_at": reversal["reversed_at"] if reversal else None,
        "operations": [dict(op) for op in ops],
        "quarantined_wells": quarantined_wells,
    }


# ---------------------------------------------------------------- 批次撤销

class ReversalConflict(Exception):
    """批次当前不可撤销（已撤销/无快照/存在后续阻塞操作/快照恢复后超容量上限）。"""

    def __init__(
        self,
        reason: str,
        message: str,
        blockers: list[dict] | None = None,
        exceeding: list[dict] | None = None,
        quarantined: list[dict] | None = None,
    ) -> None:
        self.reason = reason
        self.message = message
        self.blockers = blockers or []
        self.exceeding = exceeding or []
        self.quarantined = quarantined or []
        super().__init__(message)


def find_reversal_by_key(conn: sqlite3.Connection, request_key: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM reversals WHERE request_key = ?", (request_key,)
    ).fetchone()


def get_reversal(conn: sqlite3.Connection, reversal_id: int, replayed: bool = False) -> dict | None:
    row = conn.execute(
        "SELECT * FROM reversals WHERE id = ?", (reversal_id,)
    ).fetchone()
    if row is None:
        return None
    wells = [
        r["well"]
        for r in conn.execute(
            "SELECT well FROM batch_well_snapshots WHERE batch_id = ? ORDER BY well",
            (row["batch_id"],),
        ).fetchall()
    ]
    return {
        "reversal_id": row["id"],
        "batch_id": row["batch_id"],
        "request_key": row["request_key"],
        "reversed_at": row["reversed_at"],
        "replayed": replayed,
        "restored_wells": wells,
        "batch": batch_payload(conn, row["batch_id"]),
    }


def _batch_blockers(conn: sqlite3.Connection, batch_id: int, wells: list[str]) -> list[dict]:
    """批次之后触及这些孔的补液（top_up）与已提交批次（batch）事件。

    阻塞基准是「该批次在这个孔上的事件 id」——批次自身在其他孔的事件
    id 可能更大，但不算后续操作。
    """
    blockers: list[dict] = []
    for well in wells:
        marker = conn.execute(
            "SELECT id FROM well_events WHERE type = 'batch' AND batch_id = ? AND well = ?",
            (batch_id, well),
        ).fetchone()
        if marker is None:
            continue
        rows = conn.execute(
            "SELECT e.type AS type, e.batch_id AS blocker_batch_id, b.request_key AS request_key, "
            "       e.sample AS sample, e.volume AS volume "
            "FROM well_events e LEFT JOIN batches b ON b.id = e.batch_id "
            "WHERE e.well = ? AND e.id > ? ORDER BY e.id",
            (well, marker["id"]),
        ).fetchall()
        if not rows:
            continue
        events = []
        for r in rows:
            event: dict = {"type": r["type"]}
            if r["type"] == "top_up":
                event["sample"] = r["sample"]
                event["volume"] = r["volume"]
            else:
                event["batch_id"] = r["blocker_batch_id"]
                event["request_key"] = r["request_key"]
            events.append(event)
        blockers.append({"well": well, "events": events})
    return blockers


def _restore_snapshot(conn: sqlite3.Connection, batch_id: int) -> None:
    """把该批次快照中的每个孔原子恢复到批前状态（含删除批次中新出现的孔）。"""
    snaps = conn.execute(
        "SELECT well, volume FROM batch_well_snapshots WHERE batch_id = ? ORDER BY well",
        (batch_id,),
    ).fetchall()
    for snap in snaps:
        well = snap["well"]
        # 先清组成再动孔，遵守外键约束。
        conn.execute("DELETE FROM well_composition WHERE well = ?", (well,))
        if snap["volume"] is None:
            conn.execute("DELETE FROM wells WHERE well = ?", (well,))
            continue
        conn.execute("UPDATE wells SET volume = ? WHERE well = ?", (snap["volume"], well))
        comp_rows = conn.execute(
            "SELECT sample_id, amount FROM batch_snapshot_composition "
            "WHERE batch_id = ? AND well = ? ORDER BY sample_id",
            (batch_id, well),
        ).fetchall()
        conn.executemany(
            "INSERT INTO well_composition (well, sample_id, amount) VALUES (?, ?, ?)",
            [(well, r["sample_id"], r["amount"]) for r in comp_rows],
        )


def apply_reversal(conn: sqlite3.Connection, batch_id: int, request_key: str) -> int:
    """在调用方事务内完成可撤销性校验、快照恢复与审计记录写入。

    校验不通过抛 ReversalConflict（由调用方随事务回滚，液体状态不变）。
    """
    row = conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
    if row is None:
        raise ReversalConflict("not_found", f"批次 {batch_id} 不存在")
    if row["status"] != "committed":
        raise ReversalConflict(
            "not_committed",
            f"批次 {batch_id} 状态为 {row['status']}，仅已提交批次可撤销",
        )
    snaps = conn.execute(
        "SELECT well, volume FROM batch_well_snapshots WHERE batch_id = ? ORDER BY well",
        (batch_id,),
    ).fetchall()
    if not snaps:
        raise ReversalConflict(
            "no_snapshot",
            f"批次 {batch_id} 是上线前提交的批次，没有批前快照，不可撤销",
        )
    if conn.execute(
        "SELECT 1 FROM reversals WHERE batch_id = ?", (batch_id,)
    ).fetchone():
        raise ReversalConflict("already_reversed", f"批次 {batch_id} 已经撤销，不能重复撤销")
    wells = [r["well"] for r in snaps]
    blockers = _batch_blockers(conn, batch_id, wells)
    if blockers:
        raise ReversalConflict(
            "blocked",
            f"批次 {batch_id} 之后有补液或后续批次触及相关孔位，不可撤销",
            blockers,
        )

    # 隔离孔不得被已提交批次的撤销触及：任一快照孔当前处于隔离中即整体
    # 拒绝，快照不恢复、审计不追加，液体/批次/隔离状态全部不变。
    blocked = active_quarantine_briefs(conn, wells)
    if blocked:
        raise ReversalConflict(
            "well_quarantined",
            f"批次 {batch_id} 触及的孔位处于异常隔离中，不可撤销",
            quarantined=blocked,
        )

    # 恢复快照前核对：任一快照孔恢复后的体积超过其当前容量上限，即整次
    # 拒绝撤销（液体与审计记录均不变）。快照为 NULL 的孔恢复后即删除，无需核对。
    exceeding = []
    for snap in snaps:
        if snap["volume"] is None:
            continue
        capacity = get_well_capacity(conn, snap["well"])
        if capacity is not None and Decimal(snap["volume"]) > capacity:
            exceeding.append(
                {
                    "well": snap["well"],
                    "volume": dec_str(Decimal(snap["volume"])),
                    "max_capacity": dec_str(capacity),
                }
            )
    if exceeding:
        raise ReversalConflict(
            "capacity_exceeded",
            f"批次 {batch_id} 撤销后部分孔位将超过当前容量上限，不可撤销",
            exceeding=exceeding,
        )

    _restore_snapshot(conn, batch_id)
    cur = conn.execute(
        "INSERT INTO reversals (batch_id, request_key, reversed_at) VALUES (?, ?, ?)",
        (batch_id, request_key, _now()),
    )
    return int(cur.lastrowid)
