"""本人群昵称/昵称 CSV 导出回归：python -m scripts.verify_self_export_names。"""
import csv
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from integrations.wechatmsg_lite_client import _ensure_wechatmsg_lite_path

_ensure_wechatmsg_lite_path()
from exporter.exporter_csv import CSVExporter
from wxManager.model import Contact, Me


class SelfExportChecks(unittest.TestCase):
    def setUp(self):
        self.parent = Path(__file__).resolve().parent
        self.temporary = TemporaryDirectory(prefix="self-export-check-", dir=self.parent)
        self.root = Path(self.temporary.name)
        assert self.root.resolve().is_relative_to(self.parent)
        self.addCleanup(self.temporary.cleanup)
        me = Me()
        original = vars(me).copy()
        self.addCleanup(lambda: (vars(me).clear(), vars(me).update(original)))
        me.wxid = "wxid_self"
        me.name = me.nickname = me.remark = "本人微信昵称"

    def contact(self, wxid="wxid_self", nickname="本人微信昵称", group_name="本人群昵称"):
        contact = Contact(wxid=wxid, nickname=nickname, remark=group_name or nickname)
        contact.group_nickname = group_name
        contact.contact_remark = ""
        return contact

    def export(self, members, room="room@chatroom", fallback=None):
        database = Mock()
        database.get_chatroom_members.return_value = members
        database.get_contact_by_username.return_value = fallback
        message = SimpleNamespace(sender_id="wxid_self", server_id=1, is_sender=True,
                                  str_time="2026-10-09 12:00:00", type_name=lambda: "文本",
                                  to_text=lambda: "测试消息")
        messages = [message]
        if "wxid_other" in members:
            messages.append(SimpleNamespace(**{**vars(message), "sender_id": "wxid_other", "is_sender": False}))
        database.get_messages.return_value = messages
        exporter = CSVExporter(database, Contact(wxid=room, nickname=room, remark=room),
                               str(self.root), progress_callback=lambda _: None,
                               finish_callback=lambda _: None)
        exporter.export()
        with open(exporter.csv_path, encoding="utf-8-sig", newline="") as stream:
            return list(csv.DictReader(stream)), exporter

    def test_csv_keeps_both_self_names_and_other_members(self):
        members = {"wxid_self": self.contact(),
                   "wxid_other": self.contact("wxid_other", "其他微信昵称", "其他群昵称")}
        rows, exporter = self.export(members)
        self.assertEqual((rows[0]["群昵称"], rows[0]["昵称"], rows[0]["备注"]),
                         ("本人群昵称", "本人微信昵称", ""))
        self.assertEqual((rows[1]["群昵称"], rows[1]["昵称"]), ("其他群昵称", "其他微信昵称"))
        self.assertIsNot(exporter.group_contacts, members)
        self.assertIsNot(exporter.group_contacts["wxid_self"], members["wxid_self"])

    def test_missing_self_nickname_uses_loaded_personal_name(self):
        original = self.contact(nickname="")
        rows, _ = self.export({"wxid_self": original})
        self.assertEqual((rows[0]["群昵称"], rows[0]["昵称"]), ("本人群昵称", "本人微信昵称"))
        self.assertEqual(original.nickname, "")

    def test_missing_self_member_uses_contact_lookup(self):
        rows, _ = self.export({}, fallback=self.contact(group_name=""))
        self.assertEqual((rows[0]["群昵称"], rows[0]["昵称"]), ("", "本人微信昵称"))

    def test_no_group_nickname_stays_empty_and_groups_do_not_leak(self):
        for room, name in (("first@chatroom", "群一昵称"), ("second@chatroom", "群二昵称"),
                           ("third@chatroom", "")):
            rows, _ = self.export({"wxid_self": self.contact(group_name=name)}, room)
            self.assertEqual(rows[0]["群昵称"], name)
            self.assertEqual(rows[0]["昵称"], "本人微信昵称")
        self.assertFalse(getattr(Me(), "group_nickname", ""))

    def test_personal_json_load_synchronizes_name_fields(self):
        path = self.root / "info.json"
        path.write_text(json.dumps({"username": "wxid_self", "nickname": "加载后的昵称"}), encoding="utf-8")
        Me().load_from_json(path)
        self.assertEqual((Me().name, Me().nickname, Me().remark), ("加载后的昵称",) * 3)


if __name__ == "__main__":
    unittest.main()
