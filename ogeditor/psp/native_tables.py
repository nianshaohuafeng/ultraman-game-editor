"""Fighter, action, input, and music tables used by the v7 EBOOT."""

from collections import deque
from dataclasses import dataclass
import hashlib
import math
import struct
import zlib

from ogeditor.input_notation import input_arrows


ACTOR_TABLE = 0x089453B8
BGM_TABLE = 0x089CD284
FIGHTER_BGM = 0x089CD398
COMMAND_GETTERS = 0x089D3EC4
COMBO_UPDATES = frozenset(('0x0889820C', '0x088982DC', '0x0889A4D8'))
Q12 = 0x1000

# Zoffy's entry callbacks select these ANM and BOM indices when 089FBA78 is 2.
ZOFFY_RAGE_ENTRIES = {
    '0x0884C524': 57, '0x0884C5B8': 58, '0x0884C64C': 55,
    '0x0884C6E4': 56, '0x0884C204': 179,
}
LIFE_BANDS = 8
INITIAL_HP = 1024  # 08877804 → 08886BEC, before mode-specific setup.
ENERGY_TABLE = 0x08945BA4

THROWS = {
    101: '抓取尝试 A', 102: '抓取尝试 B', 103: '抓取尝试 C', 104: '抓空收势',
    105: '抓取成立 A', 106: '抓取成立 B', 107: '抓取成立 C', 108: '被抓成立',
    112: '被投过渡', 113: '被投落地', 114: '被投起身', 115: '投技出手', 116: '投技收势',
    117: '抓取分支 A', 118: '被抓分支 A', 119: '被抓分支 B', 120: '抓取分支 B',
    121: '抓取分支 C', 122: '抓取挣脱', 124: '专用抓取', 125: '抓取后受击',
    132: '被投演出 A', 133: '被投演出 B', 134: '被投演出 C', 135: '被投演出 D',
    136: '被投演出 E', 137: '被投演出 F', 141: '被投演出 G', 142: '被投演出 H',
    143: '被投演出 I', 144: '被投演出 J',
}

COMMAND_NAMES = tuple(map(input_arrows, (
    '保持前', '保持后', '保持上', '保持下', '保持后下', '保持后上', '保持前上',
    '点按前', '点按后', '点按下', '△', '×', '□', '○', 'R',
    '向前冲刺', '向后冲刺', '上方向连按', '下方向连按', '向前跑', '向后跑',
    '前滚', '后滚', '后 + □', '前 + □', '下 + □', '前下 + □', '后下 + □',
    '后 + △', '前 + △', '下 + △', '前下 + △', '后下 + △',
    '前 + ○', '后 + ○', '下 + ○', '后 + ×', '下 + ×', '前 + ×', '无方向及战斗键',
)))


def edit_action(move):
    return move['category'] == 5 or move['id'] in THROWS


def action_name(move):
    if move['id'] == 0:
        return '默认待机'
    if move['id'] == 14 and move['category'] == 1:
        return '←滚'
    if move['id'] in THROWS:
        return THROWS[move['id']]
    if move['category'] == 11:
        return f"投技状态 {move['id']}"
    if move['category'] == 5:
        return f"必杀状态 {move['id']}"
    return ''


@dataclass(frozen=True)
class NativeField:
    offset: int
    va: int
    fmt: str

    @property
    def size(self):
        return struct.calcsize(self.fmt)

    def read(self, data):
        return struct.unpack_from(self.fmt, data, self.offset)[0]

    def encode(self, value):
        if self.fmt == '<f':
            if type(value) not in (int, float) or not (0 < value <= 3.4028234e38):
                raise ValueError('请输入有限的正数倍率。')
            raw = struct.pack(self.fmt, value)
            if struct.unpack(self.fmt, raw)[0] <= 0:
                raise ValueError('倍率小于原生浮点精度。')
            return raw
        limit = 0xFFFE if self.fmt == '<H' else 0x7FFFFFFF
        if type(value) is not int or not 0 <= value <= limit:
            raise ValueError(f'请输入 0 至 {limit} 的整数。')
        return struct.pack(self.fmt, value)


class EnergyField(NativeField):
    def encode(self, value):
        if type(value) not in (int, float) or not 0 <= value <= 3.4028234663852886e38:
            raise ValueError('耗能须为有限的非负数。')
        packed = struct.pack('<f', value)
        if value and not struct.unpack('<f', packed)[0]:
            raise ValueError('耗能小于原生浮点精度。')
        return packed


