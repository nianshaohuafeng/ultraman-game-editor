"""Bake FE3 controls, native light normals and VAU hand shapes into PSP MDL."""

from collections import Counter, defaultdict
import struct

import numpy as np

from assets import BASE
from cross_move import Retarget, Skeleton, is_fe3, skeleton
from cross_hands import hand_combinations, surface_points
from cross_texture import (editable_texture, indexed_textures, lamp_palettes, lamp_slots, make_atlas,
                           make_lamp, retained_slots, used_uvs)
from fe3_formats import require
from fe3_preview import make_model
from fe3_workbench import morph_pose
from fighter_parts import native_mesh
from kgf_readers.mdl_reader import read_mdl
from mesh_import import bound_mesh_to_mdl


def normalize(array):
    return array / np.maximum(np.linalg.norm(array, axis=-1, keepdims=True), 1e-10)


def matrices(binding):
    result = {}
    for index, matrix in enumerate(binding.source.world):
        track = binding.source.ancestor(index, binding.common)
        if track is not None:
            donor = binding.source.tracks[track]
            receiver = binding.target.tracks[track]
            local = np.linalg.solve(binding.source.world[donor], matrix)
            local[:3] *= binding.scale
            result[index] = receiver, binding.target.world[receiver] @ local
    return result


def bind_surface(mesh, mapping):
    require(all(node in mapping for node in mesh['controlBones']), '来源模型的可见部件缺少相容的动作骨链。')
    controls = [mapping[node][0] for node in mesh['controlBones']]
    weights = [{node:1.0} for node in controls]
    for terms in mesh['subdivision']:
        row = defaultdict(float)
        for index, weight in zip(terms[::2], terms[1::2]):
            row[controls[index]] += weight
        weights.append(dict(row))
    return weights


def posed_surface(mesh, mapping, morph=None):
    transforms = np.asarray([mapping[node][1] for node in mesh['controlBones']])
    return surface_points(mesh['positions'], mesh['controlOffsets'], mesh['subdivision'], transforms, morph)


def native_normals(mesh, points, mapping):
    light = mesh['lighting']
    authored = [mapping[node][1][:3, :3] @ np.asarray((x, y, z)) if node in mapping else np.zeros(3)
                for node, x, y, z in light['authored']]
    # Use the source normal ring and color indirection, as in VU1 0x0EF–0x237.
    for center, *ring in light['rings']:
        edges = points[ring] - points[center]
        authored.append(np.cross(edges[:-1], edges[1:]).sum(axis=0))
    authored = normalize(np.asarray(authored).reshape(-1, 3))
    directions = [np.zeros(3) for _ in light['rgba']]
    directions.extend(authored[index] for index, *_ in light['lit'])
    directions.extend(normalize(directions[a] + directions[b]) for a, b in light['pairs'])
    return np.asarray(directions)[mesh['colorIds']]


def face_materials(model, surfaces):
    # Emissive texture regions have raw GS RGBA indices rather than lit ones.
    regions = defaultdict(list)
    lit = set()
    count = Counter()
    for mesh, (points, normals) in zip(model['meshes'], surfaces):
        for material, corners in mesh['batches']:
            regions[material].extend(points[np.asarray(mesh['source'])[corners]])
            count[material] += len(corners)
            if np.any(normals[corners]):
                lit.add(material)
    return regions, lit, count


def lamp_material(model, rig, regions, lit):
    if rig.timer is None or 13 not in rig.tracks or 10 not in rig.tracks:
        return None
    low = min(p[1] for vertices in regions.values() for p in vertices)
    high = max(p[1] for vertices in regions.values() for p in vertices)
    height = high - low
    neck = rig.centers[rig.tracks[13]]
    chest = rig.centers[rig.tracks[10]]
    candidates = []
    for material, vertices in regions.items():
        if material in lit or model['materials'][material]['gs']['alpha'] is not None:
            continue
        rows = np.asarray(vertices)
        mean = (rows.min(axis=0) + rows.max(axis=0)) * .5
        wide, tall = np.ptp(rows, axis=0)[:2]
        if (0 < wide <= .09 * height and 0 < tall <= .09 * height
                and abs(mean[0] - neck[0]) < .04 * height
                and neck[1] - .19 * height < mean[1] < neck[1] + .015 * height
                and mean[2] > chest[2] + .025 * height):
            candidates.append((abs(mean[1] - (neck[1] + chest[1]) * .5), material))
    return min(candidates)[1] if candidates else None


