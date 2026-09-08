"""洪峰事件状态机（纯函数，不涉及数据库）。

规则（同一站点按观测时间升序处理）：
- 水位首次 >= flood_level：开启一个事件；
- 事件期间持续计数、记录峰值（峰值相同取最早观测时间）；
  水位回落到 flood_level 以下但仍高于 recovery_level 时事件继续，不拆分；
- 水位首次 <= recovery_level：事件结束，该条结束观测计入事件；
- 结束后再次 >= flood_level 才开启新事件；阈值之间的普通观测不构成事件；
- 全部观测处理完仍未回落则为 open 事件，end_at 为 null。
"""

from __future__ import annotations

from typing import Iterable, Sequence


def compute_events(
    readings: Sequence[tuple[str, float]],
    flood_level: float,
    recovery_level: float,
) -> list[dict]:
    """根据按时间升序排列的 (observed_at_utc, level) 序列计算事件列表。

    返回的事件字典字段：start_at / end_at / peak_at / peak_level /
    sample_count / status，时间均为 UTC 规范字符串。
    """
    events: list[dict] = []
    current: dict | None = None

    for observed_at, level in readings:
        if current is None:
            if level >= flood_level:
                current = {
                    "start_at": observed_at,
                    "end_at": None,
                    "peak_at": observed_at,
                    "peak_level": level,
                    "sample_count": 1,
                    "status": "open",
                }
            # 低于洪水阈值的普通观测：不开启事件
            continue

        # 事件进行中：每条观测都计入
        current["sample_count"] += 1
        # 严格大于才更新峰值 → 峰值相同时保留最早观测时间
        if level > current["peak_level"]:
            current["peak_level"] = level
            current["peak_at"] = observed_at
        if level <= recovery_level:
            current["end_at"] = observed_at
            current["status"] = "closed"
            events.append(current)
            current = None

    if current is not None:
        events.append(current)
    return events


def affected_station_ids(readings: Iterable[dict]) -> list[str]:
    """提取批次涉及的站点 id（保持出现顺序、去重）。"""
    seen: set[str] = set()
    ids: list[str] = []
    for r in readings:
        sid = r["station_id"]
        if sid not in seen:
            seen.add(sid)
            ids.append(sid)
    return ids