def native_layout(segments, base, actions):
    def field(va, fmt):
        rva = va - base
        for segment in segments:
            rel = rva - segment['vaddr']
            if 0 <= rel <= segment['filesz'] - struct.calcsize(fmt):
                return NativeField(segment['offset'] + rel, va, fmt)
        raise ValueError('角色数据表超出 EBOOT 的文件段。')

    attributes = {}
    clips = {}
    for role, moves in enumerate(actions):
        start = ACTOR_TABLE + 96 * role
        for band in range(LIFE_BANDS):
            for name, pos, fmt in (('attack', 0, '<I'), ('defense', 32, '<I'), ('speed', 64, '<f')):
                attributes[role, band, name] = field(start + pos + 4 * band, fmt)
        for action, move in moves.items():
            if edit_action(move):
                clips[role, action] = field(int(move['va'], 16) + 14, '<H')
    return attributes, clips


def energy_layout(segments, base, count):
    size = 16 * count
    for segment in segments:
        relative = ENERGY_TABLE - base - segment['vaddr']
        if 0 <= relative <= segment['filesz'] - size:
            return [[EnergyField(segment['offset'] + relative + 16*r + 4*t,
                                 ENERGY_TABLE + 16*r + 4*t, '<f') for t in range(4)] for r in range(count)]
    raise ValueError('技能能量表超出当前 EBOOT 的装载段。')


def known_page_base(raw, canonical, patches):
    # The SELECT constructor call survives native edits, move sets and AI layers.
    site = 0xBB130
    if raw[site:site+4] == canonical[site:site+4]:
        return canonical
    data = patches.read_bytes()
    pos = 0
    while pos < len(data):
        instruction, size = struct.unpack_from('<4sI', data, pos)
        digest = data[pos+8:pos+40]
        packed = data[pos+40:pos+40+size]
        pos += 40+size
        if raw[site:site+4] != instruction:
            continue
        patch = zlib.decompress(packed)
        result = bytearray(canonical)
        at = 0
        while at < len(patch):
            offset,n = struct.unpack_from('<II',patch,at)
            at += 8
            if offset+n>len(result) or at+n>len(patch):
                raise ValueError('分页基底差异数据无效。')
            result[offset:offset+n] = patch[at:at+n]
            at += n
        if hashlib.sha256(result).digest() != digest:
            raise ValueError('分页基底校验失败。')
        return bytes(result)
    return canonical


def check_eboot(raw, digest, canonical, fields):
    if hashlib.sha256(raw).hexdigest() == digest:
        return
    if len(raw) != len(canonical) or hashlib.sha256(canonical).hexdigest() != digest:
        raise ValueError('无法核对奥0的 EBOOT 数据表。')
    restored = bytearray(raw)
    for field in fields:
        if field.read(raw) != field.read(canonical):
            field.encode(field.read(raw))
            start = field.offset
            restored[start:start + field.size] = canonical[start:start + field.size]
    if hashlib.sha256(restored).hexdigest() != digest:
        raise ValueError('此镜像的 EBOOT 与奥0受支持的版本不匹配。')


def command_catalog(data):
    # 088797C8 walks a named sequence of 14-byte input checks.
    count = struct.unpack_from('<H', data, 2)[0]
    cursor = 4
    rows = []
    for index in range(count):
        name_len = struct.unpack_from('<H', data, cursor)[0]
        end = cursor + 2 + name_len
        name = data[cursor + 2:end].rstrip(b'\0').decode('ascii')
        steps = struct.unpack_from('<H', data, end)[0]
        offset = end + 2
        controls = list(struct.iter_unpack('<7H', data[offset:offset + 14 * steps]))
        if len(controls) != steps:
            raise ValueError('手柄命令表的输入序列不完整。')
        rows.append({'id': index, 'name': COMMAND_NAMES[index] if index < len(COMMAND_NAMES) else name,
                     'symbol': name, 'offset': offset, 'steps': controls})
        cursor = offset + 14 * steps
    return rows


