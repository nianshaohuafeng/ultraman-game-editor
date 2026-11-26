"""FE3 post-knockback charge gauge, state 95 and EE updater 0x2B4C70."""
import math
import struct


CHARGE_RATES = ((0x4BA60C, 1.5), (0x4BA608, 3.0))
FULL = 256.0


def f32(value):
    return struct.unpack('<f', struct.pack('<f', value))[0]


def fill_frames(rate, threshold=FULL):
    if not math.isfinite(rate) or rate <= 0:
        raise ValueError('ELF 蓄力增量必须为有限的正数。')
    if not 0 <= threshold <= FULL:
        return None
    position = 0.0
    frames = 0
    while position < threshold:
        after = f32(position + rate)
        if after == position:
            raise ValueError('此浮点增量无法使原生蓄力槽达到上限。')
        frames += 1
        position = after
        if position >= threshold:
            break
        # Float spacing is constant inside this exponent interval. Leave two
        # real additions at the boundary, including round-to-even ties.
        edge = min(threshold, math.ldexp(1., math.frexp(position)[1]))
        following = f32(position + rate)
        step = following - position
        if step > 0 and f32(following + rate) - following == step:
            jumps = max(0, math.floor((edge - position) / step) - 2)
            position += jumps * step
            frames += jumps
    return frames


def rate_for_frames(frames):
    if type(frames) is not int or not 1 <= frames <= 2**25:
        raise ValueError('请输入可由原生单精度蓄力槽表示的正整数帧数。')
    rate = f32(FULL / frames)
    try:
        if fill_frames(rate) == frames:
            return rate
    except ValueError:
        pass
    low, high = 0, 0x43800000  # positive float bits through 256.0
    while low + 1 < high:
        middle = (low + high) // 2
        candidate = struct.unpack('<f', struct.pack('<I', middle))[0]
        try:
            enough = fill_frames(candidate) <= frames
        except ValueError:
            enough = False
        if enough:
            high = middle
        else:
            low = middle
    rate = struct.unpack('<f', struct.pack('<I', high))[0]
    if fill_frames(rate) != frames:
        raise ValueError('原生浮点累加无法精确表示这个帧数。')
    return rate
