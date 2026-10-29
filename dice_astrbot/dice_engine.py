from __future__ import annotations

import re
import secrets
import threading
from dataclasses import dataclass
from typing import Protocol


MAX_EXPRESSION_LENGTH = 160
MAX_TERMS = 32
MAX_RESULT_DETAIL_LENGTH = 256
MAX_DICE_PER_TERM = 999
MAX_SIDES = 100_000
MAX_ABS_TOTAL = 2_147_483_647


class DiceError(ValueError):
    """Invalid or unsafe dice expression."""


class RandomSource(Protocol):
    def randint(self, a: int, b: int) -> int: ...


@dataclass(frozen=True)
class RollTerm:
    sign: int
    expression: str
    detail: str
    subtotal: int


@dataclass(frozen=True)
class RollResult:
    expression: str
    total: int
    terms: tuple[RollTerm, ...]

    def format(self) -> str:
        rendered = []
        combined = []
        for index, term in enumerate(self.terms):
            prefix = "-" if term.sign < 0 else ("+" if index else "")
            rendered.append(f"{prefix}{term.detail}")
            combined.append(f"{prefix}{term.subtotal}")
        if sum(map(len, rendered)) > MAX_RESULT_DETAIL_LENGTH:
            combined_text = "".join(combined)
            compact = self.expression
            if compact != combined_text:
                compact += f"={combined_text}"
            if combined_text != str(self.total):
                compact += f"={self.total}"
            return compact
        return f"{self.expression} ({' '.join(rendered)}) = {self.total}"


@dataclass(frozen=True)
class CheckResult:
    roll: int
    skill: int
    outcome: str
    threshold: int
    rule: int
    roll_detail: str | None = None

    @property
    def success(self) -> bool:
        return self.outcome in {"critical", "extreme", "hard", "special", "success"}


