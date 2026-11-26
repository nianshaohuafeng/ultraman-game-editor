"""Convert native fighter animation and contact data between FE3 and KGF v7."""

from collections import Counter, defaultdict, deque
from dataclasses import dataclass, replace
from functools import cached_property
import math
import struct

import numpy as np

from ogeditor.psp.animation_edit import STORY_BANKS, protect_story_clip, read_anm_bank
from ogeditor.psp.asset_transfer import validate_clip
from ogeditor.psp.game import selected
from ogeditor.transfer.cross_hands import fe3_hand_clouds, match_hand_events, psp_hand_clouds
from cross_timing import BomTiming
from ogeditor.ps2.formats import Bom, require
from ogeditor.ps2.game import FE3Game
from ogeditor.ps2.preview import read_bones, replace_clip, single_clip
from ogeditor.ps2.transfer import SHAPE_FIELDS, entry_bytes, script_bytes
from formats import SHAPE_TEMPLATE_OFFSETS, bom_parts, event_records, merge_form_events, read_shape, s16, u32
from ogeditor.input_notation import SEQUENCE, input_arrows
from kgf_readers.mdl_reader import read_mdl
from ogeditor.models.model_retarget import local_matrix
from ogeditor.psp.move_sets import pack_script
from move_sound import SoundLibrary, SoundTransfer
from ogeditor.psp.native_tables import THROWS


COMBAT_CATEGORIES = frozenset((4, 5, 11))
HIT_TEMPLATES = ({0: 22, 5: 32}, {0: 22, 0x18: 32})
KGF_MOTION_NAMES = {
    0: '站立待机', 1: '站立过渡', 2: '向前走', 3: '向前冲刺', 4: '向前跑', 5: '前跑过渡',
    6: '向后走', 7: '向后冲刺', 8: '向后跑', 11: '下蹲开始', 12: '下蹲待机',
    13: '前滚', 14: '后滚', 15: '原地起跳', 16: '向前起跳', 17: '向后起跳', 18: '跑动起跳',
    19: '原地跳跃 · 空中', 20: '向前跳跃 · 空中', 21: '向后跳跃 · 空中',
    22: '原地跳跃 · 落地', 23: '向前跳跃 · 落地', 24: '向后跳跃 · 落地', 25: '站立防御', 26: '下蹲防御',
}


def is_fe3(game):
    return isinstance(game,FE3Game)


@dataclass
class Skeleton:
    bones: list
    tracks: dict

    @cached_property
    def world(self):
        result = []
        for bone in self.bones:
            local = local_matrix(bone)
            parent = bone['parent']
            result.append(result[parent] @ local if parent >= 0 else local)
        return np.asarray(result)

    @cached_property
    def by_node(self):
        return {node:track for track,node in self.tracks.items()}

    def ancestor(self,node,channels):
        while node >= 0:
            channel = self.by_node.get(node)
            if channel in channels:
                return channel
            node = self.bones[node]['parent']
        return None

    def parent_track(self,track,channels):
        return self.ancestor(self.bones[self.tracks[track]]['parent'],channels)

    def channel_parent_matrix(self, track, channels):
        parent = self.bones[self.tracks[track]]['parent']
        root = self.parent_track(track, channels)
        if parent < 0:
            return np.eye(3)
        current = self.world[parent]
        relative = current if root is None else np.linalg.solve(self.world[self.tracks[root]], current)
        return relative[:3, :3]


def skeleton(game,role,config,shape):
    if is_fe3(game):
        f = game.workbench.form(role,shape)
        bones = read_bones(game.data(f.model))
        tracks = {}
        for node,track in f.tracks:
            if node >= 0 and track >= 0:
                tracks.setdefault(track,node)
                if track == 0 and bones[node]['parent'] < 0:
                    tracks[0] = node
        return Skeleton(bones,tracks)
    _,form,_,_ = game.form_binding(role,config,shape)
    raw = game.read_member(form['mdl']['id'],b'MDL\0')
    rig = game.model_rig(read_mdl(raw),(role,config,shape))
    return Skeleton(rig.bones,rig.tracks)


@dataclass
class Retarget:
    source: Skeleton
    target: Skeleton

    @cached_property
    def same_pose(self):
        if self.source.tracks.keys() != self.target.tracks.keys():
            return False
        return all(np.array_equal(self.source.world[self.source.tracks[t]],self.target.world[self.target.tracks[t]])
                   for t in self.common)

    def __post_init__(self):
        self.common = self.source.tracks.keys() & self.target.tracks.keys()
        require(0 in self.common and len(self.common) >= 2,'双方没有可核对的根轨道和骨骼链。')
        require(all(self.source.parent_track(i,self.common) == self.target.parent_track(i,self.common)
                    for i in self.common),'双方公共轨道的父链不同，需先适配这套骨架。')
        ratios = []
        for track in sorted(self.common):
            parent = self.source.parent_track(track,self.common)
            if parent is not None:
                a,b = (np.linalg.norm(rig.world[rig.tracks[track],:3,3]-rig.world[rig.tracks[parent],:3,3])
                       for rig in (self.source,self.target))
                if min(a,b)>1e-4:
                    ratios.append(b/a)
        self.scale = float(np.median(ratios)) if ratios else 1.0

    def bone(self,node):
        if node == -1:
            return -1
        require(0 <= node < len(self.source.bones),'来源判定引用了不存在的节点。')
        track = self.source.ancestor(node,self.common)
        require(track is not None,'判定挂点没有接收骨骼。')
        return self.target.tracks[track]

    def shape(self,geometry):
        node = geometry['node']
        dest = self.bone(node)
        if dest == -1:
            rotation,shift = np.eye(3),np.zeros(3)
        else:
            source = self.source.world[node]
            parent = self.source.world[self.source.tracks[self.target.by_node[dest]]]
            local = np.linalg.solve(parent, source)
            rotation, shift = local[:3, :3], local[:3, 3]
        positions = [geometry['center']]
        if geometry.get('to') is not None:
            positions.append(geometry['to'])
        xyz = (np.asarray(positions) @ rotation.T + shift)*self.scale
        radius = geometry['radius']*self.scale
        require(np.isfinite(xyz).all() and math.isfinite(radius) and radius>0,'转换后的形状无效。')
        return dest,struct.pack(f'<I{xyz.size+1}f',len(positions),*xyz.flat,radius)


def euler(matrix):
    y = math.asin(float(np.clip(-matrix[2,0],-1,1)))
    if abs(math.cos(y)) < 1e-6:
        return (0,y,math.atan2(-matrix[0,1],matrix[1,1]))
    return (math.atan2(matrix[2,1],matrix[2,2]),y,math.atan2(matrix[1,0],matrix[0,0]))


