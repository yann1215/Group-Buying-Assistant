import unittest
from copy import deepcopy

import test_share_workflow as fixtures
from app.analysis.special_constants import SINGLE_PERSON_ROLES
from app.analysis.special_parser import parse_special_member_updates
from app.analysis.special_member import (
    SpecialMemberError, update_special_member_cache, enrich_special_members,
)
from app.core.intent_parser import parse_user_intent


class SpecialMemberOperationTests(unittest.TestCase):
    def member(self, role='章稿画师'):
        return {'角色': role, '昵称': '小王', '群昵称': '012 小王', '单号': '12', '参摊': True}

    def apply(self, text, members=None):
        return update_special_member_cache(
            members if members is not None else [self.member()], parse_special_member_updates(text))

    def test_all_single_roles_default_to_nickname(self):
        for role in SINGLE_PERSON_ROLES:
            for action in ('改为', '改成', '修改为', '修改成', '换成', '换为', '设为', '设置为'):
                with self.subTest(role=role, action=action):
                    result = self.apply(f'请把{role}{action}小李', [self.member(role)])[0]
                    self.assertEqual(result, {'角色': role, '昵称': '小李', '群昵称': '', '单号': '', '参摊': True})

    def test_long_role_and_optional_de(self):
        for text in ('章稿画师昵称改为小李', '章稿画师的昵称改为小李', '修改章稿画师昵称为小李', '修改章稿画师为小李'):
            self.assertEqual(self.apply(text)[0]['昵称'], '小李')

    def test_explicit_fields_and_numeric_nickname(self):
        for label, field, value in [('群名片', '群昵称', '013 小李'), ('订单号', '单号', '13'), ('昵称', '昵称', '13')]:
            result = self.apply(f'章稿画师{label}改为{value}')[0]
            self.assertEqual(result[field], value)
            self.assertTrue(all(not result[f] for f in ('昵称', '群昵称', '单号') if f != field))
        self.assertEqual(self.apply('章稿画师改为12')[0]['昵称'], '12')

    def test_multiple_fields(self):
        result = self.apply('章稿画师昵称改为小李，单号=13')[0]
        self.assertEqual((result['昵称'], result['单号'], result['群昵称']), ('小李', '13', ''))

    def test_clear_field_preserves_others(self):
        for text in ('删除章稿画师昵称', '把章稿画师的昵称删掉', '章稿画师昵称清空', '请帮我清空章稿画师昵称一下'):
            result = self.apply(text)[0]
            expected = self.member()
            expected['昵称'] = ''
            self.assertEqual(result, expected)

    def test_remove_and_last_field(self):
        for text in ('删除章稿画师', '把章稿画师去掉', '清空章稿画师信息', '重置章稿画师', '章稿画师删掉'):
            self.assertEqual(self.apply(text), [])
        self.assertEqual(self.apply('清空章稿画师昵称', [{'角色': '章稿画师', '昵称': '小王'}]), [])

    def test_errors_do_not_mutate_input(self):
        original = [self.member()]
        for text in ('章稿画师改为', '章稿画师单号改为0', '删除章稿画师手机号', '章稿画师手机号改为123'):
            snapshot = deepcopy(original)
            with self.assertRaises(SpecialMemberError, msg=text):
                self.apply(text, original)
            self.assertEqual(original, snapshot)
        with self.assertRaises(SpecialMemberError):
            self.apply('章稿画师改为小李', [])
        with self.assertRaises(SpecialMemberError):
            self.apply('章稿画师改为小李', original * 2)

    def test_selector_is_validation(self):
        self.assertEqual(self.apply('把章稿画师小王的昵称改为小李')[0]['昵称'], '小李')
        self.assertEqual(self.apply('把单号12的章稿画师的昵称改为小李')[0]['单号'], '')
        with self.assertRaises(SpecialMemberError):
            self.apply('把章稿画师别人昵称改为小李')

    def test_read_only_and_action_as_value(self):
        for text in ('不要删除章稿画师', '怎么删除章稿画师', '章稿画师能删除吗？', '章稿画师不要删除', '请帮我不要清空章稿画师'):
            self.assertEqual(parse_special_member_updates(text), [])
            self.assertNotEqual(parse_user_intent(text)['intent'], 'update_special_members')
        self.assertEqual(self.apply('章稿画师改为删除')[0]['昵称'], '删除')
        intent = parse_user_intent('章稿画师昵称改为不参摊')
        self.assertEqual(intent['intent'], 'update_special_members')
        self.assertEqual(intent['special_member_updates'][0]['昵称'], '不参摊')

    def test_existing_setting_and_multi_person_not_deleted(self):
        self.assertEqual(self.apply('章稿画师：昵称=小李')[0]['单号'], '12')
        with self.assertRaises(SpecialMemberError):
            self.apply('删除工具人', [self.member('工具人')])

    def test_enrichment_uses_new_identity(self):
        members = self.apply('章稿画师改为小李')
        result = enrich_special_members(members, [
            {'昵称': '小王', '群昵称': '012 小王'}, {'昵称': '小李', '群昵称': '013 小李'},
        ], [])
        self.assertEqual(result[0]['单号'], '13')
        self.assertEqual(result[0]['群昵称'], '013 小李')
        self.assertEqual(enrich_special_members(members, [], [])[0]['单号'], '')
        ambiguous = enrich_special_members(members, [
            {'昵称': '小李', '群昵称': '013 小李'}, {'昵称': '小李', '群昵称': '014 小李'},
        ], [])
        self.assertEqual(ambiguous[0]['单号'], '')

    def test_enrichment_by_order_number(self):
        members = self.apply('章稿画师单号改为13')
        result = enrich_special_members(members, [{'昵称': '小李', '群昵称': '013 小李'}], [])
        self.assertEqual(result[0]['昵称'], '小李')
        self.assertEqual(result[0]['群昵称'], '013 小李')


