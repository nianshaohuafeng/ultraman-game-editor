"""Read-only ANM and BOM decoding for the KGF v7 preview."""

from dataclasses import dataclass
import math
import struct


def u32(data, offset):
    return struct.unpack_from('<I', data, offset)[0]


def s16(data, offset):
    return struct.unpack_from('<h', data, offset)[0]


def sections(data, start, total):
    offsets = struct.unpack_from(f'<{total}I', data, start)
    used = sorted(set(offsets) - {0xFFFFFFFF})
    if any(off % 4 or not start + 4 * total <= off < len(data) for off in used):
        raise ValueError('配招资源的目录超出文件范围。')
    lengths = {off: end - off for off, end in zip(used, [*used[1:], len(data)])}
    return offsets, [data[off:off + lengths[off]] if off in lengths else b'' for off in offsets]


def bom_parts(data):
    if len(data) < 12 or data[:4] != b'BOM\0':
        raise ValueError('配招读取到无效的 BOM。')
    count, templates = struct.unpack_from('<II', data, 4)
    if count > 4096 or count + templates > min(8192, (len(data) - 12) // 4):
        raise ValueError('配招 BOM 的目录长度无效。')
    offsets, parts = sections(data, 12, count + templates)
    return count, offsets, parts


def rotation(x, y, z):
    # 088D0248 stores the three half angles as u16, biased by pi/2.
    a, b, c = (value * (math.pi / 65535) - math.pi / 2 for value in (x, y, z))
    sx, sy, sz = math.sin(a), math.sin(b), math.sin(c)
    cx, cy, cz = math.cos(a), math.cos(b), math.cos(c)
    return (sx * cy * cz - cx * sy * sz, cx * sy * cz + sx * cy * sz,
            cx * cy * sz - sx * sy * cz, cx * cy * cz + sx * sy * sz)


def anm_header(clip):
    if len(clip) < 48 or clip[:4] != b'ANM\0' or struct.unpack_from('<III', clip, 8) != (0, 3, 32):
        raise ValueError('ANM 片段使用了未识别的轨道布局。')
    control = u32(clip, 20)
    _, _, curve_count, count = struct.unpack_from('<4H', clip, 32)
    directory_end = 40 + count * 14
    if not 0 < count <= 156 or not directory_end <= control <= len(clip) - 10:
        raise ValueError('ANM 的轨道目录超出片段范围。')
    override, rate = struct.unpack_from('<hf', clip, control + 2)
    if not curve_count or not math.isfinite(rate) or rate <= 0:
        raise ValueError('ANM 的播放时长或速率无效。')
    last = (override if override > 0 else curve_count) - 1
    return control, curve_count, count, last, rate


def decode_anm(clip):
    control, curve_count, count, last, rate = anm_header(clip)
    directory_end = 40 + count * 14
    decoded, tracks, channel_index = {}, [], {}
    for i in range(count):
        start = 40 + i * 14
        logical_id = clip[start + 12]
        channels = []
        for channel, stride in enumerate((8, 16, 16)):
            a, b = struct.unpack_from('<HH', clip, start + channel * 4)
            a, b = 32 + 8 * a, 32 + 8 * b
            length = b - a or stride
            if a < directory_end or length <= 0 or length % stride or a + length > control:
                raise ValueError(f'逻辑轨道 {logical_id} 的关键帧范围无效。')
            key = (channel, a, b)
            if key not in decoded:
                keys = []
                for offset in range(a, a + length, stride):
                    if channel == 0:
                        x, y, z, frame = struct.unpack_from('<4H', clip, offset)
                        values = rotation(x, y, z)
                    else:
                        x, y, z, frame = struct.unpack_from('<4f', clip, offset)
                        values = (x, y, z)
                    if a == b:
                        frame = 0
                    if not all(math.isfinite(v) for v in (frame, *values)):
                        raise ValueError(f'逻辑轨道 {logical_id} 含非有限关键帧。')
                    keys.extend((frame, *values))
                decoded[key] = len(tracks)
                tracks.append(keys)
            channels.append(decoded[key])
        channel_index[logical_id] = channels
    local_translation, local_scale = struct.unpack_from('<BB', clip, control + 8)
    return {'channels': channel_index, 'tracks': tracks, 'first':max(0,s16(clip,control)), 'last': last, 'curve': curve_count - 1,
            'rate': rate, 'localTranslation': bool(local_translation), 'localScale': bool(local_scale)}


EVENTS = {
    0x00: ('hit', '攻击判定'),
    0x01: ('hit', '附加判定'),
    0x02: ('hit', '身体判定'),
    0x03: ('skill', '连段窗口'),
    0x04: ('hit', '判定参数'),
    0x05: ('hit', '特殊攻击判定'),
    0x06: ('skill', '技能参数 A'),
    0x07: ('skill', '技能参数 B'),
    0x08: ('effects', '技能特效'),
    0x09: ('other', '动作参数'),
    0x0A: ('hit', '附加判定'),
    0x0B: ('hit', '判定标志'),
    0x0C: ('other', '播放倍率'),
    0x0E: ('audio', '音效'),
    0x0F: ('audio', '节点可见时播放音效'),
    0x10: ('other', '手型节点'),
    0x15: ('other', '节点特效'),
    0x1B: ('other', '节点可见时设置动作参数'),
}

SHAPE_TEMPLATE_OFFSETS = {0x00: 0x16, 0x01: 0x08, 0x02: 0x08, 0x05: 0x20, 0x0A: 0x08}
# 08887414/08887674 populate the normal hit cache. 08887810/08887898
# populate the skill caches, including the word read by the throw HP routines.
DAMAGE_OFFSETS = {0: (8, 24), 5: (8, 34), 6: (4, 18), 7: (4, 18)}
# 08887414 / 08887674 write these shared fields. The readers are traced in the v7 EBOOT report.
ATTACK_FIELDS = {
    0x0A: ('连击回气值', '命中后累加到攻击方。连击结束时结算，倍率为 1 + 向下取整（连击数 ÷ 4），受气槽上限约束。'),
    0x0C: ('受击／防御反应号', '索引姿态和方向对应的受击状态表，以及站防与蹲防状态表。原生表索引为 0 至 24。'),
    0x0E: ('受击反应计时', '进入受击状态时写入反应上下文。部分受击状态用它判断何时结束，部分命中特效也用作持续量。'),
    0x10: ('防御类型', '1 仅站防，2 仅蹲防，3 站防与蹲防均可，4 在防御时进入重防反应。'),
    0x12: ('防御反应计时', '格挡时装入防御反应计时。普通格挡还有独立的 16 次更新上限。'),
    0x14: ('受击／格挡推移速度', '部分受击和格挡状态按攻击方的水平朝向、这个有符号速度及当前时间步推移防守方。'),
}


def base_damage(raw):
    field = DAMAGE_OFFSETS.get(raw[0])
    if field is not None and len(raw) >= field[1]:
        return s16(raw, field[0])
    return None


def read_shape(templates, index, node, layer):
    if not 0 <= index < len(templates):
        return None
    raw = templates[index]
    if len(raw) < 4:
        return None
    kind = u32(raw, 0)
    shape = {'type': kind, 'template': index, 'node': node, 'layer': layer}
    if kind == 1 and len(raw) >= 20:
        x, y, z, r = struct.unpack_from('<4f', raw, 4)
        if all(math.isfinite(n) for n in (x, y, z, r)) and r > 0:
            shape.update(center=(x, y, z), radius=r)
    elif kind == 2 and len(raw) >= 32:
        ax, ay, az, bx, by, bz, r = struct.unpack_from('<7f', raw, 4)
        if all(math.isfinite(n) for n in (ax, ay, az, bx, by, bz, r)) and r > 0:
            shape.update(center=(ax, ay, az), to=(bx, by, bz), radius=r)
    return shape


def decode_event(raw, templates, sound_code):
    opcode, mask, frame = struct.unpack_from('<BBh', raw)
    kind, name = EVENTS.get(opcode, ('other', '原生事件'))
    fields = {}
    event = {'op': opcode, 'kind': kind, 'name': name, 'mask': mask, 'frame': frame,
             'until': frame, 'fields': fields}
    size = len(raw)
    template_at = SHAPE_TEMPLATE_OFFSETS.get(opcode)
    if template_at is not None and size >= template_at + 2:
        node, span = struct.unpack_from('<hh', raw, 4)
        index = s16(raw, template_at)
        event['until'] = frame + max(0, span)
        fields.update({'MDL 节点': node if node >= 0 else '角色基准', '期间': span, '形状模板': index})
        layer = 'attack' if opcode in (0, 5) else 'body' if opcode == 2 else 'extra'
        event['shape'] = read_shape(templates, index, node, layer)
        if event['shape']:
            shape = event['shape']
            fields['模板类型'] = {1: '球', 2: '胶囊'}.get(shape['type'], shape['type'])
            if 'center' in shape:
                fields['模板坐标'] = [round(n, 3) for n in shape['center']]
                fields['模板半径'] = round(shape['radius'], 3)
            if 'to' in shape:
                fields['模板终点'] = [round(n, 3) for n in shape['to']]
        if opcode in (0, 5):
            fields.update((label, s16(raw, offset)) for offset, (label, _) in ATTACK_FIELDS.items())
    elif opcode in (6, 7) and size >= 18:
        fields.update({'防御字段': s16(raw, 12), '参数字': list(struct.unpack_from('<7h', raw, 4))})
    elif opcode == 3 and size >= 10:
        command, window, target = struct.unpack_from('<3h', raw, 4)
        event['until'] = frame + max(0, window)
        fields.update({'窗口长度': window, '命令编号': command, '目标 ANM': target})
    elif opcode == 8 and size >= 6:
        fields['特效工厂编号'] = s16(raw, 4)
    elif opcode in (4, 11) and size >= 6:
        fields['原生值'] = s16(raw, 4)
    elif opcode in (14, 15) and size >= 8:
        cue, node = struct.unpack_from('<hh', raw, 4)
        fields['音效编号'] = cue
        if opcode == 15:
            fields['MDL 节点条件'] = node
        code = sound_code(cue)
        if code is not None:
            bank, program, tone = (code >> 16) & 255, (code >> 8) & 255, code & 255
            fields.update({'音效编码': f'0x{code:06X}', '音库': bank, 'Program': program, 'Tone': tone})
    elif opcode == 12 and size >= 8:
        rate = struct.unpack_from('<f', raw, 4)[0]
        if math.isfinite(rate) and rate > 0:
            fields['倍率'] = round(rate, 5)
            event['rate'] = rate
    elif opcode == 16 and size >= 6:
        choice = s16(raw, 4)
        fields['显隐编号'] = choice
        if 0 <= choice < 12:
            event['hand'] = [choice // 6, choice % 6]
    elif opcode == 0x15 and size >= 10:
        fields['MDL 节点'] = s16(raw, 8)
    if not fields and size > 4:
        fields['原生参数字'] = [x[0] for x in struct.iter_unpack('<h', raw[4:size - size % 2])]
    damage = base_damage(raw)
    if damage is not None:
        event['damage'] = fields['基础伤害'] = damage
    return event


@dataclass(frozen=True)
class EventRecord:
    index: int
    offset: int
    raw: bytes
    overlap: bool


def merge_form_events(entries, selector, additions):
    """Replace one form's rows in directory order, keeping shared rows for the other forms."""
    slots, old = [], []
    last_slot = None
    for raw,replace in entries:
        if raw[1] & selector and replace:
            old.append(raw[:1] + bytes([selector]) + raw[2:])
            if raw[1] != selector:
                slots.append(raw[:1] + bytes([raw[1] & ~selector]) + raw[2:])
            last_slot = len(slots)
            slots.append(None)
        else:
            slots.append(raw)
    if old == additions:
        return None
    pending = iter(additions)
    rows = []
    for index,raw in enumerate(slots):
        if raw is not None:
            rows.append(raw)
        elif index == last_slot:
            rows.extend(pending)
        else:
            replacement = next(pending,None)
            if replacement is not None:
                rows.append(replacement)
    if last_slot is None:
        rows.extend(pending)
    return rows


def event_records(script):
    if len(script) < 4:
        return
    count = u32(script, 0)
    directory_end = 4 + 4 * count
    if directory_end > len(script):
        raise ValueError('BOM 事件目录超出脚本范围。')
    offsets = struct.unpack_from(f'<{count}I', script, 4)
    # An irregular directory can share bytes with another event's header.
    locations = sorted((offset, i) for i, offset in enumerate(offsets)
                       if directory_end <= offset <= len(script) - 4)
    ranges = {}
    for pos, (offset, i) in enumerate(locations):
        end = locations[pos + 1][0] if pos + 1 < len(locations) else len(script)
        before = locations[pos - 1][0] if pos else directory_end - 4
        ranges[i] = (end, end < offset + 4 or offset < before + 4)
    for i, offset in enumerate(offsets):
        if i not in ranges:
            continue
        end, overlap = ranges[i]
        yield EventRecord(i, offset, script[offset:max(offset + 4, end)], overlap)
