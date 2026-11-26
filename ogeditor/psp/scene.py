"""Build PSP battle previews from action transitions, event scripts and texture bindings."""

import base64
import math
import struct

from ogeditor.psp.asset_transfer import texture_png
from ogeditor.psp.skill_phases import skill_phases
from ogeditor.battle_camera import camera_curve, elapsed_frames
from formats import decode_anm
from ogeditor.psp.role_effects import THROW_STATES, throw_victim_clip
from move_eboot import BASE
from ogeditor.psp.role_effects import BACK_SKILLS, JACK_THROW, SCENE_ROLES, SCENE_SCRIPTS
from ogeditor.psp.move_taro import TARO_DOWN
from ogeditor.psp.move_textures import DRAW_PALETTES
from ogeditor.psp.move_throw import LEO_THROW, LEO_THROW_STAGES


# The four-key request at 0x0889A204 uses slot + side + distance.
# 0x0889A00C resolves the three regular throws from the entered state.
CAMERA_START = {0x08898918:12, 0x08898ACC:12, 0x08898B50:16, 0x08898C28:16, 0x0882A3F4:12}
GRAB_KINDS = {JACK_THROW:4, TARO_DOWN:5, LEO_THROW:6}


def source_action(game, role, action):
    source = game.move_source(role, action)
    return (source['role'], source['state']) if source else (role, action)


def scene_resources(game, role, config, action):
    owner, source = source_action(game, role, action)
    case = game.roles[owner]['cases'][config if owner == role else 0]
    cameras = case['camera_assets']
    move = game.native_actions[owner].get(source)
    return case, cameras, owner, source, move


def camera_options(game, role, config, action):
    game.form_binding(role, config, 0)
    _, cameras, _, _, _ = scene_resources(game, role, config, action)
    return [{'id':row['id'], 'name':game.read_member(row['id'], b'CMSP', 48)[16:48]
             .split(b'\0', 1)[0].decode('ascii', 'replace')} for row in cameras]


def camera_cut(game, cameras, slot, frame):
    if not 0 <= slot < len(cameras):
        return None
    member = cameras[slot]['id']
    return {'frame':frame, 'actor':0, 'start':0,
            'curve':camera_curve(game.read_member(member, b'CMSP'), key=[member])}


def clip_payload(game, role, config, shape, clip):
    case, _, bom, _ = game.form_binding(role, config, shape)
    bank, index, _ = game.anim_bank(case, clip)
    _, _, parts = game.bank(bank, b'ANM\0')
    if not 0 <= index < len(parts):
        return None
    animation = decode_anm(game.asset_edits.get((bank, index), parts[index]))
    events = game._motion_events(case, role, bom['id'], clip, animation['last'], None)
    return {'animation':animation, 'events':[item for item in events if item['mask'] & (1 << shape)],
            'anm':bank, 'slot':index}


def action_payload(game, role, config, shape, state, frame, clip=-1):
    if clip >= 0:
        result = clip_payload(game, role, config, shape, clip)
    else:
        plan = game.current_moves() if game.move_sets else None
        move = plan.preview_state(role, state) if plan else game.native_actions[role].get(state)
        if not move or move['clip'] is None:
            return None
        result = game.motion(role, config, shape, state)
    if result:
        return {'frame':frame, 'state':state, 'animation':result['animation'],
                'events':[item for item in result['events'] if item['mask'] & (1 << shape)],
                'anm':result['anm'], 'slot':result['slot']}
    return None


def scene_effect(game, owner, config, action, shape):
    candidates = [action, *BACK_SKILLS.get((owner, action), ())]
    for state in candidates:
        move = game.native_actions[owner].get(state)
        if not move or move['clip'] is None:
            continue
        case, _, bom, _ = game.form_binding(owner, config, shape)
        for _, raw in game.current_moves().original_events(bom['id'], move['clip'], stock=True):
            if len(raw) >= 6 and raw[0] == 8 and raw[1] & (1 << shape):
                effect, = struct.unpack_from('<h', raw, 4)
                if SCENE_ROLES.get(effect) == owner:
                    return effect, state
    return None, action


