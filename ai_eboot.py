"""Install difficulty-specific AI parameters over a stock or compiled v7 EBOOT."""

import base64
from collections import defaultdict
import json
import struct
import zlib

from ai_script import FIGHTERS, LEVELS, NEAR_DISTANCE
from formats import u32
from move_eboot import (A0, A1, A2, A3, BASE, Eboot, PTR, T4, T5, T6, T7, Z, addiu, addu, branch,
                        compiled_layout, iword, lw, payload_limit, rword, sll, sltiu, sw)


MARKER = b'KGFAI\x01'
INIT = 0x0887CFCC
INIT_CALLS = (0x0887C10C, 0x0887C2CC, 0x0887CBA0)
REINIT_CALL = 0x0887C2CC
T0, T1, T2, T3, T8, T9 = 8, 9, 10, 11, 24, 25
S2 = 18
SCRIPT_SIZE_LIMIT = 0x7FFF


def xor_bytes(left, right):
    return bytes(a ^ b for a, b in zip(left, right))


def strip_ai(raw, layout, canonical, marker=MARKER, *, details=False):
    """Recover the exact executable below the AI layer, including an installed move set."""
    limit = payload_limit(canonical, layout)
    at = u32(raw, limit - 4)
    last = layout['segments'][-1]
    base_end = last['offset'] + last['filesz']
    if not base_end <= at < limit - len(marker) - 8 or not raw.startswith(marker, at):
        return raw, None
    start = at + len(marker) + 4
    if start + u32(raw, start - 4) != limit - 4:
        raise ValueError('ISO 的 EBOOT 扩展记录长度无效。')
    try:
        meta = json.loads(raw[start:limit - 4])
        end = meta['end']
        if type(end) is not int or not base_end <= end <= at:
            raise ValueError
        decompressor = zlib.decompressobj()
        delta = decompressor.decompress(base64.b64decode(meta['tail'], validate=True), limit - end + 1)
        if not decompressor.eof or decompressor.unused_data or len(delta) != limit - end:
            raise ValueError
        out = bytearray(raw)
        out[end:limit] = xor_bytes(delta, canonical[end:limit])
        for offset, text in meta['before']:
            data = bytes.fromhex(text)
            if type(offset) is not int or len(data) not in (4, 8) or not 0 <= offset <= len(raw) - len(data):
                raise ValueError
            out[offset:offset + len(data)] = data
        return bytes(out), meta if details else meta['edits']
    except (TypeError, ValueError, KeyError, UnicodeDecodeError, zlib.error) as exc:
        raise ValueError('ISO 的 EBOOT 扩展记录无效。') from exc


def init_hook(eboot, profiles, scratch, stride):
    """Shadow one fighter's program on initialization, then tail-call the stock interpreter setup."""
    code, targets, branches = [], {}, []

    def label(name):
        targets[name] = len(code)

    def jump(op, left, right, to, delay=0):
        branches.append((len(code), op, left, right, to))
        code.extend((0, delay))

    # a0 is the AI object, a1 the stock initialization program. The practice
    # adapter supplies the fighter slot in a3 and enters after this first load.
    code.append(lw(A3, 0x130, A0))
    jump(4, A1, Z, 'stock', lw(T0, 0x318, A0))
    code.extend((sltiu(T1, T0, FIGHTERS), lw(T2, 0x26C, A0)))
    jump(4, T1, Z, 'stock', sltiu(T3, T2, LEVELS))
    jump(5, T3, Z, 'level', sll(T0, T0, 2))
    code.append(addiu(T2, Z, LEVELS - 1))
    label('level')
    code.extend((addu(T0, T0, T2), sll(T0, T0, 2)))
    profile_load = len(code)
    code.extend((0, 0, addu(T0, T0, T4), lw(T4, 0, T0)))
    jump(4, T4, Z, 'stock', sltiu(T2, A3, 2))
    code.append(lw(T0, 0, T4))
    jump(4, T2, Z, 'stock')
    jump(5, T0, A1, 'stock')

    scratch_load = len(code)
    code.extend((0, 0, addiu(A2, A2, 8), addiu(T0, A2, stride)))
    code.append(rword(T0, A3, A2, 0x0B))  # movn a2,t0,a3 chooses the second fighter's buffer.
    code.extend((lw(T0, 4, T4), addu(T1, Z, A1), addu(T2, Z, A2), addu(T0, T0, A1)))
    label('copy')
    code.extend((lw(T3, 0, T1), addiu(T1, T1, 4), sw(T3, 0, T2)))
    jump(5, T1, T0, 'copy', addiu(T2, T2, 4))

    # All six strategy tables repeat one program pointer in eight mode slots.
    # There are no address operands in the live instruction set.
    code.extend((rword(A2, A1, T8, 0x23), addiu(T0, T4, 16), addiu(T1, A0, 664), addiu(T9, T4, 40)))
    label('table')
    code.extend((lw(T2, 0, T0), addiu(T0, T0, 4), addu(T2, T2, A2),
                 lw(T3, 0, T2), sw(T2, 0, T1), addu(T3, T3, T8)))
    code.extend(sw(T3, off, T2) for off in range(0, 32, 4))
    jump(5, T0, T9, 'table', addiu(T1, T1, 4))

    code.extend((lw(T0, 8, T4), lw(T8, 12, T4), addu(A1, A2, Z)))
    jump(4, T0, Z, 'near')
    label('patch')
    code.extend((lw(T1, 0, T9), lw(T2, 4, T9), addiu(T9, T9, 8),
                 addu(T1, T1, A2), sw(T2, 0, T1), addiu(T0, T0, -1)))
    jump(5, T0, Z, 'patch')
    label('near')
    # An optional native "set near distance" instruction precedes the stock
    # initialization. Its continuation flag falls through to the stock program.
    jump(1, T8, Z, 'stock')  # bltz t8,stock
    code.extend((iword(15, Z, T0, 0x55AA), iword(13, T0, T0, 0x010A),
                 addiu(A1, A2, -8), sw(T0, 0, A1), sw(T8, 4, A1)))
    label('stock')
    code.extend((0, 0))
    for at, op, left, right, target in branches:
        code[at] = branch(op, left, right, targets[target] - at - 1)
    entry = eboot.code(code)
    eboot.absolute(entry + 4 * profile_load, T4, profiles)
    eboot.absolute(entry + 4 * scratch_load, A2, scratch)
    eboot.redirect(entry + 4 * targets['stock'], INIT)
    return entry


