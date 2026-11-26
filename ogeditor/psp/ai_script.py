"""Decode the fighter AI programs in the v7 executable."""

from dataclasses import dataclass
import struct

from formats import u32
from move_eboot import BASE, signed16


GETTERS = 0x089D3F08
FIGHTERS = 17
LEVELS = 4
NEAR_DISTANCE = 0
GROUPS = ('初始化', '近距', '远距', '对手攻击', '对手防御', '对手投技等', '对手受创等')
RELATIONS = ('恒不成立', '>', '<', '≠', '=', '≥', '≤', '恒成立')
SITUATION = (
    '战斗槽位', '角色编号', '角色配置', '动作状态号', 'AI 动作类别',
    '受创等标志', '攻击标志', '防御标志', '对侧模型状态为 1', '对侧模型状态参数',
    '投技等标志', '气槽已达首档', '气槽状态', '艾雷王电量',
    '当前动作帧', '动作总帧', '生命值', '快照保留值',
    '气槽档位', '气槽原值', '水平距离变化', '水平距离',
    '高度变化', '离地高度', '必杀 163 对抗条件', '空间距离', '双方生命差',
)
BATTLE = ('游戏模式', '剧情参数 1', '剧情参数 2', '战斗实际难度', '回合计时',
          'AI 上下文参数', '上下文保留值')
OP_NAMES = (
    '跳过标记值', '等待', '清／设本步输出辅助值', '设置脚本切换标志',
    '设计数器', '设脚本变量', '增加脚本变量', '登记子程序', '登记原生回调',
    '设置解释器间隔', '设置近距分界', '抽取随机结果', '设置独立计时器',
    '进入子程序', '按槽进入子程序', '返回子程序调用点', '调用原生函数', '按槽调用原生函数',
    '回到当前脚本入口', '停在当前指令',
    '比较计数器', '随机比较', '随机与脚本变量比较', '比较角色数据',
    '比较双方生命差', '比较战斗上下文', '比较动作剩余帧',
    '比较希波利特陷阱计数', '比较希波利特陷阱距离', '比较希波利特陷阱朝向',
    '比较陷阱与角色距离差', '比较持续接近／远离计数', '检查精神状态',
    '比较精神类别', '比较精神编号', '比较独立计时器', '比较脚本变量',
    '同组后续条件', '结束条件组', '计数器循环条件', '随机循环条件', '角色数据循环条件',
    '战斗上下文循环条件', '剩余帧循环条件', '变量循环条件', '跳出循环', '循环末尾',
    '释放方向与战斗键', '地面方向', '跳跃／跳跃攻击', '下／后下方向', '跑动／跑动攻击',
    '前／后冲刺', '向上／向下连按', '滚动／方向准备', '单键攻击／蓄力', '△招式',
    '□招式', '×招式／抓取', '站防／蹲防／反击', '必杀输入',
)
WORDS = (
    1, 1, 1, 1, 2, 2, 2, 2, 2, 1, 1, 1, 2, 1, 1, 0, 1, 1, 0, 0,
    2, 2, 2, 3, 3, 2, 2, 2, 3, 2, 3, 2, 3, 2, 2, 2, 2,
    2, 0, 2, 2, 3, 2, 2, 2, 0, 0, 0, 1, 1, 1, 1, 1,
    1, 1, 1, 1, 1, 1, 1, 1,
)
LOOP_CONDITION = {0x27:0x14, 0x28:0x15, 0x29:0x17, 0x2A:0x19, 0x2B:0x1A, 0x2C:0x24}
DECISIONS = {
    0x2F: {0:'松开方向和战斗键'},
    0x30: {-1:'保持后（后退或站防）', 0:'地面方向 0', 1:'保持前', 20:'背向陷阱移动', 21:'朝向陷阱移动'},
    0x34: {-1:'后冲刺', 1:'前冲刺'},
    0x37: {0:'○', 1:'△', 2:'□', 3:'×', 4:'R'},
    0x38: {0:'△', 1:'前＋△', 2:'前下＋△', 3:'下＋△', 4:'后下＋△', 5:'后＋△'},
    0x39: {0:'□', 1:'前＋□', 2:'前下＋□', 3:'下＋□', 4:'后下＋□', 5:'后＋□'},
    0x3A: {0:'×', 1:'前＋×', 2:'下＋×', 3:'后＋×'},
    0x3C: {1:'前＋○', 2:'下＋○', 3:'后＋○'},
}
MIN_WORD, MAX_WORD = -(1 << 31), (1 << 31) - 1


def signed(word):
    return word if word < (1 << 31) else word - (1 << 32)


def getter_tables(image, address):
    registers = {}
    tables = {}
    for site in range(address, address + 112, 4):
        word = u32(image, site - BASE)
        op, source, dest = word >> 26, (word >> 21) & 31, (word >> 16) & 31
        if op == 15:
            registers[dest] = (word & 0xFFFF) << 16
        elif op == 9 and source in registers:
            registers[dest] = registers[source] + signed16(word)
        elif op == 43 and source == 4 and dest in registers:
            tables[word & 0xFFFF] = registers[dest]
        if len(tables) == 7:
            break
    if set(tables) != set(range(0, 28, 4)):
        raise ValueError('本镜像的 AI 脚本入口不符合奥0布局。')
    return tuple(tables[slot] for slot in range(0, 28, 4))


