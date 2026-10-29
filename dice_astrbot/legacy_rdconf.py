"""Bounded decoders for the pinned Dice legacy binary formats.

The source archives are never modified. Explicit helpers can atomically import
selected safe fields into a caller-provided plugin store. The formats have no
magic/version header, so only the pinned little-endian x64 layouts are accepted.
"""
from __future__ import annotations

import json
import math
import re
import struct

SOURCE_REVISION = "5e2a8324a631765f694b2e3a71f5d50f7ae6e94f"
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_RECORDS = 100_000
MAX_TABLE_ITEMS = 32_767
MAX_LEGACY_CARDS = 33
MAX_STRING_BYTES = 32_767
MAX_FUNCTION_BYTES = 8 * 1024 * 1024
MAX_NESTING = 16
MAX_USER_TAGS = 128
MAX_CHAT_TAGS = 128
MAX_WELCOME_MESSAGE_LENGTH = 500

GROUP_OPTION_FLAGS = {
    "停用指令": ("enabled", False, True),
    "禁用回复": ("custom_replies_enabled", False, True),
    "禁用jrrp": ("jrrp_disabled", True, False),
    "禁用draw": ("draw_enabled", False, True),
    "禁用me": ("me_disabled", True, False),
    "禁用help": ("help_enabled", False, True),
}
_LEGACY_CHAT_KEYS = frozenset((*GROUP_OPTION_FLAGS, "rc房规", "Rh盲骰"))
_LEGACY_CHAT_ATTRIBUTE_KEYS = frozenset((*_LEGACY_CHAT_KEYS, "入群欢迎"))
_USER_CONFIG_KEYS = frozenset(("默认骰", "rc房规", "默认规则", "gender"))


class LegacyRDConfError(ValueError):
    """The file is corrupt or does not match the supported pinned layout."""


class _Reader:
    def __init__(self, data: bytes) -> None:
        if not isinstance(data, bytes):
            raise LegacyRDConfError("archive input must be bytes")
        if len(data) > MAX_ARCHIVE_BYTES:
            raise LegacyRDConfError("archive exceeds the 64 MiB safety limit")
        self.data = memoryview(data)
        self.offset = 0

    def read(self, size: int) -> bytes:
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise LegacyRDConfError("invalid binary field length")
        end = self.offset + size
        if end > len(self.data):
            raise LegacyRDConfError(f"truncated archive at byte {self.offset}")
        result = self.data[self.offset:end].tobytes()
        self.offset = end
        return result

    def peek_u8(self) -> int | None:
        if self.offset >= len(self.data):
            return None
        return self.data[self.offset]

    def unpack(self, fmt: str):
        return struct.unpack("<" + fmt, self.read(struct.calcsize("<" + fmt)))[0]

    def u8(self) -> int:
        return self.unpack("B")

    def i16(self) -> int:
        return self.unpack("h")

    def u16(self) -> int:
        return self.unpack("H")

    def i32(self) -> int:
        return self.unpack("i")

    def i64(self) -> int:
        return self.unpack("q")

    def u64(self) -> int:
        return self.unpack("Q")

    def f64(self) -> float:
        value = self.unpack("d")
        if not math.isfinite(value):
            raise LegacyRDConfError("non-finite floating-point value")
        return value

    def string_bytes(self) -> bytes:
        length = self.i16()
        if length < 0 or length > MAX_STRING_BYTES:
            raise LegacyRDConfError(f"invalid string length {length} at byte {self.offset - 2}")
        return self.read(length)

    def string(self, encoding: str = "utf-8") -> str:
        raw = self.string_bytes()
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError as exc:
            raise LegacyRDConfError(f"invalid {encoding} string at byte {self.offset - len(raw)}") from exc

    def count16(self, field: str) -> int:
        count = self.i16()
        if count < 0 or count > MAX_TABLE_ITEMS:
            raise LegacyRDConfError(f"invalid {field} count {count}")
        return count

    def string_map(self) -> None:
        count = self.count16("string map")
        for _ in range(count):
            self.string_bytes()
            self.string_bytes()

    def table(self, key_encoding: str = "utf-8", depth: int = 0) -> dict:
        if depth > MAX_NESTING:
            raise LegacyRDConfError("attribute table nesting limit exceeded")
        count = self.count16("attribute table")
        # Match AnysTable::readb/readgb padding behavior in the pinned source.
        if count and self.peek_u8() == 0:
            self.read(2)
        result = {}
        for _ in range(count):
            key = self.string(key_encoding)
            result[key] = self.attr(depth + 1)
        return result

    def set_item(self):
        tag = self.u8()
        if tag == 2:
            return self.i32()
        if tag == 3:
            return self.f64()
        if tag == 4:
            return self.string("gbk")
        if tag == 7:
            return self.i64()
        if tag == 20:
            return self.string("utf-8")
        raise LegacyRDConfError(f"unsupported attribute-set tag {tag}")

    def attr(self, depth: int = 0):
        if depth > MAX_NESTING:
            raise LegacyRDConfError("attribute value nesting limit exceeded")
        tag = self.u8()
        if tag == 0:
            return None
        if tag == 1:
            value = self.u8()
            if value not in (0, 1):
                raise LegacyRDConfError(f"invalid boolean value {value}")
            return bool(value)
        if tag == 2:
            return self.i32()
        if tag == 3:
            return self.f64()
        if tag == 4:
            return self.string("gbk")
        if tag == 5:
            return self.table("gbk", depth + 1)
        if tag == 6:
            length = self.u64()
            if length > MAX_FUNCTION_BYTES:
                raise LegacyRDConfError("function payload exceeds the 8 MiB safety limit")
            return self.read(length)
        if tag == 7:
            return self.i64()
        if tag == 8:
            count = self.u64()
            if count > MAX_TABLE_ITEMS:
                raise LegacyRDConfError(f"invalid attribute set count {count}")
            return [self.set_item() for _ in range(count)]
        if tag == 20:
            return self.string("utf-8")
        if tag == 21:
            return self.table("utf-8", depth + 1)
        raise LegacyRDConfError(f"unknown attribute value tag {tag}")


