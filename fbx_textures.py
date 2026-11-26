"""Bind the FBX's diffuse PNGs to a native PSP texture and preserve the model's UVs."""

from collections import defaultdict
import heapq
from pathlib import Path, PureWindowsPath

import numpy as np

from cross_texture import (body_palettes, boot_pom, editable_texture, lamp_palettes, lamp_slots, place_tiles,
                           remap_uvs, retained_slots)
from fbx_import import joint_track, skin_alignment
from kgf_readers.pom_to_tm2 import decode_png, get_pom_layout


def indexed_palette(rgba, count):
    colors, lookup, frequency = np.unique(rgba.reshape(-1, 4), axis=0, return_inverse=True, return_counts=True)
    clusters = []

    def add(ids):
        samples = colors[ids]
        spans = np.ptp(samples, axis=0).astype(int) * (3, 4, 2, 4)
        axis = int(np.argmax(spans))
        score = 0 if len(ids) == 1 else int(spans[axis]) * np.sqrt(frequency[ids].sum())
        heapq.heappush(clusters, (-score, int(ids[0]), axis, ids))

    add(np.arange(len(colors)))
    while len(clusters) < count and clusters[0][0] < 0:
        _, _, axis, ids = heapq.heappop(clusters)
        order = ids[np.argsort(colors[ids, axis], kind='stable')]
        density = np.cumsum(frequency[order])
        split = min(len(order) - 1, int(np.searchsorted(density, density[-1] / 2)) + 1)
        add(order[:split])
        add(order[split:])
    palette = np.zeros((count, 4), dtype=np.uint8)
    remap = np.empty(len(colors), dtype=np.uint8)
    for i, (_, _, _, ids) in enumerate(clusters):
        palette[i] = np.rint(np.average(colors[ids], axis=0, weights=frequency[ids])).astype(np.uint8)
        remap[ids] = i
    return remap[lookup].reshape(rgba.shape[:2]), palette.tobytes()


