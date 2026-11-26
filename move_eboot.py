"""Native v7 code and relocated data emitted when a move set is saved."""

from collections import defaultdict
from copy import deepcopy
import json
import struct

from formats import u32


BASE = 0x08804000
PSP_REL = 0x700000A0
PTR, JUMP, HI, LO = 2, 4, 5, 6
SLOT_BASE = 0x6000
EFFECT_BASE = 0x1000
MARKER = b'KGFMOVES\x01'
ANM_JUMP = 0x088D0240
ANM_RAGE = 0x08A1B6A0
FX_CALLER = 0x08809800
FX_DISPATCH = 0x08885888
GROUP_SLOT = 0x0888BADC

Z, V0, V1, A0, A1, A2, A3, T4, T5, T6, T7, SP, RA = 0, 2, 3, 4, 5, 6, 7, 12, 13, 14, 15, 29, 31


def aligned(value, alignment=16):
    return (value + alignment - 1) & -alignment


def signed16(value):
    return (value & 0xFFFF) - 0x10000 if value & 0x8000 else value & 0xFFFF


def iword(op, source, dest, operand):
    return op << 26 | source << 21 | dest << 16 | (operand & 0xFFFF)


def rword(left, right, dest, function, shift=0):
    return left << 21 | right << 16 | dest << 11 | shift << 6 | function


def addiu(dest, source, value):
    return iword(9, source, dest, value)


def addu(dest, left, right):
    return rword(left, right, dest, 0x21)


def sltiu(dest, source, value):
    return iword(11, source, dest, value)


def lw(dest, offset, source):
    return iword(35, source, dest, offset)


def sw(dest, offset, source):
    return iword(43, source, dest, offset)


def sll(dest, source, bits):
    return rword(Z, source, dest, 0, bits)


def jr(source=RA):
    return rword(source, Z, Z, 8)


def branch(op, left, right, distance):
    # Distance is measured in instructions from the branch's delay slot.
    return iword(op, left, right, distance)


def payload_limit(raw, layout):
    end = layout['segments'][-1]['offset'] + layout['segments'][-1]['filesz']
    return min([u32(raw, 32), len(raw), *(
        s['offset'] for s in layout['sections'] if s['size'] and s['type'] != 8 and s['offset'] >= end
    )])


