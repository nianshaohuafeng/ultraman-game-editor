"""Bind imported surfaces to the target PSP skeleton and build native MDL blocks."""

import math
import struct

import numpy as np

from ogeditor.readers import read_mdl
from kgf_readers.layouts import FIELDS
from kgf_readers.mdl_reader import align
from kgf_readers.mesh_merge import decode_block, merge_blocks
from kgf_readers.native_writer import ensure_skin_drivers, pack_block


def topology(model):
    return tuple((node['parent'], node['sibling'], node['child']) for node in model['bones'])


def is_shadow(model, mesh):
    return model['materials'][mesh['material_indices'][0]]['kind'] in (3, 8)


def rigid_inverse(matrix):
    result = np.eye(4)
    result[:3, :3] = matrix[:3, :3].T
    result[:3, 3] = -result[:3, :3] @ matrix[:3, 3]
    return result


def local_matrix(node):
    # 089048AC builds Rz @ Ry @ Rx, then scales the three basis columns.
    x, y, z = node['rotation']
    sx, sy, sz = map(math.sin, (x, y, z))
    cx, cy, cz = map(math.cos, (x, y, z))
    rotation = np.array([
        (cz * cy, cz * sy * sx - sz * cx, cz * sy * cx + sz * sx),
        (sz * cy, sz * sy * sx + cz * cx, sz * sy * cx - cz * sx),
        (-sy, cy * sx, cy * cx),
    ])
    local = np.eye(4)
    local[:3, :3] = rotation * node['scale']
    local[:3, 3] = node['translation']
    return local


def transform_points(matrix, points):
    points = np.asarray(points, dtype=float)
    return np.einsum('...ij,...j->...i', matrix[..., :3, :3], points) + matrix[..., :3, 3]


def transform_normals(matrix, normals):
    normals = np.einsum('...ij,...j->...i', matrix[..., :3, :3], normals)
    return normals / np.maximum(np.linalg.norm(normals, axis=-1, keepdims=True), 1e-12)


def normalized_weights(rows):
    weights = np.maximum(np.asarray(rows, dtype=float), 0)
    total = weights.sum(axis=1, keepdims=True)
    return np.divide(weights, total, out=np.zeros_like(weights), where=total > 1e-10)


def mesh_weights(mesh):
    palette = list(mesh['palette'])
    owner = mesh['owner']
    if not palette:
        return [owner], np.ones((len(mesh['positions']), 1))
    rows = normalized_weights(mesh['weights'])
    empty = ~rows.any(axis=1)
    if empty.any():
        if owner not in palette:
            palette.append(owner)
            rows = np.pad(rows, ((0, 0), (0, 1)))
        rows[empty, palette.index(owner)] = 1
    return palette, rows


class Rig:
    def __init__(self, model, tracks, hands=(), parts=(), timer=None, matrix_sources=()):
        self.model = model
        self.bones = model['bones']
        self.tracks = tracks
        self.hands = hands
        self.timer = timer
        self.parts = set(parts)
        self.shadow = {mesh['owner'] for mesh in model['meshes'] if is_shadow(model, mesh)}
        self.hand_nodes = {node for side in hands for node in side if node >= 0}
        self.matrix_sources = dict(matrix_sources)
        self.driven_nodes = set(tracks.values()) | self.hand_nodes | self.parts
        for mesh in model['meshes']:
            if mesh['faces']:
                self.driven_nodes.add(mesh['owner'])
                self.driven_nodes.update(mesh['palette'])
        roots, world, relative = [], [], []
        for index, node in enumerate(self.bones):
            parent = node['parent']
            roots.append(index if parent < 0 else roots[parent])
            local = local_matrix(node)
            world.append(local if parent < 0 else world[parent] @ local)
            relative.append(rigid_inverse(world[roots[index]]) @ world[-1])
        self.world = np.array(world)
        self.relative = np.array(relative)
        self.skin = np.array([m @ rigid_inverse(r) for m, r in zip(world, relative)])
        self.centers = self.world[:, :3, 3]
        self.unit_scale = all(np.allclose(node['scale'], 1, atol=1e-6) for node in self.bones)
        body_roots = {roots[mesh['owner']] for mesh in model['meshes']
                      if mesh['owner'] not in self.shadow and mesh['faces']}
        body_roots.update(roots[node] for node in tracks.values() if node not in self.shadow)
        self.body_nodes = [node for node, root in enumerate(roots)
                           if root in body_roots and node not in self.shadow]
        self.joints = list(dict.fromkeys(node for node in tracks.values() if node in self.body_nodes))
        if not self.joints:
            self.joints = self.body_nodes

    def mesh_matrix(self, owner, palette, weights):
        if not palette or self.unit_scale:
            return self.world[owner]
        return np.einsum('vi,ijk->vjk', weights, self.skin[list(palette)]) @ self.relative[owner]

    def surface(self):
        """Return reference vertices and weights in the native bind pose."""
        points, weights = [], []
        hand_alternates = self.hand_nodes - {next((n for n in side if n >= 0), -1) for side in self.hands}
        ignored = self.shadow | self.parts | hand_alternates
        for mesh in self.model['meshes']:
            owner = mesh['owner']
            if owner in ignored or not mesh['faces']:
                continue
            palette, rows = mesh_weights(mesh)
            matrix = self.mesh_matrix(owner, palette if mesh['palette'] else (), rows)
            points.append(transform_points(matrix, mesh['positions']))
            dense = np.zeros((len(rows), len(self.bones)))
            for column, node in enumerate(palette):
                dense[:, node] += rows[:, column]
            weights.append(dense)
        if not points:
            raise ValueError('模型没有可用于对齐的主体网格。')
        return np.concatenate(points), np.concatenate(weights)


