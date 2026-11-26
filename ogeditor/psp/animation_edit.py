"""Edit one KGF ANM channel while retaining the clip's other native blocks."""

import math
import struct

from formats import anm_header, u32


# 08A1B6A0 replaces the pointer to slot 6 with slot 22 when the rage flag is active.
STORY_BANKS = {932, 967, 1007}
STORY_CLIPS = {6: 0xD150, 22: 0x5BBD0}
STRIDES = (8, 16, 16)


def read_anm_bank(data):
    if len(data) < 12 or not data.startswith(b'ANM\0'):
        raise ValueError('ANM 动画库格式无效。')
    count = u32(data, 4)
    if not 1 <= count <= 4096 or 8 + 4 * count > len(data):
        raise ValueError('ANM 动画库目录无效。')
    offsets = struct.unpack_from(f'<{count}I', data, 8)
    parts = []
    for offset in offsets:
        if offset == 0xFFFFFFFF:
            parts.append(b'')
            continue
        if offset % 16 or not 8 + 4 * count <= offset <= len(data) - 8:
            raise ValueError('ANM 动画库中的片段偏移无效。')
        size = u32(data, offset + 4)
        if data[offset:offset + 4] != b'ANM\0' or size < 48 or offset + size > len(data):
            raise ValueError('ANM 动画库中的片段不完整。')
        parts.append(data[offset:offset + size])
    bounds = sorted((offset, offset + len(part)) for offset, part in zip(offsets, parts) if part)
    if any(left[1] > right[0] for left, right in zip(bounds, bounds[1:])):
        raise ValueError('ANM 动画库的片段重叠。')
    return count, offsets, parts


def protect_story_clip(offsets, size, slot):
    if slot in STORY_CLIPS:
        start = STORY_CLIPS[slot]
        if offsets[slot] != start or any(start < other < start + size for other in STORY_CLIPS.values()):
            raise ValueError('本片段须保留剧情固定起点，且不能跨过另一处固定起点。')


def replace_story_bank(data, edits):
    _, offsets, parts = read_anm_bank(data)
    if any(offsets[slot] != at for slot, at in STORY_CLIPS.items()):
        raise ValueError('佐菲剧情 ANM 的固定片段位置已变化。')
    for slot, clip in edits.items():
        if not 0 <= slot < len(parts) or not parts[slot]:
            raise ValueError('ANM 的目标片段不存在。')
        protect_story_clip(offsets, len(clip), slot)
    fixed = [(offsets[i], offsets[i]+len(clip)) for i,clip in edits.items() if i in STORY_CLIPS]
    out = bytearray(data)
    # Only indexed ANMs or zero alignment bytes may be displaced by a larger
    # fixed clip. A project can also have opaque resources appended to this bank.
    for start,end in fixed:
        overlap = bytearray(data[start:end])
        for pos,part in zip(offsets,parts):
            lo,hi=max(start,pos),min(end,pos+len(part))
            if lo<hi:overlap[lo-start:hi-start]=bytes(hi-lo)
        if any(overlap):
            raise ValueError('剧情片段扩展范围内含独立附加资源。')
        if end>len(out):out.extend(bytes(end-len(out)))
    for i,part in enumerate(parts):
        if not part or i in STORY_CLIPS:continue
        pos=offsets[i]
        clip=edits.get(i,part)
        displaced=any(pos<end and start<pos+len(part) for start,end in fixed)
        if displaced or len(clip)>len(part):
            out.extend(bytes((-len(out))%16))
            pos=len(out)
            struct.pack_into('<I',out,8+4*i,pos)
            out.extend(clip)
        elif i in edits:
            out[pos:pos+len(clip)]=clip
    for i,clip in edits.items():
        if i in STORY_CLIPS:out[offsets[i]:offsets[i]+len(clip)]=clip
    return bytes(out)


def replace_anm_clips(data, edits, pinned=()):
    """Preserve the locations of existing clips and data attached to the animation bank."""
    count, offsets, parts = read_anm_bank(data)
    out = bytearray(data)
    for slot, clip in edits.items():
        if not 0 <= slot < count or not parts[slot]:
            raise ValueError('ANM 的目标片段不存在。')
        if len(clip) <= len(parts[slot]) or slot in pinned:
            start = offsets[slot]
            out[start:start + len(clip)] = clip
        else:
            out.extend(bytes((-len(out)) % 16))
            struct.pack_into('<I', out, 8 + 4 * slot, len(out))
            out.extend(clip)
    return bytes(out)


def track_ranges(clip):
    control, _, count, _, _ = anm_header(clip)
    for track in range(count):
        entry = 40 + track * 14
        for channel, stride in enumerate(STRIDES):
            pointer = entry + 4 * channel
            start, end = struct.unpack_from('<HH', clip, pointer)
            start, end = 32 + 8 * start, 32 + 8 * end
            length = end - start or stride
            if start < 40 + count * 14 or length <= 0 or length % stride or start + length > control:
                raise ValueError('ANM 的关键帧范围无效。')
            yield clip[entry + 12], channel, pointer, start, start + length, start == end