def convert_textures(game, source, case, member, model, textures, rig, receiver, surfaces):
    regions, lit, counts = face_materials(model, surfaces)
    source_images, source_materials = indexed_textures(source, model)
    coords = used_uvs(model)
    stock = rig.model
    require(0 not in retained_slots(rig) and editable_texture(game, member, 0, textures),
            '目标主体贴图与其他模型或保留部件共用，无法一起重映射 UV。')
    replacements, binds = {}, {}
    lamp = lamp_material(model, rig, regions, lit)
    if lamp is not None:
        carrier = lamp_slots(game, case, rig, receiver, member, textures, BASE)
        key, clut = source_materials[lamp]
        if carrier and len(clut) == carrier[-1]['palette_block_size']:
            light_pom, transform = make_lamp(carrier[5], source_images[key], coords.pop(lamp))
            replacements.update(lamp_palettes(carrier, light_pom, clut))
            binds[lamp] = *carrier[:2], *transform

    body = max(coords, key=counts.get)
    texture_id = textures[0]['id']
    original = game.read_source_member(texture_id, b'POM\0')
    replacements[texture_id], uv = make_atlas(original, source_images, source_materials, coords, body)
    binds.update({material:(0, *placement) for material, placement in uv.items()})

    presets = Counter(mat for mesh in stock['meshes'] for mat, _ in mesh['faces']
                      if stock['materials'][mat]['kind'] in (1, 2) and stock['materials'][mat]['texture'] == 0)
    body_material = stock['materials'][presets.most_common(1)[0][0]] if presets else {
        'kind':2, 'ambient':0xFF1F1F1F, 'diffuse':0xFFB2B2B2, 'specular':0xFFE5E5E5, 'shininess':20.}
    emissive_material = {'kind':0, 'ambient':0xFF000000, 'diffuse':0xFFFFFFFF, 'specular':0xFF000000, 'shininess':0.}
    settings = {}
    for mat, (slot, variant, _, _) in binds.items():
        gs_alpha = model['materials'][mat]['gs']['alpha']
        mode = {0x44:1, 0x48:2}.get(gs_alpha, 0)
        # GS strips are two-sided. EBOOT material byte +0x1D disables GE backface culling.
        settings[mat] = {**(body_material if mat in lit else emissive_material),
                         'texture':slot, 'variant':variant, 'flags':0x100 | mode}
    return replacements, binds, settings


def split_hand_regions(model, rig, mapping, poses):
    """Find all source faces whose control points are changed by a hand sample."""
    moved = {}
    changed = {side:set().union(*(pose.keys() for pose in poses)) for side, poses in poses.items() if poses}
    wrists = {rig.tracks.get(track):side for side, track in enumerate((18, 22)) if side in changed}
    for mesh_id, mesh in enumerate(model['meshes']):
        vertices = [set() for _ in mesh['controlBones']]
        for i, (node, offset) in enumerate(zip(mesh['controlBones'], mesh['controlOffsets'])):
            side = wrists.get(mapping[node][0])
            if side is not None and offset in changed[side]:
                vertices[i].add(side)
        for terms in mesh['subdivision']:
            vertices.append(set().union(*(vertices[i] for i in terms[::2])))
        moving = {}
        for material, corners in mesh['batches']:
            for i in range(0, len(corners), 3):
                triangle = tuple(corners[i:i + 3])
                ids = [mesh['source'][j] for j in triangle]
                sides = set().union(*(vertices[index] for index in ids))
                if len(sides) == 1:
                    moving[material, triangle] = sides.pop()
        moved[mesh_id] = moving
    return moved


def hand_matches(rig, side, owners, clouds):
    """Choose the donor VAU shapes by comparison with the recipient's native hand slots."""
    stock = {mesh['owner']:mesh for mesh in rig.model['meshes'] if mesh['faces']}
    refs = {node:native_mesh(rig, stock[node])[0] for node in owners if node in stock}
    a, b = ((17, 18), (21, 22))[side]
    wrist = rig.centers[rig.tracks[b]]
    axis = normalize(wrist - rig.centers[rig.tracks[a]])

    def prepare(cloud):
        cloud = np.unique(np.concatenate(cloud) if isinstance(cloud, list) else cloud, axis=0)
        points = cloud - wrist
        along = points @ axis
        # Match palm/fingers while allowing the source's sleeve and cuff to differ.
        positive = points[along > np.quantile(along, .25)]
        return positive if len(positive) >= 3 else points

    source = [prepare(cloud) for cloud in clouds]
    target = {node:prepare(points) for node, points in refs.items()}
    resize = np.max([np.max(p @ axis) for p in target.values()]) / max(.1, np.max([
        np.max(p @ axis) for p in source]))
    source = [points * resize for points in source]

    def score(target, source):
        error = np.sum((target[:, None] - source[None]) ** 2, axis=2)
        return error.min(axis=0).mean() + error.min(axis=1).mean()

    return {node:min(range(len(source)), key=lambda i:score(points, source[i])) for node, points in target.items()}


