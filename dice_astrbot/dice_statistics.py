from __future__ import annotations

import copy
import math

MAX_STAT_COUNT = 2_147_483_647
MAX_STAT_TOTAL = 9_007_199_254_740_991
ROLL_FACE_LIMIT = 100

ROLL_ENTRY_KEYS = {"count", "sum", "sum_squares"}
CHECK_KEYS = {"count", "successes", "rate_sum", "ones", "low_1_5", "high_96_100", "hundreds"}


class CardStatisticsError(ValueError):
    """Invalid or exhausted per-card statistics; never overwrite malformed data."""


def empty_card_statistics() -> dict:
    return {
        "rolls": {},
        "checks": {
            "count": 0,
            "successes": 0,
            "rate_sum": 0,
            "ones": 0,
            "low_1_5": 0,
            "high_96_100": 0,
            "hundreds": 0,
        },
    }


def _bounded_int(value: object, maximum: int = MAX_STAT_TOTAL) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= maximum


def validate_card_statistics(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {"rolls", "checks"}:
        return False
    rolls = value.get("rolls")
    checks = value.get("checks")
    if not isinstance(rolls, dict) or len(rolls) > ROLL_FACE_LIMIT:
        return False
    for raw_face, entry in rolls.items():
        if not isinstance(raw_face, str) or not raw_face.isascii() or not raw_face.isdecimal():
            return False
        face = int(raw_face)
        if not 1 <= face <= ROLL_FACE_LIMIT or str(face) != raw_face:
            return False
        if not isinstance(entry, dict) or set(entry) != ROLL_ENTRY_KEYS:
            return False
        count = entry.get("count")
        total = entry.get("sum")
        squares = entry.get("sum_squares")
        if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= MAX_STAT_COUNT:
            return False
        if not _bounded_int(total) or not _bounded_int(squares):
            return False
        if not count <= total <= count * face or not count <= squares <= count * face * face:
            return False
    if not isinstance(checks, dict) or set(checks) != CHECK_KEYS:
        return False
    if any(not _bounded_int(checks.get(key), MAX_STAT_COUNT) for key in CHECK_KEYS):
        return False
    count = checks["count"]
    if checks["successes"] > count or checks["ones"] > checks["low_1_5"] or checks["low_1_5"] > count:
        return False
    if checks["hundreds"] > checks["high_96_100"] or checks["high_96_100"] > count:
        return False
    if checks["rate_sum"] > 99 * count:
        return False
    return True


def updated_card_statistics(
    current: object,
    rolls: tuple[tuple[int, int], ...] | list[tuple[int, int]] = (),
    checks: tuple[tuple[int, int], ...] | list[tuple[int, int]] = (),
) -> dict:
    if current is None:
        result = empty_card_statistics()
    elif not validate_card_statistics(current):
        raise CardStatisticsError("角色卡统计数据格式无效，未覆盖原记录")
    else:
        result = copy.deepcopy(current)
    for die, face in rolls:
        if not (isinstance(die, int) and not isinstance(die, bool) and isinstance(face, int) and not isinstance(face, bool)):
            raise CardStatisticsError("角色卡统计输入无效")
        if not 1 <= face <= ROLL_FACE_LIMIT or not 1 <= die <= face:
            continue
        entry = result["rolls"].get(str(face))
        if entry is None:
            entry = {"count": 0, "sum": 0, "sum_squares": 0}
            result["rolls"][str(face)] = entry
        if entry["count"] >= MAX_STAT_COUNT or entry["sum"] + die > MAX_STAT_TOTAL or entry["sum_squares"] + die * die > MAX_STAT_TOTAL:
            raise CardStatisticsError("角色卡统计计数已达安全上限，未更新")
        entry["count"] += 1
        entry["sum"] += die
        entry["sum_squares"] += die * die
    for die, rate in checks:
        if not (isinstance(die, int) and not isinstance(die, bool) and isinstance(rate, int) and not isinstance(rate, bool)):
            raise CardStatisticsError("角色卡检定统计输入无效")
        if not 1 <= die <= 100 or not 1 <= rate <= 99:
            continue
        row = result["checks"]
        if row["count"] >= MAX_STAT_COUNT or row["rate_sum"] + rate > MAX_STAT_TOTAL:
            raise CardStatisticsError("角色卡检定统计计数已达安全上限，未更新")
        row["count"] += 1
        row["successes"] += int(die <= rate)
        row["ones"] += int(die == 1)
        row["low_1_5"] += int(die <= 5)
        row["high_96_100"] += int(die >= 96)
        row["hundreds"] += int(die == 100)
        row["rate_sum"] += rate
    if not validate_card_statistics(result):
        raise CardStatisticsError("更新后的角色卡统计无效，未保存")
    return result


def _percent(value: float) -> str:
    return f"{value:.2f}%"


def format_card_statistics(value: object, die_face: int) -> str | None:
    if value is None:
        value = empty_card_statistics()
    if not validate_card_statistics(value):
        raise CardStatisticsError("角色卡统计数据格式无效，拒绝显示")
    lines: list[str] = []
    if 2 <= die_face <= ROLL_FACE_LIMIT:
        row = value["rolls"].get(str(die_face))
        if isinstance(row, dict) and row["count"] > 0:
            count = row["count"]
            mean = row["sum"] / count
            expected_mean = (die_face + 1) / 2
            lines.append(f"D{die_face}统计次数: {count}")
            lines.append(f"均值: {mean:.2f} [期望 {expected_mean:.2f}]")
            if count > 1:
                expected_std = math.sqrt((die_face * die_face - 1) / 12)
                variance = max(0.0, row["sum_squares"] / count - mean * mean)
                observed_std = math.sqrt(variance)
                z_score = (mean - expected_mean) * math.sqrt(count) / expected_std
                percentile = 0.5 + 0.5 * math.erf(z_score / math.sqrt(2))
                if percentile < 0.5:
                    lines.append(f"低于{(1 - percentile) * 100:.2f}%的用户")
                else:
                    lines.append(f"高于{percentile * 100:.2f}%的用户")
                if observed_std > 0:
                    lines.append(f"标准差: {observed_std:.2f} [期望 {expected_std:.2f}]")
    checks = value["checks"]
    count = checks["count"]
    if count:
        actual_rate = checks["successes"] / count * 100
        expected_rate = checks["rate_sum"] / count
        low_rate = _percent(checks["low_1_5"] / count * 100) + f"({checks['low_1_5']})" if checks["low_1_5"] else "0%"
        high_rate = _percent(checks["high_96_100"] / count * 100) + f"({checks['high_96_100']})" if checks["high_96_100"] else "0%"
        lines.append(f"检定成功率: {checks['successes']}/{count}={actual_rate:.2f}% [期望{expected_rate:.2f}%]")
        lines.append(f"5- | 96+ 出现率: {low_rate} | {high_rate}")
        if checks["ones"] or checks["hundreds"]:
            lines.append(f"1 | 100 出现数: {checks['ones']} | {checks['hundreds']}")
    return "\n".join(lines) if lines else None