@dataclass(frozen=True)
class Instruction:
    address: int
    word: int
    args: tuple
    family: int

    @property
    def op(self):
        return self.word & 255

    @property
    def relation(self):
        return (self.word >> 9) & 7


def program(image, start, end):
    instructions, stack = [], []
    address = start
    while address < end:
        word = u32(image, address - BASE)
        if word == 0:
            if any(image[address - BASE:end - BASE]):
                raise ValueError(f'AI 脚本 0x{start:08X} 的尾部布局异常。')
            break
        op = word & 255
        if word >> 16 != 0x55AA or op >= len(WORDS) or address + 4 * (1 + WORDS[op]) > end:
            raise ValueError(f'无法解码 AI 指令 0x{address:08X}。')
        args = struct.unpack_from(f'<{WORDS[op]}i', image, address - BASE + 4)
        family = LOOP_CONDITION.get(op, op)
        if 0x14 <= op <= 0x24:
            stack.append(op)
        elif op == 0x25:
            if not stack:
                raise ValueError(f'无对应条件组的 AI 分支 0x{address:08X}。')
            family = stack[-1]
        elif op == 0x26:
            # The native handler also accepts a standalone reset of the comparison register.
            if stack:
                stack.pop()
        instructions.append(Instruction(address, word, args, family))
        address += 4 * (len(args) + 1)
    if stack:
        raise ValueError(f'AI 脚本 0x{start:08X} 的条件组未闭合。')
    return instructions


def action(instruction):
    if instruction is None or instruction.op < 0x2F:
        return ''
    name = OP_NAMES[instruction.op]
    value = instruction.args[0] if instruction.args else 0
    return DECISIONS.get(instruction.op, {}).get(value, f'{name} {value}')


def variable_name(index, initial=False):
    if index in (20, 21) and initial:
        return {20:'近距等待随机阈值 · 变量 20', 21:'对手攻击时等待随机阈值 · 变量 21'}[index]
    return f'脚本变量 {index}'


def condition(instruction):
    """Return the displayed operand, its index and its edit category."""
    family, args = instruction.family, instruction.args
    if instruction.op == 0x25 and instruction.relation == 7:
        return '否则', None, 'condition'
    if family == 0x14:
        return f'计数器 {args[0]}', 1, 'condition'
    if family in (0x15, 0x16):
        name = f'随机整数 0–{args[0]}' if args[0] >= 0 else '固定值 −1'
        if family == 0x16:
            return name + f' 与变量 {args[1]} 比较', None, 'random'
        return name, 1, 'random'
    if family == 0x17:
        field = args[0]
        side = ('自身' if args[2] == 0 else '对手') if len(args) > 2 else '沿用上个分支的角色'
        return f'{side} · {SITUATION[field]}', 1, 'distance' if field in (20, 21, 22, 23, 25) else 'condition'
    if family == 0x19:
        return BATTLE[args[0]], 1, 'condition'
    if family in (0x18, 0x20):
        return OP_NAMES[family], 2 if family == 0x18 else 1, 'condition'
    if family in (0x1A, 0x1B, 0x1C, 0x1D, 0x1E, 0x1F, 0x21, 0x22):
        kind = 'time' if family == 0x1A else 'distance' if family in (0x1C, 0x1E) else 'condition'
        return OP_NAMES[family], 0, kind
    if family == 0x23:
        return f'独立计时器 {args[0]}', 1, 'time'
    if family == 0x24:
        return variable_name(args[0]), 1, 'condition'
    return OP_NAMES[instruction.op], None, 'script'


@dataclass(frozen=True)
class Field:
    label: str
    value: int
    minimum: int = MIN_WORD
    maximum: int = MAX_WORD
    relation: bool = False

    def encode(self, value):
        if type(value) is not int or not self.minimum <= value <= self.maximum:
            raise ValueError(f'{self.label}须为 {self.minimum} 到 {self.maximum} 的整数。')
        if self.relation:
            return (self.value & ~0xE00) | (value << 9)
        return value & 0xFFFFFFFF

    def decode(self, word):
        return (word >> 9 & 7) if self.relation else signed(word)


def odds(relation, bound, threshold):
    if bound < 0:
        return None
    count = bound + 1
    lower = max(0, min(count, threshold))
    through = max(0, min(count, threshold + 1))
    equal = int(0 <= threshold <= bound)
    return (0, count-through, lower, count-equal, equal, count-lower, through, count)[relation] * 100 / count


