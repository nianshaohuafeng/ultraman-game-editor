"""Shared native callbacks for multi-stage PSP moves."""

from collections import defaultdict
import struct

from formats import u32
from move_eboot import (A0, A1, A2, A3, BASE, PTR, RA, SP, T4, T5, T6, T7, V0, V1, Z,
                        addiu, addu, branch, iword, jr, lw, rword, sll, sltiu, sw)
from ogeditor.psp.native_tables import COMMAND_GETTERS, command_slot


FIGHTER_INIT = 0x089D3E3C
ELEKING_LEVELS = 0x089FB978
ELEKING_CHARGE = 0x08AEE6C0
ELEKING_SIXTH_VTABLE = 0x08AEEAC0
ELECTRIC_NODES = 0x089450CC
ACTION_GETTERS = 0x089D3E80
ACTION_FLAGS = 0x089DE608
FIRST_PRIVATE_STATE = 192
STATE_CALLBACKS = ((0x0889A718, 0x0889A864), (0x0889A818, 0x0889A8A4),
                   (0x0889A7E8, 0x0889A8D8))


def charge_factory(eboot, atlas):
    return eboot.clone(ELEKING_CHARGE, 0x08AEE79C, texture_words=(0x08AEE71C,), slot=atlas)


def install_borrowed_charge(eboot, image, roles, anchor_maps):
    """Use the native charge state for each side and keep its original effect identity."""
    for role in sorted(roles):
        if role == 11:
            continue
        pointer = FIGHTER_INIT + 4 * role
        original = u32(image, pointer - BASE)
        reset = eboot.code([
            lw(T4, 4, A0), sltiu(T5, T4, 2), branch(4, T5, Z, 6), 0,
            0, 0, sll(T4, T4, 2), addu(T6, T6, T4), sw(Z, 0, T6), 0, 0,
        ])
        eboot.absolute(reset + 16, T6, ELEKING_LEVELS)
        eboot.redirect(reset + 36, original)
        eboot.linked(pointer, reset, PTR)
    if not anchor_maps:
        return
    active = eboot.code([
        branch(4, A0, Z, 11), addiu(V0, Z, 0), lw(T4, 4, A0),
        sltiu(T5, T4, 2), branch(4, T5, Z, 7), sll(T4, T4, 2),
        0, 0, addu(T6, T6, T4), lw(T4, 0, T6), iword(14, T4, T4, 6),
        sltiu(V0, T4, 1), jr(), 0,
    ])
    eboot.absolute(active + 24, T6, ELEKING_LEVELS)
    for site in (0x08AEE940, 0x08AEEA90):
        eboot.redirect(site, active, call=True)
    lookup = electric_anchor_lookup(eboot, anchor_maps)
    install_anchor_load(eboot, lookup, 0x08851B98, biased=True)
    install_anchor_load(eboot, lookup, 0x08AEE9C0, biased=False)


def electric_anchor_lookup(eboot, maps):
    table = eboot.allocate(bytes(12 * (len(maps) + 1)))
    for index, ((descriptor, shape), anchors) in enumerate(sorted(maps.items())):
        record = table + index * 12
        eboot.linked(record, descriptor, PTR)
        eboot.word(record + 4, shape)
        array = eboot.allocate(struct.pack('<11I', *anchors))
        eboot.linked(record + 8, array, PTR)
    code = eboot.code([
        lw(T4, 36, A0), lw(T4, 0, T4), sll(T5, T4, 1), addu(T5, T5, T4),
        sll(T5, T5, 2), rword(T5, T4, T5, 0x23), sll(T5, T5, 2),
        0, 0, addu(V0, V0, T5),
        lw(T4, 80, A0), lw(T6, 0, T4), lw(T7, 8, T4), lw(A2, 36, T6), lw(A3, 256, T7),
        0, 0,
        # Each row contains the loaded descriptor address, form index and 11 node indices.
        lw(T4, 0, T6), branch(4, T4, Z, 9), 0,
        branch(5, T4, A2, 5), lw(T4, 4, T6),
        branch(5, T4, A3, 3), 0,
        branch(4, Z, Z, 3), lw(V0, 8, T6),
        branch(4, Z, Z, -10), addiu(T6, T6, 12),
        branch(4, A1, Z, 2), 0, rword(V0, T5, V0, 0x23), jr(), 0,
    ])
    eboot.absolute(code + 28, V0, ELECTRIC_NODES)
    eboot.absolute(code + 60, T6, table)
    return code


def install_anchor_load(eboot, lookup, site, biased):
    # Both native emitters keep their effect object in s3.
    registers = (RA, A0, A1, A2, A3, V0, V1, T4, T5, T6, T7)
    saved = [sw(reg, 4 * index, SP) for index, reg in enumerate(registers)]
    restored = [lw(reg, 4 * index, SP) for index, reg in enumerate(registers)]
    stub = eboot.code([addiu(SP, SP, -48), *saved, lw(A0, 36, 19), addiu(A1, Z, int(biased)),
                       0, 0, addu(21, V0, Z), *restored, 0, addiu(SP, SP, 48)])
    eboot.redirect(stub + 4 * (len(saved) + 3), lookup, call=True)
    eboot.redirect(stub + 4 * (len(saved) + len(restored) + 6), site + 8)
    eboot.replace_address_load(site, stub)