def preview_textures(game, role, action, case, factory, cache, private, carrier_case):
    output = []
    for slot, palettes in DRAW_PALETTES.get(factory, {}).items():
        if slot >= len(case['common_poms']):
            continue
        member = case['common_poms'][slot]['id']
        logical = role, action, member
        key = game.texture_copy(carrier_case, logical) if logical in private else None
        data = game.move_texture_data(key) if key else game.read_member(member, b'POM\0')
        for palette in palettes:
            ref = key, member, palette
            if ref not in cache:
                cache[ref] = {'id':member, 'palette':palette,
                              'uri':'data:image/png;base64,' + base64.b64encode(texture_png(data, palette)).decode('ascii')}
            output.append(cache[ref])
    return output


def special_grab(game, participants, source, action, kind, plan, cameras, select_camera):
    # The successful front-contact row in 0x08806E84 selects these states on
    # the two native actors. The move compiler assigns new states to the same phases.
    attack, defend = struct.unpack_from('<2I', game.image, 0x0894E910 - BASE + kind * 16)
    role = participants[0][0]
    donor, _ = source
    remap = {(move.source, move.original):state for (target, state), move in plan.states.items()
             if target == role and move.root == action} if plan else {}
    if not remap and game.installed_moves:
        remap = {(data[0], data[1]):state for (target, state), data in game.installed_moves.items()
                 if target == role and data[0] == donor}
    phases = list(LEO_THROW_STAGES) if kind == 6 else [attack]
    rows, receives, cuts, frame = [], [], [], 0.0
    for i, state in enumerate(phases):
        target = remap.get((donor, state), state)
        if donor != role and not remap.get((donor, state)):
            break
        item = action_payload(game, *participants[0], target, frame)
        if item is None:
            break
        rows.append(item)
        slot = 16 if kind == 4 else 16 + 2 * i if kind == 6 else -1
        if select_camera and (cut := camera_cut(game, cameras, slot, frame)) is not None:
            cuts.append(cut)
        victim_state = (defend, 136, 136, 136, 137)[i] if kind == 6 else defend
        if victim := action_payload(game, *participants[1], victim_state, frame):
            receives.append(victim)
        frame += elapsed_frames(item, item['animation']['last'])
    return rows, receives, cuts, frame


