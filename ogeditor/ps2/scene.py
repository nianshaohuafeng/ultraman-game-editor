"""Resolve FE3 battle scenes against the active ELF, ERX and FUM resources."""

import math
import struct

from ogeditor.battle_camera import camera_curve, elapsed_frames
from ogeditor.ps2.formats import require
from ogeditor.ps2.movie import BattleFilm


SCENE_OPS = {
    0: '演员显隐', 1: '模型节点显隐', 2: '原版 IPU 影片', 3: '原版 IPU 影片', 4: '开始播放影片',
    5: '暂停或恢复影片', 6: '停止播放影片', 7: '影片跳转',
    8: '切换演员动作', 9: '按命中结果切换受身', 10: 'CMSP 镜头', 11: '切回双方镜头',
    12: '恢复对战镜头', 13: '设置镜头参考演员', 15: '命中结果分支',
    16: '音效', 17: '持续音效', 18: '停止音效', 19: '音效控制',
    24: '演出时间跳转', 27: '演出结束', 28: '条件结束', 29: '演出结束',
    30: '切换 ANM', 32: '切换 ANM',
}
PAIRED_STATES = {112:113, 113:112, 120:121, 121:120, 122:123, 123:122}
SCENE_END = (27, 28, 29)
SCENE_BEGIN = (0x4D, 0x24000012)


def scene_address(workbench, role, action):
    # The local enter callback passes a relocated .data pointer in a1 to a
    # role-local setup function whose tail calls the EE battle-scene initializer.
    program = workbench.program(role)
    entry = workbench.callbacks(role, action)[1]['erx']
    try:
        head = program.at(entry, '<7I', b'.text')
    except ValueError:
        return None
    if (head[1] >> 16, head[3] >> 16, head[5] >> 26) != (0x3C05, 0x24A5, 2):
        return None
    setup = (head[5] & 0x3FFFFFF) << 2
    _, _, _, start, _, size, *_ = program.sections[b'.text']
    for at in range(setup, min(setup + 0xC0, start + size - 7), 4):
        opcode, = program.at(at, '<I', b'.text')
        if opcode >> 26 != 2:
            continue
        destination = (opcode & 0x3FFFFFF) << 2
        if not start <= destination <= start + size - 8:
            continue
        trap, ordinal = program.at(destination, '<2I', b'.text')
        if (trap, ordinal) == SCENE_BEGIN:
            return (head[1] & 65535) * 65536 + (head[3] & 32767) - (head[3] & 32768)
    return None


def scene_argument(program, entry, seen, incoming=None):
    """Recover the a1 argument at the original ERX scene initializer."""
    if entry in seen:
        return None
    seen.add(entry)
    start, _, size = program.region(entry, 4, b'.text')
    values = {0:0, 5:incoming}

    def step(word):
        op, rs, rt = word >> 26, (word >> 21) & 31, (word >> 16) & 31
        immediate = word & 65535
        if op == 15:
            values[rt] = immediate << 16
        elif op in (9, 13):
            value = values.get(rs)
            if value is None:
                values[rt] = None
            elif op == 9:
                values[rt] = value + (immediate & 32767) - (immediate & 32768)
            else:
                values[rt] = value | immediate
        elif op == 0 and word & 63 in (0x21, 0x25, 0x2D):
            values[(word >> 11) & 31] = values.get(rs or rt) if not (rs and rt) else None
        elif op in (32, 33, 35, 36, 37, 39, 55):
            values[rt] = None
        values[0] = 0

    for address in range(entry, min(entry + 0x200, start + size - 7), 4):
        word, delay = program.at(address, '<2I', b'.text')
        op = word >> 26
        if op in (2, 3):
            step(delay)
            destination = (word & 0x3FFFFFF) << 2
            if start <= destination <= start + size - 8:
                trap = program.at(destination, '<2I', b'.text')
                if trap == SCENE_BEGIN:
                    return values.get(5)
                if op == 2 and trap[0] != 0x4D:
                    return scene_argument(program, destination, seen, values.get(5))
            if op == 2:
                break
            for register in (*range(1,16), 24, 25):
                values.pop(register, None)
        elif word == 0x03E00008:
            break
        else:
            step(word)
    return None


def preview_scene_address(workbench, role, action):
    address = scene_address(workbench, role, action)
    if address is not None:
        return address
    program = workbench.program(role)
    entry = workbench.callbacks(role, action)[1]['erx']
    address = scene_argument(program, entry, set())
    if address:
        try:
            program.region(address,32)
        except ValueError:
            return None
        return address
    return None