def _read_user_nickname_map(reader: _Reader, encoding: str) -> dict[str, str]:
    count = reader.count16("user nickname map")
    nicknames = {}
    for _ in range(count):
        group_id = str(reader.i64())
        if group_id in nicknames:
            raise LegacyRDConfError(f"duplicate nickname group id {group_id}")
        nicknames[group_id] = reader.string(encoding)
    return nicknames


def _legacy_user_config_key(raw: bytes) -> str | None:
    for key in _USER_CONFIG_KEYS:
        if raw == key.encode("utf-8") or raw == key.encode("gbk"):
            return key
    return None


def _decode_legacy_user_config_text(raw: bytes) -> str:
    for encoding in ("utf-8", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise LegacyRDConfError("legacy user preference contains invalid text encoding")


def _user_config_preferences(config: dict) -> dict[str, object]:
    if not isinstance(config, dict):
        raise LegacyRDConfError("legacy user configuration is invalid")
    preferences = {}
    if config.get("默认骰") is not None:
        value = config["默认骰"]
        if type(value) is not int or not 1 <= value <= 9_999:
            raise LegacyRDConfError("legacy default die must be an integer from 1 to 9999")
        preferences["default_die"] = value
    if config.get("rc房规") is not None:
        value = config["rc房规"]
        if type(value) is not int or not 0 <= value <= 7:
            raise LegacyRDConfError("legacy private COC rule must be an integer from 0 to 7")
        preferences["coc_rule"] = value
    if config.get("默认规则") is not None:
        value = config["默认规则"]
        if not isinstance(value, str) or len(value) > 128 or any(ord(char) < 32 for char in value):
            raise LegacyRDConfError("legacy default rule name is invalid or exceeds 128 characters")
        value = value.strip()
        if value:
            preferences["default_rule_name"] = value
    if config.get("gender") is not None:
        value = config["gender"]
        if not isinstance(value, str) or len(value) > 128 or any(ord(char) < 32 or 127 <= ord(char) < 160 for char in value):
            raise LegacyRDConfError("legacy gender must be text without controls and at most 128 characters")
        preferences["gender"] = value
    return preferences


def _read_legacy_user_integer_config(reader: _Reader) -> dict[str, int]:
    fields = {}
    for _ in range(reader.count16("legacy integer config")):
        key = _legacy_user_config_key(reader.string_bytes())
        value = reader.i32()
        if key is None:
            continue
        if key == "默认规则":
            raise LegacyRDConfError("legacy default rule name has an invalid integer type")
        if key == "gender":
            raise LegacyRDConfError("legacy gender has an invalid integer type")
        if key in fields:
            raise LegacyRDConfError(f"duplicate legacy user config key {key}")
        fields[key] = value
    return fields


def _read_legacy_user_string_config(reader: _Reader) -> dict[str, str]:
    fields = {}
    for _ in range(reader.count16("legacy string config")):
        key = _legacy_user_config_key(reader.string_bytes())
        value = reader.string_bytes()
        if key is None:
            continue
        if key not in {"默认规则", "gender"}:
            raise LegacyRDConfError(f"legacy user config key {key} has an invalid string type")
        if key in fields:
            raise LegacyRDConfError(f"duplicate legacy user config key {key}")
        fields[key] = _decode_legacy_user_config_text(value)
    return fields


def decode_user_conf_rdconf_importable(data: bytes) -> dict[str, dict[str, object]]:
    """Decode nicknames and a small allowlist from the pinned legacy layout.

    Trust/configuration fields outside the allowlist and embedded IDs are
    consumed but never returned.
    """
    reader = _Reader(data)
    user_count = reader.i32()
    if user_count < 0 or user_count > MAX_RECORDS:
        raise LegacyRDConfError(f"invalid legacy user count {user_count}")
    users = {}
    for _ in range(user_count):
        user_id = str(reader.i64())
        if user_id in users:
            raise LegacyRDConfError(f"duplicate legacy user id {user_id}")
        reader.i64()  # old_readb's embedded ID is not imported
        config = _read_legacy_user_integer_config(reader)
        config.update(_read_legacy_user_string_config(reader))
        users[user_id] = {
            "nicknames": _read_user_nickname_map(reader, "utf-8"),
            "preferences": _user_config_preferences(config),
        }
    if reader.offset != len(reader.data):
        raise LegacyRDConfError(f"unexpected trailing bytes at offset {reader.offset}")
    return users


def decode_user_conf_rdconf(data: bytes) -> dict[str, dict[str, str]]:
    """Backward-compatible nickname-only view of UserConf.RDconf."""
    return {user_id: record["nicknames"] for user_id, record in decode_user_conf_rdconf_importable(data).items()}


def decode_user_conf_dat_importable(data: bytes) -> dict[str, dict[str, object]]:
    """Decode nicknames and safe user preferences from pinned UserConf.dat.

    Other configuration, trust, timestamps and IDs are parsed only for framing.
    NN is UTF-8; the legacy Nick tag stores GBK strings.
    """
    reader = _Reader(data)
    user_count = reader.i32()
    if user_count < 0 or user_count > MAX_RECORDS:
        raise LegacyRDConfError(f"invalid user count {user_count}")
    users = {}
    for _ in range(user_count):
        user_id = str(reader.i64())
        if user_id in users:
            raise LegacyRDConfError(f"duplicate user id {user_id}")
        nicknames = {}
        preferences = {}
        ended = False
        for _ in range(MAX_USER_TAGS):
            tag = reader.string("utf-8")
            if tag == "END":
                ended = True
                break
            if tag == "Cfg":
                preferences.update(_user_config_preferences(reader.table("utf-8")))
            elif tag == "Conf":
                preferences.update(_user_config_preferences(reader.table("gbk")))
            elif tag == "NN":
                nicknames.update(_read_user_nickname_map(reader, "utf-8"))
            elif tag == "Nick":
                nicknames.update(_read_user_nickname_map(reader, "gbk"))
            elif tag == "ID":
                reader.i64()
            else:
                raise LegacyRDConfError(f"unknown user record tag {tag!r}")
        if not ended:
            raise LegacyRDConfError("user record is missing its END tag")
        users[user_id] = {"nicknames": nicknames, "preferences": preferences}
    if reader.offset != len(reader.data):
        raise LegacyRDConfError(f"unexpected trailing bytes at offset {reader.offset}")
    return users


def decode_user_conf_dat(data: bytes) -> dict[str, dict[str, str]]:
    """Backward-compatible nickname-only view of UserConf.dat."""
    return {user_id: record["nicknames"] for user_id, record in decode_user_conf_dat_importable(data).items()}


def _legacy_chat_key(raw: bytes) -> str | None:
    for key in _LEGACY_CHAT_KEYS:
        if raw == key.encode("utf-8") or raw == key.encode("gbk"):
            return key
    return None


def _read_chat_setting_set(reader: _Reader) -> dict[str, object]:
    fields = {}
    for _ in range(reader.count16("chat setting set")):
        key = _legacy_chat_key(reader.string_bytes())
        if key is not None:
            fields[key] = True
    return fields


def _read_chat_integer_map(reader: _Reader) -> dict[str, object]:
    fields = {}
    for _ in range(reader.count16("chat integer map")):
        key = _legacy_chat_key(reader.string_bytes())
        value = reader.i32()
        if key is not None:
            fields[key] = value
    return fields


def _read_chat_string_map(reader: _Reader) -> dict[str, object]:
    fields = {}
    for _ in range(reader.count16("chat string map")):
        key = _legacy_chat_key(reader.string_bytes())
        value = reader.string_bytes()
        if key is not None:
            fields[key] = value  # preserve bytes for upstream non-null option truth semantics
    return fields


def _read_chat_attribute_table(reader: _Reader, key_encoding: str) -> dict[str, object]:
    table = reader.table(key_encoding)
    return {key: value for key, value in table.items() if key in _LEGACY_CHAT_ATTRIBUTE_KEYS}


def _discard_chat_channel_tables(reader: _Reader, key_encoding: str) -> None:
    for _ in range(reader.count16("channel config map")):
        reader.i64()
        reader.table(key_encoding)


def decode_chat_conf_dat(data: bytes) -> dict[str, dict[str, object]]:
    """Decode allowlisted group settings from the pinned ChatConf.dat layout.

    Names, channel-indexed Chat::ChConf tables, and non-allowlisted fields are consumed but
    never returned. Group welcome is imported only as bounded inert text, never as a template or
    notification hook. The source format has no magic/version header.
    """
    reader = _Reader(data)
    group_count = reader.i32()
    if group_count < 0 or group_count > MAX_RECORDS:
        raise LegacyRDConfError(f"invalid legacy group count {group_count}")
    groups = {}
    for _ in range(group_count):
        group_id = reader.i64()
        if group_id <= 0:
            raise LegacyRDConfError(f"invalid legacy group id {group_id}")
        group_key = str(group_id)
        if group_key in groups:
            raise LegacyRDConfError(f"duplicate legacy group id {group_key}")
        embedded_id = reader.i64()
        if embedded_id != group_id:
            raise LegacyRDConfError(f"legacy group id mismatch: {group_key} vs {embedded_id}")
        fields = {}
        ended = False
        for _ in range(MAX_CHAT_TAGS):
            tag = reader.i16()
            if tag == -1:
                ended = True
                break
            if tag == 0:
                reader.string("gbk")  # legacy group name
            elif tag == 4:
                reader.string("utf-8")
            elif tag == 1:
                fields.update(_read_chat_setting_set(reader))
            elif tag == 2:
                fields.update(_read_chat_integer_map(reader))
            elif tag == 3:
                fields.update(_read_chat_string_map(reader))
            elif tag == 11:
                fields.update(_read_chat_attribute_table(reader, "utf-8"))
            elif tag == 10:
                fields.update(_read_chat_attribute_table(reader, "gbk"))
            elif tag == 21:
                _discard_chat_channel_tables(reader, "utf-8")
            elif tag == 20:
                _discard_chat_channel_tables(reader, "gbk")
            else:
                raise LegacyRDConfError(f"unknown ChatConf record tag {tag}")
        if not ended:
            raise LegacyRDConfError(f"legacy group {group_key} is missing its end tag")
        groups[group_key] = fields
    if reader.offset != len(reader.data):
        raise LegacyRDConfError(f"unexpected trailing bytes at offset {reader.offset}")
    return groups


def _discard_card_map(reader: _Reader) -> None:
    reader.string_map()


def _read_card(reader: _Reader, card_id: int) -> dict | None:
    name = ""
    system = ""
    raw_attributes = {}
    skipped_metadata_sections = 0
    for _ in range(64):
        tag = reader.string("utf-8")
        if tag == "END":
            break
        if tag == "Tag":
            name = reader.string("utf-8")
        elif tag == "Name":
            name = reader.string("gbk")
        elif tag == "Type":
            system = reader.string("utf-8")
        elif tag == "Attr":
            raw_attributes = reader.table("utf-8")
        elif tag == "Attrs":
            raw_attributes = reader.table("gbk")
        elif tag in {"Lock", "DiceExp", "Info"}:
            _discard_card_map(reader)
            skipped_metadata_sections += 1
        elif tag == "Note":
            reader.string_bytes()
            skipped_metadata_sections += 1
        else:
            raise LegacyRDConfError(f"unknown character-card section tag {tag!r}")
    else:
        raise LegacyRDConfError("character card is missing its END tag")

    if not name:
        fallback_name = raw_attributes.get("__Name")
        name = fallback_name if isinstance(fallback_name, str) else ""
    if not name.strip():
        return None
    if not system:
        fallback_system = raw_attributes.get("__Type")
        system = fallback_system if isinstance(fallback_system, str) else "coc7"

    attributes = {}
    skipped = 0
    for key, value in raw_attributes.items():
        if not isinstance(key, str):
            skipped += 1
            continue
        if key in {"__Name", "__Type"}:
            continue
        if key.startswith("__") or key.startswith("&"):
            skipped += 1
            continue
        if isinstance(value, str):
            if not re.fullmatch(r"[+-]?\d+", value.strip()):
                skipped += 1
                continue
            value = int(value.strip())
        elif isinstance(value, float):
            if not value.is_integer():
                skipped += 1
                continue
            value = int(value)
        elif isinstance(value, bool) or value is None or isinstance(value, (dict, list)):
            skipped += 1
            continue
        if isinstance(value, int):
            attributes[key] = value
        else:
            skipped += 1
    return {"id": card_id, "name": name.strip(), "system": system.strip() or "coc7", "attributes": attributes, "skipped_attributes": skipped, "skipped_metadata_sections": skipped_metadata_sections}


def _read_player(reader: _Reader) -> dict:
    reader.i16()  # indexMax
    card_count = reader.count16("character card")
    cards_by_id = {}
    names = set()
    seen_card_ids = set()
    cards = []
    skipped_attributes = 0
    skipped_metadata_sections = 0
    skipped_empty_name_cards = 0
    for _ in range(card_count):
        card_id = reader.u16()
        if card_id in seen_card_ids:
            raise LegacyRDConfError(f"duplicate character card id {card_id}")
        seen_card_ids.add(card_id)
        card = _read_card(reader, card_id)
        if card is None:
            skipped_empty_name_cards += 1
            continue
        key = card["name"].casefold()
        if key in names:
            raise LegacyRDConfError(f"duplicate card name {card['name']!r}")
        names.add(key)
        cards_by_id[card_id] = card
        skipped_attributes += card["skipped_attributes"]
        skipped_metadata_sections += card["skipped_metadata_sections"]
        cards.append(card)

    group_count = reader.count16("group card map")
    active_by_group = {}
    seen_group_ids = set()
    skipped_group_mappings = 0
    if group_count:
        for _ in range(group_count):
            group_id = reader.u64()
            if group_id in seen_group_ids:
                raise LegacyRDConfError(f"duplicate group id {group_id} in character-card map")
            seen_group_ids.add(group_id)
            card_id = reader.u16()
            if card_id in cards_by_id:
                active_by_group[str(group_id)] = cards_by_id[card_id]["name"]
            else:
                skipped_group_mappings += 1
    elif 0 in cards_by_id:
        active_by_group["0"] = cards_by_id[0]["name"]
    return {"cards": cards, "active_by_group": active_by_group, "skipped_attributes": skipped_attributes, "skipped_metadata_sections": skipped_metadata_sections, "skipped_empty_name_cards": skipped_empty_name_cards, "skipped_group_mappings": skipped_group_mappings}


def decode_player_cards_rdconf(data: bytes) -> dict[str, dict]:
    """Decode PlayerCards.RDconf for the pinned x64 little-endian Dice layout.

    No file is modified. A real archive must still be checked before relying on this
    reader; the upstream format has no magic/version header.
    """
    reader = _Reader(data)
    player_count = reader.i32()
    if player_count < 0 or player_count > MAX_RECORDS:
        raise LegacyRDConfError(f"invalid player count {player_count}")
    players = {}
    for _ in range(player_count):
        user_id = str(reader.i64())
        if user_id in players:
            raise LegacyRDConfError(f"duplicate player id {user_id}")
        players[user_id] = _read_player(reader)
    if reader.offset != len(reader.data):
        raise LegacyRDConfError(f"unexpected trailing bytes at offset {reader.offset}")
    return players


def build_player_import_updates(legacy_players: dict, platform_scope: str, imported_at: str) -> tuple[dict[str, dict], dict[str, int]]:
    """Map decoded QQ player records into this bot's scoped, plugin-owned player keys."""
    if not isinstance(platform_scope, str) or len(platform_scope) > 512:
        raise LegacyRDConfError("invalid AstrBot platform/bot scope")
    try:
        identity = json.loads(platform_scope)
    except (TypeError, json.JSONDecodeError) as exc:
        raise LegacyRDConfError("legacy import requires a valid AstrBot platform/bot identity") from exc
    if not isinstance(identity, list) or len(identity) != 3 or any(not isinstance(item, str) or not item.strip() for item in identity):
        raise LegacyRDConfError("legacy import requires platform name, platform id, and bot id")
    if not isinstance(imported_at, str) or not imported_at.strip() or len(imported_at) > 64:
        raise LegacyRDConfError("invalid import timestamp")
    if not isinstance(legacy_players, dict):
        raise LegacyRDConfError("decoded legacy player records must be an object")

    updates = {}
    card_total = 0
    skipped_attributes = 0
    skipped_metadata_sections = 0
    skipped_empty_name_cards = 0
    skipped_group_mappings = 0
    active_group_count = 0
    for legacy_user_id, source in legacy_players.items():
        if not isinstance(legacy_user_id, str) or not re.fullmatch(r"[0-9]{1,20}", legacy_user_id):
            raise LegacyRDConfError("legacy archive contains an invalid player id")
        if not isinstance(source, dict) or not isinstance(source.get("cards"), list):
            raise LegacyRDConfError(f"legacy player {legacy_user_id} has an invalid card list")
        source_cards = source["cards"]
        skipped_empty = source.get("skipped_empty_name_cards", 0)
        if type(skipped_empty) is not int or skipped_empty < 0:
            raise LegacyRDConfError(f"legacy player {legacy_user_id} has an invalid skipped-empty-card count")
        skipped_empty_name_cards += skipped_empty
        skipped_section_count = source.get("skipped_metadata_sections", 0)
        if type(skipped_section_count) is not int or skipped_section_count < 0:
            raise LegacyRDConfError(f"legacy player {legacy_user_id} has an invalid skipped-metadata count")
        skipped_metadata_sections += skipped_section_count
        skipped_mappings = source.get("skipped_group_mappings", 0)
        if type(skipped_mappings) is not int or skipped_mappings < 0:
            raise LegacyRDConfError(f"legacy player {legacy_user_id} has an invalid skipped-group-mapping count")
        skipped_group_mappings += skipped_mappings
        if len(source_cards) > MAX_LEGACY_CARDS:
            raise LegacyRDConfError(f"legacy player {legacy_user_id} has more than {MAX_LEGACY_CARDS} cards; refusing data loss")
        cards = {}
        normalized_names = set()
        for source_card in source_cards:
            if not isinstance(source_card, dict):
                raise LegacyRDConfError(f"legacy player {legacy_user_id} contains an invalid card")
            name = source_card.get("name")
            system = source_card.get("system", "coc7")
            attributes = source_card.get("attributes", {})
            card_id = source_card.get("id")
            if not isinstance(name, str) or not name.strip() or len(name) > 128:
                raise LegacyRDConfError(f"legacy player {legacy_user_id} contains an invalid card name")
            name = name.strip()
            folded = name.casefold()
            if folded in normalized_names:
                raise LegacyRDConfError(f"legacy player {legacy_user_id} has duplicate card names")
            if not isinstance(system, str) or len(system) > 32 or not isinstance(attributes, dict):
                raise LegacyRDConfError(f"legacy card {name!r} has invalid metadata")
            if any(not isinstance(key, str) or len(key) > 128 or type(value) is not int for key, value in attributes.items()):
                raise LegacyRDConfError(f"legacy card {name!r} contains unsupported attribute values")
            if type(card_id) is not int or not 0 <= card_id <= 65_535:
                raise LegacyRDConfError(f"legacy card {name!r} has an invalid card id")
            normalized_names.add(folded)
            cards[name] = {"name": name, "system": system.strip() or "coc7", "attributes": dict(attributes), "created_at": imported_at}
            skipped = source_card.get("skipped_attributes", 0)
            if type(skipped) is not int or skipped < 0:
                raise LegacyRDConfError(f"legacy card {name!r} has an invalid skipped-attribute count")
            skipped_attributes += skipped

        source_active = source.get("active_by_group", {})
        if not isinstance(source_active, dict):
            raise LegacyRDConfError(f"legacy player {legacy_user_id} has an invalid active-card map")
        legacy_default = None
        active_by_group = {}
        for legacy_group_id, card_name in source_active.items():
            if not isinstance(legacy_group_id, str) or not re.fullmatch(r"[0-9]{1,20}", legacy_group_id):
                raise LegacyRDConfError(f"legacy player {legacy_user_id} has an invalid group id")
            if not isinstance(card_name, str) or card_name not in cards:
                raise LegacyRDConfError(f"legacy player {legacy_user_id} maps a group to a missing card")
            if legacy_group_id == "0":
                legacy_default = card_name
            else:
                active_by_group[f"{platform_scope}:{legacy_group_id}"] = card_name
                active_group_count += 1
        active_card = legacy_default or next(iter(cards), None)
        updates[f"{platform_scope}:user:{legacy_user_id}"] = {
            "cards": cards,
            "active_card": active_card,
            "legacy_active_card": legacy_default,
            "active_cards_by_group": active_by_group,
        }
        card_total += len(cards)

    if not updates:
        raise LegacyRDConfError("legacy archive contains no player records")
    summary = {"users": len(updates), "cards": card_total, "active_groups": active_group_count, "skipped_attributes": skipped_attributes, "skipped_metadata_sections": skipped_metadata_sections, "skipped_empty_name_cards": skipped_empty_name_cards, "skipped_group_mappings": skipped_group_mappings}
    return updates, summary


def import_player_cards_into_store(store, legacy_players: dict, platform_scope: str, imported_at: str) -> dict[str, int]:
    """Atomically add legacy records without overwriting any existing scoped player."""
    updates, summary = build_player_import_updates(legacy_players, platform_scope, imported_at)

    def apply(state: dict) -> dict[str, int]:
        players = state.get("players")
        if not isinstance(players, dict):
            raise LegacyRDConfError("plugin player state is invalid; no import was applied")
        if any(key in players for key in updates):
            raise LegacyRDConfError("one or more target player records already exist; refusing to overwrite")
        players.update(updates)
        return summary

    return store.transact(apply)



def build_user_nickname_import_updates(legacy_users: dict, platform_scope: str) -> tuple[dict[str, dict], dict[str, int]]:
    """Map legacy nickname-only records into this platform/bot's player keys."""
    if not isinstance(platform_scope, str) or len(platform_scope) > 512:
        raise LegacyRDConfError("invalid AstrBot platform/bot scope")
    try:
        identity = json.loads(platform_scope)
    except (TypeError, json.JSONDecodeError) as exc:
        raise LegacyRDConfError("legacy import requires a valid AstrBot platform/bot identity") from exc
    if not isinstance(identity, list) or len(identity) != 3 or any(not isinstance(item, str) or not item.strip() for item in identity):
        raise LegacyRDConfError("legacy import requires platform name, platform id, and bot id")
    if not isinstance(legacy_users, dict) or len(legacy_users) > MAX_RECORDS:
        raise LegacyRDConfError("decoded legacy user records must be a bounded object")

    updates = {}
    nickname_count = 0
    global_count = 0
    group_count = 0
    for legacy_user_id, source_nicknames in legacy_users.items():
        if not isinstance(legacy_user_id, str) or not re.fullmatch(r"[0-9]{1,20}", legacy_user_id):
            raise LegacyRDConfError("legacy archive contains an invalid user id")
        if not isinstance(source_nicknames, dict) or len(source_nicknames) > MAX_TABLE_ITEMS:
            raise LegacyRDConfError(f"legacy user {legacy_user_id} has an invalid nickname map")
        nicknames = {}
        for legacy_group_id, nickname in source_nicknames.items():
            if not isinstance(legacy_group_id, str) or not re.fullmatch(r"[0-9]{1,20}", legacy_group_id):
                raise LegacyRDConfError(f"legacy user {legacy_user_id} has an invalid nickname group id")
            if not isinstance(nickname, str) or len(nickname.encode("utf-8")) > MAX_STRING_BYTES:
                raise LegacyRDConfError(f"legacy user {legacy_user_id} has an invalid nickname")
            local_group_id = "0" if legacy_group_id == "0" else f"{platform_scope}:{legacy_group_id}"
            if local_group_id in nicknames:
                raise LegacyRDConfError(f"legacy user {legacy_user_id} has colliding nickname group ids")
            nicknames[local_group_id] = nickname
            nickname_count += 1
            if legacy_group_id == "0":
                global_count += 1
            else:
                group_count += 1
        if nicknames:
            updates[f"{platform_scope}:user:{legacy_user_id}"] = {"nicknames": nicknames}
    if not updates:
        raise LegacyRDConfError("legacy UserConf contains no nickname records")
    summary = {"users": len(updates), "nicknames": nickname_count, "global_nicknames": global_count, "group_nicknames": group_count}
    return updates, summary


def count_user_nickname_import_conflicts(existing_players: dict, updates: dict) -> int:
    """Count different values at the same scoped user/group nickname key."""
    if not isinstance(existing_players, dict) or not isinstance(updates, dict):
        raise LegacyRDConfError("plugin player state is invalid; nickname preview aborted")
    conflicts = 0
    for player_id, patch in updates.items():
        if player_id not in existing_players:
            continue
        player = existing_players[player_id]
        if not isinstance(player, dict):
            raise LegacyRDConfError("local player record is invalid; nickname preview aborted")
        if "nicknames" not in player:
            continue
        current = player["nicknames"]
        if not isinstance(current, dict):
            raise LegacyRDConfError("local nickname map is invalid; nickname preview aborted")
        proposed = patch.get("nicknames") if isinstance(patch, dict) else None
        if not isinstance(proposed, dict):
            raise LegacyRDConfError("legacy nickname update is invalid")
        conflicts += sum(1 for key, value in proposed.items() if key in current and current[key] != value)
    return conflicts


def import_user_nicknames_into_store(store, legacy_users: dict, platform_scope: str) -> dict[str, int]:
    """Merge nickname-only records atomically without overwriting different names."""
    updates, summary = build_user_nickname_import_updates(legacy_users, platform_scope)

    def apply(state: dict) -> dict[str, int]:
        players = state.get("players")
        if not isinstance(players, dict):
            raise LegacyRDConfError("plugin player state is invalid; no nickname import was applied")
        if count_user_nickname_import_conflicts(players, updates):
            raise LegacyRDConfError("one or more nickname targets already have different values; refusing the entire import")
        for player_id, patch in updates.items():
            if player_id not in players:
                player = {"cards": {}, "active_card": None}
            else:
                player = players[player_id]
                if not isinstance(player, dict):
                    raise LegacyRDConfError("local player record is invalid; no nickname import was applied")
            if "nicknames" not in player:
                player["nicknames"] = {}
            elif not isinstance(player["nicknames"], dict):
                raise LegacyRDConfError("local nickname map is invalid; no nickname import was applied")
            player["nicknames"].update(patch["nicknames"])
            players[player_id] = player
        return summary

    return store.transact(apply)


def build_user_settings_import_updates(legacy_users: dict, platform_scope: str) -> tuple[dict[str, dict], dict[str, dict], dict[str, int]]:
    """Map only nicknames and portable user preferences into scoped state."""
    if not isinstance(platform_scope, str) or len(platform_scope) > 512:
        raise LegacyRDConfError("invalid AstrBot platform/bot scope")
    try:
        identity = json.loads(platform_scope)
    except (TypeError, json.JSONDecodeError) as exc:
        raise LegacyRDConfError("legacy import requires a valid AstrBot platform/bot identity") from exc
    if not isinstance(identity, list) or len(identity) != 3 or any(not isinstance(item, str) or not item.strip() for item in identity):
        raise LegacyRDConfError("legacy import requires platform name, platform id, and bot id")
    if not isinstance(legacy_users, dict) or len(legacy_users) > MAX_RECORDS:
        raise LegacyRDConfError("decoded legacy user records must be a bounded object")

    player_updates = {}
    private_group_updates = {}
    affected_users = set()
    nickname_count = global_count = group_count = 0
    default_dice_count = private_coc_count = default_rule_count = gender_count = 0
    for legacy_user_id, source in legacy_users.items():
        if not isinstance(legacy_user_id, str) or not re.fullmatch(r"[0-9]{1,20}", legacy_user_id):
            raise LegacyRDConfError("legacy archive contains an invalid user id")
        if not isinstance(source, dict) or set(source) - {"nicknames", "preferences"}:
            raise LegacyRDConfError(f"legacy user {legacy_user_id} has an invalid import record")
        source_nicknames = source.get("nicknames", {})
        preferences = source.get("preferences", {})
        if not isinstance(source_nicknames, dict) or len(source_nicknames) > MAX_TABLE_ITEMS:
            raise LegacyRDConfError(f"legacy user {legacy_user_id} has an invalid nickname map")
        if not isinstance(preferences, dict) or len(preferences) > 4:
            raise LegacyRDConfError(f"legacy user {legacy_user_id} has invalid preferences")
        nickname_patch = {}
        for legacy_group_id, nickname in source_nicknames.items():
            if not isinstance(legacy_group_id, str) or not re.fullmatch(r"[0-9]{1,20}", legacy_group_id):
                raise LegacyRDConfError(f"legacy user {legacy_user_id} has an invalid nickname group id")
            if not isinstance(nickname, str) or len(nickname.encode("utf-8")) > MAX_STRING_BYTES:
                raise LegacyRDConfError(f"legacy user {legacy_user_id} has an invalid nickname")
            local_group_id = "0" if legacy_group_id == "0" else f"{platform_scope}:{legacy_group_id}"
            if local_group_id in nickname_patch:
                raise LegacyRDConfError(f"legacy user {legacy_user_id} has colliding nickname group ids")
            nickname_patch[local_group_id] = nickname
            nickname_count += 1
            if legacy_group_id == "0":
                global_count += 1
            else:
                group_count += 1

        patch = {}
        if nickname_patch:
            patch["nicknames"] = nickname_patch
        for key, value in preferences.items():
            if key == "default_die":
                if type(value) is not int or not 1 <= value <= 9_999:
                    raise LegacyRDConfError(f"legacy user {legacy_user_id} has an invalid default die")
                patch[key] = value
                default_dice_count += 1
            elif key == "coc_rule":
                if type(value) is not int or not 0 <= value <= 7:
                    raise LegacyRDConfError(f"legacy user {legacy_user_id} has an invalid private COC rule")
                private_group_updates[f"{platform_scope}:private:{legacy_user_id}"] = {"rule": value}
                private_coc_count += 1
            elif key == "default_rule_name":
                if not isinstance(value, str) or len(value) > 128 or any(ord(char) < 32 for char in value):
                    raise LegacyRDConfError(f"legacy user {legacy_user_id} has an invalid default rule name")
                value = value.strip()
                if value:
                    patch[key] = value
                    default_rule_count += 1
            elif key == "gender":
                if not isinstance(value, str) or len(value) > 128 or any(ord(char) < 32 or 127 <= ord(char) < 160 for char in value):
                    raise LegacyRDConfError(f"legacy user {legacy_user_id} has an invalid gender value")
                patch[key] = value
                gender_count += 1
            else:
                raise LegacyRDConfError(f"legacy user {legacy_user_id} contains a non-allowlisted preference")
        if patch:
            player_updates[f"{platform_scope}:user:{legacy_user_id}"] = patch
        if patch or any(key == "coc_rule" for key in preferences):
            affected_users.add(legacy_user_id)

    if not player_updates and not private_group_updates:
        raise LegacyRDConfError("legacy UserConf contains no allowlisted nickname or preference records")
    summary = {
        "users": len(affected_users),
        "nicknames": nickname_count,
        "global_nicknames": global_count,
        "group_nicknames": group_count,
        "default_dice": default_dice_count,
        "private_coc_rules": private_coc_count,
        "default_rules": default_rule_count,
        "genders": gender_count,
    }
    return player_updates, private_group_updates, summary


def count_user_settings_import_conflicts(existing_players: dict, existing_groups: dict, player_updates: dict, private_group_updates: dict) -> int:
    """Count changed local values without overwriting user-owned settings."""
    if not isinstance(existing_players, dict) or not isinstance(existing_groups, dict):
        raise LegacyRDConfError("plugin player/group state is invalid; UserConf preview aborted")
    if not isinstance(player_updates, dict) or not isinstance(private_group_updates, dict):
        raise LegacyRDConfError("legacy UserConf updates are invalid")
    nickname_updates = {key: {"nicknames": patch["nicknames"]} for key, patch in player_updates.items() if isinstance(patch, dict) and "nicknames" in patch}
    conflicts = count_user_nickname_import_conflicts(existing_players, nickname_updates) if nickname_updates else 0
    for player_id, patch in player_updates.items():
        if not isinstance(player_id, str) or not isinstance(patch, dict):
            raise LegacyRDConfError("legacy player preference update is invalid")
        player = existing_players.get(player_id)
        if player is None:
            continue
        if not isinstance(player, dict):
            raise LegacyRDConfError("local player record is invalid; UserConf preview aborted")
        for field in ("default_die", "default_rule_name", "gender"):
            if field not in patch or field not in player or player[field] is None:
                continue
            if type(player[field]) is not type(patch[field]) or player[field] != patch[field]:
                conflicts += 1
    for group_id, patch in private_group_updates.items():
        if not isinstance(group_id, str) or not isinstance(patch, dict) or set(patch) != {"rule"}:
            raise LegacyRDConfError("legacy private group update is invalid")
        group = existing_groups.get(group_id)
        if group is None:
            continue
        if not isinstance(group, dict):
            conflicts += 1
            continue
        current = group.get("rule")
        proposed = patch["rule"]
        if current is None or (type(current) is type(proposed) and current == proposed):
            continue
        conflicts += 1
    return conflicts


def import_user_settings_into_store(store, legacy_users: dict, platform_scope: str) -> dict[str, int]:
    """Atomically import allowlisted UserConf fields into player and private-group state."""
    player_updates, private_group_updates, summary = build_user_settings_import_updates(legacy_users, platform_scope)

    def apply(state: dict) -> dict[str, int]:
        players = state.get("players")
        groups = state.get("groups")
        if not isinstance(players, dict) or not isinstance(groups, dict):
            raise LegacyRDConfError("plugin player/group state is invalid; no UserConf import was applied")
        if count_user_settings_import_conflicts(players, groups, player_updates, private_group_updates):
            raise LegacyRDConfError("one or more UserConf targets already have different customized values; refusing the entire import")
        for player_id, patch in player_updates.items():
            player = players.get(player_id)
            if player is None:
                player = {"cards": {}, "active_card": None}
            if not isinstance(player, dict):
                raise LegacyRDConfError("local player record is invalid; no UserConf import was applied")
            for field, value in patch.items():
                if field == "nicknames":
                    nicknames = player.get("nicknames")
                    if nicknames is None:
                        nicknames = {}
                    if not isinstance(nicknames, dict):
                        raise LegacyRDConfError("local nickname map is invalid; no UserConf import was applied")
                    nicknames.update(value)
                    player["nicknames"] = nicknames
                else:
                    player[field] = value
            players[player_id] = player
        for group_id, patch in private_group_updates.items():
            group = groups.get(group_id)
            if group is None:
                group = {}
            if not isinstance(group, dict):
                raise LegacyRDConfError("local private group record is invalid; no UserConf import was applied")
            group["rule"] = patch["rule"]
            groups[group_id] = group
        return summary

    return store.transact(apply)


def _legacy_chat_attr_active(value: object, legacy_group_id: str, key: str) -> bool:
    if value is None:
        return False
    if type(value) is bool:
        return value
    if type(value) in (int, float, str, bytes, dict, list):
        return True
    raise LegacyRDConfError(f"legacy group {legacy_group_id} option {key} has an unsupported value")


def build_chat_config_import_updates(legacy_groups: dict, platform_scope: str) -> tuple[dict[str, dict], dict[str, int]]:
    """Map allowlisted group options, COC rule overrides, and inert welcome text."""
    if not isinstance(platform_scope, str) or len(platform_scope) > 512:
        raise LegacyRDConfError("invalid AstrBot platform/bot scope")
    try:
        identity = json.loads(platform_scope)
    except (TypeError, json.JSONDecodeError) as exc:
        raise LegacyRDConfError("legacy import requires a valid AstrBot platform/bot identity") from exc
    if not isinstance(identity, list) or len(identity) != 3 or any(not isinstance(item, str) or not item.strip() for item in identity):
        raise LegacyRDConfError("legacy import requires platform name, platform id, and bot id")
    if not isinstance(legacy_groups, dict) or len(legacy_groups) > MAX_RECORDS:
        raise LegacyRDConfError("decoded legacy group records must be a bounded object")

    updates = {}
    settings_count = 0
    for legacy_group_id, source_fields in legacy_groups.items():
        if not isinstance(legacy_group_id, str) or not re.fullmatch(r"[1-9][0-9]{0,18}", legacy_group_id):
            raise LegacyRDConfError("legacy archive contains an invalid group id")
        if int(legacy_group_id) > 9_223_372_036_854_775_807:
            raise LegacyRDConfError("legacy group id exceeds signed 64-bit range")
        if not isinstance(source_fields, dict) or len(source_fields) > MAX_TABLE_ITEMS:
            raise LegacyRDConfError(f"legacy group {legacy_group_id} has invalid settings")
        patch = {}
        for key, value in source_fields.items():
            if key in GROUP_OPTION_FLAGS:
                field, disabled_value, default_value = GROUP_OPTION_FLAGS[key]
                active = _legacy_chat_attr_active(value, legacy_group_id, key)
                patch[field] = disabled_value if active else default_value
            elif key == "Rh盲骰":
                patch["hidden_blind"] = _legacy_chat_attr_active(value, legacy_group_id, key)
            elif key == "rc房规":
                if value is not None and (type(value) is not int or not 0 <= value <= 7):
                    raise LegacyRDConfError(f"legacy group {legacy_group_id} has an invalid COC rule")
                patch["rule"] = value
            elif key == "入群欢迎":
                if type(value) is not str or len(value) > MAX_WELCOME_MESSAGE_LENGTH:
                    raise LegacyRDConfError(f"legacy group {legacy_group_id} has an invalid or oversized welcome message")
                patch["welcome_message"] = value
            else:
                raise LegacyRDConfError(f"legacy group {legacy_group_id} contains a non-allowlisted field")
        if patch:
            updates[f"{platform_scope}:{legacy_group_id}"] = patch
            settings_count += len(patch)
    if not updates:
        raise LegacyRDConfError("ChatConf contains no allowlisted group settings")
    summary = {"groups": len(updates), "settings": settings_count, "source_groups": len(legacy_groups)}
    return updates, summary


def count_chat_config_import_conflicts(existing_groups: dict, updates: dict) -> int:
    """Count differing non-default local group values without overwriting them."""
    if not isinstance(existing_groups, dict) or not isinstance(updates, dict):
        raise LegacyRDConfError("plugin group state is invalid; ChatConf preview aborted")
    defaults = {values[0]: values[2] for values in GROUP_OPTION_FLAGS.values()}
    defaults["rule"] = None
    defaults["hidden_blind"] = False
    defaults["welcome_message"] = ""
    conflicts = 0
    for group_id, patch in updates.items():
        if not isinstance(group_id, str) or not isinstance(patch, dict) or not patch:
            raise LegacyRDConfError("legacy group update is invalid")
        if group_id not in existing_groups:
            continue
        current_group = existing_groups[group_id]
        if not isinstance(current_group, dict):
            conflicts += len(patch)
            continue
        for field, proposed in patch.items():
            if field not in defaults:
                raise LegacyRDConfError("legacy group update contains a non-allowlisted field")
            current = current_group.get(field, defaults[field])
            if type(current) is type(proposed) and current == proposed:
                continue
            if type(current) is type(defaults[field]) and current == defaults[field]:
                continue
            conflicts += 1
    return conflicts


def import_chat_config_into_store(store, legacy_groups: dict, platform_scope: str) -> dict[str, int]:
    """Atomically merge scoped group settings, preserving all other local state."""
    updates, summary = build_chat_config_import_updates(legacy_groups, platform_scope)

    def apply(state: dict) -> dict[str, int]:
        if "groups" not in state:
            state["groups"] = {}
        groups = state.get("groups")
        if not isinstance(groups, dict):
            raise LegacyRDConfError("plugin group state is invalid; no ChatConf import was applied")
        if count_chat_config_import_conflicts(groups, updates):
            raise LegacyRDConfError("one or more group settings conflict with customized local values; refusing the entire import")
        for group_id, patch in updates.items():
            if group_id not in groups:
                group = {}
            else:
                group = groups[group_id]
                if not isinstance(group, dict):
                    raise LegacyRDConfError("local group record is invalid; no ChatConf import was applied")
            group.update(patch)
            groups[group_id] = group
        return summary

    return store.transact(apply)
