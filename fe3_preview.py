"""Decode FE3 model surfaces and animation tracks for the shared viewport."""

from collections import defaultdict
from functools import lru_cache
import math
import struct

from fe3_formats import align, clips, model_signature, png_preview, pom_layout, require, word


SUBDIVISION = ((0.375, 0.375, 0.125, 0.125), (0.4375, 0.4375, 0.125),
               (0.125, 0.125, 0.75), (0.5, 0.5))
# Original EE coefficient table at 0x49FC10, indexed by the ring width.
RING = ((.765625,.234375),(.390625,.3046875),(.4375,.1875),(.515625,.12109375),
        (.5795339345932007,.08409322053194046),(.625,.0625),(.6568255424499512,.04902492091059685),
        (.6794575452804565,.04006781056523323),(.6959348320960999,.03378501906991005),
        (.7082224488258362,.029177753254771233),(.7175917625427246,.025673476979136467),
        (.7248797416687012,.022926686331629753),(.730650007724762,.02071922831237316),
        (.7352907061576843,.018907805904746056),(.7390751242637634,.01739499345421791))


def vector(data, offset):
    result = struct.unpack_from('<3f', data, offset)
    require(all(map(math.isfinite, result)), 'FE3 向量含无效浮点数。')
    return result


def chunks(data, start, stop, fmt):
    return struct.iter_unpack(fmt, memoryview(data)[start:stop])


def read_bones(data):
    table = word(data, 16)
    bones = []
    for i in range(word(data, 32)):
        at = table + 112 * i
        parent = struct.unpack_from('<h', data, at)[0]
        bones.append({'parent':parent, 'rotation':vector(data, at + 32),
                      'scale':vector(data, at + 48), 'translation':vector(data, at + 64)})
    return bones


def surface_lighting(data, q, h, nodes, groups):
    # VU1 program 0, pairs 0x05D–0x064 and 0x0EF–0x135.
    p = q + 16 * h[3]
    owners = [node for node, row in zip(nodes, groups) for _ in range(row[0])]
    require(len(owners) == h[17] and h[16] == h[17] + h[18], 'FE3 法线目录无效。')
    authored = [(node, *vector(data, p + 16 * i)) for i, node in enumerate(owners)]
    p += 16 * len(authored)
    rings = []
    for _ in range(h[18]):
        n = word(data, p)
        require(n >= 3 and p + 4 * (1 + n) <= q + 16 * h[4], 'FE3 法线环越界。')
        row = struct.unpack_from(f'<{n}I', data, p + 4)
        require(max(row) < h[19], 'FE3 法线环的控制点引用无效。')
        rings.append(row)
        p = align(p + 4 * (1 + n), 16)
    require(p == q + 16 * h[4], 'FE3 法线环终点无效。')

    # VU1 color lookup and interpolation, pairs 0x149–0x237. Each strip
    # vertex indexes this RGBA table independently of its position.
    gains = [vector(data, q + 16 * (h[5] + i)) for i in range(h[29])]
    p = q + 16 * h[6]
    rgba = [tuple(x / 128 for x in row) for row in chunks(data, p, p + 16 * h[31], '<4I')]
    p += 16 * len(rgba)
    lit = []
    gain = 0
    for i in range(h[32] + h[33]):
        require(p + 16 <= q + 16 * h[7], 'FE3 顶点色生成命令越界。')
        normal, base, _, flags = struct.unpack_from('<4I', data, p)
        p += 16
        if i < h[32]:
            gain = flags
        elif flags:
            require(flags == 1 and p + 16 <= q + 16 * h[7], 'FE3 顶点色扩展命令无效。')
            gain = word(data, p)
            p += 16
        require(normal < h[16] and base < len(rgba) and gain < len(gains), 'FE3 光照参数引用越界。')
        lit.append((normal, base, *gains[gain]))
    start = q + 16 * h[7]
    pairs = tuple(chunks(data, start, start + 8 * h[34], '<2I'))
    require(all(max(pair) < len(rgba) + len(lit) for pair in pairs), 'FE3 插值色引用无效。')
    return {'authored':authored, 'rings':rings, 'rgba':rgba, 'lit':lit, 'pairs':pairs}


