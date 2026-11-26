"""Special effects that own accessory models or request a paired action."""

import struct

from formats import u32
from move_eboot import (A0, A1, BASE, PTR, T4, T5, T6, T7, V0, Z, addiu, addu,
                        branch, iword, jr, lw, sll)
from move_roles import accessory_slot_view


def clone_vtable(eboot, image, source, count, entries):
    result = eboot.allocate(bytes(4 * count + 4))
    for i in range(count):
        original = u32(image, source - BASE + 4 * i)
        eboot.linked(result + 4 * i, entries.get(i, original), PTR)
    return result


def hipporit_capture(eboot, image, role, root, atlas):
    update = eboot.clone(0x08822784, 0x088229A0, immediates=((0x08822790, role),))
    vtable = clone_vtable(eboot, image, 0x0893BFE8, 5, {3: update})
    return eboot.clone(0x08884DFC, 0x08884F14, texture_words=(0x08884E6C, 0x08884E7C), slot=atlas,
                       node_loads=((0x08884E44, root),), pointers=((0x08884EC0, 0x08884EC4, vtable),))


def hipporit_chamber(eboot, image, slot, atlas):
    ctor = 0x08823280
    if slot != 0:
        source = 0x0893C0E8
        calls = {}
        for i in range(5):
            method = u32(image, source - BASE + 4 * i)
            calls[i] = accessory_slot_view(eboot, method, slot, 32 if i == 2 else 80,
                                          stock_slot=0, packet=(i == 2))
        vtable = clone_vtable(eboot, image, source, 5, calls)
        ctor = eboot.clone(ctor, 0x088232BC, pointers=((0x08823298, 0x088232A0, vtable),))
    return eboot.clone(0x08884F14, 0x08884FF4, texture_words=(0x08884F84,), slot=atlas,
                       calls={0x08823280: ctor})


BABAR_CHAIN_VTABLE = 0x0893B4A8


def babar_chain(eboot, image, slot, node, paired_state):
    methods = {}
    if slot != 0:
        for i in range(5):
            method = u32(image, BABAR_CHAIN_VTABLE - BASE + 4 * i)
            methods[i] = accessory_slot_view(eboot, method, slot, 40 if i == 2 else 88,
                                            stock_slot=0, packet=(i == 2))
    table = clone_vtable(eboot, image, BABAR_CHAIN_VTABLE, 5, methods)
    eboot.word(table + 20, paired_state)
    ctor = eboot.clone(0x08816930, 0x08816990, pointers=((0x0881694C, 0x08816950, table),))
    return eboot.clone(0x08885640, 0x08885708, ((0x08885670, node), (0x08885674, node)),
                       calls={0x08816930: ctor})


def install_babar_chain_update(eboot):
    # At this branch s1 is the chain object, a0 the active state and t8
    # the stock phase 180. A borrowed chain has its own phase 181 equivalent.
    s1, t8, t9 = 17, 24, 25
    route = eboot.code([
        lw(T4, 0, s1), 0, 0, branch(5, T4, T5, 5), addiu(t9, Z, 181),
        branch(4, A0, t8, 6), 0,
        branch(4, Z, Z, 2), 0,
        lw(t9, 20, T4), 0, 0, 0, 0,
    ])
    eboot.absolute(route + 4, T5, BABAR_CHAIN_VTABLE)
    eboot.redirect(route + 40, 0x088174CC)
    eboot.redirect(route + 48, 0x088174EC)
    eboot.redirect(0x088174C4, route)
    eboot.word(0x088174C8, 0)


def restore_babar_partner(eboot):
    # The entry callback hides the opponent before effect 111 begins.
    return eboot.code([
        lw(T4, 8, A0), lw(T5, 12, A0), sll(T5, T5, 2), addu(T4, T4, T5), lw(T4, 12, T4),
        lw(T4, 80, T4), lw(T4, 8, T4), addiu(T5, Z, 1), jr(), iword(40, T4, T5, 180),
    ])


