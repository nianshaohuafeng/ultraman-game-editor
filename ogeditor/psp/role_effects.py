"""Native role-specific effects, scene bindings and opponent reactions."""

from collections import defaultdict
import struct

from formats import s16, u32
from move_eboot import (A0, A1, A2, A3, BASE, PTR, RA, SP, T4, T5, T6, T7, V0, Z, addiu, addu, branch,
                        iword, jr, lw, rword, sll, sltiu, sw)
from ogeditor.psp.move_runtime import EXPLICIT_CAMERA_SLOT


JACK_THROW = (2, 160)
BACK_SKILLS = {(role, 162): (163, 164) for role in (0, 2, 3, 4, 5, 6, 7, 9, 10, 13, 15)}
BACK_SKILLS.update({(1, 162): (163, 164, 181), (5, 162): (163, 164, 188),
                    (8, 162): (163, 164), (11, 163): (180,), (12, 163): (180,), (14, 163): (180,),
                    (16, 162): (163,)})
SCENE_ROLES = {19: 0, 24: 1, 33: 2, 39: 3, 47: 4, 50: 5, 54: 6, 57: 7, 61: 8, 66: 9, 69: 10,
               74: 11, 84: 12, 89: 13, 99: 15}
# These are explicit positive ANM selections made by the native entry
# callbacks. A selection of -1 uses the default ANM in the action row.
ACTION_CLIPS = {(2, 181): (250, 258, 265, 272, 279, 286), (2, 182): (249,),
                (0, 174): (272, 279, 292), (0, 175): (271, 278, 291),
                (0, 176): (258, 264, 270, 277, 285, 290),
                (1, 175): (255, 260, 266, 272, 277, 283, 288),
                (2, 174): (271, 285), (2, 175): (257,), (2, 176): (256, 264, 270, 278, 284),
                (3, 174): (220, 251, 258, 264, 271, 278, 284, 290),
                (3, 176): (245, 250, 257, 265, 270, 277, 283),
                (4, 175): (269, 275, 287), (4, 176): (262, 268, 274, 281, 286),
                (6, 174): (226, 238, 246, 253, 261, 268), (6, 175): (237, 245, 252, 260, 267),
                (6, 176): (179, 236, 244, 251, 259, 266),
                (7, 175): (218, 224, 236), (7, 176): (211, 217, 223, 230, 235),
                (8, 174): (222, 232, 239), (8, 176): (221, 238),
                (10, 174): (204, 211, 219, 227, 234, 242, 249, 255),
                (10, 175): (203, 210, 218, 226, 233, 241, 248),
                (10, 176): (202, 209, 217, 225, 232, 240, 247),
                (11, 180): (211, 217, 222, 228, 233),
                (12, 175): (204, 209, 215, 222, 228, 235, 241),
                (13, 174): (210, 215, 221, 227, 232, 238, 243),
                (12, 180): (216, 223, 229, 236, 242)}
