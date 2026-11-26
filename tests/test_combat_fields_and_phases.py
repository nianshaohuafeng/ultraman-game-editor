import unittest
from pathlib import Path
import sys
import tempfile

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(APP))
from ogeditor.paths import workspace_root
from ogeditor.psp.game import Game
from ogeditor.psp.scene import battle_preview
from project_file import write_project, read_manifest, parse_project
from viewer import open_game
from ogeditor.ps2.formats import Bom

ROOT = workspace_root(APP)
V7 = ROOT/'KGF原创剧情优化_20261105/KGF原创剧情修复版 v7.iso'
FE3 = ROOT/'01_游戏镜像/格斗进化3/奥特曼格斗进化3.iso'


@unittest.skipUnless(V7.exists(), '需要奥0 v7 镜像')
class SkillPhases(unittest.TestCase):
    def test_native_phase_lists_and_timelines(self):
        game = Game(V7)
        for role, root, clips in [(1,160,[201,202]),(0,158,[197,198]),(8,158,[157,158])]:
            case,_,bom,_ = game.form_binding(role,0,0)
            rows = game.move_catalog(role,case,bom)
            first = next(row for row in rows if row['id']==root)
            self.assertEqual(first['phases'],[root,root+1])
            preview = battle_preview(game,role,0,0,root,4)
            self.assertEqual([s['slot'] for s in preview['actors'][0]],clips)
            self.assertGreater(preview['last'],preview['actors'][0][1]['frame'])
            if role==1:
                self.assertEqual(sum(e.get('factory') in (22,23) for e in preview['events']),19)

    def test_transferred_second_phase_damage_survives_project_and_iso(self):
        g=Game(V7)
        g.edit_move_set(0,158,1,160)
        def event(game):
            return next(e for e in game.motion(0,0,0,159)['events'] if e['op']==7 and e['frame']==25)
        def check(game):
            preview=battle_preview(game,0,0,0,158,4)
            self.assertEqual([s['state'] for s in preview['actors'][0]],[158,159])
            self.assertEqual(event(game)['damage'],37)
            self.assertEqual(game.motion(0,0,0,159)['animation']['rate'],1.25)
            self.assertEqual(game.keyframes(0,0,0,159,0,1)[0][1],changed_x)
            self.assertEqual(game.motion(0,0,0,158)['animation']['rate'],1)
            self.assertEqual(game.motion(1,0,0,161)['animation']['rate'],1)
            self.assertEqual(next(e for e in game.motion(1,0,0,161)['events'] if e['op']==7)['damage'],1)
            self.assertEqual(next(e for e in game.motion(0,0,0,158)['events'] if e['op']==7)['damage'],1)
        g.motion(0,0,0,159,(event(g)['id'],{'4':37}))
        m=g.motion(0,0,0,159)
        g.edit_timing(0,0,0,159,m['animation']['last']+1,1.25)
        keys=g.keyframes(0,0,0,159,0,1)
        changed_x=keys[0][1]=3.25
        g.edit_keys(0,0,0,159,0,1,keys)
        check(g)
        with tempfile.TemporaryDirectory() as directory:
            project=Path(directory)/'phases.kgfp'
            write_project(g,project,{})
            h=Game(V7)
            parse_project(h,project,read_manifest(project))
            check(h)
            output=Path(directory)/'phases.iso'
            h.export(output)
            mounted=Game(output)
            check(mounted)
            mounted.edit_move_set(0,101,1,101)
            check(mounted)
            mounted.export(Path(directory)/'reopened.iso')
            check(Game(Path(directory)/'reopened.iso'))


@unittest.skipUnless(FE3.exists(), '需要奥3镜像')
class DamageFields(unittest.TestCase):
    def test_batch_covers_replacement_and_alternate_damage(self):
        g=open_game(FE3)
        key=g.workbench.form(0,0).bom
        def args(script,op):
            return next(e['args'] for e in Bom(g.data(key)).events(script) if e['op']==op)
        for script,op,fields in [(460,10,(0,)),(69,8,(0,)),(43,24,(2,9))]:
            before=args(script,op)
            self.assertTrue(g.batch(0,0,'inspect',str(script),1)['rows'])
            g.batch(0,0,'damage',str(script),2)
            self.assertEqual(args(script,op),tuple(v*2 if i in fields else v for i,v in enumerate(before)))

    def test_travel_scales_delta_from_first_pose(self):
        from ogeditor.ps2.preview import clip_index, key_rows
        g=open_game(FE3)
        key, index=g.workbench.binding(0)[1]
        track=clip_index(g.data(key))[index].tracks.index(0)
        before=key_rows(g.data(key),index,track,1)
        self.assertGreater(abs(before[0]['value'][2]),1)
        g.batch(0,0,'travel','1',2)
        after=key_rows(g.data(key),index,track,1)
        self.assertEqual(before[0],after[0])
        for src,dst in zip(before,after):
            self.assertEqual(src['value'][1],dst['value'][1])
            for axis in (0,2):
                start=before[0]['value'][axis]
                self.assertAlmostEqual(dst['value'][axis]-start,2*(src['value'][axis]-start),places=5)
