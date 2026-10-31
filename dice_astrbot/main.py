from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
import secrets
import shlex
import threading
import time
from collections.abc import Mapping
from typing import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from zoneinfo import ZoneInfo

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Plain
from astrbot.api.star import Context, Star, StarTools

try:
    from .dice_engine import DiceEngine, DiceError, MAX_SIDES
    from .card_expressions import CardExpressionResolver, validate_card_expression_source
    from .storage import JsonStore
    from .coc_skills import SKILL_DEFAULT_VALUES, SKILL_NAME_ALIASES
    from .coc_formulas import COC7_ROLL_EXPRESSIONS, COC7_VARIABLE_EXPRESSIONS
    from .coc_insanity import format_insanity
    from .dice_pool import DicePoolError, MAX_TOTAL_POOL_ROLLS, roll_dice_pool
    from .dice_statistics import CardStatisticsError, format_card_statistics, updated_card_statistics, validate_card_statistics
    from .legacy_rdconf import GROUP_OPTION_FLAGS, MAX_ARCHIVE_BYTES, MAX_WELCOME_MESSAGE_LENGTH, LegacyRDConfError, build_chat_config_import_updates, build_player_import_updates, build_user_nickname_import_updates, build_user_settings_import_updates, count_chat_config_import_conflicts, count_user_nickname_import_conflicts, count_user_settings_import_conflicts, decode_chat_conf_dat, decode_player_cards_rdconf, decode_user_conf_dat, decode_user_conf_dat_importable, decode_user_conf_rdconf, decode_user_conf_rdconf_importable, import_chat_config_into_store, import_player_cards_into_store, import_user_nicknames_into_store, import_user_settings_into_store
    from .builtin_decks import BUILTIN_DECKS
    from .builtin_deck_templates import BUILTIN_PUBLIC_DECK_NAMES, BUILTIN_TEMPLATE_DECKS, draw_builtin_public_deck, draw_builtin_public_deck_batch, expand_builtin_public_deck_texts, find_builtin_public_deck
    from .safe_regex import MAX_INPUT_LENGTH as MAX_SAFE_REGEX_INPUT_LENGTH, SafeRegexError, compile_safe_regex
except ImportError:
    from dice_engine import DiceEngine, DiceError, MAX_SIDES
    from card_expressions import CardExpressionResolver, validate_card_expression_source
    from storage import JsonStore
    from coc_skills import SKILL_DEFAULT_VALUES, SKILL_NAME_ALIASES
    from coc_formulas import COC7_ROLL_EXPRESSIONS, COC7_VARIABLE_EXPRESSIONS
    from coc_insanity import format_insanity
    from dice_pool import DicePoolError, MAX_TOTAL_POOL_ROLLS, roll_dice_pool
    from dice_statistics import CardStatisticsError, format_card_statistics, updated_card_statistics, validate_card_statistics
    from legacy_rdconf import GROUP_OPTION_FLAGS, MAX_ARCHIVE_BYTES, MAX_WELCOME_MESSAGE_LENGTH, LegacyRDConfError, build_chat_config_import_updates, build_player_import_updates, build_user_nickname_import_updates, build_user_settings_import_updates, count_chat_config_import_conflicts, count_user_nickname_import_conflicts, count_user_settings_import_conflicts, decode_chat_conf_dat, decode_player_cards_rdconf, decode_user_conf_dat, decode_user_conf_dat_importable, decode_user_conf_rdconf, decode_user_conf_rdconf_importable, import_chat_config_into_store, import_player_cards_into_store, import_user_nicknames_into_store, import_user_settings_into_store
    from builtin_decks import BUILTIN_DECKS
    from builtin_deck_templates import BUILTIN_PUBLIC_DECK_NAMES, BUILTIN_TEMPLATE_DECKS, draw_builtin_public_deck, draw_builtin_public_deck_batch, expand_builtin_public_deck_texts, find_builtin_public_deck
    from safe_regex import MAX_INPUT_LENGTH as MAX_SAFE_REGEX_INPUT_LENGTH, SafeRegexError, compile_safe_regex

MAX_DECK_WEIGHT_DRAWS = 100_000
MAX_SEND_NOTICE_TEXT_LENGTH = 500
ADMIN_NOTICE_PREFIX = "[Dice-AstrBot-AdminNotice] "


@dataclass(frozen=True)
class HiddenCheckReply:
    private_text: str
    public_ack: str = "隐藏检定已执行；结果仅尝试私信发送，若未收到请检查平台私聊权限。"


@dataclass(frozen=True)
class GroupMessageReply:
    target_umo: str
    text: str


@dataclass(frozen=True)
class NoticeCommandReply:
    text: str
    notice_level: int
    broadcast_text: str | None = None


class _SessionRandomSource:
    """Random source that consumes configured session dice bags inside a store transaction."""

    _dice_session_roulette = True

    def __init__(self, session: dict) -> None:
        self.session = session
        self._random = secrets.SystemRandom()

    @staticmethod
    def _new_bag(face: int, copies: int) -> dict:
        pool = [value for value in range(1, face + 1) for _ in range(copies)]
        return {"face": face, "copies": copies, "pool": pool, "remaining": len(pool)}

    def randint(self, low: int, high: int) -> int:
        roulette = self.session.get("roulette")
        if low != 1 or not 1 <= high <= 100 or not isinstance(roulette, dict):
            return self._random.randint(low, high)
        key = str(high)
        bag = roulette.get(key)
        if not isinstance(bag, dict):
            return self._random.randint(low, high)
        face, copies = bag.get("face"), bag.get("copies")
        pool, remaining = bag.get("pool"), bag.get("remaining")
        valid = (
            type(face) is int and face == high
            and type(copies) is int and 1 <= copies and face * copies <= 100
            and isinstance(pool, list) and len(pool) == face * copies
            and all(type(value) is int and 1 <= value <= face for value in pool)
            and sorted(pool) == [value for value in range(1, face + 1) for _ in range(copies)]
            and type(remaining) is int and 0 <= remaining <= len(pool)
        )
        if not valid:
            bag = self._new_bag(high, 1)
            roulette[key] = bag
            pool, remaining = bag["pool"], bag["remaining"]
        if remaining == 0:
            remaining = len(pool)
        index = secrets.randbelow(remaining)
        last = remaining - 1
        pool[index], pool[last] = pool[last], pool[index]
        result = pool[last]
        bag["remaining"] = last
        return result


PLUGIN_DATA_DIR = "dice_astrbot"
STAT_ALIASES = {
    "STR": "STR", "STRENGTH": "STR", "力量": "STR", "CON": "CON", "CONSTITUTION": "CON", "体质": "CON",
    "SIZ": "SIZ", "SIZE": "SIZ", "体型": "SIZ", "DEX": "DEX", "DEXTERITY": "DEX", "敏捷": "DEX",
    "APP": "APP", "外貌": "APP", "INT": "INT", "INTELLIGENCE": "INT", "智力": "INT",
    "POW": "POW", "POWER": "POW", "意志": "POW", "EDU": "EDU", "EDUCATION": "EDU", "教育": "EDU",
    "LUCK": "LUCK", "幸运": "LUCK", "SAN": "SAN", "SANITY": "SAN", "理智": "SAN",
    "HP": "HP", "生命": "HP", "MP": "MP", "魔力": "MP",
}


COC7_BASE_BUILD_FORMULAS = (
    ("STR", "3d6*5"), ("CON", "3d6*5"), ("SIZ", "2d6*5+30"),
    ("DEX", "3d6*5"), ("APP", "3d6*5"), ("INT", "2d6*5+30"),
    ("POW", "3d6*5"), ("EDU", "2d6*5+30"), ("LUCK", "3d6*5"),
)

# Upstream COC7_BG placeholders resolve to 0 when absent.
COC7_BG_BUILD_FORMULAS = (
    ("性别", None), ("年龄", "7d6+8"), ("职业", None),
    ("个人描述", None), ("重要之人", None), ("思想信念", None),
    ("意义非凡之地", None), ("宝贵之物", None), ("特质", None),
)

# Dice/CharacterCard.cpp BasicCOC7, pinned at 5e2a8324a631765f694b2e3a71f5d50f7ae6e94f.
COC7_TEMPLATE_ATTRIBUTE_NAMES = (
    "职业", "年龄", "性别", "住地", "出身", "时代",
    "力量", "体质", "体型", "敏捷", "外貌", "智力", "意志", "教育",
    "生命", "理智", "魔法", "幸运", "身体状态", "精神状态", "DB", "闪避", "护甲",
)

COC_RULE_INFO = {
    0: ("规则书", "1 为大成功；技能低于 50 时 96–100 大失败，技能达到 50 时仅 100 大失败。"),
    1: ("规则 1", "技能低于 50 时 1 为大成功，技能达到 50 时 1–5 为大成功；大失败按规则书。"),
    2: ("规则 2", "1–5 且不高于成功率为大成功；100 或高于成功率的 96–99 为大失败。"),
    3: ("规则 3", "1–5 为大成功，96–100 为大失败。"),
    4: ("规则 4", "1–5 且不高于十分之一成功率为大成功；低于 50 时从 96+十分之一成功率起为大失败。"),
    5: ("规则 5", "1–2 且低于五分之一成功率为大成功；低于 50 时 96–100、达到 50 时 99–100 为大失败。"),
    6: ("绿色三角洲", "成功范围内的 1 或重复数字为大成功；失败范围内的 100 或重复数字为大失败。"),
    7: ("BRP", "1 或不高于五分之一成功率为极难成功；96–100 强制失败并按成功率判定大失败。"),
}


class Main(Star):
    """Native AstrBot implementation of core Dice TRPG workflows."""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context, config)
        self.config = config or {}
        self.data_dir = StarTools.get_data_dir(PLUGIN_DATA_DIR)
        self.store = JsonStore(self.data_dir / "dice_state.json")
        self._system_clock_task: asyncio.Task | None = None
        self._custom_reply_cooldowns: dict[tuple[str, str, str, str], float] = {}
        self._custom_reply_limit_lock = threading.Lock()
        self._custom_reply_locks: dict[tuple[int, str, str, str], asyncio.Lock] = {}
        self._custom_reply_lock_refs: dict[tuple[int, str, str, str], int] = {}
        self._custom_reply_lock_guard = threading.Lock()
        raw_import_owner = self.config.get("legacy_import_owner_id")
        self.legacy_import_owner_id = str(raw_import_owner).strip() if isinstance(raw_import_owner, (str, int)) and not isinstance(raw_import_owner, bool) else ""
        if not self.legacy_import_owner_id.isascii() or not self.legacy_import_owner_id.isdigit():
            self.legacy_import_owner_id = ""
        raw_owner_scope = self.config.get("legacy_import_owner_scope")
        self.legacy_import_owner_scope = ""
        if isinstance(raw_owner_scope, str) and len(raw_owner_scope) <= 512:
            try:
                scope_identity = json.loads(raw_owner_scope)
            except json.JSONDecodeError:
                scope_identity = None
            if isinstance(scope_identity, list) and len(scope_identity) == 3 and all(isinstance(item, str) and item.strip() for item in scope_identity):
                self.legacy_import_owner_scope = json.dumps(scope_identity, ensure_ascii=False, separators=(",", ":"))
        self._legacy_preview = None
        self._legacy_users_preview = None
        self._legacy_groups_preview = None
        self.engine = DiceEngine()
        self.default_die = self._int_config("default_die", 100, 1, 100_000)
        self.default_coc_rule = self._int_config("default_coc_rule", 0, 0, 7)
        self.disable_jrrp = self.config.get("disable_jrrp") is True
        self.disable_me = self.config.get("disable_me") is True
        raw_prefixes = self.config.get("prefixes")
        if isinstance(raw_prefixes, str):
            raw_prefixes = [raw_prefixes]
        valid_prefixes = []
        if isinstance(raw_prefixes, (list, tuple)):
            valid_prefixes = list(dict.fromkeys(
                prefix.strip() for prefix in raw_prefixes
                if isinstance(prefix, str) and prefix.strip()
            ))
        if not valid_prefixes:
            single_prefix = self.config.get("prefix")
            if isinstance(single_prefix, str) and single_prefix.strip():
                valid_prefixes = [single_prefix.strip()]
            else:
                valid_prefixes = [".", "!"]
        self.prefixes = tuple(sorted(valid_prefixes, key=len, reverse=True))
        self.max_message_length = self._int_config("max_message_length", 500, 32, 4000)
        self.max_response_length = self._int_config("max_response_length", 4000, 256, 8000)
        self.command_names = {
            "r", "roll", "rs", "rh", "rsh", "h", "hs", "ra", "rc", "sc", "en", "w", "ww", "wh", "wwh", "coc6d", "coc7d", "coc", "coc7", "cocs", "coc7s", "cocd", "coc6", "coc6s",
            "set", "setcoc", "pc", "card", "st", "jrrp", "li", "ti", "game", "ob", "init", "ri", "ak", "deck", "draw",
            "name", "nnn", "nn", "dnd", "d20", "rule", "log", "bot", "ban", "group", "reply", "re",
            "help", "helpdoc", "user", "version", "legacy", "me", "link", "system", "admin", "master", "mod", "str", "authorize", "dismiss", "groups", "4kick", "chan", "welcome", "warning", "send", "cloud",
        }

    async def initialize(self) -> None:
        task = self._system_clock_task
        if task is None or task.done():
            self._system_clock_task = asyncio.create_task(self._system_clock_loop(), name="dice-astrbot-system-clock")

    async def terminate(self) -> None:
        task = self._system_clock_task
        self._system_clock_task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    def _int_config(self, name: str, default: int, low: int, high: int) -> int:
        try:
            value = int(self.config.get(name, default))
        except (TypeError, ValueError):
            return default
        return min(high, max(low, value))

    @staticmethod
    def _onebot_notice_payload(event: AstrMessageEvent) -> Mapping | None:
        """Return OneBot V11 notice payloads; AstrBot has no portable member-event hook."""
        try:
            platform_name = event.get_platform_name()
        except Exception:
            return None
        if not isinstance(platform_name, str) or platform_name.strip().casefold() != "aiocqhttp":
            return None
        message_obj = getattr(event, "message_obj", None)
        payload = getattr(message_obj, "raw_message", None)
        if not isinstance(payload, Mapping) or payload.get("post_type") != "notice":
            return None
        return payload

    def _onebot_lifecycle_reply(self, event: AstrMessageEvent, notice: Mapping) -> str | None:
        """Build a bounded group join/leave notice from an aiocqhttp raw event."""
        notice_type = notice.get("notice_type")
        if notice_type not in {"group_increase", "group_decrease"} or not self._system_enabled():
            return None
        group_value = notice.get("group_id")
        user_value = notice.get("user_id")
        if isinstance(group_value, bool) or not isinstance(group_value, (str, int)):
            return None
        if isinstance(user_value, bool) or not isinstance(user_value, (str, int)):
            return None
        raw_group_id = str(group_value).strip()
        user_id = str(user_value).strip()
        if re.fullmatch(r"[0-9]{1,32}", raw_group_id) is None or re.fullmatch(r"[0-9]{1,32}", user_id) is None:
            return None
        try:
            if event.is_private_chat() or str(event.get_group_id()).strip() != raw_group_id:
                return None
        except Exception:
            return None
        group_id = self._group_id(event)
        group = self._group_snapshot(group_id)
        if group.get("enabled", True) is not True:
            return None
        sub_type = notice.get("sub_type")
        if notice_type == "group_increase":
            if sub_type not in {"approve", "invite"}:
                return None
            welcome = group.get("welcome_message", "")
            if not isinstance(welcome, str) or not welcome or len(welcome) > MAX_WELCOME_MESSAGE_LENGTH:
                return None
            return welcome
        if sub_type == "kick_me":
            return None
        try:
            bot_id = str(event.get_self_id()).strip()
        except Exception:
            bot_id = ""
        if bot_id and bot_id == user_id:
            return None
        if sub_type == "leave":
            return f"群成员 {user_id} 已退出本群。"
        if sub_type == "kick":
            return f"群成员 {user_id} 已被移出本群。"
        return None

    @staticmethod
    def _platform_scope(event: AstrMessageEvent) -> str:
        identity = []
        for method_name in ("get_platform_name", "get_platform_id", "get_self_id"):
            try:
                value = getattr(event, method_name, None)
                value = value() if callable(value) else value
            except Exception:
                continue
            if value is not None and str(value).strip():
                identity.append(str(value).strip())
        return json.dumps(identity, ensure_ascii=False, separators=(",", ":")) if identity else ""

    @staticmethod
    def _group_id(event: AstrMessageEvent) -> str:
        scope = Main._platform_scope(event)
        def scoped(key: str) -> str:
            return f"{scope}:{key}" if scope else key

        try:
            if event.is_private_chat():
                return scoped(f"private:{event.get_sender_id()}")
        except Exception:
            pass
        try:
            value = event.get_group_id()
        except Exception:
            value = None
        if value is not None and str(value).strip():
            return scoped(str(value))
        return scoped(f"private:{event.get_sender_id()}")

    @staticmethod
    def _sender_id(event: AstrMessageEvent) -> str:
        return str(event.get_sender_id())

    @staticmethod
    def _player_id(event: AstrMessageEvent) -> str:
        sender_id = Main._sender_id(event)
        scope = Main._platform_scope(event)
        return f"{scope}:user:{sender_id}" if scope else sender_id

    @staticmethod
    def _raw_platform_user_id(user_id: str) -> str:
        """Unwrap only IDs produced by _player_id; leave raw or malformed IDs intact."""
        scope, separator, raw_id = user_id.rpartition(":user:")
        if not separator or not raw_id:
            return user_id
        try:
            identity = json.loads(scope)
        except (TypeError, json.JSONDecodeError):
            return user_id
        return raw_id if isinstance(identity, list) and identity else user_id

    @staticmethod
    def _is_admin(event: AstrMessageEvent) -> bool:
        try:
            if event.is_private_chat():
                return True
            checker = getattr(event, "is_admin", None)
            return bool(checker()) if callable(checker) else False
        except Exception:
            return False

    @staticmethod
    def _is_platform_admin(event: AstrMessageEvent) -> bool:
        try:
            checker = getattr(event, "is_admin", None)
            return bool(checker()) if callable(checker) else False
        except Exception:
            return False

    @staticmethod
    def _default_group() -> dict:
        return {"enabled": True, "help_enabled": True, "draw_enabled": True, "rule": None, "hidden_blind": False, "jrrp_disabled": False, "me_disabled": False, "session": None, "session_ref": None, "sessions": {}, "session_counter": 0, "initiative": [], "decks": {}, "banned_users": [], "custom_replies": {}, "custom_reply_prefixes": {}, "custom_reply_searches": {}, "custom_reply_regexes": {}, "custom_replies_enabled": True, "custom_reply_usage": {}, "welcome_message": "", "logs": []}

    @staticmethod
    def _normalize_group_state(group: dict) -> dict:
        for key in ("sessions", "decks"):
            if not isinstance(group.get(key), dict):
                group[key] = {}
        for key in ("custom_replies", "custom_reply_prefixes", "custom_reply_searches", "custom_reply_regexes"):
            if key not in group:
                group[key] = {}
        if "custom_reply_usage" not in group:
            group["custom_reply_usage"] = {}
        for key in ("initiative", "logs"):
            if not isinstance(group.get(key), list):
                group[key] = []
        if "room_observers" in group and not isinstance(group.get("room_observers"), list):
            group["room_observers"] = []
        banned = group.get("banned_users")
        if isinstance(banned, str):
            group["banned_users"] = [banned] if banned else []
        elif not isinstance(banned, list):
            group["banned_users"] = []
        group["sessions"] = {key: value for key, value in group["sessions"].items() if isinstance(value, dict)}
        session_ref = group.get("session_ref")
        if session_ref is not None and (not isinstance(session_ref, str) or re.fullmatch(r"[0-9a-f]{32}", session_ref) is None):
            group["session_ref"] = None
        group["logs"] = [entry for entry in group["logs"] if isinstance(entry, dict)]
        return group

    @staticmethod
    def _valid_system_clock_entry(entry: object) -> bool:
        return (
            isinstance(entry, dict)
            and isinstance(entry.get("id"), str)
            and re.fullmatch(r"[0-9a-f]{16}", entry["id"]) is not None
            and entry.get("action") in {"on", "off"}
            and isinstance(entry.get("time"), str)
            and re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", entry["time"]) is not None
        )

    def _system_state_snapshot(self) -> dict | None:
        system = self.store.snapshot().get("system")
        if not isinstance(system, dict) or type(system.get("enabled", True)) is not bool:
            return None
        clocks = system.get("clocks", [])
        last_minute = system.get("last_clock_minute", "")
        if not isinstance(clocks, list) or len(clocks) > 100 or not isinstance(last_minute, str) or len(last_minute) > 16:
            return None
        if any(not self._valid_system_clock_entry(entry) for entry in clocks):
            return None
        return system

    def _system_enabled(self) -> bool:
        system = self._system_state_snapshot()
        return system is not None and system.get("enabled", True) is True

    def _mutate_system(self, mutation: Callable[[dict], object]):
        def apply(state: dict):
            system = state.get("system")
            if not isinstance(system, dict):
                raise ValueError("插件系统状态损坏；拒绝覆盖")
            if "enabled" not in system:
                system["enabled"] = True
            if type(system.get("enabled")) is not bool:
                raise ValueError("插件启用状态损坏；拒绝覆盖")
            if "clocks" not in system:
                system["clocks"] = []
            clocks = system.get("clocks")
            if not isinstance(clocks, list) or len(clocks) > 100 or any(not self._valid_system_clock_entry(item) for item in clocks):
                raise ValueError("定时任务状态损坏；拒绝覆盖")
            if "last_clock_minute" not in system:
                system["last_clock_minute"] = ""
            if not isinstance(system.get("last_clock_minute"), str) or len(system["last_clock_minute"]) > 16:
                raise ValueError("定时执行状态损坏；拒绝覆盖")
            return mutation(system)
        return self.store.transact(apply)

    def _execute_system_clock_minute(self, minute_key: str, clock_text: str) -> list[str]:
        if (re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-2][0-9]:[0-5][0-9]", minute_key) is None
                or re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", clock_text) is None):
            return []
        snapshot = self._system_state_snapshot()
        if snapshot is None or snapshot.get("last_clock_minute", "") == minute_key:
            return []
        if not any(entry["time"] == clock_text for entry in snapshot.get("clocks", [])):
            return []

        def apply(system: dict):
            if system.get("last_clock_minute", "") == minute_key:
                return []
            due = [entry for entry in system.get("clocks", []) if entry["time"] == clock_text]
            actions = {entry["action"] for entry in due}
            if not actions or len(actions) != 1:
                return []
            action = next(iter(actions))
            system["enabled"] = action == "on"
            system["last_clock_minute"] = minute_key
            return [action]

        actions = self._mutate_system(apply)
        if actions:
            self.logger.info("Dice-AstrBot system clock applied %s at %s", actions[0], clock_text)
        return actions

    async def _system_clock_loop(self) -> None:
        last_minute = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
        while True:
            await asyncio.sleep(5)
            now = datetime.now().astimezone()
            minute_key = now.strftime("%Y-%m-%d %H:%M")
            if minute_key == last_minute:
                continue
            last_minute = minute_key
            try:
                self._execute_system_clock_minute(minute_key, now.strftime("%H:%M"))
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.exception("Dice-AstrBot system clock failed at %s", minute_key)

    @staticmethod
    def _notice_key(platform_scope: str, target_type: str, target_id: str) -> str:
        return json.dumps([platform_scope, target_type, target_id], ensure_ascii=False, separators=(",", ":"))

    def _valid_notice_record(self, key: object, entry: object) -> bool:
        if not isinstance(key, str) or len(key) > 768 or not isinstance(entry, dict):
            return False
        if set(entry) != {"platform_scope", "platform_id", "target_type", "target_id", "level"}:
            return False
        scope = entry.get("platform_scope")
        platform_id = entry.get("platform_id")
        target_type = entry.get("target_type")
        target_id = entry.get("target_id")
        level = entry.get("level")
        if not isinstance(scope, str) or not scope or len(scope) > 512:
            return False
        if not isinstance(platform_id, str) or not platform_id or len(platform_id) > 128 or ":" in platform_id:
            return False
        try:
            identity = json.loads(scope)
        except (TypeError, json.JSONDecodeError):
            return False
        if not isinstance(identity, list) or len(identity) != 3 or not all(isinstance(value, str) and value.strip() for value in identity) or identity[1] != platform_id:
            return False
        if not isinstance(target_type, str) or target_type not in {"group", "private"} or not isinstance(target_id, str) or not target_id.isascii() or not target_id.isdecimal() or len(target_id) > 20:
            return False
        numeric_id = int(target_id)
        if numeric_id <= 0 or numeric_id > 9_223_372_036_854_775_807 or str(numeric_id) != target_id:
            return False
        if type(level) is not int or not 1 <= level <= 63:
            return False
        return key == self._notice_key(scope, target_type, target_id)

    def _valid_notice_map(self, notices: object) -> bool:
        return isinstance(notices, dict) and len(notices) <= 100 and all(self._valid_notice_record(key, item) for key, item in notices.items())

    @staticmethod
    def _normalize_censor_text(value: object) -> str:
        if not isinstance(value, str):
            return ""
        ignored = frozenset("~!@#$%^&*()-=`_+[]\\{}|;':\",./<>?")
        return "".join(char.casefold() for char in value if not char.isspace() and char not in ignored)

    def _valid_censor_map(self, censor: object) -> bool:
        if not isinstance(censor, dict) or len(censor) > 100:
            return False
        for scope, entries in censor.items():
            if not isinstance(scope, str) or not scope or len(scope) > 512 or not isinstance(entries, dict) or len(entries) > 256:
                return False
            try:
                identity = json.loads(scope)
            except (TypeError, json.JSONDecodeError):
                return False
            if not isinstance(identity, list) or len(identity) != 3 or not all(isinstance(value, str) and value.strip() and len(value) <= 128 for value in identity):
                return False
            if scope != json.dumps(identity, ensure_ascii=False, separators=(",", ":")):
                return False
            for key, entry in entries.items():
                if not isinstance(key, str) or not key or len(key) > 128 or not isinstance(entry, dict) or set(entry) != {"word", "level"}:
                    return False
                word, level = entry.get("word"), entry.get("level")
                if not isinstance(word, str) or not 1 <= len(word) <= 64 or any(ord(char) < 32 or ord(char) == 127 for char in word):
                    return False
                if self._normalize_censor_text(word) != key or type(level) is not int or not 0 <= level <= 5:
                    return False
        return True

    def _censor_matches(self, text: str, platform_scope: str) -> tuple[bool, int, list[str]]:
        try:
            censor = self.store.snapshot().get("censor", {})
        except Exception:
            return False, 0, []
        if not self._valid_censor_map(censor):
            return False, 0, []
        normalized = self._normalize_censor_text(text)
        if not normalized:
            return True, 0, []
        entries = censor.get(platform_scope, {})
        matches = [(entry["level"], entry["word"]) for key, entry in entries.items() if entry["level"] > 0 and key in normalized]
        if not matches:
            return True, 0, []
        return True, max(level for level, _ in matches), [word for _, word in matches[:5]]

    def _admin_censor(self, args: str, is_admin: bool, is_private: bool, platform_scope: str) -> str | NoticeCommandReply:
        if not is_private or not is_admin:
            return "敏感词规则仅允许 AstrBot 全局管理员在私聊中管理。"
        if not platform_scope or len(platform_scope) > 512:
            return "无法确认当前平台/bot 身份，敏感词操作已拒绝。"
        try:
            identity = json.loads(platform_scope)
        except json.JSONDecodeError:
            identity = None
        if not isinstance(identity, list) or len(identity) != 3 or not all(isinstance(value, str) and value.strip() and len(value) <= 128 for value in identity) or platform_scope != json.dumps(identity, ensure_ascii=False, separators=(",", ":")):
            return "无法确认当前平台/bot 身份，敏感词操作已拒绝。"
        current = self.store.snapshot().get("censor", {})
        if not self._valid_censor_map(current):
            return "敏感词规则数据损坏；为避免静默覆盖，管理操作已拒绝。"
        raw = args.strip()
        if len(raw) > 512 or not raw or raw[0] not in "+-":
            return "用法：.admin censor +Warning:词1,词2（可选 Ignore/Notice/Caution/Warning/Danger/Critical）；.admin censor -词1,词2。仅全局管理员私聊；最多 256 词/平台 bot。"
        operation = raw[0]
        payload = raw[1:].strip()
        level = 3
        if operation == "+":
            level_name, separator, payload = payload.partition(":")
            if not separator:
                return "添加格式：.admin censor +Warning:词1,词2。"
            level = {"ignore": 0, "notice": 1, "caution": 2, "warning": 3, "danger": 4, "critical": 5}.get(level_name.strip().casefold(), 3)
        words = [item.strip() for item in re.split(r"[,，;；]+", payload) if item.strip()]
        if not words or len(words) > 8:
            return "每次需提供 1–8 个逗号分隔的敏感词。"
        normalized_words: list[tuple[str, str]] = []
        seen: set[str] = set()
        for word in words:
            key = self._normalize_censor_text(word)
            if not key or len(word) > 64 or len(key) > 128 or any(ord(char) < 32 or ord(char) == 127 for char in word):
                return "敏感词不能为空，且每词最多 64 字符；不接受控制字符。"
            if key not in seen:
                normalized_words.append((key, word))
                seen.add(key)
        try:
            def update(state: dict) -> int:
                censor = state.get("censor")
                if not self._valid_censor_map(censor):
                    raise ValueError("敏感词规则数据损坏")
                entries = censor.get(platform_scope)
                if entries is None:
                    if len(censor) >= 100:
                        raise ValueError("平台/bot 范围已达 100 个上限")
                    entries = {}
                    censor[platform_scope] = entries
                if operation == "+" and len(set(entries) | {key for key, _ in normalized_words}) > 256:
                    raise ValueError("当前平台/bot 的敏感词达到 256 个上限")
                changed = 0
                for key, word in normalized_words:
                    if operation == "+":
                        value = {"word": word, "level": level}
                        if entries.get(key) != value:
                            entries[key] = value
                            changed += 1
                    elif key in entries:
                        entries.pop(key)
                        changed += 1
                if operation == "-" and not entries:
                    censor.pop(platform_scope, None)
                return changed
            changed = self.store.transact(update)
        except ValueError as exc:
            return f"敏感词规则未修改：{exc}。"
        if not changed:
            return "没有规则发生变化。"
        action = "添加/更新" if operation == "+" else "移除"
        level_name = ("Ignore", "Notice", "Caution", "Warning", "Danger", "Critical")[level]
        suffix = f"，级别 {level_name}" if operation == "+" else ""
        return NoticeCommandReply(f"已{action}敏感词规则 {changed} 条{suffix}。只过滤带配置前缀的 Dice 命令。", 1)

    def _admin_command(self, args: str, group_id: str, sender_id: str, is_admin: bool, is_private: bool, platform_scope: str, platform_id: str) -> str | NoticeCommandReply:
        parts = args.split()
        if parts and parts[0].casefold() in {"on", "off"}:
            if len(parts) != 1:
                return "用法：.admin on/off（不接受额外参数）。"
            return self._system_command(parts[0].casefold(), is_admin, is_private)
        if parts and parts[0].casefold() == "censor":
            return self._admin_censor(" ".join(parts[1:]), is_admin, is_private, platform_scope)
        if not parts or parts[0].casefold() != "notice":
            return "本插件仅实现 .admin notice 与受限 .admin censor 子集；其他上游管理接口依赖 Dice 主控/驱动，未移植。"
        return self._admin_notice(" ".join(parts[1:]), group_id, sender_id, is_admin, is_private, platform_scope, platform_id)

    def _admin_notice(self, args: str, group_id: str, sender_id: str, is_admin: bool, is_private: bool, platform_scope: str, platform_id: str) -> str | NoticeCommandReply:
        if not is_admin:
            return "通知窗口管理需要当前群管理员；私聊需 AstrBot 全局管理员。"
        if not platform_id or len(platform_id) > 128 or ":" in platform_id or not platform_scope or len(platform_scope) > 512:
            return "无法安全确认当前 AstrBot 平台/bot 身份，通知窗口操作已拒绝。"
        try:
            identity = json.loads(platform_scope)
        except json.JSONDecodeError:
            identity = None
        if not isinstance(identity, list) or len(identity) != 3 or not all(isinstance(value, str) and value.strip() for value in identity) or identity[1] != platform_id:
            return "平台/bot 身份不完整，通知窗口操作已拒绝。"
        if is_private:
            target_type = "private"
            target_id = self._raw_platform_user_id(sender_id)
        else:
            prefix = platform_scope + ":"
            if not group_id.startswith(prefix):
                return "当前群缺少平台隔离信息，通知窗口操作已拒绝。"
            target_type = "group"
            target_id = group_id[len(prefix):]
        if not target_id or not target_id.isascii() or not target_id.isdecimal() or len(target_id) > 20:
            return "通知窗口仅支持当前平台的规范数字群号/用户号。"
        numeric_id = int(target_id)
        if numeric_id <= 0 or numeric_id > 9_223_372_036_854_775_807 or str(numeric_id) != target_id:
            return "通知窗口目标不是规范数字 ID，操作已拒绝。"

        parts = args.split()
        remove = False
        if parts and parts[0] in {"+", "-"}:
            sign = parts.pop(0)
            if not parts or parts.pop(0).casefold() != "this":
                return "为防止跨会话误操作，本地仅支持目标 this。"
            if sign == "-":
                if parts:
                    return "用法：.admin notice [-] this [0-63|+N -N]。"
                remove = True
        elif parts and parts[0].casefold() in {"+this", "-this"}:
            sign = parts.pop(0)[0]
            if sign == "-":
                if parts:
                    return "用法：.admin notice [-] this [0-63|+N -N]。"
                remove = True
        elif parts and parts[0].casefold() == "this":
            parts.pop(0)
        elif parts and parts[0].casefold() == "list":
            if len(parts) != 1:
                return "用法：.admin notice list。"
            all_notices = self.store.snapshot().get("notices")
            if not self._valid_notice_map(all_notices):
                return "通知窗口数据损坏或超过 100 项；为避免泄露/覆盖，清单读取已拒绝。"
            rows = []
            for _, entry in sorted(all_notices.items()):
                if not is_private and (entry["platform_scope"] != platform_scope or entry["target_type"] != target_type or entry["target_id"] != target_id):
                    continue
                label = ""
                if is_private:
                    identity = json.loads(entry["platform_scope"])
                    scope_label = ["".join(char for char in value if ord(char) >= 32 and not 0x7F <= ord(char) <= 0x9F)[:24] for value in identity[1:]]
                    label = "/".join(scope_label) + " "
                rows.append(f"{label}{entry['target_type']}:{entry['target_id']} level={entry['level']}")
            shown = rows[:24]
            header = f"{'所有本地 platform/bot 范围' if is_private else '当前会话'}通知订阅共 {len(rows)} 条"
            if not shown:
                return header + "：无。"
            suffix = f"\n…另有 {len(rows) - len(shown)} 条未显示。" if len(rows) > len(shown) else ""
            return header + "：\n" + "\n".join(shown) + suffix
        elif parts and parts[0].casefold() == "status":
            parts.pop(0)
        if len(parts) > 12:
            return "通知级别参数过多。"

        snapshot = self.store.snapshot()
        notices = snapshot.get("notices")
        if not self._valid_notice_map(notices):
            return "通知窗口数据损坏或超过 100 项；为避免覆盖数据，操作已拒绝。"
        key = self._notice_key(platform_scope, target_type, target_id)
        current = notices.get(key)
        current_level = current["level"] if isinstance(current, dict) else 0
        status_text = f"当前会话通知级别：{current_level}（0–63 位掩码；0 表示未订阅）。"
        if not parts and not remove:
            return status_text
        new_level = 0
        bit_operations = None
        if remove or (len(parts) == 1 and parts[0].casefold() == "off"):
            new_level = 0
        elif len(parts) == 1 and parts[0].casefold() == "on":
            new_level = 63
        elif len(parts) == 1 and parts[0].isascii() and parts[0].isdecimal():
            new_level = int(parts[0])
            if new_level > 63:
                return "通知级别须为 0–63。"
        elif len(parts) == 2 and parts[0].casefold() == "set" and parts[1].isascii() and parts[1].isdecimal():
            new_level = int(parts[1])
            if new_level > 63:
                return "通知级别须为 0–63。"
        else:
            operations: list[tuple[str, int]] = []
            index = 0
            while index < len(parts):
                token = parts[index]
                if token in {"+", "-"}:
                    sign = token
                    index += 1
                    if index >= len(parts):
                        return "通知级别位须为 0–5。"
                    bit_text = parts[index]
                elif len(token) == 2 and token[0] in {"+", "-"}:
                    sign, bit_text = token[0], token[1]
                else:
                    return "用法：.admin notice [this] [0-63|+N -N|off]。"
                if not bit_text.isascii() or not bit_text.isdecimal() or len(bit_text) != 1 or int(bit_text) > 5:
                    return "通知级别位须为 0–5。"
                operations.append((sign, int(bit_text)))
                index += 1
            bit_operations = operations
            new_level = current_level
            for sign, bit in operations:
                if sign == "+":
                    new_level |= 1 << bit
                else:
                    new_level &= ~(1 << bit)

        try:
            def update(state: dict):
                entries = state.get("notices")
                if not self._valid_notice_map(entries):
                    raise ValueError("通知窗口数据损坏或超过 100 项；为避免覆盖数据，操作已拒绝。")
                previous = entries.get(key)
                old_level = previous["level"] if isinstance(previous, dict) else 0
                final_level = 0 if remove else new_level
                if bit_operations is not None:
                    final_level = old_level
                    for operation, bit in bit_operations:
                        if operation == "+":
                            final_level |= 1 << bit
                        else:
                            final_level &= ~(1 << bit)
                if final_level == old_level:
                    return old_level, final_level, False
                if final_level == 0:
                    entries.pop(key, None)
                else:
                    record = {"platform_scope": platform_scope, "platform_id": platform_id, "target_type": target_type, "target_id": target_id, "level": final_level}
                    if not self._valid_notice_record(key, record):
                        raise ValueError("当前通知窗口记录不符合安全格式，操作已拒绝。")
                    entries[key] = record
                if len(entries) > 100:
                    raise ValueError("最多保存 100 个通知窗口。")
                return old_level, final_level, True
            old_level, final_level, changed = self.store.transact(update)
        except ValueError as exc:
            return str(exc)
        if not changed:
            return status_text
        if final_level:
            message = f"当前会话通知级别已设为 {final_level}（二进制 {final_level:06b}）。"
        else:
            message = "当前会话通知窗口已关闭。"
        return NoticeCommandReply(message, 1)

    async def _send_admin_notice(self, notice: NoticeCommandReply, raw_sender_id: str, raw_group_id: str, platform_scope: str, platform_id: str) -> None:
        if type(notice.notice_level) is not int or not 1 <= notice.notice_level <= 63 or not platform_scope or not platform_id or ":" in platform_id:
            return
        try:
            identity = json.loads(platform_scope)
        except json.JSONDecodeError:
            return
        if not isinstance(identity, list) or len(identity) != 3 or not all(isinstance(value, str) and value.strip() for value in identity) or identity[1] != platform_id:
            return
        notices = self.store.snapshot().get("notices")
        if not self._valid_notice_map(notices):
            self.logger.warning("Dice admin notice store is malformed; fanout skipped")
            return
        targets = []
        for key in sorted(notices):
            entry = notices[key]
            if entry["platform_scope"] != platform_scope or entry["platform_id"] != platform_id or not (entry["level"] & notice.notice_level):
                continue
            if entry["target_type"] == "group" and raw_group_id and entry["target_id"] == raw_group_id:
                continue
            if entry["target_type"] == "private" and entry["target_id"] == raw_sender_id:
                continue
            if entry["target_type"] == "group":
                target_umo = f"{platform_id}:GroupMessage:{entry['target_id']}"
            else:
                target_umo = f"{platform_id}:FriendMessage:{entry['target_id']}"
            targets.append(target_umo)
            if len(targets) >= 50:
                break
        send_message = getattr(self.context, "send_message", None)
        if not callable(send_message) or not targets:
            return
        semaphore = asyncio.Semaphore(10)
        notice_text = notice.broadcast_text if notice.broadcast_text is not None else notice.text
        payload = f"{ADMIN_NOTICE_PREFIX}{notice_text}"[:self.max_response_length]
        async def send_one(target_umo: str) -> None:
            async with semaphore:
                try:
                    sent = await asyncio.wait_for(send_message(target_umo, [Plain(payload)]), timeout=5)
                    if sent is False:
                        self.logger.warning("Dice admin notice target adapter did not accept %s", target_umo.split(":", 2)[-1])
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.logger.exception("Dice admin notice send failed")
        await asyncio.gather(*(send_one(target) for target in targets))

    def _system_command(self, args: str, is_admin: bool, is_private: bool) -> str | NoticeCommandReply:
        if not is_private:
            return ".system 仅允许在私聊中管理。"
        if not is_admin:
            return ".system 需要 AstrBot 全局管理员身份。"
        parts = args.split()
        if not parts:
            return "用法：.system state|on|off|clock [list|+on HH:MM|-on HH:MM|+off HH:MM|-off HH:MM]"
        verb = parts[0].casefold()
        system = self._system_state_snapshot()
        if system is None:
            return "插件系统/定时状态格式无效；未修改数据。"
        if verb in {"state", "status"} and len(parts) == 1:
            state_text = "启用" if system.get("enabled", True) else "全局静默（仅本插件）"
            return f"Dice-AstrBot 状态：{state_text}；定时任务 {len(system.get('clocks', []))}/100。"
        if verb in {"on", "off"} and len(parts) == 1:
            enabled = verb == "on"
            def set_system_enabled(current: dict):
                changed = current.get("enabled", True) != enabled
                current["enabled"] = enabled
                return changed
            changed = self._mutate_system(set_system_enabled)
            message = "Dice-AstrBot 已启用。" if enabled else "Dice-AstrBot 已静默；私聊管理员的 .system 管理入口仍可用。"
            return NoticeCommandReply(message, 3) if changed else message
        if verb != "clock":
            return "仅支持本地 .system state/on/off/clock；备份、过期清理与扩展任务未移植。"
        if len(parts) == 1 or (len(parts) == 2 and parts[1].casefold() == "list"):
            clocks = system.get("clocks", [])
            if not clocks:
                return "暂无定时任务（使用宿主本地时区）。"
            lines = ["定时任务（宿主本地时区，每日执行）："]
            lines.extend(f"{entry['time']} {entry['action']}" for entry in clocks)
            return "\n".join(lines)

        sign = ""
        action = ""
        clock_text = ""
        if len(parts) == 4 and parts[1] in {"+", "-"}:
            sign, action, clock_text = parts[1], parts[2].casefold(), parts[3]
        elif len(parts) == 3 and parts[1][:1] in {"+", "-"}:
            sign, action, clock_text = parts[1][0], parts[1][1:].casefold(), parts[2]
        if action in {"save", "clear"}:
            return f"定时 {action} 依赖上游备份/数据清理接口，本插件不执行该动作。"
        if sign not in {"+", "-"} or action not in {"on", "off"}:
            return "本地只支持 .system clock [+|-]on/off HH:MM；save/clear/扩展任务不支持。"
        if re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", clock_text) is None:
            return "时间须为宿主本地 HH:MM（00:00–23:59）。"

        if sign == "+":
            clock_id = secrets.token_hex(8)
            def add_clock(current: dict):
                clocks = current["clocks"]
                if len(clocks) >= 100:
                    raise ValueError("最多保存 100 个定时任务。")
                if any(entry["time"] == clock_text for entry in clocks):
                    raise ValueError("同一分钟只允许一个 on/off 动作，请先删除已有任务。")
                clocks.append({"id": clock_id, "action": action, "time": clock_text})
                return True
            try:
                self._mutate_system(add_clock)
            except ValueError as exc:
                return str(exc)
            message = f"已添加每日 {clock_text} {action} 定时任务（宿主本地时区）。"
            return NoticeCommandReply(message, 2)

        def remove_clock(current: dict):
            clocks = current["clocks"]
            for index, entry in enumerate(clocks):
                if entry["action"] == action and entry["time"] == clock_text:
                    clocks.pop(index)
                    return True
            return False
        try:
            removed = self._mutate_system(remove_clock)
        except ValueError as exc:
            return str(exc)
        if removed:
            return NoticeCommandReply(f"已删除 {clock_text} {action} 定时任务。", 2)
        return "未找到对应定时任务。"

    @staticmethod
    def _group_platform_scope(group_id: str) -> str:
        if not isinstance(group_id, str):
            return ""
        try:
            identity, end = json.JSONDecoder().raw_decode(group_id)
        except (TypeError, json.JSONDecodeError):
            return ""
        if not isinstance(identity, list) or not 1 <= len(identity) <= 3 or end >= len(group_id) or group_id[end] != ":":
            return ""
        if any(not isinstance(value, str) or not value.strip() for value in identity):
            return ""
        scope = group_id[:end]
        canonical = json.dumps(identity, ensure_ascii=False, separators=(",", ":"))
        return scope if scope == canonical else ""

    def _group_for_state(self, state: dict, group_id: str) -> dict:
        groups = state.setdefault("groups", {})
        group = groups.get(group_id)
        if not isinstance(group, dict):
            group = self._default_group()
            groups[group_id] = group
        for key, value in self._default_group().items():
            group.setdefault(key, value.copy() if isinstance(value, (dict, list)) else value)
        return self._normalize_group_state(group)

    @staticmethod
    def _shared_session_room(state: dict, group_id: str, group: dict) -> dict | None:
        reference = group.get("session_ref")
        if not isinstance(reference, str) or re.fullmatch(r"[0-9a-f]{32}", reference) is None:
            return None
        scope = Main._group_platform_scope(group_id)
        rooms = state.get("shared_sessions")
        if not scope or not isinstance(rooms, dict):
            return None
        room = rooms.get(reference)
        if not isinstance(room, dict) or room.get("scope") != scope or not isinstance(room.get("session"), dict):
            return None
        return room

    def _group_snapshot(self, group_id: str) -> dict:
        state = self.store.snapshot()
        result = self._default_group()
        stored = state.get("groups", {}).get(group_id)
        if isinstance(stored, dict):
            result.update(stored)
        reference = result.get("session_ref")
        room = self._shared_session_room(state, group_id, result)
        if isinstance(room, dict):
            result["session"] = room["session"]
        elif reference is not None:
            result["session_ref"] = None
            result["session"] = None
        return self._normalize_group_state(result)

    def _mutate_group(self, group_id: str, mutation):
        def apply(state: dict):
            group = self._group_for_state(state, group_id)
            reference = group.get("session_ref")
            room = self._shared_session_room(state, group_id, group) if reference is not None else None
            shared_session = room.get("session") if isinstance(room, dict) else None
            if isinstance(shared_session, dict):
                group["session"] = shared_session
            elif reference is not None:
                reference = None
                group["session_ref"] = None
                group["session"] = None
            result = mutation(group)
            if reference is not None and group.get("session_ref") == reference:
                if group.get("session") is shared_session:
                    group.pop("session", None)
                else:
                    group["session_ref"] = None
            return result
        return self.store.transact(apply)

    def _command_from(self, text: str) -> tuple[str, str] | None:
        for prefix in self.prefixes:
            if text.startswith(prefix):
                payload = text[len(prefix):].strip()
                if not payload:
                    return None
                name, separator, args = payload.partition(" ")
                name = name.casefold()
                if name in self.command_names:
                    return name, args.strip() if separator else ""
                roll_form = re.match(r"(?i)^(rsh|rh|rs|r|hs|h)(?=[0-9dDbBpPfF+*/()#\-\u3400-\u9fff]|$)(.*)$", payload)
                if roll_form:
                    return roll_form.group(1).casefold(), roll_form.group(2).strip()
                pool_form = re.match(r"(?i)^(wwh|wh|ww|w)(?=[0-9dD+\-#\u3400-\u9fff])(.*)$", payload)
                if pool_form:
                    return pool_form.group(1).casefold(), pool_form.group(2).strip()
                if re.match(r"(?i)^ak(?=$|\s|[#+=-])", payload):
                    return "ak", payload[2:].strip()
                hidden_form = re.match(r"(?i)^(ra|rc)(?:(h)(?=\s)|(_)|([bp]\d*(?=[_\s:：]|$|[\u3400-\u9fff]))|(\d+#))", payload)
                if hidden_form:
                    command = hidden_form.group(1).casefold()
                    tail = payload[len(command):]
                    if hidden_form.group(2):
                        tail = "_" + tail[1:].lstrip()
                    return command, tail.strip()
                return None
        return None

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        notice = self._onebot_notice_payload(event)
        if notice is not None:
            reply = self._onebot_lifecycle_reply(event, notice)
            if reply is not None:
                yield event.plain_result(reply)
            return
        raw = (event.get_message_str() or "").strip()
        if len(raw) > self.max_message_length:
            return
        if raw.startswith("[Dice-AstrBot-AdminNotice]"):
            return
        parsed = self._command_from(raw)
        group_id = self._group_id(event)
        raw_sender_id = self._sender_id(event)
        sender_id = self._player_id(event)
        platform_scope = self._platform_scope(event)
        platform_id = ""
        try:
            raw_platform_id = event.get_platform_id()
            platform_id = str(raw_platform_id).strip() if raw_platform_id else ""
        except Exception:
            pass
        bot_id = ""
        try:
            raw_bot_id = event.get_self_id()
            bot_id = str(raw_bot_id).strip() if raw_bot_id else ""
        except Exception:
            pass
        try:
            is_private = bool(event.is_private_chat())
        except Exception:
            is_private = False
        bot_action = ""
        if parsed is not None and parsed[0] == "bot":
            bot_parts = parsed[1].split(maxsplit=1)
            bot_action = bot_parts[0].casefold() if bot_parts else ""
        hidden_draw = bool(parsed is not None and parsed[0] == "draw" and re.match(r"(?:_|h(?:\s|$))", parsed[1].lstrip(), re.IGNORECASE))
        pc_args = parsed[1].split(maxsplit=1) if parsed is not None and parsed[0] in {"pc", "card"} else []
        card_json_export = bool(pc_args and pc_args[0].casefold() == "tojson")
        raw_group_id = ""
        if not is_private:
            try:
                raw_group_value = event.get_group_id()
                raw_group_id = str(raw_group_value).strip() if raw_group_value is not None else ""
            except Exception:
                pass
        admin_action = parsed[1].split()[0].casefold() if parsed is not None and parsed[0] == "admin" and parsed[1].split() else ""
        group_args = parsed[1].strip().split(maxsplit=1) if parsed is not None and parsed[0] == "group" else []
        group_all_action = bool(group_args and group_args[0].casefold() == "all")
        group_local_action = bool(group_args and group_args[0].casefold() in {"status", "state", "on", "off"})
        system_admin = bool(parsed is not None and is_private and self._is_platform_admin(event) and (parsed[0] == "system" or (parsed[0] == "admin" and admin_action in {"on", "off"}) or group_all_action))
        if not self._system_enabled() and not system_admin:
            return
        group = self._group_snapshot(group_id)
        banned = set(map(str, group.get("banned_users", [])))
        if raw_sender_id in banned:
            if parsed is None or not self._is_admin(event):
                if parsed is not None:
                    event.stop_event()
                    yield event.plain_result("你已被本群 Dice 插件禁用。")
                return
        if not group.get("enabled", True) and not (system_admin or (not is_private and parsed is not None and parsed[0] == "bot" and bot_action in {"on", "off", "status"}) or (not is_private and parsed is not None and parsed[0] == "group" and group_local_action)):
            return
        if parsed is not None and parsed[0] not in {"admin", "system", "bot"}:
            censor_valid, censor_level, censor_hits = self._censor_matches(raw, platform_scope)
            if not censor_valid:
                self.logger.error("Dice censor store is malformed; plugin command blocked")
                event.stop_event()
                yield event.plain_result("本地敏感词规则数据异常，Dice 命令已停止处理。")
                return
            if censor_level == 1:
                self.logger.info("Dice censor notice matched %d rule(s)", len(censor_hits))
            elif censor_level >= 2:
                self.logger.info("Dice censor blocked a command at severity %d", censor_level)
                event.stop_event()
                yield event.plain_result("消息命中本地敏感词规则，命令已拒绝处理。")
                return
        if parsed is None or (parsed[0] not in {"log", "helpdoc", "user", "group", "master", "mod", "str", "authorize", "dismiss", "groups", "4kick", "chan", "welcome", "warning", "send", "cloud"} and not group_all_action and not hidden_draw and not card_json_export):
            self._append_session_log(group_id, sender_id, raw_sender_id, "in", raw)
        if parsed is None:
            await self._relay_link_message(group_id, raw, platform_id, is_private=is_private, speaker=raw_sender_id)
            try:
                reply_sender_name = event.get_sender_name()
            except Exception:
                reply_sender_name = ""
            if not isinstance(reply_sender_name, str):
                reply_sender_name = str(reply_sender_name) if reply_sender_name is not None else ""
            response, reply_locks = await self._match_custom_reply(
                raw, group, raw_sender_id, raw_group_id, group_scope=group_id, user_scope=sender_id,
                usage_check=lambda field, trigger, limits: self._consume_custom_reply_usage(group_id, field, trigger, limits, user_scope=sender_id),
                sender_name=reply_sender_name,
            )
            if response:
                try:
                    event.stop_event()
                    replies = response if isinstance(response, list) else [response]
                    for reply_text in replies:
                        reply_text = str(reply_text)[: self.max_response_length]
                        self._append_session_log(group_id, sender_id, raw_sender_id, "out", reply_text)
                        await self._relay_link_message(group_id, reply_text, platform_id, is_private=is_private, speaker="Dice-AstrBot", from_bot=True)
                        yield event.plain_result(reply_text)
                finally:
                    if reply_locks:
                        self._release_custom_reply_locks(reply_locks)
            elif reply_locks:
                self._release_custom_reply_locks(reply_locks)
            return
        command, args = parsed
        if command == "send":
            is_admin = bool(is_private and self._is_platform_admin(event))
        else:
            is_admin = system_admin if command == "system" or group_all_action else (self._is_platform_admin(event) if command == "helpdoc" or (command == "admin" and is_private) else self._is_admin(event))
        relay_input = command not in {"link", "admin", "master", "system", "helpdoc", "user", "group", "reply", "re", "rh", "rsh", "h", "hs", "wh", "wwh", "mod", "str", "authorize", "dismiss", "groups", "4kick", "chan", "welcome", "warning", "send", "cloud"} and not group_all_action and not hidden_draw and not card_json_export
        if command in {"ra", "rc"}:
            try:
                relay_input = not self._parse_check_spec(args, sender_id, group_id)[-1]
            except Exception:
                relay_input = False
        if relay_input:
            await self._relay_link_message(group_id, raw, platform_id, is_private=is_private, speaker=raw_sender_id)
        event.stop_event()
        mention_ids: list[str] = []
        try:
            reply = self._dispatch(command, args, group_id, sender_id, is_admin, is_private, mention_ids=mention_ids, platform_scope=platform_scope, platform_id=platform_id, bot_id=bot_id, raw_group_id=raw_group_id, group_info=getattr(getattr(event, "message_obj", None), "group", None))
        except DicePoolError as exc:
            mention_ids.clear()
            reply = f"骰池表达式错误：{exc}"
        except DiceError as exc:
            mention_ids.clear()
            reply = f"骰子表达式错误：{exc}"
        except (ValueError, KeyError, IndexError) as exc:
            mention_ids.clear()
            reply = f"参数错误：{exc}"
        except Exception:
            mention_ids.clear()
            self.logger.exception("Dice command failed: %s", command)
            reply = "命令执行失败；错误已写入 AstrBot 日志，请检查插件状态。"
        if isinstance(reply, HiddenCheckReply):
            if is_private:
                reply = reply.private_text
            else:
                await self._send_hidden_check(event, reply, group_id, sender_id)
                reply = reply.public_ack
        if isinstance(reply, NoticeCommandReply):
            await self._send_admin_notice(reply, raw_sender_id, raw_group_id, platform_scope, platform_id)
            reply = reply.text
        if isinstance(reply, GroupMessageReply):
            try:
                send_message = getattr(self.context, "send_message", None)
                sent = await send_message(reply.target_umo, [Plain(reply.text)]) if callable(send_message) else False
                reply = "已将角色扮演消息提交给目标群适配器。" if sent is True else "未找到可发送的当前平台实例；目标群消息未发送。"
            except Exception:
                self.logger.exception("Dice .me proactive group send failed")
                reply = "目标群消息发送失败；请检查 AstrBot 日志与适配器支持。"
        if reply:
            if command not in {"log", "helpdoc", "user", "group", "master", "mod", "str", "authorize", "dismiss", "groups", "4kick", "chan", "welcome", "warning", "send", "cloud"} and not group_all_action and not card_json_export:
                self._append_session_log(group_id, sender_id, raw_sender_id, "out", str(reply))
            if command not in {"link", "log", "admin", "master", "system", "helpdoc", "user", "group", "reply", "re", "mod", "str", "authorize", "dismiss", "groups", "4kick", "chan", "welcome", "warning", "send", "cloud"} and not group_all_action and not card_json_export:
                await self._relay_link_message(group_id, str(reply)[: self.max_response_length], platform_id, is_private=is_private, speaker="Dice-AstrBot", from_bot=True)
            if mention_ids:
                chain_result = getattr(event, "chain_result", None)
                if callable(chain_result):
                    chain = [Plain("团务玩家：")]
                    for player_id in mention_ids:
                        chain.extend((At(qq=self._raw_platform_user_id(player_id)), Plain(" ")))
                    yield chain_result(chain)
                    return
            yield event.plain_result(reply[: self.max_response_length])

    def _send_notice_command(self, args: str, is_admin: bool, is_private: bool, platform_scope: str, platform_id: str) -> str | NoticeCommandReply:
        if not is_private:
            return ".send notice 仅允许私聊平台全局管理员使用；任意目标发送未移植。"
        if not is_admin:
            return ".send notice 需要 AstrBot 平台全局管理员身份。"
        raw = args.strip()
        if len(raw) > MAX_SEND_NOTICE_TEXT_LENGTH + 64:
            return "通知命令总长度超限。"
        if any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in raw):
            return "通知只支持单行纯文本。"
        match = re.fullmatch(r"notice[ ]*([0-5](?:[ ]*\+[ ]*[0-5])*)[ ]+(.+)", raw, flags=re.IGNORECASE)
        if not match:
            return "用法：.send notice0+2 <单行文本>；等级仅支持 0–5，任意目标发送未移植。"
        text = match.group(2).strip()
        if not text:
            return "通知文本不能为空。"
        max_text_length = min(MAX_SEND_NOTICE_TEXT_LENGTH, max(0, self.max_response_length - len(ADMIN_NOTICE_PREFIX)))
        if len(text) > max_text_length:
            return f"通知文本超过当前单条消息上限（最多 {max_text_length} 字）。"
        if not platform_scope or len(platform_scope) > 512 or not platform_id or len(platform_id) > 128 or ":" in platform_id:
            return "缺少有效的平台/bot 身份，通知未提交。"
        try:
            identity = json.loads(platform_scope)
        except (TypeError, json.JSONDecodeError):
            return "缺少有效的平台/bot 身份，通知未提交。"
        if not isinstance(identity, list) or len(identity) != 3 or not all(isinstance(value, str) and value.strip() for value in identity) or identity[1] != platform_id:
            return "缺少有效的平台/bot 身份，通知未提交。"
        if not callable(getattr(self.context, "send_message", None)):
            return "当前宿主不支持跨会话发送，通知未提交。"
        notice_level = 0
        for level in re.split(r"[ ]*\+[ ]*", match.group(1)):
            notice_level |= 1 << int(level)
        return NoticeCommandReply("通知请求已接收；仅发送给同平台/bot 的匹配订阅者，实际送达由适配器决定。", notice_level, text)

    def _dispatch(self, command: str, args: str, group_id: str, sender_id: str, is_admin: bool, is_private: bool = False, mention_ids: list[str] | None = None, platform_scope: str = "", platform_id: str = "", bot_id: str = "", raw_group_id: str = "", group_info: object | None = None) -> str | HiddenCheckReply | GroupMessageReply | NoticeCommandReply:
        if command == "master":
            return "上游 .master 涉及初始化口令、Dice 主控与信任等级；本插件不实现认主，且不会记录或转发该命令。"
        if command == "mod":
            return "上游 .mod 模组操作依赖 C++ 扩展宿主；本插件不安装、更新、重载或删除模组，也不代理该操作。"
        if command == "str":
            return "上游 .str 操作依赖 Dice trust 与全局 Formatter 字符串表；本插件没有等价存储/权限模型，操作已拒绝。"
        if command == "welcome":
            return self._welcome(args, group_id, is_admin, is_private)
        if command in {"authorize", "dismiss", "groups", "4kick", "chan", "warning"}:
            reasons = {
                "authorize": "群许可证状态与 GroupAuthorize hook",
                "dismiss": "连接器退群 API",
                "groups": "Dice 驱动群缓存与过期清理策略",
                "4kick": "连接器踢人/禁言管理 API",
                "chan": "驱动频道订阅/管理 API",
                "warning": "BlackListManager warningHandler、BlackInfoWarned hook 与全局 blacklist.verify",
            }
            return f"上游 .{command} 依赖{reasons[command]}；本插件没有等价宿主能力，操作已拒绝且不会代理。"
        if command == "send":
            return self._send_notice_command(args, is_admin, is_private, platform_scope, platform_id)
        if command == "cloud":
            return "上游 .cloud update/black 依赖 Dice 云服务与授权状态；本插件无等价宿主能力，操作已拒绝且不会联网。"
        if command == "re":
            parts = args.strip().split(maxsplit=1)
            if not parts:
                return "用法：.re <正则模式> <选项1|选项2|...>；省略选项删除该 Regex 回复。"
            pattern = parts[0]
            if len(parts) == 1 or not parts[1].strip():
                return self._reply_command(f"del regex {pattern}", group_id, is_admin, is_private)
            choices = parts[1].strip()
            if any(ord(char) < 32 or ord(char) == 127 for char in choices):
                return "Deck 回复不能包含控制字符。"
            return self._reply_command(f"set Regex:{pattern}\tDeck:{choices}", group_id, is_admin, is_private)
        if command in {"r", "roll", "rs", "rh", "rsh", "h", "hs"}:
            short = command in {"rs", "rsh", "hs"}
            hidden = command in {"rh", "rsh", "h", "hs"}
            return self._roll(args, group_id, sender_id, detailed=not short, hidden=hidden, is_private=is_private)
        if command in {"ra", "rc"}:
            return self._check(args, group_id, sender_id)
        if command in {"coc", "coc7", "cocs", "coc7s"}:
            return self._generate_stats_batch("coc7", args, group_id, sender_id)
        if command in {"coc6", "coc6s"}:
            return self._generate_stats_batch("coc6", args, group_id, sender_id)
        if command == "sc":
            return self._sanity(args, group_id, sender_id)
        if command == "en":
            return self._growth_check(args, group_id, sender_id)
        if command in {"w", "ww", "wh", "wwh"}:
            return self._dice_pool_command(args, group_id, sender_id, detailed=command in {"ww", "wwh"}, hidden=command in {"wh", "wwh"}, is_private=is_private)
        if command in {"li", "ti"}:
            return self._insanity(command, group_id, sender_id)
        if command in {"coc6d", "coc7d", "cocd"}:
            return self._generate_character("coc6d" if command == "coc6d" else "coc7d", args, sender_id, group_id)
        if command in {"pc", "card"}:
            return self._character_command(args, sender_id, group_id)
        if command == "st":
            return self._set_or_show_stats(args, sender_id, group_id)
        if command == "jrrp":
            return self._jrrp(args, group_id, sender_id, is_admin, is_private)
        if command == "me":
            return self._me(args, group_id, sender_id, is_admin, is_private, platform_scope, platform_id)
        if command == "link":
            return self._link_command(args, group_id, sender_id, is_admin, is_private, platform_scope, platform_id)
        if command == "game":
            return self._game(args, group_id, sender_id, is_admin, is_private, mention_ids, platform_scope)
        if command == "ob":
            return self._ob(args, group_id, sender_id, is_admin, is_private)
        if command == "init":
            return self._initiative(args, group_id)
        if command == "ri":
            return self._roll_initiative(args, group_id, sender_id, is_private)
        if command == "deck":
            return self._deck(args, group_id, sender_id, is_admin and not is_private)
        if command == "draw":
            if self._group_snapshot(group_id).get("draw_enabled", True) is not True:
                return "本群已停用 .draw。"
            return self._draw(args, group_id, is_private)
        if command == "ak":
            return self._ak(args, group_id)
        if command == "legacy":
            return self._legacy_rdconf_command(args, sender_id, is_private, platform_scope)
        if command == "nn":
            return self._nickname(args, group_id, sender_id)
        if command == "name":
            return self._random_name(args, group_id)
        if command == "nnn":
            return self._random_nickname(args, group_id, sender_id)
        if command == "dnd":
            return self._dnd(args, group_id, sender_id)
        if command == "d20":
            return self._d20(args, group_id, sender_id)
        if command == "set":
            return self._set_default_die(args, group_id, sender_id)
        if command == "setcoc":
            return self._set_rule(args, group_id, is_admin)
        if command == "rule":
            return self._rule_command(args, group_id, sender_id)
        if command == "log":
            return self._log_command(args, group_id, sender_id, is_admin, is_private)
        if command == "bot":
            return self._bot(args, group_id, is_admin, is_private, bot_id)
        if command == "ban":
            return self._ban(args, group_id, is_admin)
        if command == "group":
            return self._group_command(args, group_id, is_admin, is_private, platform_scope, sender_id, raw_group_id, group_info)
        if command == "reply":
            return self._reply_command(args, group_id, is_admin, is_private)
        if command == "system":
            return self._system_command(args, is_admin, is_private)
        if command == "admin":
            return self._admin_command(args, group_id, sender_id, is_admin, is_private, platform_scope, platform_id)
        if command == "user":
            return self._user_command(args, sender_id, group_id, is_private)
        if command == "version":
            return "Dice-AstrBot 0.2.0 · 原生 AstrBot 插件"
        if command == "helpdoc":
            return self._helpdoc_command(args, is_admin, is_private, platform_scope)
        if command == "help":
            return self._help_command(args, group_id, is_admin, is_private)
        return self._help()

    def _session_rng_for(self, group: dict, sender_id: str):
        session = group.get("session")
        if not isinstance(session, dict):
            return None
        members = self._session_member_ids(session, "game_masters", "game_master")
        members.update(self._session_member_ids(session, "participants"))
        if sender_id not in members or not isinstance(session.get("roulette"), dict):
            return None
        if not session["roulette"]:
            return None
        return _SessionRandomSource(session)

    @staticmethod
    def _card_attribute_value(attributes: object, key: str, default=None):
        if not isinstance(attributes, dict):
            return default
        if key in attributes:
            return attributes[key]
        wanted = key.casefold()
        matches = [value for stored_key, value in attributes.items() if isinstance(stored_key, str) and stored_key.casefold() == wanted]
        return matches[0] if len(matches) == 1 else default

    @staticmethod
    def _normalize_card_reference(name: str) -> str:
        raw = name.strip().upper()
        if raw in STAT_ALIASES:
            return STAT_ALIASES[raw]
        skill = SKILL_NAME_ALIASES.get(name.casefold())
        return skill if isinstance(skill, str) else Main._normalize_stat(name)

    def _card_expression_resolver(self, attributes: object, *, system: str | None = None) -> CardExpressionResolver:
        template: dict[str, object] = {}
        if isinstance(system, str) and system.casefold() == "coc7":
            template.update(COC7_VARIABLE_EXPRESSIONS)
            template.update({"&" + name: expression for name, expression in COC7_ROLL_EXPRESSIONS.items()})
            template.update(SKILL_DEFAULT_VALUES)
        custom = attributes if isinstance(attributes, dict) else {}
        custom_names = {
            self._normalize_card_reference(key[1:] if key.startswith("&") else key).casefold()
            for key in custom
            if isinstance(key, str)
        }
        for key in tuple(template):
            base = key[1:] if key.startswith("&") else key
            if self._normalize_card_reference(base).casefold() in custom_names:
                del template[key]
        template.update(custom)
        return CardExpressionResolver(template, self._normalize_card_reference)

    def _expand_pool_attributes(self, expression: str, attributes: object, *, system: str | None = None) -> str:
        if not isinstance(attributes, dict):
            return expression
        resolver = self._card_expression_resolver(attributes, system=system)
        result = expression
        for stored_name in attributes:
            if not isinstance(stored_name, str) or not stored_name.startswith("&") or len(stored_name) < 2:
                continue
            name = stored_name[1:]
            if len(name) > 64:
                continue
            pattern = r"(?<!\w)" + re.escape(name) + r"(?!\w)"
            if re.search(pattern, result, flags=re.IGNORECASE):
                formula = resolver.resolve_reference(name)
                result = re.sub(pattern, lambda _match: formula, result, flags=re.IGNORECASE)
        if isinstance(system, str) and system.casefold() == "coc7":
            template_names = set(COC7_VARIABLE_EXPRESSIONS) | set(COC7_ROLL_EXPRESSIONS) | set(SKILL_DEFAULT_VALUES)
            for name in sorted(template_names, key=lambda item: (-len(item), item.casefold())):
                pattern = r"(?<!\w)" + re.escape(name) + r"(?!\w)"
                if re.search(pattern, result, flags=re.IGNORECASE):
                    formula = resolver.resolve_reference(name)
                    result = re.sub(pattern, lambda _match: formula, result, flags=re.IGNORECASE)

        values: dict[str, int] = {}
        for name, raw in attributes.items():
            if not isinstance(name, str) or not name or name.startswith(("__", "&")):
                continue
            if type(raw) is int:
                value = raw
            elif isinstance(raw, str) and re.fullmatch(r"[+-]?\d{1,10}", raw.strip()):
                value = int(raw.strip())
            else:
                continue
            if abs(value) > 2_147_483_647:
                continue
            key = name.casefold()
            values.setdefault(key, value)
            canonical = Main._normalize_stat(name).casefold()
            values.setdefault(canonical, value)
            skill_canonical = SKILL_NAME_ALIASES.get(name.casefold())
            if isinstance(skill_canonical, str):
                values.setdefault(skill_canonical.casefold(), value)
        for alias, canonical in STAT_ALIASES.items():
            value = values.get(str(canonical).casefold())
            if value is not None:
                values.setdefault(str(alias).casefold(), value)
        for alias, canonical in SKILL_NAME_ALIASES.items():
            value = values.get(str(canonical).casefold())
            if value is not None:
                values.setdefault(str(alias).casefold(), value)
        for name, value in sorted(values.items(), key=lambda item: (-len(item[0]), item[0])):
            if len(name) > 64:
                continue
            pattern = r"(?<!\w)" + re.escape(name) + r"(?!\w)"
            result = re.sub(pattern, str(value), result, flags=re.IGNORECASE)
        return result

    def _pool_target(self, group_id: str, card: dict | None) -> int:
        group = self._group_snapshot(group_id)
        session = group.get("session")
        attributes = card.get("attributes") if isinstance(card, dict) else None
        raw = session.get("__DPTarget") if isinstance(session, dict) else None
        if raw is None:
            raw = self._card_attribute_value(attributes, "__DPTarget")
        try:
            target = int(raw) if not isinstance(raw, bool) else 8
        except (TypeError, ValueError):
            target = 8
        if not 1 <= target <= 10:
            raise ValueError("成功目标必须在 1 到 10 之间")
        return target

    def _dice_pool_command(self, args: str, group_id: str, sender_id: str, *, detailed: bool, hidden: bool, is_private: bool) -> str | HiddenCheckReply:
        text = args.strip()
        if not text:
            raise ValueError("用法：.w/.ww [次数#]骰池表达式 [理由]；.wh/.wwh 为群内隐藏投掷")
        expression, separator, reason = text.partition(" ")
        reason = reason.strip() if separator else ""
        card_name, card = self._active_card(sender_id, group_id)
        attributes = card.get("attributes") if isinstance(card, dict) else None
        attributes = attributes if isinstance(attributes, dict) else {}
        system = card.get("system", "coc7") if isinstance(card, dict) else None
        default_die = 10
        template_default_die = SKILL_DEFAULT_VALUES.get("__defaultdice", 10) if isinstance(system, str) and system.casefold() == "coc7" else 10
        raw_default_die = self._card_attribute_value(attributes, "__DefaultDice", template_default_die)
        try:
            configured_default = int(raw_default_die) if not isinstance(raw_default_die, bool) else 10
            if 1 <= configured_default <= 100_000:
                default_die = configured_default
        except (TypeError, ValueError):
            pass
        count = 1
        if "#" in expression:
            if expression.count("#") != 1:
                raise ValueError("批量骰池只允许一个 #")
            count_text, expression = expression.split("#", 1)
            count_text = count_text or "1"
            count_text = self._expand_pool_attributes(count_text, attributes, system=system)
            if count_text.isascii() and count_text.isdecimal():
                count = int(count_text)
            else:
                count = self.engine.roll(count_text, default_die).total
            if not 1 <= count <= 10:
                raise ValueError("投掷次数必须在 1 到 10 之间")
        if card is not None:
            expression = self._expand_pool_attributes(expression, attributes, system=system)
        if not expression:
            raise ValueError("骰池表达式不能为空")
        target = self._pool_target(group_id, card)
        remaining_rolls = MAX_TOTAL_POOL_ROLLS

        def command_randint(low: int, high: int) -> int:
            nonlocal remaining_rolls
            if remaining_rolls <= 0:
                raise DicePoolError(f"本次池骰命令累计投掷超过安全上限 {MAX_TOTAL_POOL_ROLLS:,}")
            remaining_rolls -= 1
            return secrets.randbelow(high - low + 1) + low

        dice_results = [
            roll_dice_pool(expression, target=target, randint=command_randint)
            for _ in range(count)
        ]
        if detailed or count == 1:
            body = "\n\n".join(
                (f"{index}. " if count > 1 else "") + result.format(detailed=detailed)
                for index, result in enumerate(dice_results, 1)
            )
        else:
            body = f"{expression}（目标 {target}，{count} 次）：{{ " + ", ".join(str(result.total) for result in dice_results) + " }"
        if reason:
            body += "\n理由：" + reason
        if hidden and not is_private:
            self._append_log(group_id, sender_id, "w [hidden]", "隐藏骰池已执行；结果未写入群日志。")
            return HiddenCheckReply(body)
        self._append_log(group_id, sender_id, "ww" if detailed else "w", body)
        return body

    def _user_default_die(self, sender_id: str) -> int:
        user = self.store.player(sender_id)
        raw = user.get("default_die") if isinstance(user, dict) else None
        if type(raw) is int and 1 <= raw <= 9999:
            return raw
        return self.default_die if type(self.default_die) is int and 1 <= self.default_die <= MAX_SIDES else 100

    def _card_default_die(self, attributes: object, *, fallback: int | None = None) -> int:
        fallback = fallback if type(fallback) is int and 1 <= fallback <= MAX_SIDES else self.default_die
        fallback = fallback if type(fallback) is int and 1 <= fallback <= MAX_SIDES else 100
        if not isinstance(attributes, dict):
            return fallback
        raw = self._card_attribute_value(attributes, "__DefaultDice")
        try:
            value = raw if type(raw) is int else int(raw) if isinstance(raw, str) and raw.strip().isascii() and raw.strip().isdecimal() else None
        except (TypeError, ValueError):
            value = None
        return value if type(value) is int and 1 <= value <= MAX_SIDES else fallback

    def _set_default_die(self, args: str, group_id: str, sender_id: str) -> str:
        raw = args.strip()
        if raw:
            if not re.fullmatch(r"[0-9]+", raw):
                raise ValueError("用法：.set [默认骰面数 1-9999]")
            normalized = raw.lstrip("0") or "0"
            if len(normalized) > 4:
                raise ValueError("默认骰面数最多 4 位，范围为 1 到 9999")
            value = int(normalized)
            if not 1 <= value <= 9999:
                raise ValueError("默认骰面数范围为 1 到 9999")
        else:
            value = 100

        active_name, active_card = self._active_card(sender_id, group_id)
        if not isinstance(active_card, dict):
            active_name = None

        def save_default(state: dict) -> tuple[str, str | int]:
            players = state.get("players")
            if not isinstance(players, dict):
                raise ValueError("玩家状态格式无效，默认骰未修改")
            user = players.setdefault(sender_id, {"cards": {}, "active_card": None})
            if not isinstance(user, dict):
                raise ValueError("玩家状态格式无效，默认骰未修改")
            if active_name:
                cards = user.get("cards")
                actual_name = self._card_name_key(cards, active_name)
                card = cards.get(actual_name) if isinstance(cards, dict) and actual_name is not None else None
                if not isinstance(card, dict):
                    raise ValueError("活动角色卡已不存在，默认骰未修改")
                attributes = card.get("attributes")
                if not isinstance(attributes, dict):
                    attributes = card["attributes"] = {}
                attributes["__DefaultDice"] = value
                return "card", actual_name
            if value == self.default_die:
                user.pop("default_die", None)
            else:
                user["default_die"] = value
            return "user", value

        scope, target = self.store.transact(save_default)
        if scope == "card":
            return f"已将角色卡「{target}」的默认骰设置为 D{value}。"
        return f"已将你的默认骰设置为 D{value}。"

    def _roll(self, args: str, group_id: str, sender_id: str, *, detailed: bool = True, hidden: bool = False, is_private: bool = False) -> str | HiddenCheckReply:
        active_name, card = self._active_card(sender_id, group_id)
        attributes = card.get("attributes", {}) if isinstance(card, dict) else {}
        attributes = attributes if isinstance(attributes, dict) else {}
        default_die = self._card_default_die(attributes, fallback=self._user_default_die(sender_id))
        system = card.get("system", "coc7") if isinstance(card, dict) else None
        resolver = self._card_expression_resolver(attributes, system=system)
        raw = args.strip()
        reason = ""
        count_source = None
        if not raw:
            expression_text = resolver.resolve_reference("__DefaultDiceExp") if resolver.has_reference("__DefaultDiceExp") else ""
        else:
            parts = raw.split(None, 1)
            head = parts[0]
            reason = parts[1].strip() if len(parts) > 1 else ""
            candidate = head
            if candidate.count("#") > 1:
                raise DiceError("批量掷骰只能包含一个 #")
            if "#" in candidate:
                count_source, candidate = candidate.split("#", 1)
            if resolver.has_reference(candidate):
                expression = resolver.resolve_reference(candidate)
                if not reason and count_source is None:
                    reason = candidate
            else:
                expression = candidate
            expression_text = f"{count_source}#{expression}" if count_source is not None else expression

        if expression_text.count("#") > 1:
            raise DiceError("批量掷骰只能包含一个 #")
        count_result = None
        if "#" in expression_text:
            count_source, expression = expression_text.split("#", 1)
            if count_source.strip():
                count_result = self.engine.roll(count_source.strip(), default_die)
                count = count_result.total
            else:
                count = 1
        else:
            expression = expression_text
            count = 1
        if not 1 <= count <= 10:
            raise DiceError("掷骰次数必须在 1 到 10 之间")

        def roll_many(group: dict | None = None):
            rng = self._session_rng_for(group, sender_id) if isinstance(group, dict) else None
            results = []
            for _ in range(count):
                if isinstance(self.engine, DiceEngine):
                    results.append(self.engine.roll(expression, default_die, rng=rng))
                else:
                    results.append(self.engine.roll(expression, default_die))
            return results

        if isinstance(self._group_snapshot(group_id).get("session"), dict):
            results = self._mutate_group(group_id, roll_many)
        else:
            results = roll_many()

        compact_expression = re.sub(r"\s+", "", expression).upper()
        if active_name and isinstance(card, dict) and default_die <= 100 and (not compact_expression or re.fullmatch(rf"(?:1)?D0*{default_die}", compact_expression)):
            self._record_card_statistics(sender_id, active_name, rolls=tuple((result.total, default_die) for result in results))

        if detailed:
            if count == 1:
                text = results[0].format()
            else:
                details = [f"{index}. {result.format()}" for index, result in enumerate(results, 1)]
                text = f"{results[0].expression}（{count} 次）：\n" + "\n".join(details)
        elif count == 1:
            text = f"{results[0].expression}={results[0].total}"
        else:
            totals = [result.total for result in results]
            text = f"{results[0].expression}（{count} 次）：{{ " + ",".join(map(str, totals)) + " }"
            extremes = [value for result, value in zip(results, totals) if result.expression in {"D100", "1D100"} and (value <= 5 or value >= 96)]
            if extremes:
                text += "，极值：" + ",".join(map(str, extremes))
        if count_result is not None and count_source and re.search(r"[dDfF]", count_source):
            count_detail = count_result.format() if callable(getattr(count_result, "format", None)) else str(count_result.total)
            text = f"次数骰：{count_detail} → {count}\n" + text
        if reason:
            text += f"\n理由：{reason}"
        if len(text) > self.max_response_length:
            totals = [str(result.total) for result in results]
            if count == 1:
                text = f"{results[0].expression}={results[0].total}"
            else:
                text = f"{results[0].expression}（{count} 次）：{{ " + ",".join(totals) + " }"
                if len(text) > self.max_response_length:
                    text = f"{count}次结果：{','.join(totals)}"
            if count_result is not None and count_source and re.search(r"[dDfF]", count_source):
                count_summary = f"次数骰：{count_result.total} → {count}\n"
                if len(count_summary) + len(text) <= self.max_response_length:
                    text = count_summary + text
            if reason:
                reason_prefix = "\n理由："
                reason_room = self.max_response_length - len(text) - len(reason_prefix)
                if reason_room > 0:
                    text += reason_prefix + reason[:reason_room]
        if hidden:
            command = "rsh" if not detailed else "rh"
            self._append_log(group_id, sender_id, f"{command} [hidden]", "隐藏掷骰已执行；结果未写入群日志。")
            return HiddenCheckReply(text, "隐藏掷骰已执行；结果仅尝试私信发送，若未收到请检查平台私聊权限。")
        self._append_log(group_id, sender_id, f"{'rs' if not detailed else 'r'} {args or 'D100'}", text)
        return text

    @staticmethod
    def _card_name_key(cards: object, requested: object) -> str | None:
        if not isinstance(cards, dict) or not isinstance(requested, str):
            return None
        if requested in cards:
            return requested
        folded = requested.casefold()
        matches = [name for name in cards if isinstance(name, str) and name.casefold() == folded]
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _card_name_exists(cards: object, requested: object) -> bool:
        return isinstance(cards, dict) and isinstance(requested, str) and any(
            isinstance(name, str) and name.casefold() == requested.casefold() for name in cards
        )

    @staticmethod
    def _active_card_name(user: dict, group_id: str | None = None) -> str | None:
        cards = user.get("cards", {})
        if not isinstance(cards, dict):
            return None
        # Preserve the pre-group pointer as a global fallback; group selections never overwrite it.
        active = user.get("active_card")
        if group_id is not None:
            scoped = user.get("active_cards_by_group")
            scoped_active = scoped.get(group_id) if isinstance(scoped, dict) else None
            if Main._card_name_key(cards, scoped_active) is not None:
                active = Main._card_name_key(cards, scoped_active)
            else:
                active = user.get("legacy_active_card", active)
        active = Main._card_name_key(cards, active)
        if active is None:
            active = next((name for name in cards if isinstance(name, str)), None)
        return active

    def _active_card(self, sender_id: str, group_id: str | None = None) -> tuple[str | None, dict | None]:
        user = self.store.player(sender_id)
        cards = user.get("cards", {})
        if not isinstance(cards, dict):
            return None, None
        active = self._active_card_name(user, group_id)
        return (active, cards[active]) if active is not None else (None, None)

    @staticmethod
    def _set_active_card_for_group(user: dict, name: str, group_id: str | None = None) -> None:
        if group_id is None:
            user["active_card"] = name
            return
        if "legacy_active_card" not in user:
            user["legacy_active_card"] = user.get("active_card")
        active_cards = user.get("active_cards_by_group")
        if not isinstance(active_cards, dict):
            active_cards = user["active_cards_by_group"] = {}
        active_cards[group_id] = name
        # Keep the old field as a compatibility mirror for existing integrations.
        user["active_card"] = name

    def _nickname_for_user(self, sender_id: str, group_id: str) -> str | None:
        player = self.store.player(sender_id)
        if not isinstance(player, dict):
            return None
        nicknames = player.get("nicknames")
        if not isinstance(nicknames, dict):
            return None
        if group_id in nicknames:
            nickname = nicknames[group_id]
        elif "0" in nicknames:
            nickname = nicknames["0"]
        elif len(nicknames) == 1:
            nickname = next(iter(nicknames.values()))
        else:
            nickname = None
        return nickname.strip() if isinstance(nickname, str) and nickname.strip() else None

    @staticmethod
    def _normalize_stat(stat: str) -> str:
        raw = stat.strip().upper()
        return STAT_ALIASES.get(raw, raw)

    def _resolve_skill(self, text: str, sender_id: str, *, card_name: str | None = None, group_id: str | None = None) -> tuple[int, str]:
        value_text = text.strip()
        if not value_text:
            raise ValueError("用法：.ra 技能名或数值")
        modifier = 0
        mod_match = re.search(r"\s*([+-]\s*\d+)$", value_text)
        if mod_match:
            modifier = int(mod_match.group(1).replace(" ", ""))
            value_text = value_text[:mod_match.start()].strip()
        if value_text.isdecimal():
            skill, label = int(value_text), f"技能 {value_text}"
        else:
            if card_name is None:
                _, card = self._active_card(sender_id, group_id)
            else:
                cards = self.store.player(sender_id).get("cards", {})
                actual_name = self._card_name_key(cards, card_name)
                card = cards.get(actual_name) if isinstance(cards, dict) and actual_name is not None else None
                if not isinstance(card, dict):
                    raise ValueError(f"角色卡「{card_name}」不存在")
            canonical = SKILL_NAME_ALIASES.get(value_text.casefold(), value_text)
            if isinstance(card, dict):
                attrs = card.get("attributes", {})
                attrs = attrs if isinstance(attrs, dict) else {}
                resolver = self._card_expression_resolver(attrs, system=card.get("system", "coc7"))
                if not resolver.has_reference(canonical):
                    raise ValueError(f"角色卡「{card.get('name', '?')}」没有属性/技能「{value_text}」")
                resolved = resolver.resolve_reference(canonical).strip()
                if re.fullmatch(r"[+-]?\d{1,10}", resolved):
                    skill = int(resolved)
                else:
                    skill = self.engine.roll(resolved, self._card_default_die(attrs, fallback=self._user_default_die(sender_id))).total
                label = f"{card.get('name', '角色')}·{canonical} {skill}"
            else:
                raw = SKILL_DEFAULT_VALUES.get(canonical.casefold())
                if raw is None:
                    raise ValueError("先用 .pc create 创建并 .pc use 选中角色卡，或直接输入技能数值")
                skill, label = int(raw), f"{canonical} {raw}"
        skill = max(0, min(10_000, skill + modifier))
        return skill, label + (f"{modifier:+d}" if modifier else "")

    def _effective_coc_rule(self, group_id: str, *, include_session: bool = True) -> int:
        group = self._group_snapshot(group_id)
        session = group.get("session")
        if include_session and isinstance(session, dict):
            configured = session.get("rule")
            if isinstance(configured, int) and not isinstance(configured, bool) and 0 <= configured <= 7:
                return configured
        configured = group.get("rule")
        if isinstance(configured, int) and not isinstance(configured, bool) and 0 <= configured <= 7:
            return configured
        return self.default_coc_rule

    @staticmethod
    def _trunc_coc_division(numerator: int, denominator: int) -> int:
        quotient = abs(numerator) // denominator
        return -quotient if numerator < 0 else quotient

    def _parse_check_spec(self, args: str, sender_id: str, group_id: str | None = None) -> tuple[int, str, bool, str, int, str, int, str | None, bool]:
        if len(args) > 512:
            raise ValueError("检定参数不能超过 512 个字符")
        text = args.strip()
        hidden = False
        if text.startswith("_"):
            hidden = True
            text = text[1:].lstrip()
        count_match = re.match(r"^([0-9]*)#", text)
        roll_count = 1
        if count_match:
            roll_count = int(count_match.group(1) or "1")
            if not 1 <= roll_count <= 10:
                raise ValueError("检定次数必须在 1 到 10 之间")
            text = text[count_match.end():].lstrip()
        if text.startswith("_"):
            hidden = True
            text = text[1:].lstrip()
        if not text:
            raise ValueError("用法：.ra 技能名、数值或检定表达式")

        percent_mode = ""
        percent_count = 1
        percent_match = re.match(r"(?i)^([bp])(\d*)(?=$|\s|[:：]|[\u3400-\u9fff])", text)
        if not percent_match:
            percent_match = re.match(r"(?i)^([bp])(\d*)_", text)
            if percent_match:
                hidden = True
        if percent_match:
            percent_mode = percent_match.group(1).upper()
            percent_count = int(percent_match.group(2) or "1")
            if not 1 <= percent_count <= 9:
                raise ValueError("奖励/惩罚骰数量必须在 1 到 9 之间")
            text = text[percent_match.end():].lstrip(" \t:：")

        automatic = text.startswith("自动成功")
        prefix = ""
        if automatic:
            prefix = "自动成功"
            text = text[len("自动成功"):].lstrip()

        difficulty = ""
        for candidate in ("困难", "极难", "极限", "简单"):
            if text.startswith(candidate):
                difficulty = candidate
                prefix += candidate
                text = text[len(candidate):].lstrip()
                break

        explicit_value = None
        if not text.isdecimal():
            explicit_match = re.search(r"(?:^|[=:]|\s)(\d{1,3})(?=\s|$)", text)
            if explicit_match:
                before_value = text[:explicit_match.start(1)].rstrip()
                if before_value and before_value[-1] not in "+-*/":
                    explicit_value = int(explicit_match.group(1))
                    text = before_value.rstrip(" :=\t")

        def read_operand(raw: str, name: str) -> int:
            if len(raw) > 10:
                raise ValueError(f"{name}超出 32 位整数范围")
            value = int(raw)
            if value > 2_147_483_647:
                raise ValueError(f"{name}超出 32 位整数范围")
            return value

        divisor = 1
        divisor_match = re.search(r"/\s*(\d+)\s*$", text)
        if divisor_match:
            divisor = read_operand(divisor_match.group(1), "除数")
            if divisor == 0:
                raise ValueError("除数不能为 0")
            text = text[:divisor_match.start()].rstrip()

        modifier = 0
        while True:
            modifier_match = re.search(r"([+-])\s*(\d+)\s*$", text)
            if not modifier_match:
                break
            value = read_operand(modifier_match.group(2), "技能修正")
            modifier += value if modifier_match.group(1) == "+" else -value
            if not -2_147_483_648 <= modifier <= 2_147_483_647:
                raise ValueError("技能修正超出 32 位整数范围")
            text = text[:modifier_match.start()].rstrip()

        multiple = 1
        multiple_match = re.search(r"\*\s*(\d+)\s*$", text)
        if multiple_match:
            multiple = read_operand(multiple_match.group(1), "技能倍率")
            text = text[:multiple_match.start()].rstrip()

        skill_text = text.strip()
        if not skill_text:
            raise ValueError("用法：.ra 技能名、数值或检定表达式")
        card_name = None
        cards = self.store.player(sender_id).get("cards", {})
        if not isinstance(cards, dict):
            cards = {}
        card_candidates = sorted((str(name) for name in cards), key=lambda name: (-len(name), name))
        for candidate in card_candidates:
            matched = False
            for separator in (":", "的"):
                prefix = candidate + separator
                if skill_text[:len(prefix)].casefold() == prefix.casefold():
                    card_name = candidate
                    skill_text = skill_text[len(prefix):].lstrip(":" if separator == ":" else " ").lstrip()
                    matched = True
                    break
            if matched:
                break
        if card_name is None and (":" in skill_text or "的" in skill_text):
            unknown_name = re.split(r"[:：]|的", skill_text, maxsplit=1)[0].strip()
            raise ValueError(f"角色卡「{unknown_name or skill_text}」不存在")
        base_skill, label = self._resolve_skill(skill_text, sender_id, card_name=card_name, group_id=group_id)
        if explicit_value is not None:
            base_skill = explicit_value
            label_parts = label.rsplit(" ", 1)
            if len(label_parts) == 2 and label_parts[1].isdecimal():
                label = f"{label_parts[0]} {explicit_value}"
            elif label.startswith("技能 "):
                label = f"技能 {explicit_value}"
            else:
                label = f"{label} {explicit_value}"

        simple_multiplier = 2 if difficulty == "简单" else 1
        numerator = (base_skill * multiple + modifier) * simple_multiplier
        skill = self._trunc_coc_division(self._trunc_coc_division(numerator, divisor), 2 if difficulty == "困难" else 5 if difficulty in {"极难", "极限"} else 1)
        if not 0 <= skill <= 1000:
            raise ValueError("检定成功率必须在 0 到 1000 之间")

        if multiple != 1:
            label += f"×{multiple}"
        if modifier:
            label += f"{modifier:+d}"
        if divisor != 1:
            label += f"/{divisor}"
        return skill, prefix + label, automatic, difficulty, roll_count, percent_mode, percent_count, card_name, hidden

    def _check(self, args: str, group_id: str, sender_id: str) -> str | HiddenCheckReply:
        skill, label, automatic, difficulty, roll_count, percent_mode, percent_count, card_name, hidden = self._parse_check_spec(args, sender_id, group_id)
        rule = self._effective_coc_rule(group_id)

        def normalize_result(result):
            if automatic and result.outcome == "failure":
                return replace(result, outcome="success")
            if roll_count == 1 and difficulty == "简单" and result.outcome == "hard":
                return replace(result, outcome="success")
            if roll_count > 1 and difficulty in {"困难", "极难", "极限"} and result.outcome in {"extreme", "hard"}:
                return replace(result, outcome="success")
            return result

        def roll_checks(rng=None):
            results = []
            for _ in range(roll_count):
                if isinstance(self.engine, DiceEngine):
                    result = self.engine.check(skill, rule=rule, rng=rng, percent_mode=percent_mode or None, percent_count=percent_count)
                else:
                    if percent_mode:
                        raise ValueError("当前骰子引擎不支持奖励/惩罚检定")
                    result = self.engine.check(skill, rule=rule)
                results.append(normalize_result(result))
            return results

        if isinstance(self._group_snapshot(group_id).get("session"), dict):
            def apply(group: dict):
                return roll_checks(self._session_rng_for(group, sender_id))
            results = self._mutate_group(group_id, apply)
        else:
            results = roll_checks()

        stat_card_name = card_name or self._active_card_name(self.store.player(sender_id), group_id)
        if not percent_mode and stat_card_name:
            self._record_card_statistics(sender_id, stat_card_name, rolls=tuple((result.roll, 100) for result in results), checks=tuple((result.roll, skill) for result in results))

        if roll_count == 1:
            text = f"{label}：{self.engine.describe_check(results[0])}（房规 {rule}：{COC_RULE_INFO[rule][0]}）"
        else:
            details = [f"{index}. {self.engine.describe_check(result)}" for index, result in enumerate(results, 1)]
            text = f"{label}（{roll_count} 次）" + chr(10) + chr(10).join(details)
        if hidden:
            self._append_log(group_id, sender_id, "ra [hidden]", "隐藏检定已执行；结果未写入群日志。")
            return HiddenCheckReply(text)
        self._append_log(group_id, sender_id, f"ra {args}", text)
        return text

    async def _send_hidden_check(self, event: AstrMessageEvent, result: HiddenCheckReply, group_id: str, sender_id: str) -> None:
        try:
            raw_platform_id = event.get_platform_id()
            platform_id = str(raw_platform_id).strip() if raw_platform_id else ""
        except Exception:
            platform_id = ""
        sender_raw_id = self._raw_platform_user_id(sender_id)
        recipients = {sender_raw_id} if sender_raw_id else set()
        group = self._group_snapshot(group_id)
        blind_sender = group.get("hidden_blind") is True
        session = group.get("session")
        if isinstance(session, dict):
            for key, fallback in (("game_masters", "game_master"), ("observers", None)):
                recipients.update(self._raw_platform_user_id(value) for value in self._session_member_ids(session, key, fallback))
        recipients.discard("")
        if blind_sender and sender_raw_id:
            recipients.discard(sender_raw_id)
        if not platform_id:
            self.logger.warning("Cannot route hidden Dice result: platform id is unavailable")
            return
        message = f"来自群聊的隐藏检定\n{result.private_text}"[: self.max_response_length]
        for recipient in sorted(recipients):
            try:
                await StarTools.send_message(f"{platform_id}:FriendMessage:{recipient}", [Plain(message)])
            except Exception:
                self.logger.warning("Could not privately deliver hidden Dice result")

    def _sanity(self, args: str, group_id: str, sender_id: str) -> str:
        match = re.fullmatch(r"\s*(\S+?)\s*/\s*(\S+)\s*", args)
        if not match:
            raise ValueError("用法：.sc 成功损失/失败损失，例如 .sc 1/1d6")
        active, card = self._active_card(sender_id, group_id)
        if not card or "SAN" not in card.get("attributes", {}):
            raise ValueError("当前角色卡没有 SAN；请先创建并选中 COC 角色卡")
        sanity = int(card["attributes"]["SAN"])
        attributes = card.get("attributes", {}) if isinstance(card, dict) else {}
        default_die = self._card_default_die(attributes, fallback=self._user_default_die(sender_id))
        rule = self._effective_coc_rule(group_id, include_session=False)
        def roll_sanity(group: dict):
            rng = self._session_rng_for(group, sender_id)
            if isinstance(self.engine, DiceEngine):
                check_result = self.engine.check(sanity, rule=rule, rng=rng)
                loss_expr = match.group(1) if check_result.success else match.group(2)
                loss_total = self.engine.roll(loss_expr, default_die, rng=rng).total
            else:
                check_result = self.engine.check(sanity, rule=rule)
                loss_expr = match.group(1) if check_result.success else match.group(2)
                loss_total = self.engine.roll(loss_expr, default_die).total
            return check_result, loss_expr, loss_total
        if isinstance(self._group_snapshot(group_id).get("session"), dict):
            check, loss_expression, loss = self._mutate_group(group_id, roll_sanity)
        else:
            check, loss_expression, loss = roll_sanity(self._group_snapshot(group_id))
        new_sanity = max(0, sanity - max(0, loss))
        def apply(state: dict) -> None:
            user = state["players"].setdefault(sender_id, {"cards": {}, "active_card": None})
            target = user["cards"][active]
            target.setdefault("attributes", {})["SAN"] = new_sanity
            try:
                self._update_card_statistics_in_state(state, sender_id, active, rolls=((check.roll, 100),), checks=((check.roll, sanity),))
            except CardStatisticsError as exc:
                self.logger.warning("Could not update SAN card statistics: %s", exc)
        self.store.transact(apply)
        text = f"SAN 检定：{check.roll}/{sanity} {'成功' if check.success else '失败'}，损失 {loss_expression}={loss}；SAN {sanity}→{new_sanity}"
        self._append_log(group_id, sender_id, f"sc {args}", text)
        return text

    def _growth_check(self, args: str, group_id: str, sender_id: str) -> str:
        if len(args) > 512:
            raise ValueError("成长检定参数不能超过 512 个字符")
        tokens = shlex.split(args)
        if not tokens:
            raise ValueError("用法：.en 技能名 [当前值] [失败变化/成功变化]")
        attribute = tokens[0]
        player = self.store.player(sender_id)
        cards = player.get("cards", {})
        cards = cards if isinstance(cards, dict) else {}
        active_name = self._active_card_name(player, group_id)
        card = cards.get(active_name) if active_name is not None else None
        attributes = card.get("attributes", {}) if isinstance(card, dict) else {}
        attributes = attributes if isinstance(attributes, dict) else {}
        canonical = SKILL_NAME_ALIASES.get(attribute.casefold(), attribute)
        candidates = {attribute.casefold(), self._normalize_stat(attribute).casefold(), canonical.casefold(), self._normalize_stat(canonical).casefold()}
        attribute_key = next((key for key in attributes if str(key).casefold() in candidates), None)
        target_attribute = attribute_key if attribute_key is not None else canonical
        observed_present = target_attribute in attributes
        observed_value = attributes.get(target_attribute)
        current = None
        change_index = 1
        if len(tokens) > 1 and tokens[1].isascii() and tokens[1].isdecimal():
            if len(tokens[1]) > 3:
                raise ValueError("成长检定当前值最多 3 位数字")
            current = int(tokens[1])
            if not 0 <= current <= 999:
                raise ValueError("成长检定当前值必须在 0 到 999 之间")
            change_index = 2
        elif attribute_key is not None:
            raw_current = attributes[attribute_key]
            if isinstance(raw_current, bool) or not isinstance(raw_current, (int, str)):
                raise ValueError(f"角色卡属性「{attribute}」不是整数")
            if isinstance(raw_current, str) and not re.fullmatch(r"[+-]?\d+", raw_current.strip()):
                raise ValueError(f"角色卡属性「{attribute}」不是整数")
            try:
                current = int(raw_current)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"角色卡属性「{attribute}」不是整数") from exc
        else:
            current, _ = self._resolve_skill(attribute, sender_id, group_id=group_id)
        if current is None or not 0 <= current <= 10_000:
            raise ValueError("成长检定当前值必须在 0 到 10000 之间")
        change_tokens = tokens[change_index:]
        if len(change_tokens) > 1:
            raise ValueError("成长变化式不能包含空格；用引号包住技能名即可")
        failure_expression = ""
        success_expression = "1d10"
        if change_tokens:
            change = change_tokens[0]
            if not change.startswith(("+", "-")):
                raise ValueError("成长变化式须以 + 或 - 开头")
            if "/" in change:
                failure_expression, success_expression = change.split("/", 1)
                if not success_expression:
                    raise ValueError("斜线后必须提供成功成长骰式")
            else:
                success_expression = change
        has_session = isinstance(self._group_snapshot(group_id).get("session"), dict)
        def roll_percentile(group: dict) -> int:
            rng = self._session_rng_for(group, sender_id)
            if isinstance(self.engine, DiceEngine):
                return self.engine.roll("1d100", 100, rng=rng).total
            return self.engine.roll("1d100", 100).total
        percentile = self._mutate_group(group_id, roll_percentile) if has_session else roll_percentile(self._group_snapshot(group_id))
        if isinstance(card, dict) and active_name is not None:
            self._record_card_statistics(sender_id, active_name, rolls=((percentile, 100),))
        grew = not (percentile <= current and percentile <= 95)
        change_expression = success_expression if grew else failure_expression
        change_result = self.engine.roll(change_expression, 100) if change_expression else None
        delta = change_result.total if change_result is not None else 0
        new_value = current + delta
        saved = False
        if change_result is not None and isinstance(card, dict) and active_name is not None:
            if not 0 <= new_value <= 10_000:
                raise ValueError("成长后的属性必须在 0 到 10000 之间；未写入角色卡")
            def save_growth(state: dict) -> None:
                players = state.get("players")
                user = players.get(sender_id) if isinstance(players, dict) else None
                user_cards = user.get("cards") if isinstance(user, dict) else None
                target = user_cards.get(active_name) if isinstance(user_cards, dict) else None
                if not isinstance(target, dict):
                    raise ValueError(f"角色卡「{active_name}」已不存在，成长结果未写入")
                stored = target.get("attributes")
                if not isinstance(stored, dict):
                    stored = target["attributes"] = {}
                if (target_attribute in stored) != observed_present or stored.get(target_attribute) != observed_value:
                    raise ValueError("成长检定期间该属性已变化；为避免覆盖，新结果未写入")
                stored[target_attribute] = new_value
            self.store.transact(save_growth)
            saved = True
        status = "成长成功" if grew else ("成长失败并应用失败变化" if change_result is not None else "成长失败，属性不变")
        text = f"{attribute} 成长检定：D100={percentile}/{current}，{status}"
        if change_result is not None:
            text += f"；{change_expression}={delta:+d}，属性 {current}→{new_value}"
            text += "（已更新角色卡）" if saved else "（没有活动角色卡，未保存）"
        self._append_log(group_id, sender_id, f"en {args}", text)
        return text

    @staticmethod
    def _format_character_generation_report(system: str, attrs: dict) -> str:
        if system == "coc7":
            fields = (
                ("力量STR=3D6*5=", "STR"),
                ("体质CON=3D6*5=", "CON"),
                ("体型SIZ=(2D6+6)*5=", "SIZ"),
                ("敏捷DEX=3D6*5=", "DEX"),
                ("外貌APP=3D6*5=", "APP"),
                ("智力INT=(2D6+6)*5=", "INT"),
                ("意志POW=3D6*5=", "POW"),
                ("教育EDU=(2D6+6)*5=", "EDU"),
            )
            lines = [
                f"{label}{attrs[key]}/{attrs[key] // 2}/{attrs[key] // 5}"
                for label, key in fields
            ]
            lines.append(f"幸运LUCK=3D6*5={attrs['LUCK']}")
            total_without_luck = sum(attrs[key] for _, key in fields)
            lines.append(f"共计:{total_without_luck}/{total_without_luck + attrs['LUCK']}")
            db = attrs["DB"]
            if db == "1d4":
                db = "1D4"
            build = "计算错误" if attrs["BUILD"] is None else str(attrs["BUILD"])
            lines.extend((
                f"理智SAN=POW={attrs['SAN']}",
                f"生命值HP=(SIZ+CON)/10={attrs['HP']}",
                f"魔法值MP=POW/5={attrs['MP']}",
                f"伤害奖励DB={db}",
                f"体格={build}",
                f"移动力MOV={attrs['MOV']}",
            ))
            return "\n" + "\n".join(lines)
        if system == "coc6":
            fields = (
                ("力量STR=3D6=", "STR"),
                ("体质CON=3D6=", "CON"),
                ("体型SIZ=2D6+6=", "SIZ"),
                ("敏捷DEX=3D6=", "DEX"),
                ("外貌APP=3D6=", "APP"),
                ("智力INT=2D6+6=", "INT"),
                ("意志POW=3D6=", "POW"),
                ("教育EDU=3D6+3=", "EDU"),
            )
            lines = [f"{label}{attrs[key]}" for label, key in fields]
            lines.append(f"共计:{sum(attrs[key] for _, key in fields)}")
            db = "计算错误!" if attrs["DB"] == "计算错误" else str(attrs["DB"]).upper()
            lines.extend((
                f"理智SAN=POW*5={attrs['SAN']}",
                f"灵感IDEA=INT*5={attrs['IDEA']}",
                f"幸运LUCK=POW*5={attrs['LUCK']}",
                f"知识KNOW=EDU*5={attrs['KNOW']}",
                f"生命值HP=(CON+SIZ)/2={attrs['HP']}",
                f"魔法值MP=POW={attrs['MP']}",
                f"伤害奖励DB={db}",
                f"资产=1D10={attrs['ASSET']}",
            ))
            return "\n" + "\n".join(lines)
        raise ValueError("不支持的角色生成规则")

    def _generate_character(self, command: str, args: str, sender_id: str, group_id: str | None = None) -> str:
        system, default_name = ("coc6", "COC6") if command == "coc6d" else ("coc7", "COC7")
        requested_name = args.strip() or None
        if requested_name is not None and (not 1 <= len(requested_name) <= 40 or "=" in requested_name):
            raise ValueError("角色名必须为 1 到 40 个字符，且不能包含 =")
        existing_cards = self.store.player(sender_id).get("cards", {})
        if not isinstance(existing_cards, dict):
            raise ValueError("角色卡数据格式无效，无法创建角色卡")
        if len(existing_cards) >= 32:
            raise ValueError("每位用户最多保存 32 张角色卡")
        if requested_name and self._card_name_exists(existing_cards, requested_name):
            raise ValueError(f"角色卡「{requested_name}」已存在；请改名或先删除旧卡")

        if system == "coc7":
            formulas = dict(COC7_BASE_BUILD_FORMULAS)
            attrs = {key: self.engine.roll(expr, self.default_die).total for key, expr in formulas.items()}
            strength_size = attrs["STR"] + attrs["SIZ"]
            if strength_size <= 64:
                damage_bonus, build = "-2", -2
            elif strength_size <= 84:
                damage_bonus, build = "-1", -1
            elif strength_size <= 124:
                damage_bonus, build = "0", 0
            elif strength_size <= 164:
                damage_bonus, build = "1d4", 1
            elif strength_size <= 204:
                damage_bonus, build = "1d6", 2
            else:
                damage_bonus, build = "计算错误", None
            if attrs["DEX"] < attrs["SIZ"] and attrs["STR"] < attrs["SIZ"]:
                movement = 7
            elif attrs["DEX"] > attrs["SIZ"] and attrs["STR"] > attrs["SIZ"]:
                movement = 9
            else:
                movement = 8
            attrs.update({
                "SAN": attrs["POW"], "HP": (attrs["CON"] + attrs["SIZ"]) // 10,
                "MP": attrs["POW"] // 5, "DB": damage_bonus, "BUILD": build, "MOV": movement,
            })
            derived = f"SAN={attrs['SAN']} HP={attrs['HP']} MP={attrs['MP']} DB={damage_bonus} BUILD={build if build is not None else '计算错误'} MOV={movement}"
        else:
            formulas = {
                "STR": "3d6", "CON": "3d6", "SIZ": "2d6+6", "DEX": "3d6",
                "APP": "3d6", "INT": "2d6+6", "POW": "3d6", "EDU": "3d6+3",
            }
            attrs = {key: self.engine.roll(expr, self.default_die).total for key, expr in formulas.items()}
            strength_size = attrs["STR"] + attrs["SIZ"]
            if strength_size <= 12:
                damage_bonus = "-1d6"
            elif strength_size <= 16:
                damage_bonus = "-1d4"
            elif strength_size <= 24:
                damage_bonus = "0"
            elif strength_size <= 32:
                damage_bonus = "1d4"
            else:
                damage_bonus = "1d6"
            attrs.update({
                "SAN": attrs["POW"] * 5, "IDEA": attrs["INT"] * 5,
                "LUCK": attrs["POW"] * 5, "KNOW": attrs["EDU"] * 5,
                "HP": (attrs["CON"] + attrs["SIZ"] + 1) // 2, "MP": attrs["POW"],
                "DB": damage_bonus, "ASSET": self.engine.roll("1d10").total,
            })
            derived = f"SAN={attrs['SAN']} IDEA={attrs['IDEA']} LUCK={attrs['LUCK']} KNOW={attrs['KNOW']} HP={attrs['HP']} MP={attrs['MP']} DB={damage_bonus} ASSET={attrs['ASSET']}"

        created_at = self._now()
        def apply(state: dict) -> str:
            user = state["players"].setdefault(sender_id, {"cards": {}, "active_card": None})
            cards = user.get("cards", {})
            if not isinstance(cards, dict):
                raise ValueError("角色卡数据格式无效，无法创建角色卡")
            if len(cards) >= 32:
                raise ValueError("每位用户最多保存 32 张角色卡")
            user["cards"] = cards
            name = requested_name
            if name is None:
                name = default_name
                suffix = 2
                while self._card_name_exists(cards, name):
                    name = f"{default_name} {suffix}"
                    suffix += 1
            elif self._card_name_exists(cards, name):
                raise ValueError(f"角色卡「{name}」已存在；请改名或先删除旧卡")
            cards[name] = {"name": name, "system": system, "attributes": attrs, "created_at": created_at}
            self._set_active_card_for_group(user, name, group_id)
            return name
        name = self.store.transact(apply)
        stats = " ".join(f"{key}={attrs[key]}" for key in formulas)
        compact_response = f"已生成并选中 {system.upper()} 角色卡「{name}」：{stats}\n{derived}"
        upstream_report = self._format_character_generation_report(system, attrs)
        full_response = f"{name}的调查员作成:{upstream_report}\n（角色卡已保存并设为当前活动卡）"
        return full_response if len(full_response) <= self.max_response_length else compact_response

    def _generate_stats_batch(self, system: str, args: str, group_id: str = "", sender_id: str = "") -> str:
        if system in {"coc7", "coc6"}:
            count_match = re.match(r"[ \t\r\n\v\f]*([0-9]+)", args)
            count_token = count_match.group(1) if count_match else ""
            if system == "coc7":
                if len(count_token) > 1 and count_token != "10":
                    raise ValueError("属性生成组数只能为 1 到 10")
                count = int(count_token) if count_token else 1
            else:
                if len(count_token) > 2:
                    raise ValueError("COC6 属性生成组数不能超过两位")
                count = int(count_token) if count_token else 1
                if count > 10:
                    raise ValueError("属性生成组数不能超过 10")
            if count == 0:
                raise ValueError("属性生成组数不能为 0")
        else:
            raw_count = args.strip()
            if raw_count and (not raw_count.isascii() or not raw_count.isdecimal()):
                raise ValueError(f"用法：.{system} [生成组数 1-10]")
            count = int(raw_count) if raw_count else 1
            if not 1 <= count <= 10:
                raise ValueError("属性生成组数必须在 1 到 10 之间")
        if system == "coc7":
            entries = [
                ("力量", "3d6", 5), ("体质", "3d6", 5), ("体型", "2d6+6", 5),
                ("敏捷", "3d6", 5), ("外貌", "3d6", 5), ("智力", "2d6+6", 5),
                ("意志", "3d6", 5), ("教育", "2d6+6", 5), ("幸运", "3d6", 5),
            ]
        elif system == "coc6":
            entries = [
                ("力量", "3d6", 1), ("体质", "3d6", 1), ("体型", "2d6+6", 1),
                ("敏捷", "3d6", 1), ("外貌", "3d6", 1), ("智力", "2d6+6", 1),
                ("意志", "3d6", 1), ("教育", "3d6+3", 1), ("资产", "1d10", 1),
            ]
        else:
            raise ValueError("仅支持 COC6/COC7 属性生成")

        if system in {"coc7", "coc6"} and sender_id:
            player = self.store.player(sender_id)
            active_card = self._active_card_name(player, group_id)
            nickname = self._nickname_for_user(sender_id, group_id)
            pc_name = active_card if isinstance(active_card, str) and active_card != "角色卡" else (nickname or sender_id)
            if not isinstance(pc_name, str):
                pc_name = ""
            prefix = f"{pc_name[:128]}的调查员作成:"
        else:
            prefix = f"{system.upper()} 属性生成："

        if system == "coc7":
            max_row = " ".join(f"{label}:90" for label, _, _ in entries) + " 共计:720/810"
            if len(prefix) + count * (len(max_row) + 1) > self.max_response_length:
                raise ValueError("COC7 结果超出响应上限；请降低组数或提高响应长度配置")
        else:
            # Bound every first-eight stat, alignment padding, total, and the final asset value.
            value_and_spaces = 3 if count == 1 else 4
            max_row = sum(len(label) + 1 + value_and_spaces for label, _, _ in entries[:-1])
            max_row += len("共计:147") + len(" 资产:10")
            if len(prefix) + count * (max_row + 1) > self.max_response_length:
                raise ValueError("COC6 结果超出响应上限；请降低组数或提高响应长度配置")

        rows = []
        for _ in range(count):
            values = [self.engine.roll(expr, self.default_die).total * scale for _, expr, scale in entries]
            total = sum(values[:-1])
            if system == "coc7":
                shown = " ".join(f"{label}:{value}" for (label, _, _), value in zip(entries, values))
                shown += f" 共计:{total}/{sum(values)}"
            else:
                shown = "".join(
                    f"{label}:{value} " + ("  " if count != 1 and value < 10 else "")
                    for (label, _, _), value in zip(entries[:-1], values[:-1])
                )
                shown += f"共计:{total} {entries[-1][0]}:{values[-1]}"
            rows.append(shown)
        return prefix + "\n" + "\n".join(rows)

    def _update_card_statistics_in_state(self, state: dict, sender_id: str, card_name: str | None, *, rolls: tuple[tuple[int, int], ...] = (), checks: tuple[tuple[int, int], ...] = ()) -> bool:
        if not card_name or not rolls and not checks:
            return False
        players = state.get("players")
        user = players.get(sender_id) if isinstance(players, dict) else None
        cards = user.get("cards") if isinstance(user, dict) else None
        actual_name = self._card_name_key(cards, card_name)
        card = cards.get(actual_name) if isinstance(cards, dict) and actual_name is not None else None
        if not isinstance(card, dict):
            return False
        if "statistics" in card and not validate_card_statistics(card.get("statistics")):
            raise CardStatisticsError("角色卡统计数据格式无效，未覆盖原记录")
        current = card.get("statistics") if "statistics" in card else None
        card["statistics"] = updated_card_statistics(current, rolls, checks)
        return True

    def _record_card_statistics(self, sender_id: str, card_name: str | None, *, rolls: tuple[tuple[int, int], ...] = (), checks: tuple[tuple[int, int], ...] = ()) -> bool:
        if not card_name or not rolls and not checks:
            return False
        try:
            return bool(self.store.transact(lambda state: self._update_card_statistics_in_state(state, sender_id, card_name, rolls=rolls, checks=checks)))
        except Exception as exc:
            # Statistics are ancillary after a completed roll; a persistence error must not hide the result.
            self.logger.warning("Could not update Dice card statistics: %s", exc)
            return False

    def _character_command(self, args: str, sender_id: str, group_id: str | None = None) -> str:
        tokens = shlex.split(args)
        if not tokens or tokens[0].casefold() in {"help", "?"}:
            return "角色卡最多 32 张：.pc create 名称 [coc6|coc7]｜.pc stat｜.pc type [COC7]/temp（仅安全内置模板）｜.pc build/redo [pc|bg|pc:bg][:卡名]｜.pc list/grp（列出本平台/bot的默认及群映射）｜.pc use/tag 名称（tag 不带名称重置本会话选择）｜.pc show [名称]｜.pc set 名称 属性 数值｜.pc nn/rename 新名称｜.pc cpy 目标[=来源]｜.pc tojson [卡名]（导出本人卡属性 JSON）｜.pc delete 名称｜.pc clr 清空角色卡"
        action = tokens[0].casefold()
        if action in {"build", "redo"}:
            spec = " ".join(tokens[1:]).strip()
            presets = ["pc"]
            requested_name = None
            if spec:
                if spec.casefold() == "pc:bg":
                    presets = ["pc", "bg"]
                elif ":" in spec:
                    prefix, requested_name = spec.rsplit(":", 1)
                    presets = [part.strip().casefold() for part in prefix.split(":")]
                    requested_name = requested_name.strip()
                    if not requested_name:
                        raise ValueError("用法：.pc build/redo [pc|bg|pc:bg][:角色卡名]")
                elif spec.casefold() in {"pc", "bg"}:
                    presets = [spec.casefold()]
                else:
                    requested_name = spec
            if not presets or any(preset not in {"pc", "bg"} for preset in presets):
                raise ValueError("仅支持 COC7 的 pc、bg 或 pc:bg 预设；自定义模板未导入")
            if requested_name is not None and (not 1 <= len(requested_name) <= 40 or ":" in requested_name or "=" in requested_name):
                raise ValueError("角色卡名称须为 1–40 字，且不能包含 : 或 =")
            redo = action == "redo"
            preset_formulas = {"pc": COC7_BASE_BUILD_FORMULAS, "bg": COC7_BG_BUILD_FORMULAS}

            def apply_preset(state: dict) -> tuple[str, dict[str, int], bool, tuple[str, ...]]:
                players = state.get("players")
                if not isinstance(players, dict):
                    raise ValueError("玩家数据格式无效，无法生成角色卡")
                user = players.setdefault(sender_id, {"cards": {}, "active_card": None})
                cards = user.get("cards")
                if cards is None:
                    cards = user["cards"] = {}
                if not isinstance(cards, dict):
                    raise ValueError("角色卡数据格式无效，无法生成预设")
                actual_name = self._card_name_key(cards, requested_name) if requested_name is not None else self._active_card_name(user, group_id)
                card = cards.get(actual_name) if actual_name is not None else None
                is_new = False
                if card is None:
                    if requested_name is None:
                        raise ValueError("当前会话没有活动角色卡；请先用 .pc create 创建角色卡")
                    if self._card_name_exists(cards, requested_name):
                        raise ValueError("角色卡名称存在大小写冲突；请先整理重名卡片")
                    if len(cards) >= 32:
                        raise ValueError("每位用户最多保存 32 张角色卡")
                    actual_name = requested_name
                    card = {"name": actual_name, "system": "coc7", "attributes": {}, "created_at": self._now()}
                    cards[actual_name] = card
                    is_new = True
                if not isinstance(card, dict):
                    raise ValueError("角色卡数据格式无效，无法生成预设")
                system = card.get("system", "coc7")
                if not isinstance(system, str) or system.casefold() != "coc7":
                    raise ValueError("COC7 基础预设只能用于 COC7 角色卡")
                raw_attributes = card.get("attributes", {})
                if not isinstance(raw_attributes, dict):
                    if not redo:
                        raise ValueError("角色卡属性数据格式无效；使用 .pc redo 可清空并重建")
                    raw_attributes = {}
                attributes = {} if redo else raw_attributes
                existing = {
                    self._normalize_stat(key)
                    for key in attributes
                    if isinstance(key, str)
                }
                selected_presets = list(presets)
                if is_new and "pc" not in selected_presets:
                    selected_presets.append("pc")
                generated: dict[str, int] = {}
                for selected_preset in selected_presets:
                    for key, expression in preset_formulas[selected_preset]:
                        normalized = self._normalize_stat(key)
                        if normalized in existing:
                            continue
                        value = 0 if expression is None else self.engine.roll(expression, self.default_die).total
                        generated[key] = value
                        existing.add(normalized)
                attributes.update(generated)
                card["attributes"] = attributes
                card["system"] = "coc7"
                card.setdefault("name", actual_name)
                self._set_active_card_for_group(user, actual_name, group_id)
                return actual_name, generated, is_new, tuple(selected_presets)

            actual_name, generated, is_new, selected_presets = self.store.transact(apply_preset)
            preset_label = "+".join(selected_presets)
            if not generated:
                return f"角色卡「{actual_name}」的 COC7 {preset_label} 预设属性已齐全，未覆盖现有值。"
            operation = "重建" if redo else "补齐"
            created_note = "（新建）" if is_new else ""
            shown = " ".join(f"{key}={value}" for key, value in generated.items())
            return f"已{operation} COC7 {preset_label} 预设角色卡「{actual_name}」{created_note}：{shown}"
        if action in {"nn", "rename"}:
            if len(tokens) < 2:
                raise ValueError("用法：.pc nn 新名称（重命名当前角色卡）")
            new_name = " ".join(tokens[1:]).strip()
            if not 1 <= len(new_name) <= 40 or ":" in new_name or "=" in new_name:
                raise ValueError("新名称需为 1–40 字，且不能包含 : 或 =")

            def rename_active_card(state: dict) -> str:
                players = state.get("players")
                user = players.get(sender_id) if isinstance(players, dict) else None
                if not isinstance(user, dict):
                    raise ValueError("当前用户没有角色卡")
                cards = user.get("cards")
                if not isinstance(cards, dict) or not cards:
                    raise ValueError("当前用户没有角色卡")
                old_name = self._card_name_key(cards, self._active_card_name(user, group_id))
                if old_name is None:
                    raise ValueError("当前会话没有活动角色卡")
                if self._card_name_exists(cards, new_name):
                    raise ValueError(f"角色卡「{new_name}」已存在；请选择其他名称")
                card = cards[old_name]
                if not isinstance(card, dict):
                    raise ValueError("活动角色卡数据格式无效，无法重命名")
                renamed_cards = {}
                for card_name, card_data in cards.items():
                    renamed_cards[new_name if card_name == old_name else card_name] = card_data
                user["cards"] = renamed_cards
                card["name"] = new_name
                scoped = user.get("active_cards_by_group")
                if isinstance(scoped, dict):
                    for scoped_group, active_name in list(scoped.items()):
                        if isinstance(active_name, str) and active_name.casefold() == old_name.casefold():
                            scoped[scoped_group] = new_name
                for pointer in ("legacy_active_card", "active_card"):
                    active_name = user.get(pointer)
                    if isinstance(active_name, str) and active_name.casefold() == old_name.casefold():
                        user[pointer] = new_name
                return old_name

            old_name = self.store.transact(rename_active_card)
            return f"已将当前角色卡「{old_name}」重命名为「{new_name}」；其他会话选择已同步。"
        if action == "clr":
            if len(tokens) != 1:
                raise ValueError("用法：.pc clr")

            def clear_cards(state: dict) -> int:
                players = state.get("players")
                user = players.get(sender_id) if isinstance(players, dict) else None
                if not isinstance(user, dict):
                    return 0
                cards = user.get("cards")
                count = len(cards) if isinstance(cards, dict) else 0
                user["cards"] = {}
                user["active_card"] = None
                user["legacy_active_card"] = None
                user["active_cards_by_group"] = {}
                return count

            cleared = self.store.transact(clear_cards)
            return f"已清空 {cleared} 张角色卡；昵称和其他玩家状态保留。" if cleared else "没有角色卡可清空；其他玩家状态已保留。"
        if action == "cpy":
            spec = " ".join(tokens[1:]).strip()
            if not spec or spec.count("=") > 1:
                raise ValueError("用法：.pc cpy 目标角色卡[=来源角色卡]；省略来源时复制当前卡")
            if "=" in spec:
                target_name, source_spec = spec.split("=", 1)
                target_name, source_name = target_name.strip(), source_spec.strip() or None
            else:
                target_name, source_name = spec, None
            if not 1 <= len(target_name) <= 40 or ":" in target_name or "=" in target_name:
                raise ValueError("目标角色卡名称须为 1–40 字，且不能包含 : 或 =")

            def copy_card(state: dict) -> tuple[str, str, bool]:
                players = state.get("players")
                user = players.get(sender_id) if isinstance(players, dict) else None
                if not isinstance(user, dict):
                    raise ValueError("没有可复制的来源角色卡")
                cards = user.get("cards")
                if not isinstance(cards, dict):
                    raise ValueError("角色卡数据格式无效，无法复制")
                actual_source = self._card_name_key(cards, source_name) if source_name else self._active_card_name(user, group_id)
                source_card = cards.get(actual_source) if actual_source is not None else None
                if not isinstance(source_card, dict):
                    raise ValueError(f"找不到来源角色卡「{source_name or '当前角色卡'}」")
                source_attributes = source_card.get("attributes", {})
                if not isinstance(source_attributes, dict):
                    raise ValueError("来源角色卡属性数据格式无效，无法复制")
                actual_target = self._card_name_key(cards, target_name)
                if actual_target is None and self._card_name_exists(cards, target_name):
                    raise ValueError("角色卡名称存在大小写冲突；请先整理重名卡片")
                is_new = actual_target is None
                if is_new and len(cards) >= 32:
                    raise ValueError("每位用户最多保存 32 张角色卡")
                if actual_target == actual_source:
                    return actual_target, actual_source, False
                old_target = cards.get(actual_target) if actual_target is not None else None
                copied = copy.deepcopy(source_card)
                copied["name"] = actual_target or target_name
                copied["attributes"] = copy.deepcopy(source_attributes)
                if isinstance(old_target, dict) and "created_at" in old_target:
                    copied["created_at"] = old_target["created_at"]
                else:
                    copied["created_at"] = self._now()
                destination = actual_target or target_name
                cards[destination] = copied
                return destination, actual_source, is_new

            destination, source, is_new = self.store.transact(copy_card)
            if destination == source:
                return f"「{destination}」与来源相同，未作改动。"
            operation = "新建" if is_new else "覆盖"
            return f"已从「{source}」复制到{operation}角色卡「{destination}」；活动角色卡选择未更改。"
        if action == "create":
            if len(tokens) not in (2, 3):
                raise ValueError("用法：.pc create 名称 [coc6|coc7]")
            name, system = tokens[1], tokens[2].casefold() if len(tokens) == 3 else "coc7"
            if not 1 <= len(name) <= 40 or "=" in name or system not in {"coc6", "coc7", "dnd"}:
                raise ValueError("角色名最多 40 字、不能包含 =，规则仅支持 coc6/coc7/dnd")
            existing_cards = self.store.player(sender_id).get("cards", {})
            if not isinstance(existing_cards, dict):
                raise ValueError("角色卡数据格式无效，无法创建角色卡")
            if len(existing_cards) >= 32:
                raise ValueError("每位用户最多保存 32 张角色卡")
            if self._card_name_exists(existing_cards, name):
                raise ValueError(f"角色卡「{name}」已存在；请改名或先删除旧卡")
            def apply(state: dict) -> None:
                user = state["players"].setdefault(sender_id, {"cards": {}, "active_card": None})
                cards = user.get("cards", {})
                if not isinstance(cards, dict):
                    raise ValueError("角色卡数据格式无效，无法创建角色卡")
                if len(cards) >= 32:
                    raise ValueError("每位用户最多保存 32 张角色卡")
                user["cards"] = cards
                if self._card_name_exists(cards, name):
                    raise ValueError(f"角色卡「{name}」已存在；请改名或先删除旧卡")
                cards[name] = {"name": name, "system": system, "attributes": {}, "created_at": self._now()}
                self._set_active_card_for_group(user, name, group_id)
            self.store.transact(apply)
            return f"已创建并选中「{name}」({system})。用 .st 力量 60 敏捷 55 设置属性。"
        user = self.store.player(sender_id)
        cards = user.get("cards", {})
        if action == "tojson":
            requested = " ".join(tokens[1:]).strip() if len(tokens) > 1 else ""
            if len(tokens) > 1 and not requested:
                raise ValueError("用法：.pc tojson [角色卡名]")
            if not isinstance(cards, dict) or len(cards) > 33 or any(not isinstance(key, str) or not isinstance(value, dict) for key, value in cards.items()):
                raise ValueError("角色卡数据格式无效，拒绝导出")
            card_name = self._card_name_key(cards, requested if requested else self._active_card_name(user, group_id))
            card = cards.get(card_name) if card_name is not None else None
            if card_name is None or not isinstance(card, dict):
                raise ValueError("找不到要导出的角色卡；请先创建角色卡或检查名称")
            stored_name = card.get("name", card_name)
            system = card.get("system", "coc7")
            attributes = card.get("attributes", {})
            if stored_name != card_name or not isinstance(system, str) or not 1 <= len(system) <= 64 or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in system) or not isinstance(attributes, dict):
                raise ValueError("角色卡数据格式无效，拒绝导出")
            exported = dict(attributes)
            for metadata_key, metadata_value in (("__Name", card_name), ("__Type", system.upper())):
                matches = [key for key in exported if isinstance(key, str) and key.casefold() == metadata_key.casefold()]
                if len(matches) > 1:
                    raise ValueError("角色卡系统字段存在大小写冲突，拒绝导出")
                exported[matches[0] if matches else metadata_key] = metadata_value
            limit = min(4000, self.max_response_length)
            stack = [(exported, 0)]
            nodes = 0
            source_chars = 0
            while stack:
                value, depth = stack.pop()
                nodes += 1
                if nodes > 4096 or depth > 32:
                    raise ValueError("角色卡 JSON 结构超过安全导出边界")
                if isinstance(value, dict):
                    if nodes + len(stack) + len(value) > 4096:
                        raise ValueError("角色卡 JSON 结构超过安全导出边界")
                    for key, child in value.items():
                        if not isinstance(key, str):
                            raise ValueError("角色卡 JSON 键必须是文本")
                        try:
                            key.encode("utf-8")
                        except UnicodeEncodeError as exc:
                            raise ValueError("角色卡含无效 Unicode，拒绝导出") from exc
                        source_chars += len(key)
                        stack.append((child, depth + 1))
                elif isinstance(value, list):
                    if nodes + len(stack) + len(value) > 4096:
                        raise ValueError("角色卡 JSON 结构超过安全导出边界")
                    stack.extend((child, depth + 1) for child in value)
                elif isinstance(value, str):
                    try:
                        value.encode("utf-8")
                    except UnicodeEncodeError as exc:
                        raise ValueError("角色卡含无效 Unicode，拒绝导出") from exc
                    source_chars += len(value)
                elif value is None or isinstance(value, bool):
                    pass
                elif isinstance(value, int):
                    if not -(2**63) <= value <= 2**63 - 1:
                        raise ValueError("角色卡整数超出兼容范围，拒绝导出")
                elif isinstance(value, float):
                    pass  # JSONEncoder rejects NaN and infinities below.
                else:
                    raise ValueError("角色卡包含不支持的 JSON 值，拒绝导出")
                if source_chars > limit:
                    raise ValueError(f"角色卡 JSON 超过 {limit} 字符；拒绝截断导出")
            def dice_json_value(value: object) -> object:
                if isinstance(value, dict):
                    result = {}
                    for key, child in value.items():
                        if child is None or child is False:
                            continue
                        result[key] = dice_json_value(child)
                    return result
                if isinstance(value, list):
                    return [None if child is None or child is False else dice_json_value(child) for child in value]
                return value

            exported = dice_json_value(exported)
            try:
                encoder = json.JSONEncoder(ensure_ascii=False, separators=(",", ":"), allow_nan=False)
                chunks = []
                output_chars = 0
                for chunk in encoder.iterencode(exported):
                    output_chars += len(chunk)
                    if output_chars > limit:
                        raise ValueError(f"角色卡 JSON 超过 {limit} 字符；拒绝截断导出")
                    chunks.append(chunk)
                return "".join(chunks)
            except (TypeError, ValueError, RecursionError) as exc:
                if isinstance(exc, ValueError) and str(exc).startswith("角色卡 JSON 超过"):
                    raise
                raise ValueError("角色卡无法安全转换为 JSON，未导出") from exc
        if action == "stat":
            if len(tokens) != 1:
                raise ValueError("用法：.pc stat（查看当前角色卡的掷骰/检定统计）")
            if not isinstance(cards, dict) or len(cards) > 33:
                raise ValueError("角色卡数据格式无效，无法读取统计")
            name = self._card_name_key(cards, self._active_card_name(user, group_id))
            card = cards.get(name) if name is not None else None
            if not isinstance(card, dict):
                raise ValueError("请先用 .pc create 创建或 .coc7d 生成角色卡")
            attributes = card.get("attributes", {})
            if not isinstance(attributes, dict):
                raise ValueError("角色卡属性数据格式无效，无法读取统计")
            face = self._card_default_die(attributes, fallback=self._user_default_die(sender_id))
            raw_statistics = card.get("statistics") if "statistics" in card else None
            try:
                if "statistics" in card and not validate_card_statistics(raw_statistics):
                    raise CardStatisticsError("角色卡统计数据格式无效，拒绝显示")
                report = format_card_statistics(raw_statistics, face)
            except CardStatisticsError as exc:
                raise ValueError(str(exc)) from exc
            return f"「{name}」统计：\n{report}" if report else f"「{name}」尚无统计记录。"
        if action in {"type", "temp"}:
            if action == "temp" and len(tokens) != 1:
                raise ValueError("用法：.pc temp（显示内置 COC7 静态模板摘要）")
            if action == "type" and len(tokens) > 2:
                raise ValueError("用法：.pc type [COC7]")
            if not isinstance(cards, dict) or len(cards) > 33 or any(not isinstance(key, str) or not isinstance(value, dict) for key, value in cards.items()):
                raise ValueError("角色卡数据格式无效，无法读取模板类型")
            active_name = self._card_name_key(cards, self._active_card_name(user, group_id))
            card = cards.get(active_name) if active_name is not None else None
            if not isinstance(card, dict):
                raise ValueError("请先用 .pc create 创建或 .coc7d 生成角色卡")
            attributes = card.get("attributes", {})
            if not isinstance(attributes, dict):
                raise ValueError("角色卡属性格式无效，拒绝修改模板类型")
            type_keys = [key for key in attributes if isinstance(key, str) and key.casefold() == "__type"]
            if len(type_keys) > 1:
                raise ValueError("角色卡模板类型键存在大小写冲突，拒绝操作")
            stored_type = attributes.get(type_keys[0]) if type_keys else None
            if type_keys and not isinstance(stored_type, str):
                raise ValueError("角色卡模板类型数据无效，拒绝操作")
            if action == "type":
                if len(tokens) == 1:
                    displayed = stored_type if type_keys else card.get("system", "未知")
                    if not isinstance(displayed, str) or len(displayed) > 64 or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in displayed):
                        raise ValueError("角色卡模板类型数据无效，拒绝显示")
                    return f"「{active_name}」模板类：{displayed or '未设置'}"
                if tokens[1].casefold() != "coc7":
                    raise ValueError("本地仅支持 COC7 内置模板类；自定义/XML/JS 模板不会加载")
                def set_type(state):
                    live_players = state.get("players")
                    live_user = live_players.get(sender_id) if isinstance(live_players, dict) else None
                    live_cards = live_user.get("cards") if isinstance(live_user, dict) else None
                    live_active = self._card_name_key(live_cards, self._active_card_name(live_user, group_id)) if isinstance(live_cards, dict) else None
                    if live_active != active_name:
                        raise ValueError("活动角色卡已变化，模板类型未修改")
                    live_card = live_cards.get(active_name) if isinstance(live_cards, dict) else None
                    if not isinstance(live_card, dict):
                        raise ValueError("角色卡不存在，模板类型未修改")
                    live_attributes = live_card.get("attributes", {})
                    if not isinstance(live_attributes, dict):
                        raise ValueError("角色卡属性格式无效，模板类型未修改")
                    live_type_keys = [key for key in live_attributes if isinstance(key, str) and key.casefold() == "__type"]
                    if len(live_type_keys) > 1:
                        raise ValueError("角色卡模板类型键存在大小写冲突，未修改")
                    type_key = live_type_keys[0] if live_type_keys else "__Type"
                    if type_key in live_attributes and not isinstance(live_attributes[type_key], str):
                        raise ValueError("角色卡模板类型数据无效，未修改")
                    live_attributes[type_key] = "COC7"
                    live_card["attributes"] = live_attributes
                    live_card["system"] = "coc7"
                self.store.transact(set_type)
                return f"已将「{active_name}」模板类设为 COC7。"
            effective_type = stored_type.strip() if isinstance(stored_type, str) and stored_type.strip() else card.get("system", "")
            if not isinstance(effective_type, str) or effective_type.casefold() != "coc7":
                raise ValueError("当前角色卡没有可安全显示的本地模板；仅内置 COC7 静态摘要可用")
            fields = "/".join(COC7_TEMPLATE_ATTRIBUTE_NAMES)
            return f"pc模板:COC7 预设属性:{fields}（仅内置静态摘要；不加载外部 XML/JS）"
        if action == "list":
            if not cards:
                return "尚无角色卡。用 .pc create 名称 或 .coc7d 生成。"
            active = self._active_card_name(user, group_id)
            return "角色卡：" + "、".join(f"{n}{'（当前）' if n == active else ''}" for n in cards)
        if action == "grp":
            if len(tokens) != 1:
                raise ValueError("用法：.pc grp")
            if not isinstance(cards, dict) or len(cards) > 33 or any(not isinstance(name, str) or not isinstance(card, dict) for name, card in cards.items()):
                raise ValueError("角色卡数据格式无效，无法列出群映射")
            active_by_group = user.get("active_cards_by_group", {})
            if active_by_group is None:
                active_by_group = {}
            if not isinstance(active_by_group, dict) or len(active_by_group) > 10000:
                raise ValueError("角色卡群映射数据格式无效，未作修改")
            if any(not isinstance(key, str) or not isinstance(name, str) for key, name in active_by_group.items()):
                raise ValueError("角色卡群映射数据格式无效，未作修改")

            def split_group_scope(value: str) -> tuple[str, str]:
                try:
                    identity, end = json.JSONDecoder().raw_decode(value)
                except (json.JSONDecodeError, TypeError):
                    return "", value
                if not isinstance(identity, list) or value[end:end + 1] != ":":
                    return "", value
                return value[:end], value[end + 1:]

            current_scope = split_group_scope(group_id)[0] if isinstance(group_id, str) else ""
            rows: list[str] = []
            default_ref = user.get("legacy_active_card") if "legacy_active_card" in user else user.get("active_card")
            default_name = self._card_name_key(cards, default_ref)
            if default_name is not None:
                rows.append(f"default:{default_name}")
            for scoped_group, card_ref in sorted(active_by_group.items(), key=lambda item: item[0].casefold()):
                mapped_scope, raw_group_id = split_group_scope(scoped_group)
                if mapped_scope != current_scope or raw_group_id.startswith("private:"):
                    continue
                card_name = self._card_name_key(cards, card_ref)
                if card_name is None:
                    raise ValueError("角色卡群映射指向不存在的卡片，已停止显示以保护数据")
                label = re.sub(r"[\x00-\x1f\x7f-\x9f]", "", raw_group_id).strip()[:80]
                if label:
                    rows.append(f"({label}){card_name}")
            if not rows:
                return "当前没有角色卡群映射。"
            budget = max(64, min(4000, self.max_response_length))
            output = "角色卡群映射："
            shown = 0
            for row in rows:
                line = "\n" + row
                if len(output) + len(line) > budget - 48:
                    break
                output += line
                shown += 1
            omitted = len(rows) - shown
            if omitted:
                output += f"\n…另有 {omitted} 项未显示（单条回复上限 {budget} 字）。"
            return output

        if action == "tag" and len(tokens) == 1:
            if not isinstance(cards, dict) or not cards:
                raise ValueError("尚无角色卡；用 .pc create 或 .coc7d 生成角色卡")

            def reset_active_card(state: dict) -> None:
                players = state.get("players")
                user = players.get(sender_id) if isinstance(players, dict) else None
                cards = user.get("cards") if isinstance(user, dict) else None
                if not isinstance(cards, dict) or not cards:
                    raise ValueError("尚无角色卡；用 .pc create 或 .coc7d 生成角色卡")
                if group_id is not None:
                    active_cards = user.get("active_cards_by_group")
                    if not isinstance(active_cards, dict):
                        active_cards = {}
                    active_cards.pop(group_id, None)
                    user["active_cards_by_group"] = active_cards
                fallback = self._card_name_key(cards, user.get("legacy_active_card", user.get("active_card")))
                if fallback is None:
                    fallback = next((name for name in cards if isinstance(name, str)), None)
                user["active_card"] = fallback

            self.store.transact(reset_active_card)
            return "已清除本会话的角色卡选择；将回退到默认卡。"
        if action in {"use", "tag"}:
            name = self._card_name_key(cards, tokens[1]) if len(tokens) == 2 else None
            if name is None:
                raise ValueError("用法：.pc use/tag 现有角色卡名称")
            def select_card(state: dict) -> None:
                user = state["players"].setdefault(sender_id, {"cards": {}, "active_card": None})
                actual_name = self._card_name_key(user.get("cards"), name)
                if actual_name is None:
                    raise ValueError(f"角色卡「{name}」已不存在")
                self._set_active_card_for_group(user, actual_name, group_id)
            self.store.transact(select_card)
            return f"当前角色卡设为「{name}」。"
        if action == "show":
            requested_name = tokens[1] if len(tokens) == 2 else self._active_card_name(user, group_id)
            if len(tokens) > 2:
                raise ValueError("用法：.pc show [角色卡名称]")
            name = self._card_name_key(cards, requested_name)
            card = cards.get(name) if name is not None else None
            if not card:
                raise ValueError("找不到角色卡；用 .pc list 查看")
            stats = card.get("attributes", {})
            return f"{card.get('system', 'coc7').upper()}「{name}」：" + ("，".join(f"{k} {v}" for k, v in sorted(stats.items())) or "尚未设置属性")
        if action == "delete":
            name = self._card_name_key(cards, tokens[1]) if len(tokens) == 2 else None
            if name is None:
                raise ValueError("用法：.pc delete 现有角色卡名称")
            def apply(state: dict) -> None:
                target = state["players"].get(sender_id)
                if not isinstance(target, dict):
                    raise ValueError("当前用户没有角色卡")
                cards = target.get("cards")
                actual_name = self._card_name_key(cards, name)
                if actual_name is None or not isinstance(cards, dict):
                    raise ValueError(f"角色卡「{name}」已不存在")
                cards.pop(actual_name, None)
                scoped = target.get("active_cards_by_group")
                if isinstance(scoped, dict):
                    for group_key, active_name in list(scoped.items()):
                        if isinstance(active_name, str) and active_name.casefold() == actual_name.casefold():
                            scoped.pop(group_key, None)
                fallback = next((card_name for card_name in cards if isinstance(card_name, str)), None)
                for pointer in ("legacy_active_card", "active_card"):
                    active_name = target.get(pointer)
                    if isinstance(active_name, str) and active_name.casefold() == actual_name.casefold():
                        target[pointer] = fallback
            self.store.transact(apply)
            return f"已删除角色卡「{name}」。"
        if action == "set":
            if len(tokens) < 4 or len(tokens[2:]) % 2:
                raise ValueError("用法：.pc set 名称 属性 数值 [属性 数值…]")
            name = self._card_name_key(cards, tokens[1])
            if name is None:
                raise ValueError(f"找不到角色卡「{tokens[1]}」")
            pairs = self._parse_stat_pairs(tokens[2:])
            self._write_stats(sender_id, name, pairs)
            return f"已更新「{tokens[1]}」：" + "，".join(f"{k} {v}" for k, v in pairs.items())
        raise ValueError("未知角色卡操作；发送 .pc help 查看用法")

    @staticmethod
    def _parse_stat_pairs(tokens: list[str]) -> dict[str, int]:
        if not tokens or len(tokens) % 2:
            raise ValueError("属性和值必须成对输入")
        values = {}
        for i in range(0, len(tokens), 2):
            key = Main._normalize_stat(tokens[i])
            if not re.fullmatch(r"[A-Z\u4e00-\u9fff_]{1,20}", key):
                raise ValueError(f"无效属性名：{tokens[i]}")
            try:
                value = int(tokens[i + 1])
            except ValueError as exc:
                raise ValueError(f"属性「{tokens[i]}」必须是整数") from exc
            if not 0 <= value <= 10_000:
                raise ValueError("属性值必须在 0 到 10000 之间")
            values[key] = value
        return values

    def _write_stats(self, sender_id: str, name: str, pairs: dict[str, int]) -> None:
        def apply(state: dict) -> None:
            user = state["players"].setdefault(sender_id, {"cards": {}, "active_card": None})
            cards = user.get("cards")
            actual_name = self._card_name_key(cards, name)
            card = cards.get(actual_name) if isinstance(cards, dict) and actual_name is not None else None
            if not card:
                raise ValueError(f"找不到角色卡「{name}」")
            card.setdefault("attributes", {}).update(pairs)
        self.store.transact(apply)

    def _set_or_show_stats(self, args: str, sender_id: str, group_id: str | None = None) -> str:
        tokens = shlex.split(args)
        if tokens and tokens[0].casefold() == "show":
            parts = args.strip().split(None, 1)
            spec = parts[1].strip() if len(parts) > 1 else ""
            user = self.store.player(sender_id)
            cards = user.get("cards", {})
            if not isinstance(cards, dict) or not cards:
                raise ValueError("请先用 .pc create 创建或 .coc7d 生成角色卡")
            selected_name = None
            attribute = spec
            if spec:
                card_names = sorted((str(card_name) for card_name in cards), key=lambda value: (-len(value), value))
                for candidate in card_names:
                    for separator in ("::", ":", "：", "的"):
                        selector = candidate + separator
                        if spec[:len(selector)].casefold() == selector.casefold():
                            selected_name = candidate
                            attribute = spec[len(selector):].lstrip(" \t:：").strip()
                            break
                    if selected_name is not None:
                        break
                if selected_name is None and any(separator in spec for separator in (":", "：")):
                    unknown_name = re.split(r"[:：]", spec, maxsplit=1)[0].strip()
                    raise ValueError(f"角色卡「{unknown_name or spec}」不存在")
            if selected_name is None:
                selected_name = self._active_card_name(user, group_id)
            selected_name = self._card_name_key(cards, selected_name)
            card = cards.get(selected_name) if selected_name is not None else None
            if not isinstance(card, dict) or not selected_name:
                raise ValueError("请先用 .pc create 创建或 .coc7d 生成角色卡")
            attributes = card.get("attributes", {})
            attributes = attributes if isinstance(attributes, dict) else {}
            if not attribute:
                shown = "，".join(f"{key} {value}" for key, value in sorted(attributes.items(), key=lambda item: str(item[0])))
                return f"「{selected_name}」：" + (shown or "尚无属性")
            wanted = {attribute.casefold(), self._normalize_stat(attribute).casefold()}
            stored_key = next((key for key in attributes if str(key).casefold() in wanted), None)
            if stored_key is None:
                raise ValueError(f"角色卡「{selected_name}」没有属性「{attribute}」")
            return f"「{selected_name}」{stored_key}：{attributes[stored_key]}"
        name, card = self._active_card(sender_id, group_id)
        if not card or not name:
            raise ValueError("请先用 .pc create 创建或 .coc7d 生成角色卡")
        if not tokens:
            stats = card.get("attributes", {})
            return f"「{name}」：" + ("，".join(f"{k} {v}" for k, v in sorted(stats.items())) or "尚无属性")
        formula_match = re.fullmatch(r"&([^:\s]{1,40})\s*[:：]\s*(.{1,160})", args.strip())
        if formula_match:
            formula_name, formula_source = formula_match.groups()
            if not re.fullmatch(r"[A-Za-z0-9_\u4e00-\u9fff-]{1,40}", formula_name) or formula_name.casefold() == "__name":
                raise ValueError("卡片表达式名称无效或保留")
            formula_source = validate_card_expression_source(formula_source)

            def save_formula(state: dict) -> None:
                players = state.get("players")
                user = players.get(sender_id) if isinstance(players, dict) else None
                cards = user.get("cards") if isinstance(user, dict) else None
                actual_name = self._card_name_key(cards, name)
                target = cards.get(actual_name) if isinstance(cards, dict) and actual_name is not None else None
                if not isinstance(target, dict):
                    raise ValueError(f"角色卡「{name}」已不存在")
                stored = target.get("attributes")
                if not isinstance(stored, dict):
                    stored = target["attributes"] = {}
                stored["&" + formula_name] = formula_source

            self.store.transact(save_formula)
            return f"已保存「{name}」的卡片表达式「{formula_name}」：{formula_source}"
        action = tokens[0].casefold()
        if action == "clr":
            if len(tokens) != 1:
                raise ValueError("用法：.st clr")

            def clear_stats(state: dict) -> int:
                players = state.get("players")
                user = players.get(sender_id) if isinstance(players, dict) else None
                cards = user.get("cards") if isinstance(user, dict) else None
                actual_name = self._card_name_key(cards, name)
                target = cards.get(actual_name) if isinstance(cards, dict) and actual_name is not None else None
                if not isinstance(target, dict):
                    raise ValueError(f"找不到角色卡「{name}」")
                attrs = target.get("attributes")
                count = len(attrs) if isinstance(attrs, dict) else 0
                target["attributes"] = {}
                return count

            cleared = self.store.transact(clear_stats)
            return f"已清空「{name}」的 {cleared} 项属性。" if cleared else f"「{name}」没有可清空的属性。"
        if action == "del":
            if len(tokens) != 2:
                raise ValueError("用法：.st del 属性名")
            requested = tokens[1]
            normalized = self._normalize_stat(requested).casefold()
            candidates = {requested.casefold(), normalized}

            def delete_stat(state: dict) -> str | None:
                players = state.get("players")
                user = players.get(sender_id) if isinstance(players, dict) else None
                cards = user.get("cards") if isinstance(user, dict) else None
                actual_name = self._card_name_key(cards, name)
                target = cards.get(actual_name) if isinstance(cards, dict) and actual_name is not None else None
                if not isinstance(target, dict):
                    raise ValueError(f"找不到角色卡「{name}」")
                attrs = target.get("attributes")
                if not isinstance(attrs, dict):
                    target["attributes"] = {}
                    return None
                stored_key = next((key for key in attrs if str(key).casefold() in candidates), None)
                if stored_key is None:
                    formula_names = {"&" + candidate.lstrip("&") for candidate in candidates}
                    stored_key = next((key for key in attrs if isinstance(key, str) and key.casefold() in {value.casefold() for value in formula_names}), None)
                if stored_key is None:
                    return None
                attrs.pop(stored_key)
                return str(stored_key)

            deleted = self.store.transact(delete_stat)
            if deleted is None:
                raise ValueError(f"角色卡「{name}」没有属性「{requested}」")
            return f"已删除「{name}」的属性「{deleted}」。"
        pairs = self._parse_stat_pairs(tokens)
        self._write_stats(sender_id, name, pairs)
        return f"已更新「{name}」：" + "，".join(f"{k} {v}" for k, v in pairs.items())

    def _insanity(self, command: str, group_id: str, sender_id: str) -> str:
        symptom_roll = secrets.randbelow(10) + 1
        duration_roll = secrets.randbelow(10) + 1
        detail_roll = secrets.randbelow(100) + 1 if symptom_roll in {9, 10} else None
        card_name, _ = self._active_card(sender_id, group_id)
        investigator = card_name or self._nickname_for_user(sender_id, group_id) or "调查员"
        text = format_insanity(command, symptom_roll, duration_roll, detail_roll, investigator=investigator)
        self._append_log(group_id, sender_id, command, text)
        return text

    def _jrrp(self, args: str, group_id: str, sender_id: str, is_admin: bool, is_private: bool = False) -> str:
        if self.disable_jrrp:
            return "今日人品已被全局禁用。"
        action = args.strip().split(None, 1)[0].casefold() if args.strip() else ""
        if not is_private and action in {"on", "off"}:
            if not is_admin:
                return "只有群管理员可以切换本群今日人品开关。"
            enabled = action == "on"
            def toggle(group: dict) -> str:
                disabled = group.get("jrrp_disabled") is True
                if enabled:
                    if not disabled:
                        return "already_on"
                    group["jrrp_disabled"] = False
                    return "enabled"
                if disabled:
                    return "already_off"
                group["jrrp_disabled"] = True
                return "disabled"
            status = self._mutate_group(group_id, toggle)
            return {
                "enabled": "已在本群开启今日人品。",
                "disabled": "已在本群关闭今日人品。",
                "already_on": "本群今日人品已经开启。",
                "already_off": "本群今日人品已经关闭。",
            }[status]
        if not is_private and self._group_snapshot(group_id).get("jrrp_disabled") is True:
            return "本群已禁用今日人品。"
        day_key = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        def apply(state: dict) -> int:
            day = state["daily"].setdefault(day_key, {})
            if sender_id not in day:
                day[sender_id] = secrets.randbelow(100) + 1
            for old_day in list(state["daily"]):
                if old_day < day_key:
                    state["daily"].pop(old_day, None)
            return int(day[sender_id])
        return f"你今日的人品是 {self.store.transact(apply)}"

    @staticmethod
    def _archive_session(group: dict, session: dict) -> str:
        sessions = group.setdefault("sessions", {})
        if not isinstance(sessions, dict):
            raise ValueError("团务存档数据无效")
        preferred = str(session.get("id") or session.get("title") or "未命名团务").strip() or "未命名团务"
        session_id = preferred
        suffix = 2
        while session_id in sessions:
            session_id = f"{preferred}#{suffix}"
            suffix += 1
        session["id"] = session_id
        sessions[session_id] = session
        return session_id

    @staticmethod
    def _archive_session_logs(group: dict, session: dict) -> bool:
        existing = group.get("session_log_archives", [])
        archived = session.get("log_archives", [])
        logger = session.get("logger")
        if (not isinstance(existing, list) or len(existing) > 3
                or any(not Main._valid_session_logger(item) for item in existing)
                or not isinstance(archived, list) or len(archived) > 3
                or any(not Main._valid_session_logger(item) for item in archived)
                or (logger is not None and not Main._valid_session_logger(logger))):
            return False
        masters = sorted(Main._session_member_ids(session, "game_masters", "game_master"))
        if len(masters) > 100 or any(len(master) > 128 for master in masters):
            return False
        carry = []
        for item in archived:
            record = copy.deepcopy(item)
            if "session_masters" not in record:
                record["session_masters"] = masters
            record["state"] = "ended"
            carry.append(record)
        if isinstance(logger, dict):
            record = copy.deepcopy(logger)
            record["session_masters"] = masters
            record["state"] = "ended"
            carry.append(record)
        combined = [copy.deepcopy(item) for item in existing] + carry
        if combined:
            group["session_log_archives"] = combined[-3:]
        return True

    @staticmethod
    def _next_session_id(group: dict, title: str) -> str:
        sessions = group.setdefault("sessions", {})
        if not isinstance(sessions, dict):
            raise ValueError("团务存档数据无效")
        counter = group.get("session_counter", 0)
        if isinstance(counter, bool) or not isinstance(counter, int) or counter < 0:
            counter = 0
        while True:
            counter += 1
            session_id = f"{title}#{counter}"
            if session_id not in sessions:
                group["session_counter"] = counter
                return session_id

    @staticmethod
    def _session_member_list(session: dict, key: str) -> list[str]:
        values = session.get(key)
        normalized = list(dict.fromkeys(str(value) for value in values if value is not None)) if isinstance(values, list) else []
        session[key] = normalized
        return normalized

    @staticmethod
    def _session_member_ids(session: dict, key: str, fallback_key: str | None = None) -> set[str]:
        values = session.get(key)
        member_ids = values if isinstance(values, list) else []
        fallback = session.get(fallback_key) if fallback_key else None
        if fallback is not None:
            member_ids = [*member_ids, fallback]
        return {str(value) for value in member_ids if value is not None}

    @staticmethod
    def _session_roulette_faces(session: dict) -> list[int]:
        roulette = session.get("roulette")
        if not isinstance(roulette, dict) or len(roulette) > 100:
            return []
        faces = []
        for key, bag in roulette.items():
            if not isinstance(key, str) or not key.isascii() or not key.isdecimal():
                continue
            face = int(key)
            if str(face) != key or not 1 <= face <= 100 or not isinstance(bag, dict):
                continue
            bag_face, copies = bag.get("face"), bag.get("copies")
            pool, remaining = bag.get("pool"), bag.get("remaining")
            if (type(bag_face) is not int or bag_face != face
                    or type(copies) is not int or copies < 1 or face * copies > 100
                    or not isinstance(pool, list) or len(pool) != face * copies
                    or any(type(value) is not int or not 1 <= value <= face for value in pool)
                    or sorted(pool) != [value for value in range(1, face + 1) for _ in range(copies)]
                    or type(remaining) is not int or not 0 <= remaining <= len(pool)):
                continue
            faces.append(face)
        return sorted(set(faces))

    def _ob(self, args: str, group_id: str, sender_id: str, is_admin: bool, is_private: bool) -> str:
        action = args.strip().split(None, 1)[0].casefold() if args.strip() else ""
        if is_private:
            raise ValueError("旁观命令仅支持群聊：.ob join/on/off/list/clr/exit")
        if action not in {"join", "on", "off", "list", "clr", "exit"}:
            raise ValueError("用法：.ob join/on/off/list/clr/exit")
        if action in {"on", "off"} and not is_admin:
            return "只有群管理员可以开启或关闭旁观加入。"

        def apply(group: dict):
            session = group.get("session")
            active = isinstance(session, dict)
            target = session if active else group
            key = "observers" if active else "room_observers"
            observers = self._session_member_list(target, key)
            if action == "join":
                if active and session.get("no_ob") is True:
                    return "disabled"
                if sender_id in observers:
                    return "already"
                if len(observers) >= 10000:
                    return "full"
                observers.append(sender_id)
                return "joined"
            if action in {"on", "off"}:
                if not active:
                    return "missing"
                if action == "off":
                    if session.get("no_ob") is True:
                        return "off_already"
                    session["no_ob"] = True
                    return "off"
                if session.get("no_ob") is not True:
                    return "on_already"
                session.pop("no_ob", None)
                return "on"
            if action == "list":
                return ("list", list(observers))
            if action == "exit":
                if sender_id not in observers:
                    return "not_member"
                observers.remove(sender_id)
                return "exited"
            masters = self._session_member_ids(session, "game_masters", "game_master") if active else set()
            if not (is_admin or (active and sender_id in masters)):
                return "denied"
            if not observers:
                return "empty"
            observers.clear()
            return "cleared"

        result = self._mutate_group(group_id, apply)
        if isinstance(result, tuple):
            return "当前旁观者：" + ("、".join(result[1]) if result[1] else "暂无。")
        return {
            "disabled": "本桌已关闭旁观加入。",
            "already": "你已在旁观者列表中。",
            "full": "旁观者人数已达上限。",
            "joined": "已加入旁观者列表。",
            "missing": "当前没有进行中的团务，无法修改旁观设置。",
            "off_already": "本桌旁观加入已关闭。",
            "off": "已关闭本桌旁观加入。",
            "on_already": "本桌旁观加入已开启。",
            "on": "已开启本桌旁观加入。",
            "not_member": "你不在旁观者列表中。",
            "exited": "已退出旁观者列表。",
            "denied": "只有群管理员或本桌主持人可以清空旁观名单。",
            "empty": "旁观者列表为空。",
            "cleared": "旁观者列表已清空。",
        }[result]

    @staticmethod
    def _is_upstream_numeric_attribute(value: str) -> bool:
        has_dot = False
        if not value:
            return False
        for index, character in enumerate(value):
            if "0" <= character <= "9":
                continue
            if character in "+-" and index == 0:
                continue
            if character == "." and not has_dot:
                has_dot = True
                continue
            return False
        return True

    @classmethod
    def _parse_game_attribute_value(cls, value: str):
        if not (
            value in {"null", "true", "false"}
            or value.startswith(('"', "[", "{"))
            or cls._is_upstream_numeric_attribute(value)
        ):
            return value

        def reject_non_finite(_token: str):
            raise ValueError("non-finite JSON number")

        try:
            return json.loads(value, parse_constant=reject_non_finite)
        except (ValueError, RecursionError):
            return value

    @staticmethod
    def _format_game_attribute_value(value: object) -> str:
        if value is None:
            return "null"
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, str):
            return value
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        if isinstance(value, float):
            if value.is_integer():
                return str(int(value))
            rendered = f"{value:.6f}"
            decimal = rendered.find(".")
            if decimal >= 0:
                rendered = rendered[:decimal + 3].rstrip("0").rstrip(".")
            return rendered
        return str(value)

    def _game(self, args: str, group_id: str, sender_id: str, is_admin: bool, is_private: bool = False, mention_ids: list[str] | None = None, platform_scope: str = "") -> str:
        action, _, rest = args.partition(" ")
        action = action.casefold() or "status"
        if action in {"start", "new"}:
            if not is_admin:
                return "只有群管理员才能开启团务。"
            title = rest.strip() or "未命名团务"
            title = title[:80]

            def create_session(group: dict) -> str:
                active = group.get("session")
                if isinstance(active, dict) and group.get("session_ref") is None:
                    self._archive_session(group, active)
                group["session_ref"] = None
                session_id = self._next_session_id(group, title)
                group.pop("room_observers", None)
                group["session"] = {
                    "id": session_id,
                    "title": title,
                    "started_at": self._now(),
                    "game_master": sender_id,
                    "game_masters": [sender_id],
                    "participants": [],
                    "observers": [],
                    "roulette": {},
                    "attributes": {},
                    "initiative": [],
                }
                return session_id

            session_id = self._mutate_group(group_id, create_session)
            return f"团务已开启：{title}（桌号 {session_id}）。"

        if action == "master":
            def manage_masters(group: dict):
                session = group.get("session")
                if not isinstance(session, dict):
                    return "missing"
                masters = self._session_member_list(session, "game_masters")
                if session.get("game_master") is not None and str(session["game_master"]) not in masters:
                    masters.append(str(session["game_master"]))
                if sender_id in masters:
                    return "list", masters
                if is_admin or (not masters and is_private):
                    if len(masters) >= 100:
                        return "full"
                    masters.append(sender_id)
                    session["game_masters"] = masters
                    if session.get("game_master") is None:
                        session["game_master"] = sender_id
                    return "promoted", masters
                return "denied"
            result = self._mutate_group(group_id, manage_masters)
            if result == "missing":
                return "当前没有进行中的团务。"
            if result == "full":
                return "团务主持人数量已达上限。"
            if result == "denied":
                return "只有现任主持人、群管理员或可开团的房间管理者可以成为主持人。"
            if result[0] == "promoted":
                return "已加入团务主持人。"
            return "团务主持人：" + ("、".join(result[1]) if result[1] else "无")

        if action == "set":
            setting = rest.strip()
            if not setting:
                raise ValueError("用法：.game set 属性=值；省略 =值可查询")
            if "=" in setting:
                key, _, value = setting.partition("=")
                key, value = key.strip(), value.strip()
                has_value = True
            else:
                parts = setting.split(None, 1)
                key = parts[0]
                value = parts[1].strip() if len(parts) > 1 else ""
                has_value = len(parts) > 1
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,39}", key):
                raise ValueError("属性名格式无效（仅限字母、数字、下划线、点和连字符）")
            reserved = {"id", "title", "started_at", "game_master", "game_masters", "participants", "observers", "initiative", "roulette", "attributes", "no_ob", "logger", "log_archives"}
            if key in reserved:
                raise ValueError("该团务属性受保护，不能直接修改")
            if has_value and len(value) > 256:
                raise ValueError("属性值最多 256 个字符")
            def set_attribute(group: dict):
                session = group.get("session")
                if not isinstance(session, dict):
                    return "missing"
                if sender_id not in self._session_member_ids(session, "game_masters", "game_master"):
                    return "denied"
                if key.casefold() == "rule":
                    if not has_value:
                        current = session.get("rule")
                        if not isinstance(current, int) or isinstance(current, bool) or not 0 <= current <= 7:
                            current = group.get("rule")
                        if not isinstance(current, int) or isinstance(current, bool) or not 0 <= current <= 7:
                            current = self.default_coc_rule
                        return f"当前团务房规：{current}（{COC_RULE_INFO[current][0]}）"
                    if value.casefold() in {"clr", "clear", "null"}:
                        session.pop("rule", None)
                        return "团务房规覆盖已清除。"
                    if not value.isdecimal() or not 0 <= int(value) <= 7:
                        raise ValueError("rule 只能设为 0–7，或 clr 清除团务覆盖")
                    session["rule"] = int(value)
                    return f"当前团务房规已设为 {value}（{COC_RULE_INFO[int(value)][0]}）。"
                attributes = session.get("attributes")
                if not isinstance(attributes, dict):
                    attributes = session["attributes"] = {}
                if not has_value:
                    if key in attributes:
                        current = attributes[key]
                        return f"{key}={self._format_game_attribute_value(current)}"
                    current = session.get(key)
                    return f"{key}={self._format_game_attribute_value(current)}" if current is not None else f"未设置团务属性：{key}"
                if value == "":
                    attributes.pop(key, None)
                    return f"已清除团务属性 {key}。"
                if key not in attributes and len(attributes) >= 50:
                    raise ValueError("每桌最多保存 50 个自定义属性")
                attributes[key] = self._parse_game_attribute_value(value)
                return f"已设置团务属性 {key}。"
            result = self._mutate_group(group_id, set_attribute)
            if result == "missing":
                return "当前没有进行中的团务。"
            if result == "denied":
                return "只有团务主持人可以查询或修改团务属性。"
            return result

        if action == "call":
            def list_players(group: dict):
                session = group.get("session")
                if not isinstance(session, dict):
                    return "missing"
                if sender_id not in self._session_member_ids(session, "game_masters", "game_master"):
                    return "denied"
                return self._session_member_list(session, "participants")
            players = self._mutate_group(group_id, list_players)
            if players == "missing":
                return "当前没有进行中的团务。"
            if players == "denied":
                return "只有团务主持人可以召集玩家。"
            if players and mention_ids is not None:
                mention_ids[:] = players
            return "团务玩家：" + ("、".join(players) if players else "暂无；玩家可用 .game join 加入。")

        if action == "kick":
            target_id = rest.strip().removeprefix("@")
            if not target_id or len(target_id) > 128:
                raise ValueError("用法：.game kick 用户ID")
            def kick_player(group: dict) -> str:
                session = group.get("session")
                if not isinstance(session, dict):
                    return "missing"
                if sender_id not in self._session_member_ids(session, "game_masters", "game_master"):
                    return "denied"
                players = self._session_member_list(session, "participants")
                observers = self._session_member_list(session, "observers")
                player_id = next((member for member in players if member == target_id or self._raw_platform_user_id(member) == target_id), None)
                observer_id = next((member for member in observers if member == target_id or self._raw_platform_user_id(member) == target_id), None)
                removed_player = player_id is not None
                removed_observer = observer_id is not None
                if player_id is not None:
                    players.remove(player_id)
                if observer_id is not None:
                    observers.remove(observer_id)
                return "removed" if removed_player or removed_observer else "not_member"
            result = self._mutate_group(group_id, kick_player)
            return {"missing": "当前没有进行中的团务。", "denied": "只有团务主持人可以踢出玩家或旁观者。", "not_member": "该用户不是本桌玩家或旁观者。", "removed": f"已将 {target_id} 移出本桌。"}[result]

        if action == "rou":
            roulette_arg = rest.strip()
            if not roulette_arg:
                raise ValueError("用法：.game rou 面数[*副本数]|hist|reset|clr")
            def roulette_action(group: dict):
                session = group.get("session")
                if not isinstance(session, dict):
                    return "missing"
                masters = self._session_member_ids(session, "game_masters", "game_master")
                roulette = session.get("roulette")
                if not isinstance(roulette, dict):
                    roulette = session["roulette"] = {}
                action_text = roulette_arg.casefold()
                if action_text == "hist":
                    histories = []
                    for face, bag in sorted(roulette.items(), key=lambda item: int(item[0]) if str(item[0]).isdecimal() else 0):
                        if not isinstance(bag, dict) or not isinstance(bag.get("pool"), list):
                            continue
                        remaining = bag.get("remaining")
                        if type(remaining) is not int or not 0 <= remaining <= len(bag["pool"]):
                            continue
                        drawn = list(reversed(bag["pool"][remaining:]))
                        histories.append(f"D{face}=" + (",".join(map(str, drawn)) if drawn else "（尚未掷出）"))
                    return "轮盘记录：\n" + "\n".join(histories) if histories else "本桌尚未配置轮盘。"
                if action_text in {"reset", "clr"}:
                    if sender_id not in masters:
                        return "denied"
                    if action_text == "clr":
                        roulette.clear()
                        return "已清除本桌轮盘。"
                    for bag in roulette.values():
                        if isinstance(bag, dict) and isinstance(bag.get("pool"), list):
                            bag["remaining"] = len(bag["pool"])
                    return "本桌轮盘已重置。"
                match = re.fullmatch(r"(\d{1,3})(?:\*(\d{1,3}))?", roulette_arg)
                if not match:
                    return "usage"
                face = int(match.group(1))
                copies = int(match.group(2) or 1)
                if not 1 <= face <= 100 or copies < 1 or face * copies > 100:
                    return "bounds"
                if sender_id not in masters:
                    return "denied"
                roulette[str(face)] = _SessionRandomSource._new_bag(face, copies)
                return f"已配置 D{face} 轮盘（{copies} 份，共 {face * copies} 个结果）。"
            result = self._mutate_group(group_id, roulette_action)
            if result == "missing":
                return "当前没有进行中的团务。"
            if result == "denied":
                return "只有团务主持人可以修改或重置轮盘。"
            if result == "usage":
                raise ValueError("用法：.game rou 面数[*副本数]|hist|reset|clr")
            if result == "bounds":
                raise ValueError("轮盘面数须为 1–100，且面数×副本数不能超过 100")
            return result

        if action == "open":
            session_id = rest.strip()
            if not session_id:
                raise ValueError("用法：.game open 桌号")

            def open_session(state: dict) -> str:
                group = self._group_for_state(state, group_id)
                groups = state["groups"]
                rooms = state.setdefault("shared_sessions", {})
                sessions = group.setdefault("sessions", {})
                if not isinstance(sessions, dict) or not isinstance(rooms, dict):
                    return "invalid"
                current_ref = group.get("session_ref")
                current_room = self._shared_session_room(state, group_id, group) if current_ref is not None else None
                active = current_room.get("session") if isinstance(current_room, dict) else group.get("session") if current_ref is None else None
                if current_ref is not None and current_room is None:
                    group["session_ref"] = None
                    group["session"] = None
                if isinstance(active, dict) and active.get("id") == session_id:
                    return "active"

                local_target = sessions.get(session_id)
                if isinstance(local_target, dict):
                    is_game_master = sender_id in self._session_member_ids(local_target, "game_masters", "game_master")
                    is_private_player = is_private and sender_id in self._session_member_ids(local_target, "participants")
                    if not (is_game_master or is_private_player):
                        return "denied"
                    if isinstance(active, dict) and current_ref is None:
                        self._archive_session(group, active)
                    group["session_ref"] = None
                    group.pop("session", None)
                    group["session"] = sessions.pop(session_id)
                    return "opened_local"

                scope = platform_scope
                if not scope or self._group_platform_scope(group_id) != scope:
                    return "missing"
                if self._group_platform_scope(f"{scope}:_") != scope:
                    return "missing"
                candidates = []
                for room_ref, room in rooms.items():
                    if not isinstance(room_ref, str) or re.fullmatch(r"[0-9a-f]{32}", room_ref) is None or not isinstance(room, dict):
                        continue
                    session = room.get("session")
                    if room.get("scope") == scope and isinstance(session, dict) and session.get("id") == session_id:
                        candidates.append(("room", room_ref, session))
                for source_id, source_group in groups.items():
                    if source_id == group_id or not isinstance(source_group, dict) or self._group_platform_scope(source_id) != scope:
                        continue
                    if source_group.get("session_ref") in rooms:
                        continue
                    source_active = source_group.get("session")
                    if isinstance(source_active, dict) and source_active.get("id") == session_id:
                        candidates.append(("active", source_id, session_id, source_active))
                    source_archives = source_group.get("sessions", {})
                    if isinstance(source_archives, dict):
                        for archive_id, saved in source_archives.items():
                            if isinstance(saved, dict) and (archive_id == session_id or saved.get("id") == session_id):
                                candidates.append(("archive", source_id, archive_id, saved))
                if not candidates:
                    return "missing"
                if len(candidates) != 1:
                    return "ambiguous"
                candidate = candidates[0]
                target = candidate[-1]
                is_game_master = sender_id in self._session_member_ids(target, "game_masters", "game_master")
                is_private_player = is_private and sender_id in self._session_member_ids(target, "participants")
                if not (is_game_master or is_private_player):
                    return "denied"

                if candidate[0] == "room":
                    room_ref = candidate[1]
                else:
                    room_ref = secrets.token_hex(16)
                    while room_ref in rooms:
                        room_ref = secrets.token_hex(16)
                    source_group = groups[candidate[1]]
                    if candidate[0] == "active":
                        source_group.pop("session", None)
                        source_group["session_ref"] = room_ref
                    else:
                        source_group.setdefault("sessions", {}).pop(candidate[2], None)
                    rooms[room_ref] = {"scope": scope, "session": target}

                if isinstance(active, dict) and current_ref is None:
                    self._archive_session(group, active)
                group["session_ref"] = room_ref
                group.pop("session", None)
                return "opened_shared"

            result = self.store.transact(open_session)
            if result == "missing":
                return f"找不到当前平台桌号「{session_id}」。"
            if result == "denied":
                return "只有该团务主持人或私聊中的团务参与者可以打开。"
            if result == "active":
                return "该团务已在当前聊天中开启。"
            if result == "ambiguous":
                return f"找到多个桌号为「{session_id}」的团务；请先在来源聊天关闭重名团务。"
            if result == "invalid":
                return "团务存档数据无效；未更改当前团务。"
            if result == "opened_shared":
                return f"团务桌号「{session_id}」已作为共享团务挂载到当前聊天。"
            return f"团务桌号「{session_id}」已在当前聊天中开启。"

        if action == "list":
            group = self._group_snapshot(group_id)
            sessions = group.get("sessions", {})
            if not isinstance(sessions, dict):
                return "团务存档数据无效。"
            lines = []
            active = group.get("session")
            if isinstance(active, dict):
                lines.append(f"{active.get('id', active.get('title', '未命名团务'))}（进行中）")
            lines.extend(
                f"{session_id}（已关闭）"
                for session_id, saved in sorted(sessions.items())
                if isinstance(saved, dict)
            )
            return "团务列表：\n" + "\n".join(lines) if lines else "没有已保存的团务。"

        if action in {"close", "stop", "over"}:
            def finish_session(state: dict) -> str:
                group = self._group_for_state(state, group_id)
                rooms = state.setdefault("shared_sessions", {})
                reference = group.get("session_ref")
                room = self._shared_session_room(state, group_id, group) if reference is not None else None
                session = room.get("session") if isinstance(room, dict) else group.get("session") if reference is None else None
                if not isinstance(session, dict):
                    if reference is not None:
                        group["session_ref"] = None
                        group["session"] = None
                    return "missing"
                game_masters = self._session_member_ids(session, "game_masters", "game_master")
                if not is_admin and sender_id not in game_masters:
                    return "denied"
                if action == "over":
                    if not self._archive_session_logs(group, session):
                        return "log_corrupt"
                    if reference is not None:
                        for attached_id, attached_group in state["groups"].items():
                            if (isinstance(attached_group, dict) and attached_group.get("session_ref") == reference
                                    and self._group_platform_scope(attached_id) == room.get("scope")):
                                attached_group["session_ref"] = None
                                attached_group.pop("session", None)
                        rooms.pop(reference, None)
                    group["session_ref"] = None
                    group["session"] = None
                    return "deleted"
                if reference is not None:
                    group["session_ref"] = None
                    group.pop("session", None)
                    return ("detached", str(session.get("id") or session.get("title") or "未命名团务"))
                session_id = self._archive_session(group, session)
                group["session"] = None
                return session_id

            result = self.store.transact(finish_session)
            if result == "missing":
                return "当前没有进行中的团务。"
            if result == "denied":
                return "只有团务主持人或群管理员可以关闭/删除团务。"
            if result == "log_corrupt":
                return "团务日志归档状态异常；为避免丢失日志，未删除团务。"
            if result == "deleted":
                return "当前团务已删除。"
            if isinstance(result, tuple) and len(result) == 2 and result[0] == "detached":
                return f"已从当前聊天解除共享团务挂载（桌号 {result[1]}）；该团务仍可在其他聊天使用。"
            return f"当前团务已保存并关闭（桌号 {result}）；可用 .game open {result} 重开。"

        if action in {"join", "leave", "exit"}:
            def apply(group: dict) -> str:
                session = group.get("session")
                if not isinstance(session, dict):
                    return "missing"
                people = self._session_member_list(session, "participants")
                if action == "join":
                    if sender_id not in people:
                        if len(people) >= 1000:
                            return "full"
                        people.append(sender_id)
                    return "joined"
                if sender_id in people:
                    people.remove(sender_id)
                    return "left_player"
                if action == "exit":
                    masters = self._session_member_list(session, "game_masters")
                    legacy_master = str(session.get("game_master") or "")
                    if sender_id in masters or legacy_master == sender_id:
                        if sender_id in masters:
                            masters.remove(sender_id)
                        session["game_masters"] = masters
                        if legacy_master == sender_id:
                            session["game_master"] = masters[0] if masters else None
                        return "left_master"
                return "not_member"
            result = self._mutate_group(group_id, apply)
            if result == "missing":
                return "当前没有进行中的团务；使用 .game new 开启。"
            if result == "full":
                return "本桌玩家已达上限。"
            if result == "not_member":
                return "你不是本桌玩家或主持人，无法退出。"
            return "已加入团务。" if result == "joined" else "已退出团务。"

        session = self._group_snapshot(group_id).get("session")
        if not isinstance(session, dict):
            return "当前没有进行中的团务。用 .game new [团名] 开启，.game open 桌号重开，.game list 查看存档。"
        if action not in {"status", "state"}:
            raise ValueError("用法：.game new|open|list|master|set|call|join|exit|kick|rou|status|close|over")
        players_raw = self._session_member_list(session, "participants")
        masters_raw = sorted(self._session_member_ids(session, "game_masters", "game_master"))
        observers_raw = sorted(self._session_member_ids(session, "observers"))

        def roster(values: list[str] | set[str]) -> str:
            safe_values = sorted({value.strip() for value in list(values)[:1000]
                                  if isinstance(value, str) and value.strip() and len(value) <= 512})
            shown = []
            for user_id in safe_values[:10]:
                nickname = self._nickname_for_user(user_id, group_id)
                label = nickname or self._raw_platform_user_id(user_id)
                label = re.sub(r"[\x00-\x1f\x7f]", "", str(label)).strip()[:48]
                shown.append(label or "未知用户")
            suffix = f" 等 {len(safe_values) - 10} 人" if len(safe_values) > 10 else ""
            if len(values) > 1000:
                suffix += "（名单过长，已截断）"
            return "、".join(shown) + suffix if shown else "无"

        def status_value(value: object, fallback: str) -> str:
            if not isinstance(value, str):
                return fallback
            clean = re.sub(r"[\x00-\x1f\x7f]", "", value).strip()[:80]
            return clean or fallback

        title = status_value(session.get("title"), "未命名")
        session_id = status_value(session.get("id"), "旧团务")
        started_at = status_value(session.get("started_at"), "?")
        lines = [f"团务：{title}；桌号：{session_id}"]
        effective_rule = self._effective_coc_rule(group_id)
        lines.append(f"检定房规：{effective_rule}（{COC_RULE_INFO[effective_rule][0]}）")
        rule_name = status_value(session.get("rule_name"), "")
        if rule_name:
            lines.append(f"团务规则名：{rule_name}")
        lines.append(f"主持人：{roster(set(masters_raw))}")
        lines.append(f"玩家（{len(players_raw)}）：{roster(players_raw)}")
        if observers_raw:
            lines.append(f"旁观者：{roster(set(observers_raw))}")
        logger = session.get("logger")
        if self._valid_session_logger(logger) and logger.get("state") == "active":
            lines.append(f"日志记录中：{logger['name']}")
        faces = self._session_roulette_faces(session)
        if faces:
            lines.append("轮盘骰：" + "/".join(f"D{face}" for face in faces))
        lines.append(f"开始于：{started_at}。")
        return "\n".join(lines)

    @staticmethod
    def _initiative_items(group: dict) -> list:
        session = group.get("session")
        if isinstance(session, dict):
            entries = session.get("initiative")
            if not isinstance(entries, list):
                entries = []
                session["initiative"] = entries
            return entries
        entries = group.get("initiative")
        if not isinstance(entries, list):
            entries = []
            group["initiative"] = entries
        return entries

    def _roll_initiative(self, args: str, group_id: str, sender_id: str, is_private: bool) -> str:
        if is_private:
            return "先攻掷骰仅支持群聊；用法：.ri [修正或骰子表达式] [次数#名称]。"

        text = args.strip()
        expression = "D20"
        name_text = text
        if text:
            token, separator, tail = text.partition(" ")
            upper = token.upper()
            if token in {"+", "-"}:
                modifier, modifier_separator, remainder = tail.partition(" ")
                if not modifier:
                    raise ValueError("用法：.ri [修正或骰子表达式] [次数#名称]")
                expression = f"D20{token}{modifier}"
                name_text = remainder if modifier_separator else ""
            elif token.startswith(("+", "-")):
                expression = f"D20{token}"
                name_text = tail if separator else ""
            elif upper in {"D", "F", "DF"} or re.match(r"^\d*[dD](?:\d+|[fF])", token):
                expression = token
                name_text = tail if separator else ""

        count = 1
        name = name_text.strip()
        if "#" in name:
            if name.count("#") != 1:
                raise ValueError("用法：.ri [修正或骰子表达式] [次数#名称]")
            count_expression, name = name.split("#", 1)
            if count_expression.strip():
                count = self.engine.roll(count_expression.strip(), 20).total
            name = name.strip()
        if not 1 <= count <= 10:
            raise ValueError("先攻掷骰次数必须在 1 到 10 之间")
        if len(name) > 80:
            raise ValueError("先攻名称最多 80 个字符")
        if not name:
            _, card = self._active_card(sender_id, group_id)
            card_name = card.get("name") if isinstance(card, dict) else None
            nickname = self._nickname_for_user(sender_id, group_id)
            name = str(card_name) if card_name and card_name != "角色卡" else (nickname or sender_id)

        rolled = []
        for index in range(count):
            result = self.engine.roll(expression, 20)
            entry_name = name if count == 1 else f"{name}{index + 1}"
            rolled.append({"name": entry_name, "score": result.total, "expression": result.expression, "detail": result.format()})

        def apply(group: dict) -> None:
            entries = self._initiative_items(group)
            for record in rolled:
                entries[:] = [entry for entry in entries if not isinstance(entry, dict) or entry.get("name") != record["name"]]
                entries.append(record)
            entries.sort(key=lambda entry: (-int(entry.get("score", 0)), str(entry.get("name", ""))) if isinstance(entry, dict) else (0, str(entry)))

        self._mutate_group(group_id, apply)
        response = "先攻掷骰：\n" + "\n".join(f"{item['name']}：{item['detail']}" for item in rolled)
        self._append_log(group_id, sender_id, f"ri {args}", response)
        return response

    def _initiative(self, args: str, group_id: str) -> str:
        action, _, rest = args.partition(" ")
        action = action.casefold() or "show"
        if action == "clear":
            self._mutate_group(group_id, lambda group: self._initiative_items(group).clear())
            return "先攻表已清空。"
        if action == "add":
            match = re.fullmatch(r"\s*(-?\d+)\s+(.{1,80})\s*", rest)
            if not match:
                raise ValueError("用法：.init add 先攻数 名称")
            score, name = int(match.group(1)), match.group(2).strip()
            def apply(group: dict) -> None:
                items = self._initiative_items(group)
                items[:] = [item for item in items if not isinstance(item, dict) or item.get("name") != name]
                items.append({"name": name, "score": score})
                items.sort(key=lambda item: (-int(item.get("score", 0)), str(item.get("name", ""))) if isinstance(item, dict) else (0, str(item)))
            self._mutate_group(group_id, apply)
            return f"已记录先攻：{name} {score}。"
        if action == "remove":
            name = rest.strip()
            if not name:
                raise ValueError("用法：.init remove 名称")
            removed = self._mutate_group(group_id, lambda group: self._remove_initiative(group, name))
            return f"已移除 {name}。" if removed else f"先攻表中没有 {name}。"
        entries = self._initiative_items(self._group_snapshot(group_id))
        if not entries:
            return "先攻表为空。用 .init add 先攻数 名称 添加。"
        def display(item):
            if isinstance(item, dict):
                return f"{item.get('name', '?')}({item.get('score', '?')})"
            return str(item)
        return "先攻顺序：" + " → ".join(display(item) for item in entries)

    @staticmethod
    def _remove_initiative(group: dict, name: str) -> bool:
        items = Main._initiative_items(group)
        before = len(items)
        items[:] = [item for item in items if not isinstance(item, dict) or item.get("name") != name]
        return len(items) != before

    @staticmethod
    def _valid_session_deck_map(value: object) -> bool:
        if not isinstance(value, dict) or len(value) > 10:
            return False
        folded = set()
        for name, deck in value.items():
            if not isinstance(name, str) or not 1 <= len(name) <= 40 or name != name.strip() or any(ord(char) < 32 or 0x7F <= ord(char) <= 0x9F for char in name):
                return False
            if name.casefold() in folded:
                return False
            folded.add(name.casefold())
            if not isinstance(deck, dict) or set(deck) != {"cards", "remaining"}:
                return False
            cards, remaining = deck.get("cards"), deck.get("remaining")
            if not isinstance(cards, list) or not 1 <= len(cards) <= 1024 or not isinstance(remaining, list) or len(remaining) > len(cards):
                return False
            if any(not isinstance(card, str) or not 1 <= len(card) <= 120 or any(ord(char) < 32 or 0x7F <= ord(char) <= 0x9F for char in card) for card in cards + remaining):
                return False
            counts = {}
            for card in cards:
                counts[card] = counts.get(card, 0) + 1
            for card in remaining:
                left = counts.get(card, 0)
                if left <= 0:
                    return False
                counts[card] = left - 1
        return True

    @staticmethod
    def _session_deck_key(decks: dict, requested: str) -> str | None:
        if requested in decks:
            return requested
        matches = [name for name in decks if isinstance(name, str) and name.casefold() == requested.casefold()]
        return matches[0] if len(matches) == 1 else None

    def _expand_session_deck_items(self, raw_items: list[str], *, limit: int, max_raw_items: int | None = 256) -> list[str]:
        if max_raw_items is not None and len(raw_items) > max_raw_items:
            raise ValueError(f"牌堆最多包含 {max_raw_items} 个输入项")
        expanded = []
        dynamic_rng = None

        class BudgetedRandomSource:
            def __init__(self, source):
                self.source = source
                self.draws = 0

            def randint(self, low: int, high: int) -> int:
                self.draws += 1
                if self.draws > MAX_DECK_WEIGHT_DRAWS:
                    raise DiceError(f"动态权重累计随机抽取超过 {MAX_DECK_WEIGHT_DRAWS} 次")
                return self.source.randint(low, high)

        for raw in raw_items:
            item = raw.strip()
            if not item:
                continue
            if len(item) > 4096 or any(ord(char) < 32 or 0x7F <= ord(char) <= 0x9F for char in item):
                raise ValueError("牌面为空、过长或包含控制字符")
            if "::" in item:
                parts = item.split("::", 2)
                if len(parts) != 3:
                    raise ValueError("权重牌语法为 标签::权重::牌面；动态权重须写成 [安全骰式]")
                label, raw_weight, card = parts
                card = card.strip()
                if not card or len(card) > 120 or any(ord(char) < 32 or 0x7F <= ord(char) <= 0x9F for char in card):
                    raise ValueError("权重牌的牌面须为 1–120 字且不含控制字符")
                if raw_weight.isascii() and raw_weight.isdecimal():
                    if len(raw_weight) > 3 or not 1 <= int(raw_weight) <= 999:
                        raise ValueError("固定权重须为 1–999")
                    weight = int(raw_weight)
                else:
                    expression_match = re.fullmatch(r"\[([^\[\]{}]{1,160})\]", raw_weight)
                    if expression_match is None:
                        raise ValueError("动态权重须写成 [骰式]；模板引用和脚本不执行")
                    expression = expression_match.group(1).strip()
                    if not expression:
                        raise ValueError("动态权重骰式不能为空")
                    if dynamic_rng is None:
                        base_rng = getattr(self.engine, "_rng", None)
                        if not callable(getattr(base_rng, "randint", None)):
                            raise ValueError("当前骰引擎不提供动态权重随机源")
                        dynamic_rng = BudgetedRandomSource(base_rng)
                    try:
                        weight = self.engine.roll(expression, rng=dynamic_rng).total
                    except DiceError as exc:
                        raise ValueError(f"动态权重骰式无效：{exc}") from exc
                    if type(weight) is not int or not 1 <= weight <= 999:
                        raise ValueError("动态权重结果须为 1–999")
                if len(expanded) + weight > limit:
                    raise ValueError(f"展开后的牌数超过 {limit} 张安全上限")
                expanded.extend([card] * weight)
            else:
                if len(item) > 120:
                    raise ValueError("单张牌最多 120 个字符")
                expanded.append(item)
                if len(expanded) > limit:
                    raise ValueError(f"展开后的牌数超过 {limit} 张安全上限")
        if not expanded:
            raise ValueError("牌堆不能为空")
        return expanded

    def _session_deck_command(self, args: str, group_id: str, sender_id: str, is_admin: bool) -> str:
        action, _, rest = args.partition(" ")
        action = action.casefold() or "show"
        rest = rest.strip()
        if action in {"list", "show"}:
            group = self._group_snapshot(group_id)
            session = group.get("session")
            if not isinstance(session, dict):
                raise ValueError("当前没有进行中的团务。")
            if "decks" not in session:
                decks = {}
            else:
                decks = session.get("decks")
                if not self._valid_session_deck_map(decks):
                    raise ValueError("团务牌堆状态损坏；为避免覆盖，操作已拒绝。")
            if action == "show" and rest:
                key = self._session_deck_key(decks, rest)
                if key is None:
                    raise ValueError("找不到该团务牌堆。")
                deck = decks[key]
                remaining = deck["remaining"]
                visible = remaining[:20]
                text = "；".join(visible) if visible else "（已抽完）"
                omitted = f"；另有 {len(remaining) - len(visible)} 张未显示" if len(remaining) > len(visible) else ""
                return f"团务牌堆「{key}」剩余 {len(remaining)}/{len(deck['cards'])} 张：{text}{omitted}"
            if not decks:
                return "当前团务没有自定义牌堆。"
            summary = "；".join(f"{name}（剩 {len(deck['remaining'])}/{len(deck['cards'])} 张）" for name, deck in sorted(decks.items(), key=lambda item: item[0].casefold()))
            return "当前团务牌堆：" + summary

        name = ""
        cards = None
        if action in {"create", "new"}:
            if action == "create":
                if "|" not in rest:
                    raise ValueError("用法：.deck create 牌堆名 | 牌1 | 牌2")
                name, _, item_text = rest.partition("|")
                name = name.strip()
                raw_items = item_text.split("|")
                cards = self._expand_session_deck_items(raw_items, limit=1024)
            else:
                left, separator, item_text = rest.partition("=")
                if separator:
                    name = left.strip()
                    raw_items = item_text.split("|")
                else:
                    name = "new"
                    raw_items = rest.split("|")
                cards = self._expand_session_deck_items(raw_items, limit=1024)
            if not 1 <= len(name) <= 40 or name != name.strip() or any(ord(char) < 32 or 0x7F <= ord(char) <= 0x9F for char in name):
                raise ValueError("团务牌堆名须为 1–40 个无控制字符文本")
        elif action == "set":
            name, separator, source = rest.partition("=")
            name, source = name.strip(), source.strip()
            if not separator or not 1 <= len(name) <= 40 or name != name.strip():
                raise ValueError("用法：.deck set 牌堆名=公共牌堆名|range 起点 [终点 [步长]]（终点缺省为 0，步长缺省为 1）")
            if source.casefold() in {"member", "群成员"}:
                raise ValueError("AstrBot 通用接口不提供当前群成员列表；群成员牌堆明确拒绝创建。")
            range_match = re.fullmatch(r"range\s+(-?[0-9]{1,16})(?:\s+(-?[0-9]{1,16}))?(?:\s+([0-9]{1,16}))?", source, re.IGNORECASE)
            if range_match:
                start, end = int(range_match.group(1)), int(range_match.group(2) or "0")
                step = int(range_match.group(3) or "1")
                if start == end or step <= 0:
                    raise ValueError("数字范围必须有不同端点且步长为正数")
                if start > end:
                    start, end = end, start
                if end - start > 1000 * step:
                    raise ValueError("数字范围超过上游牌堆安全上限")
                cards = [str(value) for value in range(start, end + 1, step)]
                if not 1 <= len(cards) <= 1024:
                    raise ValueError("数字范围展开后必须为 1–1024 项")
            else:
                source_name = next((key for key in BUILTIN_DECKS if key.casefold() == source.casefold()), None)
                if source_name is not None:
                    cards = self._expand_session_deck_items(list(BUILTIN_DECKS[source_name]), limit=1024, max_raw_items=None)
                else:
                    template_name = find_builtin_public_deck(source)
                    template_entries = BUILTIN_TEMPLATE_DECKS.get(template_name) if template_name is not None else None
                    if template_entries is None:
                        raise ValueError("公共牌堆来源不存在或不支持；群成员牌堆明确拒绝复制")
                    cards = self._expand_session_deck_items(list(template_entries), limit=1024, max_raw_items=None)
            if not name or any(ord(char) < 32 or 0x7F <= ord(char) <= 0x9F for char in name):
                raise ValueError("团务牌堆名格式无效")
        elif action in {"reset", "delete", "del", "clear", "clr"}:
            name = rest
            if action not in {"clear", "clr"} and not name:
                raise ValueError(f"用法：.deck {action} 牌堆名")
        else:
            raise ValueError("活动团务牌堆用法：.deck new/set/create/show/list/reset/del/clr")

        def apply(group: dict):
            session = group.get("session")
            if not isinstance(session, dict):
                return "missing", None
            masters = self._session_member_ids(session, "game_masters", "game_master")
            if not (is_admin or sender_id in masters):
                return "denied", None
            if "decks" not in session:
                decks = {}
            else:
                decks = session.get("decks")
                if not self._valid_session_deck_map(decks):
                    return "invalid", None
            key = self._session_deck_key(decks, name) if name else None
            if action in {"create", "new", "set"}:
                if key is None and len(decks) >= 10:
                    return "full", None
                saved_key = key or name
                decks[saved_key] = {"cards": list(cards), "remaining": list(cards)}
                session["decks"] = decks
                return "saved", (saved_key, len(cards))
            if action == "reset":
                if key is None:
                    return "not_found", None
                decks[key]["remaining"] = list(decks[key]["cards"])
                session["decks"] = decks
                return "reset", key
            if action in {"delete", "del"}:
                if key is None:
                    return "not_found", None
                del decks[key]
                session["decks"] = decks
                return "deleted", key
            if action in {"clear", "clr"}:
                session["decks"] = {}
                return "cleared", None
            return "unsupported", None

        result, payload = self._mutate_group(group_id, apply)
        if result == "missing":
            return "当前没有进行中的团务。"
        if result == "denied":
            return "团务牌堆修改仅限群管理员或当前团务主持人。"
        if result == "invalid":
            return "团务牌堆状态损坏；为避免覆盖，操作已拒绝。"
        if result == "full":
            return "当前团务最多保存 10 个自定义牌堆。"
        if result == "saved":
            return f"团务牌堆「{payload[0]}」已保存，共 {payload[1]} 张。"
        if result == "reset":
            return f"团务牌堆「{payload}」已重置。"
        if result == "deleted":
            return f"团务牌堆「{payload}」已删除。"
        if result == "cleared":
            return "当前团务自定义牌堆已清空。"
        return "团务牌堆操作未完成。"

    def _deck(self, args: str, group_id: str, sender_id: str = "", is_admin: bool = False) -> str:
        snapshot = self._group_snapshot(group_id)
        active = snapshot.get("session")
        if active is not None:
            if not isinstance(active, dict):
                raise ValueError("团务状态损坏；牌堆操作已拒绝。")
            return self._session_deck_command(args, group_id, sender_id, is_admin)
        action, _, rest = args.partition(" ")
        action = action.casefold() or "list"
        if action == "create":
            if "|" not in rest:
                raise ValueError("用法：.deck create 牌堆名 | 牌1 | 牌2")
            name, raw_cards = rest.split("|", 1)
            name = name.strip()
            cards = [card.strip() for card in raw_cards.split("|") if card.strip()]
            if not 1 <= len(name) <= 40 or not 1 <= len(cards) <= 500:
                raise ValueError("牌堆名最多 40 字，牌数必须在 1 到 500 之间")
            if any(len(card) > 120 for card in cards):
                raise ValueError("单张牌最多 120 个字符")
            deck = {"cards": cards, "remaining": list(cards)}
            self._mutate_group(group_id, lambda group: group.setdefault("decks", {}).__setitem__(name, deck))
            return f"已创建牌堆「{name}」，共 {len(cards)} 张牌。"
        if action == "new":
            left, separator, raw_items = rest.partition("=")
            name = left.strip() if separator else "new"
            raw_cards = raw_items.split("|") if separator else rest.split("|")
            if not 1 <= len(name) <= 40:
                raise ValueError("牌堆名须为 1–40 个字符")
            cards = self._expand_session_deck_items(raw_cards, limit=1024)
            def save_group_deck(group: dict) -> str:
                decks = group.get("decks")
                if not isinstance(decks, dict):
                    raise ValueError("本群牌堆状态损坏；未修改。")
                existing = next((key for key in decks if isinstance(key, str) and key.casefold() == name.casefold()), None)
                if existing is None and len(decks) >= 100:
                    raise ValueError("本群最多保存 100 个自定义牌堆")
                key = existing or name
                decks[key] = {"cards": list(cards), "remaining": list(cards)}
                return key
            saved = self._mutate_group(group_id, save_group_deck)
            return f"已新建牌堆「{saved}」，共 {len(cards)} 张牌。"
        if action == "set":
            name, separator, source = rest.partition("=")
            name, source = name.strip(), source.strip()
            if not separator or not 1 <= len(name) <= 40:
                raise ValueError("用法：.deck set 牌堆名=静态公共牌堆|range 起点 [终点 [步长]]（终点缺省为 0，步长缺省为 1）")
            if source.casefold() in {"member", "群成员"}:
                raise ValueError("AstrBot 通用接口不提供当前群成员列表；群成员牌堆明确拒绝创建。")
            range_match = re.fullmatch(r"range\s+(-?[0-9]{1,16})(?:\s+(-?[0-9]{1,16}))?(?:\s+([0-9]{1,16}))?", source, re.IGNORECASE)
            if range_match:
                start, end = int(range_match.group(1)), int(range_match.group(2) or "0")
                step = int(range_match.group(3) or "1")
                if start == end or step <= 0:
                    raise ValueError("数字范围必须有不同端点且步长为正数")
                if start > end:
                    start, end = end, start
                if end - start > 1000 * step:
                    raise ValueError("数字范围超过上游牌堆安全上限")
                cards = [str(value) for value in range(start, end + 1, step)]
                if not 1 <= len(cards) <= 1024:
                    raise ValueError("数字范围展开后必须为 1–1024 项")
            else:
                source_name = next((key for key in BUILTIN_DECKS if key.casefold() == source.casefold()), None)
                if source_name is None:
                    if find_builtin_public_deck(source) is not None:
                        raise ValueError("模板公共牌堆含动态展开；当前只复制静态公共牌堆，不执行模板")
                    raise ValueError("静态公共牌堆不存在；动态权重/模板和群成员来源不支持复制")
                cards = self._expand_session_deck_items(list(BUILTIN_DECKS[source_name]), limit=1024, max_raw_items=None)
            def save_static_group_deck(group: dict) -> str:
                decks = group.get("decks")
                if not isinstance(decks, dict):
                    raise ValueError("本群牌堆状态损坏；未修改。")
                existing = next((key for key in decks if isinstance(key, str) and key.casefold() == name.casefold()), None)
                if existing is None and len(decks) >= 100:
                    raise ValueError("本群最多保存 100 个自定义牌堆")
                key = existing or name
                decks[key] = {"cards": list(cards), "remaining": list(cards)}
                return key
            saved = self._mutate_group(group_id, save_static_group_deck)
            return f"已从静态来源保存牌堆「{saved}」，共 {len(cards)} 张牌。"
        if action == "show" and rest.strip():
            decks = self._group_snapshot(group_id).get("decks", {})
            name = rest.strip()
            key = next((candidate for candidate in decks if isinstance(candidate, str) and candidate.casefold() == name.casefold()), None) if isinstance(decks, dict) else None
            deck = decks.get(key) if key is not None else None
            remaining = deck.get("remaining") if isinstance(deck, dict) else deck
            if not isinstance(remaining, list):
                raise ValueError("找不到该牌堆或牌堆状态损坏。")
            visible = remaining[:20]
            return f"牌堆「{key}」剩余 {len(remaining)} 张：" + ("；".join(visible) if visible else "（已抽完）")
        if action == "reset":
            name = rest.strip()
            if not name:
                raise ValueError("用法：.deck reset 牌堆名")
            return f"牌堆「{name}」已重置。" if self._reset_deck(name, group_id) else "找不到此牌堆。"
        if action in {"delete", "del"}:
            name = rest.strip()
            if not name:
                raise ValueError(f"用法：.deck {action} 牌堆名")
            def delete_group_deck(group: dict) -> bool:
                decks = group.get("decks")
                if not isinstance(decks, dict):
                    raise ValueError("本群牌堆状态损坏；未修改。")
                key = next((candidate for candidate in decks if isinstance(candidate, str) and candidate.casefold() == name.casefold()), None)
                if key is None:
                    return False
                del decks[key]
                return True
            found = self._mutate_group(group_id, delete_group_deck)
            return "牌堆已删除。" if found else "找不到此牌堆。"
        if action in {"clear", "clr"}:
            self._mutate_group(group_id, lambda group: group["decks"].clear())
            return "本群自定义牌堆已清空。"
        decks = self._group_snapshot(group_id).get("decks", {})
        if not isinstance(decks, dict):
            decks = {}
        def count(deck):
            if isinstance(deck, dict):
                remaining = deck.get("remaining", [])
                return len(remaining) if isinstance(remaining, list) else 0
            return len(deck) if isinstance(deck, (list, tuple)) else 0
        custom = "；".join(f"{name}（剩 {count(deck)} 张）" for name, deck in sorted(decks.items(), key=lambda item: str(item[0]).casefold()))
        public = "、".join(sorted(BUILTIN_PUBLIC_DECK_NAMES, key=str.casefold))
        if custom:
            return f"自定义牌堆：{custom}\n内置公共牌堆（抽取后不消耗）：{public}"
        return f"没有自定义牌堆。用 .deck create 名称 | 牌1 | 牌2 创建。\n内置公共牌堆（抽取后不消耗）：{public}"

    def _reset_deck(self, name: str, group_id: str) -> bool:
        def apply(group: dict) -> bool:
            deck = group.setdefault("decks", {}).get(name)
            if not isinstance(deck, dict):
                return False
            cards = deck.get("cards")
            if not isinstance(cards, list):
                return False
            deck["remaining"] = list(cards)
            return True
        return self._mutate_group(group_id, apply)

    def _draw(self, args: str, group_id: str, is_private: bool = False) -> str | HiddenCheckReply:
        text = args.strip()
        hidden = False
        if text.startswith("_"):
            hidden = True
            text = text.lstrip("_").strip()
        elif re.match(r"h(?:\s|$)", text, re.IGNORECASE):
            hidden, text = True, re.sub(r"^h\s*", "", text, count=1, flags=re.IGNORECASE).strip()
        if is_private:
            hidden = False
        count = 1
        pieces = text.rsplit(None, 1)
        if len(pieces) == 2 and pieces[1].isascii() and pieces[1].isdecimal():
            raw_count = pieces[1]
            if len(raw_count) > 2:
                raise ValueError("抽牌次数须为 1–10")
            count = int(raw_count)
            text = pieces[0].strip()
        if not text:
            raise ValueError("用法：.draw [h]牌堆名 [次数 1–10]；前缀 _牌堆名 也可隐藏")
        if not 1 <= count <= 10:
            raise ValueError("抽牌次数须为 1–10")
        name = text
        if len(name) > 80:
            raise ValueError("牌堆名最多 80 个字符")

        def roll_template_expression(expression: str) -> str | None:
            try:
                return str(self.engine.roll(expression, self.default_die).total)
            except DiceError:
                return None

        group_snapshot = self._group_snapshot(group_id)
        active_session = group_snapshot.get("session")
        if active_session is not None:
            if not isinstance(active_session, dict):
                raise ValueError("团务状态损坏；抽牌已拒绝。")
            if "decks" in active_session:
                session_decks = active_session.get("decks")
                if not self._valid_session_deck_map(session_decks):
                    raise ValueError("团务牌堆状态损坏；为避免覆盖，抽牌已拒绝。")
            else:
                session_decks = {}
            session_name = self._session_deck_key(session_decks, name)
            if session_name is not None:
                def draw_session_cards(group: dict) -> tuple[list[str], int]:
                    session = group.get("session")
                    if not isinstance(session, dict):
                        raise ValueError("当前团务已变化；请重试抽牌。")
                    decks = session.get("decks", {})
                    if not self._valid_session_deck_map(decks):
                        raise ValueError("团务牌堆状态损坏；抽牌已拒绝。")
                    key = self._session_deck_key(decks, session_name)
                    if key is None:
                        raise ValueError("团务牌堆已变化；请重试抽牌。")
                    remaining = decks[key]["remaining"]
                    raw_drawn = []
                    while remaining and len(raw_drawn) < count:
                        raw_drawn.append(remaining.pop(secrets.randbelow(len(remaining))))
                    if not raw_drawn:
                        raise ValueError("该团务牌堆已抽完；用 .deck reset 重置。")
                    drawn = expand_builtin_public_deck_texts(raw_drawn, roll=roll_template_expression)
                    session["decks"] = decks
                    return drawn, len(remaining)
                drawn, remaining_count = self._mutate_group(group_id, draw_session_cards)
                if len(drawn) == 1:
                    result = f"从团务牌堆「{session_name}」抽到：{drawn[0]}（剩余 {remaining_count} 张）"
                else:
                    result = f"从团务牌堆「{session_name}」抽到：" + " | ".join(drawn) + f"（剩余 {remaining_count} 张；本次抽取 {len(drawn)} 张）"
                return HiddenCheckReply(result, "隐藏抽牌已执行；结果仅尝试私信发送，若未收到请检查平台私聊权限。") if hidden else result

        stored_decks = group_snapshot.get("decks", {})
        custom_name = next((candidate for candidate in stored_decks if isinstance(candidate, str) and candidate.casefold() == name.casefold()), None) if isinstance(stored_decks, dict) else None
        if custom_name is not None:
            def draw_group_cards(group: dict) -> tuple[list[str], int]:
                decks = group.get("decks")
                if not isinstance(decks, dict):
                    raise ValueError("自定义牌堆状态格式无效")
                key = next((candidate for candidate in decks if isinstance(candidate, str) and candidate.casefold() == custom_name.casefold()), None)
                deck = decks.get(key) if key is not None else None
                remaining = deck.get("remaining") if isinstance(deck, dict) else deck
                if not isinstance(remaining, list):
                    raise ValueError("自定义牌堆状态格式无效；抽牌已拒绝")
                drawn = []
                while remaining and len(drawn) < count:
                    drawn.append(remaining.pop(secrets.randbelow(len(remaining))))
                if not drawn:
                    raise ValueError("牌堆不存在或已抽完；.deck list 查看牌堆")
                return drawn, len(remaining)
            drawn, remaining_count = self._mutate_group(group_id, draw_group_cards)
            if len(drawn) == 1:
                result = f"从「{custom_name}」抽到：{drawn[0]}（剩余 {remaining_count} 张）"
            else:
                result = f"从「{custom_name}」抽到：" + " | ".join(drawn) + f"（剩余 {remaining_count} 张；本次抽取 {len(drawn)} 张）"
            return HiddenCheckReply(result, "隐藏抽牌已执行；结果仅尝试私信发送，若未收到请检查平台私聊权限。") if hidden else result

        builtin_name = find_builtin_public_deck(name)
        if builtin_name is None:
            raise ValueError("牌堆不存在或已抽完；.deck list 查看自定义及公共牌堆")
        drawn = draw_builtin_public_deck_batch(builtin_name, count, roll=roll_template_expression)
        if not isinstance(drawn, list) or not drawn or any(not isinstance(card, str) for card in drawn):
            raise ValueError("公共牌堆数据无效，未能抽取")
        if len(drawn) == 1:
            result = f"从公共牌堆「{builtin_name}」抽到：{drawn[0]}（公共牌堆每次独立抽取，不消耗）"
        else:
            result = f"从公共牌堆「{builtin_name}」抽到：" + " | ".join(drawn) + f"（公共牌堆每次独立抽取，不消耗；本次 {len(drawn)} 张）"
        return HiddenCheckReply(result, "隐藏抽牌已执行；结果仅尝试私信发送，若未收到请检查平台私聊权限。") if hidden else result

    @staticmethod
    def _normalize_ak_deck(value: object) -> dict:
        if not isinstance(value, dict):
            return {"title": "", "options": []}
        title = value.get("title")
        title = title.strip()[:40] if isinstance(title, str) else ""
        raw_options = value.get("options")
        options = []
        if isinstance(raw_options, list):
            for item in raw_options[:500]:
                if isinstance(item, str):
                    item = item.strip()
                    if item and len(item) <= 120:
                        options.append(item)
        return {"title": title, "options": options}

    @staticmethod
    def _parse_ak_items(raw: str) -> list[str]:
        items = [item.strip() for item in raw.split("|") if item.strip()]
        if len(items) > 500:
            raise ValueError("一次最多添加 500 个选项")
        if any(len(item) > 120 for item in items):
            raise ValueError("单个选项最多 120 个字符")
        return items

    @staticmethod
    def _format_ak_options(options: list[str]) -> str:
        visible = options[:20]
        lines = [f"{index}. {item}" for index, item in enumerate(visible, 1)]
        if len(options) > len(visible):
            lines.append(f"……另有 {len(options) - len(visible)} 项未显示")
        return "\n".join(lines) if lines else "（暂无选项）"

    def _ak(self, args: str, group_id: str) -> str:
        raw = args.strip()
        if not raw:
            return "用法：.ak new 标题[+选项|选项] / add 选项|选项 / del 序号 / get / show / clr；支持 .ak#标题+选项、.ak+选项、.ak-序号、.ak="
        sign = raw[0]
        if sign in "#+=":
            action = {"#": "new", "+": "add", "=": "get"}[sign]
            payload = raw[1:].strip()
        elif sign == "-":
            action, payload = "del", raw[1:].strip()
        else:
            action, separator, payload = raw.partition(" ")
            action = action.casefold()
            payload = payload.strip() if separator else ""
        if action not in {"new", "add", "del", "get", "show", "clr"}:
            raise ValueError("用法：.ak new/add/del/get/show/clr")

        if action == "new":
            title_text, has_items, raw_items = payload.partition("+")
            title = title_text.strip()
            if len(title) > 40:
                raise ValueError("选项堆标题最多 40 个字符")
            items = self._parse_ak_items(raw_items) if has_items else []
            if has_items and not items:
                raise ValueError("请至少提供一个选项")
            def create_deck(group: dict) -> tuple[str, int]:
                current = self._normalize_ak_deck(group.get("ak_deck"))
                title_saved = title or current["title"]
                group["ak_deck"] = {"title": title_saved, "options": list(items)}
                return title_saved, len(items)
            title_saved, count = self._mutate_group(group_id, create_deck)
            suffix = "\n" + self._format_ak_options(items) if items else ""
            return f"已新建选项堆「{title_saved or '未命名'}」，共 {count} 项。{suffix}"

        if action == "add":
            items = self._parse_ak_items(payload)
            if not items:
                raise ValueError("请提供待添加选项，使用 | 分隔")
            def add_items(group: dict) -> tuple[str, list[str]]:
                deck = self._normalize_ak_deck(group.get("ak_deck"))
                if len(deck["options"]) + len(items) > 500:
                    raise ValueError("选项堆最多保存 500 项")
                deck["options"].extend(items)
                group["ak_deck"] = deck
                return deck["title"], list(deck["options"])
            title, options = self._mutate_group(group_id, add_items)
            return f"选项堆「{title or '未命名'}」已添加 {len(items)} 项：\n{self._format_ak_options(options)}"

        if action == "del":
            if not payload.isascii() or not payload.isdecimal():
                raise ValueError("用法：.ak del 序号（从 1 开始）")
            index = int(payload)
            def delete_item(group: dict) -> tuple[str, str, list[str]]:
                deck = self._normalize_ak_deck(group.get("ak_deck"))
                if not 1 <= index <= len(deck["options"]):
                    raise ValueError("选项序号超出范围")
                removed = deck["options"].pop(index - 1)
                group["ak_deck"] = deck
                return deck["title"], removed, list(deck["options"])
            title, removed, options = self._mutate_group(group_id, delete_item)
            return f"已从「{title or '未命名'}」删除第 {index} 项：{removed}。\n{self._format_ak_options(options)}"

        if action == "get":
            def draw_option(group: dict) -> tuple[str, list[str], int]:
                deck = self._normalize_ak_deck(group.get("ak_deck"))
                if not deck["options"]:
                    raise ValueError("选项堆为空；先用 .ak add 添加选项")
                options = list(deck["options"])
                picked = secrets.randbelow(len(options))
                group["ak_deck"] = {"title": "", "options": []}
                return deck["title"], options, picked
            title, options, picked = self._mutate_group(group_id, draw_option)
            return f"「{title or '未命名'}」抽中第 {picked + 1} 项：{options[picked]}\n抽取前选项：\n{self._format_ak_options(options)}"

        if action == "show":
            deck = self._normalize_ak_deck(self._group_snapshot(group_id).get("ak_deck"))
            return f"选项堆「{deck['title'] or '未命名'}」（{len(deck['options'])} 项）：\n{self._format_ak_options(deck['options'])}"

        def clear_deck(group: dict) -> str:
            deck = self._normalize_ak_deck(group.get("ak_deck"))
            group["ak_deck"] = {"title": "", "options": []}
            return deck["title"]
        title = self._mutate_group(group_id, clear_deck)
        return f"选项堆「{title or '未命名'}」已清空。"

    def _dnd(self, args: str, group_id: str, sender_id: str) -> str:
        raw_count = args.strip()
        count_match = re.match(r"[ \t\r\n\v\f]*([0-9]+)", args)
        count_token = count_match.group(1) if count_match else ""
        if len(count_token) > 1 and count_token != "10":
            raise ValueError("D&D 属性生成组数只能为 1 到 10")
        count = int(count_token) if count_token else 1
        if count == 0:
            raise ValueError("D&D 属性生成组数不能为 0")

        player = self.store.player(sender_id)
        active_card = self._active_card_name(player, group_id)
        nickname = self._nickname_for_user(sender_id, group_id)
        pc_name = active_card if isinstance(active_card, str) and active_card != "角色卡" else (nickname or sender_id)
        if not isinstance(pc_name, str):
            pc_name = ""
        prefix = f"为{pc_name[:128]}的冒险者生成随机六维（降序）:"
        # Six scores each occupy two columns plus a slash; keep the worst-case response intact.
        if len(prefix) + count * 30 > self.max_response_length:
            raise ValueError("D&D 结果超出响应上限；请降低组数或提高响应长度配置")

        rows = []
        for _ in range(count):
            scores = sorted((self.engine.roll("4d6k3").total for _ in range(6)), reverse=True)
            row = "".join((" " if score < 10 else "") + f"{score} /" for score in scores)
            rows.append(row + f"总计{sum(scores)}")
        text = prefix + "".join(f"\n{row}" for row in rows)
        self._append_log(group_id, sender_id, f"dnd {raw_count or '1'}", text)
        return text

    def _d20(self, args: str, group_id: str, sender_id: str) -> str:
        match = re.fullmatch(r"\s*(?:(?:1?d20)\s*)?([+-]\s*\d+)?(?:\s+(?:DC\s*)?(\d+))?\s*", args, re.IGNORECASE)
        if not match:
            raise ValueError("用法：.d20 [d20修正] [DC]，例如 .d20 +5 15")
        modifier = int(match.group(1).replace(" ", "")) if match.group(1) else 0
        dc = int(match.group(2)) if match.group(2) else None
        if abs(modifier) > 10_000 or (dc is not None and not 0 <= dc <= 100_000):
            raise ValueError("修正值或 DC 超出安全范围")
        die = self.engine.roll("1d20").total
        total = die + modifier
        outcome = f"，DC {dc} {'成功' if total >= dc else '失败'}" if dc is not None else ""
        text = f"D20 检定：D20={die}{modifier:+d}={total}{outcome}"
        self._append_log(group_id, sender_id, f"d20 {args}", text)
        return text

    def _random_name(self, args: str = "", group_id: str = "") -> str:
        text = args.strip()
        if len(text) > 128:
            raise ValueError("姓名生成参数最多 128 个字符")
        type_match = re.match(r"^[^0-9+\-\[\]=:#\s]*", text)
        type_name = type_match.group(0).casefold() if type_match else ""
        remainder = text[type_match.end():] if type_match else text
        count = 1
        if remainder.strip():
            count_match = re.match(r"\s*([0-9]+)", remainder)
            if not count_match:
                raise ValueError("用法：.name [类型] [次数 1-10]")
            raw_count = count_match.group(1)
            if len(raw_count) > 1 and raw_count != "10":
                raise ValueError("姓名生成次数最多为 10")
            count = int(raw_count)
            if count == 0:
                raise ValueError("姓名生成次数不能为 0")
            if remainder[count_match.end():].strip():
                raise ValueError("用法：.name [类型] [次数 1-10]")

        name_pool = self._local_random_name_pool(type_name, group_id)
        if name_pool:
            names = [secrets.choice(name_pool) for _ in range(count)]
        else:
            target_name = f"随机姓名_{type_name}" if type_name else "随机姓名"
            builtin_name = find_builtin_public_deck(target_name) or find_builtin_public_deck("随机姓名")
            if builtin_name is None:
                raise ValueError("内置姓名牌堆不可用")
            names = draw_builtin_public_deck_batch(builtin_name, count)
            if not isinstance(names, list) or len(names) != count or any(not name.strip() for name in names):
                raise ValueError("姓名牌堆数据无效")
        return "随机名字：" + "、".join(name.strip() for name in names)

    def _local_random_name_pool(self, type_name: str, group_id: str) -> list[str]:
        target_name = f"随机姓名_{type_name}" if type_name else "随机姓名"
        decks = self._group_snapshot(group_id).get("decks", {})
        deck = decks.get(target_name) if isinstance(decks, dict) else None
        if deck is None and isinstance(decks, dict):
            deck = next((value for name, value in decks.items() if isinstance(name, str) and name.casefold() == target_name.casefold()), None)
        cards = deck.get("cards") if isinstance(deck, dict) else deck
        if not isinstance(cards, list):
            return []
        return [card.strip() for card in cards[:500] if isinstance(card, str) and card.strip()]

    def _random_name_from_sources(self, type_name: str, group_id: str) -> str:
        name_pool = self._local_random_name_pool(type_name, group_id)
        if name_pool:
            return secrets.choice(name_pool)
        target_name = f"随机姓名_{type_name}" if type_name else "随机姓名"
        builtin_name = find_builtin_public_deck(target_name) or find_builtin_public_deck("随机姓名")
        if builtin_name is None:
            raise ValueError("内置姓名牌堆不可用")
        name = draw_builtin_public_deck(builtin_name)
        if not isinstance(name, str) or not name.strip():
            raise ValueError("姓名牌堆数据无效")
        return name.strip()

    def _random_nickname(self, args: str, group_id: str, sender_id: str) -> str:
        text = args.strip()
        if len(text) > 128 or len(text.split()) > 1:
            raise ValueError("用法：.nnn [类型]")
        type_name = text.casefold()
        nickname = self._random_name_from_sources(type_name, group_id)
        return self._nickname(nickname, group_id, sender_id)

    def _nickname(self, args: str, group_id: str, sender_id: str) -> str:
        nickname = args.strip()
        if not nickname:
            return "用法：.nn 昵称|del|clr（昵称最多 50 个 UTF-8 字节）"
        try:
            nickname_bytes = len(nickname.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise ValueError("昵称必须是有效 UTF-8 文本") from exc
        if nickname_bytes > 50:
            raise ValueError("昵称最多 50 个 UTF-8 字节")

        def get_nicknames(state: dict) -> dict:
            players = state["players"]
            player = players.get(sender_id)
            if not isinstance(player, dict):
                player = {"cards": {}, "active_card": None}
                players[sender_id] = player
            nicknames = player.get("nicknames")
            if not isinstance(nicknames, dict):
                nicknames = {}
                player["nicknames"] = nicknames
            return nicknames

        if nickname == "del":
            removed = self.store.transact(lambda state: get_nicknames(state).pop(group_id, None) is not None)
            return "已删除本会话昵称。" if removed else "本会话尚未设置昵称。"
        if nickname == "clr":
            def clear_all(state: dict) -> None:
                get_nicknames(state).clear()
            self.store.transact(clear_all)
            return "已清除所有昵称。"

        self.store.transact(lambda state: get_nicknames(state).__setitem__(group_id, nickname))
        return f"本会话昵称已设置为「{nickname}」。"

    @staticmethod
    def _valid_group_link(owner_key: str, link: object) -> bool:
        if not isinstance(link, dict):
            return False
        scope = link.get("platform_scope")
        if not isinstance(scope, str) or len(scope) > 512:
            return False
        try:
            identity = json.loads(scope) if isinstance(scope, str) else None
        except json.JSONDecodeError:
            identity = None
        if not isinstance(identity, list) or len(identity) != 3 or not all(isinstance(item, str) and item.strip() for item in identity):
            return False
        source_group, target_group = link.get("source_group"), link.get("target_group")
        def valid_group_id(value: object) -> bool:
            if not isinstance(value, str) or not 1 <= len(value) <= 20 or not value.isascii() or not value.isdecimal():
                return False
            try:
                return 0 < int(value) <= 9_223_372_036_854_775_807
            except ValueError:
                return False
        if not valid_group_id(source_group) or not valid_group_id(target_group):
            return False
        source_key = f"{scope}:{source_group}"
        target_key = f"{scope}:{target_group}"
        return (
            owner_key == source_key
            and link.get("source_key") == source_key
            and link.get("target_key") == target_key
            and source_key != target_key
            and link.get("mode") in {"with", "from", "to"}
            and type(link.get("active")) is bool
        )

    @classmethod
    def _find_group_link(cls, groups: object, group_id: str):
        if not isinstance(groups, dict):
            return None
        matches = []
        for owner_key, state in groups.items():
            if not isinstance(state, dict):
                continue
            link = state.get("link")
            if cls._valid_group_link(str(owner_key), link) and (owner_key == group_id or link.get("target_key") == group_id):
                matches.append((str(owner_key), link))
        return matches[0] if len(matches) == 1 else None

    def _link_targets_for_group(self, group_id: str) -> list[tuple[str, str]]:
        try:
            groups = self.store.snapshot().get("groups", {})
            found = self._find_group_link(groups, group_id)
        except Exception:
            self.logger.exception("Dice .link state lookup failed")
            return []
        if found is None:
            return []
        _, link = found
        if link.get("active") is not True:
            return []
        mode = link.get("mode")
        if group_id == link.get("source_key") and mode in {"to", "with"}:
            return [(link["target_group"], link["source_group"])]
        if group_id == link.get("target_key") and mode in {"from", "with"}:
            return [(link["source_group"], link["target_group"])]
        return []

    async def _relay_link_message(self, group_id: str, text: str, platform_id: str, *, is_private: bool, speaker: str, from_bot: bool = False) -> None:
        body = text.strip()
        if is_private or not body or not platform_id or ":" in platform_id or body.startswith("[Dice-AstrBot-Link]"):
            return
        try:
            scope, separator, raw_group = group_id.rpartition(":")
            identity = json.loads(scope) if separator else None
            if not isinstance(identity, list) or len(identity) != 3 or not all(isinstance(item, str) and item.strip() for item in identity):
                return
            if not from_bot and speaker == identity[2]:
                return
            if identity[1] != platform_id or not 1 <= len(raw_group) <= 20 or not raw_group.isascii() or not raw_group.isdecimal() or not 0 < int(raw_group) <= 9_223_372_036_854_775_807:
                return
            destinations = self._link_targets_for_group(group_id)
        except Exception:
            self.logger.exception("Dice .link routing validation failed")
            return
        send_message = getattr(self.context, "send_message", None)
        if not callable(send_message):
            return
        for target_group, source_group in destinations:
            safe_speaker = speaker.replace("\r", "").replace("\n", "").replace("\0", "")[:64]
            label = "Dice-AstrBot" if from_bot else (safe_speaker or "用户")
            payload = f"[Dice-AstrBot-Link] 群 {source_group} · {label}：{body}"
            payload = payload[: self.max_response_length]
            try:
                sent = await asyncio.wait_for(send_message(f"{platform_id}:GroupMessage:{target_group}", [Plain(payload)]), timeout=5)
                if sent is False:
                    self.logger.warning("Dice .link message was not accepted by the target adapter")
            except Exception:
                self.logger.exception("Dice .link message send failed")

    def _link_command(self, args: str, group_id: str, sender_id: str, is_admin: bool, is_private: bool, platform_scope: str, platform_id: str) -> str:
        usage = "用法：.link with|to|from 群号 | .link start|close|state|list"
        if is_private:
            return "跨群链接仅允许在群聊中管理。"
        if not is_admin:
            return "只有当前群管理员可以管理群链接。"
        if not platform_id or len(platform_id) > 128 or ":" in platform_id or not platform_scope or len(platform_scope) > 512:
            return "无法安全确定当前 AstrBot 平台/bot，未修改链接。"
        try:
            identity = json.loads(platform_scope)
        except json.JSONDecodeError:
            identity = None
        if not isinstance(identity, list) or len(identity) != 3 or not all(isinstance(item, str) and item.strip() for item in identity) or identity[1] != platform_id:
            return "当前平台/bot 身份不完整，链接功能已拒绝执行。"
        prefix = f"{platform_scope}:"
        if not group_id.startswith(prefix):
            return "当前群缺少隔离范围，链接功能已拒绝执行。"
        source_group = group_id[len(prefix):]
        if not 1 <= len(source_group) <= 20 or not source_group.isascii() or not source_group.isdecimal() or not 0 < int(source_group) <= 9_223_372_036_854_775_807:
            return "跨群链接只支持当前平台/bot 的数字群号。"
        source_group = str(int(source_group))
        if f"{platform_scope}:{source_group}" != group_id:
            return "当前群号不是规范数字格式，链接功能已拒绝执行。"
        try:
            parts = shlex.split(args)
        except ValueError:
            return usage
        if not parts:
            return usage
        action = parts[0].casefold()
        if action in {"state", "status", "show"}:
            if len(parts) != 1:
                return usage
            found = self._find_group_link(self.store.snapshot().get("groups", {}), group_id)
            if found is None:
                return "当前群没有已配置的链接。"
            _, link = found
            arrow = {"to": "→", "from": "←", "with": "↔"}[link["mode"]]
            status = "转发中" if link["active"] else "已暂停"
            return f"群链接：{link['source_group']} {arrow} {link['target_group']}（{status}；仅同 AstrBot 平台实例文本消息；UMO 不含 self_id，多账号共用单实例时不能指定具体 bot）。"
        if action == "list":
            if len(parts) != 1:
                return usage
            groups = self.store.snapshot().get("groups", {})
            records = []
            if isinstance(groups, dict):
                for owner, state in groups.items():
                    if not isinstance(state, dict):
                        continue
                    link = state.get("link")
                    if self._valid_group_link(str(owner), link) and link.get("platform_scope") == platform_scope:
                        arrow = {"to": "→", "from": "←", "with": "↔"}[link["mode"]]
                        records.append(f"{link['source_group']} {arrow} {link['target_group']}（{'转发中' if link['active'] else '已暂停'}）")
            records.sort()
            if not records:
                return "本平台/bot 尚无群链接。"
            shown = records[:50]
            suffix = f"\n另有 {len(records) - 50} 条未显示。" if len(records) > 50 else ""
            return "本平台/bot 群链接：\n" + "\n".join(shown) + suffix
        if action in {"close", "start"}:
            if len(parts) != 1:
                return usage
            desired = action == "start"
            def toggle(state: dict) -> str:
                found = self._find_group_link(state.get("groups", {}), group_id)
                if found is None:
                    return "missing"
                _, link = found
                if link["active"] is desired:
                    return "same"
                link["active"] = desired
                return "changed"
            result = self.store.transact(toggle)
            if result == "missing":
                return "当前群没有可开关的链接。"
            if result == "same":
                return "群链接已经处于该状态。"
            return "群链接已恢复转发。" if desired else "群链接已暂停转发；配置保留，可用 .link start 恢复。"
        if action not in {"with", "from", "to"} or len(parts) != 2:
            return usage
        target_group = parts[1]
        if not 1 <= len(target_group) <= 20 or not target_group.isascii() or not target_group.isdecimal() or not 0 < int(target_group) <= 9_223_372_036_854_775_807:
            return "目标群号必须为有效的正整数。"
        target_group = str(int(target_group))
        target_key = f"{platform_scope}:{target_group}"
        if target_key == group_id:
            return "不能把群链接到自身。"
        new_link = {"platform_scope": platform_scope, "source_key": group_id, "target_key": target_key, "source_group": source_group, "target_group": target_group, "mode": action, "active": True, "created_by": self._raw_platform_user_id(sender_id), "created_at": self._now()}
        def build(state: dict) -> str:
            groups = state.get("groups")
            if not isinstance(groups, dict):
                return "corrupt"
            current = groups.get(group_id)
            if current is None:
                current = self._default_group()
                groups[group_id] = current
            if not isinstance(current, dict):
                return "corrupt"
            old_link = current.get("link")
            if "link" in current and not self._valid_group_link(group_id, old_link):
                return "corrupt"
            target_state = groups.get(target_key)
            if isinstance(target_state, dict) and "link" in target_state and not self._valid_group_link(target_key, target_state.get("link")):
                return "corrupt"
            for owner, state_item in groups.items():
                if owner == group_id or not isinstance(state_item, dict):
                    continue
                link = state_item.get("link")
                if self._valid_group_link(str(owner), link) and (owner == target_key or link.get("target_key") in {group_id, target_key}):
                    return "busy"
            current["link"] = new_link
            return "saved"
        result = self.store.transact(build)
        if result == "corrupt":
            return "群链接状态无效或损坏，已拒绝修改；请检查插件数据文件。"
        if result == "busy":
            return "源群或目标群已参与其他链接；为防止消息路由冲突，本次未修改。"
        arrow = {"to": "→", "from": "←", "with": "↔"}[action]
        return f"已建立群链接：{source_group} {arrow} {target_group}（当前 AstrBot 平台实例内立即转发文本；请确认目标群知情同意。UMO 不含 self_id，多账号共用单实例时不能指定具体 bot；适配器接受发送不代表消息已送达；.link close 可暂停）。"

    def _me(self, args: str, group_id: str, sender_id: str, is_admin: bool, is_private: bool, platform_scope: str, platform_id: str) -> str | GroupMessageReply:
        if self.disable_me:
            return "角色扮演动作已被全局禁用。"
        text = args.strip()
        if is_private:
            match = re.fullmatch(r"([0-9]{1,20})\s+(.+)", text, flags=re.DOTALL)
            if match is None:
                return "私聊用法：.me 群号 动作内容；按当前 AstrBot 平台实例发送。UMO 不含 self_id，多账号共用单实例时不能指定具体 bot。"
            target_group = match.group(1)
            action = match.group(2).strip()
            if not action:
                return "动作内容不能为空。"
            if int(target_group) <= 0 or int(target_group) > 9_223_372_036_854_775_807:
                return "群号超出有效范围。"
            if not platform_id or ":" in platform_id:
                return "无法安全确定当前 AstrBot 平台实例，未发送。"
            target_group_key = f"{platform_scope}:{target_group}" if platform_scope else target_group
            target_state = self._group_snapshot(target_group_key)
            if target_state.get("enabled") is False:
                return "目标群的 Dice 插件已关闭，未发送。"
            if target_state.get("me_disabled") is True:
                return "目标群已关闭角色扮演动作，未发送。"
            if str(self._raw_platform_user_id(sender_id)) in set(map(str, target_state.get("banned_users", []))):
                return "你已被目标群 Dice 插件禁用，未发送。"
            return GroupMessageReply(f"{platform_id}:GroupMessage:{target_group}", action)

        group = self._group_snapshot(group_id)
        if text.casefold() == "status":
            return "本群角色扮演动作：" + ("已关闭。" if group.get("me_disabled") is True else "已开启。")
        if text.casefold() in {"on", "off"}:
            if not is_admin:
                return "只有群管理员可以开关角色扮演动作。"
            enabled = text.casefold() == "on"
            self._mutate_group(group_id, lambda current: current.__setitem__("me_disabled", not enabled))
            return "本群角色扮演动作已开启。" if enabled else "本群角色扮演动作已关闭。"
        if not text:
            return "用法：.me 动作内容|on|off|status。私聊用法：.me 群号 动作内容。"
        if group.get("me_disabled") is True:
            return "本群已关闭角色扮演动作。"
        card_name, _ = self._active_card(sender_id, group_id)
        display_name = card_name or self._nickname_for_user(sender_id, group_id) or "有人"
        return text if is_admin else f"{display_name}{text}"

    def _set_rule(self, args: str, group_id: str, is_admin: bool) -> str:
        action = args.strip().casefold()
        if action in {"", "show"}:
            rule = self._effective_coc_rule(group_id)
            name, description = COC_RULE_INFO[rule]
            return f"当前检定房规：{rule}（{name}）。{description}"
        if action in {"clr", "clear"}:
            if not is_admin:
                return "只有群管理员可以修改本群房规。"
            def clear_rule(group: dict) -> str:
                session = group.get("session")
                if isinstance(session, dict) and "rule" in session:
                    session.pop("rule", None)
                    return "当前团务"
                group["rule"] = None
                return "本会话"
            scope = self._mutate_group(group_id, clear_rule)
            rule = self._effective_coc_rule(group_id)
            name, _ = COC_RULE_INFO[rule]
            return f"已清除{scope}房规覆盖；当前生效：{rule}（{name}）。"
        value = action.removeprefix("coc")
        if not value.isascii() or not value.isdecimal() or len(value) != 1 or not 0 <= int(value) <= 7:
            return "用法：.setcoc 0-7|show|clr。"
        if not is_admin:
            return "只有群管理员可以修改本群房规。"
        rule = int(value)
        def set_rule(group: dict) -> str:
            session = group.get("session")
            if isinstance(session, dict):
                session["rule"] = rule
                return "当前团务"
            group["rule"] = rule
            return "本会话"
        scope = self._mutate_group(group_id, set_rule)
        name, description = COC_RULE_INFO[rule]
        return f"{scope}检定房规已设为 {rule}（{name}）。{description}"

    def _effective_rule_name(self, group_id: str, sender_id: str) -> tuple[str | None, str]:
        group = self._group_snapshot(group_id)
        session = group.get("session")
        if isinstance(session, dict):
            value = session.get("rule_name")
            if isinstance(value, str) and value.strip():
                return value.strip(), "当前团务"
        player = self.store.player(sender_id)
        value = player.get("default_rule_name") if isinstance(player, dict) else None
        if isinstance(value, str) and value.strip():
            return value.strip(), "个人默认"
        return None, "未设置"

    def _legacy_rdconf_command(self, args: str, sender_id: str, is_private: bool, platform_scope: str) -> str:
        action = args.strip().casefold()
        if action == "scope":
            if not self.legacy_import_owner_id:
                return "请先配置 legacy_import_owner_id 为目标所有者原始用户 ID，再由其私聊执行 .legacy scope。"
            if not is_private or self._raw_platform_user_id(sender_id) != self.legacy_import_owner_id:
                return "当前平台/bot范围仅允许配置的所有者在私聊中查询。"
            return f"当前平台/bot范围（配置到 legacy_import_owner_scope）：{platform_scope}"
        if not self.legacy_import_owner_id:
            return "旧卡库导入默认关闭；请先设置 legacy_import_owner_id 和 legacy_import_owner_scope。"
        if not is_private or self._raw_platform_user_id(sender_id) != self.legacy_import_owner_id:
            return "旧卡库预览/导入仅允许配置的所有者在私聊中执行。"
        if not self.legacy_import_owner_scope:
            return "所有者范围未配置；请私聊执行 .legacy scope，将返回值配置到 legacy_import_owner_scope。"
        if platform_scope != self.legacy_import_owner_scope:
            return "当前连接器/bot 身份与配置所有者范围不匹配。"
        parts = args.strip().split()
        if parts and parts[0].casefold() in {"users-preview", "users-import"}:
            if len(parts) != 2 or parts[1].casefold() not in {"dat", "dat.bak", "bak", "rdconf"}:
                return "用法：.legacy users-preview dat|dat.bak|bak|rdconf；确认预览后执行对应的 users-import"
            return self._legacy_user_conf_command(parts[0].casefold(), parts[1].casefold(), platform_scope)
        if parts and parts[0].casefold() in {"groups-preview", "groups-import"}:
            source_format = parts[1].casefold() if len(parts) == 2 else ""
            if source_format not in {"dat", "dat.bak", "bak", "rdconf"}:
                return "用法：.legacy groups-preview dat|dat.bak|bak|rdconf；确认预览后执行对应的 groups-import"
            return self._legacy_chat_conf_command(parts[0].casefold(), source_format, platform_scope)
        if not parts or parts[0].casefold() not in {"preview", "import"}:
            return "用法：.legacy scope | preview|import [rdconf|rdconf.bak|bak] | users-preview dat|dat.bak|bak|rdconf | users-import 同源格式 | groups-preview dat|dat.bak|bak|rdconf | groups-import 同源格式"
        action = parts[0].casefold()
        source_format = parts[1].casefold() if len(parts) == 2 else "rdconf"
        if len(parts) > 2 or source_format not in {"rdconf", "rdconf.bak", "bak"}:
            return "用法：.legacy preview|import [rdconf|rdconf.bak|bak]（不带格式时默认 rdconf）"

        player_source_filenames = {
            "rdconf": "PlayerCards.RDconf",
            "rdconf.bak": "PlayerCards.RDconf.bak",
            "bak": "PlayerCards.bak",
        }
        filename = player_source_filenames[source_format]
        import_dir = self.data_dir / "imports"
        archive_path = import_dir / filename
        if import_dir.is_symlink() or archive_path.is_symlink():
            raise LegacyRDConfError(f"暂存目录和 {filename} 不允许为符号链接")
        if not archive_path.is_file():
            raise LegacyRDConfError(f"未找到卡库副本；请将 {filename} 放入插件数据目录 imports/ 后重试")
        try:
            with archive_path.open("rb") as archive:
                data = archive.read(MAX_ARCHIVE_BYTES + 1)
        except OSError as exc:
            raise LegacyRDConfError(f"无法读取 {filename} 旧卡库副本：{exc}") from exc
        if len(data) > MAX_ARCHIVE_BYTES:
            raise LegacyRDConfError("旧卡库副本超过 64 MiB 安全上限")
        digest = hashlib.sha256(data).hexdigest()
        decoded = decode_player_cards_rdconf(data)
        updates, summary = build_player_import_updates(decoded, platform_scope, self._now())

        if action == "preview":
            existing = self.store.snapshot().get("players", {})
            if not isinstance(existing, dict):
                raise LegacyRDConfError("本地玩家数据格式无效，预览已中止")
            conflicts = sum(1 for key in updates if key in existing)
            self._legacy_preview = (source_format, digest, platform_scope, conflicts)
            return (f"只读预览 {filename}：{summary['users']} 个玩家、{summary['cards']} 张卡、{summary['active_groups']} 个群卡映射；"
                    f"{summary['skipped_attributes']} 个非数值/内部/公式属性、{summary['skipped_empty_name_cards']} 张空名卡记录、{summary['skipped_metadata_sections']} 个 Lock/DiceExp/Info/Note 元数据区段及 {summary['skipped_group_mappings']} 个缺失卡片群映射未迁移；目标冲突 {conflicts} 个。未写入数据。"
                    "格式按固定版 Dice x64 解析；源格式无版本头，真实档案尚未验证。")

        preview = self._legacy_preview
        if not isinstance(preview, tuple) or preview[:3] != (source_format, digest, platform_scope):
            self._legacy_preview = None
            raise LegacyRDConfError("请先对当前 PlayerCards 文件、格式和当前 bot 身份执行对应的 .legacy preview")
        if preview[3] != 0:
            self._legacy_preview = None
            raise LegacyRDConfError("目标已存在同一批玩家记录；为防止覆盖，整批导入拒绝")
        try:
            imported = import_player_cards_into_store(self.store, decoded, platform_scope, self._now())
        finally:
            self._legacy_preview = None
        return (f"已从 {filename} 原子导入 {imported['users']} 个玩家、{imported['cards']} 张卡和 {imported['active_groups']} 个群卡映射；"
                f"未迁移 {imported['skipped_attributes']} 个非数值/内部/公式属性、{imported['skipped_empty_name_cards']} 张空名卡、{imported['skipped_metadata_sections']} 个旧元数据区段及 {imported['skipped_group_mappings']} 个缺失卡片映射。源文件未修改。"
                "请注意：当前只有合成格式验证，真实档案仍需备份后单独核对。")

    def _legacy_user_conf_command(self, action: str, source_format: str, platform_scope: str) -> str:
        source_filenames = {
            "dat": "UserConf.dat",
            "dat.bak": "UserConf.dat.bak",
            "bak": "UserConf.bak",
            "rdconf": "UserConf.RDconf",
        }
        if action not in {"users-preview", "users-import"} or source_format not in source_filenames:
            return "用法：.legacy users-preview dat|dat.bak|bak|rdconf；确认预览后执行对应的 users-import"
        filename = source_filenames[source_format]
        import_dir = self.data_dir / "imports"
        archive_path = import_dir / filename
        if import_dir.is_symlink() or archive_path.is_symlink():
            raise LegacyRDConfError("旧用户档案暂存目录和源文件不允许为符号链接")
        if not archive_path.is_file():
            raise LegacyRDConfError(f"未找到 {filename} 副本；请将其放入插件数据目录 imports/ 后重试")
        try:
            with archive_path.open("rb") as archive:
                data = archive.read(MAX_ARCHIVE_BYTES + 1)
        except OSError as exc:
            raise LegacyRDConfError(f"无法读取旧用户档案副本：{exc}") from exc
        if len(data) > MAX_ARCHIVE_BYTES:
            raise LegacyRDConfError("旧用户档案副本超过 64 MiB 安全上限")
        digest = hashlib.sha256(data).hexdigest()
        decoded = decode_user_conf_rdconf_importable(data) if source_format == "rdconf" else decode_user_conf_dat_importable(data)
        player_updates, private_group_updates, summary = build_user_settings_import_updates(decoded, platform_scope)

        if action == "users-preview":
            snapshot = self.store.snapshot()
            existing_players = snapshot.get("players", {})
            existing_groups = snapshot.get("groups", {})
            conflicts = count_user_settings_import_conflicts(existing_players, existing_groups, player_updates, private_group_updates)
            self._legacy_users_preview = (source_format, digest, platform_scope, conflicts)
            return (f"只读预览 {filename}：{summary['users']} 个用户、{summary['nicknames']} 条昵称（全局 {summary['global_nicknames']}、群级 {summary['group_nicknames']}）、"
                    f"个人默认骰 {summary['default_dice']} 项、私聊 COC 房规 {summary['private_coc_rules']} 项、个人规则名 {summary['default_rules']} 项、个人 gender 字符串 {summary['genders']} 项；冲突 {conflicts} 项。"
                    "仅迁移这些白名单字段；不导入 Dice trust、其他配置、时间戳或内部 ID；未写入数据。"
                    "格式按固定版 Dice x64 解析；源格式无版本头，真实档案尚未验证。")

        preview = self._legacy_users_preview
        if not isinstance(preview, tuple) or preview[:3] != (source_format, digest, platform_scope):
            self._legacy_users_preview = None
            raise LegacyRDConfError("请先对当前 UserConf 文件、格式和当前 bot 身份执行对应的 users-preview")
        if preview[3] != 0:
            self._legacy_users_preview = None
            raise LegacyRDConfError("昵称、默认骰、私聊 COC 房规、默认规则名或 gender 存在不同的本地值；为防覆盖，整批导入拒绝")
        try:
            imported = import_user_settings_into_store(self.store, decoded, platform_scope)
        finally:
            self._legacy_users_preview = None
        return (f"已原子迁移 {imported['users']} 个用户：昵称 {imported['nicknames']} 条、个人默认骰 {imported['default_dice']} 项、"
                f"私聊 COC 房规 {imported['private_coc_rules']} 项、个人规则名 {imported['default_rules']} 项、个人 gender 字符串 {imported['genders']} 项。"
                "未导入 Dice trust、其他配置、时间戳或内部 ID；源文件未修改。")

    def _legacy_chat_conf_command(self, action: str, source_format: str, platform_scope: str) -> str:
        source_filenames = {
            "dat": "ChatConf.dat",
            "dat.bak": "ChatConf.dat.bak",
            "bak": "ChatConf.bak",
            "rdconf": "ChatConf.RDconf",
        }
        if action not in {"groups-preview", "groups-import"} or source_format not in source_filenames:
            raise LegacyRDConfError("invalid ChatConf import action or source format")
        pending_preview = self._legacy_groups_preview
        self._legacy_groups_preview = None
        import_dir = self.data_dir / "imports"
        filename = source_filenames[source_format]
        archive_path = import_dir / filename
        if import_dir.is_symlink() or archive_path.is_symlink():
            raise LegacyRDConfError(f"旧群配置暂存目录和 {filename} 不允许为符号链接")
        if not archive_path.is_file():
            raise LegacyRDConfError(f"未找到 {filename} 副本；请将其放入插件数据目录 imports/ 后重试")
        try:
            with archive_path.open("rb") as archive:
                data = archive.read(MAX_ARCHIVE_BYTES + 1)
        except OSError as exc:
            raise LegacyRDConfError(f"无法读取 {filename} 旧群配置副本：{exc}") from exc
        if len(data) > MAX_ARCHIVE_BYTES:
            raise LegacyRDConfError("旧群配置副本超过 64 MiB 安全上限")
        digest = hashlib.sha256(data).hexdigest()
        decoded = decode_chat_conf_dat(data)
        updates, summary = build_chat_config_import_updates(decoded, platform_scope)

        if action == "groups-preview":
            existing = self.store.snapshot().get("groups", {})
            conflicts = count_chat_config_import_conflicts(existing, updates)
            self._legacy_groups_preview = (source_format, digest, platform_scope, conflicts)
            return (f"只读预览 {filename}：{summary['source_groups']} 个群记录，{summary['groups']} 个群有 {summary['settings']} 项可迁移设置；"
                    f"自定义值冲突 {conflicts} 项。只迁移六项群开关、Rh盲骰隐藏发送者副本设置、rc房规（0–7）及入群欢迎纯文本（≤500字，不执行模板或通知）；不迁移群名、频道 ChConf、卡组/日志/信任或其他字段；未写入数据。"
                    "格式按固定版 Dice x64 解析；源格式无版本头，真实档案尚未验证。")

        if not isinstance(pending_preview, tuple) or pending_preview[:3] != (source_format, digest, platform_scope):
            raise LegacyRDConfError(f"请先对当前 {filename} 和当前 platform/bot 身份执行 .legacy groups-preview {source_format}")
        if pending_preview[3] != 0:
            raise LegacyRDConfError("存在已自定义且不同的本地群设置；为防止覆盖，整批导入拒绝")
        try:
            imported = import_chat_config_into_store(self.store, decoded, platform_scope)
        finally:
            self._legacy_groups_preview = None
        return (f"已原子迁移 {imported['groups']} 个群的 {imported['settings']} 项设置。"
                "仅写入六项群开关、Rh盲骰隐藏设置、rc房规与入群欢迎纯文本（≤500字；不执行模板/通知）；本地群的其他状态保留，源文件未修改。")

    def _rule_command(self, args: str, group_id: str, sender_id: str) -> str:
        action, _, rest = args.strip().partition(" ")
        action = action.casefold()
        if action == "set":
            value = rest.strip()
            if len(value) > 128:
                raise ValueError("规则名最多 128 个字符")

            def save_rule(state: dict) -> str:
                group = state["groups"].get(group_id)
                session = group.get("session") if isinstance(group, dict) else None
                if isinstance(session, dict):
                    if value:
                        session["rule_name"] = value
                    else:
                        session.pop("rule_name", None)
                    return "当前团务"
                player = state["players"].get(sender_id)
                if player is None:
                    player = {"cards": {}, "active_card": None}
                    state["players"][sender_id] = player
                if not isinstance(player, dict):
                    raise ValueError("玩家数据格式无效，无法保存规则名")
                if value:
                    player["default_rule_name"] = value
                else:
                    player.pop("default_rule_name", None)
                return "个人默认"

            scope = self.store.transact(save_rule)
            if value:
                return f"{scope}规则名已保存为「{value}」。它只记录规则名，不改变骰子判定。"
            return f"{scope}规则名已重置。"

        if action in {"show", "status"}:
            value, scope = self._effective_rule_name(group_id, sender_id)
            return f"{scope}规则名：{value}（仅本地记录，不改变判定规则）。" if value else "尚未设置规则名；用 .rule set 规则名 保存。"

        if action in {"s", "search"}:
            if not rest.strip():
                raise ValueError("用法：.rule s 关键词")
            return "本插件未导入上游规则手册；按不代理原则，不会向上游服务查询。可用 .rule set 规则名 本地记录。"

        raise ValueError("用法：.rule set 规则名|.rule set（重置）|.rule show|.rule s 关键词")

    @staticmethod
    def _valid_session_logger(logger: object) -> bool:
        if not isinstance(logger, dict):
            return False
        state = logger.get("state")
        if not isinstance(state, str) or state not in {"active", "paused", "ended"}:
            return False
        name = logger.get("name")
        if not isinstance(name, str) or len(name) > 80 or name in {".", ".."} or re.fullmatch(r"[\w .-]{1,80}", name) is None:
            return False
        started_at = logger.get("started_at")
        if not isinstance(started_at, str) or len(started_at) > 40 or any(ord(char) < 32 for char in started_at):
            return False
        entries = logger.get("entries")
        if not isinstance(entries, list) or len(entries) > 200:
            return False
        dropped = logger.get("dropped", 0)
        if type(dropped) is not int or not 0 <= dropped <= 1_000_000_000:
            return False
        masters = logger.get("session_masters", [])
        if not isinstance(masters, list) or len(masters) > 100 or any(not isinstance(master, str) or len(master) > 128 for master in masters):
            return False
        for entry in entries:
            if not isinstance(entry, dict):
                return False
            direction = entry.get("direction")
            timestamp = entry.get("time")
            sender = entry.get("sender")
            text = entry.get("text")
            if (not isinstance(direction, str) or direction not in {"in", "out"}
                    or not isinstance(timestamp, str) or len(timestamp) > 40 or any(ord(char) < 32 for char in timestamp)
                    or not isinstance(sender, str) or len(sender) > 128 or any(ord(char) < 32 for char in sender)
                    or not isinstance(text, str) or len(text) > 500):
                return False
        return True

    @staticmethod
    def _new_session_logger(name: str, started_at: str) -> dict:
        return {"state": "active", "name": name, "started_at": started_at, "entries": [], "dropped": 0}

    def _show_log(self, group_id: str) -> str:
        entries = self._group_snapshot(group_id).get("logs", [])[-10:]
        return "最近记录：\n" + "\n".join(f"{e.get('time', '?')} {e.get('result', '')}" for e in entries) if entries else "暂无骰子/检定记录。"

    def _log_command(self, args: str, group_id: str, sender_id: str, is_admin: bool, is_private: bool) -> str:
        action, _, tail = args.strip().partition(" ")
        action = action.casefold()
        tail = tail.strip()
        if not action or action == "recent":
            return self._show_log(group_id)
        if action not in {"new", "on", "off", "end", "status", "show", "archives"}:
            raise ValueError("用法：.log new [名称]|on [名称]|off|end|status|show [条数]|show archive [序号]|archives|recent")
        archive_index = None
        if action == "show":
            count = 10
            if tail:
                if tail == "archive":
                    archive_index = 1
                elif tail.startswith("archive "):
                    ordinal = tail[8:].strip()
                    if not ordinal.isascii() or not ordinal.isdecimal() or not 1 <= int(ordinal) <= 6:
                        raise ValueError(".log show archive 序号范围为 1–6（1 为最新）")
                    archive_index = int(ordinal)
                elif not tail.isascii() or not tail.isdecimal() or not 1 <= int(tail) <= 12:
                    raise ValueError("用法：.log show [1–12 条]|archive [1–6 序号]")
                else:
                    count = int(tail)
        elif action in {"new", "on"}:
            if tail and (len(tail) > 80 or tail in {".", ".."} or re.fullmatch(r"[\w .-]{1,80}", tail) is None):
                raise ValueError("日志名称仅可含字母、数字、空格、点、下划线和连字符，最多 80 字符")
        elif tail:
            raise ValueError(f".log {action} 不接受额外参数")

        if action in {"status", "show", "archives"}:
            group = self._group_snapshot(group_id)
            raw_session = group.get("session")
            session = raw_session if isinstance(raw_session, dict) else None
            logger = session.get("logger") if session else None
            current_archives = session.get("log_archives", []) if session else []
            group_archives = group.get("session_log_archives", [])
            if logger is not None and not self._valid_session_logger(logger):
                return "团务日志状态异常，拒绝读取。"
            if not isinstance(group_archives, list) or len(group_archives) > 3 or any(not self._valid_session_logger(item) for item in group_archives):
                return "已结束团务日志归档状态异常，拒绝读取。"
            if not isinstance(current_archives, list) or len(current_archives) > 3 or any(not self._valid_session_logger(item) for item in current_archives):
                return "团务日志归档状态异常，拒绝读取。"
            archives = group_archives + current_archives
            visible_archives = [
                item for item in archives
                if is_admin or sender_id in item.get("session_masters", [])
            ]
            can_read_current = bool(session) and (
                is_admin or sender_id in self._session_member_ids(session, "game_masters", "game_master")
            )
            if action == "archives":
                if not visible_archives:
                    return "没有可访问的归档团务日志。"
                rows = [f"{index}. {item['name']}（{len(item['entries'])} 条，已裁剪 {item.get('dropped', 0)} 条）" for index, item in enumerate(reversed(visible_archives), 1)]
                return "归档团务日志（1 为最新）：\n" + "\n".join(rows)
            if action == "status":
                if not session:
                    return "当前没有进行中的团务。"
                if not can_read_current:
                    return "只有当前团务主持人或群管理员可以查看状态。"
                selected = logger if isinstance(logger, dict) else (visible_archives[-1] if visible_archives else None)
                if not isinstance(selected, dict):
                    return "当前团务尚未创建日志。"
                return f"团务日志「{selected['name']}」状态：{selected['state']}；保留 {len(selected['entries'])}/200 条，已裁剪 {selected.get('dropped', 0)} 条。"
            if archive_index is not None:
                if archive_index > len(visible_archives):
                    return "找不到可访问的团务日志归档。"
                selected = visible_archives[-archive_index]
            elif isinstance(logger, dict) and can_read_current:
                selected = logger
            elif visible_archives:
                selected = visible_archives[-1]
            elif session and not can_read_current:
                return "只有团务主持人或群管理员可以查看团务日志。"
            else:
                return "当前没有可查看的团务日志。"
            rows = selected["entries"][-count:]
            if not rows:
                return f"团务日志「{selected['name']}」暂无记录。"
            lines = [f"{entry['time']} {entry['sender']} {entry['direction']}: {entry['text'][:300]}" for entry in rows]
            return f"团务日志「{selected['name']}」（最近 {len(rows)} 条；已裁剪 {selected.get('dropped', 0)} 条）：\n" + "\n".join(lines)

        default_name = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("log-%Y%m%d-%H%M%S")
        requested_name = tail or default_name
        def mutate(group: dict) -> str:
            session = group.get("session")
            if not isinstance(session, dict):
                return "当前没有进行中的团务。"
            if not is_admin and sender_id not in self._session_member_ids(session, "game_masters", "game_master"):
                return "只有团务主持人或群管理员可以管理团务日志。"
            logger = session.get("logger")
            if logger is not None and not self._valid_session_logger(logger):
                return "团务日志状态异常，拒绝修改。"
            archives = session.get("log_archives", [])
            if not isinstance(archives, list) or len(archives) > 3 or any(not self._valid_session_logger(item) for item in archives):
                return "团务日志归档状态异常，拒绝修改。"
            def archive_current() -> None:
                if isinstance(logger, dict):
                    archived = copy.deepcopy(logger)
                    archived["state"] = "ended"
                    archived["session_masters"] = sorted(self._session_member_ids(session, "game_masters", "game_master"))
                    archives.append(archived)
                    del archives[:-3]
                    session["log_archives"] = archives
            def create(name: str) -> None:
                session["logger"] = self._new_session_logger(name, self._now())
            if action == "new":
                archive_current()
                create(requested_name)
                return f"已开始团务日志「{requested_name}」。"
            if action == "on":
                if tail:
                    archive_current()
                    create(requested_name)
                    return f"已开始团务日志「{requested_name}」。"
                if not isinstance(logger, dict) or logger.get("state") == "ended":
                    archive_current()
                    create(default_name)
                    return f"已开始团务日志「{default_name}」。"
                if logger.get("state") == "active":
                    return "团务日志已经处于记录状态。"
                logger["state"] = "active"
                return f"已恢复团务日志「{logger['name']}」。"
            if not isinstance(logger, dict):
                return "当前团务尚未创建日志。"
            if action == "off":
                if logger["state"] != "active":
                    return "团务日志当前未在记录。"
                logger["state"] = "paused"
                return "团务日志已暂停，已有内容保留。"
            if action == "end":
                if logger["state"] == "ended":
                    return "团务日志已经结束。"
                logger["state"] = "ended"
                logger["ended_at"] = self._now()
                return "团务日志已结束并保存在本地状态中；上游文件上传/钩子未移植。"
            return "不支持的团务日志操作。"
        return self._mutate_group(group_id, mutate)

    def _append_session_log(self, group_id: str, sender_id: str, raw_sender_id: str, direction: str, text: str) -> None:
        if direction not in {"in", "out"} or not isinstance(text, str):
            return
        clean_text = "".join(char for char in text if char in "\t\n" or (ord(char) >= 32 and ord(char) != 127))[:500]
        if not clean_text:
            return
        def append(group: dict) -> None:
            session = group.get("session")
            if not isinstance(session, dict):
                return
            members = self._session_member_ids(session, "game_masters", "game_master")
            members.update(self._session_member_ids(session, "participants"))
            if sender_id not in members:
                return
            logger = session.get("logger")
            if not self._valid_session_logger(logger) or logger.get("state") != "active":
                return
            entries = logger["entries"]
            if len(entries) >= 200:
                del entries[0]
                logger["dropped"] = min(logger.get("dropped", 0) + 1, 1_000_000_000)
            clean_sender = "".join(char for char in str(raw_sender_id) if ord(char) >= 32 and ord(char) != 127)[:128] or "?"
            entries.append({"time": self._now(), "sender": clean_sender, "direction": direction, "text": clean_text})
        self._mutate_group(group_id, append)

    def _append_log(self, group_id: str, sender_id: str, command: str, result: str) -> None:
        entry = {"time": self._now(), "sender": sender_id, "command": command[:100], "result": result[:500]}
        def apply(group: dict) -> None:
            logs = group.setdefault("logs", [])
            logs.append(entry)
            del logs[:-100]
        self._mutate_group(group_id, apply)

    @staticmethod
    def _custom_reply_deck_choices(response: object) -> list[tuple[str, int, str]] | None:
        if not isinstance(response, dict) or response.get("kind") != "deck":
            return None
        choices = response.get("choices")
        if not isinstance(choices, list) or not 2 <= len(choices) <= 32:
            return None
        clean: list[tuple[str, int, str]] = []
        total_weight = 0
        total_chars = 0
        for choice in choices:
            if not isinstance(choice, str) or not 1 <= len(choice) <= 600:
                return None
            raw = choice.strip()
            if not raw or any(ord(char) < 32 for char in raw) or "{" in raw or "}" in raw:
                return None
            parts = raw.split("::")
            if len(parts) == 1:
                label, weight, value = "", 1, raw
            elif len(parts) == 3:
                label, weight_text, value = (part.strip() for part in parts)
                if not label or len(label) > 80 or not weight_text.isascii() or not weight_text.isdecimal() or len(weight_text) > 4:
                    return None
                weight = int(weight_text)
                if not 1 <= weight <= 1000:
                    return None
            else:
                return None
            if not 1 <= len(value) <= 500 or "::" in value:
                return None
            total_weight += weight
            total_chars += len(raw)
            if total_weight > 10000 or total_chars > 4096:
                return None
            clean.append((label, weight, value))
        return clean

    @staticmethod
    def _normalize_custom_reply_limits(limits: object) -> dict | None:
        if not isinstance(limits, dict) or not 1 <= len(limits) <= 6:
            return None
        normalized = {}
        for key, value in limits.items():
            if key == "prob":
                if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
                    return None
                normalized[key] = value
                continue
            if key == "lock":
                if isinstance(value, dict) and set(value) == {"locks"}:
                    value = value.get("locks")
                if not isinstance(value, list) or not 1 <= len(value) <= 4:
                    return None
                locks = []
                seen_locks = set()
                for item in value:
                    if not isinstance(item, dict) or set(item) != {"scope", "key"}:
                        return None
                    scope, lock_key = item.get("scope"), item.get("key")
                    if (not isinstance(scope, str) or scope not in {"chat", "user", "global"}
                            or not isinstance(lock_key, str) or len(lock_key) > 80
                            or any(ord(char) < 32 or char in "&;@" for char in lock_key)):
                        return None
                    identity = (scope, lock_key)
                    if identity not in seen_locks:
                        locks.append({"scope": scope, "key": lock_key})
                        seen_locks.add(identity)
                if not locks:
                    return None
                normalized[key] = {"locks": locks}
                continue
            if key in {"cd", "today"}:
                maximum = 86_400 if key == "cd" else 10_000
                scope = "chat"
                count = value
                notice = None
                if isinstance(value, dict):
                    fields = set(value)
                    if fields in ({"scope", "value"}, {"scope", "value", "notice"}):
                        scope = value.get("scope")
                        count = value.get("value")
                        if "notice" in value:
                            raw_notice = value.get("notice")
                            if not isinstance(raw_notice, str):
                                return None
                            notice = raw_notice.strip()
                            if not 1 <= len(notice) <= 400 or any(ord(char) < 32 or char in "&;" for char in notice):
                                return None
                if not isinstance(scope, str) or scope not in {"chat", "user", "global"} or isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= maximum:
                    return None
                normalized[key] = {"scope": scope, "value": count}
                if notice is not None:
                    normalized[key]["notice"] = notice
                continue
            if key not in {"user_id", "grp_id"} or not isinstance(value, dict):
                return None
            ids = value.get("ids")
            exclude = value.get("exclude")
            if not isinstance(ids, list) or not 1 <= len(ids) <= 32 or not isinstance(exclude, bool):
                return None
            clean = []
            for item in ids:
                if not isinstance(item, str):
                    return None
                identifier = item.strip()
                if not 1 <= len(identifier) <= 128 or any(char.isspace() or ord(char) < 32 or char in ",;|" for char in identifier):
                    return None
                if identifier not in clean:
                    clean.append(identifier)
            if not clean:
                return None
            normalized[key] = {"ids": clean, "exclude": exclude}
        return normalized or None

    @staticmethod
    def _parse_custom_reply_limit_spec(raw: str) -> dict | None:
        if not isinstance(raw, str) or not raw.strip() or len(raw) > 512:
            return None
        parsed = {}
        for item in raw.split(";"):
            item = item.strip()
            if not item:
                continue
            key, separator, value = item.partition(":")
            key, value = key.strip().casefold(), value.strip()
            if key == "lock":
                if key in parsed:
                    return None
                locks = []
                if not separator:
                    locks.append({"scope": "global", "key": ""})
                else:
                    segments = value.split("&")
                    if len(segments) > 4 or any(not segment.strip() for segment in segments) and len(segments) > 1:
                        return None
                    for segment in segments:
                        segment = segment.strip()
                        scope = "chat"
                        lock_key = segment
                        if "@" in segment:
                            lock_key, scope = segment.split("@", 1)
                            scope = scope.casefold()
                            if scope not in {"chat", "user", "global"}:
                                return None
                        if len(lock_key) > 80 or any(ord(char) < 32 or char in "&;@" for char in lock_key):
                            return None
                        locks.append({"scope": scope, "key": lock_key})
                parsed[key] = locks
                continue
            if not separator or key not in {"prob", "user_id", "grp_id", "cd", "today"} or key in parsed:
                return None
            if key == "prob":
                if not value.isascii() or not value.isdecimal() or len(value) > 3:
                    return None
                probability = int(value)
                if not 0 <= probability <= 100:
                    return None
                parsed[key] = probability
                continue
            if key in {"cd", "today"}:
                maximum = 86_400 if key == "cd" else 10_000
                scope = "chat"
                notice = None
                segments = value.split("&")
                if len(segments) > 2:
                    return None
                count_segments = []
                for segment in segments:
                    segment = segment.strip()
                    if segment.casefold().startswith("@echo="):
                        if notice is not None:
                            return None
                        notice = segment[len("@echo="):].strip()
                    else:
                        count_segments.append(segment)
                if len(count_segments) != 1:
                    return None
                count_text = count_segments[0]
                if "=" in count_text:
                    scope_spec, _, count_text = count_text.partition("=")
                    scope_spec = scope_spec.strip().casefold()
                    if scope_spec not in {"@user", "@global"}:
                        return None
                    scope = scope_spec[1:]
                    count_text = count_text.strip()
                if not count_text.isascii() or not count_text.isdecimal() or len(count_text) > 5:
                    return None
                count = int(count_text)
                if count > maximum:
                    return None
                parsed[key] = {"scope": scope, "value": count}
                if notice is not None:
                    parsed[key]["notice"] = notice
                continue
            exclude = value.startswith("!")
            raw_ids = value[1:] if exclude else value
            parsed[key] = {"ids": [part.strip() for part in raw_ids.split(",")], "exclude": exclude}
        return Main._normalize_custom_reply_limits(parsed)

    @staticmethod
    def _custom_reply_limit_summary(limits: object) -> str | None:
        normalized = Main._normalize_custom_reply_limits(limits)
        if normalized is None:
            return None
        parts = []
        if "prob" in normalized:
            parts.append(f"prob:{normalized['prob']}")
        for key in ("user_id", "grp_id"):
            if key in normalized:
                restriction = normalized[key]
                prefix = "!" if restriction["exclude"] else ""
                parts.append(f"{key}:{prefix}{','.join(restriction['ids'])}")
        if "lock" in normalized:
            locks = normalized["lock"]["locks"]
            if len(locks) == 1 and locks[0] == {"scope": "global", "key": ""}:
                parts.append("lock")
            else:
                rendered_locks = []
                for item in locks:
                    value = item["key"]
                    if item["scope"] != "chat":
                        value += "@" + item["scope"]
                    rendered_locks.append(value)
                parts.append("lock:" + "&".join(rendered_locks))
        for key in ("cd", "today"):
            if key in normalized:
                limit = normalized[key]
                scope = limit["scope"]
                count = limit["value"]
                rendered = f"{key}:{count}" if scope == "chat" else f"{key}:@{scope}={count}"
                notice = limit.get("notice")
                if isinstance(notice, str) and notice:
                    rendered += f"&@echo={notice}"
                parts.append(rendered)
        return ";".join(parts)

    @staticmethod
    def _custom_reply_limit_allows(limits: object, sender_id: str, raw_group_id: str) -> bool:
        normalized = Main._normalize_custom_reply_limits(limits)
        if normalized is None:
            return False
        for key, identity in (("user_id", sender_id), ("grp_id", raw_group_id)):
            if key not in normalized:
                continue
            restriction = normalized[key]
            matches = identity in restriction["ids"]
            if (restriction["exclude"] and matches) or (not restriction["exclude"] and not matches):
                return False
        probability = normalized.get("prob")
        return probability is None or probability in {0, 100} or secrets.randbelow(100) + 1 <= probability

    @staticmethod
    def _custom_reply_set_info(response: object) -> dict | None:
        if not isinstance(response, dict) or response.get("kind") != "reply_set":
            return None
        title = response.get("title")
        modes = response.get("modes")
        if not isinstance(title, str) or not 1 <= len(title) <= 128 or any(ord(char) < 32 for char in title):
            return None
        if not isinstance(modes, dict) or not modes or not set(modes).issubset({"exact", "prefix", "search", "regex"}):
            return None
        normalized_modes = {}
        for mode, triggers in modes.items():
            if not isinstance(triggers, list) or not 1 <= len(triggers) <= 32:
                return None
            cleaned = []
            seen = set()
            for trigger in triggers:
                maximum = 128 if mode == "regex" else 80
                if not isinstance(trigger, str) or not 1 <= len(trigger) <= maximum or any(ord(char) < 32 for char in trigger):
                    return None
                identity = trigger if mode == "regex" else trigger.casefold()
                if identity in seen:
                    return None
                seen.add(identity)
                if mode == "regex":
                    try:
                        compile_safe_regex(trigger)
                    except SafeRegexError:
                        return None
                elif "|" in trigger:
                    return None
                cleaned.append(trigger)
            normalized_modes[mode] = cleaned
        inner = response.get("response")
        if isinstance(inner, dict) and inner.get("kind") == "reply_set":
            return None
        if isinstance(inner, dict) and inner.get("kind") == "limited":
            return None
        if Main._custom_reply_display(inner) is None:
            return None
        limits = response.get("limits")
        if limits is not None and Main._normalize_custom_reply_limits(limits) is None:
            return None
        return {"title": title, "identity": title.casefold(), "modes": normalized_modes, "response": inner, "limits": limits}

    @staticmethod
    def _custom_reply_display(response: object) -> str | None:
        if isinstance(response, str) and 1 <= len(response.strip()) <= 500:
            return response
        set_info = Main._custom_reply_set_info(response)
        if set_info is not None:
            rendered = Main._custom_reply_display(set_info["response"])
            if rendered is None:
                return None
            limits = set_info["limits"]
            if limits is None:
                return rendered
            summary = Main._custom_reply_limit_summary(limits)
            return f"{rendered}（限制：{summary}）" if summary is not None else None
        if isinstance(response, dict) and response.get("kind") == "limited":
            inner = response.get("response")
            if isinstance(inner, dict) and inner.get("kind") == "limited":
                return None
            rendered = Main._custom_reply_display(inner)
            limits = Main._custom_reply_limit_summary(response.get("limits"))
            return f"{rendered}（限制：{limits}）" if rendered is not None and limits is not None else None
        choices = Main._custom_reply_deck_choices(response)
        if choices is None:
            return None
        rendered = [f"{label}::{weight}::{value}" if label else value for label, weight, value in choices]
        return "Deck: " + " | ".join(rendered)

    async def _acquire_custom_reply_locks(self, lock_specs: object, group_scope: str, user_scope: str, trigger: str) -> tuple[tuple[tuple[int, str, str, str], asyncio.Lock], ...] | None:
        if not isinstance(lock_specs, list) or not 1 <= len(lock_specs) <= 4:
            return None
        loop_id = id(asyncio.get_running_loop())
        keys = []
        for spec in lock_specs:
            if not isinstance(spec, dict) or set(spec) != {"scope", "key"}:
                return None
            scope, lock_name = spec.get("scope"), spec.get("key")
            if scope == "chat":
                identity = group_scope
            elif scope == "user":
                identity = user_scope
            elif scope == "global":
                identity = ""
            else:
                return None
            if scope != "global" and (not isinstance(identity, str) or not identity or len(identity) > 512):
                return None
            if not isinstance(lock_name, str) or len(lock_name) > 80:
                return None
            name = lock_name or trigger
            if not 1 <= len(name) <= 80:
                return None
            keys.append((loop_id, scope, identity, name))
        ordered_keys = sorted(set(keys))
        with self._custom_reply_lock_guard:
            missing = [key for key in ordered_keys if key not in self._custom_reply_locks]
            if len(self._custom_reply_locks) + len(missing) > 10_000:
                return None
            handles = []
            for key in ordered_keys:
                lock = self._custom_reply_locks.get(key)
                if lock is None:
                    lock = asyncio.Lock()
                    self._custom_reply_locks[key] = lock
                self._custom_reply_lock_refs[key] = self._custom_reply_lock_refs.get(key, 0) + 1
                handles.append((key, lock))
        acquired = []
        try:
            for key, lock in handles:
                await lock.acquire()
                acquired.append((key, lock))
        except BaseException:
            for _, lock in reversed(acquired):
                lock.release()
            self._drop_custom_reply_lock_refs(handles)
            raise
        return tuple(handles)

    def _drop_custom_reply_lock_refs(self, handles: tuple[tuple[tuple[int, str, str, str], asyncio.Lock], ...] | list[tuple[tuple[int, str, str, str], asyncio.Lock]]) -> None:
        with self._custom_reply_lock_guard:
            for key, _ in handles:
                refs = self._custom_reply_lock_refs.get(key, 0)
                if refs <= 1:
                    self._custom_reply_lock_refs.pop(key, None)
                    lock = self._custom_reply_locks.get(key)
                    if lock is not None and not lock.locked():
                        self._custom_reply_locks.pop(key, None)
                else:
                    self._custom_reply_lock_refs[key] = refs - 1

    def _release_custom_reply_locks(self, handles: tuple[tuple[tuple[int, str, str, str], asyncio.Lock], ...]) -> None:
        for _, lock in reversed(handles):
            if lock.locked():
                lock.release()
        self._drop_custom_reply_lock_refs(handles)

    @staticmethod
    def _casefold_prefix_end(text: str, folded_prefix: str) -> int | None:
        if not isinstance(text, str) or not isinstance(folded_prefix, str) or not folded_prefix:
            return None
        folded_parts: list[str] = []
        for index, char in enumerate(text):
            folded_parts.append(char.casefold())
            current = "".join(folded_parts)
            if current == folded_prefix:
                return index + 1
            if not folded_prefix.startswith(current):
                return None
        return None

    @staticmethod
    def _format_custom_reply_text(text: str, values: dict[str, object]) -> str:
        if not isinstance(text, str) or len(text) > 4096 or not isinstance(values, dict):
            return str(text)[:4096]
        fields = {"uid", "gid", "nick", "pc", "fromUser", "fromQQ", "fromGroup", "fromMsg", "gender", "suffix"}
        regex_full_match = values.get("_regex_full_match")
        regex_captures = values.get("_regex_captures")
        output: list[str] = []
        index = 0
        replacements = 0
        while index < len(text):
            if (
                isinstance(regex_full_match, str)
                and text[index] == "$"
                and index + 1 < len(text)
                and text[index + 1] in "&$`'"
                and replacements < 100
            ):
                token = text[index + 1]
                if token == "&":
                    output.append(regex_full_match[:500])
                elif token == "$":
                    output.append("$")
                else:
                    output.append("")
                index += 2
                replacements += 1
                continue
            if (
                isinstance(regex_full_match, str)
                and text[index] == "$"
                and index + 1 < len(text)
                and text[index + 1] in "123456789"
                and isinstance(regex_captures, (tuple, list))
                and replacements < 100
            ):
                consumed = 1
                group_number = int(text[index + 1])
                if index + 2 < len(text) and text[index + 2].isascii() and text[index + 2].isdigit():
                    group_number = int(text[index + 1:index + 3])
                    consumed = 2
                if 1 <= group_number <= min(99, len(regex_captures)):
                    captured = regex_captures[group_number - 1]
                    output.append(captured[:500] if isinstance(captured, str) else "")
                    index += consumed + 1
                    replacements += 1
                    continue
            if text[index] == "\\" and index + 1 < len(text) and text[index + 1] == "{":
                depth = 1
                cursor = index + 2
                while cursor < len(text):
                    char = text[cursor]
                    if char in "{}" and text[cursor - 1] != "\\":
                        depth += 1 if char == "{" else -1
                        if depth == 0:
                            break
                    cursor += 1
                if depth == 0:
                    output.append("{")
                    index += 2
                    continue
            if text[index] == "{" and (index == 0 or text[index - 1] not in "\\{"):
                close = text.find("}", index + 1, min(len(text), index + 34))
                if close >= 0 and (close + 1 >= len(text) or text[close + 1] != "}"):
                    field = text[index + 1:close]
                    if field in fields and field in values and replacements < 100:
                        value = values[field]
                        limit = 500 if field in {"suffix", "fromMsg"} else 128
                        output.append(value[:limit] if isinstance(value, str) else "")
                        index = close + 1
                        replacements += 1
                        continue
            output.append(text[index])
            index += 1
        return "".join(output)[:4096]

    async def _match_custom_reply(self, text: str, group: dict, sender_id: str = "", raw_group_id: str = "", group_scope: str = "", user_scope: str = "", usage_check: Callable[[str, str, dict], bool | str] | None = None, sender_name: str = "") -> tuple[str | list[str] | None, tuple[tuple[tuple[int, str, str, str], asyncio.Lock], ...]]:
        if group.get("custom_replies_enabled", True) is False or not isinstance(text, str) or len(text) > 4096:
            return None, ()
        key = text.strip().casefold()
        nickname = self._nickname_for_user(user_scope, group_scope) if user_scope and group_scope else None
        if not nickname and isinstance(sender_name, str):
            nickname = sender_name.strip()
        player_state = self.store.player(user_scope) if isinstance(user_scope, str) and user_scope else {}
        card_scope = group_scope if isinstance(group_scope, str) and group_scope else None
        active_card_name = self._active_card_name(player_state, card_scope)
        pc_name = active_card_name if isinstance(active_card_name, str) and active_card_name != "角色卡" else (nickname or sender_id)
        if not isinstance(pc_name, str):
            pc_name = ""
        format_values = {
            "uid": sender_id[:128] if isinstance(sender_id, str) else "",
            "gid": raw_group_id[:128] if isinstance(raw_group_id, str) and raw_group_id else "0",
            "nick": (nickname or (sender_id if isinstance(sender_id, str) else ""))[:128],
            "pc": pc_name[:128],
            "fromUser": sender_id[:128] if isinstance(sender_id, str) else "",
            "fromQQ": sender_id[:128] if isinstance(sender_id, str) else "",
            "fromGroup": raw_group_id[:128] if isinstance(raw_group_id, str) and raw_group_id else "",
            "fromMsg": text[:500],
        }
        stored_player = player_state
        stored_gender = stored_player.get("gender") if isinstance(stored_player, dict) else None
        if isinstance(stored_gender, str) and len(stored_gender) <= 128 and not any(ord(char) < 32 or 127 <= ord(char) < 160 for char in stored_gender):
            format_values["gender"] = stored_gender
        notices: list[str] = []

        def with_notices(response: str | None) -> str | list[str] | None:
            if not notices:
                return response
            messages = notices[:3]
            if response is not None:
                messages.append(response)
            return messages

        async def find(field: str, predicate: Callable[[str], bool], *, preserve_pattern: bool = False) -> tuple[str | None, tuple[tuple[tuple[int, str, str, str], asyncio.Lock], ...]]:
            rules = group.get(field, {})
            if not isinstance(rules, dict):
                return None, ()
            if preserve_pattern and len(text) > MAX_SAFE_REGEX_INPUT_LENGTH:
                return None, ()
            matches: list[tuple[str, object, tuple[str | None, ...] | None]] = []
            for index, (trigger, response) in enumerate(rules.items()):
                if index >= (20 if preserve_pattern else 100):
                    break
                raw_trigger = str(trigger)
                normalized = raw_trigger if preserve_pattern else raw_trigger.strip().casefold()
                maximum = 128 if preserve_pattern else 80
                set_info = Main._custom_reply_set_info(response)
                if not 1 <= len(normalized) <= maximum or Main._custom_reply_display(response) is None:
                    continue
                if set_info is not None:
                    mode_for_field = {"custom_replies": "exact", "custom_reply_prefixes": "prefix", "custom_reply_searches": "search", "custom_reply_regexes": "regex"}.get(field)
                    registered = set_info["modes"].get(mode_for_field, []) if mode_for_field else []
                    if normalized not in (registered if preserve_pattern else [item.casefold() for item in registered]):
                        continue
                capture_values = None
                if preserve_pattern:
                    try:
                        capture_values = compile_safe_regex(normalized).fullmatch_captures(text)
                        matched = capture_values is not None
                    except SafeRegexError:
                        continue
                else:
                    matched = predicate(normalized)
                if matched:
                    matches.append((normalized, response, capture_values))
            matches.sort(key=lambda item: (-len(item[0]), item[0].casefold(), item[0]))
            for trigger, response, capture_values in matches:
                selected = response
                handles: tuple[tuple[tuple[int, str, str, str], asyncio.Lock], ...] = ()
                transferred = False
                try:
                    set_info = Main._custom_reply_set_info(selected)
                    if set_info is not None:
                        identity = set_info["identity"]
                        identity_key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
                        selected = set_info["response"]
                        limits = set_info["limits"]
                        if limits is not None:
                            limits = Main._normalize_custom_reply_limits(limits)
                            if limits is None or not Main._custom_reply_limit_allows(limits, sender_id, raw_group_id):
                                continue
                            if "lock" in limits:
                                acquired = await self._acquire_custom_reply_locks(limits["lock"]["locks"], group_scope, user_scope, identity_key)
                                if acquired is None:
                                    continue
                                handles = acquired
                            if usage_check is not None and ("cd" in limits or "today" in limits):
                                usage_result = usage_check("custom_reply_set", identity_key, limits)
                                if isinstance(usage_result, str):
                                    if usage_result and usage_result not in notices and len(notices) < 3:
                                        notices.append(usage_result)
                                    continue
                                if not usage_result:
                                    continue
                    elif isinstance(selected, dict) and selected.get("kind") == "limited":
                        limits = Main._normalize_custom_reply_limits(selected.get("limits"))
                        if limits is None or not Main._custom_reply_limit_allows(limits, sender_id, raw_group_id):
                            continue
                        if "lock" in limits:
                            acquired = await self._acquire_custom_reply_locks(limits["lock"]["locks"], group_scope, user_scope, trigger)
                            if acquired is None:
                                continue
                            handles = acquired
                        if usage_check is not None and ("cd" in limits or "today" in limits):
                            usage_result = usage_check(field, trigger, limits)
                            if isinstance(usage_result, str):
                                if usage_result and usage_result not in notices and len(notices) < 3:
                                    notices.append(usage_result)
                                continue
                            if not usage_result:
                                continue
                        selected = selected.get("response")
                    reply_format_values = format_values
                    if field == "custom_reply_regexes":
                        reply_format_values = dict(format_values)
                        reply_format_values["_regex_full_match"] = text
                        reply_format_values["_regex_captures"] = capture_values or ()
                    elif field == "custom_reply_prefixes":
                        original_message = text.strip()
                        prefix_end = Main._casefold_prefix_end(original_message, trigger)
                        if prefix_end is not None:
                            reply_format_values = dict(format_values)
                            reply_format_values["suffix"] = original_message[prefix_end:]
                    choices = Main._custom_reply_deck_choices(selected)
                    if choices is not None:
                        draw = secrets.randbelow(sum(weight for _, weight, _ in choices))
                        for _, weight, value in choices:
                            if draw < weight:
                                transferred = True
                                return Main._format_custom_reply_text(value, reply_format_values), handles
                        continue
                    if isinstance(selected, str):
                        transferred = True
                        return Main._format_custom_reply_text(selected, reply_format_values), handles
                finally:
                    if handles and not transferred:
                        self._release_custom_reply_locks(handles)
            return None, ()

        exact, handles = await find("custom_replies", lambda trigger: trigger == key)
        if exact is not None:
            return with_notices(exact), handles
        prefix, handles = await find("custom_reply_prefixes", key.startswith)
        if prefix is not None:
            return with_notices(prefix), handles
        search, handles = await find("custom_reply_searches", lambda trigger: trigger in key)
        if search is not None:
            return with_notices(search), handles
        regex, handles = await find("custom_reply_regexes", lambda _: False, preserve_pattern=True)
        return with_notices(regex), handles

    def _consume_custom_reply_usage(self, group_id: str, field: str, trigger: str, limits: dict, user_scope: str = "") -> bool | str:
        normalized = Main._normalize_custom_reply_limits(limits)
        if normalized is None:
            return False
        def blocked_notice(config: dict | None) -> bool | str:
            notice = config.get("notice") if isinstance(config, dict) else None
            return notice if isinstance(notice, str) and notice else False
        cooldown_config = normalized.get("cd")
        daily_config = normalized.get("today")
        if cooldown_config is None and daily_config is None:
            return True
        if not isinstance(field, str) or len(field) > 64 or not isinstance(trigger, str) or not 1 <= len(trigger) <= 80:
            return False

        def scope_identity(config: dict) -> tuple[str, str] | None:
            scope = config["scope"]
            if scope == "chat":
                identity = group_id
            elif scope == "user":
                identity = user_scope
            else:
                identity = ""
            if scope != "global" and (not isinstance(identity, str) or not identity or len(identity) > 512):
                return None
            return scope, identity

        cooldown_key = None
        cooldown = None
        if cooldown_config is not None:
            scoped = scope_identity(cooldown_config)
            if scoped is None:
                return False
            cooldown_scope, cooldown_identity = scoped
            cooldown = cooldown_config["value"]
            cooldown_key = (cooldown_scope, cooldown_identity, field, trigger)
        daily_scope = daily_identity = daily_limit = None
        if daily_config is not None:
            scoped = scope_identity(daily_config)
            if scoped is None:
                return False
            daily_scope, daily_identity = scoped
            daily_limit = daily_config["value"]

        with self._custom_reply_limit_lock:
            now = time.monotonic()
            day_key = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat() if daily_limit is not None else ""
            if cooldown and cooldown_key is not None:
                if self._custom_reply_cooldowns.get(cooldown_key, 0.0) > now:
                    return blocked_notice(cooldown_config)
                if len(self._custom_reply_cooldowns) >= 10_000:
                    self._custom_reply_cooldowns = {key: until for key, until in self._custom_reply_cooldowns.items() if until > now}
                    if len(self._custom_reply_cooldowns) >= 10_000 and cooldown_key not in self._custom_reply_cooldowns:
                        return False
            if daily_limit is not None:
                usage_key = f"{field}:{trigger}"
                def next_usage(usage: object, count_key: str, max_entries: int) -> tuple[dict[str, dict], bool] | None:
                    if not isinstance(usage, dict):
                        return None
                    if len(usage) > 10_000 or not isinstance(count_key, str) or not 1 <= len(count_key) <= 1024:
                        return None
                    clean: dict[str, dict] = {}
                    current = usage.get(count_key)
                    for stored_key, entry in usage.items():
                        if stored_key == count_key:
                            continue
                        if not isinstance(stored_key, str) or len(stored_key) > 1024 or not isinstance(entry, dict):
                            continue
                        count = entry.get("count")
                        if entry.get("date") != day_key or isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= 10_000:
                            continue
                        if len(clean) < max_entries - 1:
                            clean[stored_key] = {"date": day_key, "count": count}
                    current_count = 0
                    if current is not None:
                        if not isinstance(current, dict):
                            return None
                        stored_day = current.get("date")
                        if not isinstance(stored_day, str):
                            return None
                        try:
                            parsed_day = datetime.fromisoformat(stored_day)
                        except ValueError:
                            return None
                        if parsed_day.date().isoformat() != stored_day:
                            return None
                        if stored_day != day_key:
                            current_count = 0
                        else:
                            value = current.get("count")
                            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10_000:
                                return None
                            current_count = value
                    allowed = current_count < daily_limit
                    clean[count_key] = {"date": day_key, "count": current_count + (1 if allowed else 0)}
                    return clean, allowed

                if daily_scope == "chat":
                    def record_chat(group: dict) -> bool | None:
                        updated = next_usage(group.get("custom_reply_usage"), usage_key, 100)
                        if updated is None:
                            return None
                        group["custom_reply_usage"], allowed = updated
                        return allowed
                    chat_allowed = self._mutate_group(group_id, record_chat)
                    if chat_allowed is None:
                        return False
                    if not chat_allowed:
                        return blocked_notice(daily_config)
                else:
                    shared_key = json.dumps([daily_scope, daily_identity, usage_key], ensure_ascii=False, separators=(",", ":"))
                    if len(shared_key) > 1024:
                        return False
                    def record_shared(state: dict) -> bool | None:
                        root = state.get("reply_usage")
                        if not isinstance(root, dict):
                            return None
                        if "custom_replies" not in root:
                            usage = {}
                        else:
                            usage = root["custom_replies"]
                            if not isinstance(usage, dict):
                                return None
                        updated = next_usage(usage, shared_key, 10_000)
                        if updated is None:
                            return None
                        updated_usage, allowed = updated
                        root["custom_replies"] = updated_usage
                        state["reply_usage"] = root
                        return allowed
                    shared_allowed = self.store.transact(record_shared)
                    if shared_allowed is None:
                        return False
                    if not shared_allowed:
                        return blocked_notice(daily_config)
            if cooldown and cooldown_key is not None:
                self._custom_reply_cooldowns[cooldown_key] = now + cooldown
            return True

    @staticmethod
    def _parse_reply_set_spec(raw: str) -> tuple[str, dict[str, list[str]], object, dict | None]:
        if not isinstance(raw, str) or not raw.strip() or len(raw) > 4096:
            raise ValueError("用法：.reply set [Title:标题] [Type:Reply] [Limit:限制] [Match:词1|词2] [Prefix:词1|词2] [Search:词1|词2] [Regex:规则] <Tab>Text:回复 或 Deck:选项1|选项2；Title 可独立命名；省略或为空时取最后一个匹配字段原文；同 Title 多模式/别名共享限制")
        values: dict[str, str] = {}
        last_mode_keyword: str | None = None
        response_kind = None
        response_value = None
        cursor = 0
        allowed = {"title", "type", "limit", "match", "prefix", "search", "regex"}
        while cursor < len(raw):
            tab = raw.find("\t", cursor)
            token_end = len(raw) if tab < 0 else tab
            token = raw[cursor:token_end]
            candidates = [(index, char) for char in (":", "=") if (index := token.find(char)) >= 0]
            if not candidates:
                raise ValueError("每个字段须使用 字段:值 或 字段=值，并以 Tab 分隔")
            separator_at, _ = min(candidates)
            name = token[:separator_at].strip().casefold()
            if not name:
                raise ValueError("字段名不能为空")
            value_start = cursor + separator_at + 1
            if name in {"text", "deck"}:
                response_kind = name
                response_value = raw[value_start:].strip()
                cursor = len(raw)
                break
            value = raw[value_start:token_end].strip()
            if name not in allowed:
                raise ValueError("仅支持 Title/Type/Limit/Match/Prefix/Search/Regex 与 Text/Deck；脚本、Order 均拒绝")
            if name in values:
                raise ValueError(f"字段 {name} 不能重复")
            values[name] = value
            if name in {"match", "prefix", "search", "regex"}:
                last_mode_keyword = value
            cursor = len(raw) if tab < 0 else tab + 1
        mode_names = {"match": "exact", "prefix": "prefix", "search": "search", "regex": "regex"}
        modes: dict[str, list[str]] = {}
        for field_name in ("match", "prefix", "search", "regex"):
            if field_name not in values:
                continue
            mode = mode_names[field_name]
            raw_triggers = [values[field_name]] if mode == "regex" else values[field_name].split("|")
            if not 1 <= len(raw_triggers) <= 32:
                raise ValueError("每种匹配模式须有 1–32 个触发项")
            triggers = []
            seen = set()
            for raw_trigger in raw_triggers:
                trigger = raw_trigger.strip()
                if any(ord(char) < 32 for char in trigger):
                    raise ValueError("触发词不能包含控制字符")
                maximum = 128 if mode == "regex" else 80
                if not 1 <= len(trigger) <= maximum:
                    raise ValueError(f"触发项须为 1–{maximum} 字")
                identity = trigger if mode == "regex" else trigger.casefold()
                if identity in seen:
                    raise ValueError("同一模式内不能重复触发项")
                seen.add(identity)
                if mode == "regex":
                    try:
                        compile_safe_regex(trigger)
                    except SafeRegexError as exc:
                        raise ValueError(f"安全 Regex 子集不接受该表达式：{exc}") from exc
                triggers.append(trigger)
            modes[mode] = triggers
        if not modes:
            raise ValueError("至少指定一个 Match、Prefix、Search 或 Regex 字段")
        title = values.get("title") or last_mode_keyword or ""
        if not 1 <= len(title) <= 128 or any(ord(char) < 32 for char in title):
            raise ValueError("Title 须为 1–128 个无控制字符文本")
        if "type" in values and values["type"].casefold() != "reply":
            raise ValueError("只支持 Type:Reply；Order/Both/Game 等命令类型不执行")
        if response_kind is None or response_value is None:
            raise ValueError("须在模式字段后指定 Text 或 Deck 回复")
        if response_kind == "text":
            response = response_value
            if not 1 <= len(response) <= 500:
                raise ValueError("Text 回复须为 1–500 字")
        else:
            if "\t" in response_value:
                raise ValueError("Deck 字段只支持以 | 分隔的字面选项，不支持 Tab 分项")
            choices = [item.strip() for item in response_value.split("|")]
            response = {"kind": "deck", "choices": choices}
            if Main._custom_reply_deck_choices(response) is None:
                raise ValueError("Deck 只支持 2–32 个静态选项（可选静态整数权重），不执行表达式或占位符")
        limits = None
        if "limit" in values and values["limit"]:
            limits = Main._parse_custom_reply_limit_spec(values["limit"])
            if limits is None:
                raise ValueError("Limit 使用本插件安全子集：prob、user_id、grp_id、cd、today、lock（最多4把 chat/user/global 互斥）；不支持变量")
        return title, modes, response, limits

    def _reply_command(self, args: str, group_id: str, is_admin: bool, is_private: bool = False) -> str:
        action, _, rest = args.partition(" ")
        action = action.casefold() or "list"
        fields = {"exact": "custom_replies", "prefix": "custom_reply_prefixes", "search": "custom_reply_searches", "regex": "custom_reply_regexes"}
        labels = {"exact": "精确", "prefix": "前缀", "search": "包含", "regex": "正则（有界子集）"}
        aliases = {"exact": "exact", "prefix": "prefix", "search": "search", "contains": "search", "fuzzy": "search", "regex": "regex"}

        def parse_target(value: str) -> tuple[str, str]:
            first, separator, tail = value.strip().partition(" ")
            if separator and first.casefold() in aliases:
                return aliases[first.casefold()], tail.strip()
            return "exact", value.strip()

        def has_explicit_mode(value: str) -> bool:
            first, separator, _ = value.strip().partition(" ")
            return bool(separator and first.casefold() in aliases)

        def rule_key(mode: str, trigger: str) -> str:
            return trigger if mode == "regex" else trigger.casefold()

        def rule_count(group: dict) -> int:
            identities = set()
            for field in set(fields.values()):
                rules = group.get(field)
                if isinstance(rules, dict):
                    for key, value in rules.items():
                        info = Main._custom_reply_set_info(value)
                        identities.add(("set", info["identity"]) if info is not None else (field, str(key)))
                        if len(identities) >= 100:
                            return len(identities)
            return len(identities)

        def checked_reply_maps(group: dict) -> dict[str, dict]:
            result = {}
            for field in set(fields.values()):
                rules = group.get(field)
                if not isinstance(rules, dict):
                    raise ValueError("自定义回复状态格式损坏；为避免覆盖数据，管理操作已拒绝。")
                for value in rules.values():
                    if isinstance(value, dict) and value.get("kind") == "reply_set" and Main._custom_reply_set_info(value) is None:
                        raise ValueError("标题规则状态损坏；为避免覆盖数据，管理操作已拒绝。")
                result[field] = rules
            return result

        def reply_set_matches(maps: dict[str, dict], query: str, mode: str | None = None) -> list[dict]:
            normalized_query = query.casefold()
            title_matches: dict[str, dict] = {}
            alias_matches: dict[str, dict] = {}
            for stored_mode, field in fields.items():
                rules = maps[field]
                for value in rules.values():
                    info = Main._custom_reply_set_info(value)
                    if info is None:
                        continue
                    identity = info["identity"]
                    if identity == normalized_query:
                        title_matches[identity] = info
                    for registered_mode, triggers in info["modes"].items():
                        if mode is not None and mode != registered_mode:
                            continue
                        for trigger in triggers:
                            matches = trigger == query if registered_mode == "regex" else trigger.casefold() == normalized_query
                            if matches:
                                alias_matches[identity] = info
            return list((title_matches or alias_matches).values())

        def remove_reply_set(maps: dict[str, dict], identity: str) -> bool:
            removed = False
            for rules in maps.values():
                for key, value in list(rules.items()):
                    info = Main._custom_reply_set_info(value)
                    if info is not None and info["identity"] == identity:
                        del rules[key]
                        removed = True
            return removed

        if action in {"on", "off"}:
            if is_private:
                return "自定义回复开关仅限群聊使用。"
            if not is_admin:
                return "只有群管理员可以开关本群自定义回复。"
            enabled = action == "on"
            def apply(group: dict) -> bool:
                current = group.get("custom_replies_enabled", True)
                if not isinstance(current, bool):
                    current = True
                changed = current != enabled
                group["custom_replies_enabled"] = enabled
                return changed
            changed = self._mutate_group(group_id, apply)
            state = "开启" if enabled else "关闭"
            return f"本群自定义回复已{state}。" if changed else f"本群自定义回复已经{state}。"
        if not is_admin:
            return "只有群管理员可以管理本群自定义回复。"
        if action not in {"on", "off"}:
            try:
                checked_reply_maps(self._group_snapshot(group_id))
            except ValueError as exc:
                return str(exc)
        if action == "set":
            title, modes, response, limits = Main._parse_reply_set_spec(rest)
            identity = title.casefold()
            rule_record = {"kind": "reply_set", "title": title, "modes": modes, "response": response, "limits": limits}

            def save_upstream_set(group: dict) -> bool:
                maps = checked_reply_maps(group)
                existed = False
                # Replace one logical rule across all of its per-trigger storage aliases.
                for rules in maps.values():
                    for stored_key, stored_value in list(rules.items()):
                        info = Main._custom_reply_set_info(stored_value)
                        if info is not None and info["identity"] == identity:
                            del rules[stored_key]
                            existed = True

                new_alias_names = {trigger.casefold() for triggers in modes.values() for trigger in triggers}
                for existing_rules in maps.values():
                    for existing_key, existing_value in existing_rules.items():
                        existing_info = Main._custom_reply_set_info(existing_value)
                        if existing_info is not None:
                            if existing_info["identity"] == identity:
                                continue
                            existing_alias_names = {trigger.casefold() for triggers in existing_info["modes"].values() for trigger in triggers}
                            if identity in existing_alias_names or existing_info["identity"] in new_alias_names:
                                raise ValueError("Title 不能与其他规则的标题或触发别名冲突")
                        else:
                            existing_name = str(existing_key).casefold()
                            if existing_name == identity and existing_name not in new_alias_names:
                                raise ValueError("Title 不能与现有触发词冲突")

                aliases: list[tuple[str, str, str]] = []
                for mode, triggers in modes.items():
                    field = fields[mode]
                    for trigger in triggers:
                        key = rule_key(mode, trigger)
                        aliases.append((mode, field, key))
                        for other_mode, other_field in fields.items():
                            if other_mode == mode:
                                continue
                            for existing_key, existing_value in maps[other_field].items():
                                other_info = Main._custom_reply_set_info(existing_value)
                                if other_info is not None:
                                    registered = other_info["modes"].get(other_mode, [])
                                    collides = any(item.casefold() == trigger.casefold() for item in registered)
                                else:
                                    collides = str(existing_key).casefold() == trigger.casefold()
                                if collides:
                                    raise ValueError("该触发词已存在于其他匹配模式；先显式删除原规则再设置")
                        existing_value = maps[field].get(key)
                        if existing_value is not None:
                            existing_info = Main._custom_reply_set_info(existing_value)
                            if existing_info is not None:
                                raise ValueError("该触发词已属于其他标题规则；先按标题删除原规则再设置")
                            existed = True

                # Legacy single-trigger entries may be upgraded to a titled rule.
                for _, field, key in aliases:
                    maps[field].pop(key, None)
                regex_rules = maps[fields["regex"]]
                if "regex" in modes and len(regex_rules) >= 20:
                    raise ValueError("每个会话最多保存 20 条安全 Regex 规则")
                if rule_count(group) >= 100:
                    raise ValueError("每个会话最多保存 100 条自定义回复")
                for mode, field, key in aliases:
                    maps[field][key] = rule_record
                return existed

            replaced = self._mutate_group(group_id, save_upstream_set)
            return "已按安全兼容子集更新 .reply set。" if replaced else "已按安全兼容子集添加 .reply set。"
        if action in {"re", "regex"}:
            pattern, separator, reply_text = rest.partition(" ")
            if not pattern:
                raise ValueError("用法：.reply re <正则模式> [回复文本]；省略回复文本删除该规则")
            if any(ord(char) < 32 for char in pattern):
                raise ValueError("Regex 模式不能包含控制字符")
            try:
                compile_safe_regex(pattern)
            except SafeRegexError as exc:
                raise ValueError(f"安全 Regex 子集不接受该表达式：{exc}") from exc
            if not separator or not reply_text.strip():
                return self._reply_command(f"del regex {pattern}", group_id, is_admin, is_private)
            return self._reply_command(f"set Regex:{pattern}\tText:{reply_text.strip()}", group_id, is_admin, is_private)
        if action == "limit":
            mode, payload = parse_target(rest)
            if mode == "regex":
                separator = payload.rfind(" | ")
                if separator < 0:
                    raise ValueError("Regex 限制使用 .reply limit regex <表达式> | 限制；分隔符须为带空格的竖线")
                trigger, raw_limits = payload[:separator].strip(), payload[separator + 3:].strip()
            else:
                if "|" not in payload:
                    raise ValueError("用法：.reply limit [exact|prefix|search|regex] 触发词 | prob:50;user_id:123,456;grp_id:!789;cd:30&@echo=冷却中;today:5&@echo=日额已满")
                trigger, raw_limits = (part.strip() for part in payload.split("|", 1))
            max_trigger_length = 128 if mode == "regex" else 80
            if not 1 <= len(trigger) <= max_trigger_length or not raw_limits or len(raw_limits) > 512:
                raise ValueError(f"触发词须为 1-{max_trigger_length} 字，限制说明最多 512 字")
            if mode == "regex":
                compile_safe_regex(trigger)
            clear_limits = raw_limits.casefold() == "clear"
            limits = None if clear_limits else Main._parse_custom_reply_limit_spec(raw_limits)
            if not clear_limits and limits is None:
                raise ValueError("限制支持 prob:0-100、user_id/grp_id 条件、chat/@user/@global 范围的 cd/today（cd≤86400 秒、today≤10000 次），并可附加 &@echo=提示（纯文本，≤400 字）")
            key = rule_key(mode, trigger)
            field = fields[mode]
            def set_limits(group: dict) -> str:
                maps = checked_reply_maps(group)
                rules = maps[field]
                if key not in rules:
                    set_matches = reply_set_matches(maps, trigger, mode if has_explicit_mode(rest) else None)
                    if len(set_matches) > 1:
                        raise ValueError("该回复标题或别名不唯一；请使用明确的匹配模式和触发词")
                    if set_matches:
                        info = set_matches[0]
                        existing = next((value for candidate_rules in maps.values() for value in candidate_rules.values() if (stored_info := Main._custom_reply_set_info(value)) is not None and stored_info["identity"] == info["identity"]), None)
                        if existing is None:
                            return "missing"
                        updated = dict(existing)
                        if clear_limits and info["limits"] is None:
                            return "unchanged"
                        updated["limits"] = None if clear_limits else limits
                        for candidate_rules in maps.values():
                            for stored_key, value in list(candidate_rules.items()):
                                stored_info = Main._custom_reply_set_info(value)
                                if stored_info is not None and stored_info["identity"] == info["identity"]:
                                    candidate_rules[stored_key] = updated
                        return "cleared" if clear_limits else "updated"
                    return "missing"
                existing = rules[key]
                if Main._custom_reply_display(existing) is None:
                    raise ValueError("该回复数据格式无效，未应用限制")
                set_info = Main._custom_reply_set_info(existing)
                if set_info is not None:
                    if clear_limits and set_info["limits"] is None:
                        return "unchanged"
                    updated = dict(existing)
                    updated["limits"] = None if clear_limits else limits
                    for candidate_rules in maps.values():
                        for stored_key, value in list(candidate_rules.items()):
                            stored_info = Main._custom_reply_set_info(value)
                            if stored_info is not None and stored_info["identity"] == set_info["identity"]:
                                candidate_rules[stored_key] = updated
                    return "cleared" if clear_limits else "updated"
                is_limited = isinstance(existing, dict) and existing.get("kind") == "limited"
                base = existing.get("response") if is_limited else existing
                if clear_limits:
                    if not is_limited:
                        return "unchanged"
                    rules[key] = base
                    return "cleared"
                rules[key] = {"kind": "limited", "response": base, "limits": limits}
                return "updated"
            result = self._mutate_group(group_id, set_limits)
            return {"missing": "找不到该触发词。", "unchanged": "该回复没有限制条件。", "cleared": "已清除该回复的触发限制。", "updated": "已设置该回复的触发限制。"}[result]
        if action in {"add", "prefix", "search", "contains", "fuzzy", "deck"}:
            if action == "deck":
                mode, payload = parse_target(rest)
                if mode == "regex":
                    raise ValueError("Regex Deck 请使用 .reply set Regex:<表达式>	Deck:选项1|选项2")
                parts = [part.strip() for part in payload.split("|")]
                if len(parts) < 3:
                    raise ValueError("用法：.reply deck [exact|prefix|search] 触发词 | 选项1 | 标签::权重::选项2")
                trigger, choices = parts[0], parts[1:]
                response = {"kind": "deck", "choices": choices}
                if Main._custom_reply_deck_choices(response) is None:
                    raise ValueError("Deck 回复需 2-32 个选项、单项文本最多 500 字、总长≤4096 字；权重项格式 标签::整数权重::文本（1-1000，总权重≤10000）；不执行骰式表达式或花括号占位符")
            else:
                mode = "exact" if action == "add" else aliases[action]
                if "|" not in rest:
                    raise ValueError("用法：.reply add 触发词 | 回复内容；.reply prefix/search 触发词 | 回复内容")
                trigger, response = (part.strip() for part in rest.split("|", 1))
                if not 1 <= len(trigger) <= 80 or not 1 <= len(response) <= 500:
                    raise ValueError("触发词须为 1-80 字，回复内容须为 1-500 字")
            if not 1 <= len(trigger) <= 80:
                raise ValueError("触发词须为 1-80 字")
            key = rule_key(mode, trigger)
            field = fields[mode]
            def save(group: dict) -> bool:
                maps = checked_reply_maps(group)
                rules = maps[field]
                for existing_rules in maps.values():
                    for existing in existing_rules.values():
                        info = Main._custom_reply_set_info(existing)
                        if info is not None and info["identity"] == trigger.casefold():
                            raise ValueError("触发词与已存在标题冲突；请使用标题规则进行管理")
                current = rules.get(key)
                if Main._custom_reply_set_info(current) is not None:
                    raise ValueError("该触发词属于标题规则；请使用 .reply set 更新或 .reply del 删除")
                for other_mode, other_field in fields.items():
                    if other_mode == mode:
                        continue
                    for existing in maps[other_field].values():
                        info = Main._custom_reply_set_info(existing)
                        if info is not None and any(item.casefold() == trigger.casefold() for item in info["modes"].get(other_mode, [])):
                            raise ValueError("该触发词属于其他模式的标题规则；请使用 .reply set 或按标题删除")
                existed = key in rules
                if not existed and rule_count(group) >= 100:
                    raise ValueError("每个会话最多保存 100 条自定义回复")
                rules[key] = response
                return existed
            replaced = self._mutate_group(group_id, save)
            if action == "deck":
                return "已更新 Deck 回复。" if replaced else "已添加 Deck 回复。"
            return "已更新自定义回复。" if replaced else "已添加自定义回复。"
        if action in {"delete", "remove", "del"}:
            mode, trigger = parse_target(rest)
            if not trigger:
                raise ValueError("用法：.reply del [exact|prefix|search|regex] 触发词或标题")
            field = fields[mode]
            key = rule_key(mode, trigger)
            def remove(group: dict) -> bool:
                maps = checked_reply_maps(group)
                rules = maps[field]
                if key in rules:
                    info = Main._custom_reply_set_info(rules[key])
                    if info is not None:
                        return remove_reply_set(maps, info["identity"])
                    del rules[key]
                    return True
                matches = reply_set_matches(maps, trigger, mode if has_explicit_mode(rest) else None)
                if len(matches) > 1:
                    raise ValueError("标题或触发词对应多条规则；请补充匹配模式")
                return remove_reply_set(maps, matches[0]["identity"]) if matches else False
            try:
                removed = self._mutate_group(group_id, remove)
            except ValueError as exc:
                return str(exc)
            return "已删除自定义回复；若为标题规则则全部触发别名一并移除。" if removed else "找不到该触发词或标题。"
        if action in {"get", "show", "list"}:
            snapshot = self._group_snapshot(group_id)
            maps = checked_reply_maps(snapshot)
            mode, trigger = parse_target(rest)

            def find_entry(selected_mode: str, query: str):
                rules = maps[fields[selected_mode]]
                key = rule_key(selected_mode, query)
                direct = [value for stored, value in rules.items() if rule_key(selected_mode, str(stored)) == key]
                if len(direct) == 1:
                    value = direct[0]
                    info = Main._custom_reply_set_info(value)
                    display = Main._custom_reply_display(value)
                    return (display, info, selected_mode) if display is not None else None
                set_matches = reply_set_matches(maps, query, selected_mode if has_explicit_mode(rest) else None)
                if len(set_matches) == 1:
                    info = set_matches[0]
                    display = Main._custom_reply_display({"kind": "reply_set", "title": info["title"], "modes": info["modes"], "response": info["response"], "limits": info["limits"]})
                    return (display, info, None) if display is not None else None
                return None

            if action == "get":
                if not trigger:
                    raise ValueError("用法：.reply get [exact|prefix|search|regex] 触发词或标题")
                found = find_entry(mode, trigger)
                return found[0] if found is not None else "找不到该触发词或标题。"
            if action == "show" and trigger:
                found = find_entry(mode, trigger)
                if found is None:
                    return "找不到该触发词或标题。"
                display, info, matched_mode = found
                if info is not None:
                    mode_text = "；".join(f"{labels[selected]}:{'|'.join(items)}" for selected, items in info["modes"].items())
                    return f"匹配模式：标题规则\n标题：{info['title']}\n触发项：{mode_text}\n回复内容：{display}"
                return f"匹配模式：{labels[matched_mode]}\n触发词：{trigger}\n回复内容：{display}"
            rules_to_show: list[tuple[int, str, str, str]] = []
            seen_sets = set()
            for order, selected_mode in enumerate(("exact", "prefix", "search", "regex")):
                rules = maps[fields[selected_mode]]
                for index, (stored, response) in enumerate(rules.items()):
                    if index >= 100:
                        break
                    info = Main._custom_reply_set_info(response)
                    display = Main._custom_reply_display(response)
                    if display is None:
                        continue
                    if info is not None:
                        if info["identity"] in seen_sets:
                            continue
                        seen_sets.add(info["identity"])
                        mode_text = "；".join(f"{labels[selected]}:{'|'.join(items)}" for selected, items in info["modes"].items())
                        rules_to_show.append((order, info["title"], f"{mode_text} → {display}", "标题规则"))
                    else:
                        rules_to_show.append((order, str(stored), display, labels[selected_mode]))
            if not rules_to_show:
                return "本会话暂无自定义回复。用 .reply add/prefix/search、.reply deck 或 .reply set 添加；用 .reply limit 为已有回复设置触发条件。"
            rules_to_show.sort(key=lambda item: (item[0], item[1].casefold()))
            shown = rules_to_show[:20]
            lines = [f"[标题规则] {name} → {response}" if mode_label == "标题规则" else f"{name} → {response}" if mode_label == "精确" else f"[{mode_label}] {name} → {response}" for _, name, response, mode_label in shown]
            suffix = f"\n另有 {len(rules_to_show) - 20} 条未显示。" if len(rules_to_show) > 20 else ""
            return "自定义回复：\n" + "\n".join(lines) + suffix
        raise ValueError("用法：.reply on|off|list|show/get [exact|prefix|search|regex] 触发词|set [Title:触发词] [Type:Reply] [Limit:限制] Match|Prefix|Search|Regex:触发词<Tab>Text:回复 或 Deck:选项1|选项2|add/prefix/search 触发词 | 回复内容|deck [模式] 触发词 | 选项1 | 标签::权重::选项2|limit [模式] 触发词 | prob:50;user_id:123;grp_id:!456/clear|del [模式] 触发词")

    def _bot(self, args: str, group_id: str, is_admin: bool, is_private: bool = False, bot_id: str = "") -> str:
        if is_private:
            return ".bot 仅允许在群聊中使用。"
        parts = args.split()
        if not parts or parts[0].casefold() not in {"on", "off", "status"} or len(parts) > 2 or (parts[0].casefold() == "status" and len(parts) != 1):
            raise ValueError("用法：.bot on|off|status [当前 bot 数字 ID（全号或末四位）]")
        action = parts[0].casefold()
        if action != "status" and not is_admin:
            return "只有群管理员可以开关本群 Dice 插件。"
        if len(parts) == 2:
            requested_id = parts[1]
            current_id = bot_id.strip()
            if not requested_id.isascii() or not requested_id.isdecimal() or len(requested_id) > 20:
                return "目标 bot ID 必须为不超过 20 位的数字；未修改。"
            if not current_id.isascii() or not current_id.isdecimal() or not current_id or len(current_id) > 20:
                return "当前 AstrBot bot ID 不可验证，拒绝执行定向开关。"
            requested_number = int(requested_id)
            current_number = int(current_id)
            matches = requested_number == current_number or (len(requested_id) == 4 and requested_number == current_number % 10000)
            if not matches:
                return "目标 bot ID 与当前 AstrBot bot 不匹配；未修改。"
        if action == "status":
            return "本群 Dice 插件：" + ("开启" if self._group_snapshot(group_id).get("enabled", True) else "关闭")
        enabled = action == "on"
        def update(group: dict) -> bool:
            current = group.get("enabled", True)
            if type(current) is not bool:
                raise ValueError("本群开关状态损坏，未修改。")
            if current == enabled:
                return False
            group["enabled"] = enabled
            return True
        changed = self._mutate_group(group_id, update)
        if changed:
            return "本群 Dice 插件已开启。" if enabled else "本群 Dice 插件已关闭。"
        return "本群 Dice 插件已经开启。" if enabled else "本群 Dice 插件已经关闭。"

    def _ban(self, args: str, group_id: str, is_admin: bool) -> str:
        if not is_admin:
            return "只有群管理员可以管理本群 Dice 黑名单。"
        action, _, user_id = args.partition(" ")
        action, user_id = action.casefold(), user_id.strip()
        if action not in {"add", "remove"} or not user_id or len(user_id) > 128:
            raise ValueError("用法：.ban add|remove 用户ID")
        def apply(group: dict) -> bool:
            users = group.setdefault("banned_users", [])
            if action == "add":
                if user_id in users:
                    return False
                users.append(user_id)
                return True
            if user_id not in users:
                return False
            users.remove(user_id)
            return True
        changed = self._mutate_group(group_id, apply)
        if action == "add":
            return f"已将用户 {user_id} 加入本插件黑名单。" if changed else "该用户已在黑名单中。"
        return f"已将用户 {user_id} 移出本插件黑名单。" if changed else "该用户不在黑名单中。"

    @staticmethod
    def _is_private_group_state_key(key: str) -> bool:
        if key.startswith("private:"):
            return True
        marker = key.rfind(":private:")
        if marker < 0:
            return False
        try:
            identity = json.loads(key[:marker])
        except (TypeError, json.JSONDecodeError):
            return False
        return isinstance(identity, list) and 1 <= len(identity) <= 3 and all(isinstance(item, str) and item.strip() for item in identity)

    def _group_all_command(self, specification: str, is_admin: bool, is_private: bool) -> str:
        if not is_private or not is_admin:
            return "全群本地批量设置仅允许 AstrBot 全局管理员在私聊中执行。"
        raw = specification.strip()
        if len(raw) < 2 or raw[0] not in "+-":
            return "用法：.group all +选项|-选项（停用指令|禁用回复|禁用jrrp|禁用draw|禁用me|禁用help）。仅作用于插件已记录群，不触及私聊。"
        option = raw[1:].strip()
        mapping = GROUP_OPTION_FLAGS.get(option)
        if mapping is None:
            return "该上游群配置项未纳入本地安全子集；可用：停用指令、禁用回复、禁用jrrp、禁用draw、禁用me、禁用help。"
        field, set_value, default_value = mapping
        target = set_value if raw[0] == "+" else not set_value
        def apply(state: dict) -> int:
            groups = state.get("groups")
            if not isinstance(groups, dict) or len(groups) > 10000:
                raise ValueError("本地群状态损坏或超过 10000 项；批量操作已拒绝。")
            changed = 0
            for key, group in groups.items():
                if not isinstance(key, str) or not key or len(key) > 768 or any(ord(char) < 32 or ord(char) == 127 for char in key):
                    raise ValueError("本地群状态键损坏；批量操作已拒绝。")
                if self._is_private_group_state_key(key):
                    continue
                if not isinstance(group, dict):
                    raise ValueError("本地群状态损坏；批量操作已拒绝。")
                current = group.get(field, default_value)
                if type(current) is not bool:
                    raise ValueError("本地群开关状态损坏；批量操作已拒绝。")
                if current != target:
                    group[field] = target
                    changed += 1
            return changed
        try:
            changed = self.store.transact(apply)
        except ValueError as exc:
            return str(exc)
        return f"已更新 {changed} 个本地已记录群的配置项“{option}”；私聊会话未修改。"

    def _group_state_command(self, group_id: str, is_private: bool) -> str:
        if is_private:
            raise ValueError(".group state 仅支持群聊当前会话。")
        stored = self.store.group(group_id)
        if stored is None:
            group = self._default_group()
        elif isinstance(stored, dict):
            group = stored
        else:
            raise ValueError("本群插件状态损坏，拒绝输出。")
        rows = []
        for option, (field, disabled_value, default_value) in GROUP_OPTION_FLAGS.items():
            current = group.get(field, default_value)
            if type(current) is not bool:
                raise ValueError("本群配置开关数据损坏，拒绝输出。")
            rows.append(f"{option}：{'已禁用' if current is disabled_value else '已启用'}")
        return "本群插件本地配置：" + "；".join(rows)

    def _group_option_command(self, raw_args: str, group_id: str, is_admin: bool, is_private: bool) -> str:
        if is_private:
            raise ValueError("群配置仅支持群聊当前会话。")
        if not is_admin:
            raise ValueError("修改群配置需要当前群管理员。")
        raw = raw_args.strip()
        if not raw or len(raw) > 512:
            raise ValueError("用法：.group +选项|-选项；参数最多 512 字符。")
        tokens = []
        cursor = 0
        while cursor < len(raw):
            while cursor < len(raw) and raw[cursor].isspace():
                cursor += 1
            if cursor >= len(raw):
                break
            sign = raw[cursor]
            if sign not in "+-":
                raise ValueError("用法：.group +选项|-选项；选项可空格或符号分隔。")
            cursor += 1
            start = cursor
            while cursor < len(raw) and not raw[cursor].isspace() and raw[cursor] not in "+-":
                cursor += 1
            if start == cursor:
                raise ValueError("用法：.group +选项|-选项；选项不能为空。")
            tokens.append((sign, raw[start:cursor]))
            if len(tokens) > len(GROUP_OPTION_FLAGS):
                raise ValueError("一次最多设置六项已支持配置。")
        if not tokens:
            raise ValueError("用法：.group +选项|-选项；至少指定一项。")
        changes = []
        seen_fields = set()
        for sign, option in tokens:
            mapping = GROUP_OPTION_FLAGS.get(option)
            if mapping is None:
                raise ValueError("该上游群配置项未纳入本地安全子集；可用：" + "、".join(GROUP_OPTION_FLAGS) + "。")
            field, disabled_value, default_value = mapping
            if field in seen_fields:
                raise ValueError("同一配置项不能在一个请求中重复设置。")
            seen_fields.add(field)
            target = disabled_value if sign == "+" else default_value
            changes.append((option, field, disabled_value, default_value, target))

        def apply(state: dict) -> list[str]:
            groups = state.get("groups")
            if not isinstance(groups, dict) or len(groups) > 10000:
                raise ValueError("本地群状态损坏或超过 10000 项；未修改。")
            new_group = group_id not in groups
            if new_group:
                group = self._default_group()
            else:
                group = groups[group_id]
                if not isinstance(group, dict):
                    raise ValueError("本群插件状态损坏；未修改。")
            current_values = {}
            for option, field, disabled_value, default_value, target in changes:
                current = group.get(field, default_value)
                if type(current) is not bool:
                    raise ValueError("本群配置开关数据损坏；未修改。")
                current_values[field] = current
            changed = []
            for option, field, disabled_value, default_value, target in changes:
                if current_values[field] != target:
                    group[field] = target
                    changed.append(option)
            if changed and new_group:
                if len(groups) >= 10000:
                    raise ValueError("本地群状态已达到 10000 项上限；未修改。")
                groups[group_id] = group
            return changed

        try:
            changed = self.store.transact(apply)
        except ValueError as exc:
            return str(exc)
        if not changed:
            return "指定的本群配置已处于目标状态。"
        return "已更新本群配置：" + "、".join(changed) + "。"

    def _group_card_command(self, args: str, group_id: str, is_admin: bool, is_private: bool, platform_scope: str, sender_id: str = "") -> str:
        if is_private:
            raise ValueError(".group card 仅限群聊使用")
        match = re.fullmatch(r"card\s+([0-9]{1,20})\s+(.{1,80})", args.strip(), flags=re.IGNORECASE)
        if not match:
            raise ValueError("用法：.group card <数字用户 ID> <本人的角色卡名>（只修改当前群）")
        target_id, requested_name = match.group(1), match.group(2).strip()
        if not target_id.isascii() or not target_id.isdecimal() or not requested_name or any(ord(char) < 32 or 0x7F <= ord(char) <= 0x9F for char in requested_name):
            raise ValueError("用户 ID 或角色卡名无效")
        try:
            numeric_id = int(target_id)
        except ValueError as exc:
            raise ValueError("用户 ID 无效") from exc
        if not 0 < numeric_id <= 9_223_372_036_854_775_807:
            raise ValueError("用户 ID 超出支持范围")
        target_id = str(numeric_id)
        raw_sender_id = self._raw_platform_user_id(sender_id) if isinstance(sender_id, str) else ""
        sender_numeric_id = raw_sender_id.lstrip("0") or "0"
        target_numeric_id = target_id
        if not is_admin and (
            len(raw_sender_id) > 20
            or not raw_sender_id.isascii()
            or not raw_sender_id.isdecimal()
            or sender_numeric_id != target_numeric_id
        ):
            raise ValueError("普通成员只能切换本人的角色卡；修改他人角色卡需要群管理员")
        if not isinstance(platform_scope, str) or len(platform_scope) > 512:
            raise ValueError("无法确认当前平台/bot 身份，操作已拒绝")
        try:
            identity = json.loads(platform_scope)
        except (TypeError, json.JSONDecodeError):
            identity = None
        if not isinstance(identity, list) or len(identity) != 3 or not all(isinstance(item, str) and item.strip() and len(item) <= 128 for item in identity) or platform_scope != json.dumps(identity, ensure_ascii=False, separators=(",", ":")):
            raise ValueError("无法确认当前平台/bot 身份，操作已拒绝")
        group_prefix = platform_scope + ":"
        group_suffix = group_id[len(group_prefix):] if isinstance(group_id, str) and group_id.startswith(group_prefix) else ""
        if (
            not group_suffix
            or group_suffix.startswith("private:")
            or len(group_id) > 640
            or any(ord(char) < 32 or 0x7F <= ord(char) <= 0x9F for char in group_suffix)
        ):
            raise ValueError("无法确认当前群身份，操作已拒绝")
        target_key = f"{platform_scope}:user:{target_id}"

        def resolve_player(state: object) -> tuple[dict, str, dict]:
            if not isinstance(state, dict) or not isinstance(state.get("players"), dict):
                raise ValueError("玩家数据结构异常；为避免覆盖，操作已拒绝")
            players = state["players"]
            if len(players) > 100_000:
                raise ValueError("玩家数据超出安全上限；操作已拒绝")
            player = players.get(target_key)
            if not isinstance(player, dict):
                raise ValueError("目标用户没有本插件角色卡，未修改")
            cards = player.get("cards")
            if not isinstance(cards, dict) or not 1 <= len(cards) <= 33:
                raise ValueError("目标用户角色卡数据异常或为空；未修改")
            folded_names: set[str] = set()
            for card_name, card in cards.items():
                if not isinstance(card_name, str) or not 1 <= len(card_name) <= 64 or not isinstance(card, dict) or card_name.casefold() in folded_names:
                    raise ValueError("目标用户角色卡数据异常或存在重名冲突；未修改")
                folded_names.add(card_name.casefold())
            actual_name = self._card_name_key(cards, requested_name)
            if actual_name is None:
                raise ValueError("目标用户没有这张角色卡；未修改")
            raw_active = player.get("active_cards_by_group")
            if "active_cards_by_group" not in player:
                active_cards = {}
            elif not isinstance(raw_active, dict) or len(raw_active) > 10_000:
                raise ValueError("目标用户的群角色卡指针数据异常；未修改")
            else:
                active_cards = raw_active
                if any(not isinstance(key, str) or len(key) > 640 or not isinstance(value, str) or len(value) > 64 for key, value in active_cards.items()):
                    raise ValueError("目标用户的群角色卡指针数据异常；未修改")
            if len(active_cards) >= 10_000 and group_id not in active_cards:
                raise ValueError("目标用户的群角色卡指针已达安全上限；未修改")
            return player, actual_name, active_cards

        try:
            _, card_name, active_snapshot = resolve_player(self.store.snapshot())
        except ValueError:
            raise
        if active_snapshot.get(group_id) == card_name:
            return f"目标用户本群已使用角色卡「{card_name}」。"

        def assign(state: dict) -> bool:
            player, current_name, active_cards = resolve_player(state)
            if active_cards.get(group_id) == current_name:
                return False
            if "active_cards_by_group" not in player:
                player["active_cards_by_group"] = active_cards
            if "legacy_active_card" not in player:
                player["legacy_active_card"] = player.get("active_card")
            player["active_cards_by_group"][group_id] = current_name
            player["active_card"] = current_name
            return True

        try:
            changed = self.store.transact(assign)
        except ValueError as exc:
            return str(exc)
        return f"已将目标用户本群角色卡设为「{card_name}」。" if changed else f"目标用户本群已使用角色卡「{card_name}」。"

    @staticmethod
    def _group_info_text(value: object, limit: int) -> str | None:
        if not isinstance(value, str):
            return None
        cleaned = "".join(char for char in value if ord(char) >= 32 and not 0x7F <= ord(char) <= 0x9F)
        cleaned = " ".join(cleaned.split())
        return cleaned[:limit] or None

    def _group_info_command(self, args: str, raw_group_id: str, is_private: bool, group_info: object | None) -> str:
        if is_private:
            raise ValueError(".group info 仅支持当前群聊/频道，不支持私聊查询。")
        if args.strip():
            raise ValueError("本地 .group info 只显示当前消息所在群/频道；不支持查询其他群。")
        if not isinstance(raw_group_id, str) or not raw_group_id or len(raw_group_id) > 128 or any(char.isspace() or ord(char) < 32 or 0x7F <= ord(char) <= 0x9F for char in raw_group_id):
            raise ValueError("无法从当前 AstrBot 事件确认群/频道 ID。")
        safe_group_id = raw_group_id
        try:
            if isinstance(group_info, dict):
                meta_group_id = group_info.get("group_id")
                group_name = group_info.get("group_name")
                member_count = group_info.get("member_count")
            else:
                meta_group_id = getattr(group_info, "group_id", None)
                group_name = getattr(group_info, "group_name", None)
                member_count = getattr(group_info, "member_count", None)
        except Exception:
            meta_group_id = group_name = member_count = None
        if meta_group_id is not None and str(meta_group_id).strip() != raw_group_id.strip():
            group_name = None
            member_count = None
        safe_name = self._group_info_text(group_name, 80) or "本次事件未提供"
        safe_count = str(member_count) if type(member_count) is int and 0 <= member_count <= 2_147_483_647 else "本次事件未提供"
        return (
            "群信息（仅使用当前 AstrBot 消息事件元数据，不发起远程查询）：\n"
            f"群名：{safe_name}\n群/频道 ID：{safe_group_id}\n成员数：{safe_count}"
        )

    def _group_tojson_command(self, args: str, group_id: str, is_admin: bool, is_private: bool) -> str:
        if is_private:
            raise ValueError(".group tojson 仅支持当前群聊/频道。")
        if not is_admin:
            raise ValueError("本地 .group tojson 仅允许当前群管理员导出配置。")
        if args.strip():
            raise ValueError("本地 .group tojson 不接受目标群参数，只导出当前群配置。")
        group = self._group_snapshot(group_id)
        if not isinstance(group, dict):
            raise ValueError("本群配置结构异常，拒绝导出。")
        flags = {}
        for option, (field, disabled_value, default_value) in GROUP_OPTION_FLAGS.items():
            value = group.get(field, default_value)
            if type(value) is not bool:
                raise ValueError("本群配置开关状态损坏，拒绝导出。")
            flags[option] = value == disabled_value
        rule = group.get("rule")
        if rule is not None and (type(rule) is not int or rule not in COC_RULE_INFO):
            raise ValueError("本群房规配置状态损坏，拒绝导出。")
        hidden_blind = group.get("hidden_blind", False)
        if type(hidden_blind) is not bool:
            raise ValueError("本群盲骰配置状态损坏，拒绝导出。")
        payload = {
            "schema": "dice-astrbot.group-config.v1",
            "scope": "current-group",
            "flags": flags,
            "rule": rule,
            "hidden_blind": hidden_blind,
        }
        try:
            exported = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
            exported.encode("utf-8")
        except (TypeError, ValueError, RecursionError, UnicodeEncodeError) as exc:
            raise ValueError("本群配置无法安全序列化，拒绝导出。") from exc
        if len(exported) > min(4000, self.max_response_length):
            raise ValueError("群配置 JSON 超过安全回复长度上限；未截断导出。")
        return exported

    def _welcome(self, args: str, group_id: str, is_admin: bool, is_private: bool) -> str:
        if is_private:
            return "欢迎语配置仅支持当前群；自动欢迎与离群提示仅支持 OneBot V11（aiocqhttp）。"
        if not group_id:
            return "无法确定当前群，未读取或修改欢迎语。"
        if not is_admin:
            return "仅群管理员可查看或修改本群欢迎语。"
        text = args.strip()
        if not text:
            return "用法：.welcome show | .welcome set <欢迎语> | .welcome clr；也可直接用 .welcome <欢迎语> 设置。自动发送仅支持 OneBot V11（aiocqhttp）。"
        current = self._group_snapshot(group_id).get("welcome_message", "")
        if text.casefold() == "show":
            if not isinstance(current, str):
                return "本群欢迎语配置格式无效，未显示。"
            return current if current else "本群尚未设置欢迎语。"
        if text.casefold() == "clr":
            if not isinstance(current, str):
                return "本群欢迎语配置格式无效，未修改。"
            if not current:
                return "本群尚未设置欢迎语。"
            self._mutate_group(group_id, lambda group: group.__setitem__("welcome_message", ""))
            return "本群欢迎语已清除；OneBot V11（aiocqhttp）入群时不再发送。"
        action, separator, remainder = text.partition(" ")
        if action.casefold() == "set":
            welcome = remainder.strip() if separator else ""
            if not welcome:
                return "用法：.welcome set <欢迎语>（不能为空）"
        else:
            welcome = text
        if len(welcome) > MAX_WELCOME_MESSAGE_LENGTH:
            return f"欢迎语最多 {MAX_WELCOME_MESSAGE_LENGTH} 个字符。"
        self._mutate_group(group_id, lambda group: group.__setitem__("welcome_message", welcome))
        return f"本群欢迎语已保存（{len(welcome)}/{MAX_WELCOME_MESSAGE_LENGTH}）；仅 OneBot V11（aiocqhttp）入群时自动发送。"

    def _group_command(self, args: str, group_id: str, is_admin: bool, is_private: bool = False, platform_scope: str = "", sender_id: str = "", raw_group_id: str = "", group_info: object | None = None) -> str:
        raw_args = args.strip()
        parts = raw_args.split(maxsplit=1)
        subcommand = parts[0].casefold() if parts else ""
        if subcommand == "info":
            return self._group_info_command(parts[1] if len(parts) > 1 else "", raw_group_id, is_private, group_info)
        if subcommand == "tojson":
            return self._group_tojson_command(parts[1] if len(parts) > 1 else "", group_id, is_admin, is_private)
        if subcommand in {"survey", "diver", "diver4kick", "pause", "restart", "ban", "kick", "title"}:
            raise ValueError(
                f"上游 .group {subcommand} 依赖平台成员目录、群管理 API 或 Dice trust；"
                "AstrBot 通用插件接口无法安全等价实现，本插件明确拒绝，不会操作平台。"
            )
        if subcommand == "card":
            return self._group_card_command(raw_args, group_id, is_admin, is_private, platform_scope, sender_id)
        if subcommand == "all":
            specification = parts[1] if len(parts) > 1 else ""
            return self._group_all_command(specification, is_admin, is_private)
        if subcommand == "state":
            return self._group_state_command(group_id, is_private)
        if raw_args.startswith(("+", "-")):
            return self._group_option_command(raw_args, group_id, is_admin, is_private)
        if subcommand == "blind":
            if is_private:
                raise ValueError("隐藏检定盲骰设置仅限群聊")
            setting = parts[1].strip().casefold() if len(parts) > 1 else "status"
            if setting not in {"status", "on", "off"}:
                raise ValueError("用法：.group blind status|on|off")
            if setting != "status" and not is_admin:
                raise ValueError("只有群管理员可以修改本群盲骰模式")
            if setting == "status":
                enabled = self._group_snapshot(group_id).get("hidden_blind") is True
                return f"本群隐藏检定盲骰模式{'已开启' if enabled else '已关闭'}。"
            enabled = setting == "on"
            def update_blind(group: dict) -> bool:
                group["hidden_blind"] = enabled
                return enabled
            self._mutate_group(group_id, update_blind)
            return f"本群隐藏检定盲骰模式已{'开启' if enabled else '关闭'}。"
        action = raw_args.casefold() or "status"
        group = self._group_snapshot(group_id)
        if action == "status":
            rule = self._effective_coc_rule(group_id)
            blind = "开启" if group.get("hidden_blind") is True else "关闭"
            return f"本会话检定房规 {rule}（{COC_RULE_INFO[rule][0]}）；插件{'开启' if group.get('enabled', True) else '关闭'}；隐藏检定盲骰{blind}；先攻条目 {len(self._initiative_items(group))}；牌堆 {len(group.get('decks', {}))}。"
        if action in {"on", "off"}:
            return self._bot(action, group_id, is_admin)
        raise ValueError("用法：.group status|state|on|off|tojson|card <数字用户 ID> <角色卡名>|+选项|-选项|blind status|on|off")

    @staticmethod
    def _now() -> str:
        return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")

    @staticmethod
    def _valid_help_doc_key(key: object) -> bool:
        return isinstance(key, str) and 1 <= len(key) <= 64 and not any(char.isspace() or ord(char) < 32 or 0x7F <= ord(char) <= 0x9F for char in key)

    @staticmethod
    def _valid_help_doc_text(text: object) -> bool:
        return isinstance(text, str) and 1 <= len(text) <= 2000 and all(char in "\n\t" or ord(char) >= 32 and not 0x7F <= ord(char) <= 0x9F for char in text)

    @classmethod
    def _valid_help_docs(cls, docs: object) -> bool:
        if not isinstance(docs, dict) or len(docs) > 64:
            return False
        for scope, entries in docs.items():
            if not isinstance(scope, str) or len(scope) > 512 or not isinstance(entries, dict) or len(entries) > 100:
                return False
            try:
                identity = json.loads(scope)
            except (TypeError, json.JSONDecodeError):
                return False
            if not isinstance(identity, list) or len(identity) != 3 or not all(isinstance(item, str) and item.strip() and len(item) <= 128 for item in identity):
                return False
            if scope != json.dumps(identity, ensure_ascii=False, separators=(",", ":")):
                return False
            total = 0
            for key, text in entries.items():
                if not cls._valid_help_doc_key(key) or not cls._valid_help_doc_text(text):
                    return False
                total += len(text)
                if total > 32_000:
                    return False
        return True

    def _helpdoc_command(self, args: str, is_admin: bool, is_private: bool, platform_scope: str) -> str:
        if not is_private or not is_admin:
            return "自定义帮助页仅允许 AstrBot 全局管理员在私聊中管理。"
        raw = args.strip()
        if not raw or len(raw) > 4096:
            return "用法：.helpdoc <键> [纯文本正文|reset]；最多 64 字符键、每平台/bot 100 页、每页 2000 字。"
        parts = raw.split(maxsplit=1)
        key = parts[0]
        if not self._valid_help_doc_key(key):
            return "帮助页键必须为 1–64 个不含空白/控制字符的字符。"
        if not isinstance(platform_scope, str) or len(platform_scope) > 512:
            return "无法确认当前平台/bot 身份，帮助页操作已拒绝。"
        try:
            identity = json.loads(platform_scope)
        except json.JSONDecodeError:
            identity = None
        if not isinstance(identity, list) or len(identity) != 3 or not all(isinstance(item, str) and item.strip() and len(item) <= 128 for item in identity) or platform_scope != json.dumps(identity, ensure_ascii=False, separators=(",", ":")):
            return "无法确认当前平台/bot 身份，帮助页操作已拒绝。"
        snapshot = self.store.snapshot()
        docs = snapshot.get("help_docs")
        if not self._valid_help_docs(docs):
            return "帮助页存储损坏；为避免覆盖数据，操作已拒绝。"
        entries = docs.get(platform_scope, {})
        if len(parts) == 1 or not parts[1].strip():
            value = entries.get(key)
            return value if isinstance(value, str) else f"未找到帮助页：{key}。"
        content = parts[1].strip()
        if content.casefold() == "reset":
            if key not in entries:
                return f"未找到帮助页：{key}。"
            def remove_page(state: dict) -> bool:
                current = state.get("help_docs")
                if not self._valid_help_docs(current):
                    raise ValueError("帮助页存储损坏；为避免覆盖数据，操作已拒绝。")
                current_entries = current.get(platform_scope)
                if not isinstance(current_entries, dict) or key not in current_entries:
                    return False
                del current_entries[key]
                if not current_entries:
                    current.pop(platform_scope, None)
                return True
            try:
                removed = self.store.transact(remove_page)
            except ValueError as exc:
                return str(exc)
            return f"已重置帮助页：{key}。" if removed else f"未找到帮助页：{key}。"
        if not self._valid_help_doc_text(content):
            return "帮助页正文需为 1–2000 字纯文本；除换行/制表符外不接受控制字符。"
        def save_page(state: dict) -> bool:
            current = state.get("help_docs")
            if not self._valid_help_docs(current):
                raise ValueError("帮助页存储损坏；为避免覆盖数据，操作已拒绝。")
            current_entries = current.get(platform_scope)
            if current_entries is None:
                if len(current) >= 64:
                    raise ValueError("平台/bot 帮助页范围已达 64 个上限。")
                current_entries = {}
                current[platform_scope] = current_entries
            if key not in current_entries and len(current_entries) >= 100:
                raise ValueError("当前平台/bot 帮助页已达 100 页上限。")
            total = sum(len(value) for value in current_entries.values()) - len(current_entries.get(key, "")) + len(content)
            if total > 32_000:
                raise ValueError("当前平台/bot 帮助页正文总量不得超过 32000 字。")
            current_entries[key] = content
            return True
        try:
            self.store.transact(save_page)
        except ValueError as exc:
            return str(exc)
        return f"已保存自定义帮助页：{key}。"

    def _help_command(self, args: str, group_id: str, is_admin: bool, is_private: bool) -> str:
        action = args.strip().casefold()
        if action in {"on", "off"}:
            if is_private:
                return "帮助开关仅限群聊；私聊可直接查看帮助。"
            if not is_admin:
                return "只有群管理员可以开关本群帮助。"
            enabled = action == "on"
            def update(group: dict) -> bool:
                current = group.get("help_enabled", True)
                if type(current) is not bool:
                    raise ValueError("本群帮助开关状态损坏；为避免覆盖数据，操作已拒绝。")
                changed = current != enabled
                group["help_enabled"] = enabled
                return changed
            changed = self._mutate_group(group_id, update)
            state = "开启" if enabled else "关闭"
            return f"本群帮助已{state}。" if changed else f"本群帮助已经{state}。"
        enabled = self._group_snapshot(group_id).get("help_enabled", True)
        if enabled is True:
            return self._help()
        if enabled is False:
            return "本群帮助已关闭；群管理员可用 .help on 恢复。"
        return "本群帮助开关状态异常，已按关闭处理。"

    def _user_command(self, args: str, sender_id: str, group_id: str, is_private: bool = False) -> str:
        parts = args.strip().split()
        if parts and parts[0].casefold() == "tojson":
            if len(parts) != 1 or not is_private:
                raise ValueError("本地 .user tojson 仅允许私聊导出本人数据，不接受目标用户 ID。")
            user = self.store.player(sender_id)
            if not isinstance(user, dict):
                raise ValueError("本插件玩家记录格式无效，导出已拒绝。")
            cards = user.get("cards", {})
            nicknames = user.get("nicknames", {})
            if not isinstance(cards, dict) or len(cards) > 33 or not isinstance(nicknames, dict) or len(nicknames) > 10000:
                raise ValueError("本插件玩家记录结构异常，导出已拒绝。")
            try:
                exported = json.dumps(user, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                exported.encode("utf-8")
            except (TypeError, ValueError, RecursionError) as exc:
                raise ValueError("本插件玩家记录无法安全序列化，导出已拒绝。") from exc
            if len(exported) > min(4000, self.max_response_length):
                raise ValueError("本人数据 JSON 超过 4000 字符安全上限；未截断导出。")
            return exported
        if len(parts) != 1 or parts[0].casefold() != "state":
            raise ValueError("本插件仅支持只读 .user state 或私聊本人 .user tojson；Dice trust/跨用户 tojson/diss/kill/clr 未移植。")
        user = self.store.player(sender_id)
        if not isinstance(user, dict):
            raise ValueError("本插件玩家记录格式无效，未修改数据。")
        cards = user.get("cards", {})
        nicknames = user.get("nicknames", {})
        if not isinstance(cards, dict) or len(cards) > 1024 or any(
            not isinstance(name, str) or not isinstance(card, dict) for name, card in cards.items()
        ):
            raise ValueError("本插件角色卡记录格式无效，未修改数据。")
        if not isinstance(nicknames, dict) or len(nicknames) > 10000 or any(
            not isinstance(scope, str) or not isinstance(nickname, str) for scope, nickname in nicknames.items()
        ):
            raise ValueError("本插件昵称记录格式无效，未修改数据。")
        active = self._active_card_name(user, group_id)
        if not isinstance(active, str) or active not in cards:
            active = None
        active_label = (
            "".join(char for char in active if not (ord(char) < 32 or 0x7F <= ord(char) <= 0x9F))[:80]
            if active else "未选择"
        )
        nickname_count = sum(1 for value in nicknames.values() if value.strip())
        current_nickname = "已设置" if nicknames.get(group_id, "").strip() else "未设置"
        return (
            "本插件玩家状态（仅本人，按当前平台/机器人身份隔离）\n"
            f"角色卡：{len(cards)} 张\n"
            f"当前可用活动卡：{active_label}\n"
            f"昵称记录：{nickname_count} 个；本会话：{current_nickname}\n"
            f"个人默认骰：D{self._user_default_die(sender_id)}\n"
            "Dice 信任级别与账户创建时间：AstrBot 无对应字段，未映射。"
        )

    def _help(self) -> str:
        prefix = self.prefixes[0]
        return (
            f"Dice-AstrBot 原生命令（以 {prefix} 为例，其他配置前缀也可用）：\n"
            f"{prefix}r [次数#]表达式 [理由]（次数 1–10，可用骰式计算） / {prefix}rs 短式 / {prefix}rh/{prefix}rsh/{prefix}h/{prefix}hs 隐藏掷骰 — 3d6+2、2d6k1、B2/P2、4dF、d100+1d4\n"
            f"{prefix}ra [次数#][B/P骰]技能/数值、{prefix}rc [次数#][B/P骰]技能/数值、{prefix}sc 成功损失/失败损失、{prefix}en 技能 [当前值] [失败变化/成功变化]、{prefix}w/.ww [次数#]骰池表达式 [理由]、{prefix}wh/.wwh（群内隐藏） — COC 检定/成功池（次数 1–10；B/P骰数 1–9）；隐藏式 {prefix}rah/{prefix}rch、{prefix}ra_/{prefix}rc_、{prefix}raB_/{prefix}raP_/{prefix}rcB_/{prefix}rcP_；具名卡 角色名:技能/角色名的技能\n"
            f"{prefix}coc/.coc7/.cocs/.coc7s [组数 1-10]（COC7）；{prefix}coc6/.coc6s [组数 1-10]、{prefix}coc7d/.coc6d [角色名]（上游格式属性报告；并保存/激活本地卡，超限时输出完整摘要）、{prefix}pc create/list/use 名称/tag [名称]/show/set/nn 新名称/delete/clr、{prefix}st 属性 数值|&表达式:骰式|del 属性|clr|show [卡名:属性]（COC7卡内置武器公式/派生属性；掷骰别名可用于 {prefix}r）\n"
            f"{prefix}set [默认骰面数 1-9999]、{prefix}setcoc 0-7|show|clr、{prefix}rule set 规则名|{prefix}rule set（重置）|{prefix}rule show|{prefix}rule s 关键词（不代理）、{prefix}game new/start open/list status/state master/set/call/join/leave/kick/rou/close/stop/over、{prefix}ob join/on/off/list/clr/exit、{prefix}ri [修正/骰式] [次数#名称]、{prefix}init add/remove/clear/show\n"
            f"{prefix}ak new/add/del/get/show/clr（支持 #/+/-/= 快捷符号）、{prefix}deck list/create/new/set/show/reset/del/clr（活动团务牌堆随存档保存；固定整数/安全骰式权重（建牌时求值；展开≤1024张/随机抽取≤100000次）；set 仅静态来源；模板/群成员来源不支持）、{prefix}draw [h|_]牌堆名 [次数1-10]（团务→本群→公共；隐藏仅尝试私信；公共不消耗）、{prefix}name [类型] [次数1-10]、{prefix}nnn [类型]（随机设置本会话昵称）、{prefix}user state（仅本人本地状态；Dice trust/注册时间不可用）/{prefix}user tojson（仅私聊本人导出本地 JSON，≤4000字符，不接受目标 ID）、{prefix}jrrp [on/off]（群管理员）、{prefix}me 动作|on/off/status、{prefix}li/.ti（长期/临时疯狂）、{prefix}legacy scope/preview/import [rdconf|rdconf.bak|bak]（角色卡，不带格式默认 rdconf）/users-preview dat|dat.bak|bak|rdconf、users-import 同源格式（昵称/默认骰/私聊房规/个人规则名/本地 gender 文本白名单（≤128字；不查询平台资料））/groups-preview dat|dat.bak|bak|rdconf、groups-import 同源格式（ChatConf迁移群开关、Rh盲骰、房规与入群欢迎纯文本≤500字；私聊身份范围绑定，不执行模板/通知）、{prefix}dnd [组数 1-10]、{prefix}d20 [+修正] [DC]、{prefix}nn 昵称|del|clr、{prefix}log new [名称]/on/off/end/status/show [条数]|show archive [1-6]|archives/recent（团务日志本地有界保存）、{prefix}help [on/off]（仅群管理员开关本群帮助显示）、{prefix}helpdoc 键 [纯文本正文|reset]（平台全局管理员私聊；按 platform/bot 隔离）\n"
            f"{prefix}system state/on/off/clock [list|+on HH:MM|-on HH:MM|+off HH:MM|-off HH:MM]（AstrBot 全局管理员私聊；本地状态/宿主时区每日任务；clock 成功增删向订阅者发送 level 2 通知）\n"
            f"{prefix}admin on/off（私聊全局管理员开关；与 .system state 同步）\n"
            f"{prefix}admin notice list/[this] [0-63|off|+N -N]（群管理员仅查看/操作当前群；私聊全局管理员可列本插件所有已存范围，最多100条且显示前24条；级别 0–63；订阅变更/插件 .system on/off 产生通知；UMO不含self_id，多bot无法指定账号；上限100会话/50收件人）\n"
            f"{prefix}send notice0+2 <单行文本>（私聊平台全局管理员；等级 0–5；≤500字且受响应上限约束；仅发送给同平台/bot匹配订阅者；适配器可能影响送达；任意目标发送未移植）\n"
            f"{prefix}admin censor +Warning:词1,词2 / -词1,词2（全局管理员私聊；Ignore/Notice/Caution/Warning/Danger/Critical；每次 1–8 词，每 bot 256 条；仅过滤带前缀命令（admin/system/bot 控制命令不拦截））\n"
            f"{prefix}re <正则模式> <选项1|选项2>（无选项删除 Regex 规则）、{prefix}reply set/re/regex/list/show/get/add/prefix/search/deck/limit/del/delete/on/off（群管理员；set 支持独立 Title、Match/Prefix/Search 的 | 多别名及多模式，同 Title 共享限制并可按标题/别名管理；re <正则> <文本> 设置，省略文本删除；Regex 仅安全子集；limit 支持 prob(0/100 不筛选)/user_id/grp_id，cd/today 默认本会话，也支持 @user=数/@global=数与 &@echo=提示（≤400 字纯文本；拦截后继续匹配，最多3条不同提示）；cd≤86400、today≤10000，日界 Asia/Shanghai；冷却仅进程内、日计数持久化；用户范围按平台+bot隔离、全局范围限本插件存储；本进程同步锁支持 lock（全局默认标题键）或 lock:键[@chat|@user|@global]&...，最多4把且覆盖本插件当前回复；Text 回复仅对 {{uid}}/{{gid}}/{{nick}}/{{pc}}/{{fromUser}}/{{fromQQ}}/{{fromGroup}}/{{fromMsg}}/{{gender}} 作一次有界替换（nick 优先本会话昵称，其次 AstrBot 发送者名，最后用户 ID；pc 输出当前会话激活卡名（默认“角色卡”或无卡时回退到 nick，仅输出名称）；私聊 gid=0；{{fromUser}}/{{fromGroup}} 是当前原始用户/群 ID，{{fromQQ}} 是 fromUser 的上游兼容别名，私聊 fromGroup 为空；{{fromMsg}} 是当前原始消息，最多500字；{{gender}} 仅读取本地导入的字符串（≤128字），不查询平台资料）；仅 Prefix 模式额外提供 {{suffix}}（匹配前缀后的原消息，最多500字；其他模式不提供）；未知占位符及带冒号的格式指令按原文保留，不执行脚本；Regex Text 支持 $& 全匹配、$$ 字面美元与 $1–$99 编号捕获组（未参与组为空；越界组号保留原文）；全串匹配下前/后缀替换为空；Deck 占位符/复杂变量/非安全 Regex 语法/脚本均拒绝；安全 Regex 子集限 pattern≤128、输入≤400、NFA≤256 状态与 100000 步、每群≤20 条）、{prefix}welcome show|set <文本>|clr|<文本>（群管理员，仅存储当前群纯文本；不自动触发入群通知）,{prefix}group info（仅显示当前事件提供的群名/ID/成员数，不查远端或其他群）|card <用户ID> <角色卡名>（普通成员可切换本人，群管理员可切换他人；只查同平台/bot 已有卡片）|status/state/on/off/+选项|-选项（+/-需群管理员，仅六项本地配置作用于当前群；选项连续符号或空格分隔）|tojson（群管理员仅导出当前群的本地配置 JSON 子集；不含群/成员 ID、会话、角色卡、自定义回复、牌堆或日志，不接受目标群，非上游完整 Chat JSON）|survey/diver/diver4kick/pause/restart/ban/kick/title 未移植并明确拒绝）、{prefix}group blind status/on/off（on/off 需群管理员）、{prefix}group all +选项|-选项（停用指令|禁用回复|禁用jrrp|禁用draw|禁用me|禁用help；全局管理员私聊，仅本地已记录群）、{prefix}link with/to/from 群号|state/list/start/close（群管理员；同平台实例文本转发；UMO不带self_id，单实例多bot无法选账号）、{prefix}bot on/off/status [当前 bot 数字 ID（全号或末四位）]（仅群聊，开关需群管理员；私聊拒绝）、{prefix}ban add/remove 用户ID（群管理员）。数据保存在本地；上游 .master 认主/口令初始化涉及主控与信任等级，本插件显式拒绝且不记录或转发；上游 .warning（JSON 审核任务）、.send 任意目标（仍拒绝；受限 notice 子集见上方）、.cloud update/black（云服务/授权）、.mod/.str/.authorize/.dismiss/.groups/.4kick/.chan 等依赖 BlackListManager hook、C++ 扩展、Dice trust、许可 hook、云端/驱动管理或成员加入事件的命令均明确拒绝且不转发。"
        )