class SpecialMemberOperationWorkflowTests(unittest.TestCase):
    setUp = fixtures.ShareWorkflowTests.setUp
    member_result = fixtures.ShareWorkflowTests.member_result
    say = fixtures.ShareWorkflowTests.say

    def test_edit_invalidates_checks_and_results(self):
        self.ctx.member_checked = True
        self.ctx.member_check_result = {'ok': True}
        self.assertIn('其他身份参数已清空', self.say('车主改为甲'))
        self.assertEqual(self.ctx.special_members[0]['单号'], '')
        self.assertFalse(self.ctx.member_checked)
        self.assertIsNone(self.ctx.member_check_result)
        self.assertTrue(self.ctx.share_results_invalidated)
        self.assertIsNone(self.ctx.pending_participation)

    def test_remove_saves_and_previews_only_removed_member(self):
        self.ctx.special_members.append({'角色': '画师', '昵称': '其他人', '参摊': False})
        reply = self.say('删除车主')
        self.assertIn('已移除并保存', reply)
        self.assertIn('车主', reply)
        self.assertNotIn('其他人', reply)
        self.assertEqual(len(self.ctx.special_members), 1)
        self.assertEqual(self.ctx.special_members[0]['角色'], '画师')
        self.assertTrue(self.ctx.share_results_invalidated)
        self.assertIsNone(self.ctx.pending_participation)

    def test_field_clear_and_last_field_removal(self):
        self.assertIn('指定身份参数已清空', self.say('清空车主单号'))
        self.assertEqual(self.ctx.special_members[0]['昵称'], '车主')
        self.assertIsNone(self.ctx.pending_participation)
        self.assertIn('已移除', self.say('清空车主昵称'))
        self.assertEqual(self.ctx.special_members, [])

    def test_new_identity_command_replaces_participation_draft(self):
        self.say('商品商品不参摊')
        self.assertIsNotNone(self.ctx.pending_participation)
        self.assertIn('其他身份参数已清空', self.say('车主改为甲'))
        self.assertEqual(self.ctx.special_members[0]['昵称'], '甲')
        self.assertIsNone(self.ctx.pending_participation)

    def test_single_member_preview_excludes_same_role_peers(self):
        self.ctx.special_members.extend([
            {'角色': '工具人', '昵称': '小猫', '单号': '8', '参摊': False},
            {'角色': '工具人', '昵称': '小狗', '单号': '9', '参摊': False},
        ])
        reply = self.say('把工具人小猫的昵称改为小兔')
        self.assertIn('小兔', reply)
        self.assertNotIn('小狗', reply)
        self.assertNotIn('车主', reply)

    def test_creation_and_batch_preview(self):
        reply = self.say('画师 小李，供稿人 小张')
        self.assertIn('已保存', reply)
        self.assertIn('小李', reply)
        self.assertIn('小张', reply)
        self.assertNotIn('车主', reply)
        self.assertEqual(len(self.ctx.special_members), 3)
        self.assertIsNone(self.ctx.pending_participation)
        reply = self.say('画师改为小赵')
        self.assertIn('小赵', reply)
        self.assertNotIn('小张', reply)
        self.assertNotIn('车主', reply)

    def test_repeated_role_updates_preview_only_final_state(self):
        reply = self.say('画师 小李，画师 小张')
        self.assertNotIn('小李', reply)
        self.assertEqual(reply.count('昵称：小张'), 1)
