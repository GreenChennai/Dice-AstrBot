from __future__ import annotations

from functools import lru_cache

MAX_PATTERN_LENGTH = 128
MAX_INPUT_LENGTH = 400
MAX_NFA_STATES = 256
MAX_MATCH_STEPS = 100_000
MAX_REPEAT = 64
MAX_NESTING = 16
MAX_CAPTURE_GROUPS = 99


class SafeRegexError(ValueError):
    pass


class _Parser:
    def __init__(self, pattern: str) -> None:
        if not isinstance(pattern, str) or not pattern or len(pattern) > MAX_PATTERN_LENGTH:
            raise SafeRegexError("regex pattern must be 1-128 characters")
        self.pattern = pattern
        self.pos = 0
        self.group_count = 0

    def parse(self):
        node = self._alternation(0)
        if self.pos != len(self.pattern):
            raise SafeRegexError("unexpected closing delimiter")
        return node

    def _alternation(self, depth: int):
        branches = [self._sequence(depth)]
        while self._peek() == "|":
            self.pos += 1
            branches.append(self._sequence(depth))
        return branches[0] if len(branches) == 1 else ("alt", tuple(branches))

    def _sequence(self, depth: int):
        nodes = []
        while self.pos < len(self.pattern) and self._peek() not in ")|":
            node = self._atom(depth)
            if self._peek() in {"*", "+", "?", "{"}:
                node = self._quantify(node)
            nodes.append(node)
        if not nodes:
            return ("empty",)
        return nodes[0] if len(nodes) == 1 else ("seq", tuple(nodes))

    def _atom(self, depth: int):
        if self.pos >= len(self.pattern):
            raise SafeRegexError("missing expression atom")
        char = self.pattern[self.pos]
        self.pos += 1
        if char == "(":
            if self._peek() == "?":
                raise SafeRegexError("lookaround and special groups are unsupported")
            if depth >= MAX_NESTING:
                raise SafeRegexError("group nesting limit exceeded")
            self.group_count += 1
            group_id = self.group_count
            node = self._alternation(depth + 1)
            if self._peek() != ")":
                raise SafeRegexError("unclosed group")
            self.pos += 1
            return ("group", group_id, node)
        if char == "[":
            return self._character_class()
        if char == "\\":
            return self._escape(False)
        if char in ")*+?{}":
            raise SafeRegexError("quantifier or closing delimiter has no atom")
        if char == ".":
            return ("dot",)
        if char == "^":
            return ("assert", "start")
        if char == "$":
            return ("assert", "end")
        return ("lit", char)

    def _quantify(self, node):
        char = self._peek()
        if char in {"*", "+", "?"}:
            self.pos += 1
            minimum, maximum = {"*": (0, None), "+": (1, None), "?": (0, 1)}[char]
        else:
            minimum, maximum = self._brace_quantifier()
        greedy = True
        if self._peek() == "?":
            self.pos += 1
            greedy = False
        if self._peek() in {"*", "+", "?", "{"}:
            raise SafeRegexError("stacked quantifiers are unsupported")
        if node[0] == "assert":
            raise SafeRegexError("anchors cannot be quantified")
        return ("repeat", node, minimum, maximum, greedy)

    def _brace_quantifier(self) -> tuple[int, int | None]:
        self.pos += 1
        start = self.pos
        while self._peek().isascii() and self._peek().isdigit():
            self.pos += 1
        if self.pos == start:
            raise SafeRegexError("invalid bounded repetition")
        minimum = int(self.pattern[start:self.pos])
        if self._peek() == "}":
            self.pos += 1
            maximum = minimum
        elif self._peek() == ",":
            self.pos += 1
            upper_start = self.pos
            while self._peek().isascii() and self._peek().isdigit():
                self.pos += 1
            maximum = int(self.pattern[upper_start:self.pos]) if self.pos > upper_start else None
            if self._peek() != "}":
                raise SafeRegexError("unclosed bounded repetition")
            self.pos += 1
        else:
            raise SafeRegexError("invalid bounded repetition")
        if minimum > MAX_REPEAT or (maximum is not None and (maximum > MAX_REPEAT or maximum < minimum)):
            raise SafeRegexError("bounded repetition must be ordered and at most 64")
        return minimum, maximum

    def _character_class(self):
        negate = False
        if self._peek() == "^":
            self.pos += 1
            negate = True
        items = []
        if self._peek() == "]":
            items.append(("lit", "]"))
            self.pos += 1
        while self.pos < len(self.pattern) and self._peek() != "]":
            if self._peek() == "\\":
                self.pos += 1
                item = self._class_escape()
            else:
                item = ("lit", self.pattern[self.pos])
                self.pos += 1
            if item[0] == "lit" and item[1] != "-" and self._peek() == "-" and self.pos + 1 < len(self.pattern) and self.pattern[self.pos + 1] != "]":
                self.pos += 1
                if self._peek() == "\\":
                    self.pos += 1
                    end = self._class_escape()
                else:
                    end = ("lit", self.pattern[self.pos])
                    self.pos += 1
                if end[0] != "lit" or ord(item[1]) > ord(end[1]):
                    raise SafeRegexError("invalid character range")
                items.append(("range", item[1], end[1]))
            else:
                items.append(item)
        if self._peek() != "]":
            raise SafeRegexError("unclosed character class")
        self.pos += 1
        if not items:
            raise SafeRegexError("empty character class")
        return ("class", negate, tuple(items))

    def _class_escape(self):
        if self.pos >= len(self.pattern):
            raise SafeRegexError("trailing escape")
        char = self.pattern[self.pos]
        self.pos += 1
        if char in "dDwWsS":
            base = {"d": "digit", "w": "word", "s": "space"}[char.casefold()]
            return ("not_category" if char.isupper() else "category", base)
        if char in "nrtfv0":
            return ("lit", {"n": "\n", "r": "\r", "t": "\t", "f": "\f", "v": "\v", "0": "\0"}[char])
        if char == "b":
            return ("lit", "\b")
        if char == "x":
            return ("lit", self._hex_escape(2))
        if char == "u":
            return ("lit", self._hex_escape(4))
        if char.isdigit():
            raise SafeRegexError("backreferences are unsupported")
        return ("lit", char)

    def _escape(self, in_class: bool):
        if self.pos >= len(self.pattern):
            raise SafeRegexError("trailing escape")
        char = self.pattern[self.pos]
        self.pos += 1
        if char in "dDwWsS":
            if char in "bB":
                raise SafeRegexError("word-boundary assertions are unsupported")
            base = {"d": "digit", "w": "word", "s": "space"}[char.casefold()]
            item = ("not_category" if char.isupper() else "category", base)
            return ("class", False, (item,))
        if char in "nrtfv0":
            return ("lit", {"n": "\n", "r": "\r", "t": "\t", "f": "\f", "v": "\v", "0": "\0"}[char])
        if char in "bB":
            if in_class and char == "b":
                return ("lit", "\b")
            raise SafeRegexError("word-boundary assertions are unsupported")
        if char == "x":
            return ("lit", self._hex_escape(2))
        if char == "u":
            return ("lit", self._hex_escape(4))
        if char.isdigit():
            raise SafeRegexError("backreferences are unsupported")
        return ("lit", char)

    def _hex_escape(self, count: int) -> str:
        end = self.pos + count
        digits = self.pattern[self.pos:end]
        if len(digits) != count or any(char not in "0123456789abcdefABCDEF" for char in digits):
            raise SafeRegexError("invalid hexadecimal escape")
        self.pos = end
        return chr(int(digits, 16))

    def _peek(self) -> str:
        return self.pattern[self.pos] if self.pos < len(self.pattern) else ""


