"""Match physical ANM slots across fighters and stage a whole roster entry in one edit."""
from collections import defaultdict
from dataclasses import dataclass, field
from functools import cached_property
import math
import struct

import numpy as np

from assets import selected
from animation_edit import STORY_BANKS, protect_story_clip, replace_story_bank
from asset_transfer import validate_clip
from cross_move import (KGF_MOTION_NAMES, Retarget, bom_data, commit, event_form, is_fe3, native_bom, original_order,
                        pack_animation, pack_bom, skeleton)
from cross_timing import BomTiming
from fe3_formats import require
from fe3_game import ROSTER
from fe3_preview import clip_index, motion
from fe3_transfer import script_bytes
from formats import anm_header, decode_anm, event_records, merge_form_events, u32
from input_notation import input_arrows
from move_sets import pack_script
from native_tables import THROWS
from fe3_workbench import THROW_STATES


@dataclass
class Slot:
    bank: int
    index: int
    number: int
    config: int
    shape: int
    group: tuple
    raw: bytes
    title: str
    states: set
    inputs: set
    kind: str
    scripts: set = field(default_factory=set)
    locked: str = ''

    @property
    def id(self):
        return f'{self.bank}/{self.index}'

    @property
    def label(self):
        return f'ANM {self.number:03d} · {self.title} · {self.bank}/{self.index}'

    @cached_property
    def wrapped(self):
        return struct.pack('<4s3I', b'ANM\0', 1, 16, 0) + self.raw

    def animation(self, ps2):
        return motion(self.wrapped, 0) if ps2 else decode_anm(self.raw)

    def span(self, ps2):
        if ps2:
            header = clip_index(self.wrapped)[0]
            return max(0, header.lower), header.last_frame
        ctrl, _, _, last, _ = anm_header(self.raw)
        return max(0, struct.unpack_from('<h', self.raw, ctrl)[0]), last


def bindings(game, role, config):
    if is_fe3(game):
        tables = game.workbench.tables
        sizes = tables.descriptor(role)[1]
        rows = iter(tables.banks(role))
        for kind, count in enumerate(sizes[5:9]):
            for n in range(count):
                bank, start = next(rows)
                name = ROSTER[n][1] if kind == 1 and n < len(ROSTER) else n
                yield bank, start, (kind, name)
    else:
        case = game.roles[role]['cases'][config]
        yield case['anm_banks'][0]['id'], 0, (0, 0)
        for row in case['encounter_anm']:
            yield row['id'], row['first_clip'], (1, game.roles[row['encounter_role']]['label'])


def descriptions(game, role, config, shape):
    rows = defaultdict(lambda: [set(),set(),set(), ''])
    if is_fe3(game):
        profile = game.workbench.profile(role, shape)
        contexts = game.workbench.contextual_commands(role,shape)[1]
        skills = {r['anm'] for r in profile['skills'] if r['menuMask'] & 1 << shape}
        for move in profile['moves']:
            states,keys,types,_ = rows[move['id']]
            states.update(move['states'])
            keys.update(move['keys'])
            keys.update(contexts[s] for s in states if s in contexts)
            if states.intersection(THROW_STATES):
                types.add('投技')
            if move['id'] in skills: types.add('必杀')
            if move['damage'] or move['combo']: types.add('攻击')
            rows[move['id']][3] = move['name']
    else:
        case,_,bom,_ = game.form_binding(role, config, shape)
        for move in game.move_catalog(role, case, bom, shape):
            slot = move['clip']
            states,keys,types,title = rows[slot]
            if move['id'] >= 0 and not move.get('followup') and not move.get('rage'):
                states.add(move['id'])
            keys.update(move.get('combo_paths') or move.get('inputs') or ())
            if move['id'] in KGF_MOTION_NAMES:
                keys.add(input_arrows(KGF_MOTION_NAMES[move['id']]))
            if move['id'] in THROWS or move['category']==11:
                types.add('投技')
            if move['category']==5: types.add('必杀')
            elif move['category']==4 or move['damage']: types.add('攻击')
            rows[slot][3] = title or move['name']
    return rows


