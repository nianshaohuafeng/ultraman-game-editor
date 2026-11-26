"""Read, validate and decode the FE3 stereo stream referenced by the ELF."""

import struct

from fe3_formats import require
from music import decode_audio


RATE = round(48000 * 0xEB3 / 0x1000)
STREAMS = 110
STREAM_TABLE = 0x42F090
INTERLEAVE = 0x1000
HEADER = 0x800


def stream_resources(tables):
    return tuple(tables.pointer(STREAM_TABLE, 3 * index) for index in range(STREAMS))


def stream_samples(data, size=None):
    """The six-word header gives the valid SPU frames and the padded blocks."""
    require(len(data) >= 24, 'PS2 流式音轨头被截断。')
    logical, block, count, frames, tail, frames_per_block = struct.unpack_from('<6I', data)
    size = len(data) if size is None else size
    require(block == INTERLEAVE and frames_per_block == INTERLEAVE // 16 and 0 < count and count % 2 == 0
            and 0 < frames and frames % 2 == 0 and tail == count * frames_per_block - frames
            and tail < 2 * frames_per_block and logical == HEADER + frames * 16
            and size == HEADER + count * block and not any(data[24:HEADER]),
            '请选择格斗进化3原生双声道流式音轨（BIN）。')
    return frames // 2 * 28


def library(game, role):
    table = game.workbench.tables
    table.descriptor(role)
    tracks = []
    with game.path.open('rb') as source:
        for index, resource in enumerate(game.stream_resources):
            part = game.part(resource, -1)
            edited = game.asset_edits.get(part.key)
            if edited is not None:
                samples = stream_samples(edited)
            else:
                source.seek(part.at)
                samples = stream_samples(source.read(24), part.size)
            tracks.append({'id':index, 'resource':resource, 'seconds':samples / RATE, 'isoOffset':part.at})
    opening, change, _, a, b = table.at(0x3B5368 + 20 * role, '<5i')
    roles = {}
    for index, label in ((opening,'开战角色曲'), (change,'战斗条件切换曲'), (a,'结果演出曲'), (b,'结果演出曲')):
        if 0 <= index < STREAMS and index not in roles:
            roles[index] = {**tracks[index], 'label':label}
    return {'role':list(roles.values()), 'library':tracks}


def audio(game, track, wav=False, cancel=None):
    require(type(track) is int and 0 <= track < STREAMS, '请选择原盘曲库的音轨。')
    data = game.data((game.stream_resources[track], -1))
    samples = stream_samples(data)
    payload = data[HEADER:]
    # ADS carries the same two interleaved SPU channels with a standard header.
    header = struct.pack('<4s7I4sI', b'SShd', 24, 16, RATE, 2, INTERLEAVE, 0, 0, b'SSbd', len(payload))
    return decode_audio(header + payload, RATE, wav, cancel, ('-f', 'ads'), samples)
