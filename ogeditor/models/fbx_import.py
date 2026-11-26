"""Decode FBX mesh instances and their bind geometry for MDL import."""

from collections import defaultdict
from pathlib import Path
import re
from tempfile import TemporaryDirectory

import numpy as np

from ogeditor.models.model_retarget import preserve_neck_shape


# PSP logical tracks. FBX labels include the asset pack's Japanese names
# and the corresponding names common in Blender and Mixamo rigs.
TORSO_TRACKS = {
    'koshi': 0, 'hips': 0, 'pelvis': 0,
    'hara': 1, 'spine': 1, 'lowerback': 1,
    'mune': 10, 'spine1': 10, 'spine2': 10, 'chest': 10, 'upperchest': 10,
    'kubi': 13, 'neck': 13, 'neck1': 13, 'atama': 14, 'head': 14,
}
LIMB_TRACKS = {
    label: pair
    for pair, labels in (
        ((2, 3), ('momo', 'thigh', 'upperleg', 'upleg')),
        ((7, 4), ('sune', 'shin', 'calf', 'lowerleg', 'leg', 'knee')),
        ((8, 5), ('ashi', 'foot', 'ankle')),
        ((9, 6), ('tuma', 'toe', 'toes', 'toebase')),
        ((11, 12), ('mune', 'shoulder', 'collar', 'clavicle')),
        ((16, 20), ('zyouwan', 'upperarm', 'arm')),
        ((17, 21), ('ude', 'forearm', 'lowerarm', 'elbow')),
        ((18, 22), ('te', 'hand', 'wrist')),
    )
    for label in labels
}
OPPOSITE_TRACK = {track: pair[1-index] for pair in LIMB_TRACKS.values() for index, track in enumerate(pair)}


def joint_label(name):
    label = re.sub(r'[^a-z0-9]', '', name.rsplit(':', 1)[-1].lower())
    return re.sub(r'^(bip|mixamorig)\d*', '', label)


def joint_track(name):
    label = joint_label(name)
    if label in TORSO_TRACKS:
        return TORSO_TRACKS[label]
    for side, index in (('left', 0), ('right', 1), ('l', 0), ('r', 1)):
        if label.startswith(side) and label[len(side):] in LIMB_TRACKS:
            return LIMB_TRACKS[label[len(side):]][index]
        if label.endswith(side) and label[:-len(side)] in LIMB_TRACKS:
            return LIMB_TRACKS[label[:-len(side)]][index]
    return None


def skin_alignment(skin, target, rotations):
    parents, centers, _ = skin
    landmarks = {}
    for name in parents:
        track = joint_track(name)
        if track in target.tracks:
            landmarks.setdefault(track, centers[name])
    required = (2, 3, 5, 8, 13)
    if any(track not in landmarks for track in required):
        return None

    def axes(joints):
        hips = (joints[0] + joints[1]) * .5
        ankles = (joints[2] + joints[3]) * .5
        return hips, ankles, joints[4] - ankles, joints[0] - joints[1]

    hips, ankles, up, side = axes([landmarks[track] for track in required])
    native_hips, native_ankles, native_up, native_side = axes(
        target.centers[[target.tracks[track] for track in required]])
    lengths = [np.linalg.norm(v) for v in (up, side, native_up, native_side)]
    if min(lengths) <= 1e-10:
        return None

    def alignment(rotation):
        upright = np.dot(rotation @ up, native_up) / (lengths[0] * lengths[2])
        lateral = np.dot(rotation @ side, native_side) / (lengths[1] * lengths[3])
        return upright + lateral

    # Accessories and tails can extend further than the body in any axis.
    rotation = max(rotations, key=alignment)
    height = (rotation @ up)[1]
    if min(height, native_up[1]) <= 1e-10:
        return None
    matrix = rotation * (native_up[1] / height)
    offset = native_hips - matrix @ hips
    offset[1] = (native_ankles - matrix @ ankles)[1]
    return matrix, offset


def track_chain(target, tracks, parent):
    nodes = []
    for track in tracks:
        node = target.tracks.get(track)
        if node is None or target.bones[node]['parent'] != parent or node in target.parts:
            break
        nodes.append(node)
        parent = node
    return nodes


