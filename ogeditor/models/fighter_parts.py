"""Fit native hand variants and indicator meshes to imported geometry."""

from collections import Counter
import re

import numpy as np

from ogeditor.models.fbx_import import joint_label, joint_track
from ogeditor.models.model_retarget import is_shadow, mesh_weights, transform_normals, transform_points


def native_mesh(rig, mesh):
    palette, weights = mesh_weights(mesh)
    matrix = rig.mesh_matrix(mesh['owner'], mesh['palette'], weights)
    points = transform_points(matrix, mesh['positions'])
    normals = np.asarray(mesh['normals'])
    if len(normals) and matrix.ndim == 2:
        normals = transform_normals(matrix, normals)
    return points, palette, weights, matrix, normals


def missing_hands(rig):
    return rig.hand_nodes - {mesh['owner'] for mesh in rig.model['meshes'] if mesh['faces']}


def finger_vertices(skin, track, count):
    if skin is None:
        return np.ones(count, dtype=bool)
    parents, _, rows = skin
    names = {name for name in parents if joint_track(name) == track}
    fingers = r'(?:hito|naka|kusu|koyu|oya|tume|hasami|thumb|index|middle|ring|pinky|little|finger)'
    for name, parent in parents.items():
        if parent in names and re.search(fingers, joint_label(name)):
            names.add(name)
    eligible = [not row or sum(w for name, w in row.items() if name in names) >= .2 for row in rows]
    return np.asarray(eligible + [False] * (count - len(eligible)), dtype=bool)


def split_hand(points, uv, normals, faces, bind, side, rig, skin, rigid_parts):
    first, last = ((17, 18), (21, 22))[side]
    elbow, wrist = rig.tracks.get(first), rig.tracks.get(last)
    if elbow is None or wrist is None:
        return faces, None
    center = rig.centers[wrist]
    axis = center - rig.centers[elbow]
    length = np.linalg.norm(axis)
    if length < 1e-6:
        return faces, None
    axis /= length

    meshes = {m['owner']: m for m in rig.model['meshes'] if m['faces']}
    variants = [meshes[node] for node in dict.fromkeys(rig.hands[side]) if node in meshes]
    if not variants:
        return faces, None
    reference = [native_mesh(rig, mesh)[0] - center for mesh in variants]
    cut = min(np.min(p @ axis) for p in reference)
    reach = max(np.max(p @ axis) for p in reference) - cut
    width = max(np.linalg.norm(p - (p @ axis)[:, None] * axis, axis=1).max() for p in reference)

    xyz = np.asarray(points)
    delta = xyz - center
    along = delta @ axis - cut
    radial = np.linalg.norm(delta - (delta @ axis)[:, None] * axis, axis=1)
    eligible = (radial < 1.8 * width) & (along < 2.4 * reach) & (along > -.6 * length)
    anatomy = finger_vertices(skin, last, len(points))
    for start, end, _ in rigid_parts:
        eligible[start:end] = False
    scores = np.array([row.get(wrist, 0.) for row in bind])
    candidates = []
    for i, (_, face) in enumerate(faces):
        ids = [corner[0] for corner in face]
        if eligible[ids].all() and anatomy[ids].any() and scores[ids].max() >= .35 and along[ids].max() > 0:
            candidates.append(i)
    if not candidates:
        return faces, None
    wrist_ring, samples = [], []
    for i in candidates:
        mat, face = faces[i]
        for a, b in zip(face, (*face[1:], face[0])):
            va, vb = a[0], b[0]
            if along[va] > 0:
                samples.append((mat, va, a[1]))
            if (along[va] <= 0) != (along[vb] <= 0):
                u = along[va] / (along[va] - along[vb])
                wrist_ring.append((1-u) * xyz[va] + u * xyz[vb])
    if len(wrist_ring) < 6 or len(samples) < 12:
        return faces, None

    # Clip the original forearm against the palm seam. Make the boundary share
    # the wrist matrix used by the baked hand variants.
    remove = set(candidates)
    output = []
    edges = {}

    def interpolate(values, a, b, u):
        if a is None or b is None:
            return None
        values.append(tuple((1-u) * np.asarray(values[a]) + u * np.asarray(values[b])))
        return len(values) - 1

    for i, (mat, face) in enumerate(faces):
        if i not in remove:
            output.append((mat, face))
            continue
        clipped = []
        for a, b in zip(face, (*face[1:], face[0])):
            va, vb = a[0], b[0]
            if along[va] <= 0:
                clipped.append(a)
            if (along[va] <= 0) == (along[vb] <= 0):
                continue
            u = along[va] / (along[va] - along[vb])
            edge = tuple(sorted((va, vb)))
            if edge not in edges:
                edges[edge] = len(points)
                points.append((1-u)*xyz[va] + u*xyz[vb])
                bind.append({wrist: 1.})
            clipped.append((edges[edge], interpolate(uv, a[1], b[1], u),
                            interpolate(normals, a[2], b[2], u)))
        output.extend((mat, (clipped[0], clipped[j], clipped[j+1])) for j in range(1, len(clipped)-1))

    rim = np.unique(np.asarray(wrist_ring).round(5), axis=0)
    rim_center = rim.mean(axis=0)
    rim_width = np.mean(np.linalg.norm(rim - rim_center, axis=1))
    material = Counter(mat for mat, _, _ in samples).most_common(1)[0][0]
    hand_uv = [(xyz[v], uv[t]) for mat, v, t in samples if mat == material and t is not None]
    return output, (variants, center, axis, rim_center, rim_width, material, hand_uv)


