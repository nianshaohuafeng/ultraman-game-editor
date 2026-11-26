"""Apply a PNG recolor to one native CLUT, retaining indices and game alpha."""

from ogeditor.readers import render_pom_png
from kgf_readers.pom_to_tm2 import decode_png, display_palette_from_pom, storage_palette_for_pom


def recolor_palette(pom, layout, png, palette):
    if type(palette) is not int or not 0 <= palette < layout['palette_count']:
        raise ValueError('请选择现有色表。')
    incoming = decode_png(png)
    if (incoming.width, incoming.height) != (layout['width'], layout['height']):
        raise ValueError(f"PNG 尺寸须为 {layout['width']} × {layout['height']}。")
    start = layout['palette_offset'] + palette * layout['palette_stride']
    end = start + layout['palette_block_size']
    colors = bytearray(display_palette_from_pom(layout, pom[start:end]))
    indexed = decode_png(render_pom_png(pom, auto_color_key=False, palette_index=palette)[2])
    if incoming.indices == indexed.indices:
        changes = {idx: bytes(color[:3]) for idx, color in enumerate(incoming.palette[:len(colors) // 4])}
    else:
        # Photoshop may renumber an indexed image or save it as RGB/RGBA.
        changes = {}
        for pixel, idx in enumerate(indexed.indices):
            if colors[4 * idx + 3] == 0:
                continue
            rgb = incoming.rgba[pixel * 4:pixel * 4 + 3]
            if changes.setdefault(idx, rgb) != rgb:
                raise ValueError(f'原色号 {idx} 在 PNG 中对应多个颜色。请统一该色号，或使用整图导入。')
    for idx, rgb in changes.items():
        colors[4 * idx:4 * idx + 3] = rgb
    return pom[:start] + storage_palette_for_pom(layout, bytes(colors)) + pom[end:]