SCENE_SCRIPTS = {
    19: (0x088807A4, 0x088807F8, ((0x088807C4, 0x088807D0, 0x089D43F0, 0x089D4CD0),)),
    24: (0x08880F00, 0x08880F54, ((0x08880F20, 0x08880F2C, 0x089D4CD0, 0x089D5790),)),
    33: (0x088812D8, 0x0888132C, ((0x088812F8, 0x08881304, 0x089D5790, 0x089D5FF0),)),
    39: (0x08881A34, 0x08881A88, ((0x08881A54, 0x08881A60, 0x089D5FF0, 0x089D6850),)),
    47: (0x08881F08, 0x08881F5C, ((0x08881F28, 0x08881F34, 0x089D6850, 0x089D70F0),)),
    50: (0x088821DC, 0x08882230, ((0x088821FC, 0x08882208, 0x089D70F0, 0x089D7870),)),
    54: (0x08882790, 0x088827E4, ((0x088827B0, 0x088827BC, 0x089D7870, 0x089D8210),)),
    57: (0x08882A80, 0x08882AD4, ((0x08882AA0, 0x08882AAC, 0x089D8210, 0x089D8B10),)),
    61: (0x08882DA4, 0x08882DF8, ((0x08882DC4, 0x08882DD0, 0x089DBF30, 0x089DC790),)),
    66: (0x08883310, 0x08883364, ((0x08883330, 0x0888333C, 0x089D8DB0, 0x089D95B0),)),
    69: (0x088835C0, 0x08883614, ((0x088835E0, 0x088835EC, 0x089D95B0, 0x089D9E50),)),
    74: (0x08883A60, 0x08883AB4, ((0x08883A80, 0x08883A8C, 0x089D8B10, 0x089D8DB0),)),
    89: (0x08884908, 0x0888495C, ((0x08884928, 0x08884934, 0x089DA150, 0x089DAA50),)),
    84: (0x08883E5C, 0x08883EB0, ((0x08883E7C, 0x08883E88, 0x089D9E50, 0x089DA150),)),
    99: (0x088851AC, 0x08885258, ((0x088851F4, 0x088851F8, 0x089DAA50, 0x089DB3D0),
                                  (0x0888524C, 0x08885254, 0x089DB3D0, 0x089DBF30))),
}
SCENE_FACTORIES = {
    19: (0x088743D8, 0x0887447C, (0x0887441C,), (0x08874408,)),
    24: (0x0887447C, 0x08874568, (0x088744D8, 0x088744F0), (0x088744E8,)),
    39: (0x08874654, 0x0887474C, (0x088746B0, 0x088746B4, 0x088746DC), (0x088746C4,)),
    47: (0x0887474C, 0x0887484C, (0x088747CC, 0x088747E0), (0x0887477C, 0x08874790, 0x088747B4)),
    50: (0x0887484C, 0x0887493C, (0x088748A8, 0x088748BC), (0x08874894,)),
    54: (0x0887493C, 0x088749F8, (0x0887495C, 0x08874960), (0x08874974, 0x0887498C)),
    57: (0x088749F8, 0x08874AD8, (0x08874A54,), (0x08874A50,)),
    61: (0x08874AD8, 0x08874BE0, (0x08874B08,), (0x08874B20,)),
    66: (0x08874BE0, 0x08874CC8, (0x08874C54, 0x08874C58), (0x08874C24,)),
    69: (0x08874CC8, 0x08874D64, (0x08874D08,), (0x08874CF4,)),
    89: (0x08874D74, 0x08874E14, (0x08874DB8,), (0x08874DA4,)),
    99: (0x08874E1C, 0x08874EE8, (0x08874E70, 0x08874E78), (0x08874E44, 0x08874E60)),
}
BLACK_SCENE_SOURCE = (12, 0, 1)
BLACK_AUX_CLIP = 154
SCENE_EFFECT_BASE = 0x1000
SELECT_ANIMATION = 0x08899780
INIT_FORM = 0x0888A600


def clone_scene(eboot, image, effect, states, event, overlay, camera, node=None):
    start, end, scripts = SCENE_SCRIPTS[effect]
    first_camera, camera_count = camera
    pointers = []
    for hi, lo, lower, upper in scripts:
        data = bytearray(image[lower - BASE:upper - BASE])
        for at in range(0, len(data), 32):
            frame, op, actor, arg, value = struct.unpack_from('<f4I', data, at)
            if frame == -1.0:
                break
            if op == 4 and actor == 0 and arg in states:
                struct.pack_into('<I', data, at + 12, states[arg])
            elif op == 6 and actor == 0 and value + 1 < camera_count:
                # Later cues can run while the attacker is in a shared demo
                # state. Store the source track directly in the script.
                struct.pack_into('<I', data, at + 16, EXPLICIT_CAMERA_SLOT | (first_camera + value))
            elif op == 0x23 and event is not None:
                struct.pack_into('<I', data, at + 12, event)
            elif op == 2 and actor == 0 and node is not None:
                struct.pack_into('<I', data, at + 16, node)
            elif overlay is not None and actor == 0 and arg == 1:
                shape, clip = overlay
                if op == 0:
                    struct.pack_into('<I', data, at + 12, shape)
                elif op == 0x13 and value == BLACK_AUX_CLIP:
                    struct.pack_into('<2I', data, at + 12, shape, clip)
        else:
            # The v7 Mebius script ends with an explicit end opcode
            # immediately before the effect table, with no -1 frame.
            if effect != 61 or data[-32:] != struct.pack('<f7I', 1510.0, 18, 0, 0, 0, 0, 0, 0):
                raise ValueError('必杀演出脚本的终止记录与 v7 不匹配。')
            data.extend(struct.pack('<f7I', -1.0, 0, 0, 0, 0, 0, 0, 0))
        pointers.append((hi, lo, eboot.allocate(data, 16)))
    return eboot.clone(start, end, pointers=pointers)


