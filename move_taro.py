"""The v7 Taro charge, strike, grab and explosion callbacks."""

from move_creatures import clone_vtable
from move_eboot import (A0, RA, SP, T4, T5, T6, V0, V1, Z, addiu, addu, branch,
                        jr, lw, sll, sltiu, sw)


TARO_DOWN = (4, 160)
TARO_PHASES = {
    181: '蓄力', 182: '前移', 183: '火焰攻击', 184: '抓取', 185: '收势', 186: '爆炸',
}
TARO_FIRE_NODES = (*range(0x08881C88, 0x08881CBC, 4), 0x08881CC0)
_FIRE_REGISTER = 0x08AEFA80
_FIRE_CHECK = 0x08AEEC5C
_FIRE_UPDATE = 0x08AEFD48
_FIRE_DRAW = 0x08AF0090
_ROLE_ARG = 8


def taro_fire_callbacks(eboot):
    records = eboot.allocate(bytes(64))
    lookup = eboot.clone(0x08AEEBC0, _FIRE_CHECK, pointers=((0x08AEEBF4, 0x08AEEBF8, records),))

    def as_taro(start, end, role_read, **options):
        code = eboot.clone(start, end, **options)
        # The private record tracks the actual role to detect fighter swaps.
        # These specific consumers only need to know that it stores Taro's fire.
        eboot.word(code + role_read - start, addiu(_ROLE_ARG, Z, 4))
        return code

    check = as_taro(_FIRE_CHECK, 0x08AEECE4, 0x08AEEC90, calls={0x08AEEBC0: lookup})
    enter = as_taro(0x08AEECE4, 0x08AEEF80, 0x08AEEE1C, calls={0x08AEEBC0: lookup})
    # Only the hit effect query should accept the private record as well.
    either = eboot.code([
        addiu(SP, SP, -16), sw(RA, 0, SP), sw(A0, 4, SP), 0, 0,
        lw(RA, 0, SP), lw(A0, 4, SP), branch(4, V0, Z, 3), addiu(SP, SP, 16),
        jr(), 0, 0, 0,
    ])
    eboot.redirect(either + 12, check, call=True)
    eboot.redirect(either + 44, _FIRE_CHECK)
    eboot.redirect(0x08AEF0A0, either, call=True)
    reset = eboot.code([
        lw(T4, 4, A0), sltiu(T5, T4, 2), branch(4, T5, Z, 7), sll(T4, T4, 5), 0, 0,
        addu(T6, T6, T4), sw(Z, 0, T6), sw(Z, 16, T6), sw(Z, 24, T6), 0, 0,
    ])
    eboot.absolute(reset + 16, T6, records)
    eboot.redirect(reset + 40, 0x08AEF534)
    eboot.redirect(0x0880D7D0, reset, call=True)
    return enter, check


def taro_flames(eboot, image, anchors, atlas, check, first_phase):
    update = eboot.clone(_FIRE_UPDATE, 0x08AEFE30, calls={_FIRE_CHECK: check},
                         immediates=((0x08AEFDC0, -first_phase),))
    eboot.word(update + 0x08AEFD80 - _FIRE_UPDATE, addiu(_ROLE_ARG, Z, 4))
    draw = eboot.clone(_FIRE_DRAW, 0x08AF00F8, calls={_FIRE_CHECK: check})
    eboot.word(draw + 0x08AF00B0 - _FIRE_DRAW, addiu(_ROLE_ARG, Z, 4))
    table = clone_vtable(eboot, image, 0x08AF0168, 5, {3: update, 4: draw})
    register = eboot.clone(_FIRE_REGISTER, 0x08AEFD48, calls={_FIRE_CHECK: check},
                           pointers=((0x08AEFC18, 0x08AEFC1C, table),))
    eboot.word(register + 0x08AEFBC0 - _FIRE_REGISTER, addiu(_ROLE_ARG, Z, 4))
    return eboot.clone(0x08881C00, 0x08881DF4, node_words=anchors, texture_words=(0x08881D50,),
                       slot=atlas, calls={_FIRE_REGISTER: register})


def taro_timer_callbacks(eboot, explosion):
    return {start: eboot.clone(start, start + 0xC8, immediates=((start + 0x74, explosion),))
            for start in (0x0883DC5C, 0x0883DD5C, 0x0883DE5C, 0x0883DF5C, 0x0883E05C)}


def install_taro_hit_routes(eboot, phases):
    states = [0xFFFFFFFF] * 17
    for role, phase in phases.items():
        states[role] = phase
    table = eboot.allocate(bytes(4 * len(states)))
    for index, phase in enumerate(states):
        eboot.word(table + 4 * index, phase)

    def gate(site, role, stock_role, actor, fallback, native, native_delay, borrowed, borrowed_delay):
        code = eboot.code([
            branch(4, role, stock_role, 12), 0, lw(T4, 100, actor), lw(T4, 20, T4),
            sll(T5, role, 2), 0, 0, addu(T6, T6, T5), lw(T6, 0, T6),
            branch(4, T4, T6, 5), 0, 0, 0, 0, native_delay, 0, borrowed_delay,
        ])
        eboot.absolute(code + 20, T6, table)
        eboot.redirect(code + 44, fallback)
        eboot.redirect(code + 52, native)
        eboot.redirect(code + 60, borrowed)
        eboot.redirect(site, code)

    # Attacker B7 goes to the explosion; the defender in B7 is treated by
    # the native fire reaction branch. These comparisons keep real role IDs.
    gate(0x088082D4, 9, 21, 18, 0x088082DC, 0x08808394, 0, 0x088083A8, lw(A0, 100, 18))
    gate(0x08808220, V1, V0, 17, 0x08808228, 0x08808450, lw(8, 100, 17), 0x08808460, 0)
    eboot.word(0x08808224, 0)
