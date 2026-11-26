"""Verify the two FE3 name atlases and the native description link together."""

import sys
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
from ogeditor.paths import workspace_root

from viewer import open_game
from ogeditor.ps2.names import atlas_rows
from ogeditor.ps2.native import RATING_GRADES, rating_grade, rating_value_for_grade
from ogeditor.ps2.skill_ui import LOADING_NAME_RECTANGLES, LOADING_RECT_POINTERS, description_links


BASE = workspace_root(APP) / '01_游戏镜像/格斗进化3/奥特曼格斗进化3.iso'


class FinisherRating(unittest.TestCase):
    def test_native_rank_boundaries(self):
        limits = ((201, 0x7FFFFFFF), (131, 200), (91, 130), (61, 90), (21, 60), (5, 20))
        for grade, (low, high) in zip(RATING_GRADES, limits):
            with self.subTest(grade=grade):
                self.assertEqual(rating_grade(low, 1), grade)
                self.assertEqual(rating_grade(high, 1), grade)
                self.assertNotEqual(rating_grade(low - 1, 1), grade)
                self.assertEqual(rating_grade(rating_value_for_grade(130, grade)), grade)
        self.assertEqual(rating_grade(130), 'S')
        self.assertEqual(rating_grade(130, 1), 'B')
        self.assertEqual(rating_grade(2), '—')


@unittest.skipUnless(BASE.is_file(), '需要本地格斗进化3镜像')
class FinisherPresentation(unittest.TestCase):
    def check_presentation(self, game, role, candidate):
        tables = game.workbench.tables
        donor = atlas_rows(game.original(tables.skill_name_source(1)))[2]
        for key in (tables.skill_name_source(role), tables.skill_loading_source(role)):
            before = atlas_rows(game.original(key))
            after = atlas_rows(game.data(key))
            self.assertEqual(after[candidate], donor)
            self.assertEqual(after[:candidate] + after[candidate + 1:], before[:candidate] + before[candidate + 1:])
        link = tables.skill_description_source(1, 2)
        self.assertEqual(description_links(tables.elf)[0][role, candidate], link)
        row = next(entry for entry in game.workbench.skills(role) if entry['id'] == candidate)
        self.assertEqual(row['descriptionSource'], (0xB84, *link))
        self.assertEqual(row['loadingNameSource'], tables.skill_loading_source(role))

    def test_new_candidate_and_undo(self):
        game = open_game(BASE)
        before = game.snapshot()
        game.add_finisher(0, 0, 1, 2)
        self.check_presentation(game, 0, 9)
        game.step_history()
        self.assertEqual(game.snapshot(), before)
        self.assertEqual(description_links(game.workbench.tables.elf)[0], {})
        game.step_history(True)
        self.check_presentation(game, 0, 9)

    def test_replacement_includes_the_donor_rating(self):
        game = open_game(BASE)
        tables = game.workbench.tables
        source, = tables.words(0x3C74C0 + 4 * (20 + 2), 1)
        target_rating = 0x3C74C0 + 4 * (18 * 20)
        self.assertNotEqual(tables.words(target_rating, 1), (source,))
        game.transfer_finisher(18, 0, 1, 2)
        self.check_presentation(game, 18, 0)
        self.assertEqual(game.workbench.tables.words(target_rating, 1), (source,))

    def test_edited_rating_belongs_to_the_added_candidate(self):
        game = open_game(BASE)
        game.add_finisher(0, 0, 1, 2)
        target = (0, 9)
        source = (1, 2)
        power_fields = game.current_skill_words(*target)
        self.assertEqual(game.skill_rating(*target)['grade'], 'S')
        game.edit_skill_rating(*target, 'D')
        row = next(entry for entry in game.workbench.skills(0) if entry['id'] == target[1])
        self.assertEqual(row['rating'], {'value':30, 'grade':'D', 'equippedAtOne':'D', 'edited':True})
        self.assertEqual(game.skill_rating(*source)['grade'], 'S')
        self.assertEqual(row['words'], power_fields)
        game.remove_finisher(*target)
        self.assertFalse(game.rating_edits)
        game.step_history()
        self.assertEqual(game.skill_rating(*target)['grade'], 'D')
        game.edit_skill_rating(*target, None)
        self.assertEqual(game.skill_rating(*target)['grade'], 'S')

    def test_fixed_finisher_uses_the_battle_default(self):
        game = open_game(BASE)
        workbench = game.workbench
        self.assertEqual(workbench.tables.skill_rows(23), ())
        self.assertEqual(workbench.tables.default_finishers(23), {0:(1, ('○',))})
        move, = workbench.skills(23)
        self.assertEqual((move['name'], move['action'], move['anm'], move['va']),
                         ('火花传说', 332, 439, 0x4931C0))
        self.assertEqual((move['rating']['value'], move['power']), (255, 200.0))
        self.assertEqual(move['loadingNameSource'], (0xB63, -1))
        self.assertIsNone(move['descriptionSource'])
        self.assertEqual(move['fixedButtons'], ('○',))
        self.assertFalse(move['menuCandidate'])
        self.assertFalse(move['configurable'])
        scene = workbench.battle_preview(23, 0, 'skill', 0, 0)
        self.assertEqual((scene['name'], len(scene['actors'])), ('火花传说', 2))
        self.assertTrue(scene['cameras'] and scene['movies'])
        for role in (37, 38):
            move, = workbench.skills(role)
            self.assertTrue(move['menuCandidate'])
            self.assertFalse(move['configurable'])
            self.assertEqual(set(move['fixedButtons']), set('△□○×'))

    def test_last_title_row_for_ace(self):
        game = open_game(BASE)
        game.add_finisher(3, 0, 1, 2)
        self.check_presentation(game, 3, 17)
        tables = game.workbench.tables
        addresses = {tables.words(site, 1)[0] for site in LOADING_RECT_POINTERS}
        self.assertEqual(len(addresses), 1)
        address, = addresses
        self.assertEqual(tables.words(address, 17 * 6), tables.words(LOADING_NAME_RECTANGLES, 17 * 6))
        self.assertEqual(tables.words(address + 17 * 24, 6), (0, 238, 256, 14, 0, 0))
