"""按事件类型读写触发帧、伤害、音效和手型，并检查可填写的数值范围。"""

from dataclasses import dataclass
import struct

from formats import ATTACK_FIELDS, SHAPE_TEMPLATE_OFFSETS, s16, u32


@dataclass(frozen=True)
class Word:
    offset: int
    label: str
    minimum: int = -32768
    maximum: int = 32767
    help: str = ''

    def describe(self, raw):
        return {'offset': self.offset, 'label': self.label, 'value': s16(raw, self.offset),
                'min': self.minimum, 'max': self.maximum, 'help': self.help}


@dataclass(frozen=True)
class Patch:
    offset: int
    original: bytes
    edited: bytes


def event_words(raw, overlap, last, node_count, template_count, sound_count):
    opcode = raw[0]
    frame = Word(2, '触发帧', 0, min(last, 32767))
    if overlap:
        return []
    if opcode in SHAPE_TEMPLATE_OFFSETS:
        template_at = SHAPE_TEMPLATE_OFFSETS[opcode]
        fields = [frame, Word(6, '持续帧', 0),
                  Word(4, 'MDL 节点（-1 为角色基准）', -1, node_count - 1),
                  Word(template_at, '判定模板编号', 0, template_count - 1)]
        if opcode in (0, 5):
            fields.append(Word(8, '基础伤害', 0))
            fields.extend(Word(offset, label, minimum=0 if offset == 0x10 else -32768, help=help_text)
                          for offset, (label, help_text) in ATTACK_FIELDS.items())
            fields.extend(Word(offset, f'参数 +0x{offset:02X}') for offset in range(0x16, template_at, 2))
        return fields if len(raw) >= template_at + 2 else []
    if opcode == 3 and len(raw) >= 10:
        return [frame, Word(6, '窗口长度', 0), Word(4, '命令编号'), Word(8, '目标 ANM')]
    if opcode in (6, 7) and len(raw) >= 18:
        fields = [frame, Word(4, '基础伤害', 0), Word(12, '防御字段', 0)]
        fields.extend(Word(offset, f'参数 +0x{offset:02X}') for offset in (6, 8, 10, 14, 16))
        return fields
    if opcode in (4, 11) and len(raw) >= 6:
        return [frame, Word(4, '原生值', 0, 1 if opcode == 11 else 32767)]
    if opcode in (14, 15) and len(raw) >= 8:
        fields = [frame, Word(4, '音效编号', 0, sound_count - 1)]
        if opcode == 15:
            fields.append(Word(6, 'MDL 节点条件', 0, node_count - 1))
        return fields
    if opcode == 16 and len(raw) >= 6:
        return [frame, Word(4, '手型编号', 0, 11)]
    return []


def change_words(raw, fields, updates, templates):
    if not isinstance(updates, dict):
        raise ValueError('事件参数格式不正确。')
    by_offset = {str(field.offset): field for field in fields}
    pending = bytearray(raw)
    for offset, value in updates.items():
        field = by_offset.get(offset)
        if field is None or type(value) is not int:
            raise ValueError('请选择可编辑字段并输入整数。')
        if value == s16(raw, field.offset):
            continue
        if not field.minimum <= value <= field.maximum:
            raise ValueError(f'{field.label}请输入 {field.minimum} 至 {field.maximum}。')
        if field.offset == SHAPE_TEMPLATE_OFFSETS.get(raw[0]):
            template = templates[value]
            if len(template) < 4 or not 1 <= u32(template, 0) <= 4:
                raise ValueError('请选择有效的判定模板。')
        struct.pack_into('<h', pending, field.offset, value)
    return bytes(pending)