def quaternion_matrix(values):
    x,y,z,w = values
    return np.array(((1-2*(y*y+z*z),2*(x*y-z*w),2*(x*z+y*w)),
                     (2*(x*y+z*w),1-2*(x*x+z*z),2*(y*z-x*w)),
                     (2*(x*z-y*w),2*(y*z+x*w),1-2*(x*x+y*y))))


def vector_at(keys,time):
    return np.array([np.interp(time,keys[:,0],values) for values in keys[:,1:].T])


def quaternion_at(keys,time):
    idx = np.searchsorted(keys[:,0],time,side='right')
    if idx == 0 or idx == len(keys):
        return keys[0 if idx == 0 else -1,1:]
    a,b = keys[idx-1],keys[idx]
    lhs,rhs = a[1:],b[1:]
    dot = float(lhs @ rhs)
    if dot<0:
        rhs,dot = -rhs,-dot
    factor = (time-a[0])/(b[0]-a[0])
    if dot>.9999:
        out = (1-factor)*lhs+factor*rhs
        return out/np.linalg.norm(out)
    theta = math.acos(min(1,dot))
    return (math.sin(theta*(1-factor))*lhs+math.sin(theta*factor)*rhs)/math.sin(theta)


def original_order(raw):
    count, = struct.unpack_from('<H',raw,38)
    return list(raw[52:40+14*count:14])


def sample_span(animation):
    return animation.get('first',0),animation['last']


def pack_animation(reference, animation, output, bind, ps2, *, sparse=False):
    """Retarget the complete donor pose and clock, including the independent root position."""
    first,last = sample_span(animation)
    old_first,old_last = sample_span(output)
    require(0 <= first <= last < 32767, '来源动画的关键帧区间无法写入原生格式。')
    times = range(first, last + 1)
    order = original_order(reference)
    require(len(set(order)) == len(order) and 0 in order,
            '接收片段的原生轨道与当前模型绑定不同。')
    data = bytearray(reference[:40])
    data.extend(bytes(14*len(order)))
    data.extend(bytes(-len(data)%8))
    denom = 65536 if ps2 else 65535
    local_position = output['localTranslation']
    local_scale = output['localScale']
    encoded_curves = {}

    def samples(keys, integer=False):
        if not sparse or len(keys) == 1:
            return times if not sparse else (first,)
        return sorted({first, last, *((int(t) if integer else float(t)) for t in keys[:,0] if first < t < last)})

    def copy_curve(at, channel, stride):
        if (first, last) != (old_first, old_last):
            track = reference[at + 12]
            curve = output['channels'][track][channel]
            keys = np.asarray(output['tracks'][curve]).reshape(-1, 5 if channel == 0 else 4)
            grid = times
            if sparse:
                span = max(1,old_last-old_first)
                grid = sorted({first,last,*(round(first+(t-old_first)*(last-first)/span)
                              for t in keys[:,0] if old_first < t < old_last)})
            ticks = [old_first + (t-first)*(old_last-old_first)/max(last-first,1) for t in grid]
            if channel == 0:
                return put([euler(quaternion_matrix(quaternion_at(keys,t))) for t in ticks], rot_key, grid)
            return put([tuple(vector_at(keys,t)) for t in ticks], vec_key, grid)
        a, b = struct.unpack_from('<2H', reference, at + 4 * channel)
        start = (len(data) - 32) // 8
        end = start + b - a
        require(end <= 65535, '转换后的关键帧超过原生 ANM 可寻址范围。')
        length = (b - a) * 8 or stride
        data.extend(reference[32 + 8 * a:32 + 8 * a + length])
        return start, end

    if not ps2:
        for track in bind.common.intersection(order, animation['channels']):
            node = bind.source.bones[bind.source.tracks[track]]
            _, pos_id, scale_id = animation['channels'][track]
            if track and animation['localTranslation'] and not local_position:
                positions = np.asarray(animation['tracks'][pos_id]).reshape(-1, 4)[:, 1:]
                local_position = not np.allclose(positions, node['translation'], rtol=0, atol=1e-5)
            if animation['localScale'] and not local_scale:
                sizes = np.asarray(animation['tracks'][scale_id]).reshape(-1, 4)[:, 1:]
                local_scale = not np.allclose(sizes, node['scale'], rtol=0, atol=1e-5)

    def put(rows,encode,grid=None):
        start = (len(data)-32)//8
        constant = all(row==rows[0] for row in rows[1:])
        payload = b''.join(encode(row, 0 if constant else grid[i] if grid is not None else first+i)
                           for i,row in enumerate(rows[:1] if constant else rows))
        key = (encode, constant, payload)
        if sparse and key in encoded_curves:
            return encoded_curves[key]
        data.extend(payload)
        end = start if constant else (len(data)-32)//8
        require(end <= 65535,'转换后的关键帧超过原生 ANM 可寻址范围。')
        if sparse:
            encoded_curves[key] = start,end
        return start,end

    def rot_key(row,tick):
        raw = [round((value+math.pi)*denom/math.tau) for value in row]
        return struct.pack('<4H',*(min(65535,max(0,x)) for x in raw),tick)
    def vec_key(row,tick):
        return struct.pack('<4f',*row,tick)

    for index,track in enumerate(order):
        at = 40 + 14 * index
        if track not in bind.target.tracks:
            # Shared native clips carry extra tracks which this MDL never consumes.
            pointers = (*copy_curve(at, 0, 8), *copy_curve(at, 1, 16), *copy_curve(at, 2, 16))
            struct.pack_into('<6HBB', data, at, *pointers, track, reference[at + 13])
            continue
        dest = bind.target.bones[bind.target.tracks[track]]
        channels = animation['channels'].get(track) if track in bind.common else None
        if channels is None:
            angles,offsets,scales = ((tuple(dest[field]),)*len(times)
                                     for field in ('rotation','translation','scale'))
        else:
            node = bind.source.tracks[track]
            src = bind.source.bones[node]
            rot,pos,scale = (np.asarray(animation['tracks'][c]).reshape(-1,5 if i == 0 else 4)
                             for i,c in enumerate(channels))
            parent_t = bind.target.channel_parent_matrix(track, bind.common)
            parent_s = bind.source.channel_parent_matrix(track, bind.common)
            basis = np.linalg.solve(parent_t, parent_s)
            u, _, v = np.linalg.svd(basis)
            rot_times,pos_times,scale_times = samples(rot,True),samples(pos),samples(scale)
            orient = u @ v
            angles = [euler(orient @ quaternion_matrix(quaternion_at(rot, t))) for t in rot_times]
            orig_pos = np.asarray(src['translation'])
            offsets = [tuple(dest['translation']+basis@(vector_at(pos,t)-orig_pos)*bind.scale)
                       if track==0 or animation['localTranslation'] else tuple(dest['translation']) for t in pos_times]
            base = np.asarray(dest['scale'])/np.asarray(src['scale'])
            scales = [tuple(base*vector_at(scale,t)) if animation['localScale'] else tuple(dest['scale']) for t in scale_times]
        if channels is None:
            rotation = copy_curve(at, 0, 8)
            position = (put(offsets, vec_key) if track and local_position and not output['localTranslation']
                        else copy_curve(at, 1, 16))
            size = (put([(1., 1., 1.)], vec_key) if local_scale and not output['localScale']
                    else copy_curve(at, 2, 16))
        else:
            rotation = put(angles, rot_key, rot_times)
            position = put(offsets, vec_key, pos_times)
            size = put(scales, vec_key, scale_times)
        pointers = (*rotation, *position, *size)
        struct.pack_into('<6HBB',data,at,*pointers,track,reference[at + 13])
    data.extend(bytes(-len(data)%16))
    meta = len(data)
    old_meta = u32(reference,20)
    data.extend(reference[old_meta:])
    aux = meta+u32(reference,24)-old_meta
    struct.pack_into('<I',data,4,len(data))
    struct.pack_into('<3I',data,20,meta,aux,aux)
    # Use the native fallback to header +0x24 in both engines; the last sample is frames - 1.
    struct.pack_into('<H', data, 36, last + 1)
    struct.pack_into('<hhf', data, meta, first, -1, animation['rate'])
    if not ps2:
        data[meta+8:meta+10] = bytes((local_position, local_scale))
    return bytes(data)