def with_bind_tracks(clip, poses):
    control, _, count, _, _ = anm_header(clip)
    directory_end = 40 + count * 14
    present = set(clip[52:directory_end:14])
    use_translation, use_scale = clip[control + 8:control + 10]
    missing = []
    for track, variants in sorted(poses.items()):
        if track in present:
            continue
        defaults = {(angles, position if use_translation else (0., 0., 0.),
                     scale if use_scale else (1., 1., 1.)) for angles, position, scale in variants}
        # Several shapes can share this ANM bank. Only add a track with a
        # common bind value for the shapes that use it.
        if len(defaults) == 1:
            missing.append((track, *next(iter(defaults))))
    if not missing:
        return clip

    first = directory_end + 14 * len(missing)
    first += (-first) % 16
    growth = first + 48 * len(missing) - directory_end
    growth += (-growth) % 16
    out = bytearray(clip[:directory_end] + bytes(growth) + clip[directory_end:])
    for field in (4, 20, 24, 28):
        struct.pack_into('<I', out, field, u32(clip, field) + growth)
    struct.pack_into('<H', out, 38, count + len(missing))
    for _, _, pointer, _, _, _ in track_ranges(clip):
        a, b = struct.unpack_from('<HH', clip, pointer)
        if b + growth // 8 > 0xFFFF:
            raise ValueError('补齐骨骼轨道后，关键帧超过 ANM 的地址范围。')
        struct.pack_into('<HH', out, pointer, a + growth // 8, b + growth // 8)
    for i, (track, angles, position, scale) in enumerate(missing):
        start = first + 48 * i
        vector = (start - 32) // 8
        struct.pack_into('<6HBB', out, directory_end + 14 * i,
                         vector + 4, vector + 4, vector, vector, vector + 2, vector + 2, track, 0)
        struct.pack_into('<8f4H', out, start, *position, 0., *scale, 0., *angles, 0)
    return bytes(out)


def read_keys(clip, logical, channel):
    for track, kind, _, start, end, constant in track_ranges(clip):
        if track == logical and kind == channel:
            if kind == 0:
                return [[0 if constant else frame, *(a * 360 / 65535 - 180 for a in (x, y, z))]
                        for x, y, z, frame in struct.iter_unpack('<4H', clip[start:end])]
            return [[0 if constant else frame, x, y, z]
                    for x, y, z, frame in struct.iter_unpack('<4f', clip[start:end])]
    raise ValueError('请选择存在的逻辑轨道与通道。')


def write_keys(clip, logical, channel, rows):
    control, _, _, _, _ = anm_header(clip)
    ranges = list(track_ranges(clip))
    target = next((row for row in ranges if row[:2] == (logical, channel)), None)
    if target is None or not isinstance(rows, list) or not rows:
        raise ValueError('请保留至少一个关键帧。')
    if not all(isinstance(row, list) and len(row) == 4
               and all(type(v) in (int, float) and math.isfinite(v) and abs(v) <= 3.4028234e38 for v in row)
               for row in rows):
        raise ValueError('关键帧参数须为有限数值。')
    previous = -1
    data = bytearray()
    for frame, x, y, z in rows:
        if channel != 0:
            frame = struct.unpack('<f', struct.pack('<f', frame))[0]
        if not 0 <= frame <= 65535 or frame <= previous:
            raise ValueError('关键帧须按帧号递增，范围为 0 至 65535。')
        previous = frame
        if channel == 0:
            if frame != int(frame) or any(not -180 <= value <= 180 for value in (x, y, z)):
                raise ValueError('旋转通道按整帧保存，角度范围为 −180 至 180°。')
            angles = [round((value + 180) * 65535 / 360) for value in (x, y, z)]
            data.extend(struct.pack('<4H', *angles, int(frame)))
        else:
            data.extend(struct.pack('<4f', x, y, z, frame))
    _, _, pointer, start, end, _ = target
    constant = len(rows) == 1 and rows[0][0] == 0
    current = bytearray(clip)
    shared = any(row != target and row[3] < end and row[4] > start for row in ranges)
    if len(data) == end - start and not shared:
        current[start:end] = data
    else:
        before = (-control) % 16
        start = control + before
        payload = bytes(before) + data
        payload += bytes((-len(payload)) % 16)
        delta = len(payload)
        current[control:control] = payload
        for offset in (4, 20, 24, 28):
            struct.pack_into('<I', current, offset, u32(clip, offset) + delta)
    end = start if constant else start + len(data)
    if not 0 <= start - 32 <= end - 32 <= 65535 * 8:
        raise ValueError('关键帧超过 ANM 目录可寻址范围。')
    struct.pack_into('<HH', current, pointer, (start - 32) // 8, (end - 32) // 8)
    return bytes(current)


def change_timing(clip, frames, rate, horizontal=1.0):
    control, _, _, last, old_rate = anm_header(clip)
    if type(frames) is not int or not 1 <= frames <= 32767:
        raise ValueError('动作总帧数须为 1 至 32767。')
    if type(rate) not in (float, int) or not 0 < rate <= 3.4028234e38:
        raise ValueError('ANM 速率须为有限正数。')
    if type(horizontal) not in (float, int) or not 0 <= horizontal <= 3.4028234e38:
        raise ValueError('根位移倍率须为有限的非负数。')
    result = bytearray(clip)
    if frames != last + 1:
        struct.pack_into('<h', result, control + 2, frames)
    if rate != old_rate:
        struct.pack_into('<f', result, control + 4, rate)
    anm_header(result)
    if horizontal != 1:
        keys = read_keys(result, 0, 1)
        _, base_x, _, base_z = keys[0]
        shifted = [[frame, base_x + (x - base_x) * horizontal, y, base_z + (z - base_z) * horizontal]
                   for frame, x, y, z in keys]
        if shifted != keys:
            return write_keys(result, 0, 1, shifted)
    return bytes(result)