def input_key(text):
    value = input_arrows(text).replace(' ','').replace('短冲','冲刺')
    return {'保持→':'→走','保持←':'←走','→':'→走','←':'←走',
            '跑动→⟶按住→':'→跑','跑动←⟶按住←':'←跑',
            '冲刺→⟶→':'→冲刺','冲刺←⟶←':'←冲刺'}.get(value,value)


def inventory(game, actor, all_configs, receiving=False):
    role, current, shape = actor
    ps2 = is_fe3(game)
    if ps2:
        selected(ROSTER,role)
        require(current == 0 and not game.workbench.form(role,shape).accessory, '请选择角色本体形态。')
        configs = [0]
    else:
        game.form_binding(role,current,shape)
        configs = [current] + [i for i in range(len(game.roles[role]['cases'])) if all_configs and i!=current]
    owners = defaultdict(set)
    if receiving:
        roles = len(ROSTER) if ps2 else len(game.roles)
        for who in range(roles):
            for cfg in ([0] if ps2 else range(len(game.roles[who]['cases']))):
                for bank,_,_ in bindings(game,who,cfg):
                    owners[bank].add(who)
    slots = {}
    for cfg in configs:
        form = shape if ps2 or shape < len(game.roles[role]['cases'][cfg]['forms']) else 0
        desc = descriptions(game,role,cfg,form)
        if ps2:
            scripts = [(f.bom,f.id) for f in game.workbench.forms(role).values() if f.bom and not f.accessory]
        else:
            case = game.roles[role]['cases'][cfg]
            scripts = [((game.form_binding(role,cfg,i)[2]['id'],None),i) for i in range(len(case['forms']))]
        for bank,start,group in bindings(game,role,cfg):
            if ps2:
                data = game.data((bank,-1))
                clips = [data[c.start:c.start+c.size] for c in clip_index(data)]
            else:
                clips = game.bank(bank,b'ANM\0')[2]
            for index,base in enumerate(clips):
                if not base:continue
                number = start+index
                key = f'{bank}/{index}'
                if key not in slots:
                    states,inputs,kinds,title = desc[number]
                    kind = '对手动作' if group[0]==1 else next((k for k in ('必杀','投技','攻击') if k in kinds),'动作')
                    raw = base if ps2 else game.asset_edits.get((bank,index),base)
                    if not ps2 and game.move_sets:
                        raw = game.current_moves().animations.get(bank,{}).get(index,raw)
                    slot = Slot(bank,index,number,cfg,form,group,raw,title or kind,states,
                                {input_key(t) for t in inputs if t},kind)
                    if group[0]==1:
                        slot.title = f'对{group[1]} · {slot.title}'
                    if receiving and owners[bank]-{role}:
                        slot.locked = '其他角色也直接引用这个动画库'
                    if receiving and ps2 and (bank,-1) in game.transfer_plan.resources:
                        slot.locked = '工程中已有完整必杀配招接管此库'
                    if receiving and not ps2 and game.move_sets and index in game.current_moves().animations.get(bank,{}):
                        slot.locked = '工程中已有配招接管此片段'
                    slots[key] = slot
                slots[key].scripts.update((bom,number,min(7,s) if ps2 else s) for bom,s in scripts)
    return slots


def pose_signature(slot, ps2, tracks):
    """Compare nearest native rotation keys at start, middle and end on common logical tracks."""
    lo,hi = slot.span(ps2)
    times = np.asarray([lo,(lo+hi)/2,hi])
    directory = {t:40+14*i for i,t in enumerate(original_order(slot.raw))}
    features = np.zeros((len(tracks),3,3,2),dtype=np.float32)
    for i,t in enumerate(tracks):
        if t not in directory:continue
        a,b = struct.unpack_from('<2H',slot.raw,directory[t])
        keys = np.frombuffer(slot.raw,'<u2',count=4*max(1,b-a),offset=32+8*a).reshape(-1,4)
        chosen = np.abs(keys[:,3,None].astype(float)-times).argmin(axis=0)
        angles = keys[chosen,:3]*(math.tau/(65536 if ps2 else 65535))-math.pi
        features[i,:,:,0] = np.sin(angles)
        features[i,:,:,1] = np.cos(angles)
    return np.r_[features.ravel()/math.sqrt(max(1,len(tracks))), math.log1p(hi-lo)*0.7]


