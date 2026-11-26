"""Regression for independent ground-travel and energy regeneration edits."""
import sys
import tempfile
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(APP))
from ogeditor.paths import workspace_root
from ogeditor.psp.game import Game
from project_file import parse_project, read_manifest, write_project

SOURCE = workspace_root(APP) / "KGF原创剧情优化_20261105/KGF原创剧情修复版 v7.iso"


@unittest.skipUnless(SOURCE.is_file(), "需要奥0测试镜像")
class GroundTravelTests(unittest.TestCase):
    def test_move_set_and_old_project_keep_separate_fields(self):
        game = Game(SOURCE)
        game.edit_attributes(0,7,{"speed":1.5})
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)/"legacy.kgfp"
            write_project(game,project,{})
            manifest = read_manifest(project)
            del manifest["movement_rates"]
            restored = Game(SOURCE)
            parse_project(restored,project,manifest)
            row = restored.actor_attributes(0)["rows"][7]
            self.assertEqual((row["travel"],row["speed"]),(1,1.5))
            restored.edit_move_set(0,158,1,158)
            restored.edit_attributes(0,7,{"travel":2})
            restored.step_history()
            self.assertEqual(restored.actor_attributes(0)["rows"][7]["travel"],1)
            restored.step_history(True)
            image = Path(directory)/"mixed.iso"
            restored.export(image)
            mounted = Game(image)
            row = mounted.actor_attributes(0)["rows"][7]
            self.assertEqual((row["travel"],row["speed"]),(2,1.5))
            mounted.edit_move_set(0,101,1,101)
            mounted.edit_attributes(1,0,{"travel":0.5})
            image = Path(directory)/"second.iso"
            mounted.export(image)
            final = Game(image)
            self.assertEqual(final.movement_rates,{(0,7):2,(1,0):0.5})
            self.assertIn((0,158),final.installed_moves)