def bounds(points):
    low, high = points.min(axis=0), points.max(axis=0)
    center = (low + high) * .5
    center[1] = low[1]
    return center, high - low


def preserve_neck_shape(target, mapping, neck, shifts):
    if neck not in shifts:
        return
    chest, head = (target.tracks.get(track) for track in (10, 14))
    head_nodes = set()
    for node, bone in enumerate(target.bones):
        if node == head or bone['parent'] in head_nodes:
            head_nodes.add(node)
    # Move the collar and head together with the neck in the bind pose.
    # Each keeps its own animation driver and the donor's local proportions.
    for source, dest in mapping.items():
        if dest == chest or dest in head_nodes:
            shifts[source] = shifts[neck]


def block_bytes(data, mesh):
    offset = mesh['offset']
    size, = struct.unpack_from('<I', data, offset)
    return data[offset:offset + size]


def shift_materials(block, materials, delta):
    output = bytearray(block)
    _, nv, nn, nc, nu, ns = struct.unpack_from('<6I', output)
    nm, nw = struct.unpack_from('<2H', output, 28)
    for offset in range(32, 32 + 2*nm, 2):
        mat, = struct.unpack_from('<H', output, offset)
        struct.pack_into('<H', output, offset, mat + delta)
    position = align(align(32 + 2*nm) + 2*nw)
    normal = align(position + nv*(16 + 4*nw))
    offset = align(align(normal + 16*nn + 4*nc) + 8*nu)
    for _ in range(ns):
        mat, corners = struct.unpack_from('<HH', output, offset)
        struct.pack_into('<H', output, offset, mat + delta)
        offset = align(offset + 4 + 2 * corners * len(FIELDS[materials[mat]['kind']]))
    return bytes(output)