def script_rows(program, address, *, branches=False, patch=None):
    try:
        start,_,extent = program.region(address,32)
    except ValueError:
        return []
    seen, rows = 0.0, []
    end = min(address + (len(patch) if patch else 0x8000), start + extent - 31)
    for at in range(address, end, 32):
        decoded = struct.unpack_from('<fi6i', patch, at-address) if patch else program.at(at, '<fi6i')
        clock, op, *args = decoded
        if not math.isfinite(clock) or not seen <= clock < 100000 or not 0 <= op <= 126:
            break
        rows.append((clock, op, args, at))
        seen = clock
        if not branches and op in SCENE_END:
            return rows
    if branches and rows and any(op in SCENE_END for _,op,_,_ in rows):
        seeks = [to for _,op,args,_ in rows for to in (args[1:5] if op == 15 else args[:1] if op == 24 else [])]
        if rows[-1][1] in (26, *SCENE_END) and all(0 <= to <= seen for to in seeks):
            return rows
    return []


def timeline(rows):
    # 0x21A210 dispatches records in [oldClock,newClock). 0x21A338 resets
    # newClock exactly to the jump target, so its record runs next update.
    clock,elapsed = 0,0
    for native,op,args,address in rows:
        if native < clock:
            continue
        elapsed += native-clock
        yield elapsed,native,op,args,address
        clock = native
        if op in SCENE_END:
            break
        if op == 24 and args[0] > native:
            clock = args[0]
            elapsed += 1


def camera_bank(workbench, role):
    table = workbench.tables.pointer(0x40A908, role)
    return workbench.tables.pointer(table, 0)


def camera_choices(workbench, role):
    group = camera_bank(workbench,role)
    game = workbench.game
    return [{'id':part.member,'name':part.name.rsplit('/',1)[-1]}
            for part in game.children[group] if part.kind == 'cmsp'] + [
            {'id':member,'name':name.rsplit('/',1)[-1]}
            for (resource,member),(name,_) in game.transfer_plan.cameras.items() if resource == group]


def camera(workbench, role, member):
    game = workbench.game
    key = camera_bank(workbench, role), member
    appended = game.transfer_plan.cameras.get(key)
    if appended is not None:
        name,data = appended
        return camera_curve(data,key=key,name=name.rsplit('/',1)[-1])
    part = game.parts.get(key)
    if part is None or part.kind != 'cmsp':
        return None
    return camera_curve(game.data(key), key=key, name=part.name.rsplit('/', 1)[-1])


def retarget_payload(workbench, motion, source, target):
    from ogeditor.ps2.preview import motion as decode_motion
    from ogeditor.ps2.transfer import matched_rigs, retarget_clip

    source_form,target_form = workbench.form(*source),workbench.form(*target)
    start,end = matched_rigs(workbench.game,source_form,target_form)
    resource,member,index = motion['anm']
    order = tuple(motion['animation']['channels'])
    incoming = retarget_clip(workbench.game.data((resource,member)),index,order,start,end)
    animation = decode_motion(incoming,0)
    animation['id'] = motion['animation']['id']
    source_nodes = dict(source_form.tracks)
    target_nodes = {channel:node for node, channel in target_form.tracks}
    events = []
    for event in motion['events']:
        mapped = {key:value for key,value in event.items() if key != 'morph'}
        if volume := mapped.get('shape'):
            node = target_nodes.get(source_nodes.get(volume['node']))
            mapped['shape'] = {**volume, 'node':node} if node is not None else None
        events.append(mapped)
    return {**motion,'animation':animation,'events':events}


def add_clip(tracks,workbench,source,target,number,frame,state=None,rate_scale=1.0):
    clip = workbench.animation(*source,number)
    if source != target:
        clip = retarget_payload(workbench,clip,source,target)
    if rate_scale != 1.0:
        clip = {**clip,'animation':{**clip['animation'],'rate':clip['animation']['rate']*rate_scale}}
    tracks.append({'frame':frame,'state':state,**clip})


# OP 10 and OP 13 optionally clamp the camera eye height in 0x23B1C0.
EYE_HEIGHT = {9:(0x4B3204,0x4B3230),11:(0x4B31E8,0x4B3214),
              13:(0x4B31F4,0x4B3220),29:(0x4B31F4,0x4B3220),
              24:(0x4B31FC,0x4B3228),25:(0x4B31F8,0x4B3224),
              26:(0x4B31E0,0x4B3208),27:(0x4B31DC,0x4B3210),
              28:(0x4B3200,0x4B322C),31:(0x4B31E4,0x4B320C),
              37:(0x4B31F0,0x4B321C),38:(0x4B31EC,0x4B3218)}


