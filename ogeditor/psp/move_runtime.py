"""Native listeners whose data must follow the recipient or borrowed move."""

import struct

from formats import s16, u32
from move_eboot import (A0, A2, BASE, PTR, T4, T5, T6, T7, V0, V1, Z, addiu, addu,
                        branch, iword, lw, rword)


EXPLICIT_CAMERA_SLOT = 0x4000


def keep_fighter_portraits(eboot, image, descriptors):
    if not descriptors:
        return
    rows = sorted({int(case['counts_va'], 16) for case, _ in descriptors.values()})
    mapping = eboot.allocate(bytes(8 * (len(rows) + 1)))
    for index, counts in enumerate(rows):
        eboot.linked(mapping + 8 * index, counts, PTR)
        eboot.word(mapping + 8 * index + 4, s16(image, counts - BASE + 8))
    # 08864ACC uses the last common texture as the portrait and the penultimate
    # one for Eleking's left side. Its branch delay slot already loaded the count.
    shim = eboot.code([
        0, 0, lw(T5, 0, T4), branch(4, T5, Z, 6), 0,
        branch(4, T5, V1, 3), 0,
        branch(4, Z, Z, -6), addiu(T4, T4, 8),
        lw(V0, 4, T4),
        branch(4, 16, Z, 3), 0, 0, 0, 0, 0,
    ])
    eboot.absolute(shim, T4, mapping)
    eboot.redirect(shim + 48, 0x08864C28)
    eboot.redirect(shim + 56, 0x08864B24)
    eboot.redirect(0x08864B1C, shim)


def install_cameras(eboot, image, banks, bindings):
    if not bindings:
        return
    for addr, (case, slots) in banks.items():
        length = int(case['counts_va'], 16) + 10
        n = s16(image, length - BASE)
        original = u32(image, addr + 16 - BASE) - BASE
        pointer = eboot.allocate(image[original:original + 2 * n] + struct.pack(f'<{len(slots)}H', *slots))
        eboot.linked(addr + 16, pointer, PTR)
        eboot.half(length, n + len(slots))
    table = eboot.allocate(bytes(16 * (len(bindings) + 1)))
    for index, ((descriptor, state), (first, count)) in enumerate(sorted(bindings.items())):
        row = table + 16 * index
        eboot.linked(row, descriptor, PTR)
        eboot.write(row + 4, struct.pack('<3I', state, first, count))
    # The close/far camera uses four slots, the paired camera two.
    route_camera_request(eboot, 0x0889A204, table, 3)
    route_camera_request(eboot, 0x0889A320, table, 1)


def route_camera_request(eboot, entry, table, last_index):
    _, _, offset = eboot.locate(entry)
    first, second = struct.unpack_from('<2I', eboot.raw, offset)
    if any(eboot.relocs.get(entry + x) for x in (0, 4)):
        raise ValueError('镜头入口与奥0的重定位不匹配。')
    # a2 names the source's first key. The native entry still computes
    # the player side and variant.
    helper = eboot.code([
        iword(12, A2, T4, EXPLICIT_CAMERA_SLOT), branch(4, T4, Z, 4), 0,
        iword(12, A2, A2, 255), branch(4, Z, Z, 23), 0,
        lw(T4, 80, A0), lw(T4, 0, T4), lw(T4, 36, T4),
        lw(T5, 100, A0), lw(T5, 20, T5),
        0, 0,
        lw(T7, 0, T6), branch(4, T7, Z, 13), 0,
        branch(5, T7, T4, 9), lw(T7, 4, T6),
        branch(5, T7, T5, 7), lw(V0, 12, T6),
        addiu(V1, A2, last_index), rword(V1, V0, V1, 0x2B),
        branch(4, V1, Z, 5), lw(T7, 8, T6),
        branch(4, Z, Z, 3), addu(A2, A2, T7),
        branch(4, Z, Z, -14), addiu(T6, T6, 16),
        first, 0, second,
    ])
    eboot.absolute(helper + 44, T6, table)
    eboot.redirect(helper + 116, entry + 8)
    eboot.redirect(entry, helper)
    eboot.word(entry + 4, 0)

def graft_locator(raw, origin, parent):
    # A geometry-free child preserves the source grip's local position and rotation.
    count = struct.unpack_from('<H', raw, 14)[0]
    bones, start, end = struct.unpack_from('<3I', raw, 20)
    if bones + 64 * count != start or not 0 <= parent < count:
        raise ValueError('模型节点表无法添加配招挂点。')
    out = bytearray(raw[:start] + origin + raw[start:end])
    for node in range(count):
        row = bones + node * 64
        struct.pack_into('<I', out, row + 60, u32(out, row + 60) + 64)
    child = bones + parent * 64 + 54
    sibling = s16(out, child)
    struct.pack_into('<h', out, child, count)
    struct.pack_into('<HhhhII', out, start + 48, 0, parent, sibling, -1, 0xFFFFFFFF, len(out))
    struct.pack_into('<H', out, 14, count + 1)
    struct.pack_into('<I', out, 4, len(out) + 8)
    struct.pack_into('<2I', out, 24, start + 64, len(out))
    return count, bytes(out)