def install_contact_routes(eboot, routes):
    for (caller, actor_register), entries in routes.items():
        keys = sorted(entries.items())
        rows = [(*key, target) for key, target in keys]
        table = eboot.allocate(b''.join(struct.pack('<3i', *row) for row in rows) + struct.pack('<3i', -1, 0, 0))
        stub = eboot.code([
            lw(T4, 36, actor_register), lw(T4, 0, T4), lw(T5, 100, actor_register),
            lw(T5, 20, T5), 0, 0,
            lw(T7, 0, T6), iword(1, T7, Z, 9), 0,
            branch(5, T7, T4, 5), lw(T7, 4, T6),
            branch(5, T7, T5, 3), 0,
            branch(4, Z, Z, 3), lw(A1, 8, T6),
            branch(4, Z, Z, -10), addiu(T6, T6, 12), 0, 0,
        ])
        eboot.absolute(stub + 16, T6, table)
        eboot.redirect(stub + 68, 0x0889A79C)
        eboot.redirect(caller, stub, call=True)


THROW_VICTIM_TABLE = 0x089464F0
THROW_STATES = (174, 175, 176)


def throw_victim_clip(image, defender, source, state):
    index = 139 * defender - 86 + 3 * source + state
    return struct.unpack_from('<h', image, THROW_VICTIM_TABLE - BASE + 2 * index)[0]


def install_throw_victims(eboot, image, moves):
    if not moves:
        return
    table = eboot.allocate(bytes(16 * (len(moves) + 1)))
    for i, ((role, state), (donor, source_state)) in enumerate(sorted(moves.items())):
        row = table + 16 * i
        clips = [throw_victim_clip(image, receiver, donor, source_state) for receiver in range(17)]
        eboot.write(row, struct.pack('<3I', 1 + 256 * role + state, donor, source_state))
        eboot.linked(row + 12, eboot.allocate(struct.pack('<17h', *clips), 4), PTR)
    # The shared received-throw entry already picked a clip for the actual
    # attacker. Substitute the donor's opposing clip only for a borrowed throw.
    select = eboot.code([
        lw(T4, 12, A0), lw(T5, 8, A0), sll(T4, T4, 2), addu(T5, T5, T4), lw(T4, 12, T5),
        lw(V0, 0, T5), lw(T5, 100, T4), lw(T4, 36, T4), lw(T4, 0, T4), lw(T5, 20, T5),
        sll(T4, T4, 8), addu(T4, T4, T5), addiu(T4, T4, 1), 0, 0,
        lw(T7, 0, T6), branch(4, T7, Z, 11), 0, branch(4, T7, T4, 3), 0,
        branch(4, Z, Z, -6), addiu(T6, T6, 16), lw(T5, 12, T6),
        lw(V0, 36, V0), lw(V0, 0, V0), sll(V0, V0, 1), addu(T5, T5, V0),
        iword(33, T5, A1, 0), 0, 0,
    ])
    eboot.absolute(select + 52, T6, table)
    eboot.redirect(select + 112, 0x08899780)
    eboot.redirect(0x088979D0, select, call=True)
    # The end of the received throw tests the original pair of role and phase.
    phase = 8
    leave = eboot.code([
        sll(T4, A1, 8), addu(T4, T4, phase), addiu(T4, T4, 1), 0, 0,
        lw(T7, 0, T6), branch(4, T7, Z, 7), 0, branch(4, T7, T4, 3), 0,
        branch(4, Z, Z, -6), addiu(T6, T6, 16), lw(A1, 4, T6), lw(phase, 8, T6),
        0, iword(14, phase, T4, 174),
    ])
    eboot.absolute(leave + 12, T6, table)
    eboot.redirect(leave + 56, 0x08897888)
    eboot.redirect(0x08897880, leave)
    eboot.word(0x08897884, lw(A1, 0, T6))