def eye_height(workbench,role,op,enabled):
    offsets = EYE_HEIGHT.get(role)
    return workbench.tables.at(offsets[op==13],'<f')[0] if enabled and offsets else 0.0


def continue_camera(cuts,frame,**updates):
    if cuts and cuts[-1]['curve']:
        cut = cuts[-1]
        elapsed = 0 if cut.get('paused') else (frame-cut['frame'])/60
        cuts.append({**cut,'frame':frame,'start':cut['start']+elapsed,**updates})


def animation_markers(actors, last):
    for actor, segments in enumerate(actors):
        prefix = '对手' if actor else '施放方'
        for i, clip in enumerate(segments):
            first = clip['animation']['first']
            stop = segments[i+1]['frame'] if i+1 < len(segments) else last+1
            for event in clip['events']:
                if event['frame'] < first or (clip.get('held') and event['frame'] != first) or event['op'] not in (0, 1, 16, 17, 19, 20, 23, 24, 27):
                    continue
                time = clip['frame'] + elapsed_frames(clip, event['frame'])
                if time >= stop:
                    continue
                name = prefix + ' · ' + event['name']
                if event.get('damage') is not None:
                    name += f' · {event["damage"]} HP'
                yield {'frame':time, 'name':name, 'op':event['op'], 'bom':True}


def finisher_scene(workbench, owner, shape, candidate, opponent, donor, donor_candidate):
    source = (donor, 0) if donor is not None else (owner, shape)
    target = owner, shape
    rival = opponent, 0
    workbench.form(*target)
    workbench.form(*rival)
    available = workbench.skills(source[0])
    chosen = donor_candidate if donor is not None else candidate
    skill = next((item for item in available if item['id'] == chosen), None)
    require(skill is not None and skill['anm'] in workbench.binding(source[0]), '请选择有效的必杀技及施放者。')
    if donor is not None:
        source_shape = next((form.id for form in workbench.forms(donor).values()
                             if not form.accessory and skill['menuMask'] & (1 << form.id)), 0)
        source = donor, source_shape
    players,replies,cuts,flags,node_flags,events = [],[],[],[[],[]],[[],[]],[]
    add_clip(players,workbench,source,target,skill['anm'],0,skill['action'])
    if idle := workbench.action_motion(opponent,0,owner):
        add_clip(replies,workbench,rival,rival,idle['anm'],0,0,idle['rateScale'])
    address = preview_scene_address(workbench, source[0], skill['action'])
    rows = []
    if address is not None:
        program = workbench.program(source[0])
        patches = getattr(workbench.game, 'transfer_plan', None)
        patch = patches.scripts.get(source[0],{}).get(program.file_offset(address,32)) if patches else None
        rows = script_rows(program,address,branches=True,patch=patch) or script_rows(program,address,patch=patch)
    if rows and replies:
        # 0x2CB1F8 freezes the opponent's current animation until an op 8 switch.
        replies[0]['held'] = True
        flags[1].append([0,False])
    cameras = {}
    film = BattleFilm(workbench.game)
    last = elapsed_frames(players[0],players[0]['animation']['last'])
    for frame, raw, op, args, location in timeline(rows):
        film.through(frame)
        last = frame
        item = {'frame':frame, 'clock':raw, 'name':SCENE_OPS.get(op, f'演出指令 {op}'),
                'op':op, 'args':args, 'address':location}
        if op == 0 and args[0] in (0, 1):
            flags[args[0]].append([frame, bool(args[1])])
        elif op == 1 and args[0] in (0,1):
            node = args[1]
            actor = target if args[0] == 0 else rival
            if args[0] == 0 and source != target:
                logical = dict(workbench.form(*source).tracks).get(node)
                node = next((node for node,channel in workbench.form(*actor).tracks if channel == logical),-1)
            if node >= 0:
                node_flags[args[0]].append([frame,node,bool(args[2]&255)])
        elif op == 8 and args[0] in (0,1):
            actor = (source if args[1] else target) if args[0] == 0 else rival
            peer = opponent if args[0] == 0 else owner
            if motion := workbench.action_motion(actor[0],args[1],peer):
                number = motion['anm']
                add_clip(players if args[0] == 0 else replies,workbench,actor,
                         target if args[0] == 0 else rival,number,frame,args[1],motion['rateScale'])
                item['name'] = ('施放方' if args[0] == 0 else '对手') + f' · 状态 {args[1]} · ANM {number}'
        elif op in (30, 32) and args[0] in (0,1):
            # 0x21EBB8 / 0x21ECD8 pass the ANM number directly to 0x26E718.
            actor = source if args[0] == 0 else rival
            number = args[1]
            if number in workbench.binding(actor[0]):
                add_clip(players if args[0] == 0 else replies,workbench,actor,
                         target if args[0] == 0 else rival,number,frame)
                item['name'] = ('施放方' if args[0] == 0 else '对手') + f' · ANM {number}'
        elif op in (2, 3) and 0 <= args[0] < 166:
            resource = workbench.tables.pointer(0x412C38,args[0]*3)
            film.load(args[0],args[1])
            item['name'] += f' {args[0]:03d} · 资源 {resource:04X}'
        elif op == 7 and film.queue is not None:
            film.queue.seek(args[0])
            item['name'] += f' · 预读影片 {film.number:03d} 第 {args[0]} 帧'
        elif op == 10 and args[0] >= 0 and args[1] in (0, 1) and args[2] in (0, 1):
            key = (source[0] if args[1] == 0 else opponent, args[0])
            if key not in cameras:
                cameras[key] = camera(workbench, *key)
            if curve := cameras[key]:
                item['name'] = curve['name']
                initial = film.current() if args[3] == -100 else args[3]
                anchor = target[0] if args[2] == 0 else opponent
                cuts.append({'frame':frame,'actor':args[2],'curve':curve,'start':max(0,initial)/60,
                             'eyeHeight':eye_height(workbench,anchor,op,args[4])})
        elif op in (11,12):
            cuts.append({'frame':frame,'curve':None})
        elif op == 13 and args[0] in (0,1):
            anchor = target[0] if args[0] == 0 else opponent
            continue_camera(cuts,frame,actor=args[0],eyeHeight=eye_height(workbench,anchor,op,args[1]))
        elif op == 5:
            continue_camera(cuts,frame,paused=bool(args[0]))
        if op in (4,6):
            film.active = op == 4
        elif op == 5:
            film.paused = bool(args[0])
        if op in SCENE_OPS:
            events.append(item)
    if film.cues and not film.active and film.cues[-1]['id'] is not None:
        # The stop command runs after the last submitted sprite.
        last = math.floor(last)+1
        film.through(last)
    events.extend(animation_markers([players,replies],last))
    events.sort(key=lambda item:item['frame'])
    return {'last':last, 'actors':[players, replies], 'cameras':cuts, 'visibility':flags,'nodeVisibility':node_flags,'events':events,'movies':film.cues,
            'source':address if rows else None, 'name':skill['name']}


