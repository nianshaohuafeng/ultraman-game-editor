"""Triangulate and bind external meshes to a PSP MDL rig."""

from collections import defaultdict

import numpy as np

from fbx_import import map_skin_weights, skin_alignment
from fbx_hands import imported_hands
from fighter_parts import add_hands, add_timer, missing_hands, split_hand, split_timer, timer_faces
from kgf_readers.layouts import FIELDS
from model_retarget import ModelWriter, bounds


Y_UP = np.eye(3)
Z_UP = np.array([(1., 0., 0.), (0., 0., 1.), (0., -1., 0.)])
HALF_TURN = np.diag([-1., 1., -1.])
IMPORT_ROTATIONS = (Y_UP, HALF_TURN, Z_UP, HALF_TURN @ Z_UP)


def triangulate(corners, vertices):
    if len(corners) == 3:
        return [corners]
    if len(corners) > 3 and corners[0][0] == corners[-1][0]:
        corners = corners[:-1]
    points = np.array([vertices[c[0]] for c in corners])
    axes = np.cross(points, np.roll(points, -1, axis=0)).sum(axis=0)
    points = np.delete(points, int(np.argmax(np.abs(axes))), axis=1)
    span = np.ptp(points, axis=0)
    tolerance = max(float(span.max()) ** 2 * 1e-12, 1e-25)

    def signed(a, b, c):
        x, y = points[b] - points[a]
        w, h = points[c] - points[a]
        return x * h - y * w

    # Check the projected winding because dropping the middle axis flips it.
    area = sum(signed(0, i, i + 1) for i in range(1, len(points) - 1))
    if abs(area) <= tolerance:
        return []
    winding = 1 if area > 0 else -1
    remaining = list(range(len(corners)))
    triangles = []
    while len(remaining) > 3:
        for i, b in enumerate(remaining):
            a, c = remaining[i - 1], remaining[(i + 1) % len(remaining)]
            if signed(a, b, c) * winding <= tolerance:
                continue
            if any(all(signed(x, y, p) * winding > tolerance for x, y in ((a, b), (b, c), (c, a)))
                   for p in remaining if p not in (a, b, c)):
                continue
            triangles.append((corners[a], corners[b], corners[c]))
            del remaining[i]
            break
        else:
            raise ValueError('模型包含无法三角化的面。请在 Blender 中三角化该面后重试。')
    triangles.append(tuple(corners[i] for i in remaining))
    return triangles


def distance_squared(query, reference):
    return np.maximum(
        np.sum(query * query, axis=1, keepdims=True)
        + np.sum(reference * reference, axis=1) - 2 * query @ reference.T, 0)


def align_mesh(vertices, normals, reference, body):
    reference_origin, reference_span = bounds(reference)
    sample = body[np.linspace(0, len(body) - 1, min(len(body), 512), dtype=int)]
    guide = reference[np.linspace(0, len(reference) - 1, min(len(reference), 2048), dtype=int)]
    options = []
    for index, rotation in enumerate(IMPORT_ROTATIONS):
        oriented = vertices @ rotation.T
        origin, span = bounds(oriented[body])
        if span[1] <= 1e-12 or reference_span[1] <= 1e-12:
            continue
        scale = reference_span[1] / span[1]
        offset = reference_origin - origin * scale
        result = oriented * scale + offset
        distances = distance_squared(result[sample], guide)
        score = distances.min(axis=1).mean() + distances.min(axis=0).mean()
        # Prefer the usual Blender Y-up export when the silhouette is ambiguous.
        score += index * reference_span[1] ** 2 * .00003
        options.append((score, index, result, normals @ rotation.T, rotation * scale, offset))
    if not options:
        raise ValueError('模型的尺寸或朝向无法与目标模型对齐。')
    return min(options, key=lambda item: item[:2])[2:]


def leg_branches(target, left, right):
    branches = [set(), set()]
    for node, bone in enumerate(target.bones):
        for root, branch in zip((left, right), branches):
            if node == root or bone['parent'] in branch:
                branch.add(node)
    return branches