def battle_preview(game, role, config, shape, action, rival, camera=-1):
    _, _, _, _ = game.form_binding(rival, 0, 0)
    source_case, cameras, owner, source, move = scene_resources(game, role, config, action)
    if not move:
        raise ValueError('请选择已有的必杀技或投技。')
    participants = [[role, config, shape], [rival, 0, 0]]
    tracks, cuts, timeline = [[], []], [], []
    lead = action_payload(game, *participants[0], action, 0)
    if lead is None:
        raise ValueError('此状态没有动作片段。')
    tracks[0].append(lead)
    plan = game.current_moves() if game.move_sets else None
    effect_sources = plan.effect_sources if plan else game.installed_effect_sources
    last = elapsed_frames(lead, lead['animation']['last'])
    subslot = -1
    if source in THROW_STATES:
        number = throw_victim_clip(game.image, rival, owner, source)
        if number >= 0 and (victim := action_payload(game, *participants[1], 131, 0, number)):
            tracks[1].append(victim)
            timeline.append({'frame':0, 'name':f'对手受身 · ANM {number}', 'kind':'hit'})
            last = max(last, elapsed_frames(victim, victim['animation']['last']))
        subslot = 3 + 2 * (source - THROW_STATES[0])
    elif (kind := GRAB_KINDS.get((owner, source))) is not None:
        performed, received, cuts, end = special_grab(
            game, participants, (owner, source), action, kind, plan, cameras, camera == -1)
        if performed:
            tracks = [performed, received]
            last = end
            if received:
                tail = received[-1]
                last = max(last, tail['frame'] + elapsed_frames(tail, tail['animation']['last']))
            timeline.append({'frame':0, 'name':'抓取成立 · 施放方与对手受身', 'kind':'hit'})
    else:
        subslot = CAMERA_START.get(int(move['callbacks'][0], 16), -1)
        if subslot >= 0:
            subslot += 2  # Close distance branch preview.
        effect, phase = scene_effect(game, owner, config if owner == role else 0, source, shape if owner == role else 0)
        if effect in SCENE_SCRIPTS:
            phase_map = plan.scene_states.get((role, owner), {}) if plan else {}
            if not plan and (role,action) in game.installed_moves:
                allowed = BACK_SKILLS.get((owner,source), ())
                phase_map = {origin:state for (target,state),(donor,origin,_) in game.installed_moves.items()
                             if target == role and donor == owner and origin in allowed}
            if phase != source:
                actual = phase_map.get(phase, phase)
                if (after := action_payload(game, *participants[0], actual, last)) is not None:
                    tracks[0].append(after)
            if (idle := action_payload(game, *participants[1], 0, 0)):
                tracks[1].append(idle)
            segment = tracks[0][-1]
            trigger = next((event['frame'] for event in segment['events'] if event['op'] == 8 and
                            effect_sources.get(event['fields']['特效工厂编号'],event['fields']['特效工厂编号']) == effect), None)
            if trigger is not None:
                start_frame = segment['frame'] + elapsed_frames(segment,trigger)
                _, _, start, end = SCENE_SCRIPTS[effect][2][0]
                for raw, op, actor, arg, value, *_ in struct.iter_unpack('<fi6i', game.image[start - BASE:end - BASE]):
                    if not math.isfinite(raw) or raw < 0:
                        break
                    frame = start_frame + raw
                    if op == 18:
                        last = max(last, frame)
                        break
                    if op == 4 and actor in (0, 1):
                        state = phase_map.get(arg, arg) if actor == 0 else arg
                        if item := action_payload(game, *participants[actor], state, frame, value):
                            tracks[actor].append(item)
                            timeline.append({'frame':frame, 'name':('施放方' if actor == 0 else '对手') +
                                             f' · 状态 {state} · ANM {item["slot"]}', 'kind':'hit'})
                    elif op == 6 and actor == 0 and camera == -1 and (cut := camera_cut(game, cameras, value, frame)):
                        cuts.append(cut)
                    elif op == 0x23:
                        timeline.append({'frame':frame, 'name':f'演出特效工厂 {effect}', 'kind':'effects',
                                         'factory':effect})
        else:
            for state in skill_phases(game, role, action)[1:]:
                segment = action_payload(game, *participants[0], state, last)
                if segment is None:
                    break
                tracks[0][-1]['ends_at_last_key'] = True
                tracks[0].append(segment)
                timeline.append({'frame':last, 'name':f'施放方 · 后续状态 {state}', 'kind':'hit'})
                last += elapsed_frames(segment, segment['animation']['last'])
    if camera >= 0:
        subslot = next((index for index, row in enumerate(cameras) if row['id'] == camera), -1)
        if subslot < 0:
            raise ValueError('请选择此动作所属出场配置的镜头。')
    if camera != -2 and (cut := camera_cut(game, cameras, subslot, 0)):
        cuts.insert(0, cut)
    for cut in cuts:
        timeline.append({'frame':cut['frame'], 'name':cut['curve']['name'], 'kind':'camera'})
    for actor, clips in enumerate(tracks):
        for i, item in enumerate(clips):
            last = max(last, item['frame'])
            limit = clips[i + 1]['frame'] if i + 1 < len(clips) else math.inf
            for event in item['events']:
                if event['op'] != 8 and 'damage' not in event:
                    continue
                frame = item['frame'] + elapsed_frames(item, event['frame'])
                end_key = item.get('ends_at_last_key') and event['frame'] == item['animation']['last']
                if not item['frame'] <= frame <= last or frame > limit or (frame == limit and not end_key):
                    continue
                factory = event['fields'].get('特效工厂编号') if event['op'] == 8 else None
                if factory is not None:
                    factory = effect_sources.get(factory, factory)
                row = {'frame':frame, 'name':event['name'], 'kind':event['kind']}
                if factory is not None:
                    row.update(factory=factory, actor=actor, name=f'特效工厂 {factory}')
                if 'damage' in event:
                    row['name'] += f' · {event["damage"]} HP'
                timeline.append(row)
    pictures = {}
    receiver_case = game.roles[rival]['cases'][0]
    performer_case = game.roles[role]['cases'][config]
    private = game.private_texture_usage(role, config, shape)
    for event in timeline:
        if 'factory' in event:
            target = event.get('actor') == 1
            event['textures'] = preview_textures(game, rival if target else role, action,
                receiver_case if target else source_case, event['factory'], pictures, {} if target else private,
                receiver_case if target else performer_case)
    timeline.sort(key=lambda item: item['frame'])
    return {'actors':tracks, 'cameras':cuts, 'visibility':[[], []], 'events':timeline, 'last':last,
            'name':game.motion(role, config, shape, action)['action']['name'],
            'gap':0.48 if source in THROW_STATES or (owner, source) in GRAB_KINDS else 0.92}
