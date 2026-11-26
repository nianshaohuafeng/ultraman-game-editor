"""Transplant compatible FE3 finishers into a recipient's existing skill slot."""

from collections import defaultdict
from dataclasses import dataclass, field
import math
import struct

from fe3_formats import Bom, require
from fe3_preview import clip_index, key_span, read_bones, replace_clip, single_clip
from fe3_scene import camera_bank, scene_address, script_rows
from fe3_skill_ui import install_finisher_ui, stage_finisher_ui
from cross_timing import BomTiming
from formats import merge_form_events


SHAPE_FIELDS = {0:9, 1:2, 2:2, 3:2, 4:2, 12:2, 24:14}
FACIAL_EVENT = 18
STANDARD_SCENE_OPS = frozenset(range(29))
SKILL_AUDIO_TABLE = 0x3B59B0
SKILL_AUDIO_COUNT = 235


@dataclass
class Transfers:
    resources: dict = field(default_factory=dict)
    scripts: dict = field(default_factory=dict)
    names: dict = field(default_factory=dict)
    skill_words: dict = field(default_factory=dict)
    audio: dict = field(default_factory=dict)
    elf: bytes | None = None
    modules: dict = field(default_factory=dict)
    additions: dict = field(default_factory=dict)
    cameras: dict = field(default_factory=dict)
    descriptions: dict = field(default_factory=dict)
    ratings: dict = field(default_factory=dict)


def audio_table(tables):
    upper, = tables.at(0x225D5C,'<I')
    lower, = tables.at(0x225D64,'<I')
    require(upper >> 16 == 0x3C0F and lower >> 16 == 0x25EF, '格斗进化3 的必杀音库指针无效。')
    return (upper & 65535)*65536 + (lower & 32767) - (lower & 32768)


def audio_slots(tables):
    # 0x225B90 searches for the final common-bank ID before constructing the
    # 0x226FA8 lookup. 0x2A65E0 selects its slot from the skill's +8 word.
    address = audio_table(tables)
    count = SKILL_AUDIO_COUNT + 1 if address == SKILL_AUDIO_TABLE else (0x200000 - address) // 4
    rows = tables.at(address, f'<{count}i')
    require(9999 in rows, '格斗进化3 的必杀音库表无效。')
    rows = rows[:rows.index(9999)+1]
    slots, markers = {}, {}
    for role in range(39):
        desc, sizes = tables.descriptor(role)
        if not sizes[7] or not sizes[11] or not desc[18]:
            continue
        marker, = tables.at(desc[18]+4*(sizes[11]-1),'<i')
        if marker not in rows[:-1]:
            continue
        first = rows.index(marker)
        if first + sizes[7] >= len(rows) - 1:
            continue
        markers[marker] = first
        for item in tables.skill_rows(role):
            if item.id < sizes[7]:
                slots[role,item.id] = first + 1 + item.id
    return slots, markers, rows


def finisher(workbench, role, candidate, *, receiving=False):
    skill = next((item for item in workbench.tables.skill_rows(role) if item.id == candidate), None)
    require(skill is not None, '请选择双方角色的原版必杀记录。')
    require(skill.words[2] == skill.id, '来源或接收必杀的原生音库候选号已变化。')
    forms = [form for form in workbench.forms(role).values()
             if not form.accessory and skill.menu_mask & (1 << form.id)]
    require(len(forms) == 1, '这个必杀跨多个模型形态，需分别适配各形态的动作与事件。')
    program = workbench.program(role)
    source = scene_address(workbench, role, skill.action)
    rows = script_rows(program, source, branches=True) if source is not None else []
    require(rows, '这个必杀的进入回调没有可移植的通用演出表。')
    anm = program.actions()[skill.action][1]
    require(anm in workbench.binding(role), '这个必杀没有独立的动作片段。')
    if receiving:
        require(sum(action[1] == anm for action in program.actions()) == 1, '这个动作片段与其他状态共享。')
        require(sum(scene_address(workbench,role,state) == source for state in range(len(program.actions()))) == 1,
                '这个演出表与其他状态共享。')
    return skill, forms[0], program, source, rows, anm


