"""Recover an editable v7 move set from the resources inside an exported ISO."""

from collections import Counter
from functools import lru_cache
import json
import struct
import zlib

from ogeditor.psp.animation_edit import read_anm_bank
from ogeditor.readers import HERE, ROOT, editor_data
from event_edits import Patch
from formats import SHAPE_TEMPLATE_OFFSETS, bom_parts, event_records, s16, u32
from cross_timing import TAG as TIMING_TAG
from ogeditor.psp.move_sets import Compilation, MISSING, append_bytes, move_event_key, pack_script
from move_eboot import aligned


@lru_cache(maxsize=1)
def stock_layouts():
    data = json.loads(zlib.decompress(editor_data(HERE, ROOT, 'move_bank_layouts.dat').read_bytes()))
    return {kind:{int(key):value for key,value in rows.items()} for kind,rows in data.items()}


def checked(condition, reason):
    if not condition:
        raise ValueError('恢复镜像配招时，' + reason)


def native_anm(data, count, original_size, stock):
    total, offsets, clips = read_anm_bank(data)
    checked(0 < count < total, 'ANM 的原目录长度无效。')
    added = [at for at in offsets[count:] if at != MISSING]
    checked(added, 'ANM 没有已安装的片段。')
    boundary = min(added)
    # expand_anm moves any native clip covered by the enlarged directory.
    # Restore its old location when known. A custom layout can keep it at the
    # relocated address. A later keyframe edit may have moved it past boundary.
    positions = list(offsets[:count])
    header_end = 8 + 4 * total
    known = stock[1] if len(stock[1]) == count else ()
    compact = 8 + 4 * count <= original_size <= boundary
    proposed = positions.copy()
    for i, at in enumerate(positions):
        if at == MISSING or at + len(clips[i]) <= original_size:
            continue
        if not (known and 8 + 4 * count <= known[i] < header_end
                and known[i] + len(clips[i]) <= original_size and at < boundary):
            compact = False
            break
        proposed[i] = known[i]
        intact = max(header_end, known[i])
        if data[intact:known[i] + len(clips[i])] != clips[i][intact - known[i]:]:
            compact = False
            break
    spans = sorted((at, at + len(clip)) for at,clip in zip(proposed,clips) if at != MISSING)
    if any(end > following for (_,end),(following,_) in zip(spans,spans[1:])):
        compact = False
    retained = original_size if compact else boundary
    out = bytearray(data[:retained])
    out[8 + 4 * count:header_end] = bytes(header_end - 8 - 4 * count)
    if known:
        start = 8 + 4 * count
        padding = bytes.fromhex(stock[2])
        n = min(len(padding), header_end - start)
        out[start:start + n] = padding[:n]
    for i, at in enumerate(positions):
        if at == MISSING or at + len(clips[i]) <= retained:
            continue
        if compact:
            positions[i] = known[i]
            out[known[i]:known[i] + len(clips[i])] = clips[i]
        else:
            positions[i] = append_bytes(out, clips[i])
    struct.pack_into(f'<{count + 1}I', out, 4, count, *positions)
    raw = bytes(out)
    checked(read_anm_bank(raw)[2] == clips[:count], '还原 ANM 后原动作字节不一致。')
    return raw


def projected_bom(data, scripts):
    # Cross-game imports can repack the whole BOM after the original export.
    # Keep shape numbers and all referenced data offsets, including timing data.
    total, offsets, _ = bom_parts(data)
    positions = (*offsets[:scripts], *offsets[total:])
    header = 12 + 4 * len(offsets)
    start = min(at for at in offsets if at != MISSING)
    prefix = struct.pack(f'<4s{2 + len(positions)}I', b'BOM\0', scripts, len(offsets)-total, *positions)
    return prefix + data[header:start] + bytes(header - len(prefix)) + data[start:]