def leg_regions(target, reference_weights):
    required = (2, 3, 4, 7)
    if not all(track in target.tracks for track in required):
        return None
    left, right, right_knee, left_knee = (target.tracks[track] for track in required)
    a, b = target.centers[[left, right]]
    axis = (a - b)[[0, 2]]
    if np.linalg.norm(axis) < 1e-4:
        return None
    cutoff = min(a[1], b[1]) - .2 * min(abs(a[1] - target.centers[left_knee, 1]),
                                       abs(b[1] - target.centers[right_knee, 1]))
    branches = leg_branches(target, left, right)
    opposite = [np.flatnonzero(reference_weights[:, sorted(branch)].sum(axis=1) > 1e-6)
                for branch in reversed(branches)]
    return ((a + b)[[0, 2]] * .5, axis, cutoff, opposite)


def transfer_weights(points, reference, reference_weights, target):
    results = []
    count = min(3, len(reference))
    regions = leg_regions(target, reference_weights)
    for start in range(0, len(points), 256):
        query = points[start:start + 256]
        distances = distance_squared(query, reference)
        if regions:
            center, axis, cutoff, opposite = regions
            sides = (query[:, [0, 2]] - center) @ axis >= 0
            for positive, excluded in zip((True, False), opposite):
                if len(reference) - len(excluded) >= count:
                    rows = np.flatnonzero((query[:, 1] < cutoff) & (sides == positive))
                    distances[np.ix_(rows, excluded)] = np.inf
        ids = np.argpartition(distances, count - 1, axis=1)[:, :count]
        d = np.take_along_axis(distances, ids, axis=1)
        factors = 1 / np.maximum(d, 1e-8)
        factors /= factors.sum(axis=1, keepdims=True)
        mixed = np.einsum('nb,nbj->nj', factors, reference_weights[ids])
        # Keep each triangle within the PSP palette budget with weights that
        # stay identical at shared vertices, regardless of its draw node.
        joints = np.argsort(-mixed, axis=1, kind='stable')[:, :2]
        for index, scores in zip(joints, mixed):
            row = [(int(node), float(scores[node])) for node in index if scores[node] > 1e-8]
            total = sum(weight for _, weight in row)
            results.append({bone: weight / total for bone, weight in row})
    return results


def relax_bind_weights(points, faces, bind, target):
    weights = np.zeros((len(points), len(target.bones)))
    for vertex, row in enumerate(bind):
        for joint, weight in row.items():
            weights[vertex, joint] = weight

    # Use the same neighbors across UV seams. Keep coincident layers with
    # different bindings separate, such as armor sitting on the body.
    keys = np.column_stack((points.round(5), weights.round(5)))
    _, unique, weld = np.unique(keys, axis=0, return_index=True, return_inverse=True)
    triangles = np.array([[weld[corner[0]] for corner in face] for _, face in faces])
    edges = np.concatenate([triangles[:, side] for side in ((0, 1), (1, 2), (2, 0))])
    edges = np.unique(np.sort(edges, axis=1), axis=0)
    vertices = points[unique]
    lengths = np.linalg.norm(vertices[edges[:, 0]] - vertices[edges[:, 1]], axis=1)
    height = np.ptp(points[:, 1])
    nearby = (lengths > 1e-6) & (lengths < .085 * height)
    pairs = edges[nearby]
    src, dst = np.concatenate([pairs, pairs[:, ::-1]]).T
    influence = np.tile(1. / lengths[nearby], 2)
    totals = np.bincount(src, weights=influence, minlength=len(vertices))[:, None]
    weights = weights[unique]

    allowed = np.ones_like(weights, dtype=bool)
    left, right = (target.tracks.get(track) for track in (2, 3))
    if left is not None and right is not None:
        a, b = target.centers[[left, right]]
        sides = (vertices[:, [0, 2]] - (a + b)[[0, 2]] * .5) @ (a - b)[[0, 2]] >= 0
        below_hips = vertices[:, 1] <= min(a[1], b[1]) - .02 * height
        for side, opposite in zip((True, False), reversed(leg_branches(target, left, right))):
            allowed[np.ix_(below_hips & (sides == side), sorted(opposite))] = False
        allowed |= weights > 0

    # Blend across nearby edges before packing the model's bone palettes.
    for _ in range(7):
        adjacent = np.zeros_like(weights)
        np.add.at(adjacent, src, influence[:, None] * weights[dst])
        average = np.divide(adjacent, totals, out=weights.copy(), where=totals > 0)
        weights = (.4 * weights + .6 * average) * allowed

    rows = []
    for row in weights:
        joints = np.argsort(-row, kind='stable')[:4]
        rows.append({int(joint): float(row[joint]) for joint in joints if row[joint] > .005})
    return [rows[index].copy() for index in weld]


