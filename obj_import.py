"""Read Wavefront OBJ geometry and material colors."""

from collections import defaultdict
from pathlib import Path

import numpy as np

from mesh_import import triangulate


def obj_index(value, count):
    if not value:
        return None
    number = int(value)
    index = number - 1 if number > 0 else count + number
    if not number or not 0 <= index < count:
        raise ValueError('OBJ 的顶点、UV 或法线索引超出范围。')
    return index


def read_obj(path, data):
    try:
        text = data.decode('utf-8-sig')
    except UnicodeDecodeError:
        text = data.decode('gb18030')
    vertices, uv, normals, faces = [], [], [], []
    library = []
    current = ''
    source_arrays = {'v': (vertices, 3), 'vt': (uv, 2), 'vn': (normals, 3)}
    for line_number, line in enumerate(text.splitlines(), 1):
        words = line.partition('#')[0].split()
        if not words:
            continue
        key, *values = words
        try:
            if key in source_arrays:
                array, size = source_arrays[key]
                row = [float(value) for value in values[:size]]
                if len(row) != size or not np.isfinite(row).all():
                    raise ValueError('坐标无效。')
                array.append(row)
            elif key == 'usemtl':
                current = ' '.join(values)
            elif key == 'mtllib':
                name = ' '.join(values)
                library.extend([name] if (path.parent / name).is_file() else values)
            elif key == 'f':
                corners = []
                for value in values:
                    indices = value.split('/')
                    if not indices[0] or len(indices) > 3:
                        raise ValueError('面索引格式无效。')
                    indices.extend([''] * (3 - len(indices)))
                    corners.append(tuple(obj_index(item, len(array))
                                         for item, array in zip(indices, (vertices, uv, normals))))
                if len(corners) < 3:
                    raise ValueError('多边形至少需要三个顶点。')
                faces.extend((current, tri) for tri in triangulate(corners, vertices))
        except ValueError as exc:
            raise ValueError(f'OBJ 第 {line_number} 行，{exc}') from exc
    if not faces:
        raise ValueError('OBJ 没有可导入的面。')
    used = sorted({v for _, tri in faces for v, _, _ in tri})
    ids = {old: new for new, old in enumerate(used)}
    faces = [(material, tuple((ids[v], t, n) for v, t, n in tri)) for material, tri in faces]
    return np.asarray(vertices)[used], uv, np.asarray(normals).reshape(-1, 3), faces, read_mtl(path, library)


def read_mtl(path, libraries):
    settings = defaultdict(lambda: {'Kd': (.8, .8, .8), 'd': (1.0,)})
    for name in dict.fromkeys(libraries):
        local = Path(name)
        if local.is_absolute() or local.suffix.lower() != '.mtl' or not (path.parent / local).is_file():
            continue
        current = ''
        for line in (path.parent / local).read_text(encoding='utf-8-sig', errors='replace').splitlines():
            words = line.partition('#')[0].split()
            if not words:
                continue
            key, *values = words
            if key == 'newmtl':
                current = ' '.join(values)
            elif key in ('Kd', 'd', 'Tr'):
                count = 3 if key == 'Kd' else 1
                try:
                    row = tuple(float(x) for x in values[:count])
                except ValueError:
                    continue
                if len(row) != count or not np.isfinite(row).all():
                    continue
                if key == 'Tr':
                    row = (1 - row[0],)
                    key = 'd'
                settings[current][key] = row
    return settings