def actor_rig(game, form):
    bones = read_bones(game.original(form.model))
    channels = dict(form.tracks)
    return {channel:(node, channels.get(bones[node]['parent'], -1), bones[node])
            for node, channel in form.tracks if node >= 0 and channel >= 0}


def orientation(rotation):
    x,y,z = (angle/2 for angle in rotation)
    sx,sy,sz = math.sin(x),math.sin(y),math.sin(z)
    cx,cy,cz = math.cos(x),math.cos(y),math.cos(z)
    return (sx*cy*cz-cx*sy*sz, cx*sy*cz+sx*cy*sz, cx*cy*sz-sx*sy*cz, cx*cy*cz+sx*sy*sz)


def matched_rigs(game, source_form, target_form):
    source, target = actor_rig(game, source_form), actor_rig(game, target_form)
    require(source.keys() == target.keys() and all(a[1] == target[channel][1] for channel,a in source.items()),
            '双方骨骼的原生动作通道与父节点需要匹配。')
    for key, (_, _, start) in source.items():
        to = target[key][2]
        # Authoring can encode the same orientation with different Euler triples.
        a,b = orientation(start['rotation']),orientation(to['rotation'])
        require(abs(sum(x*y for x,y in zip(a,b))) >= math.cos(.02),
                '双方骨骼的原生动作坐标系不兼容。')
    return source, target


def retarget_clip(data, index, order, source, target):
    raw = bytearray(single_clip(data, index))
    disk = clip_index(bytes(raw))[0]
    require(set(disk.tracks) == set(order) <= source.keys(), '双方动作轨道的原生编号不兼容。')
    nodes = {key:i for i, key in enumerate(disk.tracks)}
    old = raw[disk.start + 0x28:disk.start + 0x28 + 14 * len(nodes)]
    for i, channel in enumerate(order):
        from_index = nodes[channel]
        row = 14 * from_index
        raw[disk.start + 0x28 + 14 * i:disk.start + 0x28 + 14 * (i + 1)] = old[row:row + 14]
    reference = bytes(raw)
    written = {}
    for i, channel in enumerate(order):
        start, end, _ = key_span(reference, 0, i, 1)
        before = source[channel][2]['translation']
        after = target[channel][2]['translation']
        delta = tuple(b-a for a,b in zip(before,after))
        for where in range(start, end, 16):
            original = struct.unpack_from('<3f', reference, where)
            updated = tuple(x+y for x,y in zip(original,delta))
            require(where not in written or written[where] == updated, '共享的位移关键帧需要独立重建。')
            written[where] = updated
            struct.pack_into('<3f', raw, where, *updated)
    return bytes(raw)


def entry_bytes(bom, index):
    start = bom.offsets[index]
    return bom.data[start:bom.ends[start]]


def script_bytes(rows):
    start = 4 + 4 * len(rows)
    offsets = []
    for raw in rows:
        offsets.append(start)
        start += len(raw)
    return struct.pack(f'<{len(rows)+1}I', len(rows), *offsets) + b''.join(rows)