def limit_face_palettes(faces, bind):
    # A draw node can use eight joints. Trim the least influence from the
    # shared vertex so both triangles at an edge keep the same binding.
    for _, triangle in faces:
        vertices = {corner[0] for corner in triangle}
        while len(set().union(*(bind[vertex] for vertex in vertices))) > 8:
            _, vertex, joint = min((weight, vertex, joint) for vertex in vertices if len(bind[vertex]) > 1
                                  for joint, weight in bind[vertex].items())
            del bind[vertex][joint]
    for vertex, row in enumerate(bind):
        total = sum(row.values())
        bind[vertex] = {joint: weight / total for joint, weight in row.items()}


def align_foot_soles(points, bind, reference, reference_weights, target):
    for ankle, toe in ((5, 6), (8, 9)):
        nodes = [target.tracks[track] for track in (ankle, toe) if track in target.tracks]
        if not nodes:
            continue
        native = reference_weights[:, nodes].sum(axis=1) > .6
        influence = np.asarray([sum(row.get(node, 0) for node in nodes) for row in bind])
        sole = influence > .8
        if np.count_nonzero(native) >= 6 and np.count_nonzero(sole) >= 6:
            ground = reference[native, 1].min()
            difference = ground - points[sole, 1].min()
            points[:, 1] += np.clip(influence / .8, 0, 1) * difference


def mesh_to_mdl(geometry, target_bytes, target, rigid_parts=(), skin=None, part_reference=None):
    points, uv, normals, faces, settings = geometry
    reference, weights = target.surface()
    body = np.ones(len(points), dtype=bool)
    for start, end, _ in rigid_parts:
        body[start:end] = False
    fit = np.flatnonzero(body) if body.any() else np.arange(len(points))
    alignment = skin_alignment(skin, target, IMPORT_ROTATIONS) if skin else None
    if alignment is None:
        points, normals, transform, offset = align_mesh(points, normals, reference, fit)
    else:
        transform, offset = alignment
        points = points @ transform.T + offset
        normals = normals @ transform.T / np.linalg.norm(transform[0])
    bind = [None] * len(points)
    if skin:
        bind, shifts = map_skin_weights(skin, target, transform, offset, alignment is not None)
        points += shifts
    for start, end, anchor in rigid_parts:
        if bind[start] is None:
            aligned = anchor @ transform.T + offset
            joint = min(target.joints, key=lambda node: np.linalg.norm(target.centers[node] - aligned))
            bind[start:end] = [{joint: 1.}] * (end - start)
    missing = [i for i, row in enumerate(bind) if row is None]
    projected = transfer_weights(points[missing], reference, weights, target)
    for i, row in zip(missing, projected):
        bind[i] = row
    if alignment is not None:
        # Match the actual boot or claw sole. The ankle joint alone can be
        # much higher in the donor rig, making a fitted mesh sink into the floor.
        align_foot_soles(points, bind, reference, weights, target)
        smoothed = relax_bind_weights(points, faces, bind, target)
        for start, end, _ in rigid_parts:
            smoothed[start:end] = bind[start:end]
        bind = smoothed
    geometry = (points, uv, normals, faces, settings)
    if skin is not None:
        if alignment is not None:
            geometry, bind, owners = imported_hands(geometry, bind, skin, target, transform, offset)
        else:
            owners = [None] * len(faces)
        return bound_mesh_to_mdl(geometry, bind, target_bytes, target, face_owners=owners)
    return bound_mesh_to_mdl(geometry, bind, target_bytes, target, rigid_parts, skin,
                             part_reference, limit_palettes=False)


