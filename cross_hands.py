"""Combine authored PS2 finger deformations for PSP hand shapes absent from VAU."""
from collections import defaultdict
from itertools import combinations
import heapq

import numpy as np


def finger_regions(points, triangles, wrist, axis):
    """Identify separated distal fingers on the evaluated VAU surface."""
    unique, inverse = np.unique(np.around(points, 3), axis=0, return_inverse=True)
    faces = inverse[triangles]
    edges = np.unique(np.sort(faces[:, [0, 1, 1, 2, 2, 0]].reshape(-1, 2), axis=1), axis=0)
    along = (unique - wrist) @ axis
    reach = max(float(np.max(along)), 1e-6)
    adjacency = [[] for _ in unique]
    for a, b in edges:
        if a != b:
            length = float(np.linalg.norm(unique[a] - unique[b]))
            adjacency[a].append((b, length))
            adjacency[b].append((a, length))
    options = []
    for cutoff in np.linspace(.43, .76, 22) * reach:
        pending = set(np.flatnonzero(along >= cutoff))
        groups = []
        while pending:
            queue = [pending.pop()]
            group = set(queue)
            while queue:
                node = queue.pop()
                for child, _ in adjacency[node]:
                    if child in pending:
                        pending.remove(child)
                        group.add(child)
                        queue.append(child)
            if len(group) >= 5:
                groups.append(group)
        if 2 <= len(groups) <= 5:
            # Broadly separated distal components identify full fingers.
            rank = len(groups), min(map(len, groups)), sum(map(len, groups)), -cutoff
            options.append((rank, groups, cutoff))
    if not options:
        return None
    rank, groups, cutoff = max(options, key=lambda row: row[0])
    distance = []
    for group in groups:
        result = np.full(len(unique), np.inf)
        result[list(group)] = 0
        queue = [(0., node) for node in group]
        heapq.heapify(queue)
        while queue:
            value, node = heapq.heappop(queue)
            if value != result[node]:
                continue
            for child, length in adjacency[node]:
                next_value = value + length
                if next_value < result[child]:
                    result[child] = next_value
                    heapq.heappush(queue, (next_value, child))
        distance.append(result)
    labels = np.argmin(distance, axis=0)
    # Keep the palm and common wrist edge in the original sample.
    amount = np.clip((along - .66 * cutoff) / (.34 * cutoff), 0, 1)
    amount = amount * amount * (3 - 2 * amount)
    weights = np.zeros((len(unique), len(groups)))
    weights[np.arange(len(unique)), labels] = amount
    return rank, unique, weights


def hand_combinations(model, mapping, rig, poses, regions, evaluate):
    """Keep authored VAU samples and add combinations of their finger controls.

    ELF 0x32B198 writes local control-point vectors. Mix those vectors at the
    detected fingers, leaving the shared palm and wrist boundary in place.
    """
    output = {side: list(samples) for side, samples in poses.items()}
    for side, samples in poses.items():
        if len(samples) < 2:
            continue
        layouts = []
        all_faces = []
        position = 0
        for mesh_id, mesh in enumerate(model['meshes']):
            region = [triangle for (_, triangle), hand in regions[mesh_id].items() if hand == side]
            vertices = sorted({mesh['source'][corner] for triangle in region for corner in triangle})
            if vertices:
                indices = {vertex: i + position for i, vertex in enumerate(vertices)}
                all_faces.extend(tuple(indices[mesh['source'][c]] for c in tri) for tri in region)
                layouts.append((mesh, vertices))
                position += len(vertices)
        if not layouts:
            continue
        coordinates = [np.concatenate([evaluate(mesh, mapping, sample)[vertices]
                                       for mesh, vertices in layouts]) for sample in samples]
        wrist = rig.centers[rig.tracks[(18, 22)[side]]]
        elbow = rig.centers[rig.tracks[(17, 21)[side]]]
        axis = wrist - elbow
        axis /= max(float(np.linalg.norm(axis)), 1e-10)
        groups = [finger_regions(cloud, np.asarray(all_faces), wrist, axis) for cloud in coordinates]
        valid = [(row[0], i) for i, row in enumerate(groups) if row is not None]
        if not valid:
            continue
        _, partition = max(valid)
        _, reference, weights = groups[partition]
        offsets = defaultdict(list)
        for mesh, _ in layouts:
            surface = evaluate(mesh, mapping, samples[partition])
            for vertex, offset in enumerate(mesh['controlOffsets']):
                if any(offset in pose for pose in samples):
                    index = int(np.argmin(np.sum((reference - surface[vertex]) ** 2, axis=1)))
                    offsets[offset].append(weights[index])
        masks = {offset: np.mean(rows, axis=0) for offset, rows in offsets.items()}
        # Compare the authored poses in wrist coordinates, independently for each side.
        def reach(cloud):
            along = (cloud - wrist) @ axis
            return float(np.quantile(along, .90))

        closed = min(range(len(samples)), key=lambda i: float(np.mean(
            np.sum((coordinates[i] - wrist) ** 2, axis=1))))
        # The straight, aligned hand has the smallest distal transverse spread.
        maximum = max(reach(cloud) for cloud in coordinates)
        extended = [i for i, cloud in enumerate(coordinates) if reach(cloud) >= maximum * .92]

        def spread(i):
            cloud = coordinates[i] - wrist
            along = cloud @ axis
            tips = cloud[along >= .64 * max(along)]
            radial = tips - (tips @ axis)[:, None] * axis
            return float(np.mean(np.sum((radial - radial.mean(axis=0)) ** 2, axis=1)))

        opened = min(extended, key=spread)
        start, end = samples[closed], samples[opened]
        total = weights.shape[1]
        for count in range(1, total):
            for selected in combinations(range(total), count):
                sample = dict(start)
                for offset, mask in masks.items():
                    amount = float(sum(mask[i] for i in selected))
                    if amount > 0 and offset in start and offset in end:
                        sample[offset] = tuple(np.asarray(start[offset]) * (1 - amount)
                                               + np.asarray(end[offset]) * amount)
                output[side].append(sample)
    return output