class DiceEngine:
    """Bounded Dice-compatible roller with injectable randomness for tests."""

    _ATOM_DICE = re.compile(r"^([0-9]*)D([0-9]+)(?:K([0-9]+))?$")
    _ATOM_FUDGE = re.compile(r"^(?:([0-9]*)D)?F$")
    _ATOM_PERCENT = re.compile(r"^([PB])([0-9]*)$")
    _ATOM_DICE_DEFAULT = re.compile(r"^([0-9]*)D$")

    def __init__(self, rng: RandomSource | None = None) -> None:
        self._unscoped_rng = secrets.SystemRandom()
        self._rng = rng or self._unscoped_rng
        self._draw_state = threading.local()

    def _randint(self, low: int, high: int, *, bypass_session_roulette: bool = False) -> int:
        draw_count = getattr(self._draw_state, "draw_count", 0) + 1
        self._draw_state.draw_count = draw_count
        if draw_count > MAX_DICE_PER_TERM * MAX_TERMS * 3:
            raise DiceError("单次表达式的随机投掷次数超限")
        rng = getattr(self._draw_state, "rng", self._rng)
        if bypass_session_roulette and getattr(rng, "_dice_session_roulette", False):
            rng = self._rng
            if getattr(rng, "_dice_session_roulette", False):
                rng = self._unscoped_rng
        return rng.randint(low, high)

    def roll(self, expression: str = "", default_die: int = 100, *, rng: RandomSource | None = None) -> RollResult:
        previous_rng = getattr(self._draw_state, "rng", None)
        previous_depth = getattr(self._draw_state, "roll_depth", 0)
        if previous_depth == 0:
            self._draw_state.draw_count = 0
        self._draw_state.roll_depth = previous_depth + 1
        if rng is not None:
            self._draw_state.rng = rng
        try:
            return self._roll(expression, default_die)
        finally:
            self._draw_state.roll_depth = previous_depth
            if rng is not None:
                if previous_rng is None:
                    del self._draw_state.rng
                else:
                    self._draw_state.rng = previous_rng

    def _roll(self, expression: str, default_die: int) -> RollResult:
        if not isinstance(expression, str):
            raise DiceError("骰子表达式必须是文本")
        if not 1 <= int(default_die) <= MAX_SIDES:
            raise DiceError(f"默认骰面数必须在 1 到 {MAX_SIDES} 之间")
        raw = expression.strip()
        if len(raw) > MAX_EXPRESSION_LENGTH:
            raise DiceError(f"表达式过长（最多 {MAX_EXPRESSION_LENGTH} 个字符）")
        raw = re.sub(r"\s+", "", raw).replace("×", "*").replace("÷", "/")
        if not raw:
            raw = f"D{int(default_die)}"
        raw = self._expand_parentheses(raw, int(default_die)).upper()
        raw = self._normalize_upstream_operators(raw, int(default_die))
        raw = re.sub(r"([+-])A", r"\g<1>1", raw)
        if raw.startswith("A"):
            raw = "1" + raw[1:]
        raw = re.sub(r"(?<=[0-9])F", "DF", raw)
        raw = re.sub(r"D(?=K)", f"D{int(default_die)}", raw)
        raw = raw.replace("K+", "K1+").replace("K-", "K1-")
        if raw.endswith("K"):
            raw += "1"
        raw = re.sub(r"(?<![A-Z0-9])DF", "4DF", raw)
        raw = re.sub(r"(?<![A-Z0-9])F", "4DF", raw)
        raw = re.sub(r"(?<![A-Z0-9])D(?=([+\-]|$))", f"D{int(default_die)}", raw)
        # D as a bare atom and omitted die sides both use the configured default.
        raw = re.sub(r"(?<![A-Z0-9])D(?=([X*/]|$))", f"D{int(default_die)}", raw)
        terms = self._split_expression(raw)
        if len(terms) > MAX_TERMS:
            raise DiceError(f"表达式项数超限（最多 {MAX_TERMS} 项）")
        total = 0
        rendered: list[RollTerm] = []
        for sign, term in terms:
            subtotal, detail = self._roll_term(term, int(default_die), depth=0)
            total += sign * subtotal
            if abs(total) > MAX_ABS_TOTAL:
                raise DiceError("结果超出安全整数范围")
            rendered.append(RollTerm(sign, term, detail, subtotal))
        return RollResult(raw, total, tuple(rendered))

    def _expand_parentheses(self, expression: str, default_die: int) -> str:
        if "(" not in expression and ")" not in expression:
            return expression
        depth = 0
        pair_count = 0
        max_depth = 0
        for char in expression:
            if char == "(":
                depth += 1
                pair_count += 1
                max_depth = max(max_depth, depth)
            elif char == ")":
                depth -= 1
                if depth < 0:
                    raise DiceError("括号不匹配")
        if depth != 0:
            raise DiceError("括号不匹配")
        if max_depth > 8 or pair_count > 16:
            raise DiceError("括号嵌套或分组数量超限")

        raw = expression
        while ")" in raw:
            right = raw.find(")")
            left = raw.rfind("(", 0, right)
            if left < 0 or right == left + 1:
                raise DiceError("括号内表达式不能为空或不匹配")
            inner = raw[left + 1:right]
            value = self.roll(inner, default_die).total
            raw = raw[:left] + str(value) + raw[right + 1:]
            if len(raw) > MAX_EXPRESSION_LENGTH:
                raise DiceError(f"展开后的表达式过长（最多 {MAX_EXPRESSION_LENGTH} 个字符）")
        return raw

    @staticmethod
    def _normalize_upstream_operators(expression: str, default_die: int) -> str:
        operators = "+-X/"
        normalized: list[str] = []
        previous = "\0"
        for char in expression:
            if char in "*xX":
                if previous in operators:
                    normalized.pop()
                    break
                char = "X"
            elif char == "/":
                if previous in operators:
                    normalized.pop()
                    break
            elif char == "+":
                if previous in operators:
                    continue
            elif char == "-":
                if previous in operators:
                    if previous == "+":
                        normalized[-1] = "-"
                    elif previous == "-":
                        normalized[-1] = "+"
                    elif previous in "X/":
                        normalized.pop()
                        break
                    continue
            char = char.upper()
            normalized.append(char)
            previous = char

        result = "".join(normalized)
        if not result:
            return f"D{default_die}"
        return result[1:] if result.startswith("+") else result

    @staticmethod
    def _split_expression(expression: str) -> list[tuple[int, str]]:
        if not expression:
            raise DiceError("骰子表达式不能为空")
        terms: list[tuple[int, str]] = []
        sign = 1
        start = 0
        if expression[0] in "+-":
            sign = -1 if expression[0] == "-" else 1
            start = 1
        for index in range(start, len(expression)):
            char = expression[index]
            if char in "+-":
                term = expression[start:index]
                if not term:
                    raise DiceError("运算符之间缺少骰子项")
                terms.append((sign, term))
                sign = -1 if char == "-" else 1
                start = index + 1
        final = expression[start:]
        if not final:
            raise DiceError("表达式不能以运算符结尾")
        terms.append((sign, final))
        return terms

    def _roll_term(self, term: str, default_die: int, depth: int) -> tuple[int, str]:
        if depth > 4:
            raise DiceError("乘除嵌套层数超限")
        if not term:
            raise DiceError("缺少骰子项")
        parts = re.split(r"([X*/])", term)
        if len(parts) % 2 == 0:
            raise DiceError("乘除表达式格式错误")
        atoms = parts[::2]
        operators = parts[1::2]
        if not atoms[0]:
            raise DiceError("骰子项不能为空")
        if any(not atom for atom in atoms[1:]):
            raise DiceError("乘除符号后缺少数值")

        # Upstream parses each slash suffix as a complete expression first, then
        # applies the remaining multiplication chain to the numerator.
        groups: list[tuple[int, int]] = []
        group_start = 0
        for index, operator in enumerate(operators):
            if operator == "/":
                groups.append((group_start, index + 1))
                group_start = index + 1
        groups.append((group_start, len(atoms)))

        atom_details = [""] * len(atoms)
        atom_values = [0] * len(atoms)

        def is_special_atom(atom: str) -> bool:
            return (
                self._ATOM_FUDGE.fullmatch(atom) is not None
                or self._ATOM_PERCENT.fullmatch(atom) is not None
            )

        def evaluate_group(start: int, end: int) -> int:
            product = 1
            for atom_index in range(end - 1, start - 1, -1):
                factor, atom_details[atom_index] = self._roll_atom(atoms[atom_index], default_die)
                atom_values[atom_index] = factor
                product *= factor
                if abs(product) > MAX_ABS_TOTAL:
                    raise DiceError("结果超出安全整数范围")
            # Upstream's Fudge and bonus/penalty branches return before applying
            # their parsed multipliers, so preserve the base result for those atoms.
            return atom_values[start] if is_special_atom(atoms[start]) else product

        denominator = 1
        for start, end in reversed(groups[1:]):
            divisor = evaluate_group(start, end)
            if divisor == 0:
                raise DiceError("不能除以 0")
            denominator *= divisor
            if abs(denominator) > MAX_ABS_TOTAL:
                raise DiceError("结果超出安全整数范围")

        numerator_special = is_special_atom(atoms[groups[0][0]])
        numerator = evaluate_group(*groups[0])
        if numerator_special:
            value = numerator
        else:
            quotient = abs(numerator) // abs(denominator)
            value = -quotient if (numerator < 0) != (denominator < 0) else quotient
        detail = atom_details[0]
        for index, operator in enumerate(operators):
            detail += f"{operator}{atom_details[index + 1]}"
        return value, detail

    def _roll_atom(self, atom: str, default_die: int) -> tuple[int, str]:
        if not atom:
            raise DiceError("骰子项不能为空")
        if atom.isascii() and atom.isdecimal():
            if len(atom) > 5:
                raise DiceError("常数最多5位")
            value = int(atom)
            if value > MAX_ABS_TOTAL:
                raise DiceError("常数超出安全整数范围")
            return value, atom

        fudge_match = self._ATOM_FUDGE.fullmatch(atom)
        if fudge_match:
            raw_count = fudge_match.group(1)
            if raw_count and len(raw_count) > 2:
                raise DiceError("Fudge骰数量不能超过99")
            count = int(raw_count) if raw_count else 4
            self._validate_count(count)
            values = [self._randint(-1, 1, bypass_session_roulette=True) for _ in range(count)]
            return sum(values), f"{count}dF[{','.join(map(str, values))}]"

        percent_match = self._ATOM_PERCENT.fullmatch(atom)
        if percent_match:
            mode, raw_count = percent_match.groups()
            if len(atom) > 2:
                raise DiceError("奖励/惩罚骰数量最多1位")
            count = int(raw_count) if raw_count else 1
            if not 1 <= count <= 9:
                raise DiceError("奖励/惩罚骰数量必须在 1 到 9 之间")
            return self._roll_percentile(mode, count)

        dice_match = self._ATOM_DICE.fullmatch(atom)
        if dice_match:
            raw_count, raw_sides, raw_keep = dice_match.groups()
            if raw_count and len(raw_count) > 3:
                raise DiceError("骰子数量最多3位")
            if raw_keep is not None and len(raw_count) == 3 and raw_count != "100":
                raise DiceError("K取高模式数量最多100")
            if len(raw_sides) > 4:
                raise DiceError("骰面数最多4位")
            if raw_keep is not None and len(raw_keep) > 3:
                raise DiceError("保留骰数量最多3位")
            count = int(raw_count) if raw_count else 1
            sides = int(raw_sides)
            self._validate_count(count)
            if not 1 <= sides <= MAX_SIDES:
                raise DiceError(f"骰面数必须在 1 到 {MAX_SIDES} 之间")
            keep = int(raw_keep) if raw_keep is not None else None
            if keep is not None and not 1 <= keep <= count:
                raise DiceError("保留骰数量必须在 1 到投掷数量之间")
            bypass_roulette = raw_keep is not None or count >= 10
            values = [self._randint(1, sides, bypass_session_roulette=bypass_roulette) for _ in range(count)]
            if keep is not None:
                kept = sorted(values, reverse=True)[:keep]
                return sum(kept), f"{count}d{sides}k{keep}[{','.join(map(str, values))};保留{','.join(map(str, kept))}]"
            display_values = sorted(values) if count > 20 else values
            return sum(values), f"{count}d{sides}[{','.join(map(str, display_values))}]"

        default_match = self._ATOM_DICE_DEFAULT.fullmatch(atom)
        if default_match:
            if len(str(default_die)) > 4:
                raise DiceError("骰面数最多4位")
            raw_count = default_match.group(1)
            if raw_count and len(raw_count) > 3:
                raise DiceError("骰子数量最多3位")
            count = int(raw_count) if raw_count else 1
            self._validate_count(count)
            values = [self._randint(1, default_die, bypass_session_roulette=count >= 10) for _ in range(count)]
            display_values = sorted(values) if count > 20 else values
            return sum(values), f"{count}d{default_die}[{','.join(map(str, display_values))}]"

        raise DiceError(f"无法识别骰子表达式：{atom}")

    def _roll_percentile(self, mode: str, count: int) -> tuple[int, str]:
        if mode not in {"B", "P"}:
            raise DiceError("百分骰模式必须是 B 或 P")
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 9:
            raise DiceError("奖励/惩罚骰数量必须在 1 到 9 之间")

        base = self._randint(1, 100)
        units = base % 10
        candidates = [base // 10]
        # DiceSession roulette applies to the base percentile only; upstream RD draws extra tens directly.
        roulette_rng = getattr(self._draw_state, "rng", None)
        if roulette_rng is not None:
            del self._draw_state.rng
        try:
            for _ in range(count):
                drawn = self._randint(1, 10)
                candidates.append(drawn if units == 0 else drawn - 1)
        finally:
            if roulette_rng is not None:
                self._draw_state.rng = roulette_rng

        chosen_tens = min(candidates) if mode == "B" else max(candidates)
        result = 100 if chosen_tens == 10 and units == 0 else chosen_tens * 10 + units
        label = "奖励" if mode == "B" else "惩罚"
        detail = f"{label}骰({base}; 十位{','.join(map(str, candidates))}; 个位{units})={result}"
        return result, detail

    @staticmethod
    def _validate_count(count: int) -> None:
        if not 1 <= count <= MAX_DICE_PER_TERM:
            raise DiceError(f"骰子数量必须在 1 到 {MAX_DICE_PER_TERM} 之间")

    @staticmethod
    def _trunc_div(numerator: int, denominator: int) -> int:
        quotient = abs(numerator) // abs(denominator)
        return -quotient if (numerator < 0) != (denominator < 0) else quotient

    def check(self, skill: int, *, rule: int = 0, rng: RandomSource | None = None, percent_mode: str | None = None, percent_count: int = 1) -> CheckResult:
        previous_rng = getattr(self._draw_state, "rng", None)
        if rng is not None:
            self._draw_state.rng = rng
        try:
            return self._check(skill, rule=rule, percent_mode=percent_mode, percent_count=percent_count)
        finally:
            if rng is not None:
                if previous_rng is None:
                    del self._draw_state.rng
                else:
                    self._draw_state.rng = previous_rng

    def _check(self, skill: int, *, rule: int, percent_mode: str | None, percent_count: int) -> CheckResult:
        self._draw_state.draw_count = 0
        if isinstance(skill, bool) or not isinstance(skill, int) or not 0 <= skill <= 10_000:
            raise DiceError("技能值必须是 0 到 10000 的整数")
        if isinstance(rule, bool) or not isinstance(rule, int) or not 0 <= rule <= 7:
            raise DiceError("COC 房规必须是 0 到 7 的整数")

        if percent_mode is None:
            if isinstance(percent_count, bool) or not isinstance(percent_count, int) or percent_count != 1:
                raise DiceError("未指定 B/P 模式时奖励/惩罚骰数量必须为 1")
            roll = self._randint(1, 100)
            roll_detail = None
        else:
            roll, roll_detail = self._roll_percentile(percent_mode, percent_count)
        if rule == 0:
            if roll == 100:
                outcome, threshold = "fumble", 100
            elif roll == 1:
                outcome, threshold = "critical", 1
            elif roll <= skill // 5:
                outcome, threshold = "extreme", skill // 5
            elif roll <= skill // 2:
                outcome, threshold = "hard", skill // 2
            elif roll <= skill:
                outcome, threshold = "success", skill
            elif skill >= 50 or roll < 96:
                outcome, threshold = "failure", skill
            else:
                outcome, threshold = "fumble", 96
        elif rule == 1:
            if roll == 100:
                outcome, threshold = "fumble", 100
            elif roll == 1 or (roll <= 5 and skill >= 50):
                outcome, threshold = "critical", 1 if roll == 1 else 5
            elif roll <= skill // 5:
                outcome, threshold = "extreme", skill // 5
            elif roll <= skill // 2:
                outcome, threshold = "hard", skill // 2
            elif roll <= skill:
                outcome, threshold = "success", skill
            elif skill >= 50 or roll < 96:
                outcome, threshold = "failure", skill
            else:
                outcome, threshold = "fumble", 96
        elif rule == 2:
            if roll == 100:
                outcome, threshold = "fumble", 100
            elif roll <= 5 and roll <= skill:
                outcome, threshold = "critical", min(5, skill)
            elif roll <= skill // 5:
                outcome, threshold = "extreme", skill // 5
            elif roll <= skill // 2:
                outcome, threshold = "hard", skill // 2
            elif roll <= skill:
                outcome, threshold = "success", skill
            elif roll < 96:
                outcome, threshold = "failure", skill
            else:
                outcome, threshold = "fumble", 96
        elif rule == 3:
            if roll >= 96:
                outcome, threshold = "fumble", 96
            elif roll <= 5:
                outcome, threshold = "critical", 5
            elif roll <= skill // 5:
                outcome, threshold = "extreme", skill // 5
            elif roll <= skill // 2:
                outcome, threshold = "hard", skill // 2
            elif roll <= skill:
                outcome, threshold = "success", skill
            else:
                outcome, threshold = "failure", skill
        elif rule == 4:
            if roll == 100:
                outcome, threshold = "fumble", 100
            elif roll <= 5 and roll <= skill // 10:
                outcome, threshold = "critical", min(5, skill // 10)
            elif roll <= skill // 5:
                outcome, threshold = "extreme", skill // 5
            elif roll <= skill // 2:
                outcome, threshold = "hard", skill // 2
            elif roll <= skill:
                outcome, threshold = "success", skill
            elif skill >= 50 or roll < 96 + skill // 10:
                outcome, threshold = "failure", skill
            else:
                outcome, threshold = "fumble", 96 + skill // 10
        elif rule == 5:
            if roll >= 99:
                outcome, threshold = "fumble", 99
            elif roll <= 2 and roll < skill // 10:
                outcome, threshold = "critical", min(2, max(1, skill // 10 - 1))
            elif roll <= skill // 5:
                outcome, threshold = "extreme", skill // 5
            elif roll <= skill // 2:
                outcome, threshold = "hard", skill // 2
            elif roll <= skill:
                outcome, threshold = "success", skill
            elif skill >= 50 or roll < 96:
                outcome, threshold = "failure", skill
            else:
                outcome, threshold = "fumble", 96
        elif rule == 6:
            if roll > skill:
                if roll == 100 or roll % 11 == 0:
                    outcome, threshold = "fumble", roll
                else:
                    outcome, threshold = "failure", skill
            elif roll == 1 or roll % 11 == 0:
                outcome, threshold = "critical", 1 if roll == 1 else skill
            else:
                outcome, threshold = "success", skill
        else:  # BRP (rule 7)
            if roll >= 100:
                outcome, threshold = "fumble", 100
            elif roll >= 96:
                fumble_offset = self._trunc_div(90 - skill, 20)
                if fumble_offset + roll >= 100:
                    outcome, threshold = "fumble", 100 - fumble_offset
                else:
                    outcome, threshold = "failure", skill
            elif roll == 1 or roll <= skill // 5:
                outcome, threshold = "extreme", max(1, skill // 5)
            elif roll <= skill:
                outcome, threshold = "success", skill
            else:
                outcome, threshold = "failure", skill

        return CheckResult(roll=roll, skill=skill, outcome=outcome, threshold=threshold, rule=rule, roll_detail=roll_detail)

    def sanity_check(self, sanity: int, success_loss: int, failure_loss: int, *, rule: int = 0, rng: RandomSource | None = None) -> tuple[CheckResult, int]:
        if isinstance(sanity, bool) or not isinstance(sanity, int) or not 0 <= sanity <= 100:
            raise DiceError("理智值必须是 0 到 100 的整数")
        if success_loss < 0 or failure_loss < 0:
            raise DiceError("理智损失不能为负数")
        result = self.check(sanity, rule=rule, rng=rng)
        loss = success_loss if result.success else failure_loss
        return result, min(sanity, loss)

    @staticmethod
    def describe_check(result: CheckResult) -> str:
        labels = {
            "critical": "大成功", "extreme": "极难成功", "hard": "困难成功",
            "special": "特殊成功", "success": "成功", "failure": "失败", "fumble": "大失败",
        }
        roll_text = result.roll_detail or f"D100={result.roll}"
        return f"{roll_text}，技能 {result.skill}，{labels[result.outcome]}（阈值 {result.threshold}）"
