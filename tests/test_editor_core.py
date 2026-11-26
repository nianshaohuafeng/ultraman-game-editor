"""Regression tests for image selection, lossless DFS and workspace history."""

from functools import lru_cache
from pathlib import Path
import random
import struct
import subprocess
import sys
import tempfile
from threading import Event, Thread
import unittest
from unittest.mock import Mock, patch

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
from workspace_paths import workspace_root
import viewer
from asset_transfer import clip_ids
from decrypt_enc import build_dfs_file, decode_dfs_enc as reference_decode
from fe3_dfs import decode_dfs_enc, unpacked_tag
from fe3_game import FE3Game, Transfers, encode_verified


class DfsTests(unittest.TestCase):
    def test_dictionary_wrap_overlap_and_last_token(self):
        rng = random.Random(33)
        payload = bytearray()
        total = 0
        for group in range(420):
            flags = rng.randrange(256) if group % 4 else 255
            payload.append(flags)
            for bit in range(8):
                if flags & (1 << bit):
                    payload.append(rng.randrange(256))
                    total += 1
                else:
                    distance = rng.choice((1, 2, 3, 17, 18, 4095, 4096))
                    slot = (0xFEE + total - distance) & 4095
                    length = rng.randrange(3, 19)
                    payload.extend((slot & 255, ((slot >> 4) & 240) | (length - 3)))
                    total += length
        for length in (1, 2, 3, 4, 10, 4095, 4096, 4097, total - 1, total):
            blob = build_dfs_file(bytes(length), payload)
            decoded = reference_decode(blob)
            self.assertEqual(decode_dfs_enc(blob), decoded)
            self.assertEqual(unpacked_tag(blob[:28]), decoded[:4])
        zeros = build_dfs_file(bytes(18), b'\0\xee\xff')
        self.assertEqual(decode_dfs_enc(zeros), bytes(18))
        self.assertEqual(unpacked_tag(zeros), bytes(4))
        self.assertEqual(decode_dfs_enc(build_dfs_file(b'', b'')), b'')
        self.assertEqual(unpacked_tag(b'MDL\0abcdef'), b'MDL\0')

    def test_truncated_streams_and_size_mismatches(self):
        for blob in (b'DFS', bytes(12), b'DFS\x01' + struct.pack('<II', 1, 13),
                     *(build_dfs_file(b'A' * 20, stream) for stream in
                       (b'', b'\xff', b'\xffAB', b'\0', b'\0\xee', b'\xffABCDEFGH'))):
            with self.subTest(blob=blob):
                with self.assertRaises(ValueError) as old:
                    reference_decode(blob)
                with self.assertRaises(ValueError) as new:
                    decode_dfs_enc(blob)
                self.assertEqual(str(new.exception), str(old.exception))

    def test_cache_keys_include_content_and_skip_unverified_results(self):
        game = FE3Game.__new__(FE3Game)
        game._encode_small = lru_cache(maxsize=8)(encode_verified)
        part = Mock(compressed=True)
        payload = bytearray(b'MDL\0abcabc')
        a = game._encoded(part, payload)
        payload[-1] = 90
        b = game._encoded(part, payload)
        self.assertNotEqual(a, b)
        self.assertEqual(reference_decode(b), bytes(payload))
        self.assertEqual(game._encoded(part, bytes(payload)), b)
        self.assertEqual(game._encode_small.cache_info().hits, 1)
        with patch('fe3_game.encode_dfs_enc', return_value=a):
            with self.assertRaises(ValueError):
                game._encoded(part, b'other bytes')
        self.assertEqual(game._encode_small.cache_info().currsize, 2)
        with patch('fe3_game.encode_verified', return_value=b'large') as direct:
            large = bytes(512 * 1024 + 1)
            self.assertEqual(game._encoded(part, large), b'large')
            direct.assert_called_once_with(large)