@dataclass
class Move:
    role: int
    config: int
    shape: int
    state: int
    title: str
    native: dict
    animation: dict
    animation_key: tuple
    clip: bytes
    bom_key: tuple
    script: int
    skill: bool


def read_move(game,role,config,shape,state):
    selection = state
    phase = None
    if isinstance(selection, list):
        require(len(selection) == 2 and all(type(value) is int for value in selection),
                '请选择动作状态和连段片段。')
        state, phase = selection
    if is_fe3(game):
        w = game.workbench
        if phase is None:
            rows = w.action_rows(role)
            require(type(state) is int and 0 <= state < len(rows), '请选择有效的 FE3 动作状态。')
            animation = rows[state][1]
        else:
            animation = phase
        require(animation in w.binding(role),'该原生状态没有独立动画。')
        payload = w.animation(role,shape,animation)
        bank,index = w.binding(role)[animation]
        clip = single_clip(game.data(bank),index)[16:]
        skill = next((row for row in w.skills(role) if row['action']==state and row['menuMask']&(1<<shape)),None)
        name = skill['name'] if skill else f'ANM {animation}'
        return Move(role,config,shape,state,name,{},payload['animation'],(*bank,index),clip,
                    payload['bom'],animation,skill is not None)
    case,_,bom,_ = game.form_binding(role,config,shape)
    require(state in game.actions[role],'请选择有动画的奥0动作状态。')
    move = game.actions[role][state]
    playback = game.motion(role,config,shape,selection)
    key = playback['anm'],playback['slot']
    inputs = game.input_paths[role].get(state, ())
    name = playback['action']['name'] or SEQUENCE.join(inputs) or f"状态 {state} · ANM {playback['action']['clip']}"
    clip = game.asset_bytes('anm', key)
    return Move(role,config,shape,state,name,move,playback['animation'],key,clip,
                (bom['id'],None),playback['bom_clip'],move['category']==5)


def bom_data(game, key):
    return game.data(key) if is_fe3(game) else game.bom_content(key[0])


def native_bom(game, move, data=None):
    if data is None:
        data = bom_data(game,move.bom_key)
    if is_fe3(game):
        b = Bom(data)
        return [entry_bytes(b,i) for i in range(b.scripts+b.shapes)],b.scripts
    count,_,parts = bom_parts(data)
    return parts,count


def hit_records(game,move,parts,count):
    if not 0<=move.script<count or not parts[move.script]:
        return
    ps2 = is_fe3(game)
    layout = HIT_TEMPLATES[ps2]
    for record in event_records(parts[move.script]):
        raw = record.raw
        template = layout.get(raw[0])
        if (record.overlap or template is None or len(raw) < template + 2
                or not event_form(raw[1], move.shape, ps2)):
            continue
        node,span,damage = struct.unpack_from('<3h',raw,4)
        shape = read_shape(parts[count:],s16(raw,template),node,'attack')
        if shape and 'center' in shape:
            yield (record,shape,span,damage)


def event_form(mask, shape, ps2):
    return ((mask - 256 if ps2 and mask >= 128 else mask) >> shape) & 1


def cross_forms(game, role):
    if is_fe3(game):
        forms = [{'id':form.id, 'name':form.label}
                 for form in game.workbench.forms(role).values() if not form.accessory]
        return [{'id':0, 'name':'默认出场', 'forms':forms}]
    cases = selected(game.catalog()['roles'], role)['cases']
    return [{'id':i, 'name':'默认出场' if i == 0 else f"出场配置 {i} · YUM {case['group']}",
             'forms':[{'id':j,'name':f'形态 {j} · MDL {mdl}'} for j,mdl in enumerate(case['forms'])]}
            for i,case in enumerate(cases)]


def cross_choices(game, role, config, shape, mode, receiving):
    if mode == 'motion':
        return motion_choices(game, role, config, shape, receiving)
    if mode == 'hit':
        return attack_choices(game, role, config, shape, receiving)
    require(mode == 'skill', '请选择需要移植的动作类型。')
    ps2 = is_fe3(game)
    if ps2:
        work = game.workbench
        form = work.form(role,shape)
        finishers = {row['action']:row['name'] for row in work.skills(role)
                     if row['menuCandidate'] and row['menuMask'] & (1 << shape)}
        rows = work.action_rows(role)
        clips = work.binding(role)
        data = ((state, anim, finishers[state], clips[anim][0])
                for state, (_, anim, _) in enumerate(rows) if state in finishers and anim in clips)
        counts = Counter(row[1] for row in rows)
    else:
        case,_,bom,_ = game.form_binding(role,config,shape)
        rows = game.move_catalog(role,case,bom,shape)
        data = ((row['id'], row['clip'], SEQUENCE.join(row['inputs']) or row['name'], row['anm']) for row in rows
                if row['id'] >= 0 and row['last'] is not None and row['category'] == 5
                and not (row.get('followup') or row.get('encounter_variant') or row.get('rage')))
        counts = Counter(row['clip'] for row in game.actions[role].values())
    result = []
    for state,anim,title,key in data:
        if receiving:
            if counts[anim] != 1:
                continue
            if ps2:
                resources = game.transfer_plan.resources
                if key in resources or form.bom in resources or game.owners.get(key[0]) != role:
                    continue
            elif (role,state) in game.move_sets or (role,state) in game.installed_moves:
                continue
        result.append({'id':state, 'name':f'{state:03d} · {title or "ANM " + str(anim)}'})
    return result