def native_bom(data, scripts, original_shapes, original_size):
    total, offsets, _ = bom_parts(data)
    checked(0 < scripts < total, 'BOM 的原脚本数无效。')
    added = [at for at in offsets[scripts:total] if at != MISSING]
    checked(added, 'BOM 没有已安装的脚本。')
    boundary = min(added)
    shapes = next((i for i, at in enumerate(offsets[total:]) if at != MISSING and at >= boundary),
                  len(offsets) - total)
    if (original_shapes is not None and shapes != original_shapes) or (not shapes and len(offsets) > total):
        return projected_bom(data, scripts)
    extra = (len(offsets) - scripts - shapes) * 4
    growth = aligned(extra)
    head = 12 + 4 * (scripts + shapes)
    length = boundary - growth
    if 0 <= length - original_size < 4:
        length = original_size
    checked(head <= length and head + growth <= boundary, 'BOM 的原资源范围无效。')
    out = bytearray(data[:head] + data[head + growth:length + growth])
    positions = [at - growth if at != MISSING else at for at in (*offsets[:scripts], *offsets[total:total + shapes])]
    if data.startswith(TIMING_TAG, head + extra):
        first = min(pos for pos in positions if pos != MISSING)
        out[head:first] = data[head + extra:first + extra]
    struct.pack_into(f'<{len(positions) + 2}I', out, 4, scripts, shapes, *positions)
    bom_parts(out)
    return bytes(out)


def native_model(data, wanted, size, weapon):
    """Reverse the append operations in graft_locator and graft_weapon."""
    raw = bytearray(data)
    while (count := struct.unpack_from('<H', raw, 14)[0]) > wanted:
        bones, start, end = struct.unpack_from('<3I', raw, 20)
        checked(bones + 64 * count == start and end <= len(raw), 'MDL 挂点表无效。')
        last = start - 64
        parent, sibling, child = struct.unpack_from('<3h', raw, last + 50)
        if (0 <= parent < count - 1 and child == -1 and u32(raw, last + 56) == MISSING
                and u32(raw, last + 60) == end and s16(raw, bones + parent * 64 + 54) == count - 1):
            struct.pack_into('<h', raw, bones + parent * 64 + 54, sibling)
            del raw[last:start]
            delta = 64
            new_count = count - 1
        else:
            checked(weapon and count == wanted + 4, 'MDL 尾部不是已识别的配招挂点。')
            first = bones + wanted * 64
            hand, following = struct.unpack_from('<2h', raw, first + 50)
            checked(0 <= hand < wanted and s16(raw, bones + hand * 64 + 54) == wanted,
                    '双截棍的手部父子节点不一致。')
            struct.pack_into('<h', raw, bones + hand * 64 + 54, following)
            delta, new_count = 32 + 4 * 64, wanted
            mesh = u32(raw, first + 60)
            original_end = size if aligned(size + delta) == mesh else mesh - delta
            checked(start <= original_end + delta <= end, '双截棍网格的起点无效。')
            del raw[original_end + delta:]
            del raw[first:start]
            del raw[bones - 32:bones]
            bones -= 32
            struct.pack_into('<H', raw, 12, struct.unpack_from('<H', raw, 12)[0] - 1)
        for n in range(new_count):
            pos = bones + 64 * n + 60
            struct.pack_into('<I', raw, pos, u32(raw, pos) - delta)
        struct.pack_into('<H', raw, 14, new_count)
        struct.pack_into('<3I', raw, 20, bones, start - delta, len(raw))
        struct.pack_into('<I', raw, 4, len(raw) + 8)
    return bytes(raw)


def recipe_key(move):
    return move.target, move.root, move.source, move.original, move.source_clip


