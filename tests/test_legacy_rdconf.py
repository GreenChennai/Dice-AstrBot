import json
import struct
import tempfile
import unittest
from pathlib import Path

from dice_astrbot.legacy_rdconf import (
    LegacyRDConfError,
    build_chat_config_import_updates,
    build_user_nickname_import_updates,
    build_user_settings_import_updates,
    count_chat_config_import_conflicts,
    count_user_nickname_import_conflicts,
    count_user_settings_import_conflicts,
    decode_chat_conf_dat,
    decode_user_conf_dat,
    decode_user_conf_dat_importable,
    decode_user_conf_rdconf,
    decode_user_conf_rdconf_importable,
    import_chat_config_into_store,
    import_user_nicknames_into_store,
    import_user_settings_into_store,
)
from dice_astrbot.storage import JsonStore


def short_string(value: str, encoding: str = "utf-8") -> bytes:
    raw = value.encode(encoding)
    return struct.pack("<h", len(raw)) + raw


def nickname_map(entries: list[tuple[int, str]], encoding: str = "utf-8") -> bytes:
    return struct.pack("<h", len(entries)) + b"".join(
        struct.pack("<q", group_id) + short_string(name, encoding)
        for group_id, name in entries
    )


def attribute_table(entries: list[tuple[str, bytes]], encoding: str = "utf-8") -> bytes:
    return struct.pack("<h", len(entries)) + b"".join(
        short_string(key, encoding) + value for key, value in entries
    )


def chat_record(group_id: int, tags: list[tuple[int, bytes]], embedded_id: int | None = None, end: bool = True) -> bytes:
    result = struct.pack("<q", group_id if embedded_id is None else embedded_id)
    result += b"".join(struct.pack("<h", tag) + payload for tag, payload in tags)
    return result + (struct.pack("<h", -1) if end else b"")


def chat_conf_file(records: list[tuple[int, bytes]]) -> bytes:
    return struct.pack("<i", len(records)) + b"".join(
        struct.pack("<q", outer_id) + record for outer_id, record in records
    )


def user_dat_record(user_id: int, tags: list[tuple[str, bytes]]) -> bytes:
    return struct.pack("<q", user_id) + b"".join(
        short_string(tag) + payload for tag, payload in tags
    )


def user_dat_file(records: list[bytes]) -> bytes:
    return struct.pack("<i", len(records)) + b"".join(records)