class WorkspaceTests(unittest.TestCase):
    def test_native_readers_load_without_a_window_or_document(self):
        code = (
            "import sys; sys.path.insert(0, sys.argv[1]); "
            "import move_sets, move_sound, formats, editor_readers; "
            "from kgf_readers.mdl_reader import read_mdl; "
            "assert move_sets.read_mdl is editor_readers.read_mdl is read_mdl; "
            "assert move_sound.bom_parts is move_sets.bom_parts is formats.bom_parts; "
            "assert 'viewer' not in sys.modules and 'assets' not in sys.modules"
        )
        subprocess.run([sys.executable, '-I', '-c', code, str(APP)], check=True, timeout=20)

    @unittest.skipUnless((workspace_root(APP)/'KGF原创剧情优化_20261105/KGF原创剧情修复版 v7.iso').is_file(),
                         '需要本地奥0 v7 镜像')
    def test_animation_track_cache_follows_whole_bank_history(self):
        game = viewer.open_game(workspace_root(APP)/'KGF原创剧情优化_20261105/KGF原创剧情修复版 v7.iso')
        member = game.roles[0]['cases'][0]['anm_banks'][0]['id']
        before = game.snapshot()
        tracks = game.bank_tracks(member)
        new_track = next(i for i in range(1, 156) if i not in tracks)
        _, offsets, clips = game.bank(member, b'ANM\0')
        slot = next(i for i, clip in enumerate(clips) if clip and len(clip_ids(clip)) >= 2)
        edited = bytearray(game.read_source_member(member, b'ANM\0'))
        edited[offsets[slot] + 52 + 14] = new_track
        modified = game.snapshot()
        modified['asset_edits'][member, None] = bytes(edited)
        game.apply_snapshot(modified)
        game.remember(before)
        self.assertIn(new_track, game.bank_tracks(member))
        game.step_history()
        self.assertEqual(game.bank_tracks(member), tracks)
        game.step_history(True)
        self.assertIn(new_track, game.bank_tracks(member))

    def test_project_candidates_checked_once_and_manual_selection_still_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            good, wrong = folder/'ok.iso', folder/'wrong.iso'
            good.touch()
            wrong.touch()
            project = folder/'sample.kgfp'
            api = viewer.ViewerApi(session_path=folder/'session.json')
            api._window = Mock()
            manifest = {'view': {'frame': 31}, 'source': str(wrong), 'source_sha256': 'ok'}
            candidates = {path:Mock(path=path) for path in (good, wrong)}
            checks = []

            def digest(game):
                checks.append(game.path)
                return game.path.stem

            with (patch('viewer.read_manifest', return_value=manifest),
                  patch('viewer.open_game', side_effect=lambda path: candidates[Path(path)]),
                  patch('project_file.iso_digest', side_effect=digest),
                  patch('project_file._parse_kgf_project') as restore):
                game, view = api._read_project(project, [wrong, good])
                self.assertEqual((game, view, checks), (candidates[good], {'frame':31}, [wrong, good]))
                api._window.create_file_dialog.assert_not_called()
                restore.assert_called_once()
                with patch('project_file.iso_digest', side_effect=[OSError('source removed'), 'ok']):
                    self.assertIs(api._read_project(project, [good])[0], candidates[good])
                manifest['source'] = None
                checks.clear()
                api._window.create_file_dialog.return_value = [str(wrong)]
                with self.assertRaises(viewer.ProjectBaseError):
                    api._read_project(project, [])
                self.assertEqual(checks, [wrong])
                restore.side_effect = ValueError('invalid event')
                with self.assertRaisesRegex(ValueError, 'invalid event'):
                    api._read_project(project, [good])
                self.assertEqual(api._window.create_file_dialog.call_count, 1)

    def test_late_move_source_result_cannot_replace_newer_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            api = viewer.ViewerApi(session_path=Path(directory)/'session.json')
            game = Mock(path=Path(directory)/'current.iso')
            game.catalog.return_value = {'name':'current', 'roles':[]}
            api._accept(game)
            api._window = Mock()
            api._window.create_file_dialog.return_value = ['slow.iso']
            started, release = Event(), Event()
            results = []

            def slow(_):
                started.set()
                if not release.wait(5):
                    raise AssertionError('source selection timed out')
                return Mock()

            with patch('viewer.open_game', side_effect=slow):
                thread = Thread(target=lambda: results.append(api.open_move_source(1)))
                thread.start()
                try:
                    self.assertTrue(started.wait(5))
                    self.assertEqual(api.open_move_source(1, True)['value']['name'], 'current')
                finally:
                    release.set()
                    thread.join(5)
            self.assertEqual(results, [{'value':None}])
            self.assertIs(api._cross_game, game)

    def test_failed_undo_and_redo_keep_edits_plan_and_stacks(self):
        game = FE3Game.__new__(FE3Game)
        game.ensure_open = Mock()
        fields = ('assets', 'native', 'skills', 'ratings', 'throws', 'bindings', 'jump',
                  'charge', 'transfers', 'additions', 'sounds')
        game._restore_edits({key:{} for key in fields})
        game.saved = game.snapshot()
        plan = game.transfer_plan = Transfers()
        game.undo, game.redo = [], []
        game.native_edits[12] = 50
        game.remember(game.saved)
        for redo in (False, True):
            before = game.snapshot(), game.undo.copy(), game.redo.copy()
            with patch('fe3_game.compile_transfers', side_effect=ValueError('failed')):
                with self.assertRaisesRegex(ValueError, 'failed'):
                    game.step_history(redo)
            self.assertEqual((game.snapshot(), game.undo, game.redo), before)
            self.assertIs(game.transfer_plan, plan)
            with patch('fe3_game.compile_transfers', return_value=plan):
                game.step_history(redo)
        self.assertEqual(game.native_edits, {12:50})


if __name__ == '__main__':
    unittest.main()
