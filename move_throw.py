"""Leo's nunchaku throw, its hand attachment, and the native paired state entry."""

import struct

from formats import anm_header, s16, u32
from move_eboot import (A0, A1, BASE, PTR, RA, SP, T4, T5, T6, T7, Z, addiu, addu, aligned,
                        branch, iword, jr, lw, sll, sw)


LEO_THROW = (5, 160)
LEO_THROW_STAGES = {183: '抓取成立', 184: '第二击', 185: '第三击', 186: '第四击', 187: '收尾'}
LEO_WEAPON_TRACKS = (85, 86)
LEO_WEAPON_NODES = (60, 61, 62, 63)
LEO_WEAPON_POM = 792
ENTER_ENDS = {0x0882A540: 0x0882A63C, 0x0882A704: 0x0882A800, 0x0882A8C8: 0x0882A9C4,
              0x0882AA8C: 0x0882AB88, 0x0882AC30: 0x0882AD30}


def weapon_clip(data):
    out = bytearray(data)
    for index in range(anm_header(data)[2]):
        offset = 52 + index * 14
        track = s16(data, offset)
        if track in (77, 78):
            struct.pack_into('<H', out, offset, LEO_WEAPON_TRACKS[track - 77])
    return bytes(out)


def graft_weapon(model, raw, donor, source, hand, texture):
    from model_retarget import block_bytes, shift_materials

    material_count, count = struct.unpack_from('<2H', raw, 12)
    mat, bones, start, end = struct.unpack_from('<4I', raw, 16)
    if count != len(model['bones']) or not 0 <= hand < count or bones + 64 * count != start:
        raise ValueError('目标 MDL 的节点目录无法挂接双截棍。')
    src_mat, src_bones = struct.unpack_from('<2I', source, 16)
    material = bytearray(source[src_mat + 32 * 8:src_mat + 32 * 9])
    struct.pack_into('<I', material, 20, texture)
    new_bones = bones + len(material)
    grow = len(material) + len(LEO_WEAPON_NODES) * 64
    out = bytearray(raw[:bones] + material + raw[bones:start] + bytes(4 * 64) + raw[start:end])
    for node in range(count):
        row = new_bones + 64 * node
        struct.pack_into('<I', out, row + 60, u32(out, row + 60) + grow)
    hand_child = new_bones + 64 * hand + 54
    sibling = s16(out, hand_child)
    struct.pack_into('<h', out, hand_child, count)
    mapped = dict(zip(LEO_WEAPON_NODES, range(count, count + 4)))
    for node in LEO_WEAPON_NODES:
        row = new_bones + 64 * mapped[node]
        original = src_bones + 64 * node
        out[row:row + 64] = source[original:original + 64]
        parent = hand if node == 60 else mapped[s16(source, original + 50)]
        following = sibling if node == 60 else mapped.get(s16(source, original + 52), -1)
        child = mapped.get(s16(source, original + 54), -1)
        mesh_offset = aligned(len(out))
        out.extend(bytes(mesh_offset - len(out)))
        struct.pack_into('<3h', out, row + 50, parent, following, child)
        struct.pack_into('<I', out, row + 60, mesh_offset)
        mesh = next((entry for entry in donor['meshes'] if entry['owner'] == node), None)
        if mesh is not None:
            block = bytearray(shift_materials(block_bytes(source, mesh), donor['materials'], material_count - 8))
            palette = aligned(32 + 2 * len(mesh['material_indices']))
            for index, original_node in enumerate(mesh['palette']):
                struct.pack_into('<H', block, palette + index * 2, mapped[original_node])
            out.extend(block)
    struct.pack_into('<I', out, 4, len(out) + 8)
    struct.pack_into('<2H3I', out, 12, material_count + 1, count + 4, mat, new_bones, start + grow)
    struct.pack_into('<I', out, 28, len(out))
    return bytes(out)


