"""密钥缓存与全局更新指令回归：python -m scripts.verify_key_cache。"""
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from integrations.wechatmsg_lite_client import _ensure_wechatmsg_lite_path
from app.core.key_commands import parse_key_update_command, redact_key_update_command
from app.core.chat_service import ChatService
from app.core.session_types import SINGLE_CAR, MERGED_SHIPPING, UNCLASSIFIED

_ensure_wechatmsg_lite_path()
from wxManager import decrypt_runner as runner
from wxManager.decrypt import decrypt_v4


class KeyCacheChecks(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        test_parent = Path(__file__).resolve().parent
        self.root = Path(self.stack.enter_context(TemporaryDirectory(prefix="key-check-", dir=test_parent)))
        assert self.root.resolve().is_relative_to(test_parent)
        (self.root / "db_storage").mkdir()
        self.old = {"db_version": 4, "wxid": "test", "source_dir": str(self.root),
                    "db_dir": "old-db", "key": "a" * 64}
        runner._save_cache(self.root, self.old)
        self.original_bytes = (self.root / "decrypt_cache.json").read_bytes()

    def unchanged(self):
        self.assertEqual((self.root / "decrypt_cache.json").read_bytes(), self.original_bytes)

    def update(self, key="", provider=None, valid=True):
        with patch.object(runner, "_resolve_v4_account_info", return_value={"wxid": "test", "source_dir": str(self.root)}), \
             patch.object(runner, "_find_validation_db", return_value=self.root / "source.db"), \
             patch.object(decrypt_v4, "validate_key_v4_detailed", return_value={
                 "ok": valid, "error_code": None if valid else "key_mismatch", "message": "validation"}):
            return runner.ensure_cached_decrypt_key(output_root=str(self.root), force_update=True,
                                                     initial_key=key, key_input_func=provider)

    def test_all_cached_decryption_failures_preserve_key_without_prompt(self):
        for code in ("key_mismatch", "validation_read_failed", "decrypt_failed", "output_replace_failed"):
            provider = Mock(side_effect=AssertionError("unexpected key prompt"))
            with patch.object(runner, "_dump_v4_with_key", return_value=runner._fail("failure", error_code=code)):
                result = runner.decrypt_wechat_database(output_root=str(self.root), force_decrypt=True,
                                                        key_input_func=provider)
            self.assertFalse(result["ok"])
            self.assertEqual(result["error_code"], code)
            provider.assert_not_called()
            self.unchanged()

    def test_valid_inline_key_replaces_only_after_validation(self):
        provider = Mock(side_effect=AssertionError("unexpected prompt"))
        self.assertTrue(self.update("B" * 64, provider)["ok"])
        cache = runner._load_cache(self.root)
        self.assertEqual(cache["key"], "b" * 64)
        self.assertNotIn("db_dir", cache)
        provider.assert_not_called()

    def test_missing_or_wrong_length_key_opens_input(self):
        for initial in ("", "short"):
            provider = Mock(return_value="b" * 64)
            self.assertTrue(self.update(initial, provider)["ok"])
            provider.assert_called_once()

    def test_invalid_inline_or_cancel_keeps_old_cache(self):
        for initial in ("b" * 64, "z" * 64, ""):
            provider = Mock(return_value="quit")
            self.assertFalse(self.update(initial, provider, valid=False)["ok"])
            self.unchanged()

    def test_failed_atomic_replace_keeps_original(self):
        with patch.object(runner.os, "replace", side_effect=PermissionError("busy")):
            self.assertFalse(self.update("b" * 64)["ok"])
        self.unchanged()
        self.assertEqual(list(self.root.glob(".decrypt_cache-*")), [])

    def test_validation_distinguishes_read_and_format_errors(self):
        result = decrypt_v4.validate_key_v4_detailed("a" * 64, str(self.root / "missing.db"))
        self.assertEqual(result["error_code"], "validation_read_failed")
        short = self.root / "short.db"
        short.write_bytes(b"short")
        self.assertEqual(decrypt_v4.validate_key_v4_detailed("a" * 64, str(short))["error_code"], "invalid_database")
        self.assertFalse(decrypt_v4.validate_key_v4("a" * 64, str(short)))

    def test_global_command_bypasses_business_and_model_and_redacts_history(self):
        for kind in (SINGLE_CAR, MERGED_SHIPPING, UNCLASSIFIED):
            service = ChatService.__new__(ChatService)
            service.tools = Mock()
            service.tools.get_context.return_value.session_type = kind
            service._ensure_context_loaded = Mock()
            service.instruction_normalizer = Mock()
            with patch("app.core.chat_service.add_message") as persist, \
                 patch("integrations.wechatmsg_lite_client.update_wechat_database_key",
                       return_value={"ok": True, "message": "updated"}) as update:
                self.assertEqual(service.send_message(1, "更新密钥：" + "b" * 64), "updated")
                update.assert_called_once_with("b" * 64, key_input_func=service.tools.key_input_func)
                self.assertEqual(persist.call_args_list[0].kwargs["content"], "更新key [已隐藏]")
                service.instruction_normalizer.normalize.assert_not_called()
                service.tools.get_context.assert_not_called()

    def test_command_parser_and_draft_redaction(self):
        for command in ("更新key", "更新KEY", "更新密钥", " 更新 key： "):
            self.assertEqual(parse_key_update_command(command), "")
        self.assertIsNone(parse_key_update_command("如何更新key？"))
        self.assertEqual(redact_key_update_command("更新key " + "b" * 64), "更新key [已隐藏]")
        service = ChatService.__new__(ChatService)
        with patch("app.core.chat_service.save_session_draft") as save:
            service.save_conversation_draft(1, "更新key " + "b" * 64)
            save.assert_called_once_with(1, "")


if __name__ == "__main__":
    unittest.main()
