"""CMSP camera curves shared by the PS2 and PSP resource readers."""

import math
import struct


def camera_curve(data, *, key, name=None):
    if len(data) < 0x4C or data[:4] != b'CMSP':
        raise ValueError('镜头资源的 CMSP 文件头无效。')
    seconds, projection, mode = struct.unpack_from('<fhh', data, 4)
    if not math.isfinite(seconds) or seconds < 0 or projection <= 0 or mode not in (0, 1):
        raise ValueError('CMSP 的时长、投影或插值方式无效。')
    channels = []
    for channel in range(7):
        count, _, offset = struct.unpack_from('<BBh', data, 0x30 + 4 * channel)
        if not count:
            channels.append([])
            continue
        if offset < 0x4C or offset + count * 8 > len(data):
            raise ValueError('CMSP 的关键点超出资源范围。')
        keys = list(struct.iter_unpack('<2f', data[offset:offset + 8 * count]))
        if not all(math.isfinite(t) and math.isfinite(v) for t, v in keys) or any(
            b[0] < a[0] for a, b in zip(keys, keys[1:])
        ):
            raise ValueError('CMSP 的关键点无效。')
        if mode == 0 and len(keys) > 2:
            keys = cubic_keys(keys)
        channels.append(keys)
    return {'key':key, 'name':name or data[16:48].split(b'\0', 1)[0].decode('ascii', 'replace'),
            'seconds':seconds, 'projection':projection, 'mode':mode, 'channels':channels}


def cubic_keys(keys):
    # Compute the natural spline coefficients used by EE 0x32DC78.
    widths = [b[0] - a[0] for a, b in zip(keys, keys[1:])]
    if any(h <= 0 for h in widths):
        raise ValueError('三次 CMSP 曲线含重叠的时间键。')
    count = len(keys)
    upper, rhs, coeff = [0.0] * count, [0.0] * count, [0.0] * count
    for i in range(1, count - 1):
        left, right = widths[i - 1:i + 1]
        diagonal = 2 * (left + right) - left * upper[i - 1]
        upper[i] = right / diagonal
        derivative = (keys[i + 1][1] - keys[i][1]) / right - (keys[i][1] - keys[i - 1][1]) / left
        rhs[i] = (derivative - left * rhs[i - 1]) / diagonal
    for i in range(count - 2, 0, -1):
        coeff[i] = rhs[i] - upper[i] * coeff[i + 1]
    return [(*key, value) for key, value in zip(keys, coeff)]


def elapsed_frames(segment, frame):
    animation = segment['animation']
    rate = base = animation['rate']
    previous, elapsed = animation.get('first', 0), 0.0
    for event in segment['events']:
        at = event['frame']
        if at >= frame:
            break
        if at >= 0 and event.get('rate'):
            at = max(at, previous)
            elapsed += (at - previous) / rate
            previous, rate = at, base * event['rate']
    return elapsed + (frame - previous) / rate