def fe3_hand_clouds(game, move, skeleton, receiving):
    """Evaluate the VAU selectors on the actual MDL surface in wrist coordinates."""
    from fe3_preview import surfaces
    from fe3_workbench import morph_pose

    form = game.workbench.form(move.role, move.shape)
    result = [{}, {}]
    if not form.vau:
        return result
    vau = game.data(form.vau)
    samples = [morph_pose(vau, i) for i in range(int.from_bytes(vau[8:12], 'little'))]
    geometry, _ = surfaces(game.data(form.model))
    hand_of = [skeleton.ancestor(node, {18, 22}) for node in range(len(skeleton.bones))]
    offsets_by_track = defaultdict(set)
    for _, _, owners, offsets, *_ in geometry:
        for owner, offset in zip(owners, offsets):
            offsets_by_track[hand_of[owner]].add(offset)
    for side, track in enumerate((18, 22)):
        if track not in skeleton.tracks:
            continue
        own = offsets_by_track[track]
        other = offsets_by_track[None] | offsets_by_track[(22, 18)[side]]
        choices = {i:pose for i, pose in enumerate(samples) if not own.isdisjoint(pose)
                   and not (receiving and not other.isdisjoint(pose))}
        if not choices:
            continue
        affected = own.intersection(set().union(*(pose.keys() for pose in choices.values())))
        relative = np.linalg.solve(skeleton.world[skeleton.tracks[track]], skeleton.world)
        clouds = defaultdict(list)
        for _, local, owners, offsets, rules, strips, _ in geometry:
            marked = [off in affected and hand_of[node] == track for off, node in zip(offsets, owners)]
            if not any(marked):
                continue
            for terms in rules:
                marked.append(any(marked[i] and weight for i, weight in zip(terms[::2], terms[1::2])))
            visible = set()
            for _, _, _, corners in strips:
                indices = [corner[2] for corner in corners]
                for tri in zip(indices, indices[1:], indices[2:]):
                    if len(set(tri)) == 3 and any(marked[v] for v in tri):
                        visible.update(tri)
            if visible:
                vertices = sorted(visible)
                matrices = relative[owners]
                for i, sample in choices.items():
                    xyz = surface_points(local, offsets, rules, matrices, sample)
                    clouds[i].append(xyz[vertices])
        result[side] = {i:np.concatenate(rows) for i, rows in clouds.items()}
    return result


def surface_points(local, offsets, rules, transforms, morph=None):
    local = np.asarray(local).reshape(-1, 3)
    if morph:
        local = np.asarray([morph.get(offset, pos) for offset, pos in zip(offsets, local)])
    points = list(np.einsum('nij,nj->ni', transforms[:, :3, :3], local) + transforms[:, :3, 3])
    for terms in rules:
        points.append(sum(points[i] * w for i, w in zip(terms[::2], terms[1::2])))
    return np.asarray(points)


def psp_hand_clouds(game, move):
    from fighter_parts import native_mesh
    from kgf_readers.mdl_reader import read_mdl

    selection = move.role, move.config, move.shape
    form = game.form_binding(*selection)[1]
    model = read_mdl(game.read_member(form['mdl']['id'], b'MDL\0'))
    rig = game.model_rig(model, selection)
    meshes = {m['owner']:m for m in model['meshes'] if m['faces']}
    result = [{}, {}]
    for side, slots in enumerate(rig.hands):
        wrist = rig.tracks.get((18, 22)[side])
        if wrist is None:
            continue
        transform = rig.world[wrist]
        for slot, node in enumerate(slots):
            if node in meshes:
                points = native_mesh(rig, meshes[node])[0] - transform[:3, 3]
                result[side][6 * side + slot] = np.linalg.solve(transform[:3, :3], points.T).T
        if result[side]:
            # A missing node in an otherwise switchable group hides that hand.
            for slot, node in enumerate(slots):
                if node == -1:
                    result[side][6 * side + slot] = np.empty((0, 3))
    return result


def match_hand_events(donor, receiver, same_engine):
    """Index target selectors by donor selector and side. Different MDLs may order them differently."""
    def prepare(poses):
        distinct = {key:np.unique(np.round(points, 4), axis=0) for key, points in poses.items()}
        span = max(np.linalg.norm(points, axis=1).max() for points in distinct.values())
        return {key:points / max(span, 1e-8) for key, points in distinct.items()}

    mapping = defaultdict(list)
    for side, (source, target) in enumerate(zip(donor, receiver)):
        if not source or not target:
            continue
        empty = [slot for slot, points in target.items() if not len(points)]
        for key, points in source.items():
            if not len(points) and empty:
                slot = key if same_engine and key in empty else empty[0]
                mapping[key].append((side, slot))
        target = prepare({slot:points for slot, points in target.items() if len(points)})
        for key, points in prepare({k:p for k,p in source.items() if len(p)}).items():
            def score(slot):
                distance = np.sum((points[:, None] - target[slot][None, :]) ** 2, axis=2)
                return distance.min(axis=1).mean() + distance.min(axis=0).mean(), not (same_engine and key == slot), slot
            mapping[key].append((side, min(target, key=score)))
    return mapping

