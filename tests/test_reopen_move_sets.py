"""Exercise a second editing session on a PSP ISO, including pre-recovery exports."""

import hashlib
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
from viewer import ViewerApi, open_game
from ogeditor.readers import ROOT
from ogeditor.psp.asset_transfer import texture_png
from kgf_readers.pom_to_png import build_png_rgba
from kgf_readers.pom_to_tm2 import decode_png
from move_eboot import BASE, Eboot
from ogeditor.psp.move_resume import stock_layouts
from ogeditor.psp.move_sets import Compilation, donor_status
from project_file import parse_project, read_manifest, write_project


STOCK = ROOT / 'KGF原创剧情优化_20261105/KGF原创剧情修复版 v7.iso'


def digest(path):
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').digest()


def legacy_export(game, path):
    # Previous releases recorded expanded states, private textures and palette
    # masks but did not write the root list, base layouts or reusable padding.
    finish = Eboot.finish
    def old_finish(writer, moves, **kw):
        optional = kw.get('optional_metadata') or {}
        kw['optional_metadata'] = {k:v for k,v in optional.items() if k == 'texture_palettes'}
        return finish(writer, moves, **kw)
    with patch.object(Eboot, 'finish', old_finish):
        game.export(path)


def hit(game, role, state):
    return next(row for row in game.motion(role,0,0,state)['events'] if row.get('damage') is not None)


