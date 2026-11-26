"""Convert the indexed FE3 textures used by a fighter to native PSP POMs."""

from collections import defaultdict
import math
import struct

import numpy as np

from ogeditor.psp.appearance import MODEL_ROLES, palette_modes
from ogeditor.ps2.formats import png_for_pom, require
from kgf_readers.pom_to_tm2 import (active_palette_for_pom, decode_png, display_palette_from_pom,
                                   extract_pom_texture_data, pack_4bpp_indices, tile_psp_16x8_pixels,
                                   tile_psp_32x8_4bpp_pixels, uses_psp_16x8_tiling, uses_psp_32x8_4bpp_tiling)


def palette(raw, index):
    layout, _, colors = extract_pom_texture_data(raw)
    return display_palette_from_pom(layout, active_palette_for_pom(layout, colors, index))


def editable_texture(game, member, slot, textures):
    if slot >= len(textures):
        return False
    texture = textures[slot]['id']
    if any(game.form_binding(*choice)[1]['mdl']['id'] != member for choice in game.pom_forms[texture]):
        return False
    for choice in game.model_forms[member]:
        poms = game.form_binding(*choice)[3]
        if slot >= len(poms) or poms[slot]['id'] != texture:
            return False
    return True


def retained_slots(rig):
    return {rig.model['materials'][index]['texture'] for mesh in rig.model['meshes']
            if mesh['owner'] in rig.shadow | rig.parts for index, _ in mesh['faces']}