class FighterAI:
    def __init__(self, image, address):
        self.start, *self.tables = getter_tables(image, address)
        self.end = self.tables[-1] + 36
        starts = []
        for table in self.tables:
            repeat = struct.unpack_from('<9I', image, table - BASE)
            if repeat != (repeat[0],) * 8 + (0,) or not self.start < repeat[0] < table:
                raise ValueError('奥0 AI 策略表的共享指针布局已变化。')
            starts.append(repeat[0])
        self.programs = [program(image, start, end) for start, end in
                         zip([self.start, *starts], [starts[0], *self.tables])]
        self.fields = {NEAR_DISTANCE:Field('近距策略分界', 250, 0, 100000)}
        self.near_setters = []
        self.rows = []
        for group, script in enumerate(self.programs):
            for index, instruction in enumerate(script):
                following = script[index + 1] if index + 1 < len(script) else None
                self.add_row(group, instruction, following)

    def add_row(self, group, ins, following):
        label, operand, kind = condition(ins)
        after = action(following)
        slots = {}
        if 0x14 <= ins.op <= 0x25 or ins.op in LOOP_CONDITION:
            if label != '否则':
                slots[ins.address] = Field('比较方式', ins.word, 0, 7, relation=True)
                if operand is not None:
                    slots[ins.address + 4 * (operand + 1)] = Field('比较值', ins.args[operand])
        if ins.op in (0x01, 0x05, 0x06, 0x09, 0x0C):
            index = len(ins.args) - 1
            if ins.op in (0x01, 0x09, 0x0C):
                kind = 'time'
                label = '等待 AI 步数' if ins.op == 0x01 else label
                value = Field('步数', ins.args[index], 0, 65535)
            else:
                kind = 'condition'
                name = variable_name(ins.args[0], group == 0)
                label = ('设置 ' if ins.op == 0x05 else '增加 ') + name
                value = Field('变量值', ins.args[index])
            slots[ins.address + 4 * (index + 1)] = value
        if ins.op == 0x0A:
            label, kind = '近距策略分界', 'distance'
            self.fields[NEAR_DISTANCE] = Field(label, ins.args[0], 0, 100000)
            self.near_setters.append(ins.address + 4)
        probability = None
        if ins.family == 0x15 and label != '否则' and ins.args[0] >= 0:
            probability = (ins.address, ins.address + 8, ins.args[0])
            if following and following.op == 0x3C:
                kind = 'skill'
            if following and (following.op == 0x3B or following.op == 0x30 and following.args[0] == -1):
                kind = 'guard'
        if ins.op >= 0x2F:
            label = action(ins)
        self.fields.update(slots)
        self.rows.append((group, ins, label, kind, tuple(slots), after, probability))

    def field(self, site):
        if type(site) is not int or site not in self.fields:
            raise ValueError('所选 AI 脚本参数不可编辑。')
        return self.fields[site]

    def view(self, values, group):
        if type(group) is not int or not -1 <= group < len(GROUPS):
            raise ValueError('请选择 AI 脚本。')
        def serialize(site):
            definition = self.fields[site]
            original = definition.decode(definition.value)
            current = values.get(site, original)
            return {'site':site, 'label':definition.label, 'value':current, 'original':original,
                    'min':definition.minimum, 'max':definition.maximum, 'relation':definition.relation}

        rows = []
        for index, ins, label, kind, slots, after, probability in self.rows:
            if group not in (-1, index):
                continue
            fields = [serialize(site) for site in slots]
            chance = None
            if probability:
                op_site, value_site, bound = probability
                chance = {'relation':op_site, 'threshold':value_site, 'bound':bound,
                          'percent':odds(values.get(op_site, ins.relation), bound,
                                         values.get(value_site, ins.args[1]))}
            values_in_record = list(ins.args)
            for field in fields:
                if not field['relation']:
                    values_in_record[(field['site'] - ins.address) // 4 - 1] = field['value']
            if ins.op == 0x0A:
                values_in_record[0] = values.get(NEAR_DISTANCE, ins.args[0])
            rows.append({'address':ins.address, 'group':index, 'op':ins.op, 'raw':f'{ins.word:08X}',
                         'args':values_in_record, 'label':label, 'kind':kind, 'fields':fields,
                         'next':after, 'chance':chance,
                         'edited':any(site in values for site in slots) or
                                  (ins.op == 0x0A and NEAR_DISTANCE in values)})
        return {'groups':GROUPS, 'relations':RELATIONS, 'counts':[len(code) for code in self.programs],
                'near':serialize(NEAR_DISTANCE), 'rows':rows}


def fighter_scripts(image):
    return [FighterAI(image, u32(image, GETTERS - BASE + 4 * role)) for role in range(FIGHTERS)]


def load_ai_edits(scripts, rows):
    if not isinstance(rows, list):
        raise ValueError('AI 工程记录无效。')
    result = {}
    seen = set()
    for row in rows:
        if not isinstance(row, list) or len(row) != 4 or any(type(x) is not int for x in row):
            raise ValueError('AI 工程字段无效。')
        role, difficulty, site, value = row
        key = role, difficulty, site
        if not 0 <= role < FIGHTERS or not 0 <= difficulty < LEVELS or key in seen:
            raise ValueError('AI 工程的角色、难度或参数重复无效。')
        seen.add(key)
        definition = scripts[role].field(site)
        if definition.encode(value) != definition.value & 0xFFFFFFFF:
            result[key] = value
    return result
