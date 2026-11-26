"""Connect FE3 finisher candidates to their native menu and loading artwork."""

import struct

from ogeditor.ps2.expand import ElfSpace, HEADER, LOAD_PREFIX, prefix_records, relocated_halfwords
from ogeditor.ps2.formats import paste_png, png_for_pom, require
from ogeditor.ps2.names import atlas_name, atlas_rows
from ogeditor.ps2.native import NativeTables, rating_address
from kgf_readers.pom_to_png import build_png_indexed
from kgf_readers.pom_to_tm2 import decode_png
from move_eboot import A0, RA, T4, T5, T6, T7, V0, Z, addiu, addu, branch, iword, jr, lw, rword, sll, sw


TITLE_HEIGHT = 14
NAME_ROWS = 256 // TITLE_HEIGHT
STOCK_NAME_ROWS = 17
LOADING_NAME_RECTANGLES = 0x3D6EB0
LOADING_RECT_POINTERS = (0x3D707C, 0x3D7084, 0x3D708C)
INTRO_GROUP = 0x0B84
INTRO_RECTANGLES = 0x3C94D0
INTRO_POINTER = 0x7C
INTRO_LOAD_CALLS = (0x23F270, 0x24326C)
INTRO_LOADER = 0x23FD68
INTRO_DRAW = 0x2432BC
INTRO_RESUME = 0x2432C4
NO_LINK = 0xFFFF


def transplant_name(source, index, target, slot):
    src = decode_png(png_for_pom(source, 0))
    dst = decode_png(png_for_pom(target, 0))
    require(0 <= index < NAME_ROWS and 0 <= slot < NAME_ROWS and src.indices is not None
            and dst.indices is not None and src.width == dst.width == 256
            and TITLE_HEIGHT * (index + 1) <= src.height and TITLE_HEIGHT * (slot + 1) <= dst.height,
            '必杀名称超出两种原生名称图的绘制范围。')
    step = TITLE_HEIGHT * dst.width
    used = src.indices[index * step:(index + 1) * step]
    require(all(src.palette[i] in dst.palette for i in set(used)), '来源名称使用目标 POM 无法表示的颜色。')
    mapping = {i: dst.palette.index(src.palette[i]) for i in set(used)}
    pixels = dst.indices[:slot * step] + bytes(mapping[i] for i in used) + dst.indices[(slot + 1) * step:]
    if pixels == dst.indices:
        return target
    return paste_png(target, build_png_indexed(dst.width, dst.height, pixels, dst.palette), 0, False)


def description_links(elf, records=None):
    records = prefix_records(elf) if records is None else records
    if records is None:
        return {}, None
    address, = struct.unpack_from('<I', elf, INTRO_POINTER)
    if address == 0:
        return {}, None
    start = address - LOAD_PREFIX
    end = records[0]
    require(HEADER + 12 <= start <= end - 4 and not start % 4, '奥3必杀介绍索引无效。')
    result = {}
    for pos in range(start, end - 3, 4):
        tile, candidate_key = struct.unpack_from('<HH', elf, pos)
        if candidate_key == NO_LINK:
            return result, address
        role, candidate = divmod(candidate_key, 32)
        require(role < 39 and candidate < NAME_ROWS and (role, candidate) not in result,
                '奥3必杀介绍索引包含重复或越界记录。')
        result[role, candidate] = divmod(tile, 4)
    raise ValueError('奥3必杀介绍索引缺少结束记录。')


def stage_finisher_ui(game, plan, role, candidate, donor, source_candidate, label):
    tables = NativeTables(game.elf)
    name_source = game.original(tables.skill_name_source(donor))
    # CSelect and CCustom use the FUM child. CLoading reads a second,
    # standalone POM with the same 14-pixel rectangles.
    for key in (tables.skill_name_source(role), tables.skill_loading_source(role)):
        base = plan.resources.get(key, game.original(key))
        new = transplant_name(name_source, source_candidate, base, candidate)
        if new != base:
            plan.resources[key] = new

    prior, _ = description_links(game.elf, game.extension)
    source = donor, source_candidate
    member, strip = prior[source] if source in prior else tables.skill_description_source(*source)
    part = game.part(INTRO_GROUP, member)
    require(part.kind == 'pom' and part.compressed and 0 <= strip < 4, '来源必杀没有可用的原生介绍图块。')
    plan.descriptions[role, candidate] = (member, strip)
    plan.names[role, candidate] = atlas_name(atlas_rows(name_source), donor, source_candidate, label)
    plan.ratings[role, candidate] = tables.finisher_rating(donor, source_candidate)


def jump(address, call=False):
    return (0x0C000000 if call else 0x08000000) | (address >> 2)