class ModelWriter:
    def __init__(self, target_bytes, target, materials):
        self.target_bytes = target_bytes
        self.rig = target
        self.materials = list(materials)
        self.groups = [[] for _ in target.bones]
        self.reserved = target.shadow | target.parts | target.hand_nodes
        # Fighter initialization can hide baked pieces by node index. Put
        # body faces on animation joints or existing skin deformation joints.
        drivers = set(target.joints)
        for mesh in target.model['meshes']:
            if mesh['owner'] not in self.reserved and mesh['faces']:
                drivers.update(mesh['palette'])
        self.draw_nodes = [node for node in target.body_nodes
                           if node in drivers and node not in self.reserved]
        self.palettes = [set() for _ in target.bones]
        self.sizes = np.zeros((len(target.bones), 4), dtype=np.int64)
        self.strip_counts = [0] * len(target.bones)
        # Keep the target's projected geometry and any separately selectable parts.
        keep = target.shadow | target.parts
        if keep:
            shift = len(self.materials)
            self.materials.extend(target.model['materials'])
            for mesh in target.model['meshes']:
                owner = mesh['owner']
                if owner in keep:
                    block = block_bytes(target_bytes, mesh)
                    self.groups[owner].append(shift_materials(block, target.model['materials'], shift))

    def reserve(self, preferred, palette, counts, strips, fixed=None):
        """Choose a draw node with room for all the vertex influences."""
        palette = set(palette)
        candidates = (fixed,) if fixed is not None else self.draw_nodes
        # The native compiler builds a GE command list in a 64 KiB scratch area
        # for each material run. Keep each owner below 4096 strip commands.
        def available(node):
            return (len(self.palettes[node] | palette) <= 8
                    and self.strip_counts[node] + strips <= 4000
                    and all(self.sizes[node] + counts <= 32767))

        if preferred in candidates and available(preferred):
            owner = preferred
        else:
            possible = [node for node in candidates if available(node)]
            if not possible:
                raise ValueError('网格的条带或骨骼权重超出此目标 MDL 的绘制节点容量。')
            origin = self.rig.centers[preferred]
            # Reuse existing palette entries before opening a new combination
            # on another node. This leaves room for later body triangles.
            owner = min(possible, key=lambda node: (len(palette - self.palettes[node]),
                                                    math.dist(self.rig.centers[node], origin), node))
        self.palettes[owner].update(palette)
        self.sizes[owner] += counts
        self.strip_counts[owner] += strips
        return owner

    def add(self, owner, data):
        palette, rows = data['palette'], np.asarray(data['weights'])
        matrix = self.rig.mesh_matrix(owner, palette, rows)
        inverse = np.linalg.inv(matrix)
        positions = transform_points(inverse, data['positions'])
        normals = np.asarray(data['normals'], dtype=float)
        strips = data['strips']
        if len(normals):
            if inverse.ndim == 2:
                normals = transform_normals(inverse, normals)
            else:
                strips, normals = adjust_normals(strips, self.materials, normals, inverse)
        self.groups[owner].append(pack_block(
            {**data, 'positions': positions, 'normals': normals, 'strips': strips}, self.materials))

    def finish(self):
        ensure_skin_drivers(self.groups, self.materials)
        target = self.target_bytes
        old_nodes, = struct.unpack_from('<I', target, 20)
        output = bytearray(target[:32])
        for m in self.materials:
            output.extend(struct.pack('<4If3I', m['kind'], m['ambient'], m['diffuse'], m['specular'],
                                      m['shininess'], m['texture'] & 0xFFFFFFFF, m['variant'], m['flags']))
        table = len(output)
        output.extend(target[old_nodes:old_nodes + len(self.groups) * 64])
        start = len(output)
        for owner, blocks in enumerate(self.groups):
            record = table + owner * 64
            node_type = self.rig.bones[owner]['node_type'] or 1
            struct.pack_into('<H', output, record + 48, node_type if blocks else 0)
            struct.pack_into('<I', output, record + 60, len(output))
            if blocks:
                output.extend(merge_blocks(blocks, self.materials, owner))
        struct.pack_into('<I', output, 4, len(output) + 8)
        struct.pack_into('<HH', output, 10, 1, len(self.materials))
        struct.pack_into('<III', output, 20, table, start, len(output))
        read_mdl(output)
        return bytes(output)


def adjust_normals(strips, materials, normals, matrices):
    new_strips, values, index = [], [], {}
    for material, corners in strips:
        fields = FIELDS[materials[material]['kind']]
        if 'n' not in fields:
            new_strips.append((material, corners))
            continue
        n, p = fields.index('n'), fields.index('v')
        changed = []
        for corner in corners:
            key = corner[n], corner[p]
            if key not in index:
                index[key] = len(values)
                values.append(transform_normals(matrices[key[1]], normals[key[0]]))
            changed.append((*corner[:n], index[key], *corner[n + 1:]))
        new_strips.append((material, changed))
    return new_strips, values


