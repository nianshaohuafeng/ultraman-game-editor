"""Audio stream conversion, music banks and PSP voice sample preview."""

from array import array
from io import BytesIO
import shutil
import struct
import subprocess
import sys
import wave


ATRAC3PLUS = bytes.fromhex('bfaa23e958cb7144a119fffa01e4ce62')


def atrac_info(data):
    """Check an ATRAC3plus RIFF and return logical byte and sample counts."""
    if len(data) < 12 or data[:4] != b'RIFF' or data[8:12] != b'WAVE':
        raise ValueError('请选择双声道 44,100 Hz 的 ATRAC3plus（AT3）。')
    end = struct.unpack_from('<I', data, 4)[0] + 8
    if not 12 < end <= len(data):
        raise ValueError('ATRAC 的 RIFF 长度不完整。')
    at = 12
    chunks = {}
    while at + 8 <= end:
        kind, length = struct.unpack_from('<4sI', data, at)
        start = at + 8
        at = start + length + (length & 1)
        if at > end:
            raise ValueError('ATRAC 的 RIFF 区块被截断。')
        if kind in (b'fmt ', b'fact', b'data'):
            if kind in chunks:
                raise ValueError('ATRAC 包含重复的 RIFF 区块。')
            chunks[kind] = start, length
    fmt, fmt_length = chunks.get(b'fmt ', (0, 0))
    fact, fact_length = chunks.get(b'fact', (0, 0))
    _, audio_length = chunks.get(b'data', (0, 0))
    if (at != end or fmt_length < 40 or fact_length < 4
            or struct.unpack_from('<HHI', data, fmt) != (0xFFFE, 2, 44100)
            or data[fmt + 24:fmt + 40] != ATRAC3PLUS):
        raise ValueError('请选择双声道 44,100 Hz 的 ATRAC3plus（AT3）。')
    block = struct.unpack_from('<H', data, fmt + 12)[0]
    samples = struct.unpack_from('<I', data, fact)[0]
    if not block or not audio_length or audio_length % block or not 0 < samples <= audio_length // block * 2048:
        raise ValueError('ATRAC 的编码块或样本数无效。')
    return end, samples


def decode_audio(data, rate, wav=False, cancel=None, input_flags=(), samples=None):
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        raise ValueError('BGM 试听和导出 WAV 需要本机 ffmpeg。')
    command = [ffmpeg, '-hide_banner', '-loglevel', 'error', *input_flags, '-i', 'pipe:0',
               '-map', '0:a:0', '-ac', '2', '-ar', str(rate if wav else 44100)]
    if samples is not None:
        command += ['-af', f'atrim=end_sample={samples}']
    command += ['-f', 's16le'] if wav else ['-c:a', 'libmp3lame', '-b:a', '128k', '-f', 'mp3']
    with subprocess.Popen([*command, 'pipe:1'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)) as process:
        incoming = data
        while True:
            if cancel is not None and cancel.is_set():
                process.kill()
                process.communicate()
                return None
            try:
                decoded, _ = process.communicate(incoming, timeout=0.15 if cancel is not None else None)
                break
            except subprocess.TimeoutExpired:
                incoming = None
    if process.returncode or not decoded:
        raise ValueError('无法解码选中的 BGM。')
    if not wav:
        return decoded
    output = BytesIO()
    with wave.open(output, 'wb') as audio:
        audio.setparams((2, 2, rate, 0, 'NONE', ''))
        audio.writeframes(decoded)
    return output.getvalue()


def decode_music(data, wav=False, cancel=None):
    end, samples = atrac_info(data)
    return decode_audio(data[:end], 44100, wav, cancel, samples=samples)


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