def install_private_states(eboot, game, definitions, public_flags):
    """Install the extra action, flag and input rows used by transplanted sequences."""
    by_role = defaultdict(dict)
    for (role, state), (flags, inputs) in sorted(definitions.items()):
        by_role[role][state] = flags, inputs
    image = game.image
    for role, flags in public_flags.items():
        if role in by_role:
            continue
        field = ACTION_FLAGS + 4 * role
        original = u32(image, field - BASE) - BASE
        count = len(game.native_actions[role]) - 145
        data = bytearray(image[original:original + 4 * count])
        for state, value in flags.items():
            struct.pack_into('<I', data, 4 * (state - 145), value)
        eboot.linked(field, eboot.allocate(data), PTR)
    if not by_role:
        return {}
    table = eboot.allocate(bytes(8 * (len(by_role) + 1)))
    records = {}
    for index, (role, entries) in enumerate(sorted(by_role.items())):
        count = len(entries)
        start = eboot.allocate(bytes(16 * count))
        # The dispatcher still multiplies the native state by 16. The base is biased
        # so that the first added state points directly at the first record.
        biased = start - 16 * FIRST_PRIVATE_STATE
        original = int(game.native_actions[role][0]['va'], 16)
        eboot.linked(table + 8 * index, original, PTR)
        eboot.linked(table + 8 * index + 4, biased, PTR)
        flag_ref = ACTION_FLAGS + 4 * role
        old_flags = u32(image, flag_ref - BASE)
        flag_array = [0] * (FIRST_PRIVATE_STATE + count - 145)
        for state in range(145, len(game.native_actions[role])):
            flag_array[state - 145] = u32(image, old_flags - BASE + (state - 145) * 4)
        for state, flags in public_flags.get(role, {}).items():
            flag_array[state - 145] = flags
        commands = eboot.allocate(bytes(4 * count))
        for offset, (state, (flags, inputs)) in enumerate(entries.items()):
            if state != FIRST_PRIVATE_STATE + offset:
                raise ValueError('配招阶段的状态编号不连续。')
            flag_array[state - 145] = flags
            if isinstance(inputs, int):
                pointer = u32(image, command_slot(image, BASE, role, inputs) - BASE)
                if pointer:
                    eboot.linked(commands + 4 * offset, pointer, PTR)
            elif inputs:
                branch_table = eboot.allocate(bytes(4 * (len(inputs) + 1)))
                for branch_index, row in enumerate(inputs):
                    data = eboot.allocate(struct.pack('<iIIi', *row))
                    eboot.linked(branch_table + 4 * branch_index, data, PTR)
                eboot.linked(commands + 4 * offset, branch_table, PTR)
            records[role, state] = start + 16 * offset
        eboot.linked(flag_ref, eboot.allocate(struct.pack(f'<{len(flag_array)}I', *flag_array)), PTR)
        install_extended_getter(eboot, image, ACTION_GETTERS + 4 * role, start, count, 4)
        install_extended_getter(eboot, image, COMMAND_GETTERS + 4 * role, commands, count, 2)
    install_extended_dispatch(eboot, table)
    return records


def install_extended_getter(eboot, image, pointer, rows, count, shift):
    old = u32(image, pointer - BASE)
    code = eboot.code([
        addiu(T4, A0, -FIRST_PRIVATE_STATE), sltiu(T5, T4, count),
        branch(4, T5, Z, 5), sll(T4, T4, shift), 0, 0,
        jr(), addu(V0, V0, T4), 0, 0,
    ])
    eboot.absolute(code + 16, V0, rows)
    eboot.redirect(code + 32, old)
    eboot.linked(pointer, code, PTR)


def install_extended_dispatch(eboot, table):
    lookup = eboot.code([
        0, 0,
        lw(T4, 0, T6), branch(4, T4, Z, 6), addu(V0, A1, Z),
        branch(4, T4, A1, 3), lw(T5, 4, T6),
        branch(4, Z, Z, -6), addiu(T6, T6, 8),
        addu(V0, T5, Z), jr(), 0,
    ])
    eboot.absolute(lookup, T6, table)
    for caller, routine in STATE_CALLBACKS:
        shim = eboot.code([
            sltiu(T4, A2, FIRST_PRIVATE_STATE), branch(5, T4, Z, 8), 0,
            addiu(SP, SP, -16), sw(RA, 0, SP), 0, 0, lw(RA, 0, SP),
            addu(A1, V0, Z), addiu(SP, SP, 16), 0, 0,
        ])
        eboot.redirect(shim + 20, lookup, call=True)
        eboot.redirect(shim + 40, routine)
        eboot.redirect(caller, shim, call=True)