def transplant_bom(source, anm, source_form, target, slot, target_form, nodes, old_last, new_last, face_slot=None):
    require(0 <= slot < 65536,'必杀 BOM 编号越界。')
    scripts = [entry_bytes(target,index) for index in range(target.scripts)]
    shapes = [entry_bytes(target,target.scripts+index) for index in range(target.shapes)]
    templates = {raw:index for index,raw in enumerate(shapes)}
    new_mask = 1 << target_form.id
    old_rows = []
    additions = []
    existing = target.events(slot if face_slot is None else face_slot) if slot < len(scripts) or face_slot is not None else ()
    for event in existing:
        start = event['offset']
        raw = target.data[start:start + 4 + 2 * len(event['args'])]
        if raw[1] & new_mask and event['op'] == FACIAL_EVENT and target_form.vau:
            frame = min(new_last, round(event['frame'] * new_last / max(1, old_last)))
            if slot < len(scripts) and frame == event['frame']:
                old_rows.append(raw)
            else:
                if slot < len(scripts) and raw[1] & ~new_mask:
                    old_rows.append(raw[:1]+bytes([raw[1] & ~new_mask])+raw[2:])
                old_rows.append(raw[:1]+bytes([new_mask])+struct.pack('<h',frame)+raw[4:])
        elif slot < len(scripts):
            old_rows.append(raw)
    for event in source.events(anm):
        op = event['op']
        if not event['selector'] & (1 << source_form.id) or op == FACIAL_EVENT:
            continue
        require(op in (0,1,2,3,4,6,8,9,10,11,12,13,14,15,16,17,21,22,24,25,26),
                f'动作包含需要专门适配的 BOM 事件 {op}。')
        start = event['offset']
        raw = bytearray(source.data[start:start + 4 + 2 * len(event['args'])])
        raw[1] = new_mask
        if op in SHAPE_FIELDS:
            argument = SHAPE_FIELDS[op]
            index = event['args'][argument]
            require(0 <= index < source.shapes, '技法的碰撞模板超出源角色目录。')
            blob = entry_bytes(source, source.scripts + index)
            if blob not in templates:
                templates[blob] = len(shapes)
                shapes.append(blob)
            struct.pack_into('<h', raw, 4 + 2*argument, templates[blob])
        if op in SHAPE_FIELDS or op == 17:
            argument = 1 if op == 17 else 0
            node = event['args'][argument]
            if node >= 0:
                require(node in nodes, '技法引用的 MDL 节点在接收角色上没有对应轨道。')
                struct.pack_into('<h', raw, 4 + 2*argument, nodes[node])
        additions.append(bytes(raw))
    rows = merge_form_events(((raw,raw[0] != FACIAL_EVENT or not target_form.vau) for raw in old_rows),new_mask,additions)
    script = script_bytes(old_rows if rows is None else rows)
    info = BomTiming(target.data)
    if slot < len(scripts) and scripts[slot] != script:
        info.anchors.pop((slot,target_form.id),None)
    scripts.extend([script_bytes([])] * max(0,slot-len(scripts)))
    scripts[slot:slot+1] = [script]
    holders = scripts + shapes
    cursor, offsets = 12 + 4 * len(holders), []
    for raw in holders:
        offsets.append(cursor)
        cursor += len(raw)
    result = struct.pack(f'<4s{len(offsets)+2}I', b'BOM\0', len(scripts), len(shapes), *offsets)
    return info.write(result + b''.join(holders))


def scene_camera_slots(rows):
    return tuple(dict.fromkeys(args[0] for _,op,args,_ in rows if op == 10 and args[1] == 0 and args[0] >= 0))


def script_patch(donor,size,mapping,old_state,new_state,nodes):
    require(len(donor) <= size, '来源演出分支超过目标 ERX 的原生占用范围。')
    result = []
    for frame, op, values, _ in donor:
        args = list(values)
        require(op in STANDARD_SCENE_OPS, f'演出操作码 {op} 依赖角色专用回调。')
        if op == 1 and args[0] == 0:
            require(args[1] in nodes,'演出显隐节点在接收形态没有原生骨骼对应。')
            args[1] = nodes[args[1]]
        if op == 8 and args[0] == 0 and args[1] != 0 and args[1] < 0x7FFF:
            require(args[1] == old_state, '演出使用来源角色的额外专用动作。')
            args[1] = new_state
        if op == 10 and args[1] == 0 and args[0] >= 0:
            args[0] = mapping[args[0]]
        result.append(struct.pack('<fi6i', frame, op, *args))
    # A shorter sequence leaves an inert, 32-byte aligned boundary at its end.
    return b''.join(result).ljust(32 * size, b'\xff')


