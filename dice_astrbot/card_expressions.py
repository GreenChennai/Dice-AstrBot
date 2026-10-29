"""Bounded expansion of user-authored Dice card expressions; never executes scripts."""
from __future__ import annotations

import re
from collections.abc import Callable

MAX_CARD_EXPRESSION_LENGTH = 160
MAX_CARD_REFERENCE_DEPTH = 16

_REFERENCE = re.compile(r"\{([^{}]+)\}")
_EXPRESSION_CHARS = re.compile(r"^[A-Za-z0-9+\-*/×÷() \t]+$")
_NAME_CHARS = re.compile(r"^[A-Za-z0-9_.\u4e00-\u9fff -]{1,40}$")


class CardExpressionError(ValueError):
    """Invalid, ambiguous, recursive, or unsafe card expression."""


def _valid_reference_name(name: str) -> bool:
    return bool(_NAME_CHARS.fullmatch(name.strip()))


def validate_card_expression_source(expression: str) -> str:
    """Validate bounded Dice syntax and placeholders before persisting it."""
    if not isinstance(expression, str):
        raise CardExpressionError("卡片表达式必须是文本")
    raw = expression.strip()
    if not raw or len(raw) > MAX_CARD_EXPRESSION_LENGTH:
        raise CardExpressionError(f"卡片表达式长度须为 1 到 {MAX_CARD_EXPRESSION_LENGTH} 个字符")
    if raw.startswith("&"):
        if not _valid_reference_name(raw[1:]):
            raise CardExpressionError("表达式引用格式无效")
        return raw
    cursor = 0
    pieces: list[str] = []
    for match in _REFERENCE.finditer(raw):
        segment = raw[cursor:match.start()]
        if "{" in segment or "}" in segment:
            raise CardExpressionError("表达式引用括号不匹配")
        if not _valid_reference_name(match.group(1)):
            raise CardExpressionError("表达式引用名称无效")
        pieces.extend((segment, "0"))
        cursor = match.end()
    tail = raw[cursor:]
    if "{" in tail or "}" in tail:
        raise CardExpressionError("表达式引用括号不匹配")
    pieces.append(tail)
    candidate = "".join(pieces)
    if not _EXPRESSION_CHARS.fullmatch(candidate):
        raise CardExpressionError("表达式包含不支持的字符；仅允许骰式、四则运算及 {属性} 引用")
    return raw


class CardExpressionResolver:
    """Resolve &name formulas and {name} values with cycle/size/depth guards."""

    def __init__(self, attributes: object, normalizer: Callable[[str], str] | None = None):
        self.attributes = attributes if isinstance(attributes, dict) else {}
        self.normalizer = normalizer or (lambda value: value)

    def _normalize(self, name: str) -> str:
        try:
            return str(self.normalizer(name.strip())).casefold()
        except Exception as exc:
            raise CardExpressionError("卡片引用名称无法规范化") from exc

    def _find_key(self, name: str, *, expression_only: bool) -> str | None:
        base = name.strip()
        if base.startswith("&"):
            base = base[1:]
        if not _valid_reference_name(base):
            raise CardExpressionError("卡片引用名称无效")
        prefixes = ("&",) if expression_only else ("&", "")
        wanted = self._normalize(base)
        for prefix in prefixes:
            exact = prefix + base
            if exact in self.attributes:
                return exact
            matches = [
                key for key in self.attributes
                if isinstance(key, str)
                and key.startswith(prefix)
                and (prefix != "&" or len(key) > 1)
                and self._normalize(key[1:] if prefix == "&" else key) == wanted
            ]
            if len(matches) > 1:
                raise CardExpressionError(f"卡片引用「{base}」大小写冲突，拒绝展开")
            if matches:
                return matches[0]
        return None

    def has_saved_expression(self, name: str) -> bool:
        base = name.strip()[1:] if isinstance(name, str) and name.strip().startswith("&") else name.strip() if isinstance(name, str) else ""
        if not _valid_reference_name(base):
            return False
        return self._find_key(base, expression_only=True) is not None

    def has_reference(self, name: str) -> bool:
        base = name.strip()[1:] if isinstance(name, str) and name.strip().startswith("&") else name.strip() if isinstance(name, str) else ""
        if not _valid_reference_name(base):
            return False
        return self._find_key(base, expression_only=False) is not None

    def resolve_saved_expression(self, name: str) -> str:
        return self._resolve_key(name, (), 0, expression_only=True)

    def resolve_reference(self, name: str) -> str:
        return self._resolve_key(name, (), 0, expression_only=False)

    def _resolve_key(self, name: str, stack: tuple[str, ...], depth: int, *, expression_only: bool) -> str:
        if depth >= MAX_CARD_REFERENCE_DEPTH:
            raise CardExpressionError(f"卡片引用深度超过 {MAX_CARD_REFERENCE_DEPTH}")
        base = name.strip()[1:] if name.strip().startswith("&") else name.strip()
        identity = self._normalize(base)
        if identity in stack:
            raise CardExpressionError(f"卡片表达式存在循环引用：{base}")
        key = self._find_key(base, expression_only=expression_only)
        if key is None:
            raise CardExpressionError(f"角色卡不存在表达式/属性「{base}」")
        raw_value = self.attributes[key]
        next_stack = stack + (identity,)
        if type(raw_value) is int:
            return str(raw_value)
        if not isinstance(raw_value, str):
            raise CardExpressionError(f"角色卡引用「{base}」不是可用文本或整数")
        raw = validate_card_expression_source(raw_value)
        if raw.startswith("&"):
            return self._resolve_key(raw[1:], next_stack, depth + 1, expression_only=False)

        def replace(match: re.Match[str]) -> str:
            return self._resolve_key(match.group(1), next_stack, depth + 1, expression_only=False)

        expanded = _REFERENCE.sub(replace, raw)
        if "{" in expanded or "}" in expanded:
            raise CardExpressionError("卡片表达式引用括号不匹配")
        if len(expanded) > MAX_CARD_EXPRESSION_LENGTH or not _EXPRESSION_CHARS.fullmatch(expanded):
            raise CardExpressionError("展开后的卡片表达式超出长度或字符限制")
        return expanded