class Eboot:
    def __init__(self, raw, layout, limit=None):
        self.raw = raw
        self.segments = layout['segments']
        self.sections = layout['sections']
        self.section_offset = u32(raw, 32)
        self.last = self.segments[-1]
        self.end = self.last['offset'] + self.last['filesz']
        self.payload_va = BASE + self.last['vaddr'] + self.last['memsz']
        self.limit = payload_limit(raw, layout) if limit is None else limit
        self.rel_section = next(index for index, section in enumerate(self.sections)
                                if section['name'] == '.rel.fini')
        self.out = bytearray(raw)
        self.payload = bytearray()
        self.new_relocs = []
        self.changed = {}
        self.relocs = defaultdict(list)
        for section in self.sections:
            if section['type'] != PSP_REL:
                continue
            start = section['offset']
            for pos in range(start, start + section['size'], 8):
                offset, info = struct.unpack_from('<II', raw, pos)
                if info & 15:
                    site = (info >> 8) & 255
                    va = BASE + self.segments[site]['vaddr'] + offset
                    self.relocs[va].append((pos, info))

    def locate(self, va):
        for index, info in enumerate(self.segments):
            relative = va - BASE - info['vaddr']
            if 0 <= relative < info['filesz'] or (
                info is self.last and 0 <= relative - info['memsz'] < len(self.payload)
            ):
                return index, relative, info['offset'] + relative
        raise ValueError(f'EBOOT 地址 0x{va:08X} 超出奥0载入段。')

    def write_raw(self, offset, data):
        current = self.out[offset:offset + len(data)]
        if current != data:
            self.changed[offset] = len(data)
            self.out[offset:offset + len(data)] = data

    def write(self, va, data):
        _, _, offset = self.locate(va)
        if offset >= self.end:
            relative = offset - self.end
            self.payload[relative:relative + len(data)] = data
        else:
            self.write_raw(offset, data)

    def half(self, va, value):
        self.write(va, struct.pack('<H', value))

    def word(self, va, value):
        self.write(va, struct.pack('<I', value))

    def allocate(self, data=b'', alignment=4):
        start = aligned(len(self.payload), alignment)
        self.payload.extend(bytes(start - len(self.payload)))
        self.payload.extend(data)
        return self.payload_va + start

    def code(self, words):
        return self.allocate(struct.pack(f'<{len(words)}I', *words))

    def relocation(self, va, info):
        site, rel, _ = self.locate(va)
        self.new_relocs.append((rel, (info & 0xFFFF00FF) | site << 8))

    def linked(self, va, target, kind, op=0):
        dst, target_rva, _ = self.locate(target)
        existing = self.relocs.get(va, [])
        if existing:
            if len(existing) != 1 or existing[0][1] & 15 != kind:
                raise ValueError(f'EBOOT 0x{va:08X} 的重定位类型不匹配。')
            pos, info = existing[0]
            self.write_raw(pos + 4, struct.pack('<I', (info & 0xFF00FFFF) | dst << 16))
        else:
            self.relocation(va, kind | dst << 16)
        value = target_rva if kind == PTR else op << 26 | target_rva >> 2
        self.word(va, value)

    def absolute(self, va, register, target):
        dst, rel, _ = self.locate(target)
        self.word(va, iword(15, Z, register, (rel + 0x8000) >> 16))
        self.word(va + 4, addiu(register, register, rel))
        self.relocation(va, dst << 16 | HI)
        self.relocation(va + 4, dst << 16 | LO)

    def redirect(self, va, target, call=False):
        self.linked(va, target, JUMP, 3 if call else 2)

    def replace_address_load(self, va, target):
        for address, kind in ((va, HI), (va + 4, LO)):
            records = self.relocs.get(address, [])
            if len(records) != 1 or records[0][1] & 15 != kind:
                raise ValueError('EBOOT 贴附点地址的重定位与支持的奥0布局不匹配。')
            position, info = self.relocs.pop(address)[0]
            self.write_raw(position + 4, struct.pack('<I', info & ~15))
        self.redirect(va, target)
        self.word(va + 4, 0)

    def clone(self, start, end, node_words=(), texture_words=(), slot=0, *,
              immediates=(), node_loads=(), calls=None, pointers=()):
        _, _, offset = self.locate(start)
        size = end - start
        clone = self.allocate(self.raw[offset:offset + size])
        # PSP HI/LO pairing uses relocation table order. Keep the original order within the clone.
        copied = [(pos, info, va) for va in range(start, end, 4)
                  for pos, info in self.relocs.get(va, [])]
        moved = {}
        relocated_pointers = {}
        for hi, lo, address in pointers:
            for site, kind in ((hi, HI), (lo, LO)):
                records = self.relocs.get(site, ())
                if not start <= site < end or len(records) != 1 or records[0][1] & 15 != kind:
                    raise ValueError(f'EBOOT 地址 0x{site:08X} 与重定位记录不匹配。')
            dest, relative, _ = self.locate(address)
            relocated_pointers[hi] = dest, (relative + 0x8000) >> 16
            relocated_pointers[lo] = dest, relative
        narrow, expanded = {}, {}
        control = (1, 2, 3, 4, 5, 6, 7, 20, 21, 22, 23)
        for site, node in node_loads:
            original = u32(self.raw, offset + site - start)
            if original >> 26 != 35 or original & 0xFFFF or self.relocs.get(site):
                raise ValueError('技能根节点的读取指令与支持的奥0布局不一致。')
            narrow[site] = addiu(original >> 16 & 31, Z, node)
        for site, mapped_node in node_words:
            original = u32(self.raw, offset + site - start)
            src, reg = original >> 21 & 31, original >> 16 & 31
            forward_offset = original >> 26 == 13 and src == Z
            field = (original & 0xFFFF) % 544
            value = mapped_node * 544 + field
            if (original >> 26 != 9 and not forward_offset) or value < 0 or self.relocs.get(site):
                raise ValueError('技能的发射骨骼与奥0寄存器布局不匹配。')
            if value <= (0xFFFF if forward_offset else 0x7FFF):
                narrow[site] = original & 0xFFFF0000 | value
                continue
            before = u32(self.raw, offset + site - start - 4)
            if before >> 26 in control and (before >> 26 not in (2, 3) or RA in (src, reg)):
                raise ValueError(f'挂点 0x{site:08X} 超出原分支延迟槽的立即数范围。')
            if forward_offset:
                expanded[site] = [iword(15, Z, reg, value >> 16), iword(13, reg, reg, value)]
            else:
                blocks, rest = divmod(value, 0x7FFF)
                expanded[site] = [addiu(reg, src, 0x7FFF), *([addiu(reg, reg, 0x7FFF)] * (blocks - 1))]
                if rest:
                    expanded[site].append(addiu(reg, reg, rest))
        if slot:
            for site in texture_words:
                original = u32(self.raw, offset + site - start)
                before = u32(self.raw, offset + site - start - 4)
                if original >> 26 != 35 or before >> 26 in control or self.relocs.get(site):
                    raise ValueError('技能贴图读取点与已分析指令不一致。')
                reg = original >> 16 & 31
                distance = slot * 112
                blocks, remaining = divmod(distance, 0x7FFF)
                expanded[site] = [original, *([addiu(reg, reg, 0x7FFF)] * blocks)]
                if remaining:
                    expanded[site].append(addiu(reg, reg, remaining))
        for site, instruction in narrow.items():
            self.word(clone + site - start, instruction)
        handled = set()
        for site in sorted(expanded):
            if site in handled:
                continue
            before = u32(self.raw, offset + site - start - 4)
            first = site - 4 if before >> 26 in (2, 3) else site
            patch = clone + first - start
            after = first
            prefix = []
            while after in expanded:
                prefix.extend(expanded[after])
                handled.add(after)
                after += 4
            body_size = 4 * len(prefix)
            following, delay = struct.unpack_from('<II', self.raw, offset + after - start)
            if following >> 26 in (2, 3):
                # A delay calculation that does not use ra can execute just
                # before the relocated jump or call.
                linked = following >> 26 == 3
                if after + 4 in expanded:
                    prefix.extend(expanded[after + 4])
                    handled.add(after + 4)
                    delay = 0
                else:
                    delay = narrow.get(after + 4, delay)
                body_size = 4 * len(prefix)
                code = [*prefix, following, delay]
                if linked:
                    code.extend((0, 0))
                stub = self.code(code)
                moved[after] = stub + body_size
                moved[after + 4] = stub + body_size + 4
                if linked:
                    self.redirect(stub + body_size + 8, clone + after + 8 - start)
                self.word(clone + after + 4 - start, 0)
            elif after > site + 4:
                # Adjacent address calculations share a jump and its delay slot.
                stub = self.code([*prefix, 0, 0])
                self.redirect(stub + body_size, clone + after - start)
            else:
                if following >> 26 in control or (following >> 26 == 0 and following & 63 in (8, 9)):
                    raise ValueError(f'挂点 0x{site:08X} 超出原分支的立即数范围。')
                stub = self.code([*prefix, 0, narrow.get(after, following)])
                moved[after] = stub + body_size + 4
                self.redirect(stub + body_size, patch + 8)
            self.redirect(patch, stub)
            self.word(patch + 4, 0)
        for site, immediate in immediates:
            original = u32(self.raw, offset + site - start)
            if original >> 26 != 9 or self.relocs.get(site) or not -0x8000 <= immediate <= 0x7FFF:
                raise ValueError('技能的原生立即数不能改写。')
            self.word(moved.get(site, clone + site - start), original & 0xFFFF0000 | (immediate & 0xFFFF))
        for _, info, va in sorted(copied):
            kind = info & 15
            target = moved.get(va, clone + va - start)
            original = u32(self.raw, offset + va - start)
            if va in relocated_pointers:
                dest, immediate = relocated_pointers[va]
                self.word(target, original & 0xFFFF0000 | (immediate & 0xFFFF))
                info = (info & 0xFF00FFFF) | dest << 16
            if kind == JUMP:
                segment = self.segments[(info >> 16) & 255]
                dest = BASE + segment['vaddr'] + ((original & 0x3FFFFFF) << 2)
                if start <= dest < end:
                    self.redirect(target, clone + dest - start, call=original >> 26 == 3)
                    continue
                if calls and dest in calls:
                    self.redirect(target, calls[dest], call=original >> 26 == 3)
                    continue
            self.relocation(target, info)
        return clone

    def install_anm(self):
        # Real entries continue to the underlying ISO's resolver, including
        # SELECT's per-seat variant on the paginated builds.
        _,_,offset = self.locate(ANM_JUMP)
        instruction = u32(self.raw,offset)
        (_,info), = self.relocs[ANM_JUMP]
        if instruction>>26 != 2 or info&15 != JUMP:
            raise ValueError('原生动画解析入口不能识别。')
        resolver = BASE+self.segments[(info>>16)&255]['vaddr']+4*(instruction&0x3ffffff)
        helper = self.code([branch(1, A1, 1, 3), 0, jr(), addu(V0, Z, Z), 0, 0])
        self.redirect(helper + 16, resolver)
        self.redirect(ANM_JUMP, helper)

    def install_effects(self, functions):
        if not functions:
            return
        table = self.allocate(bytes(4 * len(functions)))
        for index, fn in enumerate(functions):
            self.linked(table + 4 * index, fn, PTR)
        helper = self.code([
            addiu(T4, A3, -EFFECT_BASE), sltiu(T5, T4, len(functions)),
            branch(4, T5, Z, 8), 0, 0, 0, sll(T4, T4, 2),
            addu(T6, T6, T4), lw(T7, 0, T6), jr(T7), 0,
            0, 0,
        ])
        self.absolute(helper + 16, T6, table)
        self.redirect(helper + 44, FX_DISPATCH)
        self.redirect(FX_CALLER, helper, call=True)

    def install_slots(self, slots):
        if not slots:
            return
        table = self.allocate(b''.join(struct.pack('<II', anchor, offset) for anchor, offset in slots))
        _, _, entry = self.locate(GROUP_SLOT)
        start = self.code([*struct.unpack_from('<II', self.raw, entry), 0, 0])
        self.redirect(start + 8, GROUP_SLOT + 8)
        helper = self.code([
            addiu(T4, A2, -SLOT_BASE), sltiu(T5, T4, len(slots)),
            branch(4, T5, Z, 22), 0, 0, 0,
            sll(T4, T4, 3), addu(T6, T6, T4), lw(A2, 0, T6),
            lw(T4, 4, T6), addiu(SP, SP, -16), sw(T4, 0, SP), sw(A1, 4, SP),
            sw(RA, 8, SP), 0, 0,
            lw(RA, 8, SP), lw(A1, 4, SP), lw(T4, 0, SP), lw(V1, 0, A1),
            addu(V1, V1, T4), sw(V1, 0, A1), addiu(SP, SP, 16), jr(), addu(V0, V0, T4),
        ])
        # The fallback is a tail call with the original arguments still in place.
        self.absolute(helper + 16, T6, table)
        self.redirect(helper + 56, start, call=True)
        tail = self.code([0, 0])
        self.redirect(tail, start)
        self.redirect(GROUP_SLOT, helper)
        self.word(GROUP_SLOT + 4, 0)

    def finish(self, moves, *, marker=MARKER, metadata=None, optional_metadata=None):
        phoff = u32(self.raw, 28)
        size_at = phoff + 32 * (len(self.segments) - 1) + 16
        old_size = self.last['memsz']
        payload_size = aligned(len(self.payload), 64)
        self.write_raw(size_at, struct.pack('<II', old_size + payload_size, old_size + payload_size))
        section = self.sections[self.rel_section]
        start, size = section['offset'], section['size']
        reloc = self.raw[start:start + size] + b''.join(struct.pack('<II', *r) for r in self.new_relocs)
        reloc_at = self.end + payload_size
        self.write_raw(self.section_offset + self.rel_section * 40 + 16,
                       struct.pack('<II', reloc_at, len(reloc)))
        records = {'moves': moves, 'ranges': list(self.changed.items())} if metadata is None else metadata(self.changed)
        meta = json.dumps(records, separators=(',', ':')).encode()
        if optional_metadata:
            available = self.limit - reloc_at - len(reloc) - len(marker) - 8
            for key, value in optional_metadata.items():
                extended = json.dumps({**records, key:value}, separators=(',', ':')).encode()
                if len(extended) <= available:
                    records[key] = value
                    meta = extended
        trailer = marker + struct.pack('<I', len(meta)) + meta
        if reloc_at + len(reloc) + len(trailer) + 4 > self.limit:
            raise ValueError('修改超出了奥0 EBOOT 预留的代码空间。')
        self.out[self.end:reloc_at] = self.payload.ljust(payload_size, b'\0')
        self.out[reloc_at:reloc_at + len(reloc)] = reloc
        trailer_at = self.limit - 4 - len(trailer)
        self.out[trailer_at:self.limit - 4] = trailer
        struct.pack_into('<I', self.out, self.limit - 4, trailer_at)
        return bytes(self.out)


