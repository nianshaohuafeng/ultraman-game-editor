"""Read a pBAV tone and render its VAG sample once as mono PCM."""

from array import array
from io import BytesIO
import struct
import sys
import wave


def find_sample(bank, program, tone):
    if len(bank) < 24:
        return None
    programs, _, count = struct.unpack_from('<3H', bank, 0x12)
    tone_table = 0x820
    lengths = tone_table + programs * 512
    data = lengths + 512
    if not 0 <= program < programs or not 0 <= tone < 16 or not 1 <= count <= 255 or data > len(bank):
        return None
    attr = tone_table + program * 512 + tone * 32
    number = struct.unpack_from('<H', bank, attr + 22)[0]
    if not 1 <= number <= count:
        return None
    sizes = struct.unpack_from(f'<{number}H', bank, lengths + 2)
    start = data + 8 * sum(sizes[:-1])
    size = 8 * sizes[-1]
    if not size or size % 16 or start + size > len(bank):
        return None
    center, fine, note = bank[attr + 4:attr + 7]
    # 088E2E74 computes the tone pitch from these fields at 44.1 kHz SAS rate.
    rate = round(44100 * 2 ** ((note - center + fine / 128) / 12))
    if not 1000 <= rate <= 192000:
        return None
    return number, start, size, rate


def vag_wave(data, rate):
    coefficients = ((0, 0), (60, 0), (115, -52), (98, -55), (122, -60))
    pcm = array('h')
    prev = prev2 = 0
    for offset in range(0, len(data), 16):
        header, flags = data[offset:offset + 2]
        if flags == 7:
            break
        predictor, shift = header >> 4, header & 15
        if predictor >= len(coefficients):
            raise ValueError('此采样的 VAG 编码无法试听。')
        a, b = coefficients[predictor]
        for packed in data[offset + 2:offset + 16]:
            for nibble in (packed & 15, packed >> 4):
                signed = (nibble ^ 8) - 8
                sample = ((signed << 12) >> shift) + ((a * prev + b * prev2) >> 6)
                prev2, prev = prev, min(32767, max(-32768, sample))
                pcm.append(prev)
    if sys.byteorder != 'little':
        pcm.byteswap()
    result = BytesIO()
    with wave.open(result, 'wb') as output:
        output.setparams((1, 2, rate, 0, 'NONE', ''))
        output.writeframes(pcm.tobytes())
    return result.getvalue()