def attack_choices(game, role, config, shape, receiving):
    if is_fe3(game):
        return fe3_attack_choices(game, role, shape, receiving)
    case, _, bom, _ = game.form_binding(role, config, shape)
    active = game.current_moves().states if game.move_sets else {}
    users = defaultdict(set)
    for native in game.actions[role].values():
        users[native['clip']].add(native['category'])
    options = []
    for row in game.move_catalog(role, case, bom, shape):
        state, clip = row['id'], row['clip']
        has_hits = any(hit['op'] in HIT_TEMPLATES[0] for hit in row['damage'])
        if (state < 0 or row['category'] != 4 or row['last'] is None or row['opponent'] is not None
                or row.get('encounter_variant') or row.get('rage') or (role, state) in active
                or not (has_hits or row['inputs'] or row['name'])):
            continue
        if receiving and ((role, state) in game.installed_moves or users[clip] - {4}):
            continue
        label = row['name'] or SEQUENCE.join(row['inputs']) or f'状态 {state:03d}'
        paths = row.get('combo_paths', [label])
        follow = row.get('followup', False)
        options.append({'id': [state, clip] if follow else state, 'name': paths[0] + f' · ANM {clip:03d}',
                        'paths': paths, 'combo': follow, 'clip': clip, 'has_hits': has_hits})
    return sorted(options, key=lambda row: (row['clip'], row['combo'], row['name']))


def fe3_attack_choices(game, role, shape, receiving):
    work = game.workbench
    form = work.form(role, shape)
    if not form.bom or form.accessory:
        return []
    profile = work.profile(role, shape)
    moves = {row['id']: row for row in profile['moves']}
    direct = {edge['to'] for entries in profile['commands'].values() for edge in entries
              if not edge['excluded'] & (1 << shape)}
    commanded = {edge['to'] for entries in profile['commands'].values() for edge in entries}
    skills = {row['action'] for row in profile['skills'] if row['menuMask'] & (1 << shape)}
    users = defaultdict(set)
    roots = defaultdict(list)
    categories = defaultdict(set)
    for state, (_, clip, _) in enumerate(work.action_rows(role)):
        if clip not in moves:
            continue
        category = work.tables.action_category(role, state)
        users[clip].add(category)
        # Boss ERXs may register damaging input states under their own category.
        if (state not in skills and (state not in commanded or state in direct)
                and (category == 4 or (state in direct and moves[clip]['damage']))):
            roots[clip].append(state)
            categories[clip].add(category)
    found = set(roots)
    pending = deque(roots)
    while pending:
        for edge in profile['combos'].get(pending.popleft(), ()):
            clip = edge['target']
            if clip not in found:
                found.add(clip)
                pending.append(clip)
    options = []
    resources = game.transfer_plan.resources
    for clip in sorted(found):
        move = moves[clip]
        if not (move['damage'] or move['keys'] or move['combo']):
            continue
        if receiving:
            key = tuple(move['asset'][:2])
            if (users[clip] - (categories[clip] or {4}) or key in resources or form.bom in resources
                    or game.owners.get(key[0]) != role):
                continue
        paths = move['keys'] or [move['name']]
        for state in roots.get(clip, (None,)):
            follow = state is None
            label = move['name'] if not follow and f'状态 {state}）' in move['name'] else paths[0]
            options.append({'id': [-1, clip] if follow else state, 'name': label + f' · ANM {clip:03d}',
                            'paths': paths, 'combo': follow, 'clip': clip, 'has_hits': bool(move['damage'])})
    return options


def motion_choices(game, role, config, shape, receiving):
    """Use the native state category and verify contact opcodes on its ANM."""
    ps2 = is_fe3(game)
    excluded = set()
    if ps2:
        work = game.workbench
        form = work.form(role, shape)
        states = work.action_rows(role)
        binds = work.binding(role)
        native = [(state, clip, work.tables.action_category(role, state)) for state, (_, clip, _) in enumerate(states)
                  if clip in binds]
        skill_states = {item.action for kind in range(3) for item in work.tables.battle_skill_rows(role, kind)
                        if item.menu_mask & (1 << shape)}
        bank = Bom(game.data(form.bom)) if form.bom else None
        attacks = {clip for clip in {clip for _, clip, _ in native} if bank and clip < bank.scripts
                   and any(row['op'] in (0, 1, 24) and event_form(row['selector'], shape, True)
                           for row in bank.events(clip, parameters=False))}
        commands, names = work.contextual_commands(role, shape)
        names = names.copy()
        for origin in (0, 1):
            for edge in commands.get(origin, ()):
                if not edge['excluded'] & (1 << shape):
                    names.setdefault(edge['to'], edge['usage'])
        locked = game.transfer_plan.resources
        writable = {clip for clip, (key, _) in binds.items() if game.owners.get(key[0]) == role and key not in locked}
    else:
        case, _, bom, _ = game.form_binding(role, config, shape)
        native = [(state, move['clip'], move['category']) for state, move in game.actions[role].items()]
        names = {**THROWS, **{state: paths[0] for state, paths in game.input_paths[role].items() if paths},
                 **{state: input_arrows(label) for state, label in KGF_MOTION_NAMES.items()}}
        skill_states = set()
        attacks = {clip for _, clip, _ in native if game.damage_records(bom['id'], clip, shape)}
        states = game.current_moves().states if game.move_sets else game.installed_moves
        excluded = {state for owner, state in states if owner == role}
        writable = set()
        for _, clip, category in native:
            if category in COMBAT_CATEGORIES or clip in attacks:
                continue
            bank, local, opponent = game.anim_bank(case, clip)
            if opponent is None and game.animation_last(bank, local) is not None:
                writable.add(clip)

    eligible = {state for state, clip, category in native if category not in COMBAT_CATEGORIES
                and state not in skill_states and state not in excluded and clip not in attacks}
    counts = Counter(clip for _, clip, _ in native)
    names_for_clip = {}
    result = []
    for state,clip,category in native:
        if state not in eligible:
            continue
        names_for_clip.setdefault(clip, []).append(state)
        if clip in writable or ps2 and not receiving:
            label = names.get(state) or {0:'移动或过渡', 1:'下蹲', 2:'跳跃', 3:'防御',
                                         6:'受击', 7:'倒地', 12:'演出'}.get(category, '非攻击动作')
            result.append({'id':state, 'clip':clip, 'name':f'{state:03d} · {label} · ANM {clip:03d}'})
    for row in result:
        shared = names_for_clip[row['clip']]
        if len(shared) > 1:
            row['name'] += ' · 共用状态 ' + '/'.join(f'{i:03d}' for i in shared)
    return [row for row in result if not receiving or counts[row['clip']] == len(names_for_clip[row['clip']])]


