import asyncio
import json
import logging
import sys
import struct
import tempfile
import types
import unittest
from pathlib import Path


def install_astrbot_stubs():
    astrbot = types.ModuleType("astrbot")
    astrbot.__path__ = []
    api = types.ModuleType("astrbot.api")
    api.__path__ = []
    message_components_module = types.ModuleType("astrbot.api.message_components")
    event_module = types.ModuleType("astrbot.api.event")
    star_module = types.ModuleType("astrbot.api.star")

    class EventMessageType:
        ALL = object()

    class Filter:
        @staticmethod
        def event_message_type(_event_type):
            return lambda function: function
    Filter.EventMessageType = EventMessageType

    class At:
        def __init__(self, qq):
            self.qq = str(qq)

    class Plain:
        def __init__(self, text):
            self.text = text

    message_components_module.At = At
    message_components_module.Plain = Plain

    class Star:
        def __init__(self, context, config=None):
            self.context = context
            self.logger = logging.getLogger("dice_astrbot.tests")

    class StarTools:
        data_root = Path(tempfile.gettempdir())
        @classmethod
        def get_data_dir(cls, name=None):
            path = cls.data_root / (name or "plugin")
            path.mkdir(parents=True, exist_ok=True)
            return path

    class AstrMessageEvent:
        pass

    event_module.AstrMessageEvent = AstrMessageEvent
    event_module.filter = Filter
    star_module.Context = object
    star_module.Star = Star
    star_module.StarTools = StarTools
    sys.modules.update({
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.message_components": message_components_module,
        "astrbot.api.event": event_module,
        "astrbot.api.star": star_module,
    })
    return StarTools


StarTools = install_astrbot_stubs()
from astrbot.api.message_components import At, Plain
from dice_astrbot.dice_engine import DiceError
from dice_astrbot.legacy_rdconf import LegacyRDConfError
from dice_astrbot.main import Main


class FakeEvent:
    def __init__(self, message, sender="u1", group="g1", private=False, admin=False):
        self.message = message
        self.sender = sender
        self.group = group
        self.private = private
        self.admin = admin
        self.stopped = False

    def get_message_str(self):
        return self.message
    def get_sender_id(self):
        return self.sender
    def get_group_id(self):
        return self.group
    def is_private_chat(self):
        return self.private
    def is_admin(self):
        return self.admin
    def stop_event(self):
        self.stopped = True
    def plain_result(self, text):
        return text

    def chain_result(self, chain):
        return chain


class FixedRoll:
    def __init__(self, expression, total):
        self.expression = expression
        self.total = total

    def format(self):
        return f"{self.expression}={self.total}"


class SequenceEngine:
    def __init__(self, values):
        self.values = list(values)
        self.calls = []

    def roll(self, expression="", default_die=100):
        self.calls.append((expression, default_die))
        total = int(expression) if expression.strip().isdecimal() else self.values.pop(0)
        return FixedRoll(expression, total)


class SequenceRandom:
    def __init__(self, values=()):
        self.values = list(values)
        self.calls = 0

    def randint(self, low, high):
        self.calls += 1
        value = self.values.pop(0) if self.values else low
        if not low <= value <= high:
            raise AssertionError(f"{value} outside {low}..{high}")
        return value




def make_user_conf_dat(user_id=123, group_id=456, group_name="旧群昵称", global_name="全局昵称", default_die=80, coc_rule=6, default_rule_name="COC7", gender="女性"):
    def short(value, encoding="utf-8"):
        raw = value.encode(encoding)
        return struct.pack("<h", len(raw)) + raw

    nicknames = (struct.pack("<h", 2) + struct.pack("<q", group_id) + short(group_name)
                 + struct.pack("<q", 0) + short(global_name))
    config = (struct.pack("<h", 5)
              + short("默认骰") + b"\x02" + struct.pack("<i", default_die)
              + short("rc房规") + b"\x02" + struct.pack("<i", coc_rule)
              + short("默认规则") + b"\x04" + short(default_rule_name)
              + short("gender") + b"\x04" + short(gender, "gbk")
              + short("trust") + b"\x02" + struct.pack("<i", 99))
    record = (struct.pack("<q", user_id) + short("Cfg") + config
              + short("NN") + nicknames + short("END"))
    return struct.pack("<i", 1) + record


def make_user_conf_rdconf(user_id=123, group_id=456):
    def short(value, encoding="utf-8"):
        raw = value.encode(encoding)
        return struct.pack("<h", len(raw)) + raw

    integer_config = (struct.pack("<h", 2)
                      + short("默认骰", "gbk") + struct.pack("<i", 100)
                      + short("rc房规", "gbk") + struct.pack("<i", 4))
    string_config = (struct.pack("<h", 2)
                     + short("默认规则", "gbk") + short("旧规则", "gbk")
                     + short("gender", "gbk") + short("女性", "gbk"))
    nicknames = (struct.pack("<h", 2) + struct.pack("<q", group_id) + short("旧昵称")
                 + struct.pack("<q", 0) + short("全局旧名"))
    return (struct.pack("<i", 1) + struct.pack("<q", user_id) + struct.pack("<q", user_id)
            + integer_config + string_config + nicknames)


def make_player_cards_rdconf(user_id=123, card_name="旧卡"):
    def short(value, encoding="utf-8"):
        raw = value.encode(encoding)
        return struct.pack("<h", len(raw)) + raw

    player = (struct.pack("<hhH", 1, 1, 0)
              + short("Tag") + short(card_name)
              + short("Type") + short("coc7") + short("END") + struct.pack("<h", 0))
    return struct.pack("<i", 1) + struct.pack("<q", user_id) + player


def make_chat_conf_dat(group_id=456, rule=6):
    def short(value):
        raw = value.encode("utf-8")
        return struct.pack("<h", len(raw)) + raw

    group_table = (struct.pack("<h", 5)
                   + short("禁用回复") + b"\x01\x01"
                   + short("rc房规") + b"\x02" + struct.pack("<i", rule)
                   + short("Rh盲骰") + b"\x01\x01"
                   + short("入群欢迎") + b"\x14" + short("各位好")
                   + short("SENSITIVE_PRIVATE") + b"\x04" + short("do not import"))
    channel_table = struct.pack("<h", 1) + short("SENSITIVE_CHANNEL") + b"\x04" + short("do not import")
    record = (struct.pack("<q", group_id)
              + struct.pack("<h", 11) + group_table
              + struct.pack("<h", 21) + struct.pack("<h", 1) + struct.pack("<q", 123) + channel_table
              + struct.pack("<h", -1))
    return struct.pack("<i", 1) + struct.pack("<q", group_id) + record


class PluginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        StarTools.data_root = Path(self.temp.name)
        self.plugin = Main(object())

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def collect(plugin, event):
        async def run():
            return [result async for result in plugin.on_message(event)]
        return asyncio.run(run())

    @staticmethod
    def platform_event(message, sender="500", group="999", private=True, admin=True, platform_id="connector-a", self_id="bot-a"):
        event = FakeEvent(message, sender=sender, group=group, private=private, admin=admin)
        event.get_platform_name = lambda: "onebot"
        event.get_platform_id = lambda: platform_id
        event.get_self_id = lambda: self_id
        return event

    @staticmethod
    def onebot_notice_event(notice, group="999", platform_name="aiocqhttp", platform_id="connector-a", self_id="10001"):
        event = FakeEvent("", sender="20000", group=group, private=False, admin=False)
        event.message_obj = types.SimpleNamespace(raw_message=notice)
        event.get_platform_name = lambda: platform_name
        event.get_platform_id = lambda: platform_id
        event.get_self_id = lambda: self_id
        return event

    def test_prefix_order_dedup_and_single_prefix_override(self):
        plugin = Main(object(), {"prefixes": ["!", "!!", ".", "!!"]})
        self.assertEqual(plugin.prefixes, ("!!", "!", "."))
        self.assertEqual(plugin._command_from("!!version"), ("version", ""))
        single = Main(object(), {"prefix": "?"})
        self.assertEqual(single._command_from("?version"), ("version", ""))
        self.assertIsNone(single._command_from("!version"))
        empty_list_falls_back = Main(object(), {"prefix": "?", "prefixes": []})
        self.assertEqual(empty_list_falls_back.prefixes, ("?",))

    def test_jrrp_reply_uses_plain_luck_sentence(self):
        reply = self.plugin._jrrp("", "999", "500", False)
        prefix = "你今日的人品是 "
        self.assertTrue(reply.startswith(prefix), reply)
        value = reply[len(prefix):]
        self.assertTrue(value.isdigit() and 1 <= int(value) <= 100, reply)
        self.assertEqual(reply, self.plugin._jrrp("", "999", "500", False))

    def test_nickname_matches_upstream_global_and_single_entry_fallbacks(self):
        def set_nicknames(nicknames):
            self.plugin.store.transact(
                lambda state: state["players"].__setitem__("u1", {"nicknames": nicknames})
            )

        set_nicknames({"g1": "当前群", "0": "全局名"})
        self.assertEqual(self.plugin._nickname_for_user("u1", "g1"), "当前群")
        self.assertEqual(self.plugin._nickname_for_user("u1", "g2"), "全局名")

        set_nicknames({"g1": "唯一名"})
        self.assertEqual(self.plugin._nickname_for_user("u1", "g2"), "唯一名")

        set_nicknames({"g1": "甲", "g2": "乙"})
        self.assertIsNone(self.plugin._nickname_for_user("u1", "g3"))

    def test_legacy_user_conf_commands_are_owner_scope_and_preview_bound(self):
        scope = json.dumps(["qq", "adapter-1", "bot-1"], separators=(",", ":"))
        plugin = Main(object(), {
            "legacy_import_owner_id": "123",
            "legacy_import_owner_scope": scope,
        })
        import_dir = plugin.data_dir / "imports"
        import_dir.mkdir(parents=True, exist_ok=True)
        archive_path = import_dir / "UserConf.dat"
        source = make_user_conf_dat()
        archive_path.write_bytes(source)

        outsider = plugin._dispatch("legacy", "users-preview dat", "private:456", "456", False, True, platform_scope=scope)
        self.assertIn("配置的所有者", outsider)
        self.assertIsNone(plugin._legacy_users_preview)
        other_scope = json.dumps(["qq", "adapter-2", "bot-2"], separators=(",", ":"))
        wrong_bot = plugin._dispatch("legacy", "users-preview dat", "private:123", "123", False, True, platform_scope=other_scope)
        self.assertIn("身份与配置所有者范围不匹配", wrong_bot)

        with self.assertRaises(LegacyRDConfError):
            plugin._dispatch("legacy", "users-import dat", "private:123", "123", False, True, platform_scope=scope)
        preview = plugin._dispatch("legacy", "users-preview dat", "private:123", "123", False, True, platform_scope=scope)
        self.assertIn("1 个用户、2 条昵称", preview)
        self.assertIn("个人默认骰 1 项", preview)
        self.assertIn("私聊 COC 房规 1 项", preview)
        self.assertIn("个人规则名 1 项", preview)
        self.assertIn("个人 gender 字符串 1 项", preview)
        self.assertIn("未写入数据", preview)
        self.assertEqual(plugin.store.snapshot()["players"], {})
        self.assertEqual(plugin.store.snapshot()["groups"], {})
        imported = plugin._dispatch("legacy", "users-import dat", "private:123", "123", False, True, platform_scope=scope)
        self.assertIn("已原子迁移 1 个用户：昵称 2 条、个人默认骰 1 项、私聊 COC 房规 1 项、个人规则名 1 项、个人 gender 字符串 1 项", imported)
        self.assertEqual(archive_path.read_bytes(), source)
        state = plugin.store.snapshot()
        player = state["players"][f"{scope}:user:123"]
        self.assertEqual(player["nicknames"], {f"{scope}:456": "旧群昵称", "0": "全局昵称"})
        self.assertEqual(player["default_die"], 80)
        self.assertEqual(player["default_rule_name"], "COC7")
        self.assertEqual(player["gender"], "女性")
        self.assertNotIn("trust", player)
        self.assertEqual(player["cards"], {})
        self.assertIsNone(player["active_card"])
        self.assertEqual(state["groups"][f"{scope}:private:123"]["rule"], 6)
        self.assertEqual(set(state["groups"][f"{scope}:private:123"]), {"rule"})

    def test_legacy_user_conf_import_rejects_changed_source_after_preview(self):
        scope = json.dumps(["qq", "adapter-1", "bot-1"], separators=(",", ":"))
        plugin = Main(object(), {
            "legacy_import_owner_id": "123",
            "legacy_import_owner_scope": scope,
        })
        import_dir = plugin.data_dir / "imports"
        import_dir.mkdir(parents=True, exist_ok=True)
        archive_path = import_dir / "UserConf.dat"
        archive_path.write_bytes(make_user_conf_dat())
        plugin._dispatch("legacy", "users-preview dat", "private:123", "123", False, True, platform_scope=scope)
        archive_path.write_bytes(make_user_conf_dat(group_name="被替换的源文件"))
        before = plugin.store.snapshot()
        with self.assertRaises(LegacyRDConfError):
            plugin._dispatch("legacy", "users-import dat", "private:123", "123", False, True, platform_scope=scope)
        self.assertEqual(plugin.store.snapshot(), before)
        self.assertIsNone(plugin._legacy_users_preview)

    def test_legacy_user_conf_rdconf_imports_scoped_preferences(self):
        scope = json.dumps(["qq", "adapter-1", "bot-1"], separators=(",", ":"))
        plugin = Main(object(), {"legacy_import_owner_id": "123", "legacy_import_owner_scope": scope})
        import_dir = plugin.data_dir / "imports"
        import_dir.mkdir(parents=True, exist_ok=True)
        archive_path = import_dir / "UserConf.RDconf"
        source = make_user_conf_rdconf()
        archive_path.write_bytes(source)

        preview = plugin._dispatch("legacy", "users-preview rdconf", "private:123", "123", False, True, platform_scope=scope)
        self.assertIn("个人默认骰 1 项", preview)
        self.assertIn("私聊 COC 房规 1 项", preview)
        self.assertIn("个人 gender 字符串 1 项", preview)
        imported = plugin._dispatch("legacy", "users-import rdconf", "private:123", "123", False, True, platform_scope=scope)
        self.assertIn("已原子迁移", imported)
        self.assertEqual(archive_path.read_bytes(), source)
        state = plugin.store.snapshot()
        player = state["players"][f"{scope}:user:123"]
        self.assertEqual(player["default_die"], 100)
        self.assertEqual(player["default_rule_name"], "旧规则")
        self.assertEqual(player["gender"], "女性")
        self.assertEqual(player["nicknames"][f"{scope}:456"], "旧昵称")
        self.assertEqual(state["groups"][f"{scope}:private:123"]["rule"], 4)

    def test_legacy_user_conf_sources_include_upstream_fallback_files(self):
        scope = json.dumps(["qq", "adapter-1", "bot-1"], separators=(",", ":"))
        plugin = Main(object(), {"legacy_import_owner_id": "123", "legacy_import_owner_scope": scope})
        import_dir = plugin.data_dir / "imports"
        import_dir.mkdir(parents=True, exist_ok=True)
        sources = {
            "dat": ("UserConf.dat", make_user_conf_dat()),
            "dat.bak": ("UserConf.dat.bak", make_user_conf_dat()),
            "bak": ("UserConf.bak", make_user_conf_dat(user_id=125)),
            "rdconf": ("UserConf.RDconf", make_user_conf_rdconf(user_id=126)),
        }
        for filename, source in sources.values():
            (import_dir / filename).write_bytes(source)

        plugin._dispatch("legacy", "users-preview dat", "private:123", "123", False, True, platform_scope=scope)
        before = plugin.store.snapshot()
        with self.assertRaises(LegacyRDConfError):
            plugin._dispatch("legacy", "users-import dat.bak", "private:123", "123", False, True, platform_scope=scope)
        self.assertEqual(plugin.store.snapshot(), before)
        self.assertIsNone(plugin._legacy_users_preview)

        for source_format, (filename, source) in sources.items():
            with self.subTest(source_format=source_format):
                preview = plugin._dispatch("legacy", f"users-preview {source_format}", "private:123", "123", False, True, platform_scope=scope)
                self.assertIn(filename, preview)
                imported = plugin._dispatch("legacy", f"users-import {source_format}", "private:123", "123", False, True, platform_scope=scope)
                self.assertIn("已原子迁移", imported)
                self.assertEqual((import_dir / filename).read_bytes(), source)
        help_text = plugin._help()
        self.assertIn("users-preview dat|dat.bak|bak|rdconf", help_text)
        self.assertIn("groups-preview dat|dat.bak|bak|rdconf", help_text)
        self.assertIn("入群欢迎纯文本≤500字", help_text)

    def test_legacy_user_conf_preference_conflicts_reject_atomically(self):
        scope = json.dumps(["qq", "adapter-1", "bot-1"], separators=(",", ":"))
        plugin = Main(object(), {"legacy_import_owner_id": "123", "legacy_import_owner_scope": scope})
        import_dir = plugin.data_dir / "imports"
        import_dir.mkdir(parents=True, exist_ok=True)
        (import_dir / "UserConf.dat").write_bytes(make_user_conf_dat())
        plugin.store.transact(lambda state: (
            state["players"].__setitem__(f"{scope}:user:123", {"default_die": 90, "cards": {"Keep": {"name": "Keep"}}}),
            state["groups"].__setitem__(f"{scope}:private:123", {"rule": 3}),
        ))
        before = plugin.store.snapshot()
        preview = plugin._dispatch("legacy", "users-preview dat", "private:123", "123", False, True, platform_scope=scope)
        self.assertIn("冲突 2 项", preview)
        self.assertEqual(plugin.store.snapshot(), before)
        with self.assertRaises(LegacyRDConfError):
            plugin._dispatch("legacy", "users-import dat", "private:123", "123", False, True, platform_scope=scope)
        self.assertEqual(plugin.store.snapshot(), before)
        self.assertIsNone(plugin._legacy_users_preview)

    def test_legacy_chat_conf_commands_are_owner_scope_and_preview_bound(self):
        scope = json.dumps(["qq", "adapter-1", "bot-1"], separators=(",", ":"))
        plugin = Main(object(), {
            "legacy_import_owner_id": "123",
            "legacy_import_owner_scope": scope,
        })
        import_dir = plugin.data_dir / "imports"
        import_dir.mkdir(parents=True, exist_ok=True)
        archive_path = import_dir / "ChatConf.dat"
        source = make_chat_conf_dat()
        archive_path.write_bytes(source)

        outsider = plugin._dispatch("legacy", "groups-preview dat", "private:456", "456", False, True, platform_scope=scope)
        self.assertIn("配置的所有者", outsider)
        self.assertIsNone(plugin._legacy_groups_preview)
        other_scope = json.dumps(["qq", "adapter-2", "bot-2"], separators=(",", ":"))
        wrong_bot = plugin._dispatch("legacy", "groups-preview dat", "private:123", "123", False, True, platform_scope=other_scope)
        self.assertIn("身份与配置所有者范围不匹配", wrong_bot)

        with self.assertRaises(LegacyRDConfError):
            plugin._dispatch("legacy", "groups-import dat", "private:123", "123", False, True, platform_scope=scope)
        preview = plugin._dispatch("legacy", "groups-preview dat", "private:123", "123", False, True, platform_scope=scope)
        self.assertIn("1 个群记录，1 个群有 4 项可迁移设置", preview)
        self.assertIn("入群欢迎纯文本（≤500字，不执行模板或通知）", preview)
        self.assertIn("不迁移群名、频道 ChConf", preview)
        before = plugin.store.snapshot()
        self.assertNotIn(f"{scope}:456", before["groups"])
        imported = plugin._dispatch("legacy", "groups-import dat", "private:123", "123", False, True, platform_scope=scope)
        self.assertIn("已原子迁移 1 个群的 4 项设置", imported)
        self.assertEqual(archive_path.read_bytes(), source)
        group = plugin.store.snapshot()["groups"][f"{scope}:456"]
        self.assertEqual(group, {"custom_replies_enabled": False, "rule": 6, "hidden_blind": True, "welcome_message": "各位好"})
        self.assertNotIn("SENSITIVE_PRIVATE", repr(group))
        self.assertNotIn("SENSITIVE_CHANNEL", repr(group))
        self.assertEqual(plugin._welcome("show", f"{scope}:456", True, False), "各位好")

    def test_legacy_chat_conf_changed_source_and_customized_conflict_are_atomic(self):
        scope = json.dumps(["qq", "adapter-1", "bot-1"], separators=(",", ":"))
        plugin = Main(object(), {
            "legacy_import_owner_id": "123",
            "legacy_import_owner_scope": scope,
        })
        import_dir = plugin.data_dir / "imports"
        import_dir.mkdir(parents=True, exist_ok=True)
        archive_path = import_dir / "ChatConf.dat"
        archive_path.write_bytes(make_chat_conf_dat())
        plugin._dispatch("legacy", "groups-preview dat", "private:123", "123", False, True, platform_scope=scope)
        archive_path.write_bytes(make_chat_conf_dat(rule=5))
        before = plugin.store.snapshot()
        with self.assertRaises(LegacyRDConfError):
            plugin._dispatch("legacy", "groups-import dat", "private:123", "123", False, True, platform_scope=scope)
        self.assertEqual(plugin.store.snapshot(), before)
        self.assertIsNone(plugin._legacy_groups_preview)

        archive_path.write_bytes(make_chat_conf_dat())
        plugin._mutate_group(f"{scope}:456", lambda group: group.__setitem__("rule", 3))
        conflict_preview = plugin._dispatch("legacy", "groups-preview dat", "private:123", "123", False, True, platform_scope=scope)
        self.assertIn("自定义值冲突 1 项", conflict_preview)
        before = plugin.store.snapshot()
        with self.assertRaises(LegacyRDConfError):
            plugin._dispatch("legacy", "groups-import dat", "private:123", "123", False, True, platform_scope=scope)
        self.assertEqual(plugin.store.snapshot(), before)
        self.assertIsNone(plugin._legacy_groups_preview)

    def test_legacy_chat_conf_sources_include_upstream_fallback_files(self):
        scope = json.dumps(["qq", "adapter-1", "bot-1"], separators=(",", ":"))
        plugin = Main(object(), {
            "legacy_import_owner_id": "123",
            "legacy_import_owner_scope": scope,
        })
        import_dir = plugin.data_dir / "imports"
        import_dir.mkdir(parents=True, exist_ok=True)
        sources = {
            "dat": "ChatConf.dat",
            "dat.bak": "ChatConf.dat.bak",
            "bak": "ChatConf.bak",
            "rdconf": "ChatConf.RDconf",
        }
        source = make_chat_conf_dat()
        for filename in sources.values():
            (import_dir / filename).write_bytes(source)

        plugin._dispatch("legacy", "groups-preview dat", "private:123", "123", False, True, platform_scope=scope)
        before = plugin.store.snapshot()
        with self.assertRaises(LegacyRDConfError):
            plugin._dispatch("legacy", "groups-import rdconf", "private:123", "123", False, True, platform_scope=scope)
        self.assertEqual(plugin.store.snapshot(), before)
        self.assertIsNone(plugin._legacy_groups_preview)

        for source_format, filename in sources.items():
            with self.subTest(source_format=source_format):
                preview = plugin._dispatch("legacy", f"groups-preview {source_format}", "private:123", "123", False, True, platform_scope=scope)
                self.assertIn(filename, preview)
                imported = plugin._dispatch("legacy", f"groups-import {source_format}", "private:123", "123", False, True, platform_scope=scope)
                self.assertIn("已原子迁移 1 个群的 4 项设置", imported)
                self.assertEqual((import_dir / filename).read_bytes(), source)

    def test_legacy_player_cards_support_upstream_fallback_files_and_preview_binding(self):
        scope = json.dumps(["qq", "adapter-1", "bot-1"], separators=(",", ":"))
        config = {"legacy_import_owner_id": "123", "legacy_import_owner_scope": scope}
        sources = {
            "rdconf": "PlayerCards.RDconf",
            "rdconf.bak": "PlayerCards.RDconf.bak",
            "bak": "PlayerCards.bak",
        }
        source = make_player_cards_rdconf()
        original_root = StarTools.data_root
        try:
            for source_format, filename in sources.items():
                with self.subTest(source_format=source_format):
                    StarTools.data_root = Path(self.temp.name) / source_format
                    plugin = Main(object(), config)
                    import_dir = plugin.data_dir / "imports"
                    import_dir.mkdir(parents=True, exist_ok=True)
                    for candidate in sources.values():
                        (import_dir / candidate).write_bytes(source)
                    archive_path = import_dir / filename
                    preview_action = "preview" if source_format == "rdconf" else f"preview {source_format}"
                    import_action = "import" if source_format == "rdconf" else f"import {source_format}"
                    preview = plugin._dispatch("legacy", preview_action, "private:123", "123", False, True, platform_scope=scope)
                    self.assertIn(filename, preview)
                    archive_path.write_bytes(make_player_cards_rdconf(card_name="替换后的卡"))
                    before = plugin.store.snapshot()
                    with self.assertRaises(LegacyRDConfError):
                        plugin._dispatch("legacy", import_action, "private:123", "123", False, True, platform_scope=scope)
                    self.assertEqual(plugin.store.snapshot(), before)
                    self.assertIsNone(plugin._legacy_preview)
                    archive_path.write_bytes(source)
                    plugin._dispatch("legacy", preview_action, "private:123", "123", False, True, platform_scope=scope)
                    other_format = next(candidate for candidate in sources if candidate != source_format)
                    wrong_action = "import" if other_format == "rdconf" else f"import {other_format}"
                    with self.assertRaises(LegacyRDConfError):
                        plugin._dispatch("legacy", wrong_action, "private:123", "123", False, True, platform_scope=scope)
                    self.assertEqual(plugin.store.snapshot(), before)
                    self.assertIsNone(plugin._legacy_preview)
                    plugin._dispatch("legacy", preview_action, "private:123", "123", False, True, platform_scope=scope)
                    imported = plugin._dispatch("legacy", import_action, "private:123", "123", False, True, platform_scope=scope)
                    self.assertIn("已从 " + filename + " 原子导入", imported)
                    state = plugin.store.snapshot()
                    self.assertEqual(state["players"][f"{scope}:user:123"]["cards"]["旧卡"]["name"], "旧卡")
                    for candidate in sources.values():
                        self.assertEqual((import_dir / candidate).read_bytes(), source)
        finally:
            StarTools.data_root = original_root
        help_text = self.plugin._help()
        self.assertIn("preview/import [rdconf|rdconf.bak|bak]", help_text)

    def test_disabled_group_can_be_reenabled_case_insensitively(self):
        self.plugin._mutate_group("g1", lambda group: group.__setitem__("enabled", False))
        event = FakeEvent(".BOT ON", admin=True)
        replies = self.collect(self.plugin, event)
        self.assertTrue(event.stopped)
        self.assertEqual(replies, ["本群 Dice 插件已开启。"])
        self.assertTrue(self.plugin._group_snapshot("g1")["enabled"])

    def test_group_tojson_exports_only_current_group_safe_config(self):
        sensitive_values = ("SENSITIVE_SESSION_ID_91", "SENSITIVE_MEMBER_ID_92", "SENSITIVE_CARD_93", "SENSITIVE_REPLY_94")

        def seed(group):
            group.update({
                "enabled": False,
                "custom_replies_enabled": True,
                "jrrp_disabled": True,
                "draw_enabled": False,
                "me_disabled": False,
                "help_enabled": True,
                "rule": 6,
                "hidden_blind": True,
                "session": {"id": sensitive_values[0]},
                "sessions": {"SENSITIVE_SESSION": {"title": sensitive_values[0]}},
                "participants": [sensitive_values[1]],
                "decks": {"private": {"cards": [sensitive_values[2]]}},
                "custom_replies": {"trigger": sensitive_values[3]},
            })

        self.plugin._mutate_group("g1", seed)
        before = self.plugin._group_snapshot("g1")
        output = self.plugin._dispatch("group", "tojson", "g1", "admin", True)
        payload = json.loads(output)

        self.assertEqual(set(payload), {"schema", "scope", "flags", "rule", "hidden_blind"})
        self.assertEqual(payload["schema"], "dice-astrbot.group-config.v1")
        self.assertEqual(payload["scope"], "current-group")
        self.assertEqual(payload["rule"], 6)
        self.assertIs(payload["hidden_blind"], True)
        self.assertEqual(payload["flags"], {
            "停用指令": True,
            "禁用回复": False,
            "禁用jrrp": True,
            "禁用draw": True,
            "禁用me": False,
            "禁用help": False,
        })
        for value in sensitive_values + ("g1",):
            self.assertNotIn(value, output)
        self.assertEqual(self.plugin._group_snapshot("g1"), before)
        help_text = self.plugin._help()
        self.assertIn("tojson（群管理员仅导出当前群的本地配置 JSON 子集", help_text)
        self.assertIn("survey/diver/diver4kick/pause/restart/ban/kick/title 未移植并明确拒绝", help_text)

    def test_group_tojson_requires_group_admin_current_group_and_no_arguments(self):
        for args, admin, private in (("tojson", False, False), ("tojson", True, True), ("tojson other-group", True, False)):
            with self.subTest(args=args, admin=admin, private=private):
                with self.assertRaises(ValueError):
                    self.plugin._dispatch("group", args, "g1", "user", admin, is_private=private)

    def test_group_tojson_rejects_corrupt_config_without_repairing_it(self):
        self.plugin._mutate_group("g1", lambda group: group.__setitem__("draw_enabled", "corrupt"))
        before = self.plugin._group_snapshot("g1")
        with self.assertRaises(ValueError):
            self.plugin._dispatch("group", "tojson", "g1", "admin", True)
        self.assertEqual(self.plugin._group_snapshot("g1"), before)

    def test_host_bound_upstream_commands_are_refused_without_state_changes(self):
        cases = (
            ("master", "initial-secret"),
            ("mod", "install example"),
            ("str", "welcome"),
            ("authorize", ""),
            ("dismiss", ""),
            ("groups", "list"),
            ("4kick", "user"),
            ("chan", "list"),
            ("warning", "text"),
            ("cloud", "update"),
        )
        before = self.plugin.store.snapshot()
        for command, args in cases:
            with self.subTest(command=command):
                result = self.plugin._dispatch(command, args, "g1", "user", False)
                self.assertTrue(any(marker in result for marker in ("拒绝", "不代理", "未移植", "不联网", "不会记录")))
                self.assertEqual(self.plugin.store.snapshot(), before)

    def test_platform_and_bot_identity_scopes_group_and_player_state(self):
        def make_event(platform_id, self_id, group="g1"):
            event = FakeEvent(".version", sender="same-user", group=group)
            event.get_platform_name = lambda: "onebot"
            event.get_platform_id = lambda: platform_id
            event.get_self_id = lambda: self_id
            return event

        first = make_event("connector-a", "bot-a")
        other_bot = make_event("connector-b", "bot-b")
        same_bot_other_group = make_event("connector-a", "bot-a", group="g2")
        self.assertNotEqual(Main._group_id(first), Main._group_id(other_bot))
        self.assertNotEqual(Main._player_id(first), Main._player_id(other_bot))
        self.assertNotEqual(Main._group_id(first), Main._group_id(same_bot_other_group))
        self.assertEqual(Main._player_id(first), Main._player_id(same_bot_other_group))

        self.plugin._mutate_group(Main._group_id(first), lambda group: group["banned_users"].append("same-user"))
        first_reply = self.collect(self.plugin, first)
        other_bot_reply = self.collect(self.plugin, other_bot)
        self.assertEqual(first_reply, ["你已被本群 Dice 插件禁用。"])
        self.assertIn("Dice-AstrBot", other_bot_reply[0])

    def test_private_chat_uses_sender_scoped_state_key(self):
        event = FakeEvent(".version", sender="alice", group="shared", private=True)
        self.assertEqual(Main._group_id(event), "private:alice")

    def test_send_notice_is_private_platform_admin_only_and_scoped_to_matching_subscribers(self):
        class CaptureContext:
            def __init__(self):
                self.sent = []
            async def send_message(self, target_umo, chain):
                self.sent.append((target_umo, chain))
                return True

        context = CaptureContext()
        plugin = Main(context)
        event = self.platform_event(".send notice 0+2 maintenance")
        scope = Main._platform_scope(event)
        other_bot_scope = json.dumps(["onebot", "connector-a", "bot-b"], separators=(",", ":"))

        subscriptions = (
            (scope, "connector-a", "group", "100", 1),
            (scope, "connector-a", "group", "101", 2),
            (scope, "connector-a", "private", "200", 4),
            (scope, "connector-a", "private", "500", 1),
            (other_bot_scope, "connector-a", "group", "300", 1),
        )
        for subscription_scope, platform_id, target_type, target_id, level in subscriptions:
            record = {"platform_scope": subscription_scope, "platform_id": platform_id, "target_type": target_type, "target_id": target_id, "level": level}
            key = plugin._notice_key(subscription_scope, target_type, target_id)
            self.assertTrue(plugin._valid_notice_record(key, record))
            plugin.store.transact(lambda state, key=key, record=record: state["notices"].__setitem__(key, record))

        before = plugin.store.snapshot()["notices"]
        replies = self.collect(plugin, event)
        self.assertTrue(event.stopped)
        self.assertEqual(len(replies), 1)
        self.assertIn("通知请求已接收", replies[0])
        self.assertEqual([target for target, _ in context.sent], ["connector-a:GroupMessage:100", "connector-a:FriendMessage:200"])
        self.assertEqual([chain[0].text for _, chain in context.sent], ["[Dice-AstrBot-AdminNotice] maintenance"] * 2)
        self.assertEqual(plugin.store.snapshot()["notices"], before)

    def test_send_notice_rejects_non_admin_group_target_invalid_levels_and_oversized_text(self):
        class CaptureContext:
            def __init__(self):
                self.sent = []
            async def send_message(self, target_umo, chain):
                self.sent.append((target_umo, chain))
                return True

        context = CaptureContext()
        plugin = Main(context, {"max_message_length": 1024})
        cases = (
            (".send notice0+2 blocked", "500", "999", True, False),
            (".send notice0+2 blocked", "500", "999", False, True),
            (".send 123 arbitrary target", "500", "999", True, True),
            (".send notice6 unsupported", "500", "999", True, True),
            (".send notice0 " + "x" * 501, "500", "999", True, True),
            (".send notice0 line\nbreak", "500", "999", True, True),
        )
        for message, sender, group, private, admin in cases:
            with self.subTest(message=message[:40], private=private, admin=admin):
                event = self.platform_event(message, sender=sender, group=group, private=private, admin=admin)
                before = plugin.store.snapshot()
                replies = self.collect(plugin, event)
                self.assertTrue(event.stopped)
                self.assertEqual(len(replies), 1)
                self.assertEqual(plugin.store.snapshot()["notices"], before["notices"])
        self.assertEqual(context.sent, [])

        missing_identity = FakeEvent(".send notice0 missing identity", private=True, admin=True)
        self.assertIn("有效的平台/bot 身份", self.collect(plugin, missing_identity)[0])
        no_cross_send = Main(object(), {"max_message_length": 1024})
        valid_event = self.platform_event(".send notice0 no transport")
        self.assertIn("不支持跨会话发送", self.collect(no_cross_send, valid_event)[0])

        short_cap = Main(context, {"max_response_length": 256})
        too_long_for_payload = self.platform_event(".send notice0 " + "x" * 300)
        capped_reply = self.collect(short_cap, too_long_for_payload)
        self.assertIn("当前单条消息上限", capped_reply[0])
        self.assertEqual(context.sent, [])

    def test_native_message_listener_dispatches_and_stops_command(self):
        event = FakeEvent(".version")
        replies = self.collect(self.plugin, event)
        self.assertTrue(event.stopped)
        self.assertEqual(len(replies), 1)
        self.assertIn("Dice-AstrBot 0.2.0", replies[0])

    def test_welcome_configuration_round_trips_through_listener(self):
        group_id = Main._group_id(FakeEvent(".welcome show"))
        commands = (
            (".welcome set 欢迎来到冒险团", "已保存"),
            (".welcome show", "欢迎来到冒险团"),
            (".welcome 直接文本覆盖", "已保存"),
            (".welcome show", "直接文本覆盖"),
            (".welcome clr", "已清除"),
            (".welcome show", "尚未设置欢迎语"),
        )
        for message, expected in commands:
            with self.subTest(message=message):
                event = FakeEvent(message, group="g1", admin=True)
                replies = self.collect(self.plugin, event)
                self.assertTrue(event.stopped)
                self.assertEqual(len(replies), 1)
                self.assertIn(expected, replies[0])
        self.assertEqual(self.plugin._group_snapshot(group_id)["welcome_message"], "")
        self.assertIn("仅 OneBot V11", self.collect(self.plugin, FakeEvent(".welcome set 当前群", admin=True))[0])

    def test_onebot_group_join_sends_configured_welcome_only(self):
        notice = {"post_type": "notice", "notice_type": "group_increase", "sub_type": "approve", "group_id": "999", "user_id": "12345"}
        event = self.onebot_notice_event(notice)
        group_id = Main._group_id(event)
        self.plugin._mutate_group(group_id, lambda group: group.__setitem__("welcome_message", "欢迎来到冒险团！"))
        self.assertEqual(self.collect(self.plugin, event), ["欢迎来到冒险团！"])
        self.assertFalse(event.stopped)
        invite_event = self.onebot_notice_event({**notice, "sub_type": "invite"})
        self.assertEqual(self.collect(self.plugin, invite_event), ["欢迎来到冒险团！"])

        empty_notice = {**notice, "group_id": "1000", "sub_type": "invite"}
        empty_event = self.onebot_notice_event(empty_notice, group="1000")
        self.assertEqual(self.collect(self.plugin, empty_event), [])

    def test_onebot_group_departure_notice_distinguishes_leave_kick_and_bot_removal(self):
        cases = (("leave", "群成员 12345 已退出本群。"), ("kick", "群成员 12345 已被移出本群。"))
        for sub_type, expected in cases:
            with self.subTest(sub_type=sub_type):
                event = self.onebot_notice_event({"post_type": "notice", "notice_type": "group_decrease", "sub_type": sub_type, "group_id": "999", "user_id": "12345"})
                self.assertEqual(self.collect(self.plugin, event), [expected])
        bot_removed = self.onebot_notice_event({"post_type": "notice", "notice_type": "group_decrease", "sub_type": "kick_me", "group_id": "999", "user_id": "10001"})
        self.assertEqual(self.collect(self.plugin, bot_removed), [])

    def test_onebot_lifecycle_notice_rejects_other_adapters_malformed_and_mismatched_groups(self):
        notice = {"post_type": "notice", "notice_type": "group_increase", "sub_type": "approve", "group_id": "999", "user_id": "12345"}
        event = self.onebot_notice_event(notice)
        self.plugin._mutate_group(Main._group_id(event), lambda group: group.__setitem__("welcome_message", "欢迎！"))
        other_adapter = self.onebot_notice_event(notice, platform_name="other")
        self.assertEqual(self.collect(self.plugin, other_adapter), [])
        malformed = self.onebot_notice_event({**notice, "user_id": "not-an-id"})
        self.assertEqual(self.collect(self.plugin, malformed), [])
        mismatched_group = self.onebot_notice_event(notice, group="1000")
        self.assertEqual(self.collect(self.plugin, mismatched_group), [])

    def test_onebot_lifecycle_notices_respect_global_and_group_disable_switches(self):
        notice = {"post_type": "notice", "notice_type": "group_increase", "sub_type": "approve", "group_id": "999", "user_id": "12345"}
        event = self.onebot_notice_event(notice)
        group_id = Main._group_id(event)
        self.plugin._mutate_group(group_id, lambda group: group.__setitem__("welcome_message", "欢迎！"))
        self.plugin._mutate_group(group_id, lambda group: group.__setitem__("enabled", False))
        self.assertEqual(self.collect(self.plugin, event), [])
        self.plugin._mutate_group(group_id, lambda group: group.__setitem__("enabled", True))
        self.plugin._mutate_system(lambda system: system.__setitem__("enabled", False))
        self.assertEqual(self.collect(self.plugin, event), [])

    def test_welcome_access_and_length_limits_reject_without_overwriting(self):
        original_root = StarTools.data_root
        StarTools.data_root = Path(self.temp.name) / "welcome-length-test"
        try:
            plugin = Main(object(), {"max_message_length": 1024})
            group_id = Main._group_id(FakeEvent(".welcome show"))
            plugin._mutate_group(group_id, lambda group: group.__setitem__("welcome_message", "preserved"))

            denied = FakeEvent(".welcome set unauthorized", admin=False)
            denied_reply = self.collect(plugin, denied)
            self.assertTrue(denied.stopped)
            self.assertIn("仅群管理员", denied_reply[0])
            self.assertEqual(plugin._group_snapshot(group_id)["welcome_message"], "preserved")

            private = FakeEvent(".welcome set private", private=True, admin=True)
            private_reply = self.collect(plugin, private)
            self.assertTrue(private.stopped)
            self.assertIn("仅支持当前群", private_reply[0])
            self.assertEqual(plugin._group_snapshot(group_id)["welcome_message"], "preserved")

            oversized = FakeEvent(".welcome set " + "x" * 501, group="g1", admin=True)
            oversized_reply = self.collect(plugin, oversized)
            self.assertTrue(oversized.stopped)
            self.assertIn("最多 500 个字符", oversized_reply[0])
            self.assertEqual(plugin._group_snapshot(group_id)["welcome_message"], "preserved")

            exact_limit = FakeEvent(".welcome set " + "x" * 500, group="g1", admin=True)
            exact_reply = self.collect(plugin, exact_limit)
            self.assertTrue(exact_limit.stopped)
            self.assertIn("500/500", exact_reply[0])
            self.assertEqual(plugin._group_snapshot(group_id)["welcome_message"], "x" * 500)
        finally:
            StarTools.data_root = original_root

    def test_group_deck_create_and_draw_are_persisted(self):
        created = self.plugin._dispatch("deck", "create omen | sun | moon", "g1", "u1", False)
        self.assertIn("共 2 张", created)
        drawn = self.plugin._dispatch("draw", "omen", "g1", "u1", False)
        self.assertRegex(drawn, r"抽到：(sun|moon)（剩余 1 张）")
        self.assertIn("剩 1 张", self.plugin._dispatch("deck", "list", "g1", "u1", False))

    def test_deck_weight_dice_expression_expands_for_group_and_session_decks(self):
        rng = SequenceRandom([2, 1, 4])
        self.plugin.engine._rng = rng
        created = self.plugin._dispatch("deck", "new loot=rare::[2d3]::gem | common::2::coin", "g1", "u1", False)
        self.assertIn("共 5 张", created)
        group_cards = self.plugin._group_snapshot("g1")["decks"]["loot"]["cards"]
        self.assertEqual(group_cards.count("gem"), 3)
        self.assertEqual(group_cards.count("coin"), 2)
        self.assertEqual(rng.calls, 2)

        self.plugin._dispatch("game", "new weighted", "g1", "gm", True)
        saved = self.plugin._dispatch("deck", "new treasure=rare::[1d6]::gold", "g1", "gm", False)
        self.assertIn("共 4 张", saved)
        session_cards = self.plugin._group_snapshot("g1")["session"]["decks"]["treasure"]["cards"]
        self.assertEqual(session_cards, ["gold"] * 4)
        self.assertEqual(rng.calls, 3)

    def test_dynamic_deck_weight_rejects_invalid_and_oversized_input_without_writes(self):
        for index, weight in enumerate(("[0]", "[1000]", "[not-a-die]", "[{template}]")):
            name = f"bad{index}"
            with self.subTest(weight=weight), self.assertRaises(ValueError):
                self.plugin._dispatch("deck", f"new {name}=rare::{weight}::gem", "g1", "u1", False)
            self.assertNotIn(name, self.plugin._group_snapshot("g1")["decks"])

        too_many = "|".join(["x"] * 257)
        with self.assertRaisesRegex(ValueError, "最多包含 256 个输入项"):
            self.plugin._dispatch("deck", f"new raw={too_many}", "g1", "u1", False)
        self.assertNotIn("raw", self.plugin._group_snapshot("g1")["decks"])

        too_long = "1" * 161
        with self.assertRaisesRegex(ValueError, "动态权重须写成"):
            self.plugin._dispatch("deck", f"new long=rare::[{too_long}]::gem", "g1", "u1", False)
        self.assertNotIn("long", self.plugin._group_snapshot("g1")["decks"])

        with self.assertRaisesRegex(ValueError, "1024 张安全上限"):
            self.plugin._dispatch("deck", "new oversized=one::[600]::a | two::[425]::b", "g1", "u1", False)
        self.assertNotIn("oversized", self.plugin._group_snapshot("g1")["decks"])

    def test_dynamic_deck_weight_enforces_aggregate_random_budget_atomically(self):
        rng = SequenceRandom()
        self.plugin.engine._rng = rng
        items = ["w::[999d1/999]::x"] * 101
        with self.assertRaisesRegex(ValueError, "超过 100000 次"):
            self.plugin._dispatch("deck", "new budget=" + " | ".join(items), "g1", "u1", False)
        self.assertEqual(rng.calls, 100_000)
        self.assertNotIn("budget", self.plugin._group_snapshot("g1")["decks"])

    def test_deck_set_range_defaults_missing_end_to_zero(self):
        group_result = self.plugin._dispatch("deck", "set group_range=range 5", "g1", "u1", False)
        self.assertIn("已从静态来源保存牌堆", group_result)
        self.assertEqual(self.plugin._group_snapshot("g1")["decks"]["group_range"]["cards"], [str(value) for value in range(6)])
        with self.assertRaises(ValueError):
            self.plugin._dispatch("deck", "set too_large=range 0 2000", "g1", "u1", False)
        self.assertNotIn("too_large", self.plugin._group_snapshot("g1")["decks"])

        self.plugin._dispatch("game", "new range_session", "g2", "gm", True)
        session_result = self.plugin._dispatch("deck", "set session_range=range 5", "g2", "gm", False)
        self.assertIn("已保存，共 6 张", session_result)
        self.assertEqual(self.plugin._group_snapshot("g2")["session"]["decks"]["session_range"]["cards"], [str(value) for value in range(6)])
        with self.assertRaises(ValueError):
            self.plugin._dispatch("deck", "set zero_step=range 1 5 0", "g2", "gm", False)
        self.assertNotIn("zero_step", self.plugin._group_snapshot("g2")["session"]["decks"])

    def test_session_deck_set_accepts_builtin_template_but_group_set_rejects(self):
        self.plugin._dispatch("game", "new template_set", "g1", "gm", True)
        saved = self.plugin._dispatch("deck", "set session_names=随机姓名", "g1", "gm", False)
        self.assertIn("已保存，共 10 张", saved)
        cards = self.plugin._group_snapshot("g1")["session"]["decks"]["session_names"]["cards"]
        self.assertEqual(len(cards), 10)

        drawn = self.plugin._dispatch("draw", "session_names", "g1", "member", False)
        self.assertNotIn("{", drawn)
        self.assertNotIn("}", drawn)

        with self.assertRaisesRegex(ValueError, "模板公共牌堆含动态展开"):
            self.plugin._dispatch("deck", "set group_names=随机姓名", "g2", "u1", False)
        self.assertNotIn("group_names", self.plugin._group_snapshot("g2")["decks"])
        with self.assertRaisesRegex(ValueError, "静态公共牌堆不存在"):
            self.plugin._dispatch("deck", "set group_unknown=not-a-public-deck", "g2", "u1", False)
        self.assertNotIn("group_unknown", self.plugin._group_snapshot("g2")["decks"])
        with self.assertRaisesRegex(ValueError, "公共牌堆来源不存在"):
            self.plugin._dispatch("deck", "set session_unknown=not-a-public-deck", "g1", "gm", False)
        self.assertNotIn("session_unknown", self.plugin._group_snapshot("g1")["session"]["decks"])

    def test_session_deck_draw_expands_public_refs_and_dice(self):
        self.plugin._dispatch("game", "new template", "g1", "gm", True)
        self.plugin.engine._rng = SequenceRandom([4])
        self.plugin._dispatch("deck", "new cards=roll [1d6] | name {随机姓名}", "g1", "gm", False)

        result = self.plugin._dispatch("draw", "cards 2", "g1", "member", False)

        self.assertIn("roll 4", result)
        self.assertIn("name ", result)
        self.assertNotIn("[1d6]", result)
        self.assertNotIn("{随机姓名}", result)
        self.assertIn("剩余 0 张", result)

    def test_session_template_budget_failure_rolls_back_card_consumption(self):
        self.plugin._dispatch("game", "new stress", "g1", "gm", True)
        repeated_template = "{随机姓名}" * 20
        self.plugin._dispatch("deck", "new stress=" + " | ".join([repeated_template] * 10), "g1", "gm", False)
        before = self.plugin._group_snapshot("g1")["session"]["decks"]["stress"]["remaining"]
        self.assertEqual(len(before), 10)

        with self.assertRaisesRegex(ValueError, "draw budget exceeded"):
            self.plugin._dispatch("draw", "stress 10", "g1", "member", False)

        after = self.plugin._group_snapshot("g1")["session"]["decks"]["stress"]["remaining"]
        self.assertEqual(after, before)

    def test_coc_rules_set_show_clear_and_ra_rc_share_room_rule(self):
        self.assertIn("当前检定房规：0", self.plugin._dispatch("setcoc", "show", "g1", "member", False))
        denied = self.plugin._dispatch("setcoc", "6", "g1", "member", False)
        self.assertIn("只有群管理员", denied)
        self.assertIsNone(self.plugin._group_snapshot("g1")["rule"])

        changed = self.plugin._dispatch("setcoc", "6", "g1", "admin", True)
        self.assertIn("绿色三角洲", changed)
        self.assertEqual(self.plugin._effective_coc_rule("g1"), 6)
        self.assertIn("房规 6", self.plugin._dispatch("ra", "50", "g1", "member", False))
        self.assertIn("房规 6", self.plugin._dispatch("rc", "50", "g1", "member", False))
        for rule in range(8):
            with self.subTest(rule=rule):
                self.plugin._dispatch("setcoc", str(rule), "g1", "admin", True)
                self.assertEqual(self.plugin._effective_coc_rule("g1"), rule)

        cleared = self.plugin._dispatch("setcoc", "clr", "g1", "admin", True)
        self.assertIn("当前生效：0", cleared)
        self.assertIsNone(self.plugin._group_snapshot("g1")["rule"])
        self.assertIn("房规 0", self.plugin._dispatch("rc", "50", "g1", "member", False))

        configured = Main(object(), {"default_coc_rule": 5})
        self.assertEqual(configured._effective_coc_rule("g1"), 5)
        self.assertIn("房规 5", configured._dispatch("ra", "50", "g1", "member", False))

    def test_ri_rolls_and_records_initiative_session(self):
        self.plugin._mutate_group("g1", lambda group: group.__setitem__("session", {"title": "test", "initiative": []}))
        engine = SequenceEngine([18, 3])
        self.plugin.engine = engine
        result = self.plugin._dispatch("ri", "2#Alice", "g1", "u1", False)
        self.assertEqual(engine.calls, [("2", 20), ("D20", 20), ("D20", 20)])
        self.assertIn("Alice1：D20=18", result)
        self.assertEqual(
            [(entry["name"], entry["score"], entry["expression"]) for entry in self.plugin._group_snapshot("g1")["session"]["initiative"]],
            [("Alice1", 18, "D20"), ("Alice2", 3, "D20")],
        )
        shown = self.plugin._dispatch("init", "show", "g1", "u1", False)
        self.assertIn("Alice1(18)", shown)
        self.assertIn("Alice2(3)", shown)

    def test_ri_accepts_custom_expression_and_rejects_private_chat(self):
        engine = SequenceEngine([12])
        self.plugin.engine = engine
        self.plugin._dispatch("ri", "2d6+1 Alice", "g1", "u1", False)
        self.assertEqual(engine.calls, [("2d6+1", 20)])
        event = FakeEvent(".ri", private=True)
        self.assertIn("仅支持群聊", self.collect(self.plugin, event)[0])

    def test_character_creation_rejects_duplicate_names_without_overwriting(self):
        self.plugin._dispatch("pc", "create Alice", "g1", "u1", False)
        original = self.plugin.store.player("u1")["cards"]["Alice"]
        with self.assertRaisesRegex(ValueError, "已存在"):
            self.plugin._dispatch("pc", "create Alice", "g1", "u1", False)
        user = self.plugin.store.player("u1")
        self.assertEqual(user["cards"]["Alice"], original)
        self.assertEqual(user["active_card"], "Alice")

    def test_generated_cards_reject_explicit_collision_and_auto_name_defaults(self):
        self.plugin._generate_character("coc7d", "Hero", "u2")
        original = self.plugin.store.player("u2")["cards"]["Hero"]
        with self.assertRaisesRegex(ValueError, "已存在"):
            self.plugin._generate_character("coc7d", "Hero", "u2")
        self.assertEqual(self.plugin.store.player("u2")["cards"]["Hero"], original)
        self.plugin._generate_character("coc7d", "", "u3")
        self.plugin._generate_character("coc7d", "", "u3")
        self.assertEqual(set(self.plugin.store.player("u3")["cards"]), {"COC7", "COC7 2"})

    def test_coc7d_reply_preserves_upstream_report_and_card_creation(self):
        rng = SequenceRandom([1] * 24)
        self.plugin.engine._rng = rng
        reply = self.plugin._generate_character("coc7d", "Hero", "u-coc7", "g1")
        expected = "\n".join((
            "Hero的调查员作成:",
            "力量STR=3D6*5=15/7/3",
            "体质CON=3D6*5=15/7/3",
            "体型SIZ=(2D6+6)*5=40/20/8",
            "敏捷DEX=3D6*5=15/7/3",
            "外貌APP=3D6*5=15/7/3",
            "智力INT=(2D6+6)*5=40/20/8",
            "意志POW=3D6*5=15/7/3",
            "教育EDU=(2D6+6)*5=40/20/8",
            "幸运LUCK=3D6*5=15",
            "共计:195/210",
            "理智SAN=POW=15",
            "生命值HP=(SIZ+CON)/10=5",
            "魔法值MP=POW/5=3",
            "伤害奖励DB=-2",
            "体格=-2",
            "移动力MOV=7",
            "（角色卡已保存并设为当前活动卡）",
        ))
        self.assertEqual(reply, expected)
        self.assertEqual(rng.calls, 24)
        player = self.plugin.store.player("u-coc7")
        card = player["cards"]["Hero"]
        self.assertEqual(card["attributes"]["LUCK"], 15)
        self.assertEqual(card["attributes"]["HP"], 5)
        self.assertEqual(player["active_cards_by_group"]["g1"], "Hero")

    def test_coc6d_reply_preserves_upstream_report_and_card_creation(self):
        rng = SequenceRandom([1] * 23)
        self.plugin.engine._rng = rng
        reply = self.plugin._generate_character("coc6d", "Investigator", "u-coc6", "g1")
        expected = "\n".join((
            "Investigator的调查员作成:",
            "力量STR=3D6=3",
            "体质CON=3D6=3",
            "体型SIZ=2D6+6=8",
            "敏捷DEX=3D6=3",
            "外貌APP=3D6=3",
            "智力INT=2D6+6=8",
            "意志POW=3D6=3",
            "教育EDU=3D6+3=6",
            "共计:37",
            "理智SAN=POW*5=15",
            "灵感IDEA=INT*5=40",
            "幸运LUCK=POW*5=15",
            "知识KNOW=EDU*5=30",
            "生命值HP=(CON+SIZ)/2=6",
            "魔法值MP=POW=3",
            "伤害奖励DB=-1D6",
            "资产=1D10=1",
            "（角色卡已保存并设为当前活动卡）",
        ))
        self.assertEqual(reply, expected)
        self.assertEqual(rng.calls, 23)
        player = self.plugin.store.player("u-coc6")
        card = player["cards"]["Investigator"]
        self.assertEqual(card["attributes"]["ASSET"], 1)
        self.assertEqual(player["active_cards_by_group"]["g1"], "Investigator")

    def test_generated_character_uses_complete_compact_reply_under_256_cap(self):
        self.plugin.max_response_length = 256
        rng = SequenceRandom([6] * 24)
        self.plugin.engine._rng = rng
        name = "N" * 40
        reply = self.plugin._generate_character("coc7d", name, "u-cap", "g1")
        self.assertLessEqual(len(reply), 256)
        self.assertIn(f"已生成并选中 COC7 角色卡「{name}」", reply)
        self.assertIn("LUCK=90", reply)
        self.assertIn("MOV=8", reply)
        self.assertNotIn("力量STR=3D6*5=", reply)
        self.assertEqual(rng.calls, 24)
        self.assertIn(name, self.plugin.store.player("u-cap")["cards"])

    def test_session_coc_rule_overrides_group_for_ra_rc_but_not_sc(self):
        self.plugin._mutate_group("g1", lambda group: (group.__setitem__("rule", 5), group.__setitem__("session", {"title": "test", "initiative": []})))
        self.assertEqual(self.plugin._effective_coc_rule("g1"), 5)
        self.plugin._dispatch("setcoc", "6", "g1", "admin", True)
        group = self.plugin._group_snapshot("g1")
        self.assertEqual(group["rule"], 5)
        self.assertEqual(group["session"]["rule"], 6)
        self.assertEqual(self.plugin._effective_coc_rule("g1"), 6)
        self.assertEqual(self.plugin._effective_coc_rule("g1", include_session=False), 5)
        self.assertIn("房规 6", self.plugin._dispatch("ra", "50", "g1", "member", False))
        self.assertIn("房规 6", self.plugin._dispatch("rc", "50", "g1", "member", False))

        class SanityEngine:
            def __init__(self):
                self.rule = None
            def check(self, _skill, *, rule):
                self.rule = rule
                return types.SimpleNamespace(success=True, roll=1)
            def roll(self, _expression, _default_die=100):
                return types.SimpleNamespace(total=1)

        self.plugin._dispatch("pc", "create Sanity", "g1", "member", False)
        self.plugin._dispatch("pc", "set Sanity SAN 50", "g1", "member", False)
        sanity_engine = SanityEngine()
        self.plugin.engine = sanity_engine
        self.plugin._sanity("1/1", "g1", "member")
        self.assertEqual(sanity_engine.rule, 5)

        self.plugin._dispatch("setcoc", "clr", "g1", "admin", True)
        group = self.plugin._group_snapshot("g1")
        self.assertNotIn("rule", group["session"])
        self.assertEqual(group["rule"], 5)
        self.assertEqual(self.plugin._effective_coc_rule("g1"), 5)
        self.plugin._dispatch("setcoc", "clr", "g1", "admin", True)
        self.assertIsNone(self.plugin._group_snapshot("g1")["rule"])
        self.assertEqual(self.plugin._effective_coc_rule("g1"), 0)

    def test_game_start_close_open_over_manage_archived_sessions_with_permissions(self):
        denied_start = self.plugin._dispatch("game", "start 冒险", "g1", "member", False)
        self.assertIn("只有群管理员", denied_start)
        self.assertIsNone(self.plugin._group_snapshot("g1")["session"])

        started = self.plugin._dispatch("game", "new 冒险", "g1", "gm", True)
        self.assertIn("团务已开启", started)
        original_session = self.plugin._group_snapshot("g1")["session"]
        first_id = original_session["id"]
        self.assertEqual(first_id, "冒险#1")
        self.assertEqual(original_session["game_master"], "gm")

        second_start = self.plugin._dispatch("game", "start 第二场", "g1", "other-admin", True)
        self.assertIn("团务已开启", second_start)
        group = self.plugin._group_snapshot("g1")
        second_id = group["session"]["id"]
        self.assertEqual(group["sessions"][first_id], original_session)
        self.assertEqual(group["session"]["title"], "第二场")

        denied_close = self.plugin._dispatch("game", "stop", "g1", "member", False)
        self.assertIn("只有团务主持人或群管理员", denied_close)
        self.assertIsNotNone(self.plugin._group_snapshot("g1")["session"])

        closed = self.plugin._dispatch("game", "close", "g1", "other-admin", False)
        self.assertIn(f"桌号 {second_id}", closed)
        group = self.plugin._group_snapshot("g1")
        self.assertIsNone(group["session"])
        self.assertIn(second_id, group["sessions"])

        denied_open = self.plugin._dispatch("game", f"open {first_id}", "g1", "member", False)
        self.assertIn("只有该团务主持人", denied_open)
        opened = self.plugin._dispatch("game", f"open {first_id}", "g1", "gm", False)
        self.assertIn("已在当前聊天中开启", opened)
        group = self.plugin._group_snapshot("g1")
        self.assertEqual(group["session"]["id"], first_id)
        self.assertIn(second_id, group["sessions"])
        self.assertNotIn(first_id, group["sessions"])

        self.assertEqual(self.plugin._dispatch("game", "over", "g1", "other-admin", True), "当前团务已删除。")
        group = self.plugin._group_snapshot("g1")
        self.assertIsNone(group["session"])
        self.assertNotIn(first_id, group["sessions"])
        self.assertIn(second_id, group["sessions"])
        self.assertEqual(self.plugin._dispatch("game", "stop", "g1", "other-admin", True), "当前没有进行中的团务。")

    def test_game_open_shares_cross_chat_session_with_scope_and_global_over(self):
        scope = '["onebot","platform","bot"]'
        other_bot_scope = '["onebot","platform","other-bot"]'
        group_one = f"{scope}:g1"
        group_two = f"{scope}:g2"
        group_three = f"{scope}:g3"
        other_bot_group = f"{other_bot_scope}:g1"
        self.plugin._dispatch("game", "new Shared", group_one, "gm", True, platform_scope=scope)

        opened = self.plugin._dispatch("game", "open Shared#1", group_two, "gm", False, platform_scope=scope)
        self.assertIn("共享团务", opened)
        self.assertEqual(self.plugin._group_snapshot(group_one)["session"]["id"], "Shared#1")
        self.assertEqual(self.plugin._group_snapshot(group_two)["session"]["id"], "Shared#1")
        self.assertEqual(len(self.plugin.store.snapshot()["shared_sessions"]), 1)

        denied_admin = self.plugin._dispatch("game", "open Shared#1", group_three, "outsider", True, platform_scope=scope)
        self.assertIn("只有该团务主持人", denied_admin)
        self.assertIsNone(self.plugin._group_snapshot(group_three)["session"])
        hidden_from_other_bot = self.plugin._dispatch("game", "open Shared#1", other_bot_group, "gm", False, platform_scope=other_bot_scope)
        self.assertIn("找不到当前平台桌号", hidden_from_other_bot)

        self.plugin._dispatch("game", "join", group_two, "player", False, platform_scope=scope)
        self.assertIn("player", self.plugin._group_snapshot(group_one)["session"]["participants"])
        reloaded = Main(object())
        self.assertIn("player", reloaded._group_snapshot(group_two)["session"]["participants"])

        detached = reloaded._dispatch("game", "close", group_two, "gm", False, platform_scope=scope)
        self.assertIn("解除共享团务挂载", detached)
        self.assertIsNone(reloaded._group_snapshot(group_two)["session"])
        self.assertEqual(reloaded._group_snapshot(group_one)["session"]["id"], "Shared#1")

        reattached = reloaded._dispatch("game", "open Shared#1", group_two, "gm", False, platform_scope=scope)
        self.assertIn("共享团务", reattached)
        deleted = reloaded._dispatch("game", "over", group_two, "gm", False, platform_scope=scope)
        self.assertEqual(deleted, "当前团务已删除。")
        self.assertIsNone(reloaded._group_snapshot(group_one)["session"])
        self.assertIsNone(reloaded._group_snapshot(group_two)["session"])
        self.assertEqual(reloaded.store.snapshot()["shared_sessions"], {})

    def test_game_open_can_migrate_archived_session_to_another_chat(self):
        scope = '["onebot","platform","bot"]'
        group_one = f"{scope}:g1"
        group_two = f"{scope}:g2"
        self.plugin._dispatch("game", "new Archived", group_one, "gm", True, platform_scope=scope)
        self.plugin._dispatch("game", "close", group_one, "gm", False, platform_scope=scope)
        self.assertIn("Archived#1", self.plugin._group_snapshot(group_one)["sessions"])

        opened = self.plugin._dispatch("game", "open Archived#1", group_two, "gm", False, platform_scope=scope)
        self.assertIn("共享团务", opened)
        self.assertNotIn("Archived#1", self.plugin._group_snapshot(group_one)["sessions"])
        self.assertEqual(self.plugin._group_snapshot(group_two)["session"]["id"], "Archived#1")

    def test_private_session_participant_can_open_shared_table(self):
        scope = '["onebot","platform","bot"]'
        group_one = f"{scope}:g1"
        private_room = f"{scope}:private:player"
        self.plugin._dispatch("game", "new Private", group_one, "gm", True, platform_scope=scope)
        self.plugin._dispatch("game", "join", group_one, "player", False, platform_scope=scope)

        opened = self.plugin._dispatch("game", "open Private#1", private_room, "player", False, True, platform_scope=scope)
        self.assertIn("共享团务", opened)
        self.assertEqual(self.plugin._group_snapshot(private_room)["session"]["id"], "Private#1")

    def test_game_new_detaches_shared_mount_without_copying_old_session(self):
        scope = '["onebot","platform","bot"]'
        group_one = f"{scope}:g1"
        group_two = f"{scope}:g2"
        self.plugin._dispatch("game", "new Shared", group_one, "gm", True, platform_scope=scope)
        self.plugin._dispatch("game", "open Shared#1", group_two, "gm", False, platform_scope=scope)

        created = self.plugin._dispatch("game", "new Replacement", group_two, "admin", True, platform_scope=scope)
        self.assertIn("Replacement#1", created)
        self.assertEqual(self.plugin._group_snapshot(group_one)["session"]["id"], "Shared#1")
        self.assertEqual(self.plugin._group_snapshot(group_two)["session"]["id"], "Replacement#1")
        self.assertIsNone(self.plugin._group_snapshot(group_two)["session_ref"])
        self.assertEqual(len(self.plugin.store.snapshot()["shared_sessions"]), 1)

        self.plugin._dispatch("game", "over", group_one, "gm", False, platform_scope=scope)
        self.assertIsNone(self.plugin._group_snapshot(group_one)["session"])
        self.assertEqual(self.plugin._group_snapshot(group_two)["session"]["id"], "Replacement#1")

    def test_game_open_rejects_ambiguous_ids_and_missing_scope(self):
        scope = '["onebot","platform","bot"]'
        group_one = f"{scope}:g1"
        group_two = f"{scope}:g2"
        group_three = f"{scope}:g3"
        self.plugin._dispatch("game", "new Duplicate", group_one, "gm", True, platform_scope=scope)
        self.plugin._dispatch("game", "new Duplicate", group_two, "gm", True, platform_scope=scope)

        ambiguous = self.plugin._dispatch("game", "open Duplicate#1", group_three, "gm", False, platform_scope=scope)
        self.assertIn("找到多个桌号", ambiguous)
        self.assertIsNone(self.plugin._group_snapshot(group_three)["session"])
        self.assertEqual(self.plugin.store.snapshot()["shared_sessions"], {})

        unscoped = self.plugin._dispatch("game", "open Duplicate#1", "unscoped-target", "gm", False)
        self.assertIn("找不到当前平台桌号", unscoped)
        self.assertIsNone(self.plugin._group_snapshot("unscoped-target")["session"])

    def test_foreign_scope_session_ref_is_invisible_and_immutable(self):
        local_scope = '["onebot","platform","bot"]'
        foreign_scope = '["other","platform","bot"]'
        group_id = f"{local_scope}:g1"
        reference = "a" * 32
        foreign_session = {"id": "Foreign#1", "game_master": "gm", "game_masters": ["gm"], "participants": [], "attributes": {}}

        def inject_foreign_reference(state):
            state["shared_sessions"][reference] = {"scope": foreign_scope, "session": foreign_session}
            group = self.plugin._default_group()
            group["session_ref"] = reference
            state["groups"][group_id] = group

        self.plugin.store.transact(inject_foreign_reference)
        snapshot = self.plugin._group_snapshot(group_id)
        self.assertIsNone(snapshot["session"])
        self.assertIsNone(snapshot["session_ref"])
        opened = self.plugin._dispatch("game", "open Foreign#1", group_id, "gm", False, platform_scope=local_scope)
        self.assertIn("找不到当前平台桌号", opened)

        changed = self.plugin._dispatch("game", "set theme=stolen", group_id, "gm", False, platform_scope=local_scope)
        self.assertIn("没有进行中的团务", changed)
        closed = self.plugin._dispatch("game", "over", group_id, "gm", False, platform_scope=local_scope)
        self.assertIn("没有进行中的团务", closed)
        stored = self.plugin.store.snapshot()
        self.assertEqual(stored["shared_sessions"][reference]["session"]["attributes"], {})
        self.assertIn(reference, stored["shared_sessions"])

    def test_game_over_clears_only_same_scope_session_mounts(self):
        room_scope = '["onebot","platform","bot"]'
        foreign_scope = '["other","platform","bot"]'
        room_group = f"{room_scope}:owner"
        foreign_group = f"{foreign_scope}:target"
        reference = "b" * 32
        session = {"id": "Scoped#1", "game_master": "gm", "game_masters": ["gm"], "participants": []}

        def inject_mounts(state):
            state["shared_sessions"][reference] = {"scope": room_scope, "session": session}
            for group_id in (room_group, foreign_group):
                group = self.plugin._default_group()
                group["session_ref"] = reference
                state["groups"][group_id] = group

        self.plugin.store.transact(inject_mounts)
        result = self.plugin._dispatch("game", "over", room_group, "gm", False, platform_scope=room_scope)
        self.assertEqual(result, "当前团务已删除。")
        state = self.plugin.store.snapshot()
        self.assertNotIn(reference, state["shared_sessions"])
        self.assertEqual(state["groups"][foreign_group]["session_ref"], reference)
        self.assertIsNone(self.plugin._group_snapshot(foreign_group)["session"])

    def test_message_handler_uses_platform_bot_scope_for_shared_open(self):
        def scoped_event(message, sender, group, admin=False):
            event = FakeEvent(message, sender=sender, group=group, admin=admin)
            event.get_platform_name = lambda: "onebot"
            event.get_platform_id = lambda: "platform"
            event.get_self_id = lambda: "bot"
            return event

        create_event = scoped_event(".game new ThroughListener", "gm", "g1", True)
        created = self.collect(self.plugin, create_event)
        self.assertTrue(create_event.stopped)
        self.assertIn("ThroughListener#1", "".join(map(str, created)))

        open_event = scoped_event(".game open ThroughListener#1", "gm", "g2")
        opened = self.collect(self.plugin, open_event)
        self.assertTrue(open_event.stopped)
        self.assertIn("共享团务", "".join(map(str, opened)))
        group_one = self.plugin._group_id(create_event)
        group_two = self.plugin._group_id(open_event)
        self.assertEqual(self.plugin._group_snapshot(group_one)["session"]["id"], "ThroughListener#1")
        self.assertEqual(self.plugin._group_snapshot(group_two)["session"]["id"], "ThroughListener#1")

    def test_game_master_set_call_kick_and_exit_semantics(self):
        self.plugin._dispatch("game", "new Campaign", "g1", "gm", True)
        self.assertEqual(self.plugin._group_snapshot("g1")["session"]["participants"], [])
        self.assertIn("只有现任主持人", self.plugin._dispatch("game", "master", "g1", "member", False))
        self.assertIn("已加入团务主持人", self.plugin._dispatch("game", "master", "g1", "admin2", True))
        self.assertIn("团务主持人", self.plugin._dispatch("game", "master", "g1", "gm", False))

        self.assertIn("只有团务主持人", self.plugin._dispatch("game", "set theme=one shot", "g1", "member", False))
        self.assertIn("已设置团务属性", self.plugin._dispatch("game", "set theme=one shot", "g1", "gm", False))
        self.assertEqual(self.plugin._dispatch("game", "set theme", "g1", "gm", False), "theme=one shot")
        self.assertIn("已设为 6", self.plugin._dispatch("game", "set rule=6", "g1", "gm", False))
        self.assertEqual(self.plugin._effective_coc_rule("g1"), 6)
        with self.assertRaises(ValueError):
            self.plugin._dispatch("game", "set title=overwritten", "g1", "gm", False)

        self.assertIn("已加入团务", self.plugin._dispatch("game", "join", "g1", "player", False))
        self.assertIn("player", self.plugin._dispatch("game", "call", "g1", "admin2", False))
        self.assertIn("只有团务主持人", self.plugin._dispatch("game", "kick player", "g1", "member", False))
        self.assertIn("已将 player 移出", self.plugin._dispatch("game", "kick player", "g1", "gm", False))
        self.assertEqual(self.plugin._group_snapshot("g1")["session"]["participants"], [])
        self.plugin._dispatch("game", "exit", "g1", "gm", False)
        session = self.plugin._group_snapshot("g1")["session"]
        self.assertNotIn("gm", session["game_masters"])
        self.assertEqual(session["game_master"], "admin2")

    def test_game_set_parses_typed_json_attribute_values(self):
        self.plugin._dispatch("game", "new Typed attributes", "g1", "gm", True)
        cases = [
            ("enabled", "true", True, "true"),
            ("disabled", "false", False, "false"),
            ("count", "12", 12, "12"),
            ("ratio", "1.239", 1.239, "1.23"),
            ("payload", '{"x":[1,true],"label":"ok"}', {"x": [1, True], "label": "ok"}, '{"x":[1,true],"label":"ok"}'),
            ("labels", '["a","b"]', ["a", "b"], '["a","b"]'),
            ("quoted", '"two words"', "two words", "two words"),
            ("nullable", "null", None, "null"),
            ("plain", "ordinary text with spaces", "ordinary text with spaces", "ordinary text with spaces"),
            ("exponent", "1e3", "1e3", "1e3"),
            ("malformed", "[1,", "[1,", "[1,"),
        ]
        for key, raw, expected, displayed in cases:
            with self.subTest(key=key):
                self.plugin._dispatch("game", f"set {key}={raw}", "g1", "gm", False)
                attributes = self.plugin._group_snapshot("g1")["session"]["attributes"]
                self.assertEqual(attributes[key], expected)
                self.assertEqual(self.plugin._dispatch("game", f"set {key}", "g1", "gm", False), f"{key}={displayed}")

    def test_game_master_bootstrap_requires_admin_or_private_room(self):
        def install_hostless(group):
            group["session"] = {
                "id": "Hostless",
                "title": "Hostless",
                "game_master": None,
                "game_masters": [],
                "participants": [],
                "observers": [],
            }

        self.plugin._mutate_group("g-hostless", install_hostless)
        denied = self.plugin._dispatch("game", "master", "g-hostless", "member", False)
        self.assertIn("只有现任主持人", denied)
        session = self.plugin._group_snapshot("g-hostless")["session"]
        self.assertEqual(session["game_masters"], [])
        self.assertIsNone(session["game_master"])

        promoted = self.plugin._dispatch("game", "master", "g-hostless", "admin", True)
        self.assertIn("已加入团务主持人", promoted)
        session = self.plugin._group_snapshot("g-hostless")["session"]
        self.assertEqual(session["game_master"], "admin")
        self.assertEqual(session["game_masters"], ["admin"])

        self.plugin._mutate_group("private:hostless", install_hostless)
        private_promoted = self.plugin._dispatch("game", "master", "private:hostless", "owner", False, True)
        self.assertIn("已加入团务主持人", private_promoted)

    def test_observer_membership_controls_and_pregame_scope(self):
        event = FakeEvent(".ob join", sender="listener")
        listener_reply = self.collect(self.plugin, event)
        self.assertTrue(event.stopped)
        self.assertIn("已加入旁观者", listener_reply[0])
        self.assertIn("listener", self.plugin._group_snapshot("g1")["room_observers"])

        joined = self.plugin._dispatch("ob", "join", "g1", "observer", False)
        self.assertIn("已加入旁观者", joined)
        self.assertIn("observer", self.plugin._dispatch("ob", "list", "g1", "gm", False))
        duplicate = self.plugin._dispatch("ob", "join", "g1", "observer", False)
        self.assertIn("已在旁观者列表", duplicate)
        with self.assertRaises(ValueError):
            self.plugin._dispatch("ob", "join", "g1", "private-user", False, True)

        self.plugin._dispatch("game", "new Campaign", "g1", "gm", True)
        group = self.plugin._group_snapshot("g1")
        self.assertNotIn("room_observers", group)
        session = group["session"]
        self.assertEqual(session["observers"], [])

        denied = self.plugin._dispatch("ob", "off", "g1", "member", False)
        self.assertIn("只有群管理员", denied)
        self.assertIn("已关闭", self.plugin._dispatch("ob", "off", "g1", "admin", True))
        self.assertIn("已关闭旁观加入", self.plugin._dispatch("ob", "join", "g1", "observer", False))
        self.assertIn("已开启", self.plugin._dispatch("ob", "on", "g1", "admin", True))
        self.assertIn("已加入旁观者", self.plugin._dispatch("ob", "join", "g1", "observer", False))
        self.assertIn("observer", self.plugin._dispatch("ob", "list", "g1", "gm", False))
        self.assertIn("只有群管理员或本桌主持人", self.plugin._dispatch("ob", "clr", "g1", "member", False))
        self.assertIn("已清空", self.plugin._dispatch("ob", "clr", "g1", "gm", False))
        self.assertIn("不在旁观者列表", self.plugin._dispatch("ob", "exit", "g1", "observer", False))
        with self.assertRaises(ValueError):
            self.plugin._dispatch("game", "set no_ob=true", "g1", "gm", False)

    def test_game_call_sends_native_mention_components(self):
        self.plugin._dispatch("game", "new Campaign", "g1", "gm", True)
        self.plugin._dispatch("game", "join", "g1", "player1", False)
        self.plugin._dispatch("game", "join", "g1", "player2", False)

        replies = self.collect(self.plugin, FakeEvent(".game call", sender="gm"))
        self.assertEqual(len(replies), 1)
        chain = replies[0]
        self.assertIsInstance(chain[0], Plain)
        self.assertEqual(chain[0].text, "团务玩家：")
        mentions = [component for component in chain if isinstance(component, At)]
        self.assertEqual([component.qq for component in mentions], ["player1", "player2"])

    def test_game_roulette_drives_roll_check_and_sanity_and_roll_failure_rolls_back(self):
        self.plugin._dispatch("game", "new Roulette", "g1", "gm", True)
        self.assertIn("D2 轮盘", self.plugin._dispatch("game", "rou 2", "g1", "gm", False))
        self.plugin._dispatch("r", "D2", "g1", "outsider", False)
        self.assertEqual(self.plugin._group_snapshot("g1")["session"]["roulette"]["2"]["remaining"], 2)
        results = []
        for _ in range(2):
            response = self.plugin._dispatch("r", "D2", "g1", "gm", False)
            results.append(int(response.rsplit("=", 1)[1].strip()))
        self.assertEqual(set(results), {1, 2})
        self.assertEqual(self.plugin._group_snapshot("g1")["session"]["roulette"]["2"]["remaining"], 0)
        self.plugin._dispatch("r", "D2", "g1", "gm", False)
        self.assertEqual(self.plugin._group_snapshot("g1")["session"]["roulette"]["2"]["remaining"], 1)

        self.plugin._dispatch("game", "rou 1*2", "g1", "gm", False)
        self.plugin._dispatch("r", "D1", "g1", "gm", False)
        self.plugin._dispatch("r", "D1", "g1", "gm", False)
        self.assertEqual(self.plugin._group_snapshot("g1")["session"]["roulette"]["1"]["remaining"], 0)
        with self.assertRaises(DiceError):
            self.plugin._dispatch("r", "D1/0", "g1", "gm", False)
        self.assertEqual(self.plugin._group_snapshot("g1")["session"]["roulette"]["1"]["remaining"], 0)
        self.plugin._dispatch("r", "D1", "g1", "gm", False)
        self.assertEqual(self.plugin._group_snapshot("g1")["session"]["roulette"]["1"]["remaining"], 1)

        self.plugin._dispatch("game", "rou clr", "g1", "gm", False)
        self.plugin._dispatch("game", "rou 100", "g1", "gm", False)
        self.plugin._dispatch("ra", "50", "g1", "gm", False)
        self.assertEqual(self.plugin._group_snapshot("g1")["session"]["roulette"]["100"]["remaining"], 99)
        self.plugin.store.transact(lambda state: state["players"].__setitem__("gm", {"cards": {"Investigator": {"name": "Investigator", "attributes": {"SAN": 50}}}, "active_card": "Investigator"}))
        self.plugin._dispatch("sc", "1d100/1d100", "g1", "gm", False)
        self.assertEqual(self.plugin._group_snapshot("g1")["session"]["roulette"]["100"]["remaining"], 97)
        history = self.plugin._dispatch("game", "rou hist", "g1", "member", False)
        self.assertIn("D100=", history)
        self.assertIn("只有团务主持人", self.plugin._dispatch("game", "rou reset", "g1", "member", False))
        self.assertIn("已重置", self.plugin._dispatch("game", "rou reset", "g1", "gm", False))
        self.assertEqual(self.plugin._group_snapshot("g1")["session"]["roulette"]["100"]["remaining"], 100)
        with self.assertRaises(ValueError):
            self.plugin._dispatch("game", "rou 100*2", "g1", "gm", False)

    def test_game_roulette_threshold_and_keep_dice_match_upstream(self):
        self.plugin._dispatch("game", "new Threshold", "g1", "gm", True)
        self.plugin._dispatch("game", "rou 6", "g1", "gm", False)

        def remaining():
            return self.plugin._group_snapshot("g1")["session"]["roulette"]["6"]["remaining"]

        self.plugin._dispatch("r", "10d6", "g1", "gm", False)
        self.assertEqual(remaining(), 6)
        self.plugin._dispatch("r", "2d6k1", "g1", "gm", False)
        self.assertEqual(remaining(), 6)
        self.plugin._dispatch("r", "9d6", "g1", "gm", False)
        self.assertEqual(remaining(), 3)

    def test_minimum_response_cap_preserves_single_and_batch_roll_totals(self):
        plugin = Main(object(), {"max_response_length": 256})
        single = self.collect(plugin, FakeEvent(".r 125d6"))[0]
        self.assertLessEqual(len(single), 256)
        self.assertRegex(single, r"=\d+$")

        batch = self.collect(plugin, FakeEvent(".r 10#125d6"))[0]
        self.assertLessEqual(len(batch), 256)
        self.assertTrue(batch.endswith(" }"))
        results = batch.split("：{ ", 1)[1].split(" }", 1)[0].split(",")
        self.assertEqual(len(results), 10)

        counted = self.collect(plugin, FakeEvent(".r 1d6#125d6 bounded"))[0]
        self.assertLessEqual(len(counted), 256)
        self.assertIn("次数骰：", counted)
        self.assertIn("\n理由：bounded", counted)

    def test_percentile_roll_uses_session_roulette_for_base_only(self):
        self.plugin._dispatch("game", "new Roulette", "g1", "gm", True)
        self.plugin._dispatch("game", "rou 100", "g1", "gm", False)
        self.plugin._dispatch("game", "rou 10", "g1", "gm", False)
        self.plugin._dispatch("r", "B2", "g1", "gm", False)
        self.plugin._dispatch("r", "P2", "g1", "gm", False)
        roulette = self.plugin._group_snapshot("g1")["session"]["roulette"]
        self.assertEqual(roulette["100"]["remaining"], 98)
        self.assertEqual(roulette["10"]["remaining"], 10)

    def test_game_new_with_same_title_keeps_unique_ids(self):
        self.plugin._dispatch("game", "new Campaign", "g1", "gm", True)
        self.plugin._dispatch("game", "new Campaign", "g1", "gm", True)
        group = self.plugin._group_snapshot("g1")
        self.assertEqual(group["session"]["id"], "Campaign#2")
        self.assertIn("Campaign#1", group["sessions"])
        self.assertEqual(group["sessions"]["Campaign#1"]["title"], "Campaign")

    def test_game_tolerates_malformed_legacy_member_lists(self):
        legacy = {
            "id": "Broken",
            "title": "Broken",
            "game_master": "gm",
            "game_masters": None,
            "participants": None,
            "initiative": [],
        }
        self.plugin._mutate_group("g1", lambda group: group.__setitem__("session", legacy))

        denied = self.plugin._dispatch("game", "close", "g1", "member", False)
        self.assertIn("只有团务主持人或群管理员", denied)
        self.assertIsNotNone(self.plugin._group_snapshot("g1")["session"])

        closed = self.plugin._dispatch("game", "close", "g1", "gm", False)
        self.assertIn("已保存并关闭", closed)
        denied_open = self.plugin._game("open Broken", "g1", "member", False, True)
        self.assertIn("只有该团务主持人", denied_open)
        opened = self.plugin._game("open Broken", "g1", "gm", False, False)
        self.assertIn("已在当前聊天中开启", opened)
        self.assertEqual(self.plugin._group_snapshot("g1")["session"]["title"], "Broken")

    def test_new_game_archives_legacy_session_and_open_restores_initiative(self):
        legacy = {
            "title": "Legacy adventure",
            "started_at": "2024-01-01T00:00:00+08:00",
            "game_master": "gm",
            "participants": ["gm"],
            "initiative": [{"name": "Alice", "score": 15}],
        }
        self.plugin._mutate_group("g1", lambda group: group.__setitem__("session", legacy))
        self.plugin._dispatch("game", "new Current", "g1", "gm", True)
        group = self.plugin._group_snapshot("g1")
        self.assertIn("Legacy adventure", group["sessions"])
        self.assertEqual(group["sessions"]["Legacy adventure"]["initiative"], [{"name": "Alice", "score": 15}])
        self.plugin._dispatch("game", "close", "g1", "gm", False)
        self.plugin._dispatch("game", "open Legacy adventure", "g1", "gm", False)
        restored = self.plugin._group_snapshot("g1")["session"]
        self.assertEqual(restored["title"], "Legacy adventure")
        self.assertEqual(restored["initiative"], [{"name": "Alice", "score": 15}])

    def test_custom_reply_requires_admin_and_matches_exact_text(self):
        self.assertIn("只有群管理员", self.plugin._reply_command("add hello | world", "g1", False))
        self.plugin._reply_command("add hello | world", "g1", True)
        event = FakeEvent("hello", group="g1")
        self.assertEqual(self.collect(self.plugin, event), ["world"])
        self.assertTrue(event.stopped)
        near_miss = FakeEvent("hello!", group="g1")
        self.assertEqual(self.collect(self.plugin, near_miss), [])
        self.assertFalse(near_miss.stopped)

    def test_custom_reply_formatter_escapes_opening_brace_as_upstream(self):
        values = {"uid": "u1", "gid": "g1", "nick": "Alice"}
        self.assertEqual(Main._format_custom_reply_text(r"\{uid}", values), "{uid}")
        self.assertEqual(Main._format_custom_reply_text(r"\{uid} {gid}", values), "{uid} g1")
        self.assertEqual(Main._format_custom_reply_text(r"\{uid", values), r"\{uid")
        self.assertEqual(Main._format_custom_reply_text("{uid}", values), "u1")
        self.assertEqual(Main._format_custom_reply_text("{gender}", {"gender": "女性"}), "女性")
        self.assertEqual(Main._format_custom_reply_text("{gender}", {}), "{gender}")
        self.assertIn("{gender} 仅读取本地导入的字符串", self.plugin._help())
        self.assertEqual(Main._format_custom_reply_text("{suffix}", {"suffix": "x" * 600}), "x" * 500)
        self.assertEqual(Main._format_custom_reply_text("{fromMsg}", {"fromMsg": "m" * 600}), "m" * 500)
        self.assertEqual(Main._format_custom_reply_text("$&", {"_regex_full_match": "r" * 600}), "r" * 500)

    def test_reply_gender_uses_only_scoped_persisted_player_value(self):
        def scoped_event(message, sender):
            event = FakeEvent(message, sender=sender, group="g1")
            event.get_platform_name = lambda: "qq"
            event.get_platform_id = lambda: "adapter-1"
            event.get_self_id = lambda: "bot-1"
            return event

        stored_event = scoped_event("gender-context", "u4")
        group_id = self.plugin._group_id(stored_event)
        user_id = self.plugin._player_id(stored_event)
        self.plugin._reply_command(
            "set Title:Gender context\tMatch:gender-context\tText:{gender}", group_id, True
        )
        self.plugin.store.transact(lambda state: state["players"].__setitem__(user_id, {"gender": "女性"}))
        self.assertEqual(self.collect(self.plugin, stored_event), ["女性"])

        missing_event = scoped_event("gender-context", "u5")
        self.assertEqual(self.collect(self.plugin, missing_event), ["{gender}"])

        malformed_event = scoped_event("gender-context", "u6")
        malformed_user_id = self.plugin._player_id(malformed_event)
        self.plugin.store.transact(lambda state: state["players"].__setitem__(malformed_user_id, {"gender": {"unsafe": True}}))
        self.assertEqual(self.collect(self.plugin, malformed_event), ["{gender}"])

    def test_reply_text_formatter_exposes_upstream_id_aliases(self):
        self.plugin._reply_command(
            "set Title:Group ID aliases\tMatch:group-ids\tText:{fromQQ}|{fromUser}|{fromGroup}", "g1", True
        )
        group_event = FakeEvent("group-ids", sender="u1", group="g1")
        self.assertEqual(self.collect(self.plugin, group_event), ["u1|u1|g1"])

        private_event = FakeEvent("private-ids", sender="u2", group="", private=True)
        private_scope = self.plugin._group_id(private_event)
        self.plugin._reply_command(
            "set Title:Private ID aliases\tMatch:private-ids\tText:{fromQQ}|{fromUser}|{fromGroup}", private_scope, True
        )
        self.assertEqual(self.collect(self.plugin, private_event), ["u2|u2|"])

        self.plugin._reply_command(
            "set Title:Original message\tMatch:exact message\tText:Message:{fromMsg}", "g1", True
        )
        original_event = FakeEvent("Exact Message", sender="u3", group="g1")
        self.assertEqual(self.collect(self.plugin, original_event), ["Message:Exact Message"])
        self.assertIn("{fromUser}/{fromQQ}/{fromGroup}/{fromMsg}", self.plugin._help())

    def test_reply_text_formats_scoped_active_card_name(self):
        self.plugin._reply_command("set Title:Active card\tMatch:pc-context\tText:{pc}", "g1", True)
        self.plugin._reply_command("set Title:Active card\tMatch:pc-context\tText:{pc}", "g2", True)

        group_one = FakeEvent("pc-context", sender="u1", group="g1")
        group_two = FakeEvent("pc-context", sender="u1", group="g2")
        user_scope = self.plugin._player_id(group_one)
        group_one_scope = self.plugin._group_id(group_one)
        group_two_scope = self.plugin._group_id(group_two)

        def add_cards(state):
            state["players"][user_scope] = {
                "cards": {"Investigator": {"name": "Investigator"}, "Detective": {"name": "Detective"}},
                "active_card": "Investigator",
                "legacy_active_card": "Investigator",
                "active_cards_by_group": {group_one_scope: "Investigator", group_two_scope: "Detective"},
            }

        self.plugin.store.transact(add_cards)
        self.assertEqual(self.collect(self.plugin, group_one), ["Investigator"])
        self.assertEqual(self.collect(self.plugin, group_two), ["Detective"])

        no_card = FakeEvent("pc-context", sender="u2", group="g1")
        self.assertEqual(self.collect(self.plugin, no_card), ["u2"])

        default_card = FakeEvent("pc-context", sender="u3", group="g1")
        default_scope = self.plugin._player_id(default_card)
        default_group_scope = self.plugin._group_id(default_card)
        self.plugin.store.transact(lambda state: state["players"].__setitem__(
            default_scope,
            {"cards": {"角色卡": {"name": "角色卡"}}, "active_cards_by_group": {default_group_scope: "角色卡"}},
        ))
        self.assertEqual(self.collect(self.plugin, default_card), ["u3"])

    def test_reply_regex_text_formats_numbered_capture_groups(self):
        self.plugin._reply_command(
            "set Title:Numbered captures\tRegex:^say ([a-z]+) ([a-z]+)$\tText:first=$1; second=$2; all=$&; invalid=$9",
            "g1", True,
        )
        event = FakeEvent("say Hello World", group="g1")
        self.assertEqual(
            self.collect(self.plugin, event),
            ["first=Hello; second=World; all=say Hello World; invalid=$9"],
        )

        self.plugin._reply_command(
            "set Title:Unmatched captures\tRegex:^maybe( ([a-z]+))?$\tText:one=<$1>; two=<$2>",
            "g1", True,
        )
        unmatched = FakeEvent("maybe", group="g1")
        self.assertEqual(self.collect(self.plugin, unmatched), ["one=<>; two=<>"])

        ten_groups = "^" + "(.)" * 10 + "$"
        self.plugin._reply_command(
            f"set Title:Group ten\tRegex:{ten_groups}\tText:$10/$1", "g1", True,
        )
        tenth = FakeEvent("abcdefghij", group="g1")
        self.assertEqual(self.collect(self.plugin, tenth), ["j/a"])

    def test_reply_regex_formatter_supports_safe_match_tokens_only(self):
        self.plugin._reply_command(
            "set Title:Regex format tokens\tRegex:^say .*$\tText:Whole=$&; dollar=$$; before=$`; after=$'",
            "g1", True,
        )
        event = FakeEvent("say Hello", group="g1")
        self.assertEqual(self.collect(self.plugin, event), ["Whole=say Hello; dollar=$; before=; after="])
        self.assertIn("Regex Text 支持 $& 全匹配、$$ 字面美元与 $1–$99 编号捕获组", self.plugin._help())

        self.plugin._reply_command(
            "set Title:Literal dollar tokens\tMatch:dollar tokens\tText:$&/$$/$1", "g1", True
        )
        literal = FakeEvent("dollar tokens", group="g1")
        self.assertEqual(self.collect(self.plugin, literal), ["$&/$$/$1"])

    def test_reply_prefix_formatter_exposes_only_bounded_original_suffix(self):
        self.plugin._reply_command(
            "set Title:Prefix suffix\tPrefix:hello\tText:Tail:{suffix}", "g1", True
        )
        event = FakeEvent("HELLO there", group="g1")
        self.assertEqual(self.collect(self.plugin, event), ["Tail: there"])

        self.plugin._reply_command(
            "set Title:Exact no suffix\tMatch:plain\tText:{suffix}", "g1", True
        )
        exact = FakeEvent("plain", group="g1")
        self.assertEqual(self.collect(self.plugin, exact), ["{suffix}"])

        self.assertEqual(Main._casefold_prefix_end("STRASSE rest", "strasse"), 7)

    def test_reply_set_title_aliases_limits_lookup_listing_and_delete(self):
        result = self.plugin._reply_command(
            "set Title:Greeting rules\tType:Reply\tMatch:hello|hi\tPrefix:welcome\tSearch:friend\tRegex:^greet.*\tText:Welcome",
            "g1", True,
        )
        self.assertIn("添加", result)
        group = self.plugin._group_snapshot("g1")
        expected_keys = {
            "custom_replies": {"hello", "hi"},
            "custom_reply_prefixes": {"welcome"},
            "custom_reply_searches": {"friend"},
            "custom_reply_regexes": {"^greet.*"},
        }
        for field, keys in expected_keys.items():
            self.assertEqual(set(group[field]), keys)
            for value in group[field].values():
                self.assertEqual(value["kind"], "reply_set")
                self.assertEqual(value["title"], "Greeting rules")
                self.assertEqual(value["response"], "Welcome")

        for message in ("hi", "welcome aboard", "my friend", "greet me"):
            with self.subTest(message=message):
                event = FakeEvent(message, group="g1")
                self.assertEqual(self.collect(self.plugin, event), ["Welcome"])
                self.assertTrue(event.stopped)

        listed = self.plugin._reply_command("list", "g1", True)
        self.assertEqual(listed.count("[标题规则] Greeting rules"), 1)
        shown_by_title = self.plugin._reply_command("show Greeting rules", "g1", True)
        self.assertIn("标题：Greeting rules", shown_by_title)
        self.assertIn("精确:hello|hi", shown_by_title)
        self.assertIn("正则（有界子集）:^greet.*", shown_by_title)
        self.assertIn("Welcome", self.plugin._reply_command("get welcome", "g1", True))
        self.assertIn("标题：Greeting rules", self.plugin._reply_command("show friend", "g1", True))
        with self.assertRaises(ValueError):
            self.plugin._reply_command("add Greeting rules | ambiguous", "g1", True)

        updated = self.plugin._reply_command("limit welcome | prob:100", "g1", True)
        self.assertIn("已设置", updated)
        group = self.plugin._group_snapshot("g1")
        all_entries = [value for field in expected_keys for value in group[field].values()]
        self.assertEqual(len(all_entries), 5)
        self.assertTrue(all(value["limits"] == {"prob": 100} for value in all_entries))
        self.assertIn("限制：", self.plugin._reply_command("get Greeting rules", "g1", True))

        update = self.plugin._reply_command(
            "set Title:Greeting rules\tMatch:hello|hey\tPrefix:welcome aboard\tText:Updated",
            "g1", True,
        )
        self.assertIn("更新", update)
        group = self.plugin._group_snapshot("g1")
        self.assertEqual(set(group["custom_replies"]), {"hello", "hey"})
        self.assertEqual(set(group["custom_reply_prefixes"]), {"welcome aboard"})
        self.assertEqual(group["custom_reply_searches"], {})
        self.assertEqual(group["custom_reply_regexes"], {})
        self.assertTrue(all(value["limits"] is None for field in expected_keys for value in group[field].values()))
        with self.assertRaises(ValueError):
            self.plugin._reply_command("add hello | legacy override", "g1", True)
        self.assertEqual(group["custom_replies"]["hello"]["response"], "Updated")

        removed = self.plugin._reply_command("del welcome aboard", "g1", True)
        self.assertIn("已删除", removed)
        self.assertEqual(self.plugin._group_snapshot("g1")["custom_replies"], {})
        self.assertEqual(self.plugin._group_snapshot("g1")["custom_reply_prefixes"], {})

        self.plugin._reply_command("set Title:Delete By Title\tMatch:one|two\tText:Gone", "g1", True)
        removed_by_title = self.plugin._reply_command("del Delete By Title", "g1", True)
        self.assertIn("已删除", removed_by_title)
        self.assertEqual(self.plugin._group_snapshot("g1")["custom_replies"], {})

    def test_reply_set_defaults_title_to_last_mode_field_keyword(self):
        added = self.plugin._reply_command(
            "set Match:hello|hi\tPrefix:welcome\tText:First", "g1", True
        )
        self.assertIn("添加", added)
        prefix_rule = self.plugin._group_snapshot("g1")["custom_reply_prefixes"]["welcome"]
        self.assertEqual(prefix_rule["title"], "welcome")
        self.assertEqual(self.plugin._reply_command("get welcome", "g1", True), "First")

        added = self.plugin._reply_command(
            "set Title:\tSearch:one|two\tMatch:last|alias\tText:Second", "g1", True
        )
        self.assertIn("添加", added)
        exact_rules = self.plugin._group_snapshot("g1")["custom_replies"]
        self.assertEqual(exact_rules["last"]["title"], "last|alias")
        self.assertEqual(exact_rules["alias"]["title"], "last|alias")
        self.assertEqual(self.plugin._reply_command("get last|alias", "g1", True), "Second")

    def test_reply_set_daily_limit_is_shared_across_trigger_aliases(self):
        self.plugin._reply_command(
            "set Title:Shared quota\tMatch:quota exact\tPrefix:quota prefix\tLimit:today:1\tText:Allowed once",
            "g1", True,
        )
        first = FakeEvent("quota exact", sender="u1", group="g1")
        second = FakeEvent("quota prefix more", sender="u1", group="g1")
        self.assertEqual(self.collect(self.plugin, first), ["Allowed once"])
        self.assertEqual(self.collect(self.plugin, second), [])
        usage = self.plugin.store.snapshot()["groups"]["g1"]["custom_reply_usage"]
        self.assertEqual(len(usage), 1)
        self.assertEqual(next(iter(usage.values()))["count"], 1)

    def test_reply_set_conflicting_update_is_atomic_and_empty_title_defaults(self):
        self.plugin._reply_command("add occupied | legacy", "g1", True)
        self.plugin._reply_command("prefix blocked | legacy", "g1", True)
        self.plugin._reply_command(
            "set Title:Original\tMatch:old\tPrefix:owned\tText:Before",
            "g1", True,
        )
        before = self.plugin._group_snapshot("g1")
        with self.assertRaises(ValueError):
            self.plugin._reply_command(
                "set Title:Original\tMatch:new|blocked\tText:After",
                "g1", True,
            )
        self.assertEqual(self.plugin._group_snapshot("g1"), before)
        for ambiguous_spec in (
            "set Title:owned\tMatch:other\tText:Collision",
            "set Title:Other\tMatch:Original\tText:Collision",
        ):
            with self.subTest(ambiguous_spec=ambiguous_spec):
                with self.assertRaises(ValueError):
                    self.plugin._reply_command(ambiguous_spec, "g1", True)
                self.assertEqual(self.plugin._group_snapshot("g1"), before)
        self.assertEqual(Main._parse_reply_set_spec("Title:\tMatch:hello\tText:Reply")[0], "hello")

    def test_coc7_matches_upstream_output_and_cocs_alias(self):
        values = [1, 2, 3, 1, 1, 1, 1, 2, 4, 5, 6, 2, 3, 4, 5, 6, 6, 6, 6, 3, 4, 2, 2, 2] + [1] * 24
        rng = SequenceRandom(values)
        self.plugin.engine._rng = rng
        result = self.plugin._dispatch("cocs", "1 trailing", "g1", "u1", False)
        self.assertEqual(
            result,
            "u1的调查员作成:" + chr(10)
            + "力量:30 体质:15 体型:45 敏捷:75 外貌:45 智力:85 意志:90 教育:65 幸运:30 共计:450/480",
        )
        self.assertEqual(rng.calls, 24)
        self.assertIn("cocs", self.plugin.command_names)
        self.assertIn("coc7s", self.plugin.command_names)
        alias_result = self.plugin._dispatch("coc7s", "not-a-count", "g1", "u1", False)
        self.assertTrue(alias_result.startswith("u1的调查员作成:"))
        self.assertEqual(len(alias_result.splitlines()), 2)
        self.assertEqual(rng.calls, 48)

    def test_coc7_upstream_count_tokens_and_response_preflight(self):
        rng = SequenceRandom()
        self.plugin.engine._rng = rng
        for invalid in ("0", "00", "01", "11", "100"):
            with self.subTest(count=invalid), self.assertRaises(ValueError):
                self.plugin._generate_stats_batch("coc7", invalid, "g1", "u1")
        self.assertEqual(rng.calls, 0)
        self.plugin.max_response_length = 256
        with self.assertRaises(ValueError):
            self.plugin._generate_stats_batch("coc7", "10", "g1", "u1")
        self.assertEqual(rng.calls, 0)

    def test_coc6_matches_upstream_output_and_coc6s_alias(self):
        values = [1, 1, 1, 2, 2, 2, 1, 1, 3, 3, 3, 1, 2, 3, 2, 2, 2, 3, 4, 1, 1, 1, 10]
        rng = SequenceRandom(values)
        self.plugin.engine._rng = rng
        result = self.plugin._dispatch("coc6s", "1 trailing", "g1", "u1", False)
        expected = "u1的调查员作成:" + chr(10) + "力量:3 体质:6 体型:8 敏捷:9 外貌:6 智力:10 意志:9 教育:6 共计:57 资产:10"
        self.assertEqual(result, expected)
        self.assertEqual(rng.calls, 23)

    def test_coc6_multirow_spacing_and_count_prefix(self):
        rng = SequenceRandom([1] * 46)
        self.plugin.engine._rng = rng
        result = self.plugin._dispatch("coc6s", "2 trailing", "g1", "u1", False)
        row = "力量:3   体质:3   体型:8   敏捷:3   外貌:3   智力:8   意志:3   教育:6   共计:37 资产:1"
        self.assertEqual(result, "u1的调查员作成:" + chr(10) + row + chr(10) + row)
        self.assertEqual(rng.calls, 46)

        rng = SequenceRandom([1] * 230)
        self.plugin.engine._rng = rng
        ten_groups = self.plugin._dispatch("coc6s", "10 trailing", "g1", "u1", False)
        self.assertEqual(len(ten_groups.splitlines()), 11)
        self.assertEqual(rng.calls, 230)

        rng = SequenceRandom([1] * 23)
        self.plugin.engine._rng = rng
        leading_zero = self.plugin._dispatch("coc6", "01 trailing", "g1", "u1", False)
        self.assertEqual(len(leading_zero.splitlines()), 2)
        self.assertEqual(rng.calls, 23)

        rng = SequenceRandom([1] * 23)
        self.plugin.engine._rng = rng
        default_count = self.plugin._dispatch("coc6", "no digits", "g1", "u1", False)
        self.assertEqual(len(default_count.splitlines()), 2)
        self.assertEqual(rng.calls, 23)

    def test_coc6_rejects_invalid_counts_and_preflights_response(self):
        rng = SequenceRandom()
        self.plugin.engine._rng = rng
        for invalid in ("0", "00", "11", "100"):
            with self.subTest(count=invalid), self.assertRaises(ValueError):
                self.plugin._generate_stats_batch("coc6", invalid, "g1", "u1")
        self.assertEqual(rng.calls, 0)

        self.plugin.max_response_length = 256
        with self.assertRaises(ValueError):
            self.plugin._generate_stats_batch("coc6", "10", "g1", "u1")
        self.assertEqual(rng.calls, 0)

    def test_cocs_alias_runs_through_message_listener(self):
        rng = SequenceRandom([1] * 24)
        self.plugin.engine._rng = rng
        event = FakeEvent(".cocs not-a-count", sender="u1", group="g1")
        replies = self.collect(self.plugin, event)
        self.assertEqual(len(replies), 1)
        self.assertTrue(replies[0].startswith("u1的调查员作成:"))
        self.assertTrue(event.stopped)
        self.assertEqual(rng.calls, 24)

    def test_coc_and_dnd_batch_limits_and_shapes(self):
        coc = self.plugin._generate_stats_batch("coc7", "2")
        self.assertEqual(len(coc.splitlines()), 3)
        self.assertIn("共计:", coc)
        rng = SequenceRandom([1, 2, 3, 4, 6, 6, 6, 1, 6, 5, 4, 3, 2, 2, 2, 2, 5, 5, 2, 1, 4, 3, 2, 1])
        self.plugin.engine._rng = rng
        dnd = self.plugin._dnd("", "g1", "u1")
        self.assertEqual(
            dnd,
            "为u1的冒险者生成随机六维（降序）:"
            + chr(10)
            + "18 /15 /12 / 9 / 9 / 6 /总计69",
        )
        self.assertEqual(rng.calls, 24)
        batch = self.plugin._dnd("2", "g1", "u1")
        rows = batch.splitlines()
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[1].count(" /"), 6)
        self.assertEqual(rows[2].count(" /"), 6)
        self.assertEqual(rng.calls, 72)

    def test_dnd_matches_upstream_count_token_rules(self):
        self.assertEqual(len(self.plugin._dnd("2 trailing", "g1", "u1").splitlines()), 3)
        self.assertEqual(len(self.plugin._dnd("not-a-count", "g1", "u1").splitlines()), 2)
        self.assertEqual(len(self.plugin._dnd("10", "g1", "u1").splitlines()), 11)
        for invalid in ("0", "00", "01", "11", "100"):
            with self.subTest(count=invalid), self.assertRaises(ValueError):
                self.plugin._dnd(invalid, "g1", "u1")

    def test_dnd_preflights_response_cap_before_rng(self):
        rng = SequenceRandom()
        self.plugin.engine._rng = rng
        self.plugin.max_response_length = 256
        before = self.plugin._group_snapshot("g1")
        with self.assertRaises(ValueError):
            self.plugin._dnd("10", "g1", "u1")
        self.assertEqual(rng.calls, 0)
        self.assertEqual(self.plugin._group_snapshot("g1"), before)

    def test_private_data_is_not_shared_with_group(self):
        self.plugin._mutate_group("private:u1", lambda group: group.__setitem__("rule", 6))
        self.assertIsNone(self.plugin._group_snapshot("g1")["rule"])
        self.assertEqual(self.plugin._effective_coc_rule("g1"), 0)
        self.assertEqual(self.plugin._group_snapshot("private:u1")["rule"], 6)


if __name__ == "__main__":
    unittest.main()