def throw_scene(workbench, role, shape, state, opponent, member):
    target, rival = (role, shape), (opponent, 0)
    selected = next((item for item in workbench.throw_rows(role, opponent) if item['action'] == state), None)
    require(selected is not None, '请选择此角色的投技阶段。')
    paired = next((item for item in workbench.throw_rows(opponent, role)
                   if item['action'] == PAIRED_STATES.get(state)), None)
    players, replies, cuts = [], [], []
    add_clip(players,workbench,target,target,selected['anm'],0,state,selected['rateScale'])
    anchor = None
    if paired:
        add_clip(replies,workbench,rival,rival,paired['anm'],0,paired['action'],paired['rateScale'])
        entries = {selected['callbacks'][1]['ee'],paired['callbacks'][1]['ee']}
        # 0x2CC400 and 0x2CBDC0 share the grabbed actor's origin. In
        # initial grab stages 0x2CC340 and 0x2CBF60 both turn by pi.
        for begin,reply,facing in ((0x2C78C8,0x2C7AB0,'same'),(0x2C8218,0x2C8480,'same'),
                                   (0x2C8760,0x2C8908,'opposite')):
            if entries == {begin,reply}:
                anchor = facing
                break
    if member >= 0 and (curve := camera(workbench, role, member)):
        cuts.append({'frame':0, 'actor':0, 'curve':curve, 'start':0})
    last = max((elapsed_frames(clip,clip['animation']['last']) for clip in players + replies), default=0)
    events = [{'frame':0, 'name':f'对手 · 状态 {paired["action"]} · ANM {paired["anm"]}'}] if paired else []
    if cuts:
        events.append({'frame':0, 'name':cuts[0]['curve']['name'], 'op':10})
    events.extend(animation_markers([players,replies],last))
    events.sort(key=lambda item:item['frame'])
    return {'last':last, 'actors':[players, replies], 'cameras':cuts, 'visibility':[[], []], 'events':events,
            'source':selected['erx'],'name':selected['name'],'paired':paired is not None,
            'pairedFacing':anchor}