def install_last_name_rectangle(space):
    """The atlas can fit one more name than the three original rectangle tables."""
    tables = NativeTables(space.elf)
    original = tables.at(LOADING_NAME_RECTANGLES, f'<{6 * STOCK_NAME_ROWS}I')
    extended = (*original, 0, TITLE_HEIGHT * STOCK_NAME_ROWS, 256, TITLE_HEIGHT, 0, 0)
    pointers = [tables.words(va, 1)[0] for va in LOADING_RECT_POINTERS]
    if pointers[0] != LOADING_NAME_RECTANGLES:
        require(len(set(pointers)) == 1 and tables.words(pointers[0], len(extended)) == extended,
                '奥3扩展名称矩形表无效。')
        return
    require(len(set(pointers)) == 1
            and all(tables.words(va, len(original)) == original for va in (0x3C9098, 0x3D0408)),
            '奥3三处名称矩形的布局不一致。')
    address = space.allocate(struct.pack(f'<{len(extended)}I', *extended))
    hi, lo = relocated_halfwords(address)
    # CCustom name rows and CSelect equipped summaries load the same UV data.
    pairs = [
        (0x2422D4, 0x2422D8, 0x3C0F003D, 0x25EF9098),
        (0x242380, 0x242384, 0x3C0F003D, 0x25EF9098),
        (0x2423D0, 0x2423D4, 0x3C0F003D, 0x25EF9098),
        (0x242C14, 0x242C1C, 0x3C0E003D, 0x25D79098),
        (0x2670FC, 0x267104, 0x3C0C003D, 0x25870408),
        (0x267348, 0x26734C, 0x3C0F003D, 0x25EF0408),
    ]
    for upper, lower, stock_upper, stock_lower in pairs:
        require(tables.words(upper, 1)[0] == stock_upper and tables.words(lower, 1)[0] == stock_lower,
                '奥3名称矩形的读取入口与当前 ELF 不符。')
        space.patch_word(upper, (stock_upper & 0xFFFF0000) | hi)
        space.patch_word(lower, (stock_lower & 0xFFFF0000) | lo)
    # This call shares the caller's s7 with other data. Change only a3.
    require(tables.words(0x242104, 2) == (jump(0x23FF70, call=True), 0x26E79098),
            '奥3名称矩形初始化入口与当前 ELF 不符。')
    a3 = 7
    shim = space.allocate(struct.pack('<4I',
        iword(15, Z, a3, hi), addiu(a3, a3, lo), jump(0x23FF70), 0))
    space.patch_word(0x242104, jump(shim, call=True))
    for va in LOADING_RECT_POINTERS:
        space.patch_word(va, address)


def install_description_hooks(space):
    tables = NativeTables(space.elf)
    require(all(tables.words(site, 1)[0] == jump(INTRO_LOADER, call=True) for site in INTRO_LOAD_CALLS)
            and tables.words(INTRO_DRAW, 2) == (0x01CF7021, 0xAE0E3754),
            '奥3必杀介绍的原生入口与当前 ELF 不符。')
    # Enter with a0 = CCustom. Return member*4+rect in v0, or -1 for the
    # original CCustom lookup. Keep t6 (stock rectangle) and t1 (stock child).
    t8, t9, t1, s0 = 24, 25, 9, 16
    hi, lo = relocated_halfwords(LOAD_PREFIX + INTRO_POINTER)
    resolve = space.allocate(struct.pack('<17I',
        lw(T4, 0x6C, A0), lw(T5, 0x94, A0),
        branch(1, T5, Z, 12), sll(T4, T4, 5),            # Negative cursor → miss.
        rword(T4, T5, T4, 0x25),                        # role * 32 + candidate
        iword(15, Z, T5, hi), lw(T5, lo, T5), iword(13, Z, t9, NO_LINK),
        iword(37, T5, T7, 2),                          # loop: read key
        branch(4, T7, t9, 5), 0,                       # End of table → miss.
        branch(5, T7, T4, -4), addiu(T5, T5, 4),        # Next record or match.
        jr(), iword(37, T5, V0, -4),
        jr(), addiu(V0, Z, -1),                        # miss
    ))
    # Tail-call the original loader with its arguments and return address.
    loader = space.allocate(struct.pack('<9I',
        addu(t8, RA, Z), jump(resolve, call=True), 0, addu(RA, t8, Z),
        branch(1, V0, Z, 2), 0, rword(Z, V0, t1, 2, 2),
        jump(INTRO_LOADER), 0))
    rect_hi, rect_lo = relocated_halfwords(INTRO_RECTANGLES)
    rectangle = space.allocate(struct.pack('<12I',
        jump(resolve, call=True), addu(A0, s0, Z),
        branch(1, V0, Z, 7), iword(12, V0, V0, 3),
        sll(T6, V0, 4), sll(T5, V0, 3), addu(T6, T6, T5),
        iword(15, Z, T7, rect_hi), addiu(T7, T7, rect_lo), addu(T6, T6, T7),
        jump(INTRO_RESUME), sw(T6, 0x3754, s0),
    ))
    for site in INTRO_LOAD_CALLS:
        space.patch_word(site, jump(loader, call=True))
    space.patch(INTRO_DRAW, struct.pack('<II', jump(rectangle), addu(T6, T6, T7)))


def install_finisher_ui(game, plan):
    if not plan.descriptions:
        return
    elf = plan.elf if plan.elf is not None else game.elf
    records = prefix_records(elf)
    space = ElfSpace(elf, records)
    old, address = description_links(elf, records)
    links = old | plan.descriptions
    if any(candidate >= STOCK_NAME_ROWS for _, candidate in links):
        install_last_name_rectangle(space)
    if links != old:
        raw = b''.join(struct.pack('<HH', 4 * member + strip, 32 * role + candidate)
                       for (role, candidate), (member, strip) in sorted(links.items()))
        raw += struct.pack('<HH', 0, NO_LINK)
        if address is None:
            install_description_hooks(space)
        if address is not None and len(old) == len(links):
            space.patch(address, raw)
        else:
            address = space.allocate(raw)
            space.patch_file(INTRO_POINTER, struct.pack('<I', address))
    for (role, candidate), value in plan.ratings.items():
        space.patch_word(rating_address(role, candidate), value)
    plan.elf = space.finish()
