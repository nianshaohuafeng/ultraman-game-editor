"""FE3 native skill tables, program modules and post-knockback charge timing."""

from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
import math
import struct

from ogeditor.input_notation import SEQUENCE, input_arrows
from ogeditor.ps2.formats import require, word


ROLE_COUNT = 39
SKILL_CANDIDATES = 20
FINISHER_TABLE = 0x3E4E78
SKILL_TABLES = ((0x3E4FB8, 12), (FINISHER_TABLE, 20), (0x3E4F18, 12))
GUARD_TYPES = {0:'关闭屏障效果', 1:'类型 1 · 通常屏障', 2:'类型 2 · 特殊防御', 3:'类型 3 · 特殊防御'}
# 0x2CF418 interprets these as signed words. -2 dispatches the role's CHelpBase.
SHOULDER_ASSIST = -2
SHOULDER_ASSIST_WORD = SHOULDER_ASSIST & 0xFFFFFFFF
AID_DISPATCH = 0x48A690
NO_AID = 0x25B0E0
DEFAULT_FINISHERS = 0x3D1A00
FINISHER_SLOTS = ('△', '×', '□', '○')
RATING_TABLE = 0x3C74C0
RATING_GRADES = ('S', 'A', 'B', 'C', 'D', 'E')
RATING_THRESHOLDS = (200, 130, 90, 60, 20)
MIN_RATED_SCORE = 5
CANDIDATE_MULTIPLIER = 2
MIN_RATING_VALUE = (MIN_RATED_SCORE + CANDIDATE_MULTIPLIER - 1) // CANDIDATE_MULTIPLIER
INPUT_RESOURCE = 0x9D1
PAD_BITS = ((0x2000,'前'),(0x8000,'后'),(0x1000,'上'),(0x4000,'下'),(0x80,'□'),(0x10,'△'),
            (0x20,'○'),(0x40,'×'),(4,'L1'),(1,'L2'),(8,'R1'),(2,'R2'))