def resize_color(rgba, width, height):
    current = rgba.astype(float)
    current[:, :, :3] *= current[:, :, 3:] / 255
    for axis, size in ((0, height), (1, width)):
        length = current.shape[axis]
        if size < length:
            cuts = np.linspace(0, length, size + 1).astype(int)
            spans = np.diff(cuts).reshape((-1, 1, 1) if axis == 0 else (1, -1, 1))
            current = np.add.reduceat(current, cuts[:-1], axis=axis) / spans
        elif size > length:
            indices = (np.arange(size) * length // size).astype(int)
            current = current.take(indices, axis=axis)
    alpha = current[:, :, 3:]
    np.divide(current[:, :, :3] * 255, alpha, out=current[:, :, :3], where=alpha > 0)
    return np.rint(current).clip(0, 255).astype(np.uint8)


def find_diffuse(path, reference):
    name = PureWindowsPath(reference).name
    if Path(name).suffix.lower() != '.png':
        return None
    file = path.parent / name
    return file if file.is_file() else None


def tile_for(png, uv, tint):
    rgba = np.frombuffer(png.rgba, dtype=np.uint8).reshape(png.height, png.width, 4)
    size = np.array((png.width, png.height))
    first = np.floor(uv.min(axis=0) * size - .5).astype(int)
    stop = np.ceil(uv.max(axis=0) * size + .5).astype(int)
    # Each triangle keeps its winding and continuous UVs. Duplicate texels
    # across the repeat seam inside the tile to keep interpolation exact.
    x = np.arange(first[0], stop[0]) % png.width
    y = np.arange(first[1], stop[1]) % png.height
    cropped = rgba[y[:, None], x]
    return np.rint(cropped * np.clip(tint, 0, 1)).astype(np.uint8), size, first


def source_lamp(geometry, skin, rig, rotations):
    if rig.timer is None:
        return None
    fit = skin_alignment(skin, rig, rotations)
    if fit is None or 13 not in rig.tracks or 10 not in rig.tracks:
        return None
    points, _, _, faces, settings = geometry
    transform, offset = fit
    by_material = defaultdict(set)
    for material, corners in faces:
        r, g, b = settings[material]['Kd']
        if b > r + .18 and g > r + .12:
            by_material[material].update(v for v, _, _ in corners)
    neck = rig.centers[rig.tracks[13]]
    chest = rig.centers[rig.tracks[10]]
    height = np.ptp((points @ transform.T)[:, 1])
    candidates = []
    for material, indices in by_material.items():
        sample = points[sorted(indices)] @ transform.T + offset
        center = (sample.min(axis=0) + sample.max(axis=0)) / 2
        if (np.ptp(sample, axis=0).max() < .09 * height
                and abs(center[0] - neck[0]) < .045 * height
                and neck[1] - .17 * height < center[1] < neck[1] + .02 * height
                and center[2] > chest[2] + .025 * height):
            candidates.append((abs(center[1] - chest[1]), material))
    return min(candidates)[1] if candidates else None


def stage_fbx_textures(game, path, geometry, skin, rig, selection, base_address, rotations):
    points, uv, normals, faces, settings = geometry
    has_uv = set()
    for material, corners in faces:
        if all(i is not None for _, i, _ in corners):
            has_uv.add(material)
    used = {item for item, _ in faces}
    files = {}
    missing = set()
    for material in used:
        if not (ref := settings[material].get('diffuse_file')):
            continue
        file = find_diffuse(path, ref)
        if file and material in has_uv:
            files[material] = file
        else:
            missing.add(Path(ref).name)
    if not files:
        return geometry, {}, sorted(missing)
    case, form, _, textures = game.form_binding(*selection)
    member = form['mdl']['id']
    available = []
    for slot, tex in enumerate(textures):
        if slot not in retained_slots(rig) and editable_texture(game, member, slot, textures):
            if get_pom_layout(game.read_source_member(tex['id'], b'POM\0', 64))['color_entries'] == 256:
                available.append(slot)
    if not available:
        return geometry, {}, ['当前形态没有可独立写入的 8 位贴图槽']
    body_slot = available[0]
    lamp = source_lamp(geometry, skin, rig, rotations)
    carrier = lamp_slots(game, case, rig, selection, member, textures, base_address) if lamp in files else None
    if carrier and textures[body_slot]['id'] in carrier[3:5]:
        carrier = None

    decoded, transforms, staged, tile_keys = {}, {}, {}, {}
    regions = {}
    uvs = np.asarray(uv)
    sampled = defaultdict(list)
    repeats = {}
    for index, (material, corners) in enumerate(faces):
        if material in files and all(tex is not None for _, tex, _ in corners):
            raw = uvs[[tex for _,tex,_ in corners]] * (1, -1) + (0, 1)
            period = np.floor(raw.min(axis=0)).astype(int)
            reduced = raw - period
            repeats[index] = tuple(period)
            sampled[material].append(reduced)
    for material, file in files.items():
        original = settings[material]
        tint = (*original['Kd'], original['d'][0])
        key = str(file), tint
        tile_keys[material] = key
        if file not in decoded:
            decoded[file] = decode_png(file.read_bytes())
        tex = np.concatenate(sampled[material])
        if carrier and material == lamp:
            tile = tile_for(decoded[file], tex, tint)
            layout = carrier[-1]
            w, h = layout['width'], layout['height']
            p, clut = indexed_palette(resize_color(tile[0], w, h), layout['color_entries'])
            staged.update(lamp_palettes(carrier, boot_pom(carrier[5], p), clut))
            scale, bias = remap_uvs(tile, (0, 0, w, h), (w, h))
            transforms[material] = (*carrier[:2], scale, bias, 0, 0)
        else:
            regions.setdefault(key, []).append(tex)
    tiles = {key:tile_for(decoded[Path(key[0])], np.concatenate(coords), key[1]) for key, coords in regions.items()}
    if tiles:
        w, h, rect = place_tiles(tiles)
        image = np.zeros((h, w, 4), dtype=np.uint8)
        for key, (x, y, width, height) in rect.items():
            image[y:y+height, x:x+width] = resize_color(tiles[key][0], width, height)
        tex_id = textures[body_slot]['id']
        original = game.read_source_member(tex_id, b'POM\0')
        pixels, clut = indexed_palette(image, 256)
        staged[tex_id] = boot_pom(original, pixels, body_palettes(original, clut))
        for material, key in tile_keys.items():
            if material in transforms:
                continue
            scale, bias = remap_uvs(tiles[key], rect[key], (w, h))
            blend = int(np.any(tiles[key][0][:, :, 3] < 255))
            transforms[material] = body_slot, 0, scale, bias, 1, blend

    # Material changes can bind different UV rectangles even for the same PNG.
    output = list(uv)
    mapping = {}
    new_faces = []
    for index, (material, corners) in enumerate(faces):
        if index not in repeats:
            new_faces.append((material, corners))
            continue
        _, _, scale, bias, _, _ = transforms[material]
        period = repeats[index]
        corners_out = []
        for v, tex, n in corners:
            key = material, tex, period
            if key not in mapping:
                u, t = (uvs[tex] * (1, -1) + (0, 1) - period) * scale + bias
                mapping[key] = len(output)
                output.append((u, 1 - t))
            corners_out.append((v, mapping[key], n))
        new_faces.append((material, tuple(corners_out)))
    for material, (slot, variant, _, _, kind, blend) in transforms.items():
        settings[material] = {
            'kind':kind, 'texture':slot, 'variant':variant, 'ambient':0xFF1F1F1F,
            'diffuse':0xFFFFFFFF, 'specular':0, 'shininess':0., 'flags':0x100 | blend
        }
    return (points, output, normals, new_faces, settings), staged, sorted(missing)
