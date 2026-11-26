"""Material and item bindings for the v7 fighter model loader."""

from formats import s16, u32


APPEARANCE_TABLE = 0x089DE71C
MODE_NAMES = ((0, 'normal', '原始'), (4, 'gold', '金色'), (3, 'bronze', '铜像'), (5, 'rage', '暴走'))
TIMER_ENTRIES = {0x0889A9A8, 0x0889AC08, 0x0889AE88, 0x0889B0F0}


def color_timer(image, base, role):
    if role is not None:
        # Mode 1 is entered by 0889CB04 on low health or time. Its
        # enter/update callbacks copy the chest or forehead lamp CLUT.
        callback = u32(image, APPEARANCE_TABLE - base + 96 * role + 16)
        if callback in TIMER_ENTRIES:
            return 1, 1
    return None

# 0889CB04 dispatches these enter callbacks. Each copy is (destination slot,
# destination palette, source slot, source palette) at 0889C888.
PALETTE_COPIES = {
    0x0889A93C: ((0, 0, 0, 2), (1, 0, 3, 4), (1, 1, 3, 0)),
    0x0889AB84: ((0, 0, 0, 2), (1, 0, 3, 4), (1, 1, 3, 0), (1, 2, 3, 7)),
    0x0889AE04: ((0, 0, 0, 2), (1, 0, 4, 4), (1, 1, 4, 0), (1, 2, 4, 7)),
    0x0889B084: ((0, 0, 0, 2), (1, 0, 2, 4), (1, 1, 2, 0)),
    0x0889B3F0: ((0, 0, 0, 3), (1, 0, 2, 0)),
    0x0889B85C: ((0, 0, 0, 3), (1, 0, 2, 0)),
    0x0889B9F4: ((0, 0, 0, 3),),
    0x0889BCF8: ((0, 0, 0, 3), (1, 0, 2, 0)),
    0x0889BE48: ((0, 0, 0, 3), (1, 0, 2, 0)),
    0x0889BF98: ((0, 0, 0, 3), (1, 0, 2, 0), (1, 1, 2, 5), (1, 2, 2, 9)),
    0x0889C220: ((0, 0, 0, 3), (1, 0, 2, 0), (1, 1, 2, 4)),
    0x0889C554: ((0, 0, 0, 3), (1, 0, 2, 0), (1, 1, 2, 8)),
    0x0889AAF8: ((0, 0, 0, 1), (1, 0, 3, 8), (1, 1, 3, 7)),
    0x0889AD88: ((0, 0, 0, 1), (1, 0, 3, 11), (1, 1, 3, 10), (1, 2, 3, 12)),
    0x0889B008: ((0, 0, 0, 1), (1, 0, 4, 11), (1, 1, 4, 10), (1, 2, 4, 12)),
    0x0889B240: ((0, 0, 0, 1), (1, 0, 2, 8), (1, 1, 2, 7)),
    0x0889B53C: ((0, 0, 0, 1), (1, 0, 2, 2)),
    0x0889B8FC: ((0, 0, 0, 1), (1, 0, 2, 2)),
    0x0889BB5C: ((0, 0, 0, 1),),
    0x0889BD98: ((0, 0, 0, 1), (1, 0, 2, 2)),
    0x0889BEE8: ((0, 0, 0, 1), (1, 0, 2, 2)),
    0x0889C098: ((0, 0, 0, 1), (1, 0, 2, 2), (1, 1, 2, 7), (1, 2, 2, 11)),
    0x0889C2F0: ((0, 0, 0, 1), (1, 0, 2, 2), (1, 1, 2, 8)),
    0x0889C624: ((0, 0, 0, 1), (1, 0, 2, 6), (1, 1, 2, 9)),
    0x0889AB5C: ((0, 0, 0, 3),),
    0x0889B588: ((0, 0, 0, 2), (1, 0, 2, 0)),
    0x0889B948: ((0, 0, 0, 2), (1, 0, 2, 3)),
    0x0889BB84: ((0, 0, 0, 2),),
    0x0889BDE4: ((0, 0, 0, 2),),
    0x0889BF34: ((0, 0, 0, 2),),
    0x0889C114: ((0, 0, 0, 2), (1, 0, 2, 3), (1, 1, 2, 5), (1, 2, 2, 9)),
    0x0889C354: ((0, 0, 0, 2), (1, 0, 2, 0), (1, 1, 2, 4)),
    0x0889C688: ((0, 0, 0, 2), (1, 0, 2, 0), (1, 1, 2, 12)),
    0x0889B2A4: ((0, 0, 0, 2), (1, 0, 2, 4), (1, 1, 2, 9)),
    0x0889B6D4: ((0, 0, 0, 3), (1, 0, 2, 3)),
    0x0889C430: ((0, 0, 0, 3), (1, 0, 2, 10), (1, 1, 2, 10)),
    0x0889C764: ((0, 0, 0, 3), (1, 0, 2, 0), (1, 1, 2, 8)),
}

# Mode 5 updates copy a blend of four palettes in slot 2 to the eye/lamp slot.
RAGE_LAMPS = {
    0x0889B308: (9, (0, 1)),
    0x0889B720: (3, (0,)),
    0x0889C494: (10, (0, 1)),
    0x0889C7C8: (2, (0, 1)),
}

# Stock stand-ins reuse byte-identical MDLs from another roster entry.
MODEL_ROLES = {
    101: 6, 139: 3, 240: 6, 291: 7, 328: 3, 423: 6, 464: 3, 555: 6, 594: 4,
    687: 6, 727: 3, 829: 1, 874: 3, 972: 7, 2026: 7, 2114: 10, 2128: 3,
    2165: 10, 2179: 4,
}


def palette_modes(image, base, role):
    if role is None:
        return
    table = APPEARANCE_TABLE - base + 96 * role
    # 089DED7C saves the normal body palette in a slot reused by later modes.
    backup = {(0, 2 if role <= 8 else 3): (0, 0)}
    normal = {}
    for mode, key, name in MODE_NAMES:
        callback = u32(image, table + 16 * mode)
        copies = PALETTE_COPIES.get(callback)
        if copies is None:
            continue
        palettes = dict(normal)
        for slot, palette, source, source_palette in copies:
            palettes[slot, palette] = backup.get((source, source_palette), (source, source_palette))
        if mode == 0:
            normal = palettes
        lamps = RAGE_LAMPS.get(u32(image, table + 16 * mode + 8)) if mode == 5 else None
        yield key, name, callback, palettes, lamps


def item_models(image, base, case, group):
    # 0888BC90 walks the accessory MDL and POM lists before 088894C0 loads them.
    desc = int(case['descriptor_va'], 16) - base
    counts = int(case['counts_va'], 16) - base
    models = u32(image, desc + 4) - base
    pom_lists = u32(image, desc + 8) - base
    pom_counts = u32(image, counts + 4) - base
    _, _, group_count, first = group
    shared = [texture['id'] for texture in case['forms'][0]['pom_slots']]
    for index in range(s16(image, counts)):
        slot = s16(image, models + 2 * index)
        if not 0 <= slot < group_count:
            continue
        pointer = u32(image, pom_lists + 4 * index) - base
        textures = [s16(image, pointer + 2 * n) for n in range(s16(image, pom_counts + 2 * index))]
        if textures == [-0x1C0]:  # FE40 reuses the base form's POM table.
            members = shared
        else:
            members = [first + value if 0 <= value < group_count else None for value in textures]
        yield {'slot': index, 'member': first + slot, 'poms': members}