def compile_transfers(game, choices, additions=None):
    from fe3_workbench import Workbench
    from fe3_support import compile_support, installed_supports

    result = Transfers()
    installed = installed_supports(game.elf)
    if not choices and not additions and not any(game.skill_records[key].kind == 0
                                                 for key in installed if key in game.skill_records):
        return result
    work = Workbench(game,preview=False)
    audio, markers, original_audio = audio_slots(work.tables)
    mixed_audio = list(original_audio)
    reserved_cameras = set()
    support = {}
    for (role,candidate),(donor,donor_candidate) in sorted(choices.items()):
        require(role != donor, '请选择不同的来源角色。')
        dest = game.skill_records.get((role,candidate))
        require(dest is not None,'请选择目标镜像已有的技能候选。')
        if dest.kind != 1:
            support[role, candidate] = donor, donor_candidate
            continue
        dest, form, program, begin, out_rows, slot = finisher(work,role,candidate,receiving=True)
        src, form_in, _, _, in_rows, anm = finisher(work,donor,donor_candidate)
        rig_in, rig_out = matched_rigs(game,form_in,form)
        key, index = work.binding(role)[slot]
        src_key, src_index = work.binding(donor)[anm]
        bank = result.resources.get(key, game.original(key))
        base_clip = clip_index(bank)[index]
        incoming = retarget_clip(game.original(src_key),src_index,base_clip.tracks,rig_in,rig_out)
        result.resources[key] = replace_clip(bank,index,incoming)
        in_bom = Bom(game.original(form_in.bom))
        out_bom = Bom(result.resources.get(form.bom, game.original(form.bom)))
        node_map = {data[0]:rig_out[channel][0] for channel,data in rig_in.items()}
        raised = clip_index(incoming)[0]
        old_last = base_clip.last_frame
        new_last = raised.last_frame
        result.resources[form.bom] = transplant_bom(in_bom,anm,form_in,out_bom,slot,form,node_map,old_last,new_last)
        stage_finisher_ui(game, result, role, candidate, donor, donor_candidate, src.label)
        target_cameras, donor_cameras = scene_camera_slots(out_rows), scene_camera_slots(in_rows)
        require(len(donor_cameras) <= len(target_cameras), '接收必杀的原生镜头槽不足。')
        mapping = dict(zip(donor_cameras,target_cameras))
        for a,b in mapping.items():
            camera_out, camera_in = (camera_bank(work,role), b), (camera_bank(work,donor), a)
            require(camera_out not in reserved_cameras, '两条配招使用同一个原生镜头槽。')
            for other in work.tables.skill_rows(role):
                if other.id != candidate:
                    at = scene_address(work,role,other.action)
                    require(at is None or b not in scene_camera_slots(script_rows(program,at,branches=True)),
                            '这个镜头还被其他必杀引用。')
            result.resources[camera_out] = game.original(camera_in)
            reserved_cameras.add(camera_out)
        data = script_patch(in_rows,len(out_rows),mapping,src.action,dest.action,node_map)
        offset = program.file_offset(begin,len(data))
        require(not any(begin <= pos < begin + len(data) for pos in program.relocations),
                '演出表包含角色专用的原生指针。')
        result.scripts.setdefault(role,{})[offset] = data
        result.skill_words[role,candidate] = (*dest.words[:3],*src.words[3:])
        destination, origin = audio.get((role,candidate)), audio.get((donor,donor_candidate))
        selected = original_audio[origin] if origin is not None else 140
        require(destination is not None or selected in (-1,140), '接收角色没有对应的必杀音库槽。')
        if destination is not None:
            mixed_audio[destination] = selected
            result.audio[destination] = selected
    require(all(mixed_audio.index(marker) == index for marker,index in markers.items()),
            '这个声音资源编号会与原生角色音库的定位值重叠。')
    if additions:
        from fe3_add import append_skills
        append_skills(game,work,result,additions,mixed_audio,markers)
    if support:
        compile_support(game,work,result,support)
    from fe3_guard import compile_guards
    compile_guards(game, work, result, installed_supports(result.elf) if result.elf else installed)
    install_finisher_ui(game,result)
    return result