class _Compiler:
    def __init__(self) -> None:
        self.states: list[dict] = []

    def _state(self) -> int:
        if len(self.states) >= MAX_NFA_STATES:
            raise SafeRegexError("compiled regex exceeds the 256-state limit")
        self.states.append({"eps": [], "assert": [], "trans": [], "tags": [], "accept": False})
        return len(self.states) - 1

    def _empty(self) -> tuple[int, int]:
        start, end = self._state(), self._state()
        self.states[start]["eps"].append(end)
        return start, end

    def _concat(self, fragments: list[tuple[int, int]]) -> tuple[int, int]:
        if not fragments:
            return self._empty()
        for left, right in zip(fragments, fragments[1:]):
            self.states[left[1]]["eps"].append(right[0])
        return fragments[0][0], fragments[-1][1]

    def _optional(self, node, greedy: bool = True) -> tuple[int, int]:
        start, end = self._state(), self._state()
        child_start, child_end = self._compile(node)
        branches = (child_start, end) if greedy else (end, child_start)
        self.states[start]["eps"].extend(branches)
        self.states[child_end]["eps"].append(end)
        return start, end

    def _star(self, node, greedy: bool = True) -> tuple[int, int]:
        start, end = self._state(), self._state()
        child_start, child_end = self._compile(node)
        branches = (child_start, end) if greedy else (end, child_start)
        self.states[start]["eps"].extend(branches)
        self.states[child_end]["eps"].extend((child_start, end))
        return start, end

    def _compile(self, node) -> tuple[int, int]:
        kind = node[0]
        if kind == "empty":
            return self._empty()
        if kind == "seq":
            return self._concat([self._compile(child) for child in node[1]])
        if kind == "alt":
            start, end = self._state(), self._state()
            for child in node[1]:
                child_start, child_end = self._compile(child)
                self.states[start]["eps"].append(child_start)
                self.states[child_end]["eps"].append(end)
            return start, end
        if kind == "group":
            group_id = node[1]
            child_start, child_end = self._compile(node[2])
            if group_id > MAX_CAPTURE_GROUPS:
                return child_start, child_end
            start, end = self._state(), self._state()
            self.states[start]["eps"].append(child_start)
            self.states[start]["tags"].append((child_start, "open", group_id))
            self.states[child_end]["eps"].append(end)
            self.states[child_end]["tags"].append((end, "close", group_id))
            return start, end
        if kind == "repeat":
            child, minimum, maximum, greedy = node[1], node[2], node[3], node[4]
            fragments = [self._compile(child) for _ in range(minimum)]
            if maximum is None:
                fragments.append(self._star(child, greedy))
            else:
                fragments.extend(self._optional(child, greedy) for _ in range(maximum - minimum))
            return self._concat(fragments)
        start, end = self._state(), self._state()
        if kind == "assert":
            self.states[start]["assert"].append((node[1], end))
        else:
            self.states[start]["trans"].append((kind, node[1:] if kind != "lit" else node[1], end))
        return start, end

    def build(self, node, capture_count: int = 0):
        self.capture_count = capture_count
        start, end = self._compile(node)
        self.states[end]["accept"] = True
        frozen = tuple({
            "eps": tuple(state["eps"]),
            "assert": tuple(state["assert"]),
            "trans": tuple(state["trans"]),
            "tags": tuple(state["tags"]),
            "accept": state["accept"],
        } for state in self.states)
        return _CompiledRegex(frozen, start, end, self.capture_count)