def install_weapon_visibility(eboot, attachments, initialize=0x0888A600):
    # From 0888C968 onwards the initialized model keeps its remap pointer at +44
    # and its native node array at +96. These custom maps identify the grafted forms.
    table = eboot.allocate(bytes(8 * (len(attachments) + 1)))
    for i, (pointer, first) in enumerate(sorted(attachments.items())):
        eboot.linked(table + 8 * i, pointer, PTR)
        eboot.word(table + 8 * i + 4, first * 544)
    toggle = eboot.code([
        lw(T4, 44, A0), 0, 0, lw(T6, 0, T5), branch(4, T6, Z, 11), 0,
        branch(4, T6, T4, 3), lw(T7, 4, T5),
        branch(4, Z, Z, -6), addiu(T5, T5, 8),
        lw(T4, 96, A0), addu(T4, T4, T7), iword(40, T4, A1, 109),
        iword(40, T4, A1, 653), iword(40, T4, A1, 1197), iword(40, T4, A1, 1741),
        jr(), 0,
    ])
    eboot.absolute(toggle + 4, T5, table)
    init = eboot.code([
        addiu(SP, SP, -16), sw(RA, 0, SP), 0, sw(A0, 4, SP),
        lw(A0, 4, SP), lw(RA, 0, SP), addu(A1, Z, Z), 0, addiu(SP, SP, 16),
    ])
    eboot.redirect(init + 8, initialize, call=True)
    eboot.redirect(init + 28, toggle)
    eboot.redirect(0x0888C968, init, call=True)
    return toggle


def weapon_state_callbacks(eboot, game, toggle):
    noop = eboot.code([jr(), 0])
    callbacks = {}
    for state in LEO_THROW_STAGES:
        move = game.native_actions[5][state]
        enter, update, exit_ = (int(value, 16) for value in move['callbacks'])
        enter = eboot.clone(enter, ENTER_ENDS[enter], calls={0x088D92F0: noop})
        callbacks[state] = (update, wrap_weapon_callback(eboot, toggle, 1, enter),
                            wrap_weapon_callback(eboot, toggle, 0, None if state == 187 else exit_))
    return callbacks


def wrap_weapon_callback(eboot, toggle, enabled, original):
    code = eboot.code([
        addiu(SP, SP, -16), sw(RA, 0, SP), sw(A0, 4, SP), lw(T4, 8, A0),
        lw(T5, 12, A0), sll(T5, T5, 2), addu(T4, T4, T5), lw(T4, 0, T4),
        lw(T4, 80, T4), lw(A0, 8, T4), 0, addiu(A1, Z, enabled),
        lw(A0, 4, SP), lw(RA, 0, SP), jr(), addiu(SP, SP, 16),
    ])
    eboot.redirect(code + 40, toggle, call=True)
    if original is not None:
        eboot.redirect(code + 56, original)
    return code


def install_throw_entry(eboot, game, leo, jack, taro):
    source = 0x08806E84
    stock = 0x0894E910
    plans = {}
    for role, first in leo.items():
        plans[role, 160] = 6, first, first, ((0x088070E8, role),)
    for role, (first, second) in jack.items():
        # Run Jack's type-4 branch even when Leo is the recipient.
        plans[role, 160] = 4, first, second, ((0x088070C8, role), (0x088070E8, -1))
    for role, (grab, explosion) in taro.items():
        plans[role, grab] = 5, explosion, explosion, ((0x088070D8, role), (0x0880710C, grab))
    dispatch = eboot.allocate(bytes(12 * (len(plans) + 1)))
    for index, ((role, state), (kind, first, second, checks)) in enumerate(sorted(plans.items())):
        table = eboot.allocate(bytes(game.image[stock - BASE:stock - BASE + 8 * 16]))
        eboot.word(table + kind * 16, first)
        eboot.word(table + kind * 16 + 8, second)
        throw = eboot.clone(source, 0x088071B8, immediates=checks,
                            pointers=((0x08806F7C, 0x08806F84, table), (0x08806F78, 0x08807078, table)))
        entry = dispatch + 12 * index
        eboot.write(entry, struct.pack('<2I', role + 1, state))
        eboot.linked(entry + 8, throw, PTR)
    choose = eboot.code([
        sll(T4, A1, 2), lw(T5, 16, A0), addu(T5, T5, T4), lw(T5, 0, T5),
        lw(T4, 100, T5), lw(T4, 20, T4), lw(T5, 36, T5), lw(T5, 0, T5), addiu(T5, T5, 1),
        0, 0, lw(T7, 0, T6), branch(4, T7, Z, 10), 0,
        branch(5, T7, T5, 6), lw(T7, 4, T6), branch(5, T7, T4, 4), 0,
        lw(T7, 8, T6), jr(T7), 0,
        branch(4, Z, Z, -11), addiu(T6, T6, 12), 0, 0,
    ])
    eboot.absolute(choose + 36, T6, dispatch)
    eboot.redirect(choose + 92, source)
    eboot.redirect(0x08806DB0, choose, call=True)