def add_hands(writer, variants, material_indices):
    rig = writer.rig
    for meshes, center, axis, rim_center, rim_width, name, samples in variants:
        mat = material_indices[name]
        for mesh in meshes:
            points, palette, weights, matrix, normals = native_mesh(rig, mesh)
            delta = points - center
            along = delta @ axis
            radial = delta - along[:, None]*axis
            root = radial[along <= along.min() + 1.4]
            offset = root.mean(axis=0)
            width = np.linalg.norm(root-offset, axis=1).mean()
            scale = np.clip(rim_width / max(width, .01), .55, 1.65)
            points = rim_center + (along-along.min()-.65)[:, None]*axis + scale*(radial-offset)
            if samples:
                xyz, uv = map(np.asarray, zip(*samples))
                nearest = np.argmin(np.sum((points[:, None]-xyz[None])**2, axis=2),axis=1)
                uv = uv[nearest] * [1., -1.] + [0., 1.]
            else:
                uv = np.zeros((len(points), 2))
            # Baked shapes animate through the wrist palette even when the
            # engine has disabled updates on the mesh owner itself.
            if len(normals) and matrix.ndim != 2:
                posed_normals = []
                strips = []
                for _, face in mesh['faces']:
                    corners = []
                    for v in face:
                        posed_normals.append(transform_normals(matrix[v['v']], normals[v['n']]))
                        corners.append((v['v'], len(posed_normals)-1, v['v']))
                    strips.append((mat, tuple(corners)))
                normals = posed_normals
            else:
                strips = [(mat, tuple((v['v'], v.get('n', 0), v['v']) for v in face))
                          for _, face in mesh['faces']]
            if not len(normals):
                normals = [(0., 1., 0.)]
            owner = writer.reserve(mesh['owner'], palette, (len(points), len(normals), 0, len(uv)),
                                   len(strips), fixed=mesh['owner'])
            writer.add(owner, {'palette': palette, 'weights': weights, 'positions': points,
                               'uv': uv, 'normals': normals, 'colors': [], 'strips': strips})


def timer_faces(rig):
    if rig.timer is None:
        return []
    return [(mesh, mat, face) for mesh in rig.model['meshes'] if not is_shadow(rig.model, mesh)
            for mat, face in mesh['faces']
            if rig.model['materials'][mat]['kind'] == 0
            and (rig.model['materials'][mat]['texture'], rig.model['materials'][mat]['variant']) == rig.timer]