def compiled_layout(raw, layout):
    result = deepcopy(layout)
    segments = result['segments']
    headers = u32(raw, 28) + 32 * (len(segments) - 1)
    segments[-1]['filesz'], segments[-1]['memsz'] = struct.unpack_from('<II', raw, headers + 16)
    section = next(i for i, row in enumerate(result['sections']) if row['name'] == '.rel.fini')
    row = result['sections'][section]
    row['offset'], row['size'] = struct.unpack_from('<II', raw, u32(raw, 32) + 40 * section + 16)
    return result


def installed_eboot(raw, layout, canonical, *, details=False):
    """Recover the stock executable bytes used to validate and parse normal v7 data."""
    last = layout['segments'][-1]
    end = last['offset'] + last['filesz']
    limit = payload_limit(canonical, layout)
    at = u32(raw, limit - 4)
    if not end <= at <= limit - len(MARKER) - 8 or not raw.startswith(MARKER, at):
        return (raw, [], {}) if details else (raw, [])
    start = at + len(MARKER) + 4
    if start + u32(raw, start - 4) != limit - 4:
        raise ValueError('ISO 的配招记录长度无效。')
    try:
        meta = json.loads(raw[start:limit - 4])
        changes = meta['ranges']
        moves = meta['moves']
        if not isinstance(moves, list) or not moves:
            raise ValueError
        restored = bytearray(raw)
        restored[end:limit] = canonical[end:limit]
        for offset, size in changes:
            if type(offset) is not int or type(size) is not int or not 0 <= offset < offset + size <= len(raw):
                raise ValueError
            restored[offset:offset + size] = canonical[offset:offset + size]
        widths = dict(changes)
        for offset, text in meta.get('before', ()):
            prior = bytes.fromhex(text)
            if type(offset) is not int or widths.get(offset) != len(prior):
                raise ValueError
            restored[offset:offset + len(prior)] = prior
    except (TypeError, ValueError, KeyError, UnicodeDecodeError):
        raise ValueError('ISO 的配招记录无效。') from None
    return (bytes(restored), moves, meta) if details else (bytes(restored), moves)

