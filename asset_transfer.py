"""Import native KGF resources and exchange POM textures as PNG."""

from pathlib import Path
import struct

from formats import anm_header, decode_anm, u32
from iso_save import new_output
from model_retarget import topology
from kgf_readers.mdl_reader import read_mdl
from kgf_readers.pom_to_png import render_pom_png
from kgf_readers.pom_to_tm2 import (build_tim2_from_png_for_pom, get_pom_layout,
                                   replace_pom_texture_with_tim2)
from kgf_readers.texture_fit import fit_texture
from pom_palette import recolor_palette


ASSET_MAGIC = {'mdl': b'MDL\0', 'pom': b'POM\0', 'anm': b'ANM\0'}


def write_asset(path, extension, data):
    with new_output(path, extension) as temporary:
        temporary.write_bytes(data)
        if temporary.read_bytes() != data:
            raise ValueError('资源导出读回校验失败。')
    return {'name': Path(path).name, 'path': str(Path(path).resolve()), 'size': len(data)}


def compatible_model(original, replacement):
    model = read_mdl(replacement)
    base = read_mdl(original)
    # Native action, hand and collision tables address these nodes by index.
    if topology(model) != topology(base):
        raise ValueError('MDL 骨骼编号或层级与目标形态不兼容，请保留目标骨架后导出。')
    if any(0 in node['scale'] for node in model['bones']):
        raise ValueError('MDL 有缩放为零的骨骼，无法计算蒙皮矩阵。')
    return model


def pom_layout(data):
    layout = get_pom_layout(data)
    if layout['rule'] or layout['total_size'] > len(data):
        raise ValueError('请选择像素与色表完整的 PSP POM。')
    return layout


def check_palette(layout, palette):
    if type(palette) is not int or not 0 <= palette < layout['palette_count']:
        raise ValueError('请选择现有色表。')


def texture_png(data, palette):
    check_palette(pom_layout(data), palette)
    return render_pom_png(data, auto_color_key=False, palette_index=palette)[2]


def replace_texture(original, current, data, extension, palette, palette_only=False):
    if palette_only:
        if extension != '.png':
            raise ValueError('只换色表请选择 PNG 文件。')
        return recolor_palette(current, pom_layout(current), data, palette)
    if extension == '.png':
        layout = pom_layout(current)
        check_palette(layout, palette)
        if not data.startswith(b'\x89PNG\r\n\x1a\n') or len(data) < 24:
            raise ValueError('请选择 PNG 文件。')
        if struct.unpack_from('>2I', data, 16) != (layout['width'], layout['height']):
            raise ValueError(f"PNG 尺寸须为 {layout['width']} × {layout['height']}。")
        tim2 = build_tim2_from_png_for_pom(current, data, palette)
        return replace_pom_texture_with_tim2(current, tim2, palette)

    if extension != '.pom':
        raise ValueError('请选择 .pom 或 .png 文件。')
    source = pom_layout(data)
    if pom_layout(original)['pad2'] and not source['pad2']:
        # A target that reads from RAM has no VRAM reservation to reuse.
        donor = bytearray(data)
        struct.pack_into('<H', donor, 14, 1)
        data = bytes(donor)
    return fit_texture(original, data)[0]


def clip_ids(clip):
    _, _, count, _, _ = anm_header(clip)
    return set(clip[52:52 + count * 14:14])


def validate_clip(clip, allowed_ids):
    control, _, count, _, _ = anm_header(clip)
    end = u32(clip, 24)
    if len(clip) % 16 or u32(clip, 4) != len(clip) or not control + 16 <= end == u32(clip, 28) <= len(clip):
        raise ValueError('请选择完整的单个 PSP ANM 片段。')
    ids = clip_ids(clip)
    if len(ids) != count or 0 not in ids or any(i >= 156 for i in ids):
        raise ValueError('ANM 的逻辑轨道编号无效。')
    missing = ids - allowed_ids
    if missing:
        listing = '、'.join(str(i) for i in sorted(missing))
        raise ValueError(f'目标角色的骨架未使用这些逻辑轨道：{listing}。')
    decode_anm(clip)
