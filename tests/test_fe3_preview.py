"""Regressions for native events, textures and battle scene playback."""

from pathlib import Path
import sys
import unittest

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
from workspace_paths import workspace_root

from viewer import open_game
from fe3_scene import preview_scene_address, scene_address


BASE = workspace_root(APP) / '01_游戏镜像/格斗进化3/奥特曼格斗进化3.iso'


@unittest.skipUnless(BASE.is_file(), '需要本地格斗进化3镜像')
class NativePreview(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.game = open_game(BASE)
        cls.work = cls.game.workbench

    def animation_cuts(self, role, skill):
        scene = self.work.battle_preview(role, 0, 'skill', skill, 0)
        return scene, [(clip['frame'], clip['animation']['id']) for clip in scene['actors'][0]]

    def test_attack_event_parameter_offsets_and_expiry(self):
        motion = self.work.animation(0, 0, 49)
        attack = next(event for event in motion['events'] if event['op'] == 0)
        self.assertEqual(motion['bom'], (1, 8))
        self.assertEqual((attack['frame'], attack['until'], attack['selector']), (31, 42, 7))
        self.assertEqual(attack['args'], (24, 11, 7, 0, 13, 50, 3, 15, 20, 11))
        self.assertEqual([(item['offset'], item['name']) for item in attack['params'][3:9]], [
            (0x0A, '眩晕累积基值'), (0x0C, '命中反应类型'), (0x0E, '受击硬直帧'),
            (0x10, '保留字段（原版）'), (0x12, '防御硬直帧'), (0x14, '受击推移量'),
        ])
        self.assertFalse(attack['params'][6]['editable'])

    def test_guard_release_event_fields(self):
        events = self.work.animation(0, 0, 49)['events']
        paired = [event for event in events if event['op'] == 0x0B]
        self.assertEqual([(event['frame'], event['args']) for event in paired], [(0,(0,0)), (42,(1,0))])
        permit, tail = paired[1]['params']
        self.assertEqual((permit['name'],permit['offset'],permit['editable']), ('对手防御解除许可',4,True))
        self.assertEqual((tail['name'],tail['offset'],tail['editable']), ('记录尾部填充',6,False))

    def test_role_displacement_is_a_single_float(self):
        record = next(event for event in self.game.events(1, 8, 59) if event['op'] == 0x1C)
        self.assertEqual([(item['offset'],item['type'],item['value'],item['editable']) for item in record['params']],
                         [(4,'f',40.0,True),(8,'h',27,True),(10,'h',0,False)])

    def test_attack_parameters_without_shapes(self):
        for script, opcode, damage in ((69, 8, 6), (460, 10, 90)):
            with self.subTest(script=script):
                record = next(event for event in self.work.animation(0, 0, script)['events'] if event['op'] == opcode)
                self.assertEqual(record['base_damage'], damage)
                self.assertIsNone(record['damage'])
                self.assertEqual(record['params'][0]['name'], '基础伤害')
                self.assertEqual(record['params'][0]['offset'], 4)
                self.assertEqual(record['params'][4]['name'], '保留字段（原版）')
                self.assertEqual(record['params'][6]['name'], '受击推移量')

    def test_extended_attack_and_combo_windows(self):
        events = self.work.animation(0, 0, 43)['events']
        attack = next(event for event in events if event['op'] == 24)
        self.assertEqual((attack['frame'], attack['until'], attack['damage']), (23, 35, 5))
        self.assertEqual([(item['offset'], item['value'], item['name']) for item in attack['params'][9:14]], [
            (0x16, 7, '变体基础伤害'), (0x18, 60, '变体眩晕累积基值'),
            (0x1A, 0, '变体命中反应类型'), (0x1C, 60, '变体受击硬直帧'),
            (0x1E, 15, '变体受击推移量'),
        ])
        events = self.work.animation(0, 0, 30)['events']
        window = next(event for event in events if event['op'] == 5)
        self.assertEqual((window['frame'], window['until']), (28, 1027))

    def test_logo_and_native_texture_groups(self):
        game = self.game
        shared = game.shared_resources
        logo = (2661, -1)
        self.assertEqual(game.part(*logo).name, 'rogo')
        self.assertEqual(shared.details(logo)['name'], 'rogo')
        self.assertEqual(shared.export(logo, 'native', 0), ('.pom', game.original(logo)))
        self.assertEqual(shared.row(self.work.tables.skill_loading_source(0))['name'], '初代 · 加载页技能名称')
        self.assertEqual(shared.row((0x0C13, -1))['name'], '解锁通知图块 000')

    def test_ace_killer_continues_after_first_animation(self):
        self.assertIsNone(scene_address(self.work, 10, 331))
        self.assertEqual(preview_scene_address(self.work, 10, 331), 0x40D0)
        scene, cuts = self.animation_cuts(10, 1)
        self.assertEqual(cuts, [(0, 400), (240, 401), (352, 402), (496, 0)])
        self.assertGreaterEqual(scene['last'], 496)
        self.assertEqual({cue['id'] for cue in scene['movies'] if cue['id'] is not None}, {54})
        self.assertIn(399, [cut['frame'] for cut in scene['cameras']])
        self.assertIn(412, [clip['frame'] for clip in scene['actors'][1]])

    def test_other_native_scene_entry_and_clip_patterns(self):
        cases = ((1, 6, (176, 459)), (31, 0, (256, 386)), (36, 0, (71, 434)))
        for role, skill, expected in cases:
            with self.subTest(role=role, skill=skill):
                scene, cuts = self.animation_cuts(role, skill)
                self.assertIsNotNone(scene['source'])
                self.assertIn(expected, cuts)


if __name__ == '__main__':
    unittest.main()