def polygon4(data, q, h):
    # 0x337838 fills the TEX0 table. 0x33CC54 submits the compact
    # triangles in the EE path, with GS ALPHA read from header byte +2.
    count = h[38]
    pos = q + 16 * h[11] + 8 * count
    end = q + 16 * h[12]
    texture = None
    strips = []
    for _ in range(count):
        require(pos + 8 <= end, 'FE3 polygon4 记录越界。')
        if not data[pos]:
            texture, = struct.unpack_from('<H', data, pos + 2)
            pos += 0x28
        require(texture is not None and pos + 8 <= end, 'FE3 polygon4 缺少原生材质。')
        n, _, alpha, _, palette, _ = struct.unpack_from('<BBBBHH', data, pos)
        pos += 8
        require(n >= 3 and pos + 16 * n <= end and alpha in (0x44, 0x48),
                'FE3 polygon4 顶点或混合模式无效。')
        rows = chunks(data, pos, pos + 16 * n, '<HHIff')
        corners = tuple((u, v, index & 0x7FFF, color) for index, color, _, u, v in rows)
        strips.append((texture, palette, alpha, corners))
        pos += n * 16
    require(align(pos, 16) == end, 'FE3 polygon4 目录范围无效。')
    return strips


def decode_surface(data, q, limit, nodes):
    h = struct.unpack_from('<44I', data, q)
    if not (h[36] or h[38]):
        return None, False
    count = len(nodes)
    require(0 < count == h[15] and q + 16 * h[1] <= limit and
            11 <= h[2] <= h[4] <= h[5] <= h[9] <= h[10] <= h[11] <= h[12] <= h[1], 'FE3 几何目录无效。')
    table = q + 16 * h[2]
    split = table + 16 * (count + 1)
    require(split + 16 * (count + 1) <= q + 16 * h[4], 'FE3 控制点表超出片段边界。')
    a = list(chunks(data, table, split, '<4I'))
    b = list(chunks(data, split, split + 16 * (count + 1), '<4I'))
    groups = ([row[2] for row in a[:-1]], [row[3] for row in b[:-1]], [row[3] for row in a[:-1]])
    require(tuple(map(sum, groups)) == h[20:23], 'FE3 控制点分组无效。')
    owners = [node for group in groups for node, n in zip(nodes, group) for _ in range(n)]
    p = q + 16 * h[4]
    end = q + 16 * h[5]
    require(p + 16 * len(owners) <= end, 'FE3 控制点区越界。')
    local = [coord for i in range(len(owners)) for coord in vector(data, p + 16 * i)]
    offsets = tuple(q + 16 * (h[4] + i) for i in range(len(owners)))
    p += 16 * len(owners)
    rules = []
    for kind, size in enumerate(h[24:29]):
        for _ in range(size):
            require(p + 16 <= end, 'FE3 细分指令区越界。')
            if kind < 4:
                weights = SUBDIVISION[kind]
                indices = struct.unpack_from(f'<{len(weights)}I', data, p)
                p += 16
            else:
                n = word(data, p)
                require(2 <= n <= 16 and p + 4 + 4 * n <= end, 'FE3 环形细分点数无效。')
                indices = struct.unpack_from(f'<{n}I', data, p + 4)
                p = align(p + 4 + 4 * n, 16)
                center, neighbor = RING[n - 2]
                weights = (center,) + (neighbor,) * (n - 1)
            require(all(i < len(owners) for i in indices), 'FE3 细分点引用无效。')
            rules.append([part for pair in zip(indices, weights) for part in pair])
    require(p == end and len(owners) + len(rules) == h[19], 'FE3 细分点数量不符。')

    p, end = q + 16 * h[9], q + 16 * h[10]
    texture = None
    strips = []
    while len(strips) < h[36]:
        require(p + 16 <= end, 'FE3 三角条带目录越界。')
        _, _, length, flags = struct.unpack_from('<4I', data, p)
        p += 16
        if length == 0:
            texture = flags
            p += 48
            continue
        require(texture is not None and p + 16 * length <= end, 'FE3 三角条带或材质越界。')
        corners = tuple(chunks(data, p, p + 16 * length, '<2f2I'))
        strips.append((texture, flags >> 16, None, corners))
        p += 16 * length
    require(p == end, 'FE3 三角条带长度不符。')
    if h[38]:
        strips.extend(polygon4(data, q, h))
    lighting = surface_lighting(data, q, h, nodes, a)
    colors = len(lighting['rgba']) + len(lighting['lit']) + len(lighting['pairs'])
    require(all(index < h[19] and color < colors and math.isfinite(u + v)
                for *_, corners in strips for u, v, index, color in corners), 'FE3 三角条带顶点无效。')
    return (local, owners, offsets, rules, strips, lighting), bool(h[38])


