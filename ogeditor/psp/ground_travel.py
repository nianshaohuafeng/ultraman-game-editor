"""Per-life-band cadence for FE0 ground travel, applied at the fighter update."""
import struct

from ogeditor.psp.ai_eboot import finish_layer
from move_eboot import (Eboot, Z, A0, V0, V1, T4, T5, T6, addiu, addu, branch,
                        compiled_layout, iword, lw, payload_limit, rword, sll, sltiu)
from ogeditor.psp.native_tables import LIFE_BANDS, NativeField

MARKER = b'KGFTRAVEL\x01'
TICK_CALL = 0x08809760
TICK = 0x08878108
RATE = NativeField(0, 0, '<f')


def load_rates(rows, roles):
    rates = {}
    if not isinstance(rows, list):
        raise ValueError('移动倍率记录无效。')
    for row in rows:
        if not isinstance(row, list) or len(row) != 3:
            raise ValueError('移动倍率记录无效。')
        role, band, value = row
        if (type(role) is not int or not 0 <= role < roles or type(band) is not int
                or not 0 <= band < LIFE_BANDS or (role, band) in rates):
            raise ValueError('移动倍率的角色或生命分段无效。')
        rates[role, band] = struct.unpack('<f', RATE.encode(value))[0]
    return rates


def install_rates(raw, layout, canonical, roles, rates):
    if not rates:
        return raw
    limit = payload_limit(canonical, layout)
    image = Eboot(raw, compiled_layout(raw, layout), limit)
    site = image.locate(TICK_CALL)[2]
    if raw[site:site+8] != canonical[site:site+8]:
        raise ValueError('移动动作更新入口与当前奥0基底不一致。')
    table = image.allocate(b''.join(RATE.encode(rates.get((r, b), 1.0))
                                   for r in range(roles) for b in range(LIFE_BANDS)))
    # a0 = fighter, f12 = simulation step. The original delay slot supplies a0.
    # States 2..8 cover walk, dash, run and the forward-running transition.
    words = [lw(V0, 100, A0), lw(V0, 20, V0), addiu(V0,V0,-2), sltiu(V0,V0,7)]
    skips = []
    def skip_if_zero(reg):
        skips.append((len(words),reg))
        words.extend([0,0])
    skip_if_zero(V0)
    words.extend([lw(V0,28,A0), lw(T4,0,V0), sltiu(V0,T4,roles)])
    skip_if_zero(V0)
    words.extend([lw(V0,104,A0), lw(T5,4,V0), lw(T6,8,V0),
                  rword(Z,T6,V0,0x2a)])  # slt v0, zero, maxHP
    skip_if_zero(V0)
    # The native HP range is 0..1024. Saturate full HP to band 7.
    words.extend([sll(T5,T5,3), rword(T5,T6,Z,0x1b), rword(Z,Z,V1,0x12),  # divu; mflo
                  sltiu(V0,V1,LIFE_BANDS), iword(5,V0,Z,2), 0,
                  addiu(V1,Z,LIFE_BANDS-1), sll(T4,T4,3), addu(V1,V1,T4), sll(V1,V1,2)])
    table_load = len(words)
    words.extend([0,0,addu(V0,V0,V1), iword(49,V0,0,0), 0x46006302])  # lwc1 f0; mul.s f12,f12,f0
    tail = len(words)
    words.extend([0,0])
    for pos,reg in skips:
        words[pos] = branch(4,reg,Z,tail-pos-1)
    entry = image.code(words)
    image.absolute(entry+4*table_load,V0,table)
    image.redirect(entry+4*tail,TICK)
    image.redirect(TICK_CALL,entry,call=True)
    rows = [[*key,value] for key,value in sorted(rates.items())]
    return finish_layer(image,raw,canonical,limit,MARKER,rows)