def auxiliary_joints(parents, target):
    tail = track_chain(target, range(25, 30), target.tracks.get(0))
    if len(tail) == 5:
        animated = set(target.tracks.values())
        while True:
            children = [node for node in target.body_nodes if node not in animated
                        and target.bones[node]['parent'] == tail[-1]]
            if len(children) != 1:
                break
            tail.append(children[0])
    cloth = track_chain(target, range(77, 81), target.tracks.get(10))
    mapping, cloth_joints = {}, defaultdict(list)
    for name in parents:
        label = joint_label(name)
        segment = re.fullmatch(r'(?:shippo|tail)([1-9][0-9]*)', label)
        if segment and int(segment[1]) <= len(tail):
            mapping[name] = tail[int(segment[1]) - 1]
        strip = re.fullmatch(r'[lr]obi([1-4])', label)
        if strip and int(strip[1]) <= len(cloth):
            node = cloth[int(strip[1]) - 1]
            mapping[name] = node
            cloth_joints[node].append(name)
        if label == 'obiall' and cloth:
            mapping[name] = cloth[0]
        face = {'kao': 30, 'ago': 31, 'jaw': 31}.get(label)
        node = target.tracks.get(face)
        head = target.tracks.get(14)
        if node is not None and head is not None:
            ancestor = target.bones[node]['parent']
            while ancestor >= 0 and ancestor != head:
                ancestor = target.bones[ancestor]['parent']
            if ancestor == head:
                mapping[name] = node
    return mapping, cloth_joints


def map_skin_weights(skin, target, transform, offset, align_joints):
    parents, centers, weights = skin
    tracks = {name: track for name in parents if (track := joint_track(name)) is not None}
    hips = {track: centers[name] for name, track in tracks.items() if track in (2, 3)}
    mirrored = False
    if all(track in hips and track in target.tracks for track in (2, 3)):
        donor = (hips[2] - hips[3]) @ transform.T
        native = target.centers[target.tracks[2]] - target.centers[target.tracks[3]]
        mirrored = np.dot(donor[[0, 2]], native[[0, 2]]) < 0

    mapping, cloth_joints = auxiliary_joints(parents, target)
    for name, track in tracks.items():
        dest_track = OPPOSITE_TRACK.get(track, track) if mirrored else track
        # The driver below the upper-arm pivot follows the twist channel.
        if dest_track not in target.tracks:
            dest_track = {16: 15, 20: 19}.get(dest_track)
        if dest_track in target.tracks:
            mapping[name] = target.tracks[dest_track]
    direct = mapping.copy()
    shifts = {}
    if align_joints:
        shifts = {name: target.centers[node] - (centers[name] @ transform.T + offset)
                  for name, node in direct.items()}
        trunk = {track:name for name, track in tracks.items() if track in (0, 1, 10, 13, 14) and name in shifts}
        if len(trunk) == 5:
            # Native ANM matrices already rotate about the target's pivots. Pulling
            # the surface separately toward every spine pivot changes the source waist.
            displacement = np.median([shifts[name] for name in trunk.values()], axis=0)
            shifts.update(dict.fromkeys(trunk.values(), displacement))
        else:
            neck = trunk.get(13)
            preserve_neck_shape(target, direct, neck, shifts)
        for names in cloth_joints.values():
            # The two source strips share a centered native bone.
            middle = np.mean([shifts[name] for name in names], axis=0)
            shifts.update(dict.fromkeys(names, middle))
    for name in parents:
        if name in direct:
            continue
        current = parents[name]
        seen = {name}
        while current and current not in direct and current not in seen:
            seen.add(current)
            current = parents.get(current)
        if current in direct:
            mapping[name] = direct[current]
            if align_joints:
                shifts[name] = shifts[current]

    result, delta = [], np.zeros((len(weights), 3))
    for i, weights_at_vertex in enumerate(weights):
        row = defaultdict(float)
        if weights_at_vertex and all(name in mapping for name in weights_at_vertex):
            for name, weight in weights_at_vertex.items():
                row[mapping[name]] += weight
                if align_joints:
                    delta[i] += weight * shifts[name]
            if align_joints:
                delta[i] /= sum(weights_at_vertex.values())
        strongest = sorted(row.items(), key=lambda pair: (-pair[1], pair[0]))[:4]
        total = sum(weight for _, weight in strongest)
        result.append({bone: weight / total for bone, weight in strongest} if total else None)
    return result, delta


def read_fbx(data):
    try:
        import assimp_py
    except ImportError as exc:
        raise ValueError('源码版需安装 assimp-py==1.2.0 才能读取 FBX。新版 EXE 已包含此读取库。') from exc

    flags = (assimp_py.Process_Triangulate | assimp_py.Process_SortByPType
             | assimp_py.Process_GenSmoothNormals | assimp_py.Process_JoinIdenticalVertices)
    # The binding takes a narrow filename on Windows. Stage the bytes so the
    # selected source can have a Chinese name or be in a deep directory.
    with TemporaryDirectory(prefix='kgf-fbx-') as folder:
        staged = Path(folder) / 'model.fbx'
        staged.write_bytes(data)
        try:
            scene = assimp_py.import_file(str(staged), flags)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ValueError('无法读取 FBX。' + str(exc).replace(str(staged), '')) from exc
    return scene_geometry(scene)


