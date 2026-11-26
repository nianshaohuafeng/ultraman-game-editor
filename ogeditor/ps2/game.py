"""Read and edit the two locally verified SLPS_254.41 resource layouts."""

from collections import defaultdict
from dataclasses import dataclass, replace
from functools import cached_property, lru_cache
from hashlib import sha256
import math
from pathlib import Path
import re
import shutil
import struct

from ogeditor.psp.game import selected
from ogeditor.readers import encode_dfs_enc, list_iso_files, read_iso_file
from ogeditor.ps2.formats import decode_dfs_enc
from ogeditor.ps2.formats import (BOM_DAMAGE_FIELDS, damage_fields, Bom, EDITABLE, NATIVE_TAGS, align, clips, model_signature, paste_png, png_for_pom, png_preview,
                         pom_layout, require, unpacked_tag, word)
from ogeditor.ps2.native import (DEFAULT_FINISHERS, GUARD_TYPES, NativeTables, RATING_TABLE, SHOULDER_ASSIST,
                        SHOULDER_ASSIST_WORD, SKILL_CANDIDATES, native_offset,
                        rating_address, rating_grade, rating_value_for_grade)
from ogeditor.ps2.preview import clip_index, key_rows, make_model, replace_key, replace_key_rows, single_clip, replace_clip
from ogeditor.ps2.transfer import Transfers, audio_slots, audio_table, compile_transfers
from ogeditor.ps2.add import next_candidate
from ogeditor.ps2.audio import stream_resources, stream_samples
from ogeditor.ps2.names import standalone_pom_names
from ogeditor.ps2.native import CHARGE_RATES, fill_frames, rate_for_frames
from ogeditor.ps2.sound import install as install_sound, read_codes as read_sound_codes
from ogeditor.ps2.expand import ElfSpace, prefix_records, stock_elf, write_expanded_iso
from ogeditor.ps2.workbench import THROW_STATES, Workbench
from iso_save import new_output
from ogeditor.shared_resources import SharedResources


BOOT = 'SLPS_254.41'
SECTOR = 2048
TABLE = 0x413F98
TOTAL = 0xDE0
DFS_TOP = 0x426DD8
DFS_CHILD = 0x421D98
HP_TABLE = 0x3F0EE0
FORM_DEFENSE = 0x3F0DF0
DEFENSE_FORMS = frozenset((*range(17, 23), 25))
JUMP_TABLES = (0x3E4898, 0x3E4938)
# Kept in the base checksum mask so old exported images still open.
LEGACY_BINDINGS = DEFAULT_FINISHERS
REVISIONS = {
    '29aabed1a47bd00dd4db384e7bd60e92f6340c359b7a18df382a682523eab756':'日版首发',
    '804b6dd0ad115c482e97e47a62caafb520b39c744a8b265847fcad4676c9dca4':'本地资源版',
}
ROSTER = (
    ('man','初代'), ('sev','赛文'), ('jack','杰克'), ('ace','艾斯'), ('taro','泰罗'), ('leo','雷欧'),
    ('zof','佐菲'), ('bal','巴尔坦'), ('kj','金古桥'), ('bem','贝姆斯塔'), ('ak','艾斯杀手'),
    ('tai','泰兰特'), ('mag','马格马星人'), ('gomo','哥莫拉'), ('zet','杰顿'), ('dada','达达'),
    ('eity','爱迪'), ('tiga','迪迦'), ('dyna','戴拿'), ('gaia','盖亚'), ('agul','阿古茹'),
    ('cos','高斯'), ('jus','杰斯提斯'), ('lege','雷杰多'), ('golz','哥尔赞'), ('rei','雷丘巴斯'),
    ('ganq','眼Q'), ('gb','葛洛卡·主教'), ('rk','雷德王'), ('gudo','古墩'), ('twin','双尾怪'), ('vaki','巴克西姆'),
    ('evil','邪恶迪迦'), ('robo','艾斯机器人'), ('dsev','妄想赛文'), ('idyna','假戴拿'),
    ('astra','阿斯特拉'), ('gata','加坦杰厄'), ('sb','圆盘生物'),
)
PORTRAIT = re.compile(r'(?:^|/)c_([a-z0-9]+)_pom\.enc$', re.IGNORECASE)


def read_at(handle, offset, length):
    handle.seek(offset)
    result = handle.read(length)
    require(len(result) == length, '镜像资源被截断。')
    return result


def integer(value, lower, upper, name):
    require(type(value) is int and lower <= value <= upper, f'{name}须为 {lower} 至 {upper} 的整数。')
    return value


@dataclass(frozen=True)
class Part:
    resource: int
    member: int
    at: int
    size: int
    span: int
    name: str
    kind: str
    compressed: bool
    expanded: int

    @property
    def key(self):
        return self.resource, self.member


def child_length_locations(elf):
    start = native_offset(elf, DFS_CHILD, 2568 * 8)
    result = defaultdict(list)
    for offset in range(start, start + 2568 * 8, 8):
        result[word(elf, offset)].append(offset + 4)
    return result


def resource_part(handle, resource, member, at, size, span, name, expanded):
    header = read_at(handle, at, min(size, 28))
    compressed = header.startswith(b'DFS\x01')
    if compressed:
        require(len(header) >= 13 and (word(header, 4) == expanded > 0) and 13 <= word(header, 8) <= size,
                'DFS 资源声明长度不一致。')
    else:
        require(expanded == 0, '原生资源的解压长度表不一致。')
    tag = unpacked_tag(header)
    kind = 'fum' if tag == b'FUM\0' else NATIVE_TAGS.get(tag, 'bin')
    return Part(resource, member, at, size, span, name, kind, compressed, expanded)


def encode_verified(data):
    packed = encode_dfs_enc(data)
    require(decode_dfs_enc(packed) == data, 'DFS 重压缩读回校验失败。')
    return packed


