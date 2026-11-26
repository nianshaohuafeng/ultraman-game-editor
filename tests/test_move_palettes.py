"""使用本地 v7 镜像核对配招色表的目录、改色与另存读回。"""

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
from workspace_paths import workspace_root

from viewer import open_game
from asset_transfer import texture_png
from kgf_readers.pom_to_png import build_png_rgba
from kgf_readers.pom_to_tm2 import decode_png, get_pom_layout
from project_file import parse_project, read_manifest, write_project


BASE = workspace_root(APP) / 'KGF原创剧情优化_20261105/KGF原创剧情修复版 v7.iso'


@unittest.skipUnless(BASE.is_file(), '需要本地奥0 v7 镜像')
class MovePaletteRoundtrip(unittest.TestCase):
    def test_separate_skills_with_same_donor(self):
        with TemporaryDirectory() as folder:
            directory = Path(folder)
            game = open_game(BASE)
            game.edit_move_set(1, 158, 2, 158)
            game.edit_move_set(1, 160, 2, 158)
            source = directory / 'skills.iso'
            game.export(source)
            installed = open_game(source)
            expected = {(1, 158, 360): {2, 3}, (1, 160, 360): {2, 3}}
            for config, case in enumerate(installed.roles[1]['cases']):
                for shape in range(len(case['forms'])):
                    self.assertEqual(installed.private_texture_usage(1, config, shape), expected)
                    self.assertEqual(installed.legacy_texture_usage(1, config, shape), expected)
            copies = {(*key, bank): installed.move_texture_data((*key, bank), True)
                      for key, banks in installed.installed_move_textures.items() for bank in banks}
            assets = installed.model(1, 2, 1)['assets']
            slot = next(a['slot'] for a in assets if a.get('move_state') == 158 and a['member'] == 360)
            physical = installed.asset_binding(1, 2, 1, 'pom', slot)
            image = decode_png(texture_png(copies[physical], 3))
            rgba = bytearray(image.rgba)
            rgba[0::4] = bytes(channel ^ 255 for channel in rgba[0::4])
            png = directory / 'recolor.png'
            png.write_bytes(build_png_rgba(image.width, image.height, rgba))
            installed.edit_asset(1, 2, 1, 'pom', slot, 3, png, palette_only=True)
            self.assertEqual(installed.edit_status()['assets'], 1)
            edited = installed.snapshot()
            installed.step_history()
            self.assertFalse(installed.move_texture_edits)
            installed.step_history(redo=True)
            self.assertEqual(installed.snapshot(), edited)
            project = directory / 'colors.kgfp'
            write_project(installed, project, {})
            loaded = open_game(source)
            parse_project(loaded, project, read_manifest(project))
            self.assertEqual(loaded.snapshot(), edited)
            output = directory / 'edited.iso'
            loaded.export(output)
            result = open_game(output)
            self.assertEqual(result.eboot_raw, installed.eboot_raw)
            self.assertEqual(result.read_source_member(360, b'POM\0'), game.read_source_member(360, b'POM\0'))
            for key, old in copies.items():
                new = result.move_texture_data(key, True)
                if key[:3] != (1, 158, 360):
                    self.assertEqual(old, new)
                    continue
                layout = get_pom_layout(old)
                start = layout['palette_offset'] + 3 * layout['palette_stride']
                end = start + layout['palette_block_size']
                self.assertNotEqual(old[start:end], new[start:end])
                self.assertEqual((old[:start], old[end:], old[start+3:end:4]),
                                 (new[:start], new[end:], new[start+3:end:4]))


if __name__ == '__main__':
    unittest.main()