class LegacyUserConfTests(unittest.TestCase):
    def test_current_dat_extracts_only_nicknames_and_decodes_legacy_gbk_tag(self):
        cfg = struct.pack("<h", 1) + short_string("trust") + b"\x02" + struct.pack("<i", 9)
        conf = struct.pack("<h", 1) + short_string("私密键", "gbk") + b"\x04" + short_string("私密值", "gbk")
        record = user_dat_record(123, [
            ("Cfg", cfg),
            ("Conf", conf),
            ("NN", nickname_map([(456, "旧名")])),
            ("Nick", nickname_map([(0, "全局名"), (456, "新名")], "gbk")),
            ("ID", struct.pack("<q", 999)),
            ("END", b""),
        ])
        self.assertEqual(decode_user_conf_dat(user_dat_file([record])), {"123": {"456": "新名", "0": "全局名"}})

    def test_legacy_rdconf_discards_config_and_returns_nickname_map(self):
        int_config = struct.pack("<h", 1) + short_string("trust") + struct.pack("<i", 7)
        string_config = struct.pack("<h", 1) + short_string("密钥", "gbk") + short_string("秘密配置", "gbk")
        nicknames = nickname_map([(0, "全局名"), (456, "群昵称")])
        data = (struct.pack("<i", 1) + struct.pack("<q", 123) + struct.pack("<q", 999)
                + int_config + string_config + nicknames)
        self.assertEqual(decode_user_conf_rdconf(data), {"123": {"0": "全局名", "456": "群昵称"}})

    def test_current_dat_importable_decodes_only_safe_user_preferences(self):
        cfg = attribute_table([
            ("默认骰", b"\x02" + struct.pack("<i", 80)),
            ("rc房规", b"\x02" + struct.pack("<i", 6)),
            ("默认规则", b"\x04" + short_string("COC7")),
            ("gender", b"\x04" + short_string("女性", "gbk")),
            ("trust", b"\x02" + struct.pack("<i", 9)),
        ])
        record = user_dat_record(123, [
            ("Cfg", cfg),
            ("NN", nickname_map([(456, "旧名")])),
            ("END", b""),
        ])
        self.assertEqual(decode_user_conf_dat_importable(user_dat_file([record])), {
            "123": {
                "nicknames": {"456": "旧名"},
                "preferences": {"default_die": 80, "coc_rule": 6, "default_rule_name": "COC7", "gender": "女性"},
            }
        })

    def test_legacy_rdconf_importable_decodes_allowlisted_preferences(self):
        integer_config = (struct.pack("<h", 2)
                          + short_string("默认骰", "gbk") + struct.pack("<i", 100)
                          + short_string("rc房规", "gbk") + struct.pack("<i", 4))
        string_config = (struct.pack("<h", 2)
                         + short_string("默认规则", "gbk") + short_string("COC七版", "gbk")
                         + short_string("gender", "gbk") + short_string("女性", "gbk"))
        data = (struct.pack("<i", 1) + struct.pack("<q", 123) + struct.pack("<q", 999)
                + integer_config + string_config + nickname_map([(456, "群昵称")]))
        self.assertEqual(decode_user_conf_rdconf_importable(data), {
            "123": {
                "nicknames": {"456": "群昵称"},
                "preferences": {"default_die": 100, "coc_rule": 4, "default_rule_name": "COC七版", "gender": "女性"},
            }
        })

    def test_user_preference_decoder_rejects_out_of_range_values(self):
        invalid = attribute_table([
            ("默认骰", b"\x02" + struct.pack("<i", 10_000)),
        ])
        record = user_dat_record(123, [("Cfg", invalid), ("END", b"")])
        with self.assertRaises(LegacyRDConfError):
            decode_user_conf_dat_importable(user_dat_file([record]))

        for gender_value in (
            b"\x02" + struct.pack("<i", 1),
            b"\x04" + short_string("x" * 129),
            b"\x04" + short_string("bad\x7fvalue", "gbk"),
        ):
            bad_gender = attribute_table([("gender", gender_value)])
            bad_record = user_dat_record(123, [("Cfg", bad_gender), ("END", b"")])
            with self.assertRaises(LegacyRDConfError):
                decode_user_conf_dat_importable(user_dat_file([bad_record]))

        for bad_gender in (1, "x" * 129, "bad\u0085value"):
            with self.assertRaises(LegacyRDConfError):
                build_user_settings_import_updates(
                    {"123": {"preferences": {"gender": bad_gender}}},
                    json.dumps(["qq", "adapter", "bot"], separators=(",", ":")),
                )

        integer_config = struct.pack("<h", 1) + short_string("gender", "gbk") + struct.pack("<i", 1)
        empty_strings = struct.pack("<h", 0)
        old_data = (struct.pack("<i", 1) + struct.pack("<q", 123) + struct.pack("<q", 999)
                    + integer_config + empty_strings + nickname_map([]))
        with self.assertRaises(LegacyRDConfError):
            decode_user_conf_rdconf_importable(old_data)

    def test_rejects_unknown_missing_duplicate_and_trailing_dat_records(self):
        with self.assertRaises(LegacyRDConfError):
            decode_user_conf_dat(user_dat_file([user_dat_record(1, [("Unknown", b"") , ("END", b"")])]))
        with self.assertRaises(LegacyRDConfError):
            decode_user_conf_dat(user_dat_file([user_dat_record(1, [("ID", struct.pack("<q", 1))])]))
        duplicate_map = nickname_map([(5, "甲"), (5, "乙")])
        with self.assertRaises(LegacyRDConfError):
            decode_user_conf_dat(user_dat_file([user_dat_record(1, [("NN", duplicate_map), ("END", b"")])]))
        record = user_dat_record(1, [("END", b"")])
        with self.assertRaises(LegacyRDConfError):
            decode_user_conf_dat(user_dat_file([record, record]))
        with self.assertRaises(LegacyRDConfError):
            decode_user_conf_dat(user_dat_file([record]) + b"x")

    def test_scopes_group_nicknames_and_keeps_global_key(self):
        scope = json.dumps(["qq", "adapter-1", "bot-1"], separators=(",", ":"))
        updates, summary = build_user_nickname_import_updates({"123": {"0": "全局", "456": "群名"}}, scope)
        self.assertEqual(updates, {f"{scope}:user:123": {"nicknames": {"0": "全局", f"{scope}:456": "群名"}}})
        self.assertEqual(summary, {"users": 1, "nicknames": 2, "global_nicknames": 1, "group_nicknames": 1})

    def test_import_preserves_other_player_data_and_conflict_is_atomic(self):
        scope = json.dumps(["qq", "adapter-1", "bot-1"], separators=(",", ":"))
        user_id = f"{scope}:user:123"
        with tempfile.TemporaryDirectory() as temp:
            store = JsonStore(Path(temp) / "state.json")
            store.transact(lambda state: state["players"].__setitem__(user_id, {
                "cards": {"Existing": {"name": "Existing"}},
                "active_card": "Existing",
                "nicknames": {"0": "全局", "old-group": "保留"},
            }))
            data = {"123": {"0": "全局", "456": "新群名"}}
            updates, _ = build_user_nickname_import_updates(data, scope)
            self.assertEqual(count_user_nickname_import_conflicts(store.snapshot()["players"], updates), 0)
            result = import_user_nicknames_into_store(store, data, scope)
            player = store.player(user_id)
            self.assertEqual(result["nicknames"], 2)
            self.assertIn("Existing", player["cards"])
            self.assertEqual(player["active_card"], "Existing")
            self.assertEqual(player["nicknames"], {"0": "全局", "old-group": "保留", f"{scope}:456": "新群名"})

            conflict_data = {"123": {"0": "不同全局名", "789": "不会写入"}}
            before = store.snapshot()
            with self.assertRaises(LegacyRDConfError):
                import_user_nicknames_into_store(store, conflict_data, scope)
            self.assertEqual(store.snapshot(), before)


    def test_user_settings_are_scoped_merged_and_conflicts_are_atomic(self):
        scope = json.dumps(["qq", "adapter-1", "bot-1"], separators=(",", ":"))
        legacy_users = {
            "123": {
                "nicknames": {"0": "全局", "456": "群名"},
                "preferences": {"default_die": 80, "coc_rule": 6, "default_rule_name": "COC7", "gender": "女性"},
            }
        }
        player_updates, private_group_updates, summary = build_user_settings_import_updates(legacy_users, scope)
        player_id = f"{scope}:user:123"
        private_group_id = f"{scope}:private:123"
        self.assertEqual(player_updates, {player_id: {
            "nicknames": {"0": "全局", f"{scope}:456": "群名"},
            "default_die": 80,
            "default_rule_name": "COC7",
            "gender": "女性",
        }})
        self.assertEqual(private_group_updates, {private_group_id: {"rule": 6}})
        self.assertEqual(summary, {
            "users": 1, "nicknames": 2, "global_nicknames": 1, "group_nicknames": 1,
            "default_dice": 1, "private_coc_rules": 1, "default_rules": 1, "genders": 1,
        })

        with tempfile.TemporaryDirectory() as temp:
            store = JsonStore(Path(temp) / "state.json")
            store.transact(lambda state: state["players"].__setitem__(player_id, {
                "cards": {"Existing": {"name": "Existing"}},
                "active_card": "Existing",
                "nicknames": {"0": "全局", "old-group": "保留"},
            }))
            self.assertEqual(count_user_settings_import_conflicts(
                store.snapshot()["players"], store.snapshot()["groups"], player_updates, private_group_updates
            ), 0)
            imported = import_user_settings_into_store(store, legacy_users, scope)
            self.assertEqual(imported, summary)
            state = store.snapshot()
            player = state["players"][player_id]
            self.assertEqual(player["default_die"], 80)
            self.assertEqual(player["default_rule_name"], "COC7")
            self.assertEqual(player["gender"], "女性")
            self.assertEqual(player["nicknames"], {"0": "全局", "old-group": "保留", f"{scope}:456": "群名"})
            self.assertIn("Existing", player["cards"])
            self.assertEqual(state["groups"][private_group_id]["rule"], 6)

            conflicting = {
                "123": {
                    "nicknames": {"0": "全局", "456": "群名"},
                    "preferences": {"default_die": 90, "coc_rule": 5, "default_rule_name": "Different", "gender": "男性"},
                }
            }
            conflicting_players, conflicting_groups, _ = build_user_settings_import_updates(conflicting, scope)
            self.assertEqual(count_user_settings_import_conflicts(
                state["players"], state["groups"], conflicting_players, conflicting_groups
            ), 4)
            before = store.snapshot()
            with self.assertRaises(LegacyRDConfError):
                import_user_settings_into_store(store, conflicting, scope)
            self.assertEqual(store.snapshot(), before)