def install_scene_effects(eboot, callbacks):
    if not callbacks:
        return
    table = eboot.allocate(bytes(4 * len(callbacks)))
    for i, callback in enumerate(callbacks):
        eboot.linked(table + i * 4, callback, PTR)
    # Opcode 0x23 normally selects an effect by the actor's original character.
    # The augmented records carry the factory for this scene and model form.
    hook = eboot.code([
        lw(T4, 12, A1), addiu(T4, T4, -SCENE_EFFECT_BASE), sltiu(T5, T4, len(callbacks)),
        branch(4, T5, Z, 16), sll(T4, T4, 2), 0, 0, addu(T5, T5, T4),
        lw(T5, 0, T5), lw(A2, 0, A0), branch(4, A2, Z, 7), lw(A3, 4, A0),
        addiu(SP, SP, -16), sw(RA, 0, SP), rword(T5, Z, RA, 9), addu(8, Z, Z),
        lw(RA, 0, SP), addiu(SP, SP, 16), jr(), addiu(V0, Z, 1), 0, 0,
    ])
    eboot.absolute(hook + 20, T5, table)
    eboot.redirect(hook + 80, 0x088722A4)
    eboot.redirect(0x088709E4, hook, call=True)


def install_animation_aliases(eboot, aliases):
    if not aliases:
        return
    rows = [(*key, new) for key, new in sorted(aliases.items())]
    table = eboot.allocate(b''.join(struct.pack('<4I', *row) for row in rows) + struct.pack('<4i', -1, 0, 0, 0))
    _, _, offset = eboot.locate(SELECT_ANIMATION)
    first, second = struct.unpack_from('<2I', eboot.raw, offset)
    if any(eboot.relocs.get(SELECT_ANIMATION + delta) for delta in (0, 4)):
        raise ValueError('ANM 选择入口与 v7 的重定位不匹配。')
    # Negative requests use the active action row. For explicit requests the
    # actual fighter and active state disambiguate the donor's clip number.
    code = eboot.code([
        iword(1, A1, Z, 25), 0,
        lw(T4, 12, A0), lw(T5, 8, A0), sll(T4, T4, 2), addu(T5, T5, T4), lw(T5, 0, T5),
        lw(T4, 36, T5), lw(T4, 0, T4), lw(T6, 100, T5), lw(T6, 20, T6), 0, 0,
        lw(T7, 0, T5), iword(1, T7, Z, 11), 0,
        branch(5, T7, T4, 7), lw(T7, 4, T5),
        branch(5, T7, T6, 5), lw(T7, 8, T5),
        branch(5, T7, A1, 3), 0, branch(4, Z, Z, 3), lw(A1, 12, T5),
        branch(4, Z, Z, -12), addiu(T5, T5, 16), first, 0, second,
    ])
    eboot.absolute(code + 44, T5, table)
    eboot.redirect(code + 108, SELECT_ANIMATION + 8)
    eboot.redirect(SELECT_ANIMATION, code)
    eboot.word(SELECT_ANIMATION + 4, 0)


def jack_scene_factory(eboot, image, node, atlas, slot):
    stock = 0x0893C608
    vtable = eboot.allocate(bytes(24))
    for index in range(5):
        fn = u32(image, stock - BASE + index * 4)
        if index in (0, 1):
            fn = accessory_cleanup(eboot, fn, slot, 8404)
        elif index in (3, 4) and slot != 1:
            fn = accessory_slot_view(eboot, fn, slot, 8404)
        eboot.linked(vtable + 4 * index, fn, PTR)
    ctor = eboot.clone(0x08827DA0, 0x08827DE4, pointers=((0x08827DBC, 0x08827DC4, vtable),))
    return eboot.clone(0x08874568, 0x08874654, ((0x088745E4, node),),
                       (0x08874598, 0x088745C0), atlas, calls={0x08827DA0: ctor})


def accessory_slot_view(eboot, original, slot, owner_offset, stock_slot=1, packet=False):
    # Scope the view to this native method. The fighter's own slot table
    # is restored before the next update or draw.
    owner = [lw(T4, 4, A1), lw(T4, owner_offset, T4)] if packet else [lw(T4, owner_offset, A0)]
    stub = eboot.code([
        *owner, lw(T4, 80, T4), lw(T5, 12, T4),
        addiu(T6, T5, 176 * (slot - stock_slot)), addiu(SP, SP, -16),
        sw(T4, 4, SP), sw(T5, 8, SP), sw(RA, 0, SP), 0, sw(T6, 12, T4),
        lw(T4, 4, SP), lw(T5, 8, SP), lw(RA, 0, SP), sw(T5, 12, T4),
        jr(), addiu(SP, SP, 16),
    ])
    eboot.redirect(stub + 4 * (len(owner) + 7), original, call=True)
    return stub


def accessory_cleanup(eboot, original, slot, owner_offset):
    # Each item owns the renderer's visibility byte at +116.
    code = eboot.code([
        lw(T4, owner_offset, A0), branch(4, T4, Z, 8), 0,
        lw(T4, 80, T4), lw(T4, 12, T4), addiu(T4, T4, 176 * slot),
        lw(T5, 4, T4), branch(4, T5, Z, 2), 0,
        iword(40, T4, Z, 116), 0, 0,
    ])
    eboot.redirect(code + 40, original)
    return code


