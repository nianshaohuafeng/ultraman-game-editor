"""FE3 DFS decoding and native resource formats."""

from dataclasses import dataclass
import base64
import math
import struct

from ogeditor.readers import get_pom_layout, render_pom_png
from kgf_readers.pom_to_tm2 import (POM_RULE_FE3_LEGACY, build_tim2_from_png_for_pom,
                                    replace_pom_texture_with_tim2)
from ogeditor.pom_palette import recolor_palette


WINDOW = 4096
LOOKAHEAD = 18
HEADER = 12


def _expand(data, size):
    # A zero-filled prefix is the initial dictionary. The previous 4096 output
    # bytes then represent the same history as the game's circular buffer.
    out = bytearray(WINDOW)
    end = WINDOW + size
    cursor = HEADER
    while len(out) < end:
        if cursor >= len(data):
            raise ValueError('compressed stream ended before output was complete')
        flags = data[cursor]
        cursor += 1
        if flags == 255:
            count = min(8, end - len(out))
            if cursor + count > len(data):
                raise ValueError('literal token is truncated')
            out.extend(data[cursor:cursor + count])
            cursor += count
            continue
        for _ in range(8):
            if flags & 1:
                if cursor >= len(data):
                    raise ValueError('literal token is truncated')
                out.append(data[cursor])
                cursor += 1
            else:
                if cursor + 2 > len(data):
                    raise ValueError('reference token is truncated')
                high = data[cursor + 1]
                slot = data[cursor] | ((high & 0xF0) << 4)
                cursor += 2
                current = len(out)
                count = min((high & 15) + 3, end - current)
                distance = ((current - LOOKAHEAD - slot) & (WINDOW - 1)) or WINDOW
                start = current - distance
                if count <= distance:
                    out.extend(out[start:start + count])
                else:
                    # Overlapping references repeat the bytes preceding the cursor.
                    pattern = out[start:current]
                    out.extend((pattern * ((count + distance - 1) // distance))[:count])
            if len(out) == end:
                break
            flags >>= 1
    return bytes(memoryview(out)[WINDOW:])


def decode_dfs_enc(data):
    if len(data) < HEADER:
        raise ValueError('file is too small to be a DFS .ENC file')
    if data[:4] != b'DFS\x01':
        raise ValueError('unsupported header: expected DFS version 1')
    stored = int.from_bytes(data[8:12], 'little')
    if stored != len(data):
        raise ValueError(f'size field mismatch: header says {stored}, file is {len(data)}')
    return _expand(data, int.from_bytes(data[4:8], 'little'))


def unpacked_tag(header):
    if header[:4] != b'DFS\x01':
        return header[:4]
    if len(header) < HEADER + 1:
        raise ValueError('DFS 压缩头不完整。')
    size = min(int.from_bytes(header[4:8], 'little'), 4)
    literals = (1 << size) - 1
    if header[HEADER] & literals == literals and len(header) >= HEADER + 1 + size:
        return header[HEADER + 1:HEADER + 1 + size]
    return _expand(header, size)


BOM_SIZES = (24,12,12,12,12,12,8,0,20,8,20,8,12,8,8,8,8,8,8,8,8,8,8,12,36,8,8,8,12)
BOM_NAMES = (
    '攻击判定', '抓取判定', '判定形状 B', '判定形状 C', '判定形状 D', '连段接续', '设置接触判定类别',
    '保留', '攻击参数', '执行角色动作回调', '替代攻击参数', '设置防御解除许可', '判定形状 E', '设置场地接触标记', '播放倍率',
    '节点命中特效', '音效', '节点音效', 'VAU 采样', '场地粒子喷发', '场地粒子散射', '模型显隐', '镜头震动',
    '节点扬尘', '扩展攻击判定', '默认后继改用状态 1', '追加自身位移', '画面残影', '指定角色追加位移',
)
# 0x2031C8 and 0x203F10 populate the same Attack_Control fields.
# Separate consumers use the reaction, meter, two timers and displacement.
ATTACK_FIELDS = {
    0: ('MDL 节点', '攻击判定挂在该模型节点上。'),
    1: ('持续帧', '从触发帧起的判定寿命，到触发帧加持续帧时移除。'),
    2: ('基础伤害', '作为扣血计算的输入，实际扣血还受攻防倍率与规则修正。'),
    3: ('眩晕累积基值', '传入 0x2B4120，经过规则修正后增加对手的眩晕槽。'),
    4: ('命中反应类型', '0x27B3A8 读取此编号，结合对手状态选择命中反应。'),
    5: ('受击硬直帧', '供 0x2C5EE8 等受击更新回调判断退出时限，实际时长受状态与规则影响。'),
    6: ('保留字段（原版）', '原版写入 Attack_Control +0x30。主 ELF 和随盘 ERX 无消费此值的读取路径。'),
    7: ('防御硬直帧', '防御硬直进入回调 0x2C2F40 以此初始化倒计时。'),
    8: ('受击推移量', '0x2CB6D0 按此值和时间增量施加沿攻击者朝向的推移。'),
}
ALTERNATE_ATTACK_FIELDS = {9:2, 10:3, 11:4, 12:5, 13:8}
ATTACK_ALTERNATE_HINT = '攻击者字段 +0x748 大于 0 时，命中处理可使用此组数值。'
# Opcodes 8 and 10 can set damage without creating a shape.
BOM_DAMAGE_FIELDS = {0:2, 8:0, 10:0, 24:2}
def damage_fields(op):
    if op in BOM_DAMAGE_FIELDS:
        yield BOM_DAMAGE_FIELDS[op], '基础伤害'
    if op == 24:
        for index, source in ALTERNATE_ATTACK_FIELDS.items():
            if source == 2:
                yield index, '变体基础伤害'


ATTACK_PARAMETER_FIELDS = {0:2, 1:3, 2:4, 3:5, 4:6, 5:7, 6:8}
ATTACK_FIELDS_BY_OP = {
    0: ATTACK_FIELDS,
    8: {index: ATTACK_FIELDS[source] for index, source in ATTACK_PARAMETER_FIELDS.items()},
    10: {**{index: ATTACK_FIELDS[source] for index, source in ATTACK_PARAMETER_FIELDS.items() if index not in (4, 5)},
         4: ('保留字段（原版）', '备用攻击参数写入 Attack_Control +0x8C，原版无消费读取。'),
         5: ('保留字段（原版）', '备用攻击参数写入 Attack_Control +0x90，原版无消费读取。')},
    24: {**ATTACK_FIELDS,
         **{index: ('变体' + ATTACK_FIELDS[source][0], ATTACK_ALTERNATE_HINT)
            for index, source in ALTERNATE_ATTACK_FIELDS.items()}},
}

SHAPE_TEMPLATE = ('形状模板', '引用本 BOM 的形状表。')
BOM_FIELDS = {
    0: {9:SHAPE_TEMPLATE},
    5: {0:('接续输入编号', '连段窗口内匹配的游戏输入编号。'),
        1:('接续窗口持续帧', '从触发帧起计算。窗口在触发帧加持续帧时结束。'),
        2:('接续目标 ANM', '匹配接续输入后选用的 ANM 编号。')},
    6: {0:('接触判定类别', '原盘范围为 0～6。影响额外接触判定及命中反应筛选。')},
    9: {0:('角色动作回调编号', '按原生动作事件表 0x3D0840 调用回调。值 −1 表示跳过调用。')},
    11: {0:('对手防御解除许可', '0 让已进入防御等待的对手继续保持。非 0 允许原生防御退出流程。')},
    13: {0:('场地接触处理标记', '由场地碰撞更新读取。0 可进入首次接触处理分支，1 是接触处理时写回的状态。')},
    14: {0:('播放倍率', '1.0 表示原速。该倍率在后续播放更新中使用。')},
    15: {0:('命中特效挂点', '用该 MDL 节点的世界坐标触发特殊命中特效。')},
    16: {0:('音效编号', '按当前角色和场景的音效表解析。')},
    17: {0:('音效编号', '目标节点可用时提交该音效。'),
         1:('音效触发节点', '读取此 MDL 节点的启用标志，再决定是否播放。')},
    18: {0:('VAU 采样编号', '引用当前模型所绑定的 VAU 采样表。')},
    19: {0:('粒子喷发挂点', '用该 MDL 节点的世界坐标生成场地粒子。'),
         1:('粒子喷发样式', '选择 0x2ED168 内的粒子生成分支。')},
    20: {0:('粒子散射挂点', '用该 MDL 节点的世界坐标生成场地粒子。'),
         1:('粒子散射样式', '选择 0x2ED608 内的散射分支。')},
    21: {0:('隐藏模型', '1 隐藏模型。其他值显示模型。')},
    22: {0:('镜头震动持续帧', '给原生镜头震动设置倒计时。')},
    23: {0:('扬尘缩放倍率', '与模型比例相乘，用于设置节点扬尘的粒子参数。'),
         2:('扬尘挂点', '取此 MDL 节点的世界坐标作为扬尘起点。')},
    24: {14:SHAPE_TEMPLATE},
    26: {0:('自身本地 Z 位移', '累加到角色移动向量，并按角色坐标系转换。')},
    27: {0:('残影持续帧', '控制画面残影对象的倒计时。')},
    28: {0:('自身本地 Z 位移', '只有角色编号匹配且触发帧非零才累加此位移。'),
         2:('触发角色编号', '用原生角色编号筛选这条位移指令。')},
}
for shape_op in (1, 2, 3, 4, 12):
    BOM_FIELDS[shape_op] = {0:ATTACK_FIELDS[0], 1:ATTACK_FIELDS[1], 2:SHAPE_TEMPLATE}

BOM_FLOAT = frozenset((14, 23, 26, 28))
BOM_PADDING = {
    (1,3), (2,3), (3,3), (4,3), (5,3), (6,1), (8,7), (9,1), (10,7), (11,1),
    (12,3), (13,1), (15,1), (21,1), (22,1), (23,3), (24,15),
    (25,1), (27,1), (28,3),
}
BOM_UNUSED = {
    (16,1): ('音效备用半字', '音效处理器复制此值。原版播放逻辑使用前一个音效编号。'),
    (18,1): ('VAU 备用半字', '事件解析器复制此半字。VAU 原生采样只使用前一个编号。'),
    (25,0): ('后继指令保留半字', '此指令原生固定设置后继标记，执行时不采用这个值。'),
}
BOM_READ_ONLY = {(0,6), (8,4), (10,4), (10,5), (24,6)}
NATIVE_TAGS = {b'MDL\0':'mdl', b'ANM\0':'anm', b'BOM\0':'bom', b'POM\0':'pom',
               b'vau\0':'vau', b'VAGp':'vag', b'bobb':'bobb', b'CBBB':'cbbb',
               b'CMSP':'cmsp', b'ipum':'ipum'}
EDITABLE = frozenset(('mdl', 'anm', 'bom', 'pom'))


def require(ok, message):
    if not ok:
        raise ValueError(message)


def word(data, offset):
    return struct.unpack_from('<I', data, offset)[0]


def align(value, quantum):
    return (value + quantum - 1) & -quantum


def pom_layout(data):
    layout = get_pom_layout(data)
    require(layout['rule'] == POM_RULE_FE3_LEGACY and layout['total_size'] <= len(data),
            '需要完整的格斗进化3 PS2 POM。')
    return layout


def png_for_pom(data, palette):
    layout = pom_layout(data)
    require(type(palette) is int and 0 <= palette < layout['palette_count'], '请选择有效色表。')
    return render_pom_png(data, auto_color_key=False, palette_index=palette)[2]


def png_preview(data, palette, gs_alpha=False):
    if gs_alpha:
        layout = pom_layout(data)
        require(type(palette) is int and 0 <= palette < layout['palette_count'], '请选择有效色表。')
        start = layout['palette_offset'] + palette * layout['palette_stride']
        stop = start + layout['palette_block_size']
        colors = bytearray(data[start:stop])
        colors[3::4] = bytes(min(255, alpha * 2) for alpha in colors[3::4])
        data = data[:start] + bytes(colors) + data[stop:]
    return 'data:image/png;base64,' + base64.b64encode(png_for_pom(data, palette)).decode('ascii')


def paste_png(pom, png, palette, palette_only):
    current = pom_layout(pom)
    if palette_only:
        return recolor_palette(pom, current, png, palette)
    processed = build_tim2_from_png_for_pom(pom, png, palette)
    return replace_pom_texture_with_tim2(pom, processed, palette)


def model_signature(data):
    tag, size, v1, v2, nodes, secondary, fragments, tail, count, _, pieces, _ = struct.unpack_from('<4s11I', data)
    require((tag, size, v1, v2, nodes) == (b'MDL\0', len(data), 4, 5, 0x30), 'FE3 MDL 头无效。')
    require(0 < count and nodes + 0x70 * count == secondary == fragments <= tail == size - 16,
            'FE3 MDL 节点或片段边界无效。')
    signature = []
    for i in range(count):
        links = struct.unpack_from('<4h2H', data, nodes + i * 0x70)
        require(all(-1 <= node < count for node in links[:4]) and links[4] in range(4),
                'FE3 MDL 节点连接无效。')
        require(links[4] != 3 or links[5] < count, 'FE3 MDL 附着节点无效。')
        signature.append(links[:5] + (links[5] if links[4] == 3 else 0,))
    pos, owners = fragments, []
    for index in range(pieces):
        require(pos + 0x16 <= tail, 'FE3 MDL 片段被截断。')
        stride, geometry_size, payload, bounds, shared, references = struct.unpack_from('<4IiH', data, pos)
        require(stride > 0 and stride % 16 == geometry_size % 16 == 0 and pos + stride <= tail and references > 0
                and bounds == align(22 + 2 * references, 16) and payload == bounds + 16 * references,
                'FE3 MDL 片段大小无效。')
        require(all(node < count for node in struct.unpack_from(f'<{references}H', data, pos + 22)),
                'FE3 MDL 片段节点索引无效。')
        if shared == -1:
            require(payload + geometry_size == stride and geometry_size >= 0xA8, 'FE3 MDL 几何范围无效。')
        else:
            require(0 <= shared < index and owners[shared][0] == -1 and owners[shared][1] >= geometry_size
                    and payload == stride, 'FE3 MDL 共用几何索引无效。')
        owners.append((shared, geometry_size))
        pos += stride
    require(pos == tail, 'FE3 MDL 片段目录长度无效。')
    return tuple(signature), pieces


@dataclass(frozen=True)
class Clip:
    start: int
    size: int
    frames: int
    metadata: int
    lower: int
    upper: int
    rate: float
    tracks: tuple

    @property
    def last_frame(self):
        # EE 0x328FA0 supplies the same frame count from either field to 0x32A418.
        return max(0,(self.upper if self.upper > 0 else self.frames)-1)

    @property
    def fields(self):
        return {'frames':self.frames, 'first':self.lower, 'last':self.upper, 'rate':self.rate}


def clips(data):
    require(data[:4] == b'ANM\0' and len(data) >= 12, '需要 FE3 ANM 库。')
    count = word(data, 4)
    require(0 < count <= (len(data) - 8) // 4, 'FE3 ANM 目录无效。')
    offsets = struct.unpack_from(f'<{count}I', data, 8)
    result = []
    for start, end in zip(offsets, (*offsets[1:], len(data))):
        require(start % 16 == 0 and 8 + count * 4 <= start <= end - 64 and end <= len(data),
                'FE3 ANM 片段范围无效。')
        tag, size, reserved, version, sample, meta, aux, aux2 = struct.unpack_from('<4s7I', data, start)
        require((tag, size, reserved, version, sample) == (b'ANM\0', end - start, 0, 3, 0x20)
                and 0x28 <= meta and meta + 16 == aux == aux2 == size - 16, 'FE3 ANM 片段头无效。')
        frames, number = struct.unpack_from('<2H', data, start + 0x24)
        lower, upper, speed = struct.unpack_from('<hhf', data, start + meta)
        table_end = 0x28 + number * 14
        require(table_end <= meta and math.isfinite(speed) and (upper <= 0 or 0 <= lower < upper),
                'FE3 ANM 轨道或播放参数无效。')
        tracks = []
        for index in range(number):
            track = struct.unpack_from('<7H', data, start + 0x28 + 14 * index)
            require(track[6] < 256, 'FE3 ANM 轨道编号无效。')
            for lo, hi, stride in zip(track[0:6:2], track[1:6:2], (8, 16, 16)):
                require(lo <= hi and (8 * (hi - lo)) % stride == 0 and 0x20 + 8 * lo >= table_end and
                        0x20 + 8 * hi + (stride if hi == lo else 0) <= meta, 'FE3 ANM 关键帧范围无效。')
            tracks.append(track[6])
        result.append(Clip(start, size, frames, start + meta, lower, upper, speed, tuple(tracks)))
    return result


class Bom:
    def __init__(self, data):
        require(data[:4] == b'BOM\0' and len(data) >= 12, '需要 FE3 BOM。')
        self.data = data
        self.scripts, self.shapes = struct.unpack_from('<2I', data, 4)
        count = self.scripts + self.shapes
        require(count <= (len(data) - 12) // 4, 'BOM 目录被截断。')
        self.offsets = struct.unpack_from(f'<{count}I', data, 12)
        order = sorted(self.offsets)
        require(len(set(order)) == count and all(12 + 4 * count <= pos <= len(data) - 4 and pos % 4 == 0
                                                 for pos in order), 'BOM 记录边界无效。')
        self.ends = dict(zip(order, (*order[1:], len(data))))
        for pos in self.offsets[self.scripts:]:
            require((word(data, pos), self.ends[pos] - pos) in ((1, 20), (2, 32)), 'BOM 形状记录无效。')

    def script_offset(self, script):
        require(type(script) is int and 0 <= script < self.scripts, '请选择有效 BOM 脚本。')
        return self.offsets[script]

    def counts(self):
        return [word(self.data, offset) for offset in self.offsets[:self.scripts]]

    def events(self, script, parameters=True):
        begin = self.script_offset(script)
        end = self.ends[begin]
        count = word(self.data, begin)
        require(count <= (end - begin - 4) // 4, 'BOM 事件目录被截断。')
        offsets = struct.unpack_from(f'<{count}I', self.data, begin + 4)
        if not offsets:
            require(end - begin == 4, 'BOM 空脚本边界无效。')
        else:
            require(offsets[0] == 4 + 4 * count, 'BOM 事件起点无效。')
        for index, (off, stop) in enumerate(zip(offsets, (*offsets[1:], end - begin))):
            require(off + 4 <= stop <= end - begin, 'BOM 事件范围无效。')
            start = begin + off
            op, selector, frame = struct.unpack_from('<BBh', self.data, start)
            require(op < len(BOM_SIZES) and BOM_SIZES[op] == stop - off, 'BOM 事件操作码或长度无效。')
            args = struct.unpack_from(f'<{(stop - off - 4) // 2}h', self.data, start + 4)
            params = []
            skip = 0
            attack_fields = ATTACK_FIELDS_BY_OP.get(op, {})
            field_names = BOM_FIELDS.get(op, {})
            for j, value in enumerate(args if parameters else ()):
                if j < skip:
                    continue
                raw = 4 + 2 * j
                definition = attack_fields.get(j) or field_names.get(j)
                editable = definition is not None and (op,j) not in BOM_READ_ONLY
                if definition is None:
                    definition = BOM_UNUSED.get((op,j))
                if definition is None and (op,j) in BOM_PADDING:
                    definition = ('记录尾部填充', '原生处理器不采用这一记录尾半字。')
                name, help_text = definition or ('用途待确认', '此原始值供对照。')
                fmt = 'f' if not j and op in BOM_FLOAT else 'h'
                if fmt == 'f':
                    value, = struct.unpack_from('<f', self.data, start + raw)
                    value = value if math.isfinite(value) else None
                    skip = 2
                param = {'name':name,'offset':raw,'type':fmt,'value':value,'editable':editable,'help':help_text}
                params.append(param)
            damage_field = BOM_DAMAGE_FIELDS.get(op)
            base_damage = args[damage_field] if damage_field is not None else None
            # The damage key identifies attacks that generate a hit shape.
            yield {'id':index, 'offset':start, 'op':op, 'selector':selector, 'frame':frame, 'args':args,
                   'name':BOM_NAMES[op], 'base_damage':base_damage, 'damage':base_damage if op in (0, 24) else None,
                   'params':params}

    def validate(self):
        for script in range(self.scripts):
            for _ in self.events(script, parameters=False):
                pass
