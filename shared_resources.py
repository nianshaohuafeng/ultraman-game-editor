"""Browse and edit assets that are loaded independently of fighter models."""

import base64
from functools import cached_property
from pathlib import Path

from asset_transfer import pom_layout, replace_texture, texture_png
from fe3_audio import RATE, audio as fe3_audio, stream_samples
from fe3_formats import paste_png, png_for_pom, png_preview, pom_layout as fe3_pom_layout, require
from kgf_readers.pom_to_tm2 import decode_png
from music import atrac_info, decode_music
from move_eboot import BASE
from native_tables import bgm_rows


PNG = b'\x89PNG\r\n\x1a\n'
SIGNATURES = {'png': PNG, 'pom': b'POM\0', 'bgm': b'RIFF'}


def data_uri(mime, data):
    return f'data:{mime};base64,' + base64.b64encode(data).decode('ascii')


class SharedResources:
    def __init__(self, game):
        self.game = game
        self.ps2 = getattr(game, 'game_id', '') == 'fe3'

    @cached_property
    def entries(self):
        game = self.game
        rows = {}
        if self.ps2:
            for track, resource in enumerate(game.stream_resources):
                row = rows.setdefault((resource, -1), {
                    'id': resource, 'member': -1, 'kind': 'bgm', 'tracks': [],
                    'location': f'0x{resource:04X}', 'name': '',
                })
                row['tracks'].append(track)
            for row in rows.values():
                row['name'] = 'BGM ' + ' / '.join(f'{track:03d}' for track in row['tracks'])
            for part in game.parts.values():
                if part.kind != 'pom' or part.resource in game.owners:
                    continue
                label = f'0x{part.resource:04X}' + (f' / {part.member}' if part.member >= 0 else '')
                rows[part.key] = {'id': part.resource, 'member': part.member, 'kind': 'pom',
                                  'name': part.name, 'location': label}
        else:
            owned = {case['group'] for role in game.roles for case in role['cases']}
            catalog = {member: i for i, member, _ in bgm_rows(game, BASE)}
            for member, group, slot, kind in game.media_members:
                if kind == 'pom' and (group in owned or member in game.pom_forms):
                    continue
                location = f'YUM {member} · 组 {group} / {slot}'
                name = (f'BGM {catalog[member]:03d}' if member in catalog else 'ATRAC') if kind == 'bgm' else kind.upper()
                rows[member, -1] = {'id': member, 'member': -1, 'group': group,
                                    'kind': kind, 'name': name, 'location': location}
        return rows

    def row(self, key):
        require(len(key) == 2 and all(type(value) is int for value in key) and key in self.entries,
                '请选择当前镜像的通用资源。')
        return self.entries[key]

    def storage_key(self, key):
        return key if self.ps2 else (key[0], None)

    def read(self, key, original=False):
        kind = self.row(key)['kind']
        if self.ps2:
            return self.game.original(key) if original else self.game.data(key)
        reader = self.game.read_source_member if original else self.game.read_member
        return reader(key[0], SIGNATURES[kind])

    def catalog(self):
        edits = self.game.asset_edits
        return [{**row, 'edited': self.storage_key(key) in edits} for key, row in self.entries.items()]

    def details(self, key):
        row = self.row(key)
        data = self.read(key)
        kind = row['kind']
        result = {**row, 'size': len(data), 'edited': self.storage_key(key) in self.game.asset_edits}
        if kind == 'bgm':
            seconds = stream_samples(data) / RATE if self.ps2 else atrac_info(data)[1] / 44100
            return {**result, 'seconds': seconds, 'format': 'PS2 SPU ADPCM' if self.ps2 else 'PSP ATRAC3plus',
                    'native': '.bin' if self.ps2 else '.at3'}
        if kind == 'png':
            image = decode_png(data)
            return {**result, 'width': image.width, 'height': image.height, 'format': 'PNG', 'native': '.png'}
        layout = (fe3_pom_layout if self.ps2 else pom_layout)(data)
        return {**result, **{name: layout[name] for name in ('width', 'height', 'palette_count', 'color_entries')},
                'format': 'PS2 POM' if self.ps2 else 'PSP POM', 'native': '.pom'}

    def texture(self, key, palette):
        kind = self.row(key)['kind']
        require(kind in ('png', 'pom'), '请选择贴图资源。')
        data = self.read(key)
        if kind == 'png':
            return data_uri('image/png', data)
        return png_preview(data, palette, gs_alpha=True) if self.ps2 else data_uri('image/png', texture_png(data, palette))

    def audio(self, key, wav=False, cancel=None):
        row = self.row(key)
        require(row['kind'] == 'bgm', '请选择 BGM 音轨。')
        if self.ps2:
            return fe3_audio(self.game, row['tracks'][0], wav, cancel)
        return decode_music(self.read(key), wav, cancel)

    def export(self, key, mode, palette):
        kind = self.row(key)['kind']
        if mode == 'wav':
            return '.wav', self.audio(key, wav=True)
        if mode == 'png' and kind == 'pom':
            return '.png', (png_for_pom if self.ps2 else texture_png)(self.read(key), palette)
        require(mode == 'native', '请选择资源的导出格式。')
        data = self.read(key)
        if kind == 'bgm':
            return ('.bin', data) if self.ps2 else ('.at3', data[:atrac_info(data)[0]])
        return '.' + kind, data

    def validate_psp(self, key, data):
        row = self.row(key)
        original = self.read(key, original=True)
        kind = row['kind']
        if kind == 'bgm':
            end, _ = atrac_info(data)
            require(not any(data[end:]), 'ATRAC 的 RIFF 结束后还有非零数据。')
            if self.game.yum['groups'][row['group']][2] == 1:
                return data[:end]
            require(end <= len(original), f'该曲目位于多成员 YUM 组，最多可使用 {len(original):,} 字节。')
            return data[:end].ljust(len(original), b'\0')
        if kind == 'png':
            base, image = decode_png(original), decode_png(data)
            require((base.width, base.height) == (image.width, image.height),
                    f'PNG 尺寸须为 {base.width} × {base.height}。')
            require(len(data) <= len(original), f'PNG 最多可使用 {len(original):,} 字节。')
            return data.ljust(len(original), b'\0')
        require(pom_layout(original) == pom_layout(data) and len(original) == len(data)
                and original[:0x40] == data[:0x40], 'POM 须保留目标纹理头、像素布局及色表规格。')
        return data

    def _put(self, key, data):
        game = self.game
        game.ensure_open()
        if self.ps2:
            part = game.part(*key)
            return game._put(part, None if data is None else game.validate_import(part, data))
        if data is not None:
            data = self.validate_psp(key, data)
        before = game.snapshot()
        if data is None or data == self.read(key, original=True):
            game.asset_edits.pop(self.storage_key(key), None)
        else:
            game.asset_edits[self.storage_key(key)] = data
        game.texture.cache_clear()
        game.remember(before)
        return game.edit_status()

    def import_file(self, key, path, palette, palette_only=False):
        kind = self.row(key)['kind']
        path = Path(path)
        if palette_only and (kind != 'pom' or path.suffix.lower() != '.png'):
            raise ValueError('只换色表请选择 POM 资源和 PNG 文件。')
        data = path.read_bytes()
        if kind == 'pom' and path.suffix.lower() == '.png':
            if self.ps2:
                data = paste_png(self.read(key), data, palette, palette_only)
            else:
                data = replace_texture(self.read(key, original=True), self.read(key), data, '.png', palette, palette_only)
        return self._put(key, data)

    def copy_track(self, key, source):
        require(self.row(key)['kind'] == self.row(source)['kind'] == 'bgm', '请选择同一镜像的两首 BGM。')
        return self._put(key, self.read(source))

    def restore(self, key):
        self.row(key)
        return self._put(key, None)