def match_joints(source, target, scale, src_origin, dst_origin):
    # ANM logical IDs correspond even when serialized node order differs.
    matched = {node: target.tracks[track] for track, node in source.tracks.items()
               if track in target.tracks and target.tracks[track] in target.body_nodes}
    projected = (source.centers - src_origin) * scale + dst_origin
    shifts = {node: target.centers[dest] - projected[node] for node, dest in matched.items()}
    preserve_neck_shape(target, matched, source.tracks.get(13), shifts)
    mapping, displacements = [], []
    for index, node in enumerate(source.bones):
        parent = node['parent']
        if index in matched:
            dest, offset = matched[index], shifts[index]
        elif parent >= 0 and matched:
            dest, offset = mapping[parent], displacements[parent]
        else:
            dest = min(target.joints, key=lambda joint: math.dist(target.centers[joint], projected[index]))
            offset = np.zeros(3)
        mapping.append(dest)
        displacements.append(offset)
    return mapping, np.array(displacements)


def match_hands(source, target):
    result = {}
    visible = {mesh['owner'] for mesh in source.model['meshes'] if mesh['faces']}
    for side, src in enumerate(source.hands):
        dest = target.hands[side] if side < len(target.hands) else ()
        available = [n for n in src if n >= 0 and n in visible]
        if not available:
            continue
        choices = {node: [] for node in available}
        for index, node in enumerate(dest):
            if node >= 0:
                old = src[index] if index < len(src) and src[index] in choices else available[0]
                if not any(node in assigned for assigned in choices.values()):
                    choices[old].append(node)
        if not any(choices.values()):
            choices[available[0]].append(None)
        result.update(choices)
    return result


def retarget_model(target_bytes, target, data, source, materials):
    from ogeditor.models.fighter_parts import add_timer, fit_timer, timer_bounds, timer_faces

    reference, _ = target.surface()
    donor, _ = source.surface()
    dst_origin, dst_size = bounds(reference)
    src_origin, src_size = bounds(donor)
    scale = dst_size[1] / src_size[1] if src_size[1] > 1e-6 and dst_size[1] > 1e-6 else 1.0
    mapping, shift = match_joints(source, target, scale, src_origin, dst_origin)
    hand_slots = match_hands(source, target)
    writer = ModelWriter(target_bytes, target, materials)
    timer = timer_faces(target) if source.timer is not None else []
    timer_materials = {i for i, mat in enumerate(source.model['materials'])
                       if mat['kind'] == 0 and (mat['texture'], mat['variant']) == source.timer}
    timer_points = []
    for mesh in source.model['meshes']:
        owner = mesh['owner']
        if owner in source.shadow or owner in source.parts or not mesh['faces']:
            continue
        slots = hand_slots.get(owner, (None,))
        if not slots:
            continue
        block = decode_block(block_bytes(data, mesh), source.model['materials'])
        bones, rows = mesh_weights(mesh)
        matrix = source.mesh_matrix(owner, bones if block['palette'] else (), rows)
        world = transform_points(matrix, np.asarray(block['pos'])[:, :3])
        world = (world - src_origin) * scale + dst_origin + rows @ shift[bones]
        palette = list(dict.fromkeys(mapping[node] for node in bones))
        remapped = np.zeros((len(rows), len(palette)))
        for index, node in enumerate(bones):
            remapped[:, palette.index(mapping[node])] += rows[:, index]
        active = remapped.any(axis=0)
        palette = [bone for bone, used in zip(palette, active) if used]
        remapped = remapped[:, active]
        strips, normals = block['strips'], np.asarray(block['normals'])[:, :3] if block['normals'] else []
        if timer:
            for mat, corners in strips:
                if mat in timer_materials:
                    timer_points.extend(world[corner[-1]] for corner in corners)
            strips = [(mat, corners) for mat, corners in strips if mat not in timer_materials]
            if not strips:
                continue
        if len(normals):
            if matrix.ndim == 2:
                normals = transform_normals(matrix, normals)
            else:
                strips, normals = adjust_normals(strips, materials, normals, matrix)
        geometry = {'palette': palette, 'positions': world, 'weights': remapped,
                    'normals': normals, 'colors': block['colors'], 'uv': block['uv'], 'strips': strips}
        sizes = np.array([len(world), len(normals), len(block['colors']), len(block['uv'])])
        for fixed in slots:
            dest = writer.reserve(mapping[owner], palette, sizes, len(strips), fixed)
            writer.add(dest, geometry)
    if timer_points:
        points = np.asarray(timer_points)
        add_timer(writer, fit_timer(timer, timer_bounds(target, timer),
                                    points.min(axis=0), points.max(axis=0)))
    return writer.finish()