def install_ai(raw, layout, canonical, scripts, edits):
    if not edits:
        return raw
    limit = payload_limit(canonical, layout)
    eboot = Eboot(raw, compiled_layout(raw, layout), limit)
    groups = defaultdict(dict)
    for (role, difficulty, site), value in edits.items():
        groups[role, difficulty][site] = value
    stride = 8 + max(scripts[role].end - scripts[role].start for role, _ in groups)
    if stride > SCRIPT_SIZE_LIMIT:
        raise ValueError('AI 脚本块超出 v7 的复制缓冲区。')
    profiles = eboot.allocate(bytes(4 * FIGHTERS * LEVELS))
    for (role, difficulty), values in sorted(groups.items()):
        script = scripts[role]
        near = values.get(NEAR_DISTANCE, -1)
        patches = {site - script.start: script.field(site).encode(value)
                   for site, value in values.items() if site != NEAR_DISTANCE}
        if near >= 0:
            patches.update((site - script.start, near) for site in script.near_setters)
        header = (0, script.end - script.start, len(patches), near & 0xFFFFFFFF,
                  *(site - script.start for site in script.tables))
        block = eboot.code([*header, *(word for pair in sorted(patches.items()) for word in pair)])
        eboot.linked(block, script.start, PTR)
        eboot.linked(profiles + 4 * (role * LEVELS + difficulty), block, PTR)
    scratch = eboot.allocate(bytes(2 * stride), alignment=16)
    hook = init_hook(eboot, profiles, scratch, stride)
    # This entry resets the script after an in-battle practice option change.
    # Its s2 still holds the fighter, whose +4 slot is current even before the
    # AI has taken its first combat snapshot.
    practice = eboot.code([lw(A3, 4, S2), 0, 0])
    eboot.redirect(practice + 4, hook + 4)
    for call in INIT_CALLS:
        if u32(raw, eboot.locate(call)[2]) >> 26 != 3:
            raise ValueError('AI 初始化入口与 v7 不一致。')
        eboot.redirect(call, practice if call == REINIT_CALL else hook, call=True)

    rows = [[*key, value] for key, value in sorted(edits.items())]
    return finish_layer(eboot, raw, canonical, limit, MARKER, rows)


def finish_layer(eboot, raw, canonical, limit, marker, rows, **fields):
    delta = xor_bytes(raw[eboot.end:limit], canonical[eboot.end:limit])
    tail = base64.b64encode(zlib.compress(delta)).decode('ascii')
    return eboot.finish([], marker=marker, metadata=lambda changed: {
        **fields, 'end':eboot.end, 'tail':tail, 'edits':rows,
        'before':[[site, raw[site:site + size].hex()] for site, size in changed.items()],
    })