class LegacyChatConfTests(unittest.TestCase):
    def test_current_chatconf_extracts_only_group_allowlist_and_discards_channel_config(self):
        group_table = attribute_table([
            ("禁用回复", b"\x01\x01"),
            ("禁用jrrp", b"\x02" + struct.pack("<i", 1)),
            ("rc房规", b"\x02" + struct.pack("<i", 6)),
            ("Rh盲骰", b"\x01\x01"),
            ("入群欢迎", b"\x14" + short_string("欢迎 UTF-8", "utf-8")),
            ("SENSITIVE_PRIVATE", b"\x04" + short_string("do not import")),
        ])
        channel_table = attribute_table([
            ("禁用me", b"\x01\x01"),
            ("SENSITIVE_CHANNEL", b"\x04" + short_string("do not import")),
        ])
        record = chat_record(456, [
            (4, short_string("群名", "utf-8")),
            (11, group_table),
            (21, struct.pack("<h", 1) + struct.pack("<q", 123) + channel_table),
        ])
        self.assertEqual(decode_chat_conf_dat(chat_conf_file([(456, record)])), {
            "456": {"禁用回复": True, "禁用jrrp": 1, "rc房规": 6, "Rh盲骰": True, "入群欢迎": "欢迎 UTF-8"}
        })

    def test_legacy_chatconf_tags_accept_exact_gbk_keys_and_discard_other_fields(self):
        old_set = struct.pack("<h", 2) + short_string("停用指令", "gbk") + short_string("Rh盲骰", "gbk")
        old_int_map = struct.pack("<h", 1) + short_string("禁用help", "gbk") + struct.pack("<i", 1)
        old_string_map = struct.pack("<h", 1) + short_string("PRIVATE", "gbk") + short_string("SECRET", "gbk")
        old_table = attribute_table([("禁用me", b"\x01\x01"), ("入群欢迎", b"\x04" + short_string("旧欢迎", "gbk"))], "gbk")
        channel_configs = struct.pack("<h", 1) + struct.pack("<q", 88) + attribute_table([("禁用draw", b"\x01\x01")], "gbk")
        record = chat_record(789, [
            (0, short_string("旧群名", "gbk")),
            (1, old_set),
            (2, old_int_map),
            (3, old_string_map),
            (10, old_table),
            (20, channel_configs),
        ])
        self.assertEqual(decode_chat_conf_dat(chat_conf_file([(789, record)])), {
            "789": {"停用指令": True, "Rh盲骰": True, "禁用help": 1, "禁用me": True, "入群欢迎": "旧欢迎"}
        })

    def test_chatconf_welcome_is_only_decoded_from_anystable_tables(self):
        string_map = struct.pack("<h", 1) + short_string("入群欢迎") + short_string("not an AnysTable value")
        record = chat_record(456, [(3, string_map)])
        self.assertEqual(decode_chat_conf_dat(chat_conf_file([(456, record)])), {"456": {}})

    def test_chatconf_rejects_malformed_ids_tags_terminators_and_trailing_data(self):
        good = chat_record(456, [(11, attribute_table([("rc房规", b"\x02" + struct.pack("<i", 3))]))])
        with self.assertRaises(LegacyRDConfError):
            decode_chat_conf_dat(struct.pack("<i", -1))
        with self.assertRaises(LegacyRDConfError):
            decode_chat_conf_dat(chat_conf_file([(456, chat_record(456, [], embedded_id=457))]))
        with self.assertRaises(LegacyRDConfError):
            decode_chat_conf_dat(chat_conf_file([(456, chat_record(456, [(77, b"")]))]))
        with self.assertRaises(LegacyRDConfError):
            decode_chat_conf_dat(chat_conf_file([(456, chat_record(456, [], end=False))]))
        with self.assertRaises(LegacyRDConfError):
            decode_chat_conf_dat(chat_conf_file([(456, good)]) + b"x")
        with self.assertRaises(LegacyRDConfError):
            decode_chat_conf_dat(chat_conf_file([(456, good), (456, good)]))

    def test_chatconf_maps_only_scoped_allowlisted_fields_and_conflicts_atomically(self):
        scope = json.dumps(["qq", "adapter-1", "bot-1"], separators=(",", ":"))
        source = {"456": {"禁用回复": True, "禁用jrrp": 1, "rc房规": 6, "Rh盲骰": True, "入群欢迎": "欢迎 UTF-8"}}
        updates, summary = build_chat_config_import_updates(source, scope)
        group_id = f"{scope}:456"
        self.assertEqual(summary, {"groups": 1, "settings": 5, "source_groups": 1})
        self.assertEqual(updates, {group_id: {"custom_replies_enabled": False, "jrrp_disabled": True, "rule": 6, "hidden_blind": True, "welcome_message": "欢迎 UTF-8"}})
        self.assertEqual(count_chat_config_import_conflicts({group_id: {"rule": 6}}, updates), 0)
        with tempfile.TemporaryDirectory() as temp:
            store = JsonStore(Path(temp) / "state.json")
            store.transact(lambda state: state["groups"].__setitem__(group_id, {"rule": 6, "name": "keep", "session": {"id": "keep"}}))
            imported = import_chat_config_into_store(store, source, scope)
            self.assertEqual(imported, summary)
            group = store.snapshot()["groups"][group_id]
            self.assertEqual(group["custom_replies_enabled"], False)
            self.assertEqual(group["jrrp_disabled"], True)
            self.assertEqual(group["rule"], 6)
            self.assertIs(group["hidden_blind"], True)
            self.assertEqual(group["welcome_message"], "欢迎 UTF-8")
            self.assertEqual(group["name"], "keep")
            self.assertEqual(group["session"], {"id": "keep"})

            conflicting = {"456": {"rc房规": 2, "Rh盲骰": False, "入群欢迎": "不同欢迎"}, "789": {"禁用help": True}}
            before = store.snapshot()
            conflict_updates, _ = build_chat_config_import_updates(conflicting, scope)
            self.assertEqual(count_chat_config_import_conflicts(before["groups"], conflict_updates), 3)
            with self.assertRaises(LegacyRDConfError):
                import_chat_config_into_store(store, conflicting, scope)
            self.assertEqual(store.snapshot(), before)

    def test_chatconf_builder_rejects_invalid_values_and_unscoped_records(self):
        scope = json.dumps(["qq", "adapter-1", "bot-1"], separators=(",", ":"))
        for source in (
            {"0": {"rc房规": 3}},
            {"9223372036854775808": {"rc房规": 3}},
            {"456": {"rc房规": 8}},
            {"456": {"禁用回复": object()}},
            {"456": {"入群欢迎": 123}},
            {"456": {"入群欢迎": "x" * 501}},
            {"456": {"secret": True}},
        ):
            with self.subTest(source=source), self.assertRaises(LegacyRDConfError):
                build_chat_config_import_updates(source, scope)


    def test_chatconf_welcome_accepts_exact_limit_and_empty_text(self):
        scope = json.dumps(["qq", "adapter-1", "bot-1"], separators=(",", ":"))
        at_limit, _ = build_chat_config_import_updates({"456": {"入群欢迎": "x" * 500}}, scope)
        self.assertEqual(len(at_limit[f"{scope}:456"]["welcome_message"]), 500)
        empty, _ = build_chat_config_import_updates({"456": {"入群欢迎": ""}}, scope)
        self.assertEqual(empty[f"{scope}:456"]["welcome_message"], "")

    def test_chatconf_option_import_matches_upstream_attrvar_truthiness(self):
        scope = json.dumps(["qq", "adapter-1", "bot-1"], separators=(",", ":"))
        source = {"456": {
            "停用指令": None,
            "Rh盲骰": None,
            "禁用回复": False,
            "禁用jrrp": 0,
            "禁用draw": 0.0,
            "禁用me": "",
            "禁用help": b"",
        }}
        updates, _ = build_chat_config_import_updates(source, scope)
        self.assertEqual(updates, {
            f"{scope}:456": {
                "enabled": True,
                "custom_replies_enabled": True,
                "jrrp_disabled": True,
                "draw_enabled": False,
                "me_disabled": True,
                "help_enabled": False,
                "hidden_blind": False,
            }
        })

    def test_chatconf_binary_zero_string_and_function_values_keep_upstream_truthiness(self):
        scope = json.dumps(["qq", "adapter-1", "bot-1"], separators=(",", ":"))
        old_integer_map = struct.pack("<h", 1) + short_string("禁用jrrp") + struct.pack("<i", 0)
        old_string_map = struct.pack("<h", 1) + short_string("禁用help") + short_string("")
        current_table = attribute_table([
            ("禁用回复", b"\x02" + struct.pack("<i", 0)),
            ("禁用draw", b"\x06" + struct.pack("<Q", 4) + b"code"),
            ("禁用me", b"\x01\x00"),
            ("Rh盲骰", b"\x02" + struct.pack("<i", 0)),
        ])
        record = chat_record(456, [(2, old_integer_map), (3, old_string_map), (11, current_table)])
        decoded = decode_chat_conf_dat(chat_conf_file([(456, record)]))
        self.assertEqual(decoded, {
            "456": {"禁用jrrp": 0, "禁用help": b"", "禁用回复": 0, "禁用draw": b"code", "禁用me": False, "Rh盲骰": 0}
        })
        updates, _ = build_chat_config_import_updates(decoded, scope)
        self.assertEqual(updates, {
            f"{scope}:456": {
                "jrrp_disabled": True,
                "help_enabled": False,
                "custom_replies_enabled": False,
                "draw_enabled": False,
                "me_disabled": False,
                "hidden_blind": True,
            }
        })


if __name__ == "__main__":
    unittest.main()
