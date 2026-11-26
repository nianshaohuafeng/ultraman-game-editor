"""Decode the FE3 battle IPU stream without running the game."""

from pathlib import Path
import base64
from collections import deque
import math
import shutil
import struct
import subprocess
import tempfile

from fe3_formats import require


def movie_rate(number):
    # Battle draw 0x21C928 requests a picture each tick. Movie 142 alone
    # takes the alternate branch that advances every second draw.
    return 30 if number == 142 else 60


def movie_part(game, number):
    require(type(number) is int and 0 <= number < 166, '请选择原盘影片编号。')
    resource = game.workbench.tables.pointer(0x412C38,number * 3)
    part = game.part(resource,-1)
    require(part.kind == 'ipum','原版影片编号没有指向 IPU 资源。')
    return part


def movie_frame_count(game, number):
    part = movie_part(game,number)
    with game.path.open('rb') as image:
        image.seek(part.at)
        header = image.read(16)
    require(len(header) == 16 and header[:4] == b'ipum','IPU 影片头无效。')
    count, = struct.unpack_from('<I',header,12)
    require(1 < count <= (part.size-36)//8,'IPU 影片的帧数无效。')
    return count


class PictureQueue:
    """The four read windows at IPU +0xEC, replenished by 0x32C238."""

    def __init__(self, size, first):
        self.size = size
        self.cursor = first % size
        self.windows = deque()
        for _ in range(4):
            self._read(initial=True)

    def _read(self, initial=False):
        start = self.cursor
        end = start + 16
        if end > self.size or (initial and end == self.size):
            end = self.size-1
        if end > start:
            self.windows.append([start,end])
        # During the initial four requests 0x32B980 leaves the terminal
        # window empty. Replenishment wraps the next request to zero.
        self.cursor = end
        if not initial and end >= self.size-1:
            self.cursor = 0

    def seek(self, first):
        # 0x32C9D0 changes +0x130 only. It leaves the queued windows intact.
        if 0 <= first < self.size:
            self.cursor = first

    def next_picture(self):
        while not self.windows:
            self._read()
        window = self.windows[0]
        picture = window[0]
        window[0] += 1
        if window[0] >= window[1]:
            self.windows.popleft()
            self._read()
        return picture


class BattleFilm:
    def __init__(self, game):
        self.game = game
        self.number = self.queue = None
        self.loads = 0
        self.active = self.paused = False
        self.first = 0
        self.picture = None
        self.tick = self.draw_count = 0
        self.cues = []

    def load(self, number, first):
        count = movie_frame_count(self.game,number)
        self.first = (max(0,first) if self.loads & 1 else 0) % count
        self.queue = PictureQueue(count,self.first)
        self.number = number
        self.loads += 1
        self.picture = None
        self.paused = False

    def current(self):
        # 0x32CAC0 returns the decoded frame, or the load frame before decoding.
        return self.first if self.picture is None else self.picture

    def through(self, frame):
        # 0x27A8E0 queues the IPU picture before the script update of that tick.
        while self.tick <= math.floor(frame):
            if self.active and self.queue is not None:
                rate = movie_rate(self.number)
                if rate == 30:
                    self.draw_count += 1
                if not self.paused and (rate != 30 or self.draw_count & 1):
                    self.picture = self.queue.next_picture()
                if self.picture is not None:
                    self._record(self.number,self.picture,self.paused,rate)
            elif self.cues and self.cues[-1]['id'] is not None:
                self._record(None,0,True,60)
            self.tick += 1

    def _record(self, number, picture, paused, rate):
        last = self.cues[-1] if self.cues else None
        if last is not None and (last['id'],last['paused']) == (number,paused):
            offset = 0 if paused else (self.tick-last['frame'])*rate//60
            if last['first']+offset == picture:
                return
        self.cues.append({'frame':self.tick,'id':number,'first':picture,'paused':paused})


def ipu_frames(data):
    require(len(data) >= 28 and data[:4] == b'ipum', '需要 FE3 原生 IPU 影片。')
    width, height, count = struct.unpack_from('<HHI', data, 8)
    require(0 < width <= 640 and 0 < height <= 448 and width % 16 == height % 16 == 0
            and 0 < count <= (len(data) - 36) // 8, 'IPU 影片目录无效。')
    frames = []
    for index in range(count):
        sector, length, following = struct.unpack_from('<3I', data, 28 + 8 * index)
        start = 2048 * sector
        # Three original resources declare a final sector outside the file.
        # In game, completion is checked on the penultimate frame.
        if index == count - 1 and start + length > len(data):
            break
        require(start >= 36 + 8 * count and 0 < length <= (following - sector) * 2048
                and start + length <= len(data), 'IPU 影片的帧范围无效。')
        frame = data[start:start + length]
        marker = frame.rfind(b'\x00\x00\x01\xB0')
        require(marker > 0, 'IPU 帧缺少结束标记。')
        frames.append(frame[:marker + 4])
    require(bool(frames), 'IPU 影片没有可读取的帧。')
    header = struct.pack('<4sIHHI', b'ipum', sum(map(len, frames)), width, height, len(frames))
    return header + b''.join(frames), len(frames)


def movie_preview(game, number):
    part = movie_part(game,number)
    ffmpeg = shutil.which('ffmpeg')
    require(ffmpeg is not None, '播放原版必杀特效影片需要本机 ffmpeg。')
    resource = part.resource
    data, frames = ipu_frames(game.data(part.key))
    rate = movie_rate(number)
    with tempfile.TemporaryDirectory(prefix='fe3-ipu-') as directory:
        target = Path(directory) / 'effect.webm'
        result = subprocess.run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-xerror',
                                 '-r', str(rate), '-f', 'ipu', '-i', 'pipe:0', '-an',
                                 '-c:v', 'libvpx', '-crf', '15', '-b:v', '0', '-cpu-used', '5',
                                 '-g', '15', str(target)], input=data, capture_output=True, timeout=45,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        require(result.returncode == 0 and target.is_file(), '无法解码这段原版特效影片。')
        uri = 'data:video/webm;base64,' + base64.b64encode(target.read_bytes()).decode('ascii')
    return {'id':number, 'resource':resource, 'frames':frames, 'rate':rate, 'uri':uri}