def bound_mesh_to_mdl(geometry, bind, target_bytes, target, rigid_parts=(), skin=None,
                      part_reference=None, limit_palettes=True, face_owners=None):
    points, uv, normals, faces, settings = geometry
    hand_parts = []
    hands = part_reference if part_reference is not None and missing_hands(target) else target
    lamp = part_reference if part_reference is not None and not timer_faces(target) else target
    if target.hands and face_owners is None:
        points, uv, normals = list(points), list(uv), list(normals)
        for side in range(len(hands.hands)):
            faces, variants = split_hand(points, uv, normals, faces, bind, side, hands, skin, rigid_parts)
            if variants is not None:
                hand_parts.append(variants)
        points, normals = np.asarray(points), np.asarray(normals)
    timer = None
    if face_owners is None:
        faces, timer = split_timer(points, faces, lamp)
        face_owners = [None] * len(faces)
    if hand_parts or limit_palettes:
        limit_face_palettes(faces, bind)
    names = list(dict.fromkeys([name for name, _ in faces] + [hand[-2] for hand in hand_parts]))
    materials = []
    for name in names:
        m = settings[name]
        if 'kind' in m:
            materials.append(m)
            continue
        components = (*m['Kd'], m['d'][0])
        tint = sum(round(min(1, max(0, v)) * 255) << (i * 8) for i, v in enumerate(components))
        alpha = tint >> 24
        materials.append({'kind': 1, 'ambient': 0xFFFFFFFF, 'diffuse': tint, 'specular': 0,
                          'shininess': 0., 'texture': m.get('slot', -1), 'variant': 0,
                          'flags': 0x100 | m.get('blend', alpha < 255)})
    mat_ids = {name: index for index, name in enumerate(names)}
    writer = ModelWriter(target_bytes, target, materials)
    groups = defaultdict(list)
    for (name, triangle), fixed in zip(faces, face_owners):
        a, b, c = (points[v] for v, _, _ in triangle)
        normal = np.cross(b - a, c - a)
        length = np.linalg.norm(normal)
        if length <= 1e-10:
            continue
        votes = defaultdict(float)
        for v, _, _ in triangle:
            for bone, weight in bind[v].items():
                votes[bone] += weight
        preferred = max(votes, key=votes.get)
        owner = writer.reserve(preferred, votes, (3, 3, 0, 3), 1, fixed=fixed)
        groups[owner].append((mat_ids[name], triangle, normal / length))
    if not groups:
        raise ValueError('模型的面全部退化成线或点。')

    for owner, group in groups.items():
        palette = sorted(writer.palettes[owner])
        positions, rows, directions, coords, strips = [], [], [], [], []
        vertices, uv_ids, normal_ids = {}, {}, {}
        for material, triangle, normal in group:
            corners = []
            fields = FIELDS[materials[material]['kind']]
            for v, tex, n in triangle:
                if v not in vertices:
                    vertices[v] = len(positions)
                    positions.append(points[v])
                    rows.append([bind[v].get(bone, 0.) for bone in palette])
                if tex not in uv_ids:
                    uv_ids[tex] = len(coords)
                    coords.append((uv[tex][0], 1. - uv[tex][1]) if tex is not None else (0., 0.))
                if 'n' in fields:
                    key = n if n is not None else tuple(normal)
                    if key not in normal_ids:
                        normal_ids[key] = len(directions)
                        directions.append(normals[n] if n is not None else normal)
                    corners.append((uv_ids[tex], normal_ids[key], vertices[v]))
                else:
                    corners.append((uv_ids[tex], vertices[v]))
            strips.append((material, corners))
        writer.add(owner, {'palette': palette, 'positions': positions, 'weights': rows,
                           'normals': directions, 'colors': [], 'uv': coords, 'strips': strips})
    add_hands(writer, hand_parts, mat_ids)
    add_timer(writer, timer, lamp)
    return writer.finish()