def scene_geometry(scene):
    positions, uv, normals, faces, rigid_parts = [], [], [], [], []
    parents, centers, weights = {}, {}, []
    settings = defaultdict(lambda: {'Kd': (.8, .8, .8), 'd': (1.,)})
    for index, material in enumerate(scene.materials):
        color = tuple(material.get('COLOR_DIFFUSE', (.8, .8, .8)))
        opacity = material.get('OPACITY', 1.)
        if len(color) >= 3 and np.isfinite((*color[:3], opacity)).all():
            settings[index] = {'Kd': color[:3], 'd': (opacity,)}
        diffuse = material.get('TEXTURES', {}).get(1) or ()
        if diffuse:
            settings[index]['diffuse_file'] = diffuse[0]

    vertex_offset = 0
    normal_offset = 0
    nodes = [(scene.root_node, np.eye(4))]
    while nodes:
        node, parent = nodes.pop()
        world = parent @ np.asarray(node.transformation, dtype=float)
        if not np.isfinite(world).all():
            raise ValueError('FBX 含有无效的网格变换。')
        parents[node.name] = node.parent_name
        centers[node.name] = world[:3, 3]
        nodes.extend((child, world) for child in reversed(node.children))
        linear = world[:3, :3]
        reverse = np.linalg.det(linear) < 0
        for index in node.mesh_indices:
            mesh = scene.meshes[index]
            vertices = np.asarray(mesh.vertices, dtype=float).reshape(-1, 3)
            indices = np.asarray(mesh.indices, dtype=np.int64)
            if not len(vertices) or not indices.size or indices.size != 3 * mesh.num_faces:
                continue
            if indices.min() < 0 or indices.max() >= len(vertices):
                raise ValueError('FBX 的三角面引用了无效顶点。')
            points = vertices @ linear.T + world[:3, 3]
            uv_offset = len(uv)
            has_uv = bool(mesh.texcoords and mesh.texcoords[0])
            if has_uv:
                channel = np.asarray(mesh.texcoords[0], dtype=float).reshape(len(vertices), -1)
                uv.extend((row[0], row[1] if len(row) > 1 else 0.) for row in channel)
            has_normals = bool(mesh.normals)
            if has_normals:
                source_normals = np.asarray(mesh.normals, dtype=float).reshape(-1, 3)
                # Vertex positions use the node's linear matrix. Normals use
                # its inverse transpose, including non-uniform FBX scales.
                directions = source_normals @ np.linalg.pinv(linear)
                normals.append(directions)
                if not np.isfinite(directions).all():
                    raise ValueError('FBX 含有无效的法线。')
            if not np.isfinite(points).all():
                raise ValueError('FBX 含有无效的顶点。')
            triangles = indices.reshape(-1, 3)
            if reverse:
                triangles = triangles[:, (0, 2, 1)]
            for triangle in triangles.tolist():
                corners = tuple((vertex_offset + vertex, uv_offset + vertex if has_uv else None,
                                 normal_offset + vertex if has_normals else None)
                                for vertex in triangle)
                faces.append((mesh.material_index, corners))
            skin_rows = [{} for _ in vertices]
            for bone in mesh.bones:
                if not bone.num_weights:
                    continue
                for vertex, weight in zip(bone.weight_vertex_ids, bone.weights):
                    if 0 <= vertex < len(vertices) and np.isfinite(weight) and weight > 1e-8:
                        skin_rows[vertex][bone.name] = weight
            weights.extend(skin_rows)
            # Rigid pieces such as a weapon follow one destination bone,
            # regardless of how far their vertices are from the body.
            if len(mesh.bones) == 1 and mesh.bones[0].num_weights == len(vertices):
                bone = mesh.bones[0]
                offset = np.asarray(bone.offset_matrix, dtype=float)
                try:
                    anchor = (world @ np.linalg.solve(offset, (0., 0., 0., 1.)))[:3]
                except np.linalg.LinAlgError:
                    anchor = points.mean(axis=0)
                if np.isfinite(anchor).all():
                    rigid_parts.append((vertex_offset, vertex_offset + len(vertices), anchor))
            positions.append(points)
            vertex_offset += len(vertices)
            if has_normals:
                normal_offset += len(vertices)

    if not faces:
        raise ValueError('FBX 没有可导入的网格面。')
    if not np.isfinite(uv).all():
        raise ValueError('FBX 含有无效的 UV 坐标。')
    geometry = (np.concatenate(positions), uv,
                np.concatenate(normals) if normals else np.empty((0, 3)), faces, settings)
    return geometry, rigid_parts, (parents, centers, weights)