@unittest.skipUnless(STOCK.is_file(), '需要本地奥0测试镜像')
class ReopenMoveSet(unittest.TestCase):
    def test_screenshot_move_reopen_undo_project_and_complete_restore(self):
        base = open_game(STOCK)
        base.edit_move_set(0,158,1,158)
        with tempfile.TemporaryDirectory() as folder:
            work = Path(folder)
            source = work/'original-mix.iso'
            legacy_export(base,source)
            fingerprint = digest(source)
            api = ViewerApi(session_path=work/'session.json')
            serial = api._install(source)['id']
            game = api._game
            prior = game.snapshot()
            choices = api.read_move_sets(serial,0,1)['value']
            self.assertEqual(next(row for row in choices['sources'] if row['id']==101)['reason'], '')
            self.assertIn('error', api.edit_move_set(serial,0,101,1,158))
            self.assertEqual(game.snapshot(),prior)
            self.assertIn('value', api.edit_move_set(serial,0,101,1,101))
            after = game.snapshot()
            self.assertEqual(game.move_sets,{(0,158):(1,158), (0,101):(1,101)})
            game.step_history()
            self.assertEqual(game.snapshot(),prior)
            game.step_history(True)
            self.assertEqual(game.snapshot(),after)

            project = work/'reopened.kgfp'
            write_project(game,project,{'role':0, 'tab':'mix'})
            loaded, view = api._read_project(project,[])
            self.assertEqual((loaded.snapshot(),view),(after,{'role':0,'tab':'mix'}))
            edited = work/'second.iso'
            loaded.export(edited)
            mounted = open_game(edited)
            self.assertEqual(mounted.installed_moves[0,101][:2], (1,101))
            private = mounted.action_image
            def record(state):
                at = int(mounted.native_actions[0][state]['va'],16) - BASE
                return struct.unpack_from('<2h',private,at+12)
            successor, _ = record(101)
            self.assertEqual(mounted.installed_moves[0,successor][:2],(1,104))
            self.assertEqual([record(state)[0] for state in (102,103,104)], [104,104,0])
            mounted.edit_move_set(0,101,2,102)
            mounted.edit_move_set(0,158)
            mounted.edit_move_set(0,101)
            self.assertFalse(mounted.move_sets)
            final = work/'restored.iso'
            self.assertFalse(mounted.export(final)['edits']['unsaved'])
            self.assertEqual(digest(final),digest(STOCK))
            self.assertEqual(digest(source), fingerprint)

    def test_independent_damage_and_palette_survive_renumbering(self):
        game = open_game(STOCK)
        game.edit_move_set(0,158,1,158)
        game.edit_move_set(1,158,2,158)
        with tempfile.TemporaryDirectory() as folder:
            work = Path(folder)
            old = work/'old.iso'
            legacy_export(game, old)
            current = open_game(old)
            event = hit(current,0,158)
            current.motion(0,0,0,158,(event['id'],{'4':93}))
            key = (1,158,360)
            physical = current.texture_copy(current.roles[1]['cases'][0],key)
            rgba = decode_png(texture_png(current.move_texture_data(physical), 3))
            pixels = bytearray(rgba.rgba)
            pixels[0::4] = bytes(x ^ 255 for x in pixels[0::4])
            image = work/'palette.png'
            image.write_bytes(build_png_rgba(rgba.width,rgba.height,pixels))
            current.edit_private_texture(physical,3,image,True)
            expected = {bank:current.move_texture_data((*key,bank)) for bank in current.installed_move_textures[key]}
            painted = work/'painted.iso'
            current.export(painted)
            loaded = open_game(painted)
            self.assertEqual(hit(loaded,0,158)['damage'],93)
            self.assertEqual(loaded.move_texture_data(physical),expected[physical[3]])
            # This earlier target renumbers the old move's ANM and skill effects.
            loaded.edit_move_set(0,101,1,101)
            self.assertEqual(hit(loaded,0,158)['damage'],93)
            loaded.edit_move_set(0,101,2,102)
            result = work/'result.iso'
            loaded.export(result)
            again = open_game(result)
            self.assertEqual(hit(again,0,158)['damage'],93)
            self.assertEqual(again.installed_moves[0,101][:2],(2,102))
            self.assertEqual({b:again.move_texture_data((*key,b)) for b in expected},expected)
            event = hit(again,0,158)
            again.motion(0,0,0,158,(event['id'],{'4':94}))
            again.edit_move_set(3,145,0,145)
            self.assertEqual(hit(again,0,158)['damage'],94)
            patch_path = work/'changes.kgfpatch'
            write_project(again,patch_path,{},portable=True)
            revived = open_game(result)
            parse_project(revived,patch_path,read_manifest(patch_path))
            self.assertEqual(revived.snapshot(),again.snapshot())

    def test_weapon_and_already_patched_native_image_survive_another_mix(self):
        paged = STOCK.with_name('KGF原创剧情修复版 v7_选人分页_联机形态修复.iso')
        if not paged.is_file():
            self.skipTest('需要选人分页与联机形态修复版')
        base = open_game(paged)
        base.edit_energy(0,[0,111,222,333])
        base.edit_move_set(0,160,5,160)
        with tempfile.TemporaryDirectory() as folder:
            work = Path(folder)
            old = work/'nunchaku.iso'
            legacy_export(base, old)
            game = open_game(old)
            game.edit_move_set(1,158,2,158)
            second = work/'second.iso'
            game.export(second)
            base.edit_move_set(1,158,2,158)
            reference = work/'direct.iso'
            base.export(reference)
            self.assertEqual(digest(second),digest(reference))
            game = open_game(second)
            self.assertEqual(game.actor_attributes(0)['energy'],[0,111,222,333])
            game.edit_move_set(0,160)
            game.export(work/'restored.iso')
            mounted = open_game(work/'restored.iso')
            role = mounted.roles[0]
            for case in role['cases']:
                for form in case['forms']:
                    member = form['mdl']['id']
                    self.assertEqual(mounted.read_image_member(member,b'MDL\0'),base.read_image_member(member,b'MDL\0'))

    def test_recovery_uses_original_state_callbacks_for_all_fighters(self):
        game = open_game(STOCK)
        self.assertEqual(len(game.roles),17)
        for role in range(len(game.roles)):
            receiver = (role + 1) % len(game.roles)
            for state, successor in ((101,104),(102,104),(103,104),(115,116)):
                with self.subTest(role=role,state=state):
                    self.assertEqual(donor_status(game,role,state),'')
                    plan = Compilation(game,{})
                    plan.root = state
                    plan.plan_move(receiver,state,role,state)
                    root = plan.states[receiver,state]
                    self.assertEqual(plan.states[receiver,root.next_state].original,successor)
                    self.assertNotEqual(root.next_state,successor)
        # The bundled reference has only native directory data. No other ISO
        # is needed to recover frame data or textures from a mounted image.
        self.assertEqual(set(stock_layouts()['anm']),set(game.bank_owners))
        self.assertEqual(set(stock_layouts()['bom']),set(game.bom_models))