class MotionSet:
    def __init__(self,target,source,receiver,donor):
        target.ensure_open();source.ensure_open()
        self.target,self.source,self.receiver,self.donor = target,source,receiver,donor
        self.ps2,self.source_ps2 = is_fe3(target),is_fe3(source)
        self.dest = inventory(target,receiver,True,True)
        self.orig = inventory(source,donor,False)
        require(self.orig and self.dest, '当前选择缺少可读取的 ANM。')
        self.bindings = {}
        self.selection = {}
        self.reasons = {}
        self.match()

    def rig(self, slot):
        key = slot.config,slot.shape
        if key not in self.bindings:
            self.bindings[key] = Retarget(self.source_rig,skeleton(self.target,self.receiver[0],*key))
        return self.bindings[key]

    @cached_property
    def source_rig(self):
        return skeleton(self.source,*self.donor)

    def match(self):
        candidates = list(self.orig.values())
        tracks = sorted(self.rig(next(iter(self.dest.values()))).common)
        tracks = [tracks[i] for i in np.linspace(0,len(tracks)-1,min(10,len(tracks)),dtype=int)]
        poses = np.stack([pose_signature(s,self.source_ps2,tracks) for s in candidates])
        same_engine = self.ps2==self.source_ps2
        for key,slot in self.dest.items():
            if slot.locked:
                self.selection[key],self.reasons[key] = None,slot.locked
                continue
            same_group = [i for i,s in enumerate(candidates) if s.group==slot.group]
            pool = same_group or [i for i,s in enumerate(candidates) if s.group[0]==slot.group[0]] or list(range(len(candidates)))
            exact = [i for i in same_group if candidates[i].index==slot.index]
            identical_role = same_engine and self.receiver[0]==self.donor[0]
            if exact and (identical_role or slot.group[0]==1 and same_engine):
                pool, reason = exact,'同组槽位'
            elif slot.number==0 and 0 in slot.states and any(candidates[i].number==0 for i in pool):
                pool,reason = [i for i in pool if candidates[i].number==0],'待机'
            else:
                compatible = [i for i in pool if candidates[i].kind==slot.kind]
                typed = compatible or pool
                inputs = [i for i in typed if slot.inputs & candidates[i].inputs]
                states = [i for i in typed if same_engine and slot.states & candidates[i].states]
                both = [i for i in inputs if i in states]
                if both: pool,reason=both,'输入与状态'
                elif inputs: pool,reason=inputs,'输入或连段'
                elif states: pool,reason=states,'同作状态'
                else: pool,reason=typed,'姿势近似'
            if len(pool)>1:
                signature = pose_signature(slot,self.ps2,tracks)
                diff = poses[pool]-signature
                index = pool[int(np.einsum('ij,ij->i',diff,diff).argmin())]
            else:index=pool[0]
            self.selection[key],self.reasons[key] = candidates[index].id,reason

    def listing(self):
        return {'source':[{'id':k,'name':s.label} for k,s in self.orig.items()],
                'target':[{'id':k,'name':s.label,'source':self.selection[k],'reason':self.reasons[k],
                           'locked':bool(s.locked)} for k,s in self.dest.items()]}

    def apply(self, overrides):
        require(isinstance(overrides,dict) and overrides.keys()<=self.dest.keys(), '动作对照已失效，请刷新。')
        choices = self.selection.copy()
        for key,id in overrides.items():
            require(id is None or isinstance(id,str) and id in self.orig and not self.dest[key].locked,
                    '选择了不可写的片段或未知来源。')
            choices[key]=id
        changes, banks, schedules = {},defaultdict(dict),{}
        converted = 0
        cache = {}
        for key,id in choices.items():
            if id is None:continue
            dst,src = self.dest[key],self.orig[id]
            binding = self.rig(dst)
            try:
                signature=(dst.config,dst.shape,dst.raw,src.raw)
                clip=cache.get(signature)
                if self.ps2==self.source_ps2 and original_order(dst.raw)==original_order(src.raw) and binding.same_pose:
                    clip=src.raw
                elif clip is None:
                    clip=pack_animation(dst.raw,src.animation(self.source_ps2),dst.animation(self.ps2),
                                        binding,self.ps2,sparse=True)
                    cache[signature]=clip
                if clip==dst.raw:continue
                if self.ps2:
                    banks[dst.bank][dst.index]=clip
                else:
                    validate_clip(clip,set(original_order(dst.raw)))
                    if dst.bank in STORY_BANKS:
                        protect_story_clip(self.target.bank(dst.bank,b'ANM\0')[1],len(clip),dst.index)
                    changes[dst.bank,dst.index]=clip
                span = dst.span(self.ps2),src.span(self.source_ps2)
                for script in dst.scripts:
                    require(script not in schedules or schedules[script]==span,'共用事件脚本需要相同的时间范围。')
                    schedules[script]=span
                converted+=1
            except (ValueError,struct.error) as exc:
                raise ValueError(f'{dst.label} ← {src.label}：{exc}') from exc
        if not self.ps2:
            for bank in STORY_BANKS.intersection(k[0] for k in changes):
                bank_edits={slot:raw for (member,slot),raw in (self.target.asset_edits | changes).items()
                            if member==bank and slot is not None}
                replace_story_bank(self.target.read_source_member(bank,b'ANM\0'),bank_edits)
        for bank,edits in banks.items():
            key=bank,-1
            data=self.target.data(key)
            directory=clip_index(data)
            out=bytearray(data[:directory[0].start])
            for i,entry in enumerate(directory):
                struct.pack_into('<I',out,8+4*i,len(out))
                out.extend(edits.get(i,data[entry.start:entry.start+entry.size]))
                end=directory[i+1].start if i+1<len(directory) else len(data)
                out.extend(data[entry.start+entry.size:end])
            changes[key]=self.target.validate_import(self.target.part(*key),bytes(out))
        boms = self.retime(schedules)
        changes.update(boms)
        self.source.ensure_open();self.target.ensure_open()
        if changes:
            commit(self.target,changes,() if self.ps2 else tuple(k[0] for k in boms))
        kept=sum(id is None for id in choices.values())
        return {'changed':converted,'kept':kept,'same':len(choices)-converted-kept,
                'total':len(choices),'edits':self.target.edit_status()}

    def retime(self,schedules):
        grouped=defaultdict(list)
        for (key,n,shape),(old,new) in schedules.items():
            if old!=new:grouped[key].append((n,shape,old,new))
        result={}
        for key,entries in grouped.items():
            data=bom_data(self.target,key)
            parts,count=native_bom(self.target,None,data)
            timing=BomTiming(data)
            changed=False
            for n,shape,old,new in entries:
                if n>=count or not parts[n]:continue
                ev=list(event_records(parts[n]))
                mask=1<<min(shape,7)
                rows=[r.raw[:1]+bytes([mask])+r.raw[2:] for r in ev if event_form(r.raw[1],shape,self.ps2)]
                if not rows:continue
                moved=timing.remap((n,shape),rows,old,new,self.ps2)
                if moved==rows:continue
                require(len(ev)==u32(parts[n],0) and not any(r.overlap for r in ev),
                        f'BOM {key[0]} 的脚本 {n} 含共用记录，无法单独缩放。')
                merged=merge_form_events(((r.raw,event_form(r.raw[1],shape,self.ps2)) for r in ev),mask,moved)
                if merged is not None:
                    parts[n]=(script_bytes if self.ps2 else pack_script)(merged)
                    changed=True
            if changed:
                require(not self.ps2 or key not in self.target.transfer_plan.resources,'事件库正在使用独立必杀配招。')
                raw=timing.write(pack_bom(parts,count,self.ps2))
                result[key]=self.target.validate_import(self.target.part(*key),raw) if self.ps2 else raw
        return result