def pack_bom(parts,scripts,ps2):
    size = 12+4*len(parts)
    offsets = []
    filled = bytearray(size)
    for raw in parts:
        if not raw and not ps2:
            offsets.append(0xFFFFFFFF)
            continue
        offsets.append(len(filled))
        filled.extend(raw)
        filled.extend(bytes(-len(filled)%4))
    struct.pack_into(f'<4s{len(parts)+2}I',filled,0,b'BOM\0',scripts,len(parts)-scripts,*offsets)
    data = bytes(filled)
    (Bom(data).validate() if ps2 else bom_parts(data))
    return data


def replace_form_rows(target, dest, parts, count, remove, additions):
    """Split shared selectors when replacing only the selected form's records."""
    ps2 = is_fe3(target)
    original = parts[dest.script]
    records = list(event_records(original))
    selector = 1 << min(dest.shape, 7)
    rows = merge_form_events(((r.raw,bool(r.raw[1] & selector) and remove(r)) for r in records),selector,additions)
    if rows is None:
        return None
    require(not original or (len(records) == u32(original,0) and not any(r.overlap for r in records)),
            '目标 BOM 记录共用字节，无法独立改写。')
    if ps2 and dest.shape >= 7:
        require(not any(f.id != dest.shape and f.id >= 7 and f.bom == dest.bom_key
                        for f in target.workbench.forms(dest.role).values()), '目标多个形态共用同一 BOM 高位。')
    parts[dest.script] = (script_bytes if ps2 else pack_script)(rows)
    return pack_bom(parts, count, ps2)


def shared_scripts(game,dest):
    users = {(dest.bom_key,dest.shape)} if dest.bom_key else set()
    if is_fe3(game):
        users.update((form.bom,form.id) for form in game.workbench.forms(dest.role).values()
                     if not form.accessory and form.bom)
    else:
        for actor, role in enumerate(game.roles):
            for config, case in enumerate(role['cases']):
                if game.anim_bank(case,dest.script)[:2] == dest.animation_key:
                    for shape in range(len(case['forms'])):
                        bom = game.form_binding(actor,config,shape)[2]['id']
                        users.add(((bom,None),shape))
    return sorted(users)


def retime_target_rows(game, dest, first, last, target_data, info):
    lo, hi = sample_span(dest.animation)
    if (lo, hi) == (first, last) or not dest.bom_key:
        return None
    parts, count = native_bom(game, dest, target_data)
    if not 0 <= dest.script < count:
        return None
    ps2 = is_fe3(game)
    mask = bytes([1 << min(dest.shape,7)])
    rows = [r.raw[:1]+mask+r.raw[2:] for r in event_records(parts[dest.script])
            if event_form(r.raw[1],dest.shape,ps2)]
    shifted = info.remap((dest.script,dest.shape),rows,(lo,hi),(first,last),ps2)
    return replace_form_rows(game, dest, parts, count, lambda _:True, shifted)


def transfer_hits(source,from_move,target,to_move,binding,target_data=None,info=None):
    donor,ns = native_bom(source,from_move)
    parts,nt = native_bom(target,to_move,target_data)
    if not 0 <= from_move.script < ns or not 0 <= to_move.script < nt:
        return None,0
    stock = list(hit_records(target,to_move,parts,nt))
    original = list(hit_records(source,from_move,donor,ns))
    raw_hits = [r for r in event_records(donor[from_move.script]) if r.raw[0] in HIT_TEMPLATES[is_fe3(source)]
                and event_form(r.raw[1],from_move.shape,is_fe3(source))]
    require(len(original) == len(raw_hits),'来源攻击使用了无法转换的形状或重叠记录。')
    if not original and not stock:
        return None,0
    start,end = sample_span(from_move.animation)
    lo,hi = sample_span(to_move.animation)
    require(start<=end and lo<=hi,'双方动作须有可转换的播放区间。')
    speed = (hi-lo)/(end-start) if end>start else 1
    require(0 <= to_move.shape < (7 if is_fe3(target) else 8), '目标形态无法在 BOM 中使用独立掩码。')
    selector = 1<<to_move.shape
    layout = HIT_TEMPLATES[is_fe3(target)]
    if info is None:
        info = BomTiming(bom_data(target,to_move.bom_key))
    refs = Counter()
    if info.templates or any(record.raw[1] == selector for record, *_ in stock):
        offsets = ({op:4 + 2*i for op,i in SHAPE_FIELDS.items()}
                   if is_fe3(target) else SHAPE_TEMPLATE_OFFSETS)
        for body in parts[:nt]:
            if len(body) < 4 or 4 + 4 * u32(body, 0) > len(body):
                continue
            for record in event_records(body):
                at = offsets.get(record.raw[0])
                if at is not None and len(record.raw) >= at + 2:
                    refs[s16(record.raw, at)] += 1
    unused = deque(sorted(index for index in info.templates if not refs[index]))
    geometry_changed = False
    new_hits = []
    for index,(rec,geometry,span,damage) in enumerate(original):
        frame = s16(rec.raw,2)
        if not start<=frame<=end:
            continue
        node,shape = binding.shape(geometry)
        require(node >= 0 or not is_fe3(target), '奥3攻击需要实际模型节点作为判定挂点。')
        t = min(hi,max(lo,round(lo+(frame-start)*speed)))
        duration = min(32767,max(1 if span > 0 else 0,round(lo+(frame+span-start)*speed)-t))
        # Both handlers copy the same halfword fields. The extended opcode is 0x18 on EE and 5 on PSP.
        extended = rec.raw[0] != 0
        size = 36 if extended else 24
        require(len(rec.raw) >= size - (2 if extended else 0), '来源攻击记录不完整。')
        base = bytearray(rec.raw[:size].ljust(size,b'\0'))
        base[0] = (24 if is_fe3(target) else 5) if extended else 0
        at = layout[base[0]]
        old = stock[index][0].raw if index < len(stock) else None
        template = s16(old, layout[old[0]]) if old is not None else -1
        isolated = old is not None and old[1] == selector and refs[template] == 1
        if not isolated or (template not in info.templates and parts[nt + template] != shape):
            if unused:
                template = unused.popleft()
            else:
                template = len(parts)-nt
                require(template <= 32767, '接收判定模板已超出原生编号范围。')
                parts.append(shape)
                info.templates.add(template)
        if parts[nt + template] != shape:
            parts[nt + template] = shape
            geometry_changed = True
        base[1] = selector
        struct.pack_into('<4h',base,2,t,node,duration,damage)
        struct.pack_into('<h',base,at,template)
        new_hits.append(bytes(base))
    data = replace_form_rows(target,to_move,parts,nt,lambda rec:rec.raw[0] in layout,new_hits)
    if data is None and geometry_changed:
        data = pack_bom(parts,nt,is_fe3(target))
    return data,len(new_hits)