class _CompiledRegex:
    def __init__(self, states: tuple[dict, ...], start: int, accept: int, capture_count: int = 0) -> None:
        self.states = states
        self.start = start
        self.accept = accept
        self.capture_count = capture_count

    @staticmethod
    def _category_matches(category: str, char: str) -> bool:
        if category == "digit":
            return char.isdecimal()
        if category == "word":
            return char == "_" or char.isalnum()
        return char.isspace()

    @staticmethod
    def _literal_matches(expected: str, char: str) -> bool:
        return expected.casefold() == char.casefold()

    @classmethod
    def _class_matches(cls, negate: bool, items: tuple, char: str) -> bool:
        matched = False
        for item in items:
            kind = item[0]
            if kind == "lit" and cls._literal_matches(item[1], char):
                matched = True
            elif kind == "range":
                candidates = {char}
                for variant in (char.lower(), char.upper(), char.casefold()):
                    if len(variant) == 1:
                        candidates.add(variant)
                if any(ord(item[1]) <= ord(candidate) <= ord(item[2]) for candidate in candidates):
                    matched = True
            elif kind == "category" and cls._category_matches(item[1], char):
                matched = True
            elif kind == "not_category" and not cls._category_matches(item[1], char):
                matched = True
            if matched:
                break
        return not matched if negate else matched

    @classmethod
    def _transition_matches(cls, kind: str, value, char: str) -> bool:
        if kind == "lit":
            return cls._literal_matches(value, char)
        if kind == "dot":
            return char not in "\n\r\u2028\u2029"
        negate, items = value
        return cls._class_matches(negate, items, char)

    def fullmatch(self, text: str, *, max_steps: int = MAX_MATCH_STEPS) -> bool:
        if not isinstance(text, str) or len(text) > MAX_INPUT_LENGTH:
            raise SafeRegexError("regex input exceeds the 400-character limit")
        steps = 0

        def closure(initial: set[int], position: int) -> set[int]:
            nonlocal steps
            result: set[int] = set()
            stack = list(initial)
            while stack:
                state_id = stack.pop()
                if state_id in result:
                    continue
                result.add(state_id)
                state = self.states[state_id]
                for target in state["eps"]:
                    steps += 1
                    if steps > max_steps:
                        raise SafeRegexError("regex match step budget exceeded")
                    stack.append(target)
                for assertion, target in state["assert"]:
                    steps += 1
                    if steps > max_steps:
                        raise SafeRegexError("regex match step budget exceeded")
                    if (assertion == "start" and position == 0) or (assertion == "end" and position == len(text)):
                        stack.append(target)
            return result

        active = closure({self.start}, 0)
        for position, char in enumerate(text):
            next_states: set[int] = set()
            for state_id in active:
                for kind, value, target in self.states[state_id]["trans"]:
                    steps += 1
                    if steps > max_steps:
                        raise SafeRegexError("regex match step budget exceeded")
                    if self._transition_matches(kind, value, char):
                        next_states.add(target)
            if not next_states:
                return False
            active = closure(next_states, position + 1)
        return self.accept in closure(active, len(text))

    def fullmatch_captures(self, text: str, *, max_steps: int = MAX_MATCH_STEPS) -> tuple[str | None, ...] | None:
        """Return numbered capture groups for a full match, else None."""
        if not isinstance(text, str) or len(text) > MAX_INPUT_LENGTH:
            raise SafeRegexError("regex input exceeds the 400-character limit")
        initial_captures = tuple((None, None) for _ in range(self.capture_count))
        stack = [(self.start, 0, initial_captures)]
        visited: set[tuple[int, int]] = set()
        steps = 0

        while stack:
            state_id, position, captures = stack.pop()
            state_key = (state_id, position)
            if state_key in visited:
                continue
            visited.add(state_key)
            steps += 1
            if steps > max_steps:
                raise SafeRegexError("regex match step budget exceeded")

            state = self.states[state_id]
            if state["accept"] and position == len(text):
                return tuple(
                    text[start:end] if start is not None and end is not None else None
                    for start, end in captures
                )

            edges = []
            for assertion, target in state["assert"]:
                steps += 1
                if steps > max_steps:
                    raise SafeRegexError("regex match step budget exceeded")
                if (assertion == "start" and position == 0) or (assertion == "end" and position == len(text)):
                    edges.append((target, position, captures))

            tags = {target: (action, group_id) for target, action, group_id in state["tags"]}
            for target in state["eps"]:
                steps += 1
                if steps > max_steps:
                    raise SafeRegexError("regex match step budget exceeded")
                next_captures = captures
                tag = tags.get(target)
                if tag is not None:
                    action, group_id = tag
                    updated = list(captures)
                    group_start, _ = updated[group_id - 1]
                    updated[group_id - 1] = (
                        (position, None) if action == "open"
                        else (group_start if group_start is not None else position, position)
                    )
                    next_captures = tuple(updated)
                edges.append((target, position, next_captures))

            if position < len(text):
                char = text[position]
                for kind, value, target in state["trans"]:
                    steps += 1
                    if steps > max_steps:
                        raise SafeRegexError("regex match step budget exceeded")
                    if self._transition_matches(kind, value, char):
                        edges.append((target, position + 1, captures))

            stack.extend(reversed(edges))

        return None


@lru_cache(maxsize=256)
def _compile_cached(pattern: str) -> _CompiledRegex:
    parser = _Parser(pattern)
    node = parser.parse()
    return _Compiler().build(node, min(parser.group_count, MAX_CAPTURE_GROUPS))


def compile_safe_regex(pattern: object) -> _CompiledRegex:
    if not isinstance(pattern, str):
        raise SafeRegexError("regex pattern must be text")
    return _compile_cached(pattern)


def safe_regex_fullmatch(pattern: str, text: str) -> bool:
    return compile_safe_regex(pattern).fullmatch(text)