class FE3Game:
    game_id = 'fe3'

    def __init__(self, path, files=None):
        self.path = Path(path).resolve(strict=True)
        self.stamp = self.file_stamp()
        self.files = {f.path.upper(): f for f in (files if files is not None else list_iso_files(self.path))}
        require(BOOT in self.files and all(f'FILE{i}.BIN' in self.files for i in range(1, 5)),
                '需要带有 SLPS_254.41 的格斗进化3 ISO。')
        self.boot = self.files[BOOT]
        self.elf = read_iso_file(self.path, self.boot)
        require(self.elf[:7] == b'\x7fELF\x01\x01\x01', '格斗进化3 主 ELF 格式无效。')
        self.table = native_offset(self.elf, TABLE, 16 * TOTAL)
        self.hp_at = native_offset(self.elf, HP_TABLE, 18 * len(ROSTER))
        self.form_defense_at = native_offset(self.elf, FORM_DEFENSE, 6 * len(ROSTER))
        self.jump_locations = tuple(native_offset(self.elf, address, 4 * len(ROSTER)) for address in JUMP_TABLES)
        self.extension = prefix_records(self.elf)
        baseline = stock_elf(self.elf,self.extension)
        tables = NativeTables(self.elf)
        self.sound_extensions, _ = read_sound_codes(self.elf)
        actor_banks = set()
        for role in range(len(ROSTER)):
            desc, sizes = tables.descriptor(role)
            actor_banks.update(tables.words(desc[18],sizes[11]))
        self.fighter_sound_pairs = {bank:tables.at(0x42F5B8+32*bank,'<2i') for bank in actor_banks if 0 <= bank < 140}
        self.fighter_sound_parts = {(part,-1) for pair in self.fighter_sound_pairs.values() for part in pair if part >= 0}
        self.stream_resources = stream_resources(tables)
        self.stream_parts = frozenset((resource, -1) for resource in self.stream_resources)
        stock_tables = NativeTables(baseline)
        self.camera_groups = {tables.pointer(tables.pointer(0x40A908,role),0) for role in range(len(ROSTER))}
        binding_size = 48*len(ROSTER)
        bindings_at = native_offset(baseline,LEGACY_BINDINGS,binding_size)
        self.skill_records = {(role, skill.id):skill for role in range(len(ROSTER))
                              for kind in range(3) for skill in tables.battle_skill_rows(role, kind)}
        self.binding_rows = {(role, slot):tables.form_words(role, slot)
                             for role in range(len(ROSTER)) for slot in range(tables.descriptor(role)[1][4])}
        self.binding_locations = {address:native_offset(self.elf, address, 4)
                                  for row in self.binding_rows.values() for _, address in row}
        self.child_lengths = child_length_locations(self.elf)
        self.parts, self.children, self.owners = self._index(tables)
        top_lengths = native_offset(self.elf, DFS_TOP, TOTAL * 4)
        self.encoded_lengths = {key: self.child_lengths[key[0]][key[1]] if key[1] >= 0 else top_lengths+4*key[0]
                                for key,part in self.parts.items() if part.compressed and part.kind in ('bom','anm')}
        slots, _, _ = audio_slots(stock_tables)
        at = native_offset(baseline,audio_table(stock_tables),4)
        normalized = bytearray(baseline)
        self.charge_locations = [native_offset(self.elf, va, 4) for va, _ in CHARGE_RATES]
        for (va, original), pos in zip(CHARGE_RATES, self.charge_locations):
            fill_frames(struct.unpack_from('<f', self.elf, pos)[0])
            struct.pack_into('<f', normalized, native_offset(baseline, va, 4), original)
        normalized[self.hp_at:self.hp_at + 18 * len(ROSTER)] = bytes(18 * len(ROSTER))
        normalized[self.form_defense_at:self.form_defense_at + 6 * len(ROSTER)] = bytes(6 * len(ROSTER))
        rating_size = 4 * SKILL_CANDIDATES * len(ROSTER)
        rating_at = native_offset(baseline, RATING_TABLE, rating_size)
        normalized[rating_at:rating_at + rating_size] = bytes(rating_size)
        for pos in self.jump_locations:
            normalized[pos:pos + 4 * len(ROSTER)] = bytes(4 * len(ROSTER))
        for pos in range(self.table + 8, self.table + 16 * TOTAL, 16):
            normalized[pos:pos + 4] = bytes(4)
        self.stock_menu_candidates = set()
        for role in range(len(ROSTER)):
            for kind in range(3):
                for record in stock_tables.skill_rows(role,kind):
                    self.stock_menu_candidates.add((role,record.id))
                    size = 4*len(record.words)
                    pos = native_offset(baseline,record.va,size)
                    normalized[pos:pos+size] = bytes(size)
        for pos in self.binding_locations.values():
            normalized[pos:pos + 4] = bytes(4)
        for pos in self.encoded_lengths.values():
            normalized[pos:pos + 4] = bytes(4)
        for slot in slots.values():
            pos = at + 4*slot
            normalized[pos:pos+4] = bytes(4)
        normalized[bindings_at:bindings_at+binding_size] = bytes(binding_size)
        self.revision = REVISIONS.get(sha256(normalized).hexdigest())
        require(self.revision is not None, 'SLPS_254.41 的代码或容器布局与已核对的两个版本不同。')
        self.original = lru_cache(maxsize=18)(self._original)
        self._encode_small = lru_cache(maxsize=8)(encode_verified)
        self.asset_edits, self.native_edits = {}, {}
        self.skill_edits, self.throw_edits, self.binding_edits, self.jump_edits = {}, {}, {}, {}
        self.rating_edits = {}
        self.charge_edits = {}
        self.transfers, self.additions, self.transfer_plan = {}, {}, Transfers()
        self.workbench = Workbench(self)
        self.transfer_plan = compile_transfers(self, self.transfers, self.additions)
        self.undo, self.redo = [], []
        self.saved = self.snapshot()
        self.ensure_open()

    def file_stamp(self):
        stat = self.path.stat()
        return stat.st_size, stat.st_mtime_ns, stat.st_ino

    def ensure_open(self):
        require(self.file_stamp() == self.stamp, '源 ISO 已变化，请重新打开。')

    def read_iso_entry(self, path):
        return read_iso_file(self.path, self.files[path.upper()])

    def _index(self, tables):
        parts, groups, owners = {}, {}, {}
        pom_names = standalone_pom_names(tables, ROSTER)
        lengths = native_offset(self.elf, DFS_TOP, TOTAL * 4)
        packs = [self.files[f'FILE{i}.BIN'] for i in range(1, 5)]
        with self.path.open('rb') as source:
            for resource in range(TOTAL):
                offset, span, size, container = struct.unpack_from('<4I', self.elf, self.table + 16 * resource)
                pack = selected(packs, container)
                require(0 < size <= span and not offset % SECTOR and not span % SECTOR and offset + span <= pack.size
                        and pack.offset + offset + span <= self.stamp[0], '格斗进化3资源表越过容器边界。')
                at = pack.offset + offset
                main = resource_part(source, resource, -1, at, size, span, f'{resource:04X}',
                                     word(self.elf, lengths + 4 * resource))
                if main.kind == 'pom' and resource in pom_names:
                    main = replace(main, name=pom_names[resource])
                parts[main.key] = main
                if main.kind != 'fum':
                    continue
                count = word(read_at(source, at, 8), 4)
                indexed = self.child_lengths[resource]
                require(len(indexed) <= count <= (span-8)//0x80 and
                        (count == len(indexed) or self.extension and resource in self.camera_groups),
                        'FUM 资源目录和 ELF 解包索引不一致。')
                directory = read_at(source, at + 8, 128 * count)
                items = []
                last = align(8 + 128 * count, 64)
                for member, (name, relative, capacity, length) in enumerate(
                        struct.iter_unpack('<116sIII', directory)):
                    require(last <= relative and relative % 64 == capacity % 64 == 0
                            and 0 < length <= capacity and relative + capacity <= span, 'FUM 子项越过分配边界。')
                    path = name.split(b'\0', 1)[0].decode('ascii', errors='replace')
                    expanded = word(self.elf,indexed[member]) if member < len(indexed) else 0
                    child = resource_part(source,resource,member,at+relative,length,capacity,path,expanded)
                    require(member < len(indexed) or child.kind == 'cmsp','扩展 FUM 尾部须为原生镜头。')
                    items.append(child)
                    parts[child.key] = child
                    last = relative + capacity
                groups[resource] = tuple(items)
                name = PORTRAIT.search(items[0].name) if items else None
                if name and name.group(1).lower() in dict(ROSTER):
                    role = next(i for i, (code, _) in enumerate(ROSTER) if code == name.group(1).lower())
                    owners[resource] = role
        # The descriptor contains the real bank IDs. Some banks are outside
        # the run of consecutive fighter packages (including Evil Tiga's).
        consumers = defaultdict(set)
        for role in range(len(ROSTER)):
            for resource, _ in tables.banks(role):
                if parts[resource, -1].kind == 'anm':
                    consumers[resource].add(role)
        for resource, roles in consumers.items():
            if len(roles) == 1:
                owners[resource] = next(iter(roles))
        return parts, groups, owners

    def catalog(self):
        return {'game':'fe3', 'name':self.path.name, 'path':str(self.path), 'revision':self.revision,
                'roles':[{'id':i, 'code':code.upper(), 'name':name} for i, (code, name) in enumerate(ROSTER)],
                'edits':self.edit_status()}

    def part(self, resource, member):
        require(type(resource) is int and type(member) is int and (resource, member) in self.parts,
                '请选择当前镜像的资源。')
        return self.parts[resource, member]

    @cached_property
    def shared_resources(self):
        return SharedResources(self)

    def list_parts(self):
        rows = []
        for part in self.parts.values():
            if part.member < 0 and part.resource in self.children:
                children = self.children[part.resource]
                name = children[0].name if children else part.name
            else:
                name = part.name
            edited = self.asset_edits.get(part.key, self.transfer_plan.resources.get(part.key))
            rows.append({'id':part.resource, 'member':part.member, 'name':name, 'kind':part.kind,
                         'role':self.owners.get(part.resource), 'compressed':part.compressed,
                         'size':len(edited) if edited is not None else part.expanded or part.size,
                         'edited':edited is not None})
        return rows

    def _original(self, key):
        part = self.part(*key)
        with self.path.open('rb') as source:
            raw = read_at(source, part.at, part.span if part.kind == 'fum' else part.size)
        return decode_dfs_enc(raw) if part.compressed else raw

    def base_data(self, key):
        return self.transfer_plan.resources[key] if key in self.transfer_plan.resources else self.original(key)

    def data(self, key):
        return self.asset_edits[key] if key in self.asset_edits else self.base_data(key)

    def details(self, resource, member):
        part = self.part(resource, member)
        result = {'kind':part.kind, 'name':part.name, 'id':resource, 'member':member, 'offset':part.at,
                  'capacity':part.span, 'size':part.expanded or part.size, 'compressed':part.compressed,
                  'editable':part.kind in EDITABLE,
                  'edited':part.key in self.asset_edits or part.key in self.transfer_plan.resources}
        if part.kind not in EDITABLE:
            return result
        data = self.data(part.key)
        result['size'] = len(data)
        if part.kind == 'pom':
            try:
                layout = pom_layout(data)
                result.update({k:layout[k] for k in ('width', 'height', 'palette_count', 'color_entries')})
            except ValueError as error:
                result['texture_error'] = str(error)
        elif part.kind == 'anm':
            result['clips'] = [{**clip.fields, 'tracks':len(clip.tracks), 'channels':clip.tracks, 'size':clip.size}
                               for clip in clips(data)]
        elif part.kind == 'mdl':
            nodes, fragments = model_signature(data)
            result.update(nodes=len(nodes), fragments=fragments)
        elif part.kind == 'bom':
            bom = Bom(data)
            result.update(scripts=bom.counts(), shapes=bom.shapes)
        return result

    def texture(self, resource, member, palette):
        part = self.part(resource, member)
        require(part.kind == 'pom', '请选择 POM。')
        return png_preview(self.data(part.key), palette, gs_alpha=True)

    def events(self, resource, member, script):
        part = self.part(resource, member)
        require(part.kind == 'bom', '请选择 BOM。')
        return list(Bom(self.data(part.key)).events(script))

    def attributes(self, role):
        selected(ROSTER, role)
        rows = []
        for band in range(9):
            p = self.hp_at + 18 * role + 2 * band
            raw = self.native_edits.get(p, struct.unpack_from('<h', self.elf, p)[0])
            rows.append({'band':band, 'minimum':band * 32, 'maximum':band * 32 + 31, 'raw':raw,
                         'original':struct.unpack_from('<h', self.elf, p)[0], 'va':HP_TABLE + 18 * role + 2 * band})
        return rows

    def defense(self, role):
        selected(ROSTER, role)
        return [self._defense_row(role, shape) for shape in range(3 if role in DEFENSE_FORMS else 1)]

    def _defense_row(self, role, shape):
        address = FORM_DEFENSE + 6 * role + 2 * shape
        offset = native_offset(self.elf, address, 2)
        source, = struct.unpack_from('<h', self.elf, offset)
        return {'shape':shape, 'raw':self.native_edits.get(offset, source), 'original':source, 'va':address}

    def jump(self, role):
        selected(ROSTER, role)
        original = tuple(struct.unpack_from('<f', self.elf, offset + 4 * role)[0] for offset in self.jump_locations)
        return {'values':self.jump_edits.get(role, original), 'source':original,
                'va':tuple(address + 4 * role for address in JUMP_TABLES)}

    def jump_change(self, role, values):
        original = self.jump(role)['source']
        if values is None:
            return None
        require(isinstance(values, (list, tuple)) and len(values) == 2 and
                all(type(x) in (int, float) and math.isfinite(x) for x in values) and 0 <= values[0] <= 32767
                and -32767 <= values[1] < 0, '跳跃初速度须非负，重力须为负数。')
        values = struct.unpack('<2f', struct.pack('<2f', *values))
        require(values[1] < 0, '重力值低于 float32 最小精度。')
        return values if values != original else None

    def edit_jump(self, role, values):
        value = self.jump_change(role, values)
        before = self.snapshot()
        if value is None:
            self.jump_edits.pop(role, None)
        else:
            self.jump_edits[role] = value
        return self.remember(before)

    def charge(self):
        rates = [self.charge_edits.get(i, struct.unpack_from('<f', self.elf, pos)[0])
                 for i, pos in enumerate(self.charge_locations)]
        return {'frames': [fill_frames(rate) for rate in rates], 'rates': rates, 'edited': bool(self.charge_edits)}

    def charge_changes(self, values):
        if values is None:
            return {}
        require(isinstance(values, list) and len(values) == 2, '请填写两种分支的蓄满帧数。')
        changes = {}
        for i, (pos, frames) in enumerate(zip(self.charge_locations, values)):
            source, = struct.unpack_from('<f', self.elf, pos)
            require(type(frames) is int and frames > 0, '蓄满时间须为正整数帧。')
            if frames != fill_frames(source):
                changes[i] = rate_for_frames(frames)
        return changes

    def edit_charge(self, values):
        self.ensure_open()
        changes = self.charge_changes(values)
        before = self.snapshot()
        self.charge_edits = changes
        return self.remember(before)

    def snapshot(self):
        return {'assets':self.asset_edits.copy(), 'native':self.native_edits.copy(),
                'skills':self.skill_edits.copy(), 'throws':self.throw_edits.copy(),
                'ratings':self.rating_edits.copy(),
                'bindings':self.binding_edits.copy(), 'jump':self.jump_edits.copy(), 'charge':self.charge_edits.copy(),
                'transfers':self.transfers.copy(), 'additions':self.additions.copy(), 'sounds':self.sound_extensions.copy()}

    def edit_status(self):
        return {'assets':len(self.asset_edits), 'native':len(self.native_edits) + len(self.skill_edits)
                + len(self.throw_edits) + len(self.binding_edits) + len(self.jump_edits)
                + len(self.transfers) + len(self.additions) + len(self.charge_edits) + len(self.rating_edits),
                'transfers':len(self.transfers) + len(self.additions),
                'undo':bool(self.undo),
                'redo':bool(self.redo), 'unsaved':self.saved != self.snapshot()}

    def remember(self, before):
        if before != self.snapshot():
            self.undo.append(before)
            self.redo.clear()
        return self.edit_status()

    def _restore_edits(self, state):
        self.asset_edits, self.native_edits, self.skill_edits = state['assets'], state['native'], state['skills']
        self.rating_edits = state['ratings']
        self.throw_edits = state['throws']
        self.binding_edits = state['bindings']
        self.jump_edits = state['jump']
        self.charge_edits = state['charge']
        self.transfers, self.additions = state['transfers'], state['additions']
        self.sound_extensions = state['sounds']

    def step_history(self, redo=False):
        self.ensure_open()
        stack, opposite = (self.redo, self.undo) if redo else (self.undo, self.redo)
        if stack:
            before = self.snapshot()
            self._restore_edits(stack[-1])
            try:
                self.transfer_plan = compile_transfers(self, self.transfers, self.additions)
            except Exception:
                self._restore_edits(before)
                raise
            opposite.append(before)
            stack.pop()
        return self.edit_status()

    def _put(self, part, changed):
        before = self.snapshot()
        if changed is None or changed == self.base_data(part.key):
            self.asset_edits.pop(part.key, None)
        else:
            self.asset_edits[part.key] = changed
        return self.remember(before)

    def validate_import(self, part, data):
        if part.key in self.stream_parts:
            stream_samples(data)
            return data
        require(part.kind in EDITABLE and data[:4] in NATIVE_TAGS and NATIVE_TAGS[data[:4]] == part.kind,
                '请选择与目标一致的 PS2 原生格式。')
        original = self.base_data(part.key)
        require(part.kind in ('anm','bom') or len(data) == len(original),
                '原生替换须保留目标资源的解包字节数。')
        if part.kind == 'mdl':
            require(model_signature(data)[0] == model_signature(original)[0], 'MDL 节点绑定与目标不同。')
        elif part.kind == 'anm':
            require(len(data) <= part.span or self.allows_growth(part.key),
                    'ANM 库超过源镜像给该资源分配的读取空间。')
            require([c.tracks for c in clip_index(data)] == [c.tracks for c in clip_index(original)],
                    'ANM 片段或轨道编号与目标不同。')
        elif part.kind == 'bom':
            a, b = Bom(original), Bom(data)
            require(a.scripts == b.scripts and b.shapes >= a.shapes,
                    'BOM 须保留原生脚本和原有形状编号。')
            b.validate()
        elif part.kind == 'pom':
            require(pom_layout(data) == pom_layout(original) and data[0x0C:0x40] == original[0x0C:0x40],
                    'POM 像素格式、色表布局和 GS 绑定须与目标一致。')
        return data

    def replace_asset(self, resource, member, path, palette=0, palette_only=False):
        part = self.part(resource, member)
        if path is None:
            return self._put(part, None)
        if part.kind == 'pom' and palette_only and Path(path).suffix.lower() != '.png':
            raise ValueError('只换色表请选择 PNG 文件。')
        raw = Path(path).read_bytes()
        if Path(path).suffix.lower() == '.png':
            require(part.kind == 'pom', 'PNG 只能导入 POM。')
            integer(palette, 0, pom_layout(self.data(part.key))['palette_count'] - 1, 'POM 色表')
            raw = paste_png(self.data(part.key), raw, palette, palette_only)
        elif raw.startswith(b'DFS\x01'):
            require(word(raw, 4) == len(self.original(part.key)), 'DFS 解包尺寸与目标不同。')
            raw = decode_dfs_enc(raw)
        return self._put(part, self.validate_import(part, raw))

    def edit_attributes(self, role, band, multiplier):
        selected(ROSTER, role)
        integer(band, 0, 8, '生命分段')
        p = self.hp_at + 18 * role + 2 * band
        return self._edit_defense(p, multiplier)

    def edit_defense(self, role, shape, multiplier):
        row = selected(self.defense(role), shape)
        return self._edit_defense(native_offset(self.elf, row['va'], 2), multiplier)

    def _edit_defense(self, offset, multiplier):
        before = self.snapshot()
        if multiplier is None or multiplier == struct.unpack_from('<h', self.elf, offset)[0]:
            self.native_edits.pop(offset, None)
        else:
            self.native_edits[offset] = integer(multiplier, 0, 32767, 'Q7 承伤值')
        return self.remember(before)

    def skill_change(self, role, candidate, values):
        key = role, candidate
        require(type(role) is int and type(candidate) is int,'请选择当前角色的技能候选。')
        skill = self.skill_record(role,candidate)
        if values is None:
            return key, None
        base = self.transfer_plan.skill_words.get(key,skill.words)
        if type(values) in (float, int) and skill.kind == 1:
            # Projects from the previous version keep only the f32 power value.
            require(math.isfinite(values) and 0 <= values <= 32767, '请输入有效的必杀蓄力门槛。')
            words = list(self.skill_edits.get(key, base))
            words[3], = struct.unpack('<I', struct.pack('<f', values))
        else:
            require(isinstance(values, (list, tuple)) and len(values) == len(skill.words), '技能记录字段数不符。')
            words = [integer(v, 0, 0xFFFFFFFF, '技能参数') for v in values]
        if skill.kind == 1:
            power, = struct.unpack('<f', struct.pack('<I', words[3]))
            require(math.isfinite(power) and 0 <= power <= 32767, '必杀 +0x0C 须为可用的 float32 蓄力门槛。')
        if words[0] != skill.words[0]:
            if skill.kind == 0:
                require(words[0] in GUARD_TYPES, '请选择原生屏障类型。')
            elif not (skill.kind == 2 and (words[0] in (0, SHOULDER_ASSIST_WORD) or words[0] == base[0])):
                self.workbench.require_action(role, words[0])
        require(words[2] == skill.words[2] or words[2] < 20, '开放位须对应原生候选序号 0–19。')
        if key in self.transfers or key in self.additions:
            require(words[:3] == list(base[:3]), '配招记录已绑定各自的状态、形态和候选号。')
        if not skill.menu_candidate:
            require(words[:3] == list(base[:3]), '固定必杀使用原版启动状态和记录编号。')
        return key, tuple(words) if tuple(words) != base else None

    def edit_skill(self, role, candidate, values):
        key, value = self.skill_change(role, candidate, values)
        saved = self.snapshot()
        if value is None:
            self.skill_edits.pop(key, None)
        else:
            self.skill_edits[key] = value
        return self.remember(saved)

    def edit_support_skill(self, role, candidate, value):
        require(type(role) is int and type(candidate) is int, '请选择当前角色的防御技或辅助技。')
        skill = self.skill_record(role, candidate)
        require(skill.kind in (0, 2), '请选择当前角色的防御技或辅助技。')
        require(type(value) is int, '请选择适用于这条技能的原生效果。')
        words = self.current_skill_words(role, candidate)
        action = SHOULDER_ASSIST_WORD if value == SHOULDER_ASSIST else value
        require(action == words[0] or any(choice == value for choice, _, _ in self.workbench.support_choices(role, skill)),
                '请选择适用于这条技能的原生效果。')
        return self.edit_skill(role, candidate, (action, *words[1:]))

    def copy_finisher_settings(self, role, candidate, donor, donor_candidate):
        target = self.skill_record(role,candidate)
        source = self.skill_record(donor,donor_candidate)
        require(target and source and target.kind == source.kind == 1, '请选择两个角色的必杀候选。')
        current = self.current_skill_words(role, candidate)
        values = self.current_skill_words(donor, donor_candidate)
        # These inputs have the same semantics for every role. State and
        # candidate indices are local to the recipient's module and effect bank.
        defense = (current[4] & 0xFFFF0000) | (values[4] & 0xFFFF)
        return self.edit_skill(role, candidate, (*current[:3], values[3], defense))

    def skill_record(self,role,candidate):
        key = role,candidate
        if key in self.additions:
            donor,source,_ = self.additions[key]
            kind = self.skill_records[donor,source].kind
            record = next((item for item in self.workbench.tables.skill_rows(role,kind) if item.id == candidate),None)
            require(record is not None,'新增技能候选缺少原生记录。')
            return record
        require(key in self.skill_records,'请选择当前角色的原生技能候选。')
        return self.skill_records[key]

    def current_skill_words(self, role, candidate):
        key = role, candidate
        base = self.transfer_plan.skill_words.get(key)
        return self.skill_edits.get(key,base if base is not None else self.skill_record(*key).words)

    def base_skill_rating(self, role, candidate):
        require(type(role) is int and type(candidate) is int and self.skill_record(role, candidate).kind == 1,
                '请选择当前角色的必杀候选。')
        return self.workbench.tables.finisher_rating(role, candidate)

    def skill_rating(self, role, candidate):
        key = role, candidate
        base = self.workbench.tables.finisher_rating(*key)
        value = self.rating_edits.get(key, base)
        return {'value':value, 'grade':rating_grade(value), 'equippedAtOne':rating_grade(value, 1),
                'edited':key in self.rating_edits}

    def edit_skill_rating(self, role, candidate, grade):
        self.ensure_open()
        key = role, candidate
        base = self.base_skill_rating(*key)
        before = self.snapshot()
        value = base if grade is None else rating_value_for_grade(self.rating_edits.get(key, base), grade)
        if value == base:
            self.rating_edits.pop(key, None)
        else:
            self.rating_edits[key] = value
        return self.remember(before)

    def allows_growth(self,key,plan=None):
        plan = self.transfer_plan if plan is None else plan
        return bool(plan.additions and key in plan.resources or self.extension
                      or key in self.stream_parts or key in self.fighter_sound_parts or self.part(*key).kind in ('anm','bom')
                    or self.part(*key).kind == 'pom' and key[0] not in self.owners)

    def validate_transfer_capacity(self, plan):
        for resource in sorted({key[0] for key in plan.resources.keys() | plan.cameras.keys()}):
            if self.parts[resource, -1].kind == 'fum':
                self.packed_group(resource, plan)
            else:
                part = self.part(resource, -1)
                data = self.asset_edits.get(part.key, plan.resources[part.key])
                require(len(self._encoded(part,data)) <= part.span or self.allows_growth(part.key,plan),
                        f'ANM {resource:04X} 超过目标的原生扇区容量。')

    def check_transfer_change(self, plan):
        previous = self.transfer_plan.resources
        require(not any(key in self.asset_edits and previous.get(key) != plan.resources.get(key)
                        for key in previous.keys() | plan.resources.keys()),
                '相关资源已有单独编辑，请先导出或还原该资源。')
        self.validate_transfer_capacity(plan)

    def transfer_finisher(self, role, candidate, donor=None, donor_candidate=None):
        require(self.skill_record(role,candidate).kind == 1, '请选择当前角色的必杀候选。')
        return self.transfer_skill(role,candidate,donor,donor_candidate)

    def transfer_support_skill(self, role, candidate, donor=None, donor_candidate=None):
        require(self.skill_record(role,candidate).kind in (0,2), '请选择防御技或辅助技。')
        return self.transfer_skill(role,candidate,donor,donor_candidate)

    def transfer_skill(self, role, candidate, donor, donor_candidate):
        self.ensure_open()
        key = role, candidate
        skill = self.skill_record(role,candidate)
        choices = self.transfers.copy()
        if donor is None:
            choices.pop(key, None)
        else:
            require(type(donor) is int and type(donor_candidate) is int and key not in self.additions,
                    '请选择双方已有的原生技能候选。')
            require(key not in self.skill_edits or self.skill_edits[key][:3] == skill.words[:3],
                    '请先恢复目标技能的原生状态与候选项。')
            choices[key] = donor, donor_candidate
        plan = compile_transfers(self,choices,self.additions)
        self.check_transfer_change(plan)
        previous = self.snapshot()
        self.transfers, self.transfer_plan = choices, plan
        if key in self.skill_edits and self.skill_edits[key] == plan.skill_words.get(key, skill.words):
            self.skill_edits.pop(key)
        return self.remember(previous)

    def add_finisher(self,role,shape,donor,donor_candidate):
        return self.add_skill(role,shape,donor,donor_candidate,(1,))

    def add_support_skill(self,role,shape,donor,donor_candidate):
        return self.add_skill(role,shape,donor,donor_candidate,(0,2))

    def add_skill(self,role,shape,donor,donor_candidate,kinds):
        self.ensure_open()
        require(type(donor) is int and type(donor_candidate) is int
                and (donor,donor_candidate) in self.skill_records
                and self.skill_records[donor,donor_candidate].kind in kinds,'请选择同类的原生来源技能。')
        candidate = next_candidate(self.workbench.tables,role)
        choices = self.additions | {(role,candidate):(donor,donor_candidate,shape)}
        plan = compile_transfers(self,self.transfers,choices)
        self.check_transfer_change(plan)
        before = self.snapshot()
        self.additions,self.transfer_plan = choices,plan
        return self.remember(before)

    def remove_finisher(self,role,candidate):
        return self.remove_added_skill(role,candidate,(1,))

    def remove_support_skill(self,role,candidate):
        return self.remove_added_skill(role,candidate,(0,2))

    def remove_added_skill(self,role,candidate,kinds):
        self.ensure_open()
        key = role,candidate
        require(key in self.additions and self.skill_record(role,candidate).kind in kinds
                and candidate == max(c for r,c in self.additions if r == role),
                '可移除当前角色最后添加的技能。')
        choices = self.additions.copy()
        choices.pop(key)
        plan = compile_transfers(self,self.transfers,choices)
        self.check_transfer_change(plan)
        before = self.snapshot()
        self.additions,self.transfer_plan = choices,plan
        self.skill_edits.pop(key,None)
        self.rating_edits.pop(key,None)
        return self.remember(before)

    def throw_change(self, role, action, animation):
        selected(ROSTER, role)
        program = self.workbench.program(role)
        require(type(action) is int and action in THROW_STATES and action < len(program.actions()),
                '请选择原生抓取阶段。')
        _, original, _ = program.actions()[action]
        require(original >= 0 and self.workbench.opponent_clip_offset(role, action) is None,
                '此阶段由原生回调选择动作。')
        if animation is not None:
            require(type(animation) is int and animation in self.workbench.binding(role), '请选择本角色已有的 ANM。')
        return (role, action), animation if animation != original else None

    def edit_throw(self, role, action, animation):
        key, value = self.throw_change(role, action, animation)
        before = self.snapshot()
        if value is None:
            self.throw_edits.pop(key, None)
        else:
            self.throw_edits[key] = value
        return self.remember(before)

    def get_binding_rows(self, role, slot):
        self.workbench.form(role, slot)
        return [{'name':label,'va':address,'source':word(self.elf,self.binding_locations[address]),
                 'value':self.binding_edits.get(address, word(self.elf,self.binding_locations[address]))}
                for label, address in self.binding_rows[role, slot]]

    def binding_change(self, role, slot, values, previous, validate=True):
        require(type(role) is int and type(slot) is int and (role,slot) in self.binding_rows,
                '请选择原生形态绑定。')
        rows = self.binding_rows[role,slot]
        source = [word(self.elf, self.binding_locations[address]) for _, address in rows]
        require(values is None or isinstance(values, (list, tuple)) and len(values) == len(rows),
                '资源绑定字段数与原生数量不符。')
        values = source if values is None else values
        build = previous.copy()
        changed = set()
        tables = self.workbench.tables
        package = tables.descriptor(role)[0][0]
        for index, ((label, address), value, original) in enumerate(zip(rows, values, source)):
            integer(value, 0, 0xFFFFFFFF, label)
            if build.get(address, original) != value:
                changed.add(address)
            if value == original:
                build.pop(address, None)
            else:
                kind = ('mdl', 'vau', 'bom')[index] if index < 3 else 'pom'
                require((value == 0xFFFFFFFF and kind in ('vau', 'bom')) or
                        self.part(package, value).kind == kind, f'{label} 须指向当前 FUM 中有效的 {kind.upper()}。')
                build[address] = value
        if validate:
            self.validate_bindings(build, changed)
        return build

    def validate_bindings(self, values, changed):
        if not changed:
            return
        tables = self.workbench.tables
        new_tables = self.tables_with_bindings(values)
        pairs = {(r,s) for (r,s), locations in self.binding_rows.items() if changed & {address for _,address in locations}}
        for r in {r for r,_ in pairs}:
            updated = new_tables.forms(self, r)
            baseline = tables.forms(self, r)
            require(updated.keys() == baseline.keys(), '形态绑定改变了模型槽的数量。')
            for s, form in updated.items():
                before = baseline[s]
                if form == before:
                    continue
                require(model_signature(self.data(form.model))[0] == model_signature(self.data(before.model))[0],
                        '模型绑定的节点结构与原槽不同。')
                make_model(self, form)

    def tables_with_bindings(self, values, base=None):
        elf = bytearray(self.elf if base is None else base)
        for address, raw in values.items():
            struct.pack_into('<I', elf, self.binding_locations[address], raw)
        return NativeTables(elf)

    def edit_binding(self, role, slot, values):
        before = self.snapshot()
        self.binding_edits = self.binding_change(role, slot, values, self.binding_edits)
        if self.transfers or self.additions:
            try:
                plan = compile_transfers(self,self.transfers,self.additions)
                self.check_transfer_change(plan)
            except (ValueError, IndexError, struct.error):
                self.binding_edits = before['bindings']
                raise
            self.transfer_plan = plan
        return self.remember(before)

    def edit_event(self, resource, member, script, index, frame, damage):
        part = self.part(resource, member)
        require(part.kind == 'bom', '请选择 BOM。')
        event = selected(self.events(resource, member, script), index)
        data = bytearray(self.data(part.key))
        struct.pack_into('<h', data, event['offset'] + 2, integer(frame, -32768, 32767, '事件帧'))
        field = BOM_DAMAGE_FIELDS.get(event['op'])
        if field is not None:
            struct.pack_into('<h', data, event['offset'] + 4 + 2 * field, integer(damage, -32768, 32767, '基础伤害'))
        return self._put(part, bytes(data))

    def edit_event_record(self, resource, member, script, index, change):
        part = self.part(resource, member)
        require(part.kind == 'bom', '请选择 BOM。')
        bom = Bom(self.data(part.key))
        event = selected(list(bom.events(script)), index)
        begin = event['offset']
        length = 4 + 2 * len(event['args'])
        data = bytearray(bom.data)
        if change is None:
            original = self.original(part.key)
            record = selected(list(Bom(original).events(script)), index)
            require(record['op'] == event['op'] and len(record['args']) == len(event['args']), '原始事件类型不同。')
            source = record['offset']
            data[begin:begin + length] = original[source:source + length]
        else:
            values = change.get('params') if isinstance(change, dict) else None
            require(isinstance(values, list) and len(values) == len(event['params']), '帧事件参数数量不同。')
            require(all(item['editable'] or item['value'] == value for item, value in zip(event['params'], values)),
                    '帧事件的原始保留数据为只读。')
            selector = integer(change.get('selector'), 0, 255, '模型槽掩码')
            frame = integer(change.get('frame'), -32768, 32767, '触发帧')
            if event['op'] == 5:
                command, duration, target = values[:3]
                return self.edit_combo(resource, member, script, index, {'frame':frame,'selector':selector,
                                       'command':command,'duration':duration,'target':target})
            struct.pack_into('<Bh', data, begin + 1, selector, frame)
            for item, value in zip(event['params'], values):
                if item['value'] == value:
                    continue
                if item['type'] == 'f':
                    require(type(value) in (int, float) and math.isfinite(value) and abs(value) <= 3.402823466e38,
                            '请输入可表示的浮点值。')
                    require(event['op'] != 14 or value > 0, '播放倍率须为正数。')
                else:
                    integer(value, -32768, 32767, item['name'])
                    if item['name'] == '形状模板':
                        integer(value, 0, bom.shapes - 1, item['name'])
                struct.pack_into('<' + item['type'], data, begin + item['offset'], value)
        return self._put(part, bytes(data))

    def edit_combo(self, resource, member, script, index, values):
        part = self.part(resource, member)
        require(part.kind == 'bom', '请选择 BOM。')
        event = selected(self.events(resource, member, script), index)
        require(event['op'] == 5, '请选择原生连段事件。')
        if values is None:
            original = selected(list(Bom(self.original(part.key)).events(script)), index)
            require(original['op'] == 5 and original['offset'] == event['offset'], '当前连段的原始记录已经改变。')
            source = self.original(part.key)[event['offset']:event['offset'] + 12]
        else:
            require(isinstance(values, dict), '连段窗口参数无效。')
            frame = integer(values.get('frame'), -32768, 32767, '开始帧')
            duration = integer(values.get('duration'), 0, 32767, '连段持续帧')
            command = values.get('command')
            target = values.get('target')
            role = self.owners.get(resource)
            require(type(command) is int and command in self.workbench.rules and type(target) is int and role is not None
                    and target in self.workbench.binding(role), '请选择原生输入及该角色实际存在的目标 ANM。')
            selector = integer(values.get('selector'), 0, 255, '模型槽掩码')
            source = struct.pack('<BB4h', 5, selector, frame, command, duration, target) + self.data(part.key)[
                event['offset'] + 10:event['offset'] + 12]
        data = bytearray(self.data(part.key))
        data[event['offset']:event['offset'] + 12] = source
        return self._put(part, bytes(data))

    def batch(self, role, shape, mode, range_text, factor):
        require(mode in ('inspect', 'damage', 'speed', 'travel'), '请选择要修改的参数。')
        require(type(factor) in (int, float) and math.isfinite(factor) and 0 <= factor <= 32767
                and (mode != 'speed' or factor > 0), '倍率无效。')
        form = self.workbench.form(role, shape)
        slots = self.workbench.binding(role)
        primary = self.workbench.tables.banks(role)[0][0]
        require(isinstance(range_text, str) and len(range_text) <= 4096, '动作范围无效。')
        numbers = set()
        if range_text.strip():
            for word_range in re.split('[,，]', range_text.strip()):
                span = re.fullmatch(r'\s*(\d{1,5})(?:\s*-\s*(\d{1,5}))?\s*', word_range)
                require(span is not None, '动作范围格式为 0-20,30,40。')
                first, last = int(span[1]), int(span[2] or span[1])
                require(first <= last <= max(slots), '动作范围超出角色的 ANM 编号。')
                numbers.update(range(first, last + 1))
            require(numbers <= slots.keys(), '动作范围包含不存在的 ANM。')
        else:
            numbers = {number for number, (key, _) in slots.items() if key[0] == primary}
        changed, edits = [], {}
        if mode in ('damage', 'inspect'):
            require(form.bom is not None, '该形态未绑定 BOM。')
            bom = Bom(self.data(form.bom))
            data = bytearray(bom.data)
            for number in sorted(numbers):
                if number >= bom.scripts:
                    continue
                for event in self.workbench.events(form, number):
                    for field, name in damage_fields(event['op']):
                        source = event['args'][field]
                        target = integer(round(source * factor), -32768, 32767, name)
                        if source != target or mode == 'inspect':
                            changed.append([number, event['frame'], source, target, name])
                        if mode == 'damage' and source != target:
                            struct.pack_into('<h', data, event['offset'] + 4 + 2 * field, target)
            if mode == 'damage' and changed:
                edits[form.bom] = bytes(data)
        else:
            for number in sorted(numbers):
                key, index = slots[number]
                source = edits.get(key)
                if source is None:
                    source = self.data(key)
                entry = clip_index(source)[index]
                if mode == 'speed':
                    rate = (entry.rate if entry.rate > 0 else 1) * factor
                    if rate == entry.rate:
                        continue
                    require(0 < rate <= 3.402823466e38, '动作速率超过 float32 范围。')
                    edited = bytearray(source)
                    struct.pack_into('<f', edited, entry.metadata + 4, rate)
                    edited = bytes(edited)
                else:
                    if 0 not in entry.tracks:
                        continue
                    root = entry.tracks.index(0)
                    rows = key_rows(source, index, root, 1)
                    if not rows or factor == 1:
                        continue
                    origin_x, _, origin_z = rows[0]['value']
                    for row in rows[1:]:
                        x, y, z = row['value']
                        row['value'] = [origin_x + (x-origin_x)*factor, y, origin_z + (z-origin_z)*factor]
                    edited = replace_key_rows(source, index, root, 1, rows)
                    if edited == source:
                        continue
                edits[key] = self.validate_import(self.part(*key), edited)
                changed.append([number])
        before = self.snapshot()
        for key, data in edits.items():
            if data == self.original(key):
                self.asset_edits.pop(key, None)
            else:
                self.asset_edits[key] = data
        return {'rows':changed,'edits':self.remember(before)}

    def edit_clip(self, resource, member, index, frames, first, last, rate):
        part = self.part(resource, member)
        require(part.kind == 'anm', '请选择 ANM。')
        data = bytearray(self.data(part.key))
        clip = selected(clips(data), index)
        integer(first, -32768, 32767, '首帧')
        integer(last, -32768, 32767, '末帧覆盖')
        require(last <= 0 or 0 <= first < last, '末帧覆盖为正数时须大于首帧。')
        require(type(rate) in (float, int) and math.isfinite(rate) and (0 < rate <= 3.402823466e38 or rate == clip.rate),
                '请输入可表示的正数播放速率。')
        struct.pack_into('<H', data, clip.start + 0x24, integer(frames, 0, 65535, '片段帧数'))
        struct.pack_into('<hhf', data, clip.metadata, first, last, rate)
        return self._put(part, bytes(data))

    def keyframes(self, resource, member, index, track, channel):
        part = self.part(resource, member)
        require(part.kind == 'anm', '请选择 ANM。')
        return key_rows(self.data(part.key), index, track, channel)

    def edit_key(self, resource, member, index, track, channel, row, frame, values):
        part = self.part(resource, member)
        require(part.kind == 'anm', '请选择 ANM。')
        return self._put(part, self.validate_import(part, replace_key(
            self.data(part.key), index, track, channel, row, frame, values)))

    def edit_key_rows(self, resource, member, index, track, channel, rows):
        part = self.part(resource, member)
        require(part.kind == 'anm', '请选择 ANM。')
        changed = replace_key_rows(self.data(part.key), index, track, channel, rows)
        return self._put(part, self.validate_import(part, changed))

    def clip_export(self, resource, member, index):
        part = self.part(resource, member)
        require(part.kind == 'anm', '请选择 ANM。')
        return single_clip(self.data(part.key), index)

    def clip_import(self, resource, member, index, raw):
        part = self.part(resource, member)
        require(part.kind == 'anm', '请选择 ANM。')
        changed = replace_clip(self.data(part.key), index, raw)
        return self._put(part, self.validate_import(part, changed))

    def _encoded(self, part, data=None):
        if data is None:
            data = self.data(part.key)
        if not part.compressed:
            return data
        if len(data) <= 512 * 1024:
            return self._encode_small(bytes(data))
        return encode_verified(data)

    def packed_group(self, resource, plan=None):
        if plan is None:
            plan = self.transfer_plan
        container = self.part(resource,-1)
        original = self.original(container.key)
        members = self.children[resource]
        edits = plan.resources | self.asset_edits
        blobs = {part.member:self._encoded(part,edits[part.key]) for part in members if part.key in edits}
        extra = [(member,name,data) for (group,member),(name,data) in sorted(plan.cameras.items()) if group == resource]
        if not blobs and not extra:
            return original,container.size
        moved = extra or any(len(blobs[part.member]) > part.span for part in members if part.member in blobs)
        if not moved:
            out = bytearray(original)
            used = container.size
            for index,blob in blobs.items():
                part = members[index]
                start = part.at - container.at
                out[start:start+part.span] = blob.ljust(part.span,b'\0')
                struct.pack_into('<I',out,8+128*index+124,len(blob))
                used = max(used,start+len(blob))
            return bytes(out),used
        count = len(members)+len(extra)
        require([index for index,_,_ in extra] == list(range(len(members),count)), '扩展镜头编号不连续。')
        out = bytearray(b'FUM\0'+struct.pack('<I',count))
        out.extend(bytes(align(8+128*count,64)-8))
        for index in range(count):
            if index < len(members):
                part = members[index]
                at = part.at-container.at
                raw = blobs.get(index)
                name = original[8+128*index:8+128*index+116]
                logical = len(raw) if raw is not None else part.size
                blob = raw if raw is not None else original[at:at+part.span]
            else:
                _,name,blob = extra[index-len(members)]
                name = name.encode('ascii')
                logical = len(blob)
            span = align(len(blob),64)
            struct.pack_into('<116sIII',out,8+128*index,name,len(out),span,logical)
            out.extend(blob.ljust(span,b'\0'))
            used = len(out)-span+logical
        require(len(out) <= len(original) or extra or
                any(self.allows_growth((resource,index),plan) for index in blobs),
                f'FUM {resource:04X} 的子项重排超过原 2 KiB 读取范围。')
        out.extend(bytes(align(max(len(out),len(original)),SECTOR)-len(out)))
        return bytes(out),max(used,min(container.size,len(out)))

    def asset_export(self, resource, member, format_name, palette):
        part = self.part(resource, member)
        if format_name == 'png':
            require(part.kind == 'pom', '请选择 POM。')
            return '.png', png_for_pom(self.data(part.key), palette)
        require(format_name == 'native', '请选择原生资源或 PNG。')
        if part.kind == 'fum':
            return '.fum', self.packed_group(resource)[0]
        return '.' + part.kind, self.data(part.key)

    def export(self, destination):
        self.ensure_open()
        plan = self.transfer_plan
        expected = {}
        resources = plan.resources | self.asset_edits
        elf = bytearray(plan.elf if plan.elf is not None else self.elf)
        container = 'FILE3.BIN'
        index = self.files[container].size
        grown = []
        space = None
        for resource in sorted({rid for rid,_ in resources.keys() | plan.cameras.keys()}):
            main = self.part(resource,-1)
            if main.kind == 'fum':
                data,logical = self.packed_group(resource)
            else:
                encoded = self._encoded(main)
                require(len(encoded) <= main.span or self.allows_growth(main.key),
                        f'资源 {resource:04X} 超过原扇区分配空间。')
                logical = len(encoded)
                data = encoded.ljust(align(max(logical,main.span),SECTOR),b'\0')
            at = self.table+16*resource
            if len(data) > main.span:
                if space is None:
                    space = ElfSpace(elf,prefix_records(elf))
                    elf = space.elf
                for offset,value in ((0,index),(4,len(data)),(12,2)):
                    space.patch_file(at+offset,struct.pack('<I',value))
                grown.append(data)
                index += len(data)
            else:
                expected[resource] = data
            struct.pack_into('<I',elf,at+8,logical)
        for key,data in resources.items():
            if key in self.encoded_lengths:
                struct.pack_into('<I',elf,self.encoded_lengths[key],len(data))
        for offset,raw in self.native_edits.items():
            struct.pack_into('<h',elf,offset,raw)
        for (role,candidate),words in (plan.skill_words | self.skill_edits).items():
            kind = self.skill_record(role,candidate).kind
            record = next(item for item in self.workbench.tables.battle_skill_rows(role,kind) if item.id == candidate)
            data = struct.pack(f'<{len(words)}I', *words)
            offset = native_offset(elf, record.va, len(data))
            if record.menu_candidate:
                elf[offset:offset + len(data)] = data
            else:
                # Stock revision checks can recover this record from the existing ELF change ledger.
                if space is None:
                    space = ElfSpace(elf, prefix_records(elf))
                    elf = space.elf
                space.patch_file(offset, data)
        for key, value in self.rating_edits.items():
            struct.pack_into('<i', elf, native_offset(elf, rating_address(*key), 4), value)
        for slot,value in plan.audio.items():
            address = audio_table(NativeTables(elf))
            struct.pack_into('<i',elf,native_offset(elf,address+4*slot,4),value)
        for address,raw in self.binding_edits.items():
            struct.pack_into('<I',elf,self.binding_locations[address],raw)
        for role,values in self.jump_edits.items():
            for offset,value in zip(self.jump_locations,values):
                struct.pack_into('<f',elf,offset+4*role,value)
        for i, rate in self.charge_edits.items():
            struct.pack_into('<f', elf, self.charge_locations[i], rate)
        if self.sound_extensions and read_sound_codes(elf)[0] != self.sound_extensions:
            if space is None:
                space = ElfSpace(elf,prefix_records(elf))
            install_sound(space,self.sound_extensions)
        if space is not None:
            elf = space.finish()
        modules = {}
        for role in plan.scripts.keys() | plan.modules.keys():
            path = self.workbench.program_path(role)
            data = bytearray(self.workbench.program(role).data)
            if role not in plan.modules:
                for offset,raw in plan.scripts[role].items():
                    data[offset:offset+len(raw)] = raw
            modules[path] = data
        for (role,state),anm in self.throw_edits.items():
            program = self.workbench.program(role)
            path = self.workbench.program_path(role)
            data = modules.setdefault(path,bytearray(program.data))
            offset = program.file_offset(program.action_table+24*state+16,4)
            struct.pack_into('<i',data,offset,anm)
        writes = {BOOT:elf,**modules}
        with new_output(destination,'.iso') as temp:
            if grown or any(len(data) != self.files[path].size for path,data in modules.items()):
                write_expanded_iso(self.path,temp,self.files,container,grown,writes)
            else:
                shutil.copyfile(self.path,temp)
                with temp.open('r+b') as out:
                    for path,data in writes.items():
                        out.seek(self.files[path].offset)
                        out.write(data)
            with temp.open('r+b') as out:
                for resource,data in expected.items():
                    out.seek(self.part(resource,-1).at)
                    out.write(data)
            reopened = FE3Game(temp)
            require(reopened.elf == elf,'新 ISO 的 ELF 读回不匹配。')
            for key,data in resources.items():
                require(reopened.original(key) == data,'新 ISO 的资源读回不匹配。')
            for key,(_,data) in plan.cameras.items():
                require(reopened.original(key) == data,'新 ISO 的新增镜头读回不匹配。')
            for path,data in modules.items():
                require(reopened.read_iso_entry(path) == data,'新 ISO 的角色 ERX 读回不匹配。')
            self.ensure_open()
        self.saved = self.snapshot()
        return {'name':Path(destination).name,'edits':self.edit_status()}


def open_game(path):
    from ogeditor.psp.game import Game
    path = Path(path).resolve(strict=True)
    files = list_iso_files(path)
    if any(item.path.upper() == BOOT for item in files):
        return FE3Game(path, files)
    return Game(path)