def transfer_hands(source, origin, target, dest, binding, target_data=None):
    if not origin.bom_key or not dest.bom_key:
        return None, 0, ()
    source_ps2, target_ps2 = is_fe3(source), is_fe3(target)
    source_op, target_op = (18 if source_ps2 else 16), (18 if target_ps2 else 16)
    source_parts, count = native_bom(source, origin)
    if not 0 <= origin.script < count:
        return None, 0, ()
    first, last = sample_span(origin.animation)
    lo, hi = sample_span(dest.animation)
    requests = []
    for record in event_records(source_parts[origin.script]):
        raw = record.raw
        if not record.overlap and len(raw) >= 6 and raw[0] == source_op and event_form(raw[1], origin.shape, source_ps2):
            frame = s16(raw, 2)
            if frame <= last:
                requests.append((frame, s16(raw, 4)))
    parts, scripts = native_bom(target, dest, target_data)
    if not 0 <= dest.script < scripts:
        return None, 0, ()
    original = parts[dest.script]
    records = list(event_records(original))
    if not requests and not any(r.raw[0] == target_op and event_form(r.raw[1], dest.shape, target_ps2)
                                for r in records):
        return None, 0, ()

    donor = fe3_hand_clouds(source, origin, binding.source, False) if source_ps2 else psp_hand_clouds(source, origin)
    receiver = fe3_hand_clouds(target, dest, binding.target, True) if target_ps2 else psp_hand_clouds(target, dest)
    mapping = match_hand_events(donor, receiver, source_ps2 == target_ps2)
    changed_sides = {side for choices in mapping.values() for side, slot in choices}
    if not changed_sides:
        return None, 0, ()
    timeline = {}
    for frame, choice in sorted(requests, key=lambda item: item[0]):
        tick = lo if last == first else round(lo + (max(first, frame) - first) * (hi - lo) / (last - first))
        for side, slot in mapping.get(choice, ()):
            timeline[tick, side] = slot
    mask = 1 << min(dest.shape, 7)
    side_for = {key:side for side, group in enumerate(receiver) for key in group}
    if not target_ps2:
        side_for = {slot:slot // 6 for slot in range(12)}
    replaced_slots = {slot for slot,side in side_for.items() if side in changed_sides}
    existing = []
    for record in records:
        raw = record.raw
        if len(raw) >= 6 and raw[0] == target_op and raw[1] & mask:
            slot = s16(raw, 4)
            side = side_for.get(slot)
            if side in changed_sides:
                existing.append(((s16(raw, 2), side), slot))
    if len(existing) == len(timeline) and dict(existing) == timeline:
        return None, len(timeline), replaced_slots
    additions = [struct.pack('<BBhhH',target_op,mask,tick,slot,0)
                 for (tick,_),slot in sorted(timeline.items())]
    def is_hand(record):
        return len(record.raw) >= 6 and record.raw[0] == target_op and s16(record.raw,4) in replaced_slots
    data = replace_form_rows(target,dest,parts,scripts,is_hand,additions)
    return data, len(timeline), replaced_slots


def selected_voice(bank, voice):
    if isinstance(voice,dict) and 'recording' in voice:
        return bank.store(voice['recording'],True)
    if isinstance(voice,dict):
        side,chosen = voice.get('side'),voice.get('cue')
        library = bank.origin if side == 'source' else bank.receiver
        require(side in ('source','target') and chosen in library.voices(),'请选择此角色音库中的战吼。')
        return bank.resolve(chosen) if side == 'source' else chosen
    require(voice in ('source','target','mute'),'请选择有效的战吼模式。')
    return None


def transfer_voice_only(source, origin, target, dest, voice, same_move):
    if voice == 'target' or (same_move and voice == 'source'):
        return None,None,0
    require(dest.bom_key,'目标动作没有可编辑的声音脚本。')
    parts,count = native_bom(target,dest)
    require(0 <= dest.script < count,'目标动作没有可编辑的声音脚本。')
    donor = SoundLibrary(source,origin.role,origin.config)
    receiver = SoundLibrary(target,dest.role,dest.config)
    bank = SoundTransfer(donor,receiver)
    fixed = selected_voice(bank,voice)
    op = 16 if receiver.ee else 14
    mask = 1 << min(dest.shape,7)
    first,last = sample_span(dest.animation)
    def vocal(raw, library, opcode):
        return (len(raw) >= 8 and raw[0] in (opcode,opcode+1)
                and 0 <= s16(raw,4) < len(library.codes) and library.voice(s16(raw,4)))
    rows = []
    if fixed is not None:
        for record in event_records(parts[dest.script]):
            raw = record.raw
            if event_form(raw[1],dest.shape,receiver.ee) and vocal(raw,receiver,op):
                rows.append(raw[:1] + bytes([mask]) + raw[2:4] + struct.pack('<h',fixed) + raw[6:])
        if not rows:
            ticks = [s16(r.raw,2) for r,*_ in hit_records(target,dest,parts,count) if first <= s16(r.raw,2) <= last]
            rows.append(struct.pack('<BBhhh',op,mask,min(ticks,default=first),fixed,0))
    elif voice == 'source':
        require(origin.bom_key,'来源动作没有可读取的声音脚本。')
        source_parts,n = native_bom(source,origin)
        require(0 <= origin.script < n,'来源动作没有可读取的声音脚本。')
        source_op = 16 if donor.ee else 14
        lo,hi = sample_span(origin.animation)
        binding = None
        for record in event_records(source_parts[origin.script]):
            raw = record.raw
            if not event_form(raw[1],origin.shape,donor.ee) or not vocal(raw,donor,source_op):
                continue
            tick = s16(raw,2)
            if not lo <= tick <= hi:
                continue
            at = first + round((tick-lo)*(last-first)/max(1,hi-lo))
            gated = raw[0] == source_op+1
            node = 0
            if gated:
                if binding is None:
                    binding = Retarget(skeleton(source,origin.role,origin.config,origin.shape),
                                       skeleton(target,dest.role,dest.config,dest.shape))
                node = binding.bone(s16(raw,6))
            rows.append(struct.pack('<BBhhh',op+gated,mask,at,bank.resolve(s16(raw,4)),node))
    data = replace_form_rows(target,dest,parts,count,lambda r:vocal(r.raw,receiver,op),rows)
    return data,bank,len(rows)


def transfer_signals(source, origin, target, dest, binding, target_data, voice):
    """Transcode rate and sound events on the same clock as the imported poses."""
    if not origin.bom_key or not dest.bom_key:
        return None, None, 0
    ee, out_ee = is_fe3(source), is_fe3(target)
    source_ops = (14,16,17) if ee else (12,14,15)
    dest_ops = (14,16,17) if out_ee else (12,14,15)
    src, src_count = native_bom(source,origin)
    parts, count = native_bom(target,dest,target_data)
    if origin.script >= src_count or dest.script >= count:
        return None, None, 0
    lib = SoundLibrary(source,origin.role,origin.config)
    receiver = SoundLibrary(target,dest.role,dest.config)
    bank = SoundTransfer(lib,receiver)
    mask = 1 << min(dest.shape,7)
    first,last = sample_span(origin.animation)
    added = []
    voices = []
    fixed = selected_voice(bank,voice)
    n_audio = 0
    for record in event_records(src[origin.script]):
        raw = record.raw
        if record.overlap or raw[0] not in source_ops or not event_form(raw[1],origin.shape,ee):
            continue
        tick = s16(raw,2)
        if tick > last:
            continue
        op = source_ops.index(raw[0])
        if op == 0:
            multiplier, = struct.unpack_from('<f',raw,4)
            require(math.isfinite(multiplier) and multiplier > 0,'来源 BOM 播放倍率无效。')
            added.append(struct.pack('<BBhf',dest_ops[0],mask,max(first,tick),multiplier))
            continue
        if tick < first:
            continue
        cue = s16(raw,4)
        require(0 <= cue < len(lib.codes),'来源 BOM 声音编号无效。')
        vocal = lib.voice(cue)
        if vocal:
            voices.append(tick)
            if voice in ('mute','target'):
                continue
        node = binding.bone(s16(raw,6)) if op == 2 else 0
        converted = fixed if vocal and fixed is not None else bank.resolve(cue)
        added.append(struct.pack('<BBhhh',dest_ops[op],mask,tick,converted,node))
        n_audio += 1
    if fixed is not None and not voices:
        hits = [s16(rec.raw,2) for rec,*_ in hit_records(source,origin,src,src_count)
                if first <= s16(rec.raw,2) <= last]
        added.append(struct.pack('<BBhhh',dest_ops[1],mask,min(hits,default=first),fixed,0))
        n_audio += 1
    def remove(rec):
        if rec.raw[0] not in dest_ops:
            return False
        if voice == 'target' and rec.raw[0] != dest_ops[0]:
            cue = s16(rec.raw,4)
            if 0 <= cue < len(receiver.codes) and receiver.voice(cue):
                return False
        return True
    return replace_form_rows(target,dest,parts,count,remove,added), bank, n_audio


def commit(target,edits,bom_members=(),sound_bank=None):
    target.ensure_open()
    before = target.snapshot()
    if is_fe3(target):
        target.asset_edits = target.asset_edits | edits
        if sound_bank is not None:
            target.sound_extensions = sound_bank.extras
    else:
        target.asset_edits = target.asset_edits | edits
        if sound_bank is not None:
            target.sound_extensions = sound_bank.extras
            target.sound_members = sound_bank.members
            target.sound_bank.cache_clear()
        target.template_edits = {key:raw for key,raw in target.template_edits.items() if key[0] not in bom_members}
        target.edits = {key:raw for key,raw in target.edits.items() if key[0] not in bom_members}
        target.clear_bank_cache()
        target.texture.cache_clear()
        target.node_count.cache_clear()
    target.remember(before)


def transferred_clip(target, dest, origin, binding, mode):
    clip = pack_animation(dest.clip,origin.animation,dest.animation,binding,is_fe3(target))
    if is_fe3(target):
        key,index = dest.animation_key[:2],dest.animation_key[2]
        wrapped = struct.pack('<4s3I',b'ANM\0',1,16,0)+clip
        return key,target.validate_import(target.part(*key), replace_clip(target.data(key),index,wrapped))
    key = dest.animation_key
    if mode in ('hit','motion'):
        target._check_animation(key,clip)
    else:
        validate_clip(clip, set(binding.target.tracks).union(original_order(dest.clip)))
    if key[0] in STORY_BANKS:
        protect_story_clip(read_anm_bank(target.read_source_member(key[0],b'ANM\0'))[1],len(clip),key[1])
    return key,clip


def transfer_move(target,source,receiver,donor,mode,voice='source',voice_only=False):
    require(mode in ('hit','skill','motion'),'请选择普通攻击、必杀或非攻击动作。')
    source.ensure_open()
    titles = []
    if mode in ('hit', 'motion'):
        for game, selection, receiving in ((source, donor, False), (target, receiver, True)):
            options = cross_choices(game, *selection[:3], mode, receiving)
            selected_move = next((row for row in options if row['id'] == selection[3]), None)
            require(selected_move is not None, '请选择当前形态可替换的攻击、连段片段或非攻击动作。')
            titles.append(selected_move['name'])
    else:
        require(type(receiver[3]) is int and type(donor[3]) is int, '请选择必杀技的原生状态。')
    origin = read_move(source,*donor)
    dest = read_move(target,*receiver)
    require(mode!='hit' or not dest.skill,'演出必杀请走必杀动作移植，保留目标引擎结算。')
    require(mode!='skill' or dest.skill and origin.skill,'请选择双方角色的必杀状态。')
    for game, move in ((source, origin), (target, dest)):
        if mode == 'hit' and not is_fe3(game):
            require(move.native['category'] == 4,'请选择普通攻击状态。')
    same_move = (tuple(receiver[:3]) == tuple(donor[:3]) and origin.animation_key == dest.animation_key
                 and origin.bom_key == dest.bom_key and origin.script == dest.script
                 and (source is target or (source.path == target.path and source.snapshot() == target.snapshot())))
    voice_only = voice_only or same_move
    # Only a skill must have an independent state clip. Attack choices are
    # checked against every known state and use the selected script directly.
    if is_fe3(target):
        require(voice_only or mode != 'skill' or sum(x[1]==dest.script for x in target.workbench.action_rows(dest.role))==1,
                '目标动画被多个原生状态共用，请选择独立动作。')
        locks = target.transfer_plan.resources
        require(target.owners.get(dest.animation_key[0]) == dest.role,
                '目标动画库被其他角色共用。')
        require((voice_only or dest.animation_key[:2] not in locks) and (mode == 'motion' or dest.bom_key not in locks),
                '这个目标使用当前工程的完整必杀配招，请先完成该条配招。')
    else:
        slot = target.actions[dest.role][dest.state]['clip']
        require(voice_only or mode != 'skill' or sum(x['clip']==slot for x in target.actions[dest.role].values())==1,
                '目标动画被多个原生状态共用，请选择独立动作。')
        require((dest.role,dest.state) not in target.move_sets and (dest.role,dest.state) not in target.installed_moves,
                '目标状态已有奥0原生配招，请选择另一状态。')
    changes = {}
    originals, timing = {}, {}
    timed_shapes = defaultdict(set)
    def info_for(key):
        if key not in timing:
            originals[key] = bom_data(target,key)
            timing[key] = BomTiming(originals[key])
        return timing[key]
    event_count, hand_count, hand_slots = 0,0,()
    if voice_only:
        first,last = sample_span(dest.animation)
        signal_data,sound_bank,sound_count = transfer_voice_only(source,origin,target,dest,voice,same_move)
    else:
        binding = Retarget(skeleton(source,*donor[:3]),skeleton(target,*receiver[:3]))
        key,clip = transferred_clip(target,dest,origin,binding,mode)
        changes[key] = clip
        first,last = sample_span(origin.animation)
        if sample_span(dest.animation) != (first,last):
            for key,shape in shared_scripts(target,dest):
                info = info_for(key)
                timed_shapes[key].add(shape)
                timed_bom = retime_target_rows(target,replace(dest,bom_key=key,shape=shape),first,last,
                                              changes.get(key,originals[key]),info)
                if timed_bom is not None:
                    changes[key] = timed_bom
        dest = replace(dest,animation={**dest.animation,'first':first,'last':last})
        if mode in ('hit','skill') and dest.bom_key and origin.bom_key:
            data,event_count = transfer_hits(source,origin,target,dest,binding,changes.get(dest.bom_key),info_for(dest.bom_key))
            if data is not None:
                changes[dest.bom_key] = data
        data,hand_count,hand_slots = transfer_hands(source,origin,target,dest,binding,changes.get(dest.bom_key))
        if data is not None:
            changes[dest.bom_key] = data
        signal_data,sound_bank,sound_count = transfer_signals(source,origin,target,dest,binding,changes.get(dest.bom_key),voice)
    if signal_data is not None:
        changes[dest.bom_key] = signal_data
    if dest.bom_key in changes:
        info_for(dest.bom_key)
        timed_shapes[dest.bom_key].add(dest.shape)
    ps2 = is_fe3(target)
    def imported(raw):
        if voice_only:
            if sound_bank is None or raw[0] not in ((16,17) if ps2 else (14,15)):
                return False
            cue = s16(raw,4)
            return cue >= len(sound_bank.receiver.codes) or (cue >= 0 and sound_bank.receiver.voice(cue))
        if mode in ('skill','hit') and raw[0] in HIT_TEMPLATES[ps2]:
            return True
        if raw[0] == (18 if ps2 else 16):
            return s16(raw,4) in hand_slots
        if sound_bank is not None and raw[0] in ((14,16,17) if ps2 else (12,14,15)):
            if voice == 'target' and raw[0] != (14 if ps2 else 12):
                cue = s16(raw,4)
                return not (0 <= cue < len(sound_bank.receiver.codes) and sound_bank.receiver.voice(cue))
            return True
        return False

    for key,info in timing.items():
        data = changes.get(key,originals[key])
        anchored_shapes = [shape for shape in timed_shapes[key] if (dest.script,shape) in info.anchors]
        if anchored_shapes:
            parts,_ = native_bom(target,replace(dest,bom_key=key),data)
            records = list(event_records(parts[dest.script]))
            for shape in anchored_shapes:
                mask = bytes([1<<min(shape,7)])
                rows = [None if key == dest.bom_key and shape == dest.shape and imported(r.raw)
                        else r.raw[:1]+mask+r.raw[2:] for r in records if event_form(r.raw[1],shape,ps2)]
                info.retain((dest.script,shape),rows,(first,last),ps2)
        data = info.write(data)
        if data != originals[key]:
            changes[key] = data
    bom_keys = [k for k,v in changes.items() if v.startswith(b'BOM\0')]
    if is_fe3(target):
        merged = ()
        for key in bom_keys:
            require(key not in target.transfer_plan.resources, '目标脚本正由本工程的完整必杀配招管理。')
            changes[key] = target.validate_import(target.part(*key),changes[key])
    else:
        merged = tuple(key[0] for key in bom_keys)
    if sound_bank and sound_bank.bank_edit is not None:
        lib = sound_bank.receiver
        if lib.ee:
            bank_id = lib.actor_banks[0]
            head, body = target.workbench.tables.at(0x42F5B8+32*bank_id,'<2i')
            end = lib.bank(bank_id).start
            changes[head,-1], changes[body,-1] = sound_bank.bank_edit[:end],sound_bank.bank_edit[end:]
        else:
            changes[lib.member,None] = sound_bank.bank_edit
    if changes:
        commit(target,changes,merged,sound_bank)
    source_name, target_name = titles or (origin.title, dest.title)
    status = ' · 仅处理战吼' if voice_only else (' · 来源无直接攻击判定' if mode == 'hit' and not event_count else '')
    if hand_count:
        status += f' · 匹配 {hand_count} 次手型切换'
    return {'name': target_name + ' · 来源 ' + source_name + status, 'hits': event_count, 'hands': hand_count, 'sounds':sound_count,
            'animation': dest.script if is_fe3(target) else receiver[3], 'voiceOnly':bool(voice_only), 'edits': target.edit_status()}