def convert_geometry(model, matrix, surfaces, rig, poses, uv, settings):
    hands = split_hand_regions(model, rig, matrix, poses)
    poses = hand_combinations(model, matrix, rig, poses, hands, posed_surface)
    # Cache just the meshes whose evaluated controls change under the VAU sample.
    frames = {}
    refs = defaultdict(list)
    for i, mesh in enumerate(model['meshes']):
        moving = hands[i]
        for side, shapes in poses.items():
            region = {corner for (_, triangle), part in moving.items() if side == part for corner in triangle}
            if not region:
                continue
            vertices = sorted({mesh['source'][n] for n in region})
            for pose_index, pose in enumerate(shapes):
                points = posed_surface(mesh, matrix, pose)
                frames[i, side, pose_index] = points, native_normals(mesh, points, matrix)
                if not refs[side]:
                    refs[side] = [[] for _ in shapes]
                refs[side][pose_index].append(points[vertices])
    selections = {side:hand_matches(rig, side, rig.hands[side], clouds)
                  for side, clouds in refs.items()}
    points, directions, coords, faces, weights, owners = [], [], [], [], [], []
    for i, mesh in enumerate(model['meshes']):
        base, normal = surfaces[i]
        bind = bind_surface(mesh, matrix)
        source = mesh['source']
        st = np.asarray(mesh['attributes']).reshape(-1, 9)[:, 3:5]
        blocks = defaultdict(list)
        for mat, corners in mesh['batches']:
            for start in range(0, len(corners), 3):
                triangle = tuple(corners[start:start + 3])
                side = hands[i].get((mat, triangle))
                if selections.get(side):
                    for owner, pose_index in selections[side].items():
                        blocks[owner, side, pose_index].append((mat, triangle))
                else:
                    blocks[None, None, None].append((mat, triangle))
        for (owner, side, pose_index), triangles in blocks.items():
            xyz, normals = (base, normal) if owner is None else frames[i, side, pose_index]
            idx = {}
            for mat, triangle in triangles:
                _, _, scale, bias = uv[mat]
                raw_uv = st[list(triangle)] * scale + bias
                raw_uv[:, 1] = 1 - raw_uv[:, 1]
                start = len(coords)
                coords.extend(raw_uv)
                corners = []
                for j, corner in enumerate(triangle):
                    vertex = source[corner]
                    if vertex not in idx:
                        idx[vertex] = len(points)
                        points.append(xyz[vertex])
                        weights.append(bind[vertex].copy())
                    corners.append((idx[vertex], start + j, len(directions)))
                    directions.append(normals[corner])
                faces.append((mat, tuple(corners)))
                owners.append(owner)
    return (np.asarray(points), coords, np.asarray(directions), faces, settings), weights, owners


def convert_model(target, source, receiver, donor):
    require(not is_fe3(target) and is_fe3(source), 'MDL 转换的来源请选择奥3，目标请选择奥0。')
    source.ensure_open()
    form = source.workbench.form(donor[0], donor[2])
    require(not form.accessory, '请选择奥3角色本体形态。')
    model = make_model(source, form)
    case, native, _, textures = target.form_binding(*receiver)
    member = native['mdl']['id']
    original = target.read_source_member(member, b'MDL\0')
    rig = target.model_rig(read_mdl(original), receiver)
    binding = Retarget(skeleton(source, *donor), Skeleton(rig.bones, rig.tracks))
    matrix = matrices(binding)
    # Keep original PSP props, whose native callbacks own their visibility.
    model['meshes'] = [mesh for mesh in model['meshes']
                       if not all(matrix.get(node, (None,))[0] in rig.parts for node in mesh['controlBones'])]
    surfaces = []
    for mesh in model['meshes']:
        points = posed_surface(mesh, matrix)
        surfaces.append((points, native_normals(mesh, points, matrix)))
    poses = {}
    if form.vau and rig.hands:
        vau = source.data(form.vau)
        samples = [morph_pose(vau, i) for i in range(struct.unpack_from('<I', vau, 8)[0])]
        available = {mesh['owner'] for mesh in rig.model['meshes'] if mesh['faces']}
        for side, track in enumerate((18, 22)):
            if not available.intersection(rig.hands[side]) or track not in rig.tracks or track - 1 not in rig.tracks:
                continue
            offsets = {off for mesh in model['meshes'] for node, off in zip(mesh['controlBones'], mesh['controlOffsets'])
                       if matrix[node][0] == rig.tracks[track]}
            poses[side] = [pose for pose in samples if offsets.intersection(pose)]
    poms, transforms, settings = convert_textures(target, source, case, member, model, textures, rig, receiver, surfaces)
    geometry, weights, owners = convert_geometry(model, matrix, surfaces, rig, poses, transforms, settings)
    converted = bound_mesh_to_mdl(geometry, weights, original, rig, face_owners=owners)
    name = f'MDL {member} · POM ' + ' / '.join(map(str, poms))
    return target.commit_model_bundle(member, original, converted, poms, name)

