"""Bounded native implementation of Dice COC dice-pool rolls."""

import re
from dataclasses import dataclass
from secrets import SystemRandom
from typing import Callable

MAX_POOL_EXPRESSION_LENGTH = 160
MAX_POOL_TERMS = 32
MAX_INITIAL_POOL_DICE = 100
MAX_TOTAL_POOL_ROLLS = 10_000
MAX_POOL_ABS_EXTRA = 2_147_483_647


class DicePoolError(ValueError):
    """Invalid or unsafe COC dice-pool expression."""


@dataclass(frozen=True)
class DicePoolRound:
    sign: int
    rolls: tuple[int, ...]
    successes: int
    exploding: int


@dataclass(frozen=True)
class DicePoolResult:
    expression: str
    target: int
    threshold: int
    total: int
    extra: int
    rounds: tuple[DicePoolRound, ...]

    def format(self, *, detailed: bool = False) -> str:
        summary = f"{self.expression}（目标 {self.target}，爆骰阈值 {self.threshold}）= {self.total} 个成功"
        if not detailed:
            return summary
        lines = [summary]
        if self.extra:
            lines.append(f"额外成功值：{self.extra:+d}")
        for index, generation in enumerate(self.rounds, 1):
            sign_label = "正向池" if generation.sign > 0 else "反向池"
            if len(generation.rolls) > 100:
                counts = {face: generation.rolls.count(face) for face in range(1, 11)}
                rolls = "、".join(f"{face}:{count}" for face, count in counts.items() if count)
                rendered = f"骰面频数 {rolls}"
            else:
                rendered = " ".join(
                    f"{face}{'✓' if face >= self.target else ''}{'→加骰' if face >= self.threshold else ''}"
                    for face in generation.rolls
                )
            sign = "+" if generation.sign > 0 else "−"
            lines.append(f"第 {index} 轮（{sign_label} {sign}{len(generation.rolls)} 骰）：{rendered}；成功 {generation.successes}，追加 {generation.exploding} 骰")
        return "\n".join(lines)


def _normalize_expression(expression: str) -> str:
    if not isinstance(expression, str):
        raise DicePoolError("池骰表达式必须是文本")
    raw = re.sub(r"\s+", "", expression).upper().replace("×", "*")
    if not raw:
        raise DicePoolError("池骰表达式不能为空")
    if len(raw) > MAX_POOL_EXPRESSION_LENGTH:
        raise DicePoolError(f"池骰表达式过长（最多 {MAX_POOL_EXPRESSION_LENGTH} 个字符）")
    # Upstream treats a leading bare number as a count of D10 pool dice.
    match = re.match(r"^(\d+)(?=$|[+-])", raw)
    if match:
        raw = raw[:match.end()] + "D" + raw[match.end():]
    return raw


def roll_dice_pool(expression: str, *, target: int = 8, randint: Callable[[int, int], int] | None = None) -> DicePoolResult:
    """Roll an upstream-compatible D10 success pool with bounded explosions.

    D-side values 5..11 set the explosion threshold; pool successes use the
    configured target. A leading bare number means that many D10 dice.
    """
    if type(target) is not int or not 1 <= target <= 10:
        raise DicePoolError("成功目标必须在 1 到 10 之间")
    raw = _normalize_expression(expression)
    roll = randint or SystemRandom().randint
    position = 0
    signed_dice = 0
    extra = 0
    threshold = 10
    terms = 0
    while position < len(raw):
        sign = 1
        if raw[position] in "+-":
            sign = -1 if raw[position] == "-" else 1
            position += 1
            if position >= len(raw):
                raise DicePoolError("池骰表达式不能以运算符结尾")
        match = re.match(r"(\d+)(?:D(\d*))?", raw[position:])
        if match is None:
            raise DicePoolError("池骰只支持 D10 池、D5–D11 爆骰阈值及加减项")
        count = int(match.group(1))
        die_side = match.group(2)
        has_die = "D" in match.group(0)
        if has_die:
            if count == 0:
                raise DicePoolError("骰池数量不能为 0")
            if count > MAX_INITIAL_POOL_DICE:
                raise DicePoolError(f"初始骰池最多 {MAX_INITIAL_POOL_DICE} 骰")
            signed_dice += sign * count
            if die_side:
                threshold = int(die_side)
                if not 5 <= threshold <= 11:
                    raise DicePoolError("爆骰阈值必须在 D5 到 D11 之间")
            else:
                threshold = 10
        else:
            extra += sign * count
            if abs(extra) > MAX_POOL_ABS_EXTRA:
                raise DicePoolError("额外成功值超出整数范围")
        terms += 1
        if terms > MAX_POOL_TERMS:
            raise DicePoolError(f"池骰项数超限（最多 {MAX_POOL_TERMS} 项）")
        position += match.end()
        if position < len(raw) and raw[position] not in "+-":
            raise DicePoolError("池骰表达式格式错误")
    if signed_dice == 0:
        raise DicePoolError("骰池净骰数不能为 0")
    if abs(signed_dice) > MAX_INITIAL_POOL_DICE:
        raise DicePoolError(f"净骰池最多 {MAX_INITIAL_POOL_DICE} 骰")
    if not callable(roll):
        raise DicePoolError("随机源必须提供 randint(low, high)")
    if abs(extra) + abs(signed_dice) * 10 > MAX_POOL_ABS_EXTRA:
        raise DicePoolError("池骰结果可能超出整数范围")
    sign = 1 if signed_dice > 0 else -1
    pending = abs(signed_dice)
    total = extra
    rolled_count = 0
    generations: list[DicePoolRound] = []
    while pending:
        if rolled_count + pending > MAX_TOTAL_POOL_ROLLS:
            raise DicePoolError(f"爆骰累计超过安全上限 {MAX_TOTAL_POOL_ROLLS}")
        faces = []
        successes = 0
        exploding = 0
        for _ in range(pending):
            face = roll(1, 10)
            if type(face) is not int or not 1 <= face <= 10:
                raise DicePoolError("随机源返回了无效 D10 点数")
            faces.append(face)
            successes += face >= target
            exploding += face >= threshold
        generations.append(DicePoolRound(sign, tuple(faces), successes, exploding))
        total += sign * successes
        if abs(total) > MAX_POOL_ABS_EXTRA:
            raise DicePoolError("池骰结果超出安全整数范围")
        rolled_count += pending
        pending = exploding
    return DicePoolResult(raw, target, threshold, total, extra, tuple(generations))
