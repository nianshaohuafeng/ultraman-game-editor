"""Decode native PSP ATRAC tracks for listening or WAV export."""

from io import BytesIO
import shutil
import struct
import subprocess
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
