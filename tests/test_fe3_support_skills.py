"""Verify the native barrier and shoulder settings use independent skill words."""

import sys
import tempfile
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
from workspace_paths import workspace_root

from viewer import open_game
from fe3_formats import Bom
from fe3_guard import installed_absorptions
from fe3_native import GUARD_NAMES, GUARD_TYPES, NativeTables, ROLE_COUNT, SHOULDER_ASSIST, SHOULDER_ASSIST_WORD, SHOULDER_SKILLS
from fe3_preview import clip_index
from fe3_scene import camera_bank, script_rows
from fe3_support import ASSIST_WORD, installed_supports, source_choices
from project_file import parse_project, read_manifest, write_project


BASE = workspace_root(APP) / '01_游戏镜像/格斗进化3/奥特曼格斗进化3.iso'


@unittest.skipUnless(BASE.is_file(), '需要本地格斗进化3镜像')
class SupportSkills(unittest.TestCase):
    def setUp(self):
        self.game = open_game(BASE)
        self.workbench = self.game.workbench

    def test_native_choices_across_the_roster(self):
        tables = self.workbench.tables
        counts = {0:0, 2:0}
        for role in range(ROLE_COUNT):
            for kind in counts:
                for entry in self.workbench.skills(role, kind):
                    with self.subTest(role=role, candidate=entry['id']):
                        counts[kind] += 1
                        values = {value for value, _, _ in entry['choices']}
                        self.assertIn(entry['action'], values)
                        self.assertIn(0, values)
                        if kind == 0:
                            self.assertEqual(values, GUARD_TYPES.keys())
                            self.assertEqual(entry['name'], GUARD_NAMES[role][entry['id']])
                        else:
                            self.assertEqual((entry['action'], entry['name']), SHOULDER_SKILLS[role][entry['id']])
                            self.assertEqual(SHOULDER_ASSIST in values, tables.has_shoulder_assist(role))
        self.assertEqual(counts, {0:25, 2:94})
        self.assertFalse(tables.has_shoulder_assist(23))
        self.assertFalse(tables.has_shoulder_assist(38))

    def test_auxiliary_options_respect_all_equippable_forms(self):
        # Cosmos can equip this candidate in two forms. Its other native
        # action has a different form mask, so copying it would only work in one.
        with self.assertRaisesRegex(ValueError, '原生效果'):
            self.game.edit_support_skill(21, 7, 339)
        with self.assertRaisesRegex(ValueError, '防御技或辅助技'):
            self.game.edit_support_skill(0, 1, 0)
        with self.assertRaisesRegex(ValueError, '原生效果'):
            self.game.edit_support_skill(0, 0, 4)
        self.assertFalse(self.game.skill_edits)

    def test_project_roundtrip_and_history(self):
        game = self.game
        guard = game.skill_record(0, 0)
        shoulder = game.skill_record(1, 8)
        game.edit_skill(1, 8, (shoulder.words[0], 0x20, shoulder.words[2]))
        game.edit_support_skill(0, 0, 0)
        game.edit_support_skill(1, 8, SHOULDER_ASSIST)
        self.assertEqual(game.current_skill_words(0, 0), (0, *guard.words[1:]))
        self.assertEqual(game.current_skill_words(1, 8), (SHOULDER_ASSIST_WORD, 0x20, 8))
        self.assertEqual(self.workbench.skills(1, 2)[0]['anm'], -1)
        game.step_history()
        self.assertEqual(game.current_skill_words(1, 8), (shoulder.words[0], 0x20, 8))
        game.step_history(True)
        game.edit_support_skill(18, 8, 347)
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / 'support.kgfp'
            view = {'fe3Tab':'other-skills', 'fe3SupportGroup':'shoulder', 'fe3SupportId':8}
            write_project(game, path, view)
            reopened = open_game(BASE)
            manifest = read_manifest(path)
            parse_project(reopened, path, manifest)
            self.assertEqual(reopened.skill_edits, game.skill_edits)
            self.assertEqual(manifest['view'], view)
            reopened.edit_skill(0, 0, None)
            self.assertEqual(reopened.current_skill_words(0, 0), guard.words)

    def test_borrowed_guard_copies_rule_and_preserves_candidate(self):
        game = self.game
        original = game.skill_record(0,0)
        game.transfer_support_skill(0,0,9,0)
        self.assertEqual(game.current_skill_words(0,0),(2,*original.words[1:]))
        self.assertEqual(installed_supports(game.transfer_plan.elf), {(0,0):(9,0)})
        game.step_history()
        self.assertEqual(game.current_skill_words(0,0),original.words)
        game.step_history(True)
        game.transfer_support_skill(0,0)
        self.assertEqual(game.current_skill_words(0,0),original.words)

    def test_shoulder_borrow_replaces_both_phases_and_loads_donor_audio(self):
        game = self.game
        work = self.workbench
        target_form = work.form(1,0)
        source_form = work.form(34,0)
        pairs = ((464,405),(465,406))
        slots_out,slots_in = work.binding(1),work.binding(34)
        sizes = {}
        for dst,src in pairs:
            key,index = slots_in[src]
            sizes[dst] = clip_index(game.original(key))[index].last_frame
        self.assertEqual(source_choices(game,1,9,34)[0]['reason'],'')
        self.assertTrue(source_choices(game,1,9,0)[0]['reason'])
        game.transfer_support_skill(1,9,34,1)
        for dst,_ in pairs:
            key,index = slots_out[dst]
            self.assertEqual(clip_index(game.data(key))[index].last_frame,sizes[dst])
        get_hits = lambda bom,anm: [e['damage'] for e in bom.events(anm) if e['damage'] is not None]
        self.assertEqual(get_hits(Bom(game.data(target_form.bom)),464),
                         get_hits(Bom(game.original(source_form.bom)),405))
        self.assertEqual(game.current_skill_words(1,9),game.skill_record(1,9).words)
        tables = NativeTables(game.transfer_plan.elf)
        desc,sizes = tables.descriptor(1)
        old,counts = NativeTables(game.elf).descriptor(1)
        self.assertEqual(tables.words(desc[18],sizes[11])[-1],tables.words(old[18],counts[11])[-1])
        self.assertGreater(sizes[11],counts[11])
        row = next(x for x in work.skills(1,2) if x['id']==9)
        self.assertEqual((row['name'],row['anm'],row['transfer']),('足球踢',464,(34,1)))

    def test_borrowed_aid_is_per_candidate_and_coexists_with_finishers(self):
        game = self.game
        game.transfer_support_skill(0,8,29,2)
        role = 0
        self.assertEqual(game.current_skill_words(role,8),(ASSIST_WORD+29,0,8))
        self.assertEqual(game.current_skill_words(role,7),game.skill_record(role,7).words)
        game.add_finisher(role,0,1,2)
        both = game.snapshot()
        self.assertEqual(installed_supports(game.transfer_plan.elf),{(0,8):(29,2)})
        self.assertEqual(game.transfer_plan.additions,{(0,9):(1,2,0)})
        game.step_history()
        self.assertEqual(installed_supports(game.transfer_plan.elf),{(0,8):(29,2)})
        game.step_history(True)
        self.assertEqual(game.snapshot(),both)

    def test_add_guard_to_role_without_existing_candidate(self):
        game = self.game
        role = 11
        tables = self.workbench.tables
        self.assertEqual(tables.skill_rows(role,0),())
        self.assertTrue(tables.skill_menu_enabled(role))
        self.assertEqual(source_choices(game,role,None,1,0,0),
                         [{'id':7,'name':'奥特屏障','reason':''}])
        game.add_support_skill(role,0,1,7)
        new, = self.workbench.skills(role,0)
        self.assertEqual((new['id'],new['action'],new['menuMask'],new['name'],new['added']),
                         (5,1,1,'奥特屏障',(1,7,0)))
        self.assertEqual(game.skill_record(role,5).words,(1,0,5))
        self.assertEqual(self.workbench.tables.default_finishers(role),tables.default_finishers(role))
        game.step_history()
        self.assertFalse(self.workbench.skills(role,0))
        game.step_history(True)
        game.remove_support_skill(role,5)
        self.assertFalse(self.workbench.skills(role,0))

    def test_added_absorptions_have_independent_animations_and_recovery_scenes(self):
        game = self.game
        role = 11
        stock = self.workbench.program(role)
        self.assertEqual(stock.actions()[101:103], ((-1,-1,1), (-1,-1,1)))
        self.assertTrue(source_choices(game, role, None, 9, 0, 0)[0]['scene'])
        for donor, candidate in ((9,0), (24,1), (26,1)):
            game.add_support_skill(role, 0, donor, candidate)
        program = self.workbench.program(role)
        fallback, routes = installed_absorptions(program)
        self.assertEqual(fallback, tuple(value for state in (101,102)
                                         for value in stock.at(stock.action_table+24*state, '<3I')))
        self.assertEqual(program.actions(), stock.actions())
        self.assertEqual(set(routes), {(5,0), (6,0), (7,0)})
        for candidate, film, tick, hp in ((5,51,140,30), (6,100,90,15), (7,95,100,20)):
            with self.subTest(candidate=candidate):
                route = routes[candidate,0]
                anim = next(row for row in self.workbench.skills(role,0) if row['id'] == candidate)
                self.assertEqual(anim['guardMotions'], {0:route.animations[0]})
                self.assertTrue(all(clip in self.workbench.binding(role) for clip in route.animations if clip >= 0))
                words = program.at(route.callbacks[4], '<28I', b'.text')
                high, low = next((left, right) for left, right in zip(words, words[1:])
                                 if left >> 16 == 0x3C05 and right >> 16 == 0x24A5)
                script = ((high & 0xFFFF) << 16) + (low & 0x7FFF) - (low & 0x8000)
                rows = script_rows(program,script)
                self.assertEqual([args[0] for _, op, args, _ in rows if op == 2], [film])
                self.assertEqual([(clock, args[:2]) for clock, op, args, _ in rows if op == 23], [(tick,[0,hp])])
                camera, = [args[0] for _, op, args, _ in rows if op == 10]
                self.assertIn((camera_bank(self.workbench, role), camera), game.transfer_plan.cameras)

    def test_interleaved_additions_share_candidate_numbering(self):
        game = self.game
        game.add_support_skill(0,0,1,7)
        game.add_finisher(0,0,1,2)
        game.add_support_skill(0,0,29,2)
        self.assertEqual([game.skill_record(0,c).kind for c in (9,10,11)],[0,1,2])
        self.assertEqual(installed_supports(game.transfer_plan.elf),{(0,9):(1,7),(0,11):(29,2)})
        game.remove_support_skill(0,11)
        self.assertEqual(game.skill_record(0,10).kind,1)