def command_slot(image, base, role, action):
    # The per-role getters compute &lists[state] in their return delay slot.
    getter = struct.unpack_from('<I', image, COMMAND_GETTERS - base + role * 4)[0]
    if struct.unpack_from('<H', image, getter - base + 2)[0] == 0x248C:
        first, limit, check, index, hi, lo, ret, bias, fallback, nop = struct.unpack_from(
            '<10I', image, getter - base)
        if (limit >> 16, check, index, hi >> 16, lo >> 16, ret, bias, fallback >> 26, nop) != (
                0x2D8D, 0x11A00005, 0x000C6080, 0x3C02, 0x2442, 0x03E00008, 0x004C1021, 2, 0):
            raise ValueError('配招的命令分支表布局不匹配。')
        start = 0x10000 - (first & 0xFFFF)
        if start <= action < start + (limit & 0xFFFF):
            arrays = ((hi & 0xFFFF) << 16) + ((lo & 0xFFFF) ^ 0x8000) - 0x8000
            return arrays + 4 * (action - start)
        getter = (getter & 0xF0000000) | ((fallback & 0x3FFFFFF) << 2)
    hi, shift, lo, ret, delay = struct.unpack_from('<5I', image, getter - base)
    if (hi >> 16, shift, lo >> 16, ret, delay) != (0x3C06, 0x42080, 0x24C5, 0x03E00008, 0x00851021):
        raise ValueError('此角色的命令分支表布局不匹配。')
    start = ((hi & 0xFFFF) << 16) + ((lo & 0xFFFF) ^ 0x8000) - 0x8000
    return start + 4 * action


def state_branches(image, base, role, action):
    pointer = struct.unpack_from('<I', image, command_slot(image, base, role, action) - base)[0]
    rows = []
    if not pointer:
        return rows
    while base <= pointer <= base + len(image) - 4:
        entry = struct.unpack_from('<I', image, pointer - base)[0]
        if not entry:
            return rows
        if not base <= entry <= base + len(image) - 16:
            break
        target, command, flags, priority = struct.unpack_from('<iIIi', image, entry - base)
        rows.append({'target': target, 'command': command, 'flags': flags, 'priority': priority,
                     'va': entry, 'input': COMMAND_NAMES[command] if command < len(COMMAND_NAMES) else f'命令 {command}'})
        pointer += 4
    raise ValueError('状态的命令分支表未正确结束。')


def command_paths(image, base, role, moves):
    """Use the shortest named input path through the movement states."""
    paths = {0: ()}
    pending = deque([0])
    while pending:
        action = pending.popleft()
        for branch in state_branches(image, base, role, action):
            target = branch['target']
            if target not in moves or target in paths or branch['command'] >= len(COMMAND_NAMES):
                continue
            paths[target] = (*paths[action], branch['input'])
            if moves[target]['category'] < 4:
                pending.append(target)
    # These entry phases are reached by native callbacks. They are not input edges
    # from idle. Resolve their command tables independently of the ground BFS.
    contexts = {'0x088950E0': '跳跃', '0x088953C4': '防御硬直'}
    for action, move in moves.items():
        context = contexts.get(move['callbacks'][1])
        if context is not None:
            for branch in state_branches(image, base, role, action):
                if branch['target'] in moves and branch['command'] < len(COMMAND_NAMES):
                    paths.setdefault(branch['target'], (context, branch['input']))
    return paths


def bgm_rows(game, base):
    for i in range((FIGHTER_BGM - BGM_TABLE) // 12):
        group, = struct.unpack_from('<I', game.image, BGM_TABLE - base + 12 * i)
        yield i, game.yum['groups'][group][3], group


def music_catalog(game, role, base):
    ids = {member: i for i, member, _ in bgm_rows(game, base)}
    default = struct.unpack_from('<i', game.image, FIGHTER_BGM - base + 4 * role)[0]
    library = []
    for member, group, _, kind in game.media_members:
        if kind == 'bgm':
            header = game.read_member(member, b'RIFF', 256)
            library.append({'id': ids.get(member), 'member': member, 'group': group, 'seconds': riff_duration(header)})
    return {'default': next(track for track in library if track['id'] == default), 'library': library}


def riff_duration(header):
    at = 12
    rate = samples = None
    while at + 8 <= len(header):
        kind, length = struct.unpack_from('<4sI', header, at)
        if kind == b'fmt ' and length >= 16 and at + 16 <= len(header):
            rate = struct.unpack_from('<I', header, at + 12)[0]
        if kind == b'fact' and length >= 4 and at + 12 <= len(header):
            samples = struct.unpack_from('<I', header, at + 8)[0]
        at += 8 + length + (length & 1)
    return round(samples / rate, 2) if rate and samples and math.isfinite(samples / rate) else None