class ResumedMoves:
    def __init__(self, game):
        self.members = {}
        self.roots = {}
        self.clips = {}
        self.events = {}
        self.event_keys = {}
        self.clip_keys = {}
        self.shape_keys = set()
        self.shape_events = {}
        self.shape_links = {}
        self.bom_roles = {}
        self._read_bases(game)
        self.alignment = {}
        self.padding = self._group_padding(game)

    def _group_padding(self, game):
        # Legacy exports increased group sizes in whole sectors after growing
        # members in 16-byte units. Only reclaim known zero alignment bytes.
        padding = {}
        stored = game._mix_metadata.get('padding')
        recorded = {}
        if stored is not None:
            for row in stored:
                checked(isinstance(row,list) and len(row) == 2 and all(type(x) is int for x in row)
                        and 0 <= row[0] < len(game.yum['groups']) and 0 <= row[1] < 2048,
                        '资源组的对齐记录无效。')
                recorded[row[0]] = row[1]
        native = stock_layouts()['tails']
        with game.path.open('rb') as handle:
            for group, (start,length,count,first) in enumerate(game.yum['groups']):
                if not count:
                    continue
                last, size, _ = game.yum['files'][first + count - 1]
                tail = start + length - last - size
                growth = 0
                for member in range(first, first + count):
                    if member not in self.members:
                        continue
                    at, size, _ = game.yum['files'][member]
                    old_size = len(self.members[member])
                    following = (game.yum['files'][member + 1][0] if member + 1 < first + count
                                 else start + length)
                    pad = (old_size - size) % 16
                    if pad and at + size + pad <= following:
                        handle.seek(game.entry['offset'] + at + size)
                        if handle.read(pad) == bytes(pad):
                            self.alignment[member] = pad
                    growth += size + self.alignment.get(member, 0) - old_size
                if stored is not None:
                    pad = recorded.get(group, 0)
                elif group in native:
                    pad = (-growth) % 2048
                    if tail != native[group] + self.alignment.get(first + count - 1, 0) + pad:
                        continue
                else:
                    continue
                if 0 < pad <= tail - self.alignment.get(first + count - 1, 0):
                    handle.seek(game.entry['offset'] + start + length - pad)
                    if handle.read(pad) == bytes(pad):
                        padding[group] = pad
        return padding

    def _read_bases(self, game):
        layouts = stock_layouts()
        metadata = game._mix_metadata
        recorded = {}
        for row in metadata.get('bases', ()):
            checked(isinstance(row,list) and len(row) in (3,4) and
                    all(type(v) is int and v >= 0 for v in row) and row[0] not in recorded,
                    '原资源目录无效或重复。')
            member = row[0]
            kind = next((kind for kind in ('anm','bom','mdl') if member in layouts[kind]), None)
            checked(kind is not None and len(row) == (4 if kind == 'bom' else 3)
                    and row[-1] <= game.yum['files'][member][1], '原资源目录与 YUM 成员不匹配。')
            recorded[member] = row[1:]
        targets = {row[0] for row in metadata['moves']}
        weapon_roles = {row[0] for row in metadata['moves'] if tuple(row[3:5]) == (5, 160)}
        throws = any(game.native_actions[row[0]][row[1]]['category'] == 11 or
                     tuple(row[3:5]) in ((2,160),(5,160),(11,163),(12,163),(14,163))
                     for row in metadata['moves'])
        banks = (game.bank_owners.keys() & recorded.keys() if recorded else
                 [bank for bank, role in game.bank_owners.items() if role in targets or throws])
        for bank in banks:
            size, offsets, _ = layouts['anm'][bank]
            count, size = recorded.get(bank, (len(offsets), size))
            if u32(game.read_image_member(bank, b'ANM\0', 8), 4) > count:
                raw = game.read_image_member(bank, b'ANM\0')
                self.members[bank] = native_anm(raw, count, size, layouts['anm'][bank])
        for role in sorted(targets):
            for config, case in enumerate(game.roles[role]['cases']):
                for shape in range(len(case['forms'])):
                    bom = game.form_binding(role, config, shape)[2]['id']
                    self.bom_roles.setdefault(bom, set()).add(role)
        for member in self.bom_roles:
            if recorded and member not in recorded:
                continue
            count, _, size = layouts['bom'][member]
            count, nshape, size = recorded.get(member, (count, None, size))
            if u32(game.read_image_member(member, b'BOM\0', 8), 4) > count:
                self.members[member] = native_bom(game.read_image_member(member, b'BOM\0'), count, nshape, size)
        self.weapon_models = set()
        for member, placements in game.model_forms.items():
            roles = {role for role, _, _ in placements}
            if not roles & targets or recorded and member not in recorded:
                continue
            count, size = recorded.get(member, layouts['mdl'][member])
            if struct.unpack_from('<H', game.read_image_member(member, b'MDL\0', 16), 14)[0] > count:
                raw = game.read_image_member(member, b'MDL\0')
                if roles & weapon_roles:
                    self.weapon_models.add(member)
                self.members[member] = native_model(raw, count, size, member in self.weapon_models)

    def recover(self, game):
        rows = game._mix_metadata['moves']
        skeleton = Compilation(game, {})
        for role, state, _, source, original, *_ in rows:
            if (role, state) not in skeleton.states:
                self.roots[role, state] = source, original
                skeleton.root = state
                skeleton.plan_move(role, state, source, original)
        expected = {(m.target,m.state):(m.clip,m.source,m.original,m.source_clip) for m in skeleton.states.values()}
        checked(expected == {(r[0],r[1]):tuple(r[2:6]) for r in rows}, '主招和后续状态与 EBOOT 记录不一致。')
        plan = Compilation(game, self.roots)
        for bank, copies in plan.animations.items():
            actual = read_anm_bank(game.read_image_member(bank, b'ANM\0'))[2]
            owner = game.bank_owners[bank]
            for slot, raw in copies.items():
                move = plan.recipes.get((owner, slot))
                key = recipe_key(move) if move else ('fixed', slot)
                self.clip_keys[bank, slot] = key
                checked(slot < len(actual) and actual[slot], '找不到已配招的动作片段。')
                if actual[slot] != raw:
                    self.clips[bank, key] = actual[slot]
        for member, scripts in plan.scripts.items():
            total, _, physical = bom_parts(game.read_image_member(member, b'BOM\0'))
            base, _, parts = game.bank(member, b'BOM\0')
            first = len(parts) - base
            templates = parts[base:] + plan.templates[member]
            roles = self.bom_roles[member]
            for slot, records in scripts.items():
                move = next((plan.recipes[role, slot] for role in roles if (role, slot) in plan.recipes), None)
                if not move:
                    continue
                actual = list(event_records(physical[slot]))
                records = [r.raw for r in event_records(pack_script(records))]
                checked(len(actual) == len(records), '独立配招事件的数量不同。')
                local = Counter()
                for i, (raw, saved) in enumerate(zip(records, actual)):
                    mask = raw[1]
                    shape = mask.bit_length() - 1
                    checked(saved.raw[:2] == raw[:2] and len(saved.raw) == len(raw), '独立配招事件结构不同。')
                    key = move_event_key(move, member, shape, local[shape])
                    local[shape] += 1
                    self.event_keys[member,slot,i] = key
                    template = SHAPE_TEMPLATE_OFFSETS.get(raw[0])
                    if template is not None:
                        old, new = s16(saved.raw,template), s16(raw,template)
                        if old >= 0:
                            checked(total + old < len(physical), '配招事件的物理判定模板越界。')
                            source = physical[total + old]
                            self.shape_events.setdefault((member,old), []).append(key)
                            if old >= first:
                                self.shape_keys.add((member,old))
                            if old != new or source[:len(templates[new])] != templates[new]:
                                self.shape_links[key] = old, source
                    edits = {str(off):s16(saved.raw,off) for off in range(2,len(raw),2)
                             if off != template and raw[off:off+2] != saved.raw[off:off+2]}
                    if edits:
                        self.events[key] = edits

    def apply_resources(self, plan):
        if not self.clip_keys and not self.shape_keys:
            return
        clips = self.clips.copy()
        for key, data in plan.game.asset_edits.items():
            if key in self.clip_keys:
                clips[key[0],self.clip_keys[key]] = data
        for bank, copies in plan.animations.items():
            for slot in copies:
                role = plan.game.bank_owners[bank]
                move = plan.recipes.get((role,slot))
                key = recipe_key(move) if move else ('fixed',slot)
                if (bank,key) in clips:
                    copies[slot] = clips[bank,key]
        links = self.shape_links.copy()
        game = plan.game
        for (member,index), data in game.template_edits.items():
            for event in self.shape_events.get((member,index), ()):
                links[event] = index, data
        banks = {}
        for event, index in game.resumed_shape_refs.items():
            member = event[5]
            if member not in banks:
                total, _, parts = bom_parts(game.read_image_member(member,b'BOM\0'))
                banks[member] = parts[total:]
            shapes = banks[member]
            checked(type(index) is int and 0 <= index < len(shapes) and len(shapes[index]) >= 4
                    and 1 <= u32(shapes[index],0) <= 4, '配招事件指定的判定模板无效。')
            links[event] = index, game.template_edits.get((member,index),shapes[index])
        if links:
            self.apply_shape_links(plan, links)

    def apply_shape_links(self, plan, links):
        for member, scripts in plan.scripts.items():
            total, _, parts = plan.game.bank(member,b'BOM\0')
            shapes = parts[total:] + plan.templates[member]
            for slot, records in scripts.items():
                move = next((plan.recipes[role,slot] for role in self.bom_roles.get(member, ())
                             if (role,slot) in plan.recipes), None)
                if move is None:
                    continue
                local = Counter()
                for i, raw in enumerate(records):
                    shape = raw[1].bit_length() - 1
                    link = links.get(move_event_key(move,member,shape,local[shape]))
                    local[shape] += 1
                    if link is None:
                        continue
                    old_index, template = link
                    template = plan.game.template_edits.get((member,old_index),template)
                    if template not in shapes:
                        shapes.append(template)
                        plan.templates[member].append(template)
                    offset = SHAPE_TEMPLATE_OFFSETS[raw[0]]
                    records[i] = raw[:offset] + struct.pack('<h',shapes.index(template)) + raw[offset+2:]

    def migrate_edits(self, game):
        for (member, slot), raw in list(game.asset_edits.items()):
            base = self.members.get(member)
            if slot is not None or base is None:
                continue
            if raw.startswith(b'MDL\0'):
                count = struct.unpack_from('<H',base,14)[0]
                game.asset_edits[member,None] = native_model(raw,count,len(base),member in self.weapon_models)
            elif raw.startswith(b'BOM\0'):
                count, shapes = struct.unpack_from('<2I', base, 4)
                if u32(raw,4) > count:
                    game.asset_edits[member,None] = native_bom(raw,count,shapes,len(base))
        game.clear_bank_cache()
        game.node_count.cache_clear()
        for (member, slot), patches in list(game.edits.items()):
            if member not in self.members:
                continue
            total, offsets, parts = game.bank(member,b'BOM\0')
            if slot < total:
                parsed = {r.index:r for r in event_records(parts[slot])}
                game.edits[member,slot] = {i:Patch(offsets[slot] + parsed[i].offset, parsed[i].raw, p.edited)
                                          for i,p in patches.items()}
                continue
            for i, patch in patches.items():
                key = self.event_keys[member,slot,i]
                words = game.move_event_edits.get(key, {}).copy()
                shape = SHAPE_TEMPLATE_OFFSETS.get(patch.original[0])
                if shape is not None and s16(patch.original,shape) != s16(patch.edited,shape):
                    game.resumed_shape_refs[key] = s16(patch.edited,shape)
                words.update({str(off):s16(patch.edited,off) for off in range(2,len(patch.edited),2)
                              if off != shape and patch.original[off:off+2] != patch.edited[off:off+2]})
                if words:
                    game.move_event_edits[key] = words
            del game.edits[member,slot]