@lru_cache(maxsize=12)
def surfaces(data):
    """Resolve shared MDL geometry before interpreting its local bone palette."""
    model_signature(data)
    p = word(data, 24)
    geometry, result, extra = [], [], 0
    for _ in range(word(data, 40)):
        step, size, relative, _, shared, count = struct.unpack_from('<4IiH', data, p)
        nodes = struct.unpack_from(f'<{count}H', data, p + 22)
        q = p + relative if shared < 0 else geometry[shared]
        geometry.append(q)
        surface, specialized = decode_surface(data, q, q + size, nodes)
        if surface:
            result.append((nodes,*surface))
        extra += int(bool(specialized))
        p += step
    return result, extra


def make_model(game, form):
    data = game.data(form.model)
    bones = read_bones(data)
    surface_rows, extra = surfaces(data)
    bindings, images, materials, meshes = {}, [], [], []
    for nodes,local,owners,offsets,rules,strips,lighting in surface_rows:
        attributes, source, color_ids, batches = [], [], [], defaultdict(list)
        for texture, palette, blend, corners in strips:
            key = texture, palette, blend
            if key not in bindings:
                require(texture < len(form.textures) and form.textures[texture] is not None,
                        f'MDL 的贴图槽 {texture} 与当前形态的原生绑定不符。')
                target = form.textures[texture]
                pom = game.data(target)
                layout = pom_layout(pom)
                tex0, = struct.unpack_from('<Q', pom, 32)
                scale = ((1 << ((tex0 >> 26) & 15)) / layout['width'],
                         (1 << ((tex0 >> 30) & 15)) / layout['height'])
                slot = len(materials)
                bindings[key] = slot, scale
                actual = palette if palette < layout['palette_count'] else 0
                images.append({'uri':png_preview(pom, actual), 'id':f'{target[0]:04X}/{target[1]}', 'resource':target[0],
                               'member':target[1], 'slot':texture, 'palette':actual, 'requested':palette})
                materials.append({'kind':1, 'texture':slot, 'tint':[0.78, 0.82, 0.88, 1],
                                  'gs':{'tfx':(tex0 >> 35) & 3, 'tcc':(tex0 >> 34) & 1, 'alpha':blend}})
            material, scale = bindings[key]
            # The GS samples in its power-of-two TEX0 area even when the POM stores fewer rows.
            start = len(source)
            vertices = []
            for u, v, index, color in corners:
                source.append(index)
                color_ids.append(color)
                vertices.append(index)
                attributes.extend((0, 0, 0, u * scale[0], v * scale[1], 1, 1, 1, 1))
            for end, (a, b, c) in enumerate(zip(vertices, vertices[1:], vertices[2:]), 2):
                if len({a, b, c}) < 3:
                    continue
                batches[material].extend((start + end - 1, start + end - 2, start + end) if end % 2
                                         else (start + end - 2, start + end - 1, start + end))
        meshes.append({'owner':nodes[0],'drawNodes':nodes,'positions':local,'controlBones':owners,'controlOffsets':offsets,
                       'subdivision':rules, 'source':source, 'attributes':attributes, 'batches':list(batches.items()),
                       'lighting':lighting, 'colorIds':color_ids})
    node_channels = [(node, channel) for node, channel in form.tracks if 0 <= node < len(bones) and channel >= 0]
    rig = {}
    for node,channel in node_channels:
        rig.setdefault(channel,node)
        if channel == 0 and bones[node]['parent'] < 0:
            rig[0] = node
    rig.setdefault(0,0)
    # Viewport pose lookup is per node. This also keeps extended channels on native appendages.
    skeleton = {node for node, _ in node_channels} | {rig[0]}
    for node in tuple(skeleton):
        parent = bones[node]['parent']
        while parent >= 0 and parent not in skeleton:
            skeleton.add(parent)
            parent = bones[parent]['parent']
    return {'bones':bones, 'rig':rig, 'nodeChannels':node_channels, 'meshes':meshes, 'materials':materials, 'textures':images,
            'skeleton':sorted(skeleton), 'shape':form.id, 'parts':[], 'appearances':[], 'hands':[],
            'fe3':True, 'polygon4Pieces':extra,
            'triangles':sum(len(indices) // 3 for mesh in meshes for _, indices in mesh['batches'])}


def quaternion(a, b, c):
    x, y, z = (angle * (math.pi / 65536) - math.pi / 2 for angle in (a, b, c))
    sx, sy, sz = math.sin(x), math.sin(y), math.sin(z)
    cx, cy, cz = math.cos(x), math.cos(y), math.cos(z)
    return sx*cy*cz - cx*sy*sz, cx*sy*cz + sx*cy*sz, cx*cy*sz - sx*sy*cz, cx*cy*cz + sx*sy*sz


@lru_cache(maxsize=48)
def clip_index(data):
    return clips(data)


def motion(data, index):
    clip = clip_index(data)[index]
    base = clip.start + 32
    channels, result = {}, []
    for i, channel in enumerate(clip.tracks):
        ranges = struct.unpack_from('<6H', data, base + 8 + 14 * i)
        channels[channel] = [len(result), len(result) + 1, len(result) + 2]
        for kind, (lo, hi) in enumerate(zip(ranges[::2], ranges[1::2])):
            width = 8 if kind == 0 else 16
            start = base + 8 * lo
            end = start + width if lo == hi else base + 8 * hi
            if kind == 0:
                keys = [entry for a,b,c,frame in chunks(data, start, end, '<4H')
                        for entry in (frame, *quaternion(a,b,c))]
            else:
                keys = [entry for x,y,z,frame in chunks(data, start, end, '<4f') for entry in (frame,x,y,z)]
            require(all(map(math.isfinite, keys)), 'FE3 ANM 关键帧含无效浮点数。')
            result.append(keys)
    return {'channels':channels, 'tracks':result, 'first':max(0, clip.lower),
            'last':clip.last_frame,
            'frames':clip.frames, 'rate':clip.rate if clip.rate > 0 else 1,
            'localTranslation':True, 'localScale':True, 'fe3':True}


def key_span(data, index, track, channel):
    directory = clip_index(data)
    require(type(index) is int and 0 <= index < len(directory) and
            type(channel) is int and 0 <= channel <= 2, '请选择 ANM 片段和轨道分量。')
    clip = directory[index]
    require(type(track) is int and 0 <= track < len(clip.tracks), '请选择 ANM 轨道。')
    base = clip.start + 32
    lo, hi = struct.unpack_from('<2H', data, base + 8 + 14 * track + 4 * channel)
    fmt = '<4H' if channel == 0 else '<4f'
    width = struct.calcsize(fmt)
    start = base + 8 * lo
    end = start + width if lo == hi else base + 8 * hi
    return start, end, fmt


def key_rows(data, index, track, channel):
    start, end, fmt = key_span(data, index, track, channel)
    return [{'frame':frame, 'value':[x,y,z]} for x,y,z,frame in chunks(data,start,end,fmt)]


def encode_key(fmt, frame, values):
    require(isinstance(values, list) and len(values) == 3, '请输入三个关键帧分量。')
    record = [*values, frame]
    if fmt == '<4H':
        require(all(type(value) is int and 0 <= value <= 65535 for value in record),
                '原始旋转键须为 0–65535 的整数。')
    else:
        require(all(type(value) in (int, float) and math.isfinite(value) and abs(value) <= 3.402823466e38
                    for value in record) and frame >= 0, '请输入有限的平移、缩放和帧值。')
    return struct.pack(fmt, *record)


def single_clip(data, index):
    directory = clip_index(data)
    require(type(index) is int and 0 <= index < len(directory), '请选择 ANM 片段。')
    clip = directory[index]
    return struct.pack('<4s3I', b'ANM\0', 1, 16, 0) + data[clip.start:clip.start + clip.size]


def replace_clip(data, index, incoming):
    directory = clip_index(data)
    require(type(index) is int and 0 <= index < len(directory), '请选择 ANM 片段。')
    if incoming[:4] == b'ANM\0' and word(incoming, 4) == len(incoming):
        incoming = struct.pack('<4s3I', b'ANM\0', 1, 16, 0) + incoming
    replacement = clip_index(incoming)
    require(len(replacement) == 1 and replacement[0].tracks == directory[index].tracks,
            '请选择轨道编号与当前动作一致的单个 ANM。')
    motion(incoming, 0)
    current = directory[index]
    entry = replacement[0]
    if incoming[entry.start:entry.start + entry.size] == data[current.start:current.start + current.size]:
        return data
    delta = entry.size - current.size
    prefix = bytearray(data[:current.start])
    for i, clip in enumerate(directory[index + 1:], index + 1):
        struct.pack_into('<I', prefix, 8 + 4 * i, clip.start + delta)
    return bytes(prefix) + incoming[entry.start:entry.start + entry.size] + data[current.start + current.size:]


def replace_key_rows(data, index, track, channel, rows):
    start, end, fmt = key_span(data, index, track, channel)
    require(isinstance(rows, list) and 0 < len(rows) <= 32767, '轨道至少需要一个关键帧。')
    keys = b''.join(encode_key(fmt, item['frame'], item['value']) for item in rows)
    time = [f for *_, f in struct.iter_unpack(fmt, keys)]
    require(all(a < b for a, b in zip(time, time[1:])), '关键帧时间须严格递增。')
    if keys == data[start:end]:
        return data
    clip = clip_index(data)[index]
    base = clip.start + 32
    width = struct.calcsize(fmt)
    spans = [(i, kind, *key_span(data, index, i, kind)[:2])
             for i in range(len(clip.tracks)) for kind in range(3)]
    separate = any((i, kind) != (track, channel) and lo < end and hi > start for i, kind, lo, hi in spans)
    # Insert after the original key table when a sibling channel shares these bytes.
    begin, stop = (clip.metadata, clip.metadata) if separate else (start, end)
    if not separate:
        following = min((lo for _, _, lo, _ in spans if lo >= end), default=clip.metadata)
        if following - end < 16 and not any(data[end:following]):
            stop = following
    delta = align(len(keys) - (stop - begin), 16)
    extra = delta - len(keys) + (stop - begin)
    end_clip = clip.start + clip.size
    head = bytearray(data[clip.start:begin])
    metadata = clip.metadata + delta - clip.start
    aux = metadata + 16
    struct.pack_into('<I', head, 4, clip.size + delta)
    struct.pack_into('<3I', head, 20, metadata, aux, aux)
    for i, kind, lo, _ in spans:
        pos = 0x28 + 14 * i + 4 * kind
        if (i, kind) == (track, channel):
            offset = (begin - base) // 8
            upper = offset if len(keys) == width else offset + len(keys) // 8
            require(upper <= 65535, '关键帧索引超过原生半字范围。')
            struct.pack_into('<2H', head, pos, offset, upper)
        elif lo >= stop and delta:
            lower, upper = struct.unpack_from('<2H', head, pos)
            lower, upper = lower + delta // 8, upper + delta // 8
            require(upper <= 65535, '关键帧索引超过原生半字范围。')
            struct.pack_into('<2H', head, pos, lower, upper)
    return replace_clip(data, index, bytes(head) + keys + bytes(extra) + data[stop:end_clip])


def replace_key(data, index, track, channel, row, frame, values):
    rows = key_rows(data, index, track, channel)
    require(type(row) is int and 0 <= row < len(rows), '请选择一个关键帧。')
    rows[row] = {'frame':frame, 'value':values}
    return replace_key_rows(data, index, track, channel, rows)