FORM_LABELS = {
    7: ('原始', '分身'),
    17: ('复合型', '强力型', '空中型'),
    18: ('闪亮型', '奇迹型', '强壮型'),
    19: ('V1', 'V2', '至高型'),
    20: ('V1', 'V2'),
    21: ('月神型', '日冕型', '日蚀型'),
    22: ('标准型', '粉碎型'),
}
# The keys are the candidate numbers carried by the native records. They also
# index the 14-pixel name rows. The slot value can differ from this number.
FINISHER_NAMES = {
    0: {1:'八分光轮', 2:'斯派修姆光线', 3:'奥特攻击光线', 4:'奥特空中捕捉', 5:'捕捉光环', 6:'奥特水流'},
    1: {0:'艾梅利姆光线 A', 1:'头镖', 2:'集束射线', 3:'艾梅利姆光线 B', 4:'艾梅利姆光线 C',
        5:'奥特撞击战法', 6:'手里剑光线连打'},
    2: {1:'奥特长矛', 2:'奥特火花', 3:'斯派修姆光线', 4:'希奈拉玛射线', 5:'雾状光线', 6:'奥特切割',
        7:'切割手刀', 8:'奥特投掷', 9:'奥特飓风'},
    3: {1:'垂直断头刀', 2:'梅塔利姆光线', 3:'断头刀射击', 4:'宇宙 Q', 6:'冲孔镭射',
        7:'冲孔镭射特别版', 8:'奥特断头刀', 9:'水平断头刀', 10:'环形断头刀', 11:'星星光线', 12:'艾斯之刃'},
    4: {1:'斯特利姆光线', 2:'奥特炸弹', 3:'新斯特利姆光线', 4:'泰罗喷流', 5:'宇宙奇迹光线'},
    5: {0:'雷欧飞踢', 1:'奥特双重闪光', 2:'手刀切割', 3:'雷欧双截棍', 4:'能量光球', 5:'射击光束', 6:'火花光线'},
    6: {0:'M87 光线 A', 2:'M87 光线 B'},
    7: {1:'白色破坏光线', 2:'分身弹丸连打'},
    8: {1:'分离怪光线'}, 9: {1:'贝姆斯塔光束'},
    10: {0:'奥特手镯', 1:'必杀光线连打'}, 11: {2:'铁索大回旋'},
    12: {0:'佩刀光束', 1:'基拉斯旋转'}, 13: {0:'突进', 1:'尾巴攻击连打'},
    14: {1:'一兆度火球'}, 15: {1:'缩小光线'},
    16: {0:'沙库修姆光线', 1:'腰带光束', 2:'月面翻身踢', 3:'奥特光矛'},
    17: {0:'哉佩利敖光线', 1:'迪拉修姆光流', 2:'兰帕尔特光弹', 3:'头部撞击',
         4:'迪迦切割', 5:'迪迦冷冻', 6:'计时器闪光特别版'},
    18: {0:'索尔捷特光线', 1:'立波留姆光波', 2:'闪亮旋风', 3:'加尔奈特轰炸', 4:'巴尔坎挥击'},
    19: {0:'光子冰刀', 1:'量子流线', 2:'盖亚暴雪', 3:'闪耀之刃', 4:'光子流线'},
    20: {0:'清算者', 1:'光子粉碎机', 2:'光子螺旋', 3:'阿古茹流线'},
    21: {0:'满月光波', 1:'克兹缪姆光线', 2:'高斯打击', 3:'未来力量', 4:'交叉完美'},
    22: {0:'比克托流姆光线', 1:'达格流姆光线', 2:'交叉完美'},
    23: {0:'火花传说'},
    24: {0:'超音波光线'}, 25: {0:'冷气攻击', 1:'火焰弹连打'}, 26: {0:'体内吸收'},
    27: {0:'吉尔萨迪斯光束'}, 28: {0:'岩石投掷', 1:'岩石连续攻击'}, 29: {0:'残酷鞭击连打'},
    30: {0:'连续砸击'}, 31: {0:'全弹发射'}, 32: {0:'邪恶射线'}, 33: {0:'机械梅塔利姆光线'},
    34: {0:'伪艾梅利姆光线'}, 35: {0:'黑暗索尔捷特光线'}, 36: {0:'阿斯特拉飞踢', 1:'奥特双重闪光'},
    37: {0:'贯通激光'}, 38: {0:'捕食'},
}
SKILL_NAME_ATLASES = 0x0B81
# Candidate identities from the original customization atlas (0x0B81).
GUARD_NAMES = {
    0:{0:'反弹光线'}, 1:{7:'奥特屏障'}, 2:{0:'奥特防御盾'}, 3:{0:'环形屏障'}, 4:{0:'泰罗屏障'},
    5:{7:'雷欧伞'}, 6:{1:'奥特屏障'}, 7:{0:'分身术'}, 9:{0:'必杀技吸收'},
    14:{0:'杰顿屏障', 2:'杰顿最终光束'}, 16:{4:'奥特 V 屏障', 5:'反击沙库修姆光线'},
    17:{7:'奥特护盾'}, 18:{5:'立波留姆光波', 6:'奥特屏障'}, 19:{5:'奥特屏障'}, 20:{4:'奥特屏障'},
    21:{5:'月光屏障', 6:'金光屏障'}, 22:{3:'杰斯提斯屏障'}, 24:{1:'必杀技吸收'},
    26:{1:'必杀技吸收'}, 32:{1:'邪恶屏障'}, 35:{1:'黑暗屏障'},
}
REINFORCEMENT = (SHOULDER_ASSIST, '援护攻击')
POWER_UP = (SHOULDER_ASSIST, '力量提升')
# Original (action, name) pairs retain their identity when a reopened ISO
# contains an edited routing word. Each action is still checked against its ERX.
SHOULDER_SKILLS = {
    0:{7:(341, '切割光线'), 8:REINFORCEMENT},
    1:{8:(339, '手部射击'), 9:(341, '眼部光束'), 10:REINFORCEMENT},
    2:{10:(340, '奥特射击'), 11:REINFORCEMENT},
    3:{5:(336, '闪光手'), 13:(347, '计时器射击'), 14:(349, '艾斯之火'), 15:(351, '钻石光线'), 16:REINFORCEMENT},
    4:{6:(340, '射击光束'), 7:(342, '箭形光线'), 8:REINFORCEMENT},
    5:{8:(342, '奥特射击'), 9:(345, '红色手里剑光束'), 10:REINFORCEMENT},
    6:{3:(334, 'Z 光线'), 4:REINFORCEMENT},
    7:{3:(333, '冻结光线'), 4:REINFORCEMENT},
    8:{0:(330, '怪光线'), 2:REINFORCEMENT},
    9:{2:(333, '冲刺攻击'), 3:(SHOULDER_ASSIST, '自动吸收')},
    10:{2:(332, '磁力光弹'), 3:POWER_UP},
    11:{0:(331, '冷气攻击'), 1:(333, '火焰攻击'), 3:(336, '吼叫'), 4:POWER_UP},
    12:{2:(333, '装备钩爪'), 3:(SHOULDER_ASSIST, '落雷攻击')},
    13:{2:(333, '吼叫'), 3:(SHOULDER_ASSIST, '凶暴化')},
    14:{3:(SHOULDER_ASSIST, '瞬间移动'), 4:REINFORCEMENT},
    15:{0:(331, '换脸'), 2:REINFORCEMENT},
    16:{6:(339, '奥特双重箭'), 7:(341, '身体火花'), 8:REINFORCEMENT},
    17:{8:(345, '手刀切割'), 9:(347, '形态切换 A'), 10:(348, '形态切换 B'), 11:REINFORCEMENT},
    18:{7:(344, '手刀切割'), 8:(346, '形态切换 A'), 9:(347, '形态切换 B'), 10:REINFORCEMENT},
    19:{6:(337, '盖亚切割'), 7:(339, '版本升级'), 8:REINFORCEMENT},
    20:{5:(336, '阿古茹切割'), 6:(338, '阿古茹之刃'), 7:REINFORCEMENT},
    21:{7:(337, '日蚀火花'), 8:(339, '形态切换'), 9:REINFORCEMENT},
    22:{4:(335, '杰斯提斯重击'), 5:(337, '形态切换'), 6:REINFORCEMENT},
    24:{2:(331, '吼叫'), 3:POWER_UP},
    25:{2:(332, '形态切换'), 3:(SHOULDER_ASSIST, '热线攻击')},
    26:{2:(333, '大笑'), 3:REINFORCEMENT},
    27:{1:(331, '喷射冲刺'), 2:POWER_UP},
    28:{2:(332, '吼叫'), 3:POWER_UP},
    29:{1:(331, '挥舞鞭子'), 2:POWER_UP},
    30:{1:(319, '暴动'), 2:POWER_UP},
    31:{1:(331, '角导弹'), 2:(333, '火焰喷射'), 3:(335, '鼻导弹'), 4:(337, '小型导弹'), 5:POWER_UP},
    32:{2:(332, '邪恶切割'), 3:POWER_UP},
    33:{1:(332, '钻石光线'), 2:POWER_UP},
    34:{1:(332, '足球踢'), 2:POWER_UP},
    35:{2:(332, '黑暗切割'), 3:POWER_UP},
    36:{2:(332, '红色手里剑光束'), 3:REINFORCEMENT},
    37:{1:(320, '黑色气体'), 2:POWER_UP},
}