def timer_bounds(rig, template):
    used = {}
    rows = []
    for mesh, _, face in template:
        owner = mesh['owner']
        if owner not in used:
            used[owner] = native_mesh(rig, mesh)[0]
        rows.extend(used[owner][[c['v'] for c in face]])
    rows = np.asarray(rows)
    return rows.min(axis=0), rows.max(axis=0)


def fit_timer(template, native_bounds, low, high):
    n0, n1 = native_bounds
    center = (n0+n1)*.5
    destination = (low+high)*.5
    scale = np.clip(max((high-low)[:2]) / max((n1-n0)[:2]), .65, 1.5)
    destination[2] = high[2] - (n1[2]-center[2])*scale + .12
    return template, center, destination, scale


def split_timer(points, faces, target):
    template = timer_faces(target)
    if not template:
        return faces, None
    xyz = np.asarray(points)
    height = np.ptp(xyz[:, 1])
    if height <= 1e-8:
        return faces, None
    n0, n1 = timer_bounds(target, template)
    native_center = (n0+n1)*.5
    neck = target.centers[target.tracks.get(13, template[0][0]['owner'])]
    chunks = {}
    for i, (mat, face) in enumerate(faces):
        chunks.setdefault(mat, []).append(i)
    candidates = []
    for mat, indices in chunks.items():
        vertices = [corner[0] for i in indices for corner in faces[i][1]]
        p = xyz[vertices]
        low, high = p.min(axis=0), p.max(axis=0)
        c = (low+high)*.5
        named = re.search(r'color.?timer|chest.?lamp|chest.?light', str(mat), re.I)
        chest = (abs(c[0]-neck[0]) < .045*height and -.23*height < c[1]-neck[1] < -.045*height
                 and c[2]-neck[2] > .045*height)
        if len(indices) >= 4 and (high-low).max() < .085*height and (named or chest):
            candidates.append((indices, low, high))
    if candidates:
        _, low, high = min(candidates, key=lambda item: np.linalg.norm((item[1]+item[2])*.5-native_center))
        overlay = [i for ids, a, b in candidates if np.all(np.minimum(b, high) >= np.maximum(a, low))
                   for i in ids]
        removed = set(overlay)
        return [face for i, face in enumerate(faces) if i not in removed], fit_timer(
            template, (n0, n1), low, high)
    # Seat the native lamp on the imported surface at the native attachment.
    near = np.linalg.norm(xyz[:, :2] - native_center[:2], axis=1) < .028 * height
    if not near.any():
        return faces, None
    destination = native_center.copy()
    destination[2] = np.max(xyz[near, 2]) + .3
    return faces, (template, native_center, destination, 1.)


def add_timer(writer, timer, reference=None):
    if timer is None:
        return
    template, center, destination, scale = timer
    reference = reference or writer.rig
    groups = {}
    indices = {}
    for mesh, mat, face in template:
        groups.setdefault(mesh['owner'], (mesh, []))[1].append((mat, face))
        if mat not in indices:
            indices[mat] = len(writer.materials)
            writer.materials.append(reference.model['materials'][mat].copy())
    for bone, (mesh, faces) in groups.items():
        points, palette, weights, _, _ = native_mesh(reference, mesh)
        used, uv, strips = {}, [], []
        for mat, face in faces:
            corners = []
            for corner in face:
                vertex = corner['v']
                used.setdefault(vertex, len(used))
                uv.append(mesh['uv'][corner['uv']])
                corners.append((len(uv)-1, used[vertex]))
            strips.append((indices[mat], tuple(corners)))
        keep = np.fromiter(used, dtype=int)
        weights = weights[keep]
        active = weights.any(axis=0)
        palette = [node for node, yes in zip(palette, active) if yes]
        points = destination + scale * (points[keep]-center)
        owner = writer.reserve(bone, palette, (len(points), 0, 0, len(uv)), len(strips))
        writer.add(owner, {'palette': palette, 'weights': weights[:, active], 'positions': points,
                           'uv': uv, 'normals': [], 'colors': [], 'strips': strips})