def install_model_forms(eboot, game, overlays, weapon_forms):
    """Extend the native body-model descriptor without reordering its existing forms."""
    image = game.image
    overlay_maps = []
    visibility = {}
    grouped = defaultdict(dict)
    for (descriptor, shape), entry in weapon_forms.items():
        grouped[descriptor][shape] = entry
    for desc in sorted(overlays.keys() | grouped.keys()):
        extra = overlays.get(desc)
        if extra is not None:
            case, mdl, pom_slots, remap = extra
        else:
            case = next(iter(grouped[desc].values()))[0]
        count = int(case['counts_va'], 16)
        size = len(case['forms'])
        if extra is not None:
            for off, value in ((20, mdl), (44, 0xFE20)):
                source = u32(image, desc + off - BASE) - BASE
                data = image[source:source + size * 2] + struct.pack('<H', value)
                eboot.linked(desc + off, eboot.allocate(data), PTR)
        arrays = ((48, 24), (52, 28), (60, 32)) if extra else ((48, 24), (60, 32))
        for offset, count_offset in arrays:
            ptr = u32(image, desc + offset - BASE) - BASE
            lengths = u32(image, count + count_offset - BASE) - BASE
            sizes = list(struct.unpack_from(f'<{size}h', image, lengths))
            pointers = list(struct.unpack_from(f'<{size}I', image, ptr))
            for shape, (_, first, slots, nodes) in grouped.get(desc, {}).items():
                if offset == 48:
                    pointers[shape] = eboot.allocate(struct.pack(f'<{len(slots)}H', *slots))
                    sizes[shape] = len(slots)
                if offset == 60:
                    pointers[shape] = eboot.allocate(b''.join(struct.pack('<3h', *row) for row in nodes))
                    sizes[shape] = len(nodes)
                    visibility[pointers[shape]] = first
            if extra is not None:
                if offset == 48:
                    data = struct.pack(f'<{len(pom_slots)}H', *pom_slots)
                    sizes.append(len(pom_slots))
                elif offset == 52:
                    data = struct.pack('<H', 0xFFFF)
                    sizes.append(1)
                else:
                    data = b''.join(struct.pack('<3h', *row) for row in remap)
                    sizes.append(len(remap))
                pointers.append(eboot.allocate(data))
                if offset == 60:
                    overlay_maps.append(pointers[-1])
            array = eboot.allocate(bytes(4 * len(pointers)))
            for i, value in enumerate(pointers):
                if value:
                    eboot.linked(array + 4 * i, value, PTR)
            eboot.linked(desc + offset, array, PTR)
            eboot.linked(count + count_offset, eboot.allocate(struct.pack(f'<{len(sizes)}h', *sizes)), PTR)
        if extra is not None:
            eboot.half(count + 12, size + 1)
    return overlay_maps, visibility


def overlay_initializer(eboot, maps):
    table = eboot.allocate(bytes(4 * (len(maps) + 1)))
    for index, pointer in enumerate(maps):
        eboot.linked(table + index * 4, pointer, PTR)
    stub = eboot.code([
        lw(T4, 44, A1), 0, 0, lw(T6, 0, T5), branch(4, T6, Z, 16), 0,
        branch(4, T6, T4, 3), 0, branch(4, Z, Z, -6), addiu(T5, T5, 4),
        addiu(SP, SP, -16), sw(A0, 4, SP), sw(RA, 0, SP), 0, sw(Z, 48, A1),
        lw(T4, 4, SP), lw(RA, 0, SP), iword(40, T4, Z, 180), addiu(SP, SP, 16), jr(), 0, 0, 0,
    ])
    eboot.absolute(stub + 4, T5, table)
    eboot.redirect(stub + 52, INIT_FORM, call=True)
    eboot.redirect(stub + 84, INIT_FORM)
    return stub


def black_pair_callbacks(eboot):
    # Last form is the borrowed companion model. The model allocator retains its count at -4.
    last = [lw(T4, -4, A2), addiu(T4, T4, -1), sll(T5, T4, 4), addu(T5, T5, T4),
            sll(T5, T5, 4), addu(T5, A2, T5)]
    adjust = eboot.code([*last, 0, addiu(A0, T5, 80)])
    eboot.redirect(adjust + 24, 0x088DB134)
    update = eboot.clone(0x0881B60C, 0x0881B6CC, calls={0x088DB134: adjust})
    exit_ = eboot.code([
        lw(T4, 8, A0), lw(T5, 12, A0), sll(T5, T5, 2), addu(T4, T4, T5),
        lw(T4, 0, T4), lw(T4, 80, T4), lw(A2, 4, T4),
        *last, jr(), iword(40, T5, Z, 180),
    ])
    return update, 0x0881B6CC, exit_


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
