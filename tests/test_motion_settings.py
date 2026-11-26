import struct
import sys
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(APP))
from ogeditor.paths import workspace_root
from viewer import Game
from ogeditor.psp.animation_edit import STORY_CLIPS, read_anm_bank, replace_story_bank
from ogeditor.ps2.native import f32, fill_frames, rate_for_frames
from ogeditor.psp.native_tables import EnergyField


class MotionSettings(unittest.TestCase):
    def test_charge_rounding_and_skill_threshold(self):
        self.assertEqual([fill_frames(1.5), fill_frames(3.)], [171,86])
        self.assertEqual(fill_frames(1.5,150),100)
        for n in (1,2,3,31,60,86,171,255,257,501,1200):
            rate = rate_for_frames(n)
            current = 0.
            for _ in range(n-1):
                current = f32(current+rate)
            self.assertLess(current,256)
            self.assertGreaterEqual(f32(current+rate),256)
            self.assertEqual(fill_frames(rate),n)

    def test_energy_uses_nonnegative_float32(self):
        field = EnergyField(0,0,'<f')
        self.assertEqual(struct.unpack('<f',field.encode(0)),(0,))
        self.assertEqual(field.encode(102.4),struct.pack('<f',102.4))
        for value in (-1,float('nan'),float('inf'),True):
            with self.assertRaises(ValueError):field.encode(value)

    def test_fixed_story_offsets_move_indexed_neighbours(self):
        def clip(size,fill):
            return struct.pack('<4sI',b'ANM\0',size)+bytes([fill])*(size-8)
        count=23
        bank=bytearray(STORY_CLIPS[22]+64)
        offsets=[0xFFFFFFFF]*count
        for i,at in {**STORY_CLIPS,7:STORY_CLIPS[6]+64}.items():
            bank[at:at+64]=clip(64,i)
            offsets[i]=at
        struct.pack_into(f'<4sI{count}I',bank,0,b'ANM\0',count,*offsets)
        larger=clip(128,6)
        new=replace_story_bank(bytes(bank),{6:larger})
        _,loc,parts=read_anm_bank(new)
        self.assertEqual((loc[6],loc[22]),(STORY_CLIPS[6],STORY_CLIPS[22]))
        self.assertEqual((parts[6],parts[7],parts[22]),(larger,clip(64,7),clip(64,22)))
        self.assertGreaterEqual(loc[7],len(bank))
        bank[STORY_CLIPS[6]+128]=0xFE
        with self.assertRaises(ValueError):replace_story_bank(bytes(bank),{6:clip(144,6)})

    @unittest.skipUnless((workspace_root(APP)/'KGF原创剧情优化_20261105/KGF原创剧情修复版 v7_选人分页_联机形态修复.iso').is_file(),
                         '需要本地联机形态修复版')
    def test_page_base_and_atomic_energy_edit(self):
        game=Game(workspace_root(APP)/'KGF原创剧情优化_20261105/KGF原创剧情修复版 v7_选人分页_联机形态修复.iso')
        original=game.snapshot()
        with self.assertRaises(ValueError):game.edit_energy(0,[0,10,20,-1])
        self.assertEqual(game.snapshot(),original)
        game.edit_energy(0,[0,100,200,300])
        self.assertEqual(game.actor_attributes(0)['energy'],[0,100,200,300])
        game.step_history()
        self.assertEqual(game.snapshot(),original)
