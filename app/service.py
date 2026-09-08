"""业务编排层：建站、批次原子入库、事件重算与查询。

所有写操作都在一个 ``BEGIN IMMEDIATE`` 事务 + 进程内写锁中完成，
保证"整批校验 → 去重/冲突判定 → 写入 → 事件重算"原子可见：
任一步失败即回滚，不留下任何部分数据。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from .database import Database
from .errors import ConflictError, NotFoundError, ValidationError
from .events import affected_station_ids, compute_events
from .validation import (
    canonical_utc,
    finite_number,
    format_level,
    parse_timestamp,
    require_object,
    require_str,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _event_out(row: sqlite3.Row | dict) -> dict:
    """把事件记录转为对外 JSON 结构。"""
    return {
        "start_at": row["start_at"],
        "end_at": row["end_at"],
        "peak_at": row["peak_at"],
        "peak_level": format_level(float(row["peak_level"])),
        "sample_count": int(row["sample_count"]),
        "status": row["status"],
    }


class Service:
    def __init__(self, db: Database):
        self.db = db

    # ---------- 健康检查 ----------

    def health(self) -> dict:
        with self.db.lock:
            self.db.connect_read().execute("SELECT 1").fetchone()
        return {"status": "ok"}

    # ---------- 监测站 ----------

    def create_station(self, payload: object) -> dict:
        data = require_object(payload, "请求体")
        station_id = require_str(data.get("id"), "id").strip()
        name = require_str(data.get("name"), "name").strip()
        flood = finite_number(data.get("flood_level"), "flood_level")
        recovery = finite_number(data.get("recovery_level"), "recovery_level")
        if flood <= recovery:
            raise ValidationError(
                f"flood_level（{flood}）必须严格大于 recovery_level（{recovery}）",
                code="invalid_thresholds",
            )

        try:
            with self.db.transaction() as conn:
                existing = conn.execute(
                    "SELECT 1 FROM stations WHERE id = ?", (station_id,)
                ).fetchone()
                if existing is not None:
                    raise ConflictError(
                        f"监测站 {station_id!r} 已存在", code="station_exists"
                    )
                conn.execute(
                    "INSERT INTO stations (id, name, flood_level, recovery_level, created_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (station_id, name, flood, recovery, _now()),
                )
        except sqlite3.IntegrityError as exc:  # 并发下的兜底
            raise ConflictError(f"监测站 {station_id!r} 已存在",
                                code="station_exists") from exc

        return {
            "id": station_id,
            "name": name,
            "flood_level": format_level(flood),
            "recovery_level": format_level(recovery),
        }

    def _get_station(self, conn: sqlite3.Connection, station_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT id, name, flood_level, recovery_level FROM stations WHERE id = ?",
            (station_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"监测站 {station_id!r} 不存在", code="unknown_station")
        return row

    # ---------- 观测批次 ----------

    def submit_batch(self, payload: object) -> dict:
        data = require_object(payload, "请求体")
        batch_id = require_str(data.get("batch_id"), "batch_id").strip()
        readings = data.get("readings")
        if not isinstance(readings, list) or not readings:
            raise ValidationError("字段 'readings' 必须是非空数组", code="invalid_field")

        # --- 纯函数校验 + 规范化（不需要数据库） ---
        normalized: list[dict] = []
        for index, raw in enumerate(readings):
            item = require_object(raw, f"readings[{index}]")
            station_id = require_str(item.get("station_id"),
                                    f"readings[{index}].station_id").strip()
            dt = parse_timestamp(item.get("observed_at"),
                                 f"readings[{index}].observed_at")
            level = finite_number(item.get("level"), f"readings[{index}].level")
            normalized.append({
                "station_id": station_id,
                "observed_at_utc": canonical_utc(dt),
                "level": level,
                "index": index,
            })

        # 同批内去重 / 冲突：(station, utc 时刻) 相同
        deduped: dict[tuple[str, str], dict] = {}
        intra_duplicates = 0
        for r in normalized:
            key = (r["station_id"], r["observed_at_utc"])
            prev = deduped.get(key)
            if prev is None:
                deduped[key] = r
            elif prev["level"] != r["level"]:
                raise ValidationError(
                    f"同批次内站点 {r['station_id']!r} 在 {r['observed_at_utc']} "
                    f"出现不同水位（{prev['level']} 与 {r['level']}）",
                    code="intra_batch_conflict",
                )
            else:
                intra_duplicates += 1

        unique_readings = list(deduped.values())
        canonical = json.dumps(
            {
                "batch_id": batch_id,
                "readings": sorted(
                    (r["station_id"], r["observed_at_utc"], r["level"])
                    for r in unique_readings
                ),
            },
            sort_keys=True,
            separators=(",", ":"),
        )

        # --- 数据库事务：批次去重 → 站点/冲突检查 → 写入 → 重算事件 ---
        try:
            with self.db.transaction() as conn:
                existing_batch = conn.execute(
                    "SELECT payload_json, result_json FROM batches WHERE batch_id = ?",
                    (batch_id,),
                ).fetchone()
                if existing_batch is not None:
                    if existing_batch["payload_json"] == canonical:
                        # 幂等重试：原样返回首次成功结果
                        return json.loads(existing_batch["result_json"])
                    raise ConflictError(
                        f"批次 {batch_id!r} 已存在但内容不同，拒绝覆盖",
                        code="batch_conflict",
                    )

                # 站点必须存在
                station_ids = affected_station_ids(unique_readings)
                for sid in station_ids:
                    self._get_station(conn, sid)

                # 与已入库观测比对：同水位 = 重复（跳过），不同水位 = 冲突（整批失败）
                duplicates = 0
                to_insert: list[dict] = []
                for sid in station_ids:
                    times = [r["observed_at_utc"] for r in unique_readings
                             if r["station_id"] == sid]
                    stored = {
                        row["observed_at_utc"]: row["level"]
                        for row in conn.execute(
                            "SELECT observed_at_utc, level FROM observations"
                            " WHERE station_id = ? AND observed_at_utc IN ("
                            + ",".join("?" * len(times)) + ")",
                            (sid, *times),
                        )
                    }
                    for r in unique_readings:
                        if r["station_id"] != sid:
                            continue
                        old = stored.get(r["observed_at_utc"])
                        if old is None:
                            to_insert.append(r)
                        elif old != r["level"]:
                            raise ConflictError(
                                f"站点 {sid!r} 在 {r['observed_at_utc']} 已有水位 "
                                f"{old}，与本批次水位 {r['level']} 冲突",
                                code="observation_conflict",
                            )
                        else:
                            duplicates += 1

                conn.executemany(
                    "INSERT INTO observations"
                    " (station_id, observed_at_utc, level, batch_id, created_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    [
                        (r["station_id"], r["observed_at_utc"], r["level"],
                         batch_id, _now())
                        for r in to_insert
                    ],
                )

                # 重算受影响站点的完整事件（结果与一次性顺序导入一致）
                events_by_station: dict[str, list[dict]] = {}
                for sid in station_ids:
                    events_by_station[sid] = self._recompute(conn, sid)

                result = {
                    "batch_id": batch_id,
                    "received": len(readings),
                    "inserted": len(to_insert),
                    "duplicates": duplicates + intra_duplicates,
                    "stations": station_ids,
                    "events": events_by_station,
                }
                conn.execute(
                    "INSERT INTO batches (batch_id, payload_json, result_json, created_at)"
                    " VALUES (?, ?, ?, ?)",
                    (batch_id, canonical,
                     json.dumps(result, sort_keys=True), _now()),
                )
                return result
        except sqlite3.IntegrityError as exc:  # 并发下批次主键竞争的兜底
            raise ConflictError(f"批次 {batch_id!r} 提交冲突，请重试",
                                code="batch_conflict") from exc

    def _recompute(self, conn: sqlite3.Connection, station_id: str) -> list[dict]:
        station = self._get_station(conn, station_id)
        rows = conn.execute(
            "SELECT observed_at_utc, level FROM observations"
            " WHERE station_id = ? ORDER BY observed_at_utc, id",
            (station_id,),
        ).fetchall()
        events = compute_events(
            [(r["observed_at_utc"], r["level"]) for r in rows],
            float(station["flood_level"]),
            float(station["recovery_level"]),
        )
        conn.execute("DELETE FROM events WHERE station_id = ?", (station_id,))
        out: list[dict] = []
        for e in events:
            cur = conn.execute(
                "INSERT INTO events"
                " (station_id, start_at, end_at, peak_at, peak_level,"
                "  sample_count, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (station_id, e["start_at"], e["end_at"], e["peak_at"],
                 e["peak_level"], e["sample_count"], e["status"]),
            )
            row = conn.execute(
                "SELECT * FROM events WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
            out.append(_event_out(row))
        return out

    # ---------- 事件查询 ----------

    def list_events(self, station_id: str) -> list[dict]:
        with self.db.lock:
            conn = self.db.connect_read()
            self._get_station(conn, station_id)  # 未知站点 → 404
            rows = conn.execute(
                "SELECT * FROM events WHERE station_id = ? ORDER BY start_at",
                (station_id,),
            ).fetchall()
        return [_event_out(r) for r in rows]