def native_offset(elf, va, length=1):
    table = word(elf, 28)
    entry_size, count = struct.unpack_from('<HH', elf, 42)
    for i in range(count):
        kind, offset, address, _, size, _, _, _ = struct.unpack_from('<8I', elf, table + i * entry_size)
        if kind == 1 and address <= va and va + length <= address + size:
            return offset + va - address
    raise ValueError(f'地址 0x{va:08X} 超出奥3 ELF 文件段。')


@dataclass(frozen=True)
class Form:
    id: int
    label: str
    model: tuple
    bom: tuple | None
    vau: tuple | None
    textures: tuple
    secondary: tuple
    tracks: tuple
    accessory: bool


@dataclass(frozen=True)
class Skill:
    id: int
    menu_mask: int
    value: int
    va: int
    kind: int
    words: tuple
    label: str
    menu_candidate: bool = True

    @property
    def action(self):
        return self.words[0]

    @property
    def power(self):
        return struct.unpack('<f', struct.pack('<I', self.words[3]))[0] if self.kind == 1 else None


def rating_address(role, candidate):
    return RATING_TABLE + 4 * (SKILL_CANDIDATES * role + candidate)


def rating_grade(value, multiplier=CANDIDATE_MULTIPLIER):
    # SLPS_254.41 at 0x240990 uses strict thresholds through D and includes 5 for E.
    score = value * multiplier
    for rank, threshold in zip(RATING_GRADES, RATING_THRESHOLDS):
        if score > threshold:
            return rank
    return 'E' if score >= MIN_RATED_SCORE else '—'


