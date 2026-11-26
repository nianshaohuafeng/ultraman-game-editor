"""Fit the recipient's hand variants using the FBX's original digit rig and mesh."""

from collections import defaultdict
import math
import re

import numpy as np

from ogeditor.models.fbx_import import joint_label, joint_track
from ogeditor.models.fighter_parts import native_mesh


DIGIT = re.compile(
    r'(?:left|right|l|r)?(?:hand)?(oya|thumb|hito|index|naka|middle|kusu|ring|koyu|pinky|little)(\d+)(?:l|r)?')
FINGER = {'oya':0, 'thumb':0, 'hito':1, 'index':1, 'naka':2, 'middle':2,
          'kusu':3, 'ring':3, 'koyu':4, 'pinky':4, 'little':4}


def unit(value):
    return value / max(np.linalg.norm(value), 1e-10)


def rotation(axis, angle):
    x, y, z = axis
    cross = np.array(((0, -z, y), (z, 0, -x), (-y, x, 0)))
    return np.eye(3) + math.sin(angle) * cross + (1 - math.cos(angle)) * (cross @ cross)


def finger_groups(parents):
    groups = defaultdict(lambda:defaultdict(list))
    for name in parents:
        match = DIGIT.fullmatch(joint_label(name))
        if match is None:
            continue
        ancestor = parents[name]
        while ancestor in parents:
            if joint_track(ancestor) in (18, 22):
                groups[ancestor][FINGER[match[1]]].append((int(match[2]), name))
                break
            ancestor = parents[ancestor]
    return {wrist:{finger:[name for _, name in sorted(rows)] for finger, rows in shapes.items()}
            for wrist, shapes in groups.items()}


def curls(rig, side, wrist):
    meshes = {m['owner']:m for m in rig.model['meshes'] if m['faces']}
    elbow = rig.tracks.get((17, 21)[side])
    if elbow is None:
        return {}
    center = rig.centers[wrist]
    direction = unit(center - rig.centers[elbow])
    extents = {}
    for owner in rig.hands[side]:
        if owner in meshes:
            points = native_mesh(rig, meshes[owner])[0]
            extents[owner] = float(np.quantile((points - center) @ direction, .9))
    if len(extents) < 2:
        return {}
    shortest, longest = min(extents.values()), max(extents.values())
    if longest - shortest < 1.0:
        return {}
    return {owner:(longest - reach) / (longest - shortest) for owner, reach in extents.items()}


def rotations_for_hand(chains, centers, curl):
    fore = unit(centers[chains[2][-1]] - centers[chains[2][0]])
    width = unit(centers[chains[1][0]] - centers[chains[4][0]])
    palm = unit(np.cross(fore, width))
    thumb = centers[chains[0][0]] if 0 in chains else centers[chains[2][0]] + (0, -1, 0)
    if np.dot(palm, thumb - centers[chains[2][0]]) < 0:
        palm = -palm
    result = {}
    for finger, chain in chains.items():
        direction = unit(centers[chain[-1]] - centers[chain[0]])
        axis = unit(np.cross(direction, palm))
        transform = np.eye(3)
        offset = np.zeros(3)
        for index, name in enumerate(chain):
            angle = (0.64, 1.04, 0.62)[min(index, 2)] * curl
            if finger == 0:
                angle *= .52
            rot = rotation(axis, angle)
            pivot = centers[name]
            offset += transform @ (pivot - rot @ pivot)
            transform = transform @ rot
            result[name] = transform, offset.copy()
    return result


def imported_hands(geometry, bind, skin, target, transform, offset):
    points, uv, normals, faces, settings = geometry
    parents, centers, skin_rows = skin
    groups = finger_groups(parents)
    owners = [None] * len(faces)
    if not groups or not target.hands:
        return geometry, bind, owners
    anchor = {name:transform @ center + offset for name, center in centers.items()}
    regions = {}
    for wrist, chains in groups.items():
        if not all(finger in chains and len(chains[finger]) >= 2 for finger in (1, 2, 3, 4)):
            continue
        sides = [(side, target.tracks[track]) for side, track in enumerate((18, 22)) if track in target.tracks]
        if not sides:
            continue
        side, node = min(sides, key=lambda item:np.linalg.norm(anchor[wrist] - target.centers[item[1]]))
        variants = curls(target, side, node)
        if not variants:
            continue
        names = {name for chain in chains.values() for name in chain}
        vertices = {index for index, row in enumerate(skin_rows) if names.intersection(row) and bind[index].get(node, 0) > .25}
        if not vertices or side in regions:
            continue
        corrections = target.centers[node] - anchor[wrist]
        pivots = {name:anchor[name] + corrections for name in names}
        regions[side] = chains, variants, vertices, pivots

    if not regions:
        return geometry, bind, owners
    if len(regions) == 2:
        left, right = (regions[side][2] for side in (0, 1))
        for _, triangle in faces:
            vertices = {corner[0] for corner in triangle}
            if vertices & left and vertices & right:
                left.difference_update(vertices)
                right.difference_update(vertices)

    static, dynamic = [], defaultdict(list)
    for material, triangle in faces:
        sides = [side for side, (_, _, vertices, _) in regions.items() if any(c[0] in vertices for c in triangle)]
        if len(sides) == 1:
            dynamic[sides[0]].append((material, triangle))
        else:
            static.append((material, triangle))

    output, directions, weights = list(points), list(normals), list(bind)
    owners = [None] * len(static)
    for side, triangles in dynamic.items():
        chains, variants, moved_vertices, pivots = regions[side]
        vertices = {corner[0] for _, tri in triangles for corner in tri}
        for owner, curl in variants.items():
            rotations = rotations_for_hand(chains, pivots, curl)
            remap, axes = {}, {}
            for index in vertices:
                matrix = np.eye(3)
                shift = np.zeros(3)
                row = skin_rows[index] if index in moved_vertices else {}
                total = sum(row.values()) or 1.
                for bone, weight in row.items():
                    moved = rotations.get(bone)
                    if moved is not None:
                        linear, translation = moved
                        matrix += weight / total * (linear - np.eye(3))
                        shift += weight / total * translation
                remap[index] = len(output)
                axes[index] = matrix
                output.append(matrix @ points[index] + shift)
                weights.append(bind[index])
            directions_for_corner = {}
            for material, triangle in triangles:
                mapped = []
                for vertex, texcoord, normal in triangle:
                    if normal is not None:
                        key = vertex, normal
                        if key not in directions_for_corner:
                            directions_for_corner[key] = len(directions)
                            directions.append(unit(axes[vertex] @ normals[normal]))
                        normal = directions_for_corner[key]
                    mapped.append((remap[vertex], texcoord, normal))
                static.append((material, tuple(mapped)))
                owners.append(owner)
    return (np.asarray(output), uv, np.asarray(directions), static, settings), weights, owners