def indexed_textures(game, model):
    images, materials = {}, {}
    for material, desc in enumerate(model['materials']):
        tex = model['textures'][desc['texture']]
        key = tex['resource'], tex['member']
        raw = game.data(key)
        png = decode_png(png_for_pom(raw, tex['palette']))
        require(png.indices is not None, '奥3模型贴图缺少索引面。')
        if key not in images:
            images[key] = np.frombuffer(png.indices, dtype=np.uint8).reshape(png.height, png.width)
        colors = palette(raw, tex['palette'])
        blend = desc['gs']['alpha']
        # A GS DECAL/opaque primitive uses RGB without alpha modulation.
        if not desc['gs']['tcc']:
            adjusted = bytearray(colors)
            adjusted[3::4] = bytes((255 if blend else 128,)) * (len(colors) // 4)
            colors = bytes(adjusted)
        elif blend:
            adjusted = bytearray(colors)
            adjusted[3::4] = bytes(min(255, 2 * alpha) for alpha in adjusted[3::4])
            colors = bytes(adjusted)
        materials[material] = key, colors
    return images, materials


def used_uvs(model):
    result = defaultdict(list)
    for mesh in model['meshes']:
        coords = np.asarray(mesh['attributes']).reshape(-1, 9)[:, 3:5]
        for material, corners in mesh['batches']:
            if corners:
                result[material].append(coords[corners])
    return {key: np.concatenate(coords) for key, coords in result.items()}


def sampled_region(image, uvs):
    size = np.array(image.shape[::-1])
    left, top = np.floor(uvs.min(axis=0) * size - 1).astype(int)
    right, bottom = np.ceil(uvs.max(axis=0) * size + 1).astype(int)
    width, height = int(right - left), int(bottom - top)
    require(0 < width <= 2048 and 0 < height <= 2048, '奥3贴图取样范围超出可转换尺寸。')
    x = np.arange(left, right).clip(0, size[0] - 1)
    y = np.arange(top, bottom).clip(0, size[1] - 1)
    return image[y[:, None], x], size, np.array((left, top))


def resized_indices(image, width, height):
    x = ((np.arange(width) + .5) * image.shape[1] / width).astype(int)
    y = ((np.arange(height) + .5) * image.shape[0] / height).astype(int)
    return image[y[:, None], x]


def place_tiles(tiles):
    sizes = sorted(((w, h) for w in (64, 128, 256, 512) for h in (64, 128, 256, 512)),
                   key=lambda wh: (wh[0] * wh[1], max(wh), wh))
    for divisor in (1, 2, 4, 8):
        dims = {key:(max(2, math.ceil(tile.shape[1] / (divisor * math.ceil(tile.shape[1] / 512)))),
                     max(2, math.ceil(tile.shape[0] / (divisor * math.ceil(tile.shape[0] / 512)))))
                for key, (tile, _, _) in tiles.items()}
        order = sorted(dims, key=lambda key: (-dims[key][1], -dims[key][0], key))
        for aw, ah in sizes:
            x = y = row = 0
            result = {}
            for key in order:
                width, height = dims[key]
                if x + width > aw:
                    y += row
                    x = row = 0
                if x + width > aw or y + height > ah:
                    break
                result[key] = x, y, width, height
                x += width
                row = max(row, height)
            else:
                return aw, ah, result
    raise ValueError('奥3角色的贴图无法放入 PSP 图集。')


def remap_uvs(region, rect, canvas):
    tile, source_size, low = region
    x, y, width, height = rect
    factor = np.array((width, height)) / np.array(tile.shape[:2][::-1])
    return source_size * factor / canvas, (np.array((x, y)) - low * factor) / canvas


def substitute_clut(raw, index, colors):
    layout, _, _ = extract_pom_texture_data(raw)
    block = layout['palette_block_size']
    require(len(colors) <= block, '奥3灯件色表超过原生接收容量。')
    at = layout['palette_offset'] + index * layout['palette_stride']
    return raw[:at] + colors + raw[at + len(colors):]


def boot_pom(raw, pixels, cluts=None):
    """Write the EBOOT 088E7490/088E7A50 RAM path when the atlas grows."""
    layout, _, _ = extract_pom_texture_data(raw)
    height, width = pixels.shape
    colors = layout['color_entries']
    if cluts is None:
        require((width, height) == (layout['width'], layout['height']), '原生灯件贴图尺寸不一致。')
        image = pack_4bpp_indices(pixels.tobytes()) if colors == 16 else pixels.tobytes()
        if uses_psp_16x8_tiling(layout):
            image = tile_psp_16x8_pixels(image, width, height)
        elif uses_psp_32x8_4bpp_tiling(layout):
            image = tile_psp_32x8_4bpp_pixels(image, width, height)
        start = layout['image_offset']
        return raw[:start] + image + raw[start + len(image):]
    require(colors == 256, '目标主体贴图需要使用原生 8 位索引。')
    texture = tile_psp_16x8_pixels(pixels.tobytes(), width, height)
    out = bytearray(raw[:64])
    struct.pack_into('<IIHHI', out, 4, height, width, 8, 1, 0)
    struct.pack_into('<4H', out, 24, 256, len(cluts) - 1, len(texture) // 256, 4)
    register, = struct.unpack_from('<Q', raw, 32)
    mask = (15 << 26) | (15 << 30) | (63 << 14)
    register = (register & ~mask) | ((width.bit_length() - 1) << 26) | ((height.bit_length() - 1) << 30)
    register |= max(1, width // 64) << 14
    struct.pack_into('<Q', out, 32, register)
    # PSP uses direct CLUT order. The legacy PS2 permutation was undone above.
    out.extend(b''.join(colors.ljust(1024, b'\0') for colors in cluts))
    out.extend(texture)
    return bytes(out)


def make_atlas(raw, image, materials, uvs, body):
    per_texture = defaultdict(list)
    for mat, coords in uvs.items():
        per_texture[materials[mat][0]].append(coords)
    require(bool(per_texture), '来源模型没有可转换的有贴图三角面。')
    tiles = {key:sampled_region(image[key], np.concatenate(coords)) for key, coords in per_texture.items()}
    width, height, rects = place_tiles(tiles)
    canvas = np.zeros((height, width), dtype=np.uint8)
    for key, (x, y, w, h) in rects.items():
        canvas[y:y + h, x:x + w] = resized_indices(tiles[key][0], w, h)

    body_colors = materials[body][1]
    cluts = body_palettes(raw, body_colors)
    slots = {body_colors:0}
    transforms = {}
    for mat in uvs:
        key, colors = materials[mat]
        if colors not in slots:
            slots[colors] = len(cluts)
            cluts.append(colors)
        transforms[mat] = slots[colors], *remap_uvs(tiles[key], rects[key], (width, height))
    return boot_pom(raw, canvas, cluts), transforms


def body_palettes(raw, body_colors):
    layout, _, _ = extract_pom_texture_data(raw)
    stock = np.frombuffer(palette(raw, 0), dtype=np.uint8).reshape(-1, 4)
    incoming = np.frombuffer(body_colors, dtype=np.uint8).reshape(-1, 4)
    delta = incoming[:, None, :3].astype(np.int32) - stock[:, :3]
    nearest = np.argmin(np.sum(delta * delta, axis=2), axis=1)
    cluts = [body_colors]
    # The EBOOT stores the stock bronze/gold body modes in the first slots.
    for variant in range(1, layout['palette_count']):
        old = np.frombuffer(palette(raw, variant), dtype=np.uint8).reshape(-1, 4)
        colors = old[nearest].copy()
        colors[:, 3] = incoming[:, 3]
        cluts.append(colors.tobytes())
    return cluts


def make_lamp(raw, image, uvs):
    layout, _, _ = extract_pom_texture_data(raw)
    width, height = layout['width'], layout['height']
    region = sampled_region(image, uvs)
    require(int(region[0].max()) < layout['color_entries'], '灯件的像素索引超过接收色表范围。')
    canvas = resized_indices(region[0], width, height)
    return boot_pom(raw, canvas), remap_uvs(region, (0, 0, width, height), (width, height))


def lamp_slots(game, case, rig, selection, member, textures, base_address):
    if rig.timer is None:
        return None
    slot, variant = rig.timer
    role = selection[0] if member == case['forms'][0]['mdl']['id'] else MODEL_ROLES.get(member)
    mode = next((rows for key, _, _, rows, _ in palette_modes(game.image, base_address, role)
                 if key == 'normal'), {})
    backup, normal = mode.get(rig.timer, rig.timer)
    used = retained_slots(rig)
    if not (slot > 0 and backup > 0 and slot not in used and backup not in used
            and editable_texture(game, member, slot, textures)
            and editable_texture(game, member, backup, textures)):
        return None
    texture, carrier = textures[slot]['id'], textures[backup]['id']
    raw, original = (game.read_source_member(item, b'POM\0') for item in (texture, carrier))
    expected, _, _ = extract_pom_texture_data(raw)
    saved, _, _ = extract_pom_texture_data(original)
    if (variant >= expected['palette_count'] or normal >= saved['palette_count']
            or expected['palette_block_size'] > saved['palette_block_size']):
        return None
    return slot, variant, normal, texture, carrier, raw, original, expected


def lamp_palettes(binding, pixels, colors):
    _, variant, normal, texture, carrier, _, original, expected = binding
    require(len(colors) == expected['palette_block_size'], '灯件的色表长度与 EBOOT 拷贝长度不同。')
    if texture == carrier:
        return {texture:substitute_clut(substitute_clut(pixels, normal, colors), variant, colors)}
    return {texture:substitute_clut(pixels, variant, colors),
            carrier:substitute_clut(original, normal, colors)}