def rating_value_for_grade(value, grade):
    require(grade in RATING_GRADES, '必杀评级须为 S、A、B、C、D 或 E。')
    index = RATING_GRADES.index(grade)
    high = RATING_THRESHOLDS[index - 1] // CANDIDATE_MULTIPLIER if index else 0x7FFFFFFF
    low = (RATING_THRESHOLDS[index] // CANDIDATE_MULTIPLIER + 1
           if index < len(RATING_THRESHOLDS) else MIN_RATING_VALUE)
    return min(high, max(low, value))


class NativeTables:
    def __init__(self, elf):
        self.elf = elf

    def at(self, address, fmt):
        offset = native_offset(self.elf, address, struct.calcsize(fmt))
        return struct.unpack_from(fmt, self.elf, offset)

    def words(self, table, count):
        return self.at(table, f'<{count}I') if count else ()

    def pointer(self, table, i):
        return self.at(table + 4 * i, '<I')[0]

    def action_category(self, role, state):
        # The selector in 0x2C1CF0 uses the low nibble of the action attribute.
        first = 0x131
        table, index = ((0x407690, state) if state < first
                        else (self.pointer(0x4090D0, role), state - first))
        return self.pointer(table, index) & 15

    def descriptor(self, role):
        require(type(role) is int and 0 <= role < ROLE_COUNT, '请选择奥3角色。')
        desc = self.words(self.pointer(0x3E38C0, role), 21)
        return desc, self.words(desc[20], 14)

    def form_words(self, role, slot):
        desc, sizes = self.descriptor(role)
        require(type(slot) is int and 0 <= slot < sizes[4], '请选择角色的原生形态槽。')
        poms = self.pointer(desc[16], slot)
        count = self.pointer(sizes[9], slot)
        return ((('MDL', desc[7] + 4 * slot), ('VAU', desc[14] + 4 * slot), ('BOM', desc[15] + 4 * slot))
                + tuple((f'POM {index}', poms + 4 * index) for index in range(count)))

    def forms(self, game, role):
        desc, sizes = self.descriptor(role)
        count, package = sizes[4], desc[0]
        result = {}
        def resolve(value, kind):
            if value >> 24 == 0xFE:
                previous = result.get((value >> 8) & 255)
                require(previous is not None, '形态资源别名没有源模型。')
                value = getattr(previous, {3:'bom', 5:'textures', 6:'secondary'}[(value >> 16) & 255])
                return value
            if value == 0xFFFFFFFF:
                return None
            key = package, value
            require(game.part(*key).kind == kind, f'原生模型绑定 {package:04X}/{value} 的格式不符。')
            return key

        def textures(slot, size):
            raw = self.words(self.pointer(desc[slot], i), self.pointer(size, i))
            if raw and raw[0] >> 24 == 0xFE:
                return resolve(raw[0], 'pom')
            return tuple(resolve(member, 'pom') for member in raw)

        for i in range(count):
            member = self.pointer(desc[7], i)
            if member == 0xFFFFFFFF:
                continue
            model = resolve(member, 'mdl')
            mesh = game.part(*model)
            accessory = not mesh.name.lower().startswith('model/player/')
            labels = FORM_LABELS.get(role, ())
            label = labels[i] if i < len(labels) else f'形态 {i}'
            if i and (accessory or role in (5, 36)):
                label = mesh.name.split('/')[-1].removesuffix('_mdl.ENC').replace('_MODELCONV', '')
            n = self.pointer(sizes[12], i)
            tracks = self.at(self.pointer(desc[19], i), f'<{3*n}h') if n else ()
            result[i] = Form(i, label, model, resolve(self.pointer(desc[15], i), 'bom'),
                             resolve(self.pointer(desc[14], i), 'vau'), textures(16, sizes[9]),
                             textures(17, sizes[10]), tuple(zip(tracks[1::3], tracks[2::3])), accessory)
        return result

    def banks(self, role):
        desc, sizes = self.descriptor(role)
        bank_ids = tuple(resource for index, count in zip(range(9, 13), sizes[5:9])
                         for resource in self.words(desc[index], count))
        starts = self.words(desc[13], len(bank_ids))
        return tuple(zip(bank_ids, starts))

    def command_ordinal(self, role):
        stub = self.pointer(0x3E4618, role)
        dispatcher, immediate = self.words(stub, 2)
        require(dispatcher == 0x0000004D and immediate >> 16 == 0x2400, '角色命令 ERX 导入桩无效。')
        return immediate & 0xFFFF

    def has_shoulder_assist(self, role):
        return self.pointer(AID_DISPATCH, role) != NO_AID

    def skill_rows(self, role, kind=1):
        self.descriptor(role)
        result = []
        data = tuple(self.at(address + 80 * role, '<20i') for address in (0x3C5030, 0x3C5C60, 0x3C6890))
        base, size = SKILL_TABLES[kind]
        table = self.pointer(base, role)
        names = (GUARD_NAMES, FINISHER_NAMES, SHOULDER_SKILLS)[kind].get(role, {})
        for slot, (mask, group, value) in enumerate(zip(*data)):
            if mask == -1:
                break
            if group != kind:
                continue
            va = table + size * value
            words = self.words(va, size // 4)
            label = names.get(slot)
            if kind == 2 and label:
                _, label = label
            label = label or f'{("屏障", "必杀技", "肩键技")[kind]} {value}'
            result.append(Skill(slot, mask, value, va, kind, words, label))
        return tuple(result)

    def default_finishers(self, role):
        # 0x272468 reads profiles and forms from the default save template.
        values = self.at(DEFAULT_FINISHERS + 48 * role, '<48b')
        buttons = defaultdict(set)
        masks = defaultdict(int)
        for index, value in enumerate(values):
            if value >= 0:
                masks[value] |= 1 << (index // 4 % 3)
                buttons[value].add(FINISHER_SLOTS[index % 4])
        return {value: (mask, tuple(button for button in FINISHER_SLOTS if button in buttons[value]))
                for value, mask in masks.items()}

    def battle_skill_rows(self, role, kind=1):
        menu = self.skill_rows(role, kind)
        if kind != 1:
            return menu
        defaults = self.default_finishers(role)
        missing = defaults.keys() - {skill.value for skill in menu}
        if not missing:
            return menu
        used = {skill.id for group in range(3) for skill in self.skill_rows(role, group)}
        table = self.pointer(FINISHER_TABLE, role)
        records = list(menu)
        for value in sorted(missing):
            require(table and value < SKILL_CANDIDATES, '原版固定必杀槽指向无效的战斗记录。')
            va = table + 20 * value
            words = self.words(va, 5)
            candidate = words[2]
            require(0 <= candidate < SKILL_CANDIDATES and candidate not in used,
                    '原版固定必杀的记录编号无效或重复。')
            used.add(candidate)
            label = FINISHER_NAMES.get(role, {}).get(candidate, f'固定必杀 {value}')
            records.append(Skill(candidate, defaults[value][0], value, va, kind, words, label, False))
        return tuple(records)

    def skill_menu_enabled(self, role):
        return bool(self.pointer(0x3C4F90, role))

    def finisher_rating(self, role, candidate):
        return self.at(rating_address(role, candidate), '<i')[0]

    def skill_name_source(self, role):
        self.descriptor(role)
        return SKILL_NAME_ATLASES, self.pointer(0x3C4BD0, role)

    def skill_loading_source(self, role):
        self.descriptor(role)
        return self.pointer(0x3D71D8, role), -1

    def skill_description_source(self, role, candidate):
        self.descriptor(role)
        require(type(candidate) is int and 0 <= candidate < 20, '必杀介绍候选号超出原生表。')
        page, strip = divmod(candidate, 4)
        return self.pointer(0x3C4C70, 5 * role + page), strip

def pad_text(mask):
    direction = input_arrows(''.join(label for bit,label in PAD_BITS[:4] if mask & bit))
    buttons = [label for bit,label in PAD_BITS[4:] if mask & bit]
    return ' + '.join(([direction] if direction else []) + buttons)


def input_rules(data):
    """Match the 0x2CFD20 directory order used by Command_Data."""
    version, count = struct.unpack_from('<2H', data, 0)
    require(version == 0 and 0 < count < 256, '原生输入目录无效。')
    labels, offset = {}, 4
    for index in range(count):
        length, = struct.unpack_from('<H', data, offset)
        start = offset + 2
        tag = data[start:start + length].decode('ascii')
        n, = struct.unpack_from('<H', data, start + length)
        offset = start + length + 2
        require(n > 0 and offset + 14 * n <= len(data), '原生输入步骤越界。')
        steps = [struct.unpack_from('<7H', data, offset + 14 * i) for i in range(n)]
        offset += 14 * n
        positive = [pad_text(a | b | c) for a,b,c,_,*rest in steps if a or b or c]
        if tag.startswith('DASH_'):
            label = '短冲 ' + SEQUENCE.join((positive[0], positive[0]))
        elif tag.startswith('RUN_'):
            label = '跑动 ' + SEQUENCE.join((positive[0], '按住' + positive[-1]))
        elif tag.startswith('ROLLING_'):
            label = ('→' if tag.endswith('_F') else '←') + '滚'
        elif tag == 'NEUTRAL_ALL':
            label = '回中'
        elif positive:
            label = SEQUENCE.join(dict.fromkeys(positive))
        else:
            mask = steps[0][3]
            label = '松开 ' + pad_text(mask) if mask else tag
        labels[index] = {'id':index, 'name':tag, 'label':label, 'steps':steps, 'offset':start - 2}
    require(offset <= len(data), '原生输入目录被截断。')
    return labels


def input_labels(data):
    return {index:row['label'] for index, row in input_rules(data).items()}


class CharacterProgram:
    """The ordinal 5 action array and the zero-terminated candidate lists."""

    def __init__(self, data, command_ordinal):
        require(data[:7] == b'\x7fELF\x01\x01\x01', '角色 ERX 文件头无效。')
        self.data = data
        shoff = word(data, 32)
        size, count, names = struct.unpack_from('<3H', data, 46)
        require(size == 40 and 0 < names < count and shoff + size * count <= len(data), 'ERX 节表无效。')
        sections = [struct.unpack_from('<10I', data, shoff + size * i) for i in range(count)]
        strings = sections[names]
        names_at = strings[4]
        self.sections = {data[names_at + row[0]:data.index(b'\0', names_at + row[0])]:row
                         for row in sections if row[0]}
        self.source = self.sections[b'.data']
        lib = self.sections[b'.erx.lib']
        require(data[lib[4]:lib[4] + 7] == b'eRxLiB\0', '角色 ERX 导出表无效。')
        self.export = struct.unpack_from('<10I', data, lib[4] + 32)
        start, length = self.sections[b'.rel.data'][4:6]
        self.relocations = {at for at, kind in struct.iter_unpack('<II', data[start:start + length]) if kind == 2}
        if b'.rel.moves' in self.sections:
            start, length = self.sections[b'.rel.moves'][4:6]
            self.relocations.update(at for at, kind in struct.iter_unpack('<II', data[start:start + length])
                                    if kind == 2)
        f = self.at(self.export[5], '<7I', b'.text')
        require((f[0], f[2], f[4:7]) == (0x240E0018, 0x008E2018, (0x008F2021, 0x03E00008, 0x0080102D)),
                '角色动作查询代码的布局尚未识别。')
        self.action_table = self.address(f[1], f[3])
        f = self.at(self.export[command_ordinal], '<6I', b'.text')
        require((f[1], f[3:]) == (0x00042080, (0x008F2021, 0x03E00008, 0x0080102D)),
                '角色动作候选查询代码的布局尚未识别。')
        self.command_table = self.address(f[0], f[2])
        self.command_ordinal = command_ordinal

    @staticmethod
    def address(hi, lo):
        require(hi >> 16 == 0x3C0F and lo >> 16 == 0x25EF, 'ERX 表地址指令无效。')
        return (hi & 65535) * 65536 + (lo & 32767) - (lo & 32768)

    def region(self, address, size, section=b'.data'):
        regions = [self.sections[section]]
        if section in (b'.data',b'.text') and b'.moves' in self.sections:
            regions.append(self.sections[b'.moves'])
        for row in regions:
            start, offset, extent = row[3:6]
            if start <= address and address + size <= start + extent:
                return start, offset, extent
        raise ValueError('角色 ERX 记录位置越界。')

    def file_offset(self, address, size, section=b'.data'):
        start, offset, _ = self.region(address,size,section)
        return offset + address - start

    def at(self, address, fmt, section=b'.data'):
        return struct.unpack_from(fmt, self.data, self.file_offset(address, struct.calcsize(fmt), section))

    @lru_cache(maxsize=10)
    def actions(self):
        result = []
        offset = self.action_table
        while all(offset + step in self.relocations for step in (0, 4, 8)):
            result.append(self.at(offset + 12, '<3i'))
            offset += 24
        return tuple(result)

    def commands(self):
        result = defaultdict(list)
        for action in range(len(self.actions())):
            at = self.command_table + 4 * action
            if at not in self.relocations:
                continue
            pointer = self.at(at, '<I')[0]
            while pointer in self.relocations:
                entry = self.at(pointer, '<I')[0]
                target, command, condition, priority = self.at(entry, '<4i')
                if 0 <= target < len(self.actions()):
                    result[action].append((target, command, condition, priority, entry))
                pointer += 4
        return dict(result)


CHARGE_RATES = ((0x4BA60C, 1.5), (0x4BA608, 3.0))
FULL = 256.0


def f32(value):
    return struct.unpack('<f', struct.pack('<f', value))[0]


def fill_frames(rate, threshold=FULL):
    if not math.isfinite(rate) or rate <= 0:
        raise ValueError('ELF 蓄力增量必须为有限的正数。')
    if not 0 <= threshold <= FULL:
        return None
    position = 0.0
    frames = 0
    while position < threshold:
        after = f32(position + rate)
        if after == position:
            raise ValueError('此浮点增量无法使原生蓄力槽达到上限。')
        frames += 1
        position = after
        if position >= threshold:
            break
        # Float spacing is constant inside this exponent interval. Leave two
        # real additions at the boundary, including round-to-even ties.
        edge = min(threshold, math.ldexp(1., math.frexp(position)[1]))
        following = f32(position + rate)
        step = following - position
        if step > 0 and f32(following + rate) - following == step:
            jumps = max(0, math.floor((edge - position) / step) - 2)
            position += jumps * step
            frames += jumps
    return frames


def rate_for_frames(frames):
    if type(frames) is not int or not 1 <= frames <= 2**25:
        raise ValueError('请输入可由原生单精度蓄力槽表示的正整数帧数。')
    rate = f32(FULL / frames)
    try:
        if fill_frames(rate) == frames:
            return rate
    except ValueError:
        pass
    low, high = 0, 0x43800000  # positive float bits through 256.0
    while low + 1 < high:
        middle = (low + high) // 2
        candidate = struct.unpack('<f', struct.pack('<I', middle))[0]
        try:
            enough = fill_frames(candidate) <= frames
        except ValueError:
            enough = False
        if enough:
            high = middle
        else:
            low = middle
    rate = struct.unpack('<f', struct.pack('<I', high))[0]
    if fill_frames(rate) != frames:
        raise ValueError('原生浮点累加无法精确表示这个帧数。')
    return rate
