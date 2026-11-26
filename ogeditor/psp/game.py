"""Resolve KGF v7 resources and apply pending edits over the source ISO."""

import base64
from collections import Counter, defaultdict, deque
from copy import deepcopy
import csv
from dataclasses import replace
from functools import cached_property, lru_cache
from hashlib import sha256
import json
from pathlib import Path
import struct

from ogeditor.psp.appearance import MODEL_ROLES, color_timer, item_models, palette_modes
from ogeditor.psp.animation_edit import (STORY_BANKS, change_timing, protect_story_clip, read_anm_bank, replace_anm_clips,
                            read_keys, replace_story_bank, write_keys)
from ogeditor.audio import find_sample, vag_wave
from event_edits import Patch, change_words, event_words
from formats import (SHAPE_TEMPLATE_OFFSETS, anm_header, base_damage, bom_parts, decode_anm, decode_event,
                     event_records, read_shape, s16, u32)
from ogeditor.input_notation import SEQUENCE, input_arrows
from ogeditor.psp.move_textures import installed_locations, legacy_usage
from ogeditor.psp.native_tables import (COMBO_UPDATES, COMMAND_NAMES, INITIAL_HP, LIFE_BANDS, Q12, THROWS, energy_layout, known_page_base,
                           ZOFFY_RAGE_ENTRIES, action_name, check_eboot, command_catalog,
                           command_paths, music_catalog, native_layout, state_branches)


from ogeditor.readers import HERE, ROOT, STAGE, editor_data, get_pom_layout, iso_directory, read_mdl, read_yum, render_pom_png
from build_view import load_image, relocate
from pig_attack_data import replace_bank

from ogeditor.psp.asset_transfer import (ASSET_MAGIC, clip_ids, compatible_model, pom_layout, replace_texture,
                            texture_png, validate_clip, write_asset)
from iso_save import save_iso
from ogeditor.models.model_retarget import Rig, is_shadow, retarget_model, topology
from ogeditor.models.fbx_import import read_fbx
from fbx_textures import stage_fbx_textures
from ogeditor.models.fighter_parts import missing_hands, timer_faces
from ogeditor.models.mesh_import import IMPORT_ROTATIONS, mesh_to_mdl
from ogeditor.models.mesh_import import read_obj
from ogeditor.shared_resources import SharedResources
from ogeditor.psp.ai_eboot import install_ai, strip_ai
from move_sound import MARKER as SOUND_MARKER, install_sound, read_codes
from move_sound import PSP_TABLE as SOUND_TABLE, PSP_CUES as SOUND_COUNT, VOICE, load_sound_members
from ogeditor.psp.ai_script import LEVELS as AI_LEVELS, fighter_scripts, load_ai_edits
from move_eboot import compiled_layout, installed_eboot
from ogeditor.psp.move_sets import (LEO_FORWARD, LEO_STAGES, Compilation, can_mix_move, donor_status,
                       forward_skill_branches, move_event_key, move_event_words, pack_script)
from ogeditor.psp.role_effects import BLACK_SCENE_SOURCE, JACK_THROW
from ogeditor.psp.move_special import FIRST_PRIVATE_STATE
from ogeditor.psp.move_taro import TARO_DOWN, TARO_PHASES
from ogeditor.psp.move_throw import LEO_THROW_STAGES, LEO_WEAPON_POM, LEO_WEAPON_TRACKS
from ogeditor.psp.move_resume import ResumedMoves
from ogeditor.psp import ground_travel
from ogeditor.psp.skill_phases import editable_skill_states, skill_phases


BASE = 0x08804000
WORKSPACE_FIELDS = ('edits', 'asset_edits', 'native_edits', 'template_edits', 'notes', 'aliases', 'move_sets', 'model_atlases',
                    'move_event_edits', 'move_texture_edits', 'move_anm_edits', 'resumed_shape_refs', 'movement_rates', 'ai_edits', 'sound_extensions', 'sound_members')


def selected(items, index):
    if type(index) is not int or not 0 <= index < len(items):
        raise ValueError('选择项超出范围。')
    return items[index]


class Game:
    @property
    def sound_count(self):
        return SOUND_COUNT + len(self.sound_extensions)

    def __init__(self, path):
        self.resumed = False
        self._recovery = None
        baseline = json.loads((STAGE / '基准与ISO目录.json').read_text(encoding='utf-8'))
        self.path = Path(path).resolve(strict=True)
        self.stamp = self.file_stamp()
        with self.path.open('rb') as handle:
            try:
                entries = {item['path'].upper(): item for item in iso_directory(handle)}
            except (AssertionError, UnicodeDecodeError, struct.error):
                raise ValueError('无法读取这个 ISO 的目录。') from None
            self.iso_files = entries
            eboot = entries.get('/PSP_GAME/SYSDIR/EBOOT.BIN')
            self.entry = entries.get('/PSP_GAME/USRDIR/YUMFILE.BIN')
            if not eboot or not self.entry or eboot['size'] != baseline['eboot']['bytes']:
                raise ValueError('请选择 受支持的奥0 ISO。')
            handle.seek(eboot['offset'])
            raw = handle.read(eboot['size'])
            try:
                self.yum = read_yum(handle, self.entry)
            except (AssertionError, struct.error):
                raise ValueError('无法读取此镜像的奥0 YUM 目录。') from None
        if (len(self.yum['groups']), len(self.yum['files'])) != (739, 7070):
            raise ValueError('此镜像的 YUM 索引与奥0绑定表不匹配。')
        self.ensure_open()
        segments = baseline['elf']['segments']
        self.elf_layout = baseline['elf']
        self.eboot = eboot
        self.eboot_raw = raw
        self.roles = json.loads((STAGE / '角色资源绑定.json').read_text(encoding='utf-8'))['roles']
        self.native_anm_members = {b['id'] for r in self.roles for case in r['cases']
                                   for b in (*case['anm_banks'], *case['encounter_anm'])}
        self.bank_tracks = lru_cache(maxsize=64)(lambda member: frozenset().union(
            *(clip_ids(clip) for clip in self.bank(member,b'ANM\0')[2] if clip)))
        self.actions = [{} for _ in self.roles]
        self.native_actions = [{} for _ in self.roles]
        with (STAGE / '角色动作映射.csv').open(encoding='utf-8-sig', newline='') as handle:
            for row in csv.DictReader(handle):
                role, action = int(row['role']), int(row['action'])
                move = {
                    'id': action, 'clip': int(row['clip']) if row['clip'] else None,
                    'category': int(row['category']), 'flags': row['state_flags'],
                    'va': row['record_va'], 'parameter': row['packed_low'],
                    'callbacks': [row[key] for key in ('enter_callback', 'update_callback', 'exit_callback')],
                }
                self.native_actions[role][action] = move
                if move['clip'] is not None:
                    self.actions[role][action] = move
        self.attribute_fields, self.clip_fields = native_layout(segments, BASE, self.actions)
        self.native_fields = {f.offset: f for f in (*self.attribute_fields.values(), *self.clip_fields.values())}
        self.energy_fields = energy_layout(segments, BASE, len(self.roles))
        self.native_fields.update((f.offset, f) for row in self.energy_fields for f in row)
        canonical_path = STAGE / '证据/EBOOT.BIN'
        stock = canonical_path.read_bytes()
        self.canonical_eboot = known_page_base(raw,stock,editor_data(HERE, ROOT, 'select_page_bases.dat'))
        expected = (baseline['eboot']['sha256'] if self.canonical_eboot is stock
                    else sha256(self.canonical_eboot).hexdigest())
        below_travel, rates = strip_ai(raw, self.elf_layout, self.canonical_eboot, ground_travel.MARKER)
        self.base_movement = ground_travel.load_rates(rates or [], len(self.roles))
        self.movement_rates = dict(self.base_movement)
        below_sound, sound_meta = strip_ai(below_travel, self.elf_layout, self.canonical_eboot, SOUND_MARKER, details=True)
        sound_meta = sound_meta or {}
        self.sound_extensions = read_codes(sound_meta.get('edits',[]))
        self.pre_ai_eboot, ai_rows = strip_ai(below_sound, self.elf_layout, self.canonical_eboot)
        restored, installed, mix_meta = installed_eboot(
            self.pre_ai_eboot, self.elf_layout, self.canonical_eboot, details=True)
        check_eboot(restored, expected, self.canonical_eboot, self.native_fields.values())
        self._mix_metadata = mix_meta
        self._base_eboot = restored
        self._stock_actions = deepcopy((self.actions, self.native_actions)) if installed else None
        self.installed_moves = {}
        self.installed_effect_sources = dict(mix_meta.get('effects', ()))
        self.installed_move_textures, self.installed_palettes = installed_locations(
            mix_meta, self.roles, self.yum['files'])
        self.legacy_texture_usage = lru_cache(maxsize=96)(lambda role, config, shape:
                                                          legacy_usage(self, role, config, shape))
        stock_callbacks = {(role, state): move['callbacks'] for role, actions in enumerate(self.native_actions)
                           for state, move in actions.items()} if installed else {}
        for row in installed:
            if not isinstance(row, list) or len(row) not in (6, 7) or any(type(value) is not int for value in row):
                raise ValueError('ISO 里的配招记录无效。')
            role, state, clip, source, original, source_clip = row[:6]
            owned = selected(self.native_actions, role)
            move = owned.get(state)
            donor = selected(self.actions, source).get(original)
            if len(row) == 7 and move is None and donor is not None and FIRST_PRIVATE_STATE <= state < 256:
                phase = row[6]
                last = segments[-1]
                header = u32(raw, 28) + 32 * (len(segments) - 1)
                start = BASE + last['vaddr']
                if start + last['memsz'] <= phase <= start + u32(raw, header + 20) - 16:
                    move = {**donor, 'id': state, 'va': f'0x{phase:08X}'}
                    owned[state] = move
            if move is None or donor is None or not 0 <= clip < 4096:
                raise ValueError('ISO 里的配招引用无效。')
            self.installed_moves[role, state] = (source, original, source_clip)
            move.update(clip=clip, callbacks=stock_callbacks[source, original])
            self.actions[role][state] = move
        self.base_layout = compiled_layout(self.canonical_eboot, self.elf_layout)
        self.image = load_image(restored, self.base_layout['segments'])
        relocate(restored, self.image, self.base_layout['segments'], self.base_layout['sections'])
        self.action_image = self.image
        if installed:
            compiled = compiled_layout(raw, self.elf_layout)
            self.action_image = load_image(raw, compiled['segments'])
            relocate(raw, self.action_image, compiled['segments'], compiled['sections'])
            for role, state in self.installed_moves:
                move = self.actions[role][state]
                pos = int(move['va'], 16) - BASE
                move['parameter'] = f'0x{(s16(self.action_image, pos + 12) & 0xFFFF):04X}'
        self.entry_commands = [
            {row['target']: row['input'] for row in state_branches(self.action_image, BASE, role, 0)
             if row['command'] < 40}
            for role in range(len(self.roles))
        ]
        self.input_paths = [command_paths(self.action_image, BASE, role, moves)
                            for role, moves in enumerate(self.actions)]
        self._mounted_view = (self.actions, self.native_actions, self.action_image, self.installed_moves,
                              self.pre_ai_eboot, self.entry_commands, self.input_paths) if installed else None
        self.bank_owners = {case['anm_banks'][0]['id']:role for role, fighter in enumerate(self.roles)
                            for case in fighter['cases']}
        media_types = {'RIFF/WAVE': 'bgm', 'POM': 'pom', 'PNG': 'png'}
        self.media_members = []
        with (STAGE / 'YUM资源索引.csv').open(encoding='utf-8-sig', newline='') as handle:
            for row in csv.DictReader(handle):
                if row['kind'] in media_types:
                    self.media_members.append((int(row['id']), int(row['group_id']), int(row['slot']),
                                               media_types[row['kind']]))
        self.bank = lru_cache(maxsize=10)(self.read_bank)
        self.texture = lru_cache(maxsize=96)(self.read_texture)
        self.node_count = lru_cache(maxsize=None)(self.read_node_count)
        self.sound_bank = lru_cache(maxsize=10)(self.read_sound_bank)
        self.bom_models = defaultdict(set)
        self.model_forms = defaultdict(set)
        self.pom_forms = defaultdict(set)
        for role, metadata in enumerate(self.roles):
            for config, case in enumerate(metadata['cases']):
                for shape in range(len(case['forms'])):
                    _, form, bom, textures = self.form_binding(role, config, shape)
                    selection = role, config, shape
                    member = form['mdl']['id']
                    self.bom_models[bom['id']].add((shape, member))
                    self.model_forms[member].add(selection)
                    for texture in textures:
                        self.pom_forms[texture['id']].add(selection)
        self.edits = {}
        self.asset_edits = {}
        self.model_atlases = {}
        self.native_edits = {}
        self.template_edits = {}
        self.notes = {}
        self.aliases = {}
        self.move_sets = {}
        self.move_anm_edits = {}
        self.move_event_edits = {}
        self.resumed_shape_refs = {}
        self.move_texture_edits = {}
        self.ai_edits = {} if ai_rows is None else load_ai_edits(self.ai_scripts, ai_rows)
        self.move_preview = None
        self.undo = []
        self.redo = []
        self.sound_members = load_sound_members(self,sound_meta.get('sound_members',()))
        self.saved = self.snapshot()
        self.refresh_actions()

    def file_stamp(self):
        stat = self.path.stat()
        return stat.st_size, stat.st_mtime_ns, stat.st_ino

    @cached_property
    def ai_scripts(self):
        return fighter_scripts(self.image)

    @cached_property
    def shared_resources(self):
        return SharedResources(self)

    def read_ai(self, role, difficulty, group=-1):
        script = selected(self.ai_scripts, role)
        selected(range(AI_LEVELS), difficulty)
        values = {site: value for (owner, level, site), value in self.ai_edits.items()
                  if (owner, level) == (role, difficulty)}
        return script.view(values, group)

    def edit_ai(self, role, difficulty, updates):
        script = selected(self.ai_scripts, role)
        selected(range(AI_LEVELS), difficulty)
        if not isinstance(updates, dict) or not updates:
            raise ValueError('请选择要修改的 AI 参数。')
        edited = self.ai_edits.copy()
        for key, value in updates.items():
            try:
                site = int(key)
            except (TypeError, ValueError) as exc:
                raise ValueError('AI 参数编号无效。') from exc
            definition = script.field(site)
            address = role, difficulty, site
            if value is None or definition.encode(value) == definition.value & 0xFFFFFFFF:
                edited.pop(address, None)
            else:
                edited[address] = value
        before = self.snapshot()
        self.ai_edits = edited
        self.remember(before)
        return self.edit_status()

    def ensure_open(self):
        if self.file_stamp() != self.stamp:
            raise ValueError('ISO 内容已经变化，请重新打开。')

    def member_offset(self, member):
        offset, _, _ = self.yum['files'][member]
        return self.entry['offset'] + offset

    def read_member(self, member, magic, prefix_length=None):
        replacement = self.asset_edits.get((member, None))
        if replacement is not None:
            if not replacement.startswith(magic):
                raise ValueError(f'YUM {member} 的替换资源类型无效。')
            return replacement[:prefix_length] if prefix_length is not None else replacement
        return self.read_source_member(member, magic, prefix_length)

    def read_source_member(self, member, magic, prefix_length=None):
        if self.resumed and member in self._recovery.members:
            data = self._recovery.members[member]
            if not data.startswith(magic):
                raise ValueError(f'YUM {member} 的原生资源类型不符。')
            return data if prefix_length is None else data[:prefix_length]
        return self.read_image_member(member, magic, prefix_length)

    def read_image_member(self, member, magic, prefix_length=None):
        offset, size, _ = self.yum['files'][member]
        if prefix_length is not None:
            size = min(size, prefix_length)
        with self.path.open('rb') as handle:
            handle.seek(self.entry['offset'] + offset)
            data = handle.read(size)
        if len(data) != size or not data.startswith(magic):
            raise ValueError(f'YUM {member} 的资源类型或长度无效。')
        return data

    def read_node_count(self, member):
        header = self.read_member(member, b'MDL\0', 16)
        return struct.unpack_from('<H', header, 14)[0]

    def read_bank(self, member, magic):
        data = self.read_member(member, magic)
        if magic == b'ANM\0':
            return read_anm_bank(data)
        return bom_parts(data)

    def read_texture(self, member, palette, source=None, source_palette=None, *, opaque=True):
        data = self.read_member(member, b'POM\0')
        if source == member:
            palette = source_palette
        elif source is not None:
            other = self.read_member(source, b'POM\0')
            dest, origin = get_pom_layout(data), get_pom_layout(other)
            length = dest['palette_block_size']
            if not (0 <= palette < dest['palette_count'] and 0 <= source_palette < origin['palette_count']
                    and length <= origin['palette_block_size']):
                raise ValueError('当前贴图的色表与原生外观不兼容。')
            to = dest['palette_offset'] + dest['palette_stride'] * palette
            start = origin['palette_offset'] + origin['palette_stride'] * source_palette
            data = data[:to] + other[start:start + length] + data[to + length:]
        w, h, png = render_pom_png(data, auto_color_key=False, opaque=opaque, palette_index=palette)
        return {'id': member, 'palette': palette, 'width': w, 'height': h,
                'uri': 'data:image/png;base64,' + base64.b64encode(png).decode('ascii')}

    def catalog(self):
        return {
            'name': self.path.name, 'path': str(self.path), 'groups': len(self.yum['groups']),
            'edits': self.edit_status(),
            'roles': [{'name': role['label'],
                       'cases': [{'parameter': case['parameter'], 'opponent': case.get('opponent'),
                                  'group': case['group'], 'forms': [form['mdl']['id'] for form in case['forms']]}
                                 for case in role['cases']]} for role in self.roles],
        }

    def snapshot(self):
        return {'resumed':self.resumed, **{name: getattr(self, name).copy() for name in WORKSPACE_FIELDS}}

    def clear_bank_cache(self):
        self.bank.cache_clear()
        self.bank_tracks.cache_clear()

    def apply_snapshot(self, state):
        self._switch_move_base(state.get('resumed', False))
        for name in WORKSPACE_FIELDS:
            setattr(self, name, state[name].copy())
        self.clear_bank_cache()
        self.legacy_texture_usage.cache_clear()
        self.sound_bank.cache_clear()
        self.texture.cache_clear()
        self.node_count.cache_clear()
        self.refresh_actions()

    def remember(self, before):
        if before != self.snapshot():
            self.move_preview = None
            self.legacy_texture_usage.cache_clear()
            self.undo.append(before)
            self.redo.clear()

    def current_moves(self):
        if self.move_preview is None:
            self.move_preview = Compilation(self, self.move_sets)
        return self.move_preview

    def _switch_move_base(self, resumed):
        if self.resumed == resumed:
            return
        if resumed:
            self.actions, self.native_actions = self._stock_actions
            self.action_image = self.image
            self.installed_moves = {}
            self.pre_ai_eboot = self._base_eboot
            self.entry_commands = [{row['target']:row['input'] for row in state_branches(self.image, BASE, role, 0)
                                    if row['command'] < 40} for role in range(len(self.roles))]
            self.input_paths = [command_paths(self.image, BASE, role, moves) for role, moves in enumerate(self.actions)]
        else:
            (self.actions, self.native_actions, self.action_image, self.installed_moves,
             self.pre_ai_eboot, self.entry_commands, self.input_paths) = self._mounted_view
        self.resumed = resumed
        self.clear_bank_cache()
        self.node_count.cache_clear()
        self.move_preview = None

    def resume_move_workspace(self):
        if self.resumed:
            return
        if not self.installed_moves:
            raise ValueError('此工程需要含有原配招记录的 ISO。')
        if self._recovery is None:
            probe = Game(self.path)
            recovery = ResumedMoves(probe)
            probe._recovery = recovery
            probe._switch_move_base(True)
            probe.refresh_actions()
            recovery.recover(probe)
            self.ensure_open()
            self._recovery = recovery
        self._switch_move_base(True)
        self.refresh_actions()
        self.move_sets = self._recovery.roots.copy()
        self.move_event_edits = deepcopy(self._recovery.events)
        self._recovery.migrate_edits(self)

    def action_title(self, role, move):
        state = move['id']
        if role == TARO_DOWN[0] and state in TARO_PHASES:
            if state == 183:
                return '↓ + ○' + SEQUENCE + '(□ / △ / ○) · 火焰攻击'
            return '↓ + ○ · ' + input_arrows(TARO_PHASES[state])
        return action_name(move)

    def move_choices(self, role, donor):
        installed_roots = self._mix_metadata.get('roots') if self.installed_moves else None
        root_ids = {tuple(key) for key in installed_roots} if installed_roots is not None else None
        def options(actor, source=False):
            selected(self.roles, actor)
            rows = []
            uses_lead = not source and bool(forward_skill_branches(self, actor))
            moves = self._stock_actions[0][actor] if self.installed_moves else self.actions[actor]
            for move in moves.values():
                if not can_mix_move(move):
                    continue
                state = move['id']
                inputs = self.input_paths[actor].get(state, ())
                name = self.aliases.get((actor, state)) or SEQUENCE.join(inputs) or self.action_title(actor, move)
                if uses_lead and state == 158:
                    name = '→ + ○'
                if source and not self.aliases.get((actor, state)):
                    special = {(5, 158): '前 + ○ · 飞踢', (5, 160): '下 + ○ · 双截棍投技',
                               (11, 158): '前 + ○ · 蓄电', (11, 160): '下 + ○ · 放电',
                               TARO_DOWN: '下 + ○ · 蓄力火焰', (11, 163): '后 + ○ · 电击投技',
                               (8, 162): '后 + ○ · 光线演出',
                               (2, 158): '前 + ○ · 投射', JACK_THROW: '下 + ○ · 抓取',
                               (2, 162): '后 + ○ · 长矛演出', (12, 163): '后 + ○ · 双方演出',
                               (15, 158): '前 + ○ · 光线', (15, 160): '下 + ○ · 连续射击',
                               (15, 162): '后 + ○ · 光线演出'}
                    if (actor, state) in special:
                        name = input_arrows(special[actor, state])
                if not source and (mix := self.move_source(actor, state)):
                    target_input = self.entry_commands[actor].get(state, name)
                    donor_name = self.roles[mix['role']]['label']
                    name = f'{target_input} · 已配招（{donor_name}）'
                rows.append({'id': state, 'name': name or f'状态 {state:03d}', 'category': move['category'],
                             'reason': donor_status(self, actor, state, moves) if source else ''})
            return rows

        rows = [{'state': state, 'role': source, 'move': original, 'installed': False}
                for (target, state), (source, original) in sorted(self.move_sets.items()) if target == role]
        rows.extend({'state': state, 'role': source, 'move': original, 'installed': True}
                    for (target, state), (source, original, _) in sorted(self.installed_moves.items())
                    if target == role and (root_ids is None or (target,state) in root_ids)
                    and state in self._stock_actions[0][role] and can_mix_move(self._stock_actions[0][role][state]))
        return {'targets': options(role), 'sources': options(donor, True), 'bindings': rows,
                'installed': bool(self.installed_moves)}

    def edit_move_set(self, role, state, donor=None, original=None):
        self.ensure_open()
        selected(self.roles, role)
        if type(state) is not int:
            raise ValueError('请选择有效的目标动作。')
        if self.installed_moves:
            mounted = self.installed_moves.get((role,state))
            if (donor is None and mounted is None) or (mounted and mounted[:2] == (donor,original)):
                return self.edit_status()
        before = self.snapshot()
        try:
            if self.installed_moves:
                self.resume_move_workspace()
            return self._edit_move_set(before, role, state, donor, original)
        except Exception:
            self.apply_snapshot(before)
            raise

    def _edit_move_set(self, before, role, state, donor, original):
        changes = self.move_sets.copy()
        if donor is None:
            changes.pop((role, state), None)
        else:
            selected(self.roles, donor)
            if type(original) is not int:
                raise ValueError('请选择有效的来源动作。')
            changes[role, state] = donor, original
        if changes.get((role, state)) != self.move_sets.get((role, state)):
            self.move_event_edits = {key: value for key, value in self.move_event_edits.items()
                                     if key[:2] != (role, state)}
            self.resumed_shape_refs = {key:value for key,value in self.resumed_shape_refs.items()
                                       if key[:2] != (role,state)}
            self.move_anm_edits = {key: value for key,value in self.move_anm_edits.items() if key[:2] != (role,state)}
            self.move_texture_edits = {key: value for key, value in self.move_texture_edits.items()
                                       if key[:2] != (role, state)}
        plan = Compilation(self, changes)
        self.move_sets = changes
        self.remember(before)
        self.move_preview = plan
        return self.edit_status()

    def move_source(self, role, action):
        plan = self.current_moves().states.get((role, action)) if self.move_sets else None
        installed = self.installed_moves.get((role, action))
        if plan:
            source, original, clip = plan.source, plan.original, plan.source_clip
        elif installed:
            source, original, clip = installed
        else:
            return None
        return {'role': source, 'state': original, 'clip': clip, 'name': self.roles[source]['label']}

    def planned_script(self, bom, clip):
        return self.current_moves().scripts.get(bom, {}).get(clip) if self.move_sets else None

    def action_with_move(self, role, move):
        if self.move_sets and (mixed := self.current_moves().states.get((role, move['id']))):
            return {**move, 'clip': mixed.clip, 'parameter': f'0x{mixed.next_state:04X}',
                    'callbacks': self.actions[mixed.source][mixed.original]['callbacks']}
        return move

    def step_history(self, redo=False):
        self.ensure_open()
        previous, next_stack = (self.redo, self.undo) if redo else (self.undo, self.redo)
        if previous:
            before = self.snapshot()
            try:
                self.apply_snapshot(previous[-1])
            except Exception:
                self.apply_snapshot(before)
                raise
            next_stack.append(before)
            previous.pop()
        return self.edit_status()

    def native_value(self, field):
        raw = self.native_edits.get(field.offset)
        return struct.unpack(field.fmt, raw)[0] if raw is not None else field.read(
            self._base_eboot if self.resumed else self.eboot_raw)

    def set_native(self, field, value):
        if value is None:
            self.native_edits.pop(field.offset, None)
            return
        raw = field.encode(value)
        source = self._base_eboot if self.resumed else self.eboot_raw
        if raw == source[field.offset:field.offset + field.size]:
            self.native_edits.pop(field.offset, None)
        else:
            self.native_edits[field.offset] = raw

    def refresh_actions(self):
        self.move_preview = None
        for (role, action), field in self.clip_fields.items():
            self.actions[role][action]['clip'] = self.native_value(field)
        users = defaultdict(Counter)
        for role, metadata in enumerate(self.roles):
            for config, case in enumerate(metadata['cases']):
                forms = [(role, config, shape) for shape in range(len(case['forms']))]
                for move in self.actions[role].values():
                    member, slot, _ = self.anim_bank(case, move['clip'])
                    users[member, slot].update(forms)
                    rage = self.rage_animation(role, case, move)
                    if rage and rage[0] != move['clip']:
                        users[self.anim_bank(case, rage[0])[:2]].update(forms)
        self.anm_uses = users

    def actor_attributes(self, role):
        selected(self.roles, role)
        rows = []
        for band in range(LIFE_BANDS):
            fields = {name: self.attribute_fields[role, band, name] for name in ('attack', 'defense', 'speed')}
            values = {name: self.native_value(field) for name, field in fields.items()}
            rows.append({'band': band, **values, 'address': fields['defense'].va,
                         'travel': self.movement_rates.get((role, band), 1.0),
                         'edited': self.movement_rates.get((role, band)) != self.base_movement.get((role, band))
                                   or any(f.offset in self.native_edits for f in fields.values())})
        energy = self.energy_fields[role]
        return {'base_hp': INITIAL_HP, 'q12': Q12, 'rows': rows,
                'energy': [self.native_value(f) for f in energy],
                'energyEdited': any(f.offset in self.native_edits for f in energy)}

    def edit_energy(self, role, values):
        self.ensure_open()
        fields = selected(self.energy_fields, role)
        if values is not None:
            if not isinstance(values, list) or len(values) != len(fields):
                raise ValueError('请填写当前角色的四项能量值。')
            for field, value in zip(fields, values):
                field.encode(value)
        before = self.snapshot()
        for i, field in enumerate(fields):
            self.set_native(field, None if values is None else values[i])
        self.remember(before)
        return self.edit_status()

    def edit_attributes(self, role, band, updates):
        self.ensure_open()
        selected(self.roles, role)
        selected(range(LIFE_BANDS), band)
        if updates is not None and (not isinstance(updates, dict) or set(updates) - {'attack', 'defense', 'speed', 'travel'}):
            raise ValueError('请选择可编辑的角色属性。')
        changes = updates if updates is not None else dict.fromkeys(('attack', 'defense', 'speed', 'travel'))
        mapped = [(self.attribute_fields[role, band, name], value) for name, value in changes.items() if name != 'travel']
        travel = self.base_movement.get((role, band), 1.0)
        if changes.get('travel') is not None:
            travel = struct.unpack('<f', ground_travel.RATE.encode(changes['travel']))[0]
        for field, value in mapped:
            if value is not None:
                field.encode(value)
        before = self.snapshot()
        if 'travel' in changes:
            if travel == 1.0:
                self.movement_rates.pop((role, band), None)
            else:
                self.movement_rates[role, band] = travel
        for field, value in mapped:
            self.set_native(field, value)
        self.remember(before)
        return {'data': self.actor_attributes(role), 'edits': self.edit_status()}

    def valid_action_clip(self, role, action, clip):
        # Encounter banks are only prepared for their matching opponent.
        field = self.clip_fields[role, action]
        original = field.read(self.eboot_raw)
        for config, case in enumerate(self.roles[role]['cases']):
            member, slot, opponent = self.anim_bank(case, clip)
            if opponent is not None and opponent != self.anim_bank(case, original)[2]:
                return False
            if not 0 <= slot < self.bank(member, b'ANM\0')[0]:
                return False
            for shape in range(len(case['forms'])):
                bom = self.form_binding(role, config, shape)[2]['id']
                if clip >= self.bank(bom, b'BOM\0')[0]:
                    return False
        return True

    def edit_action_clip(self, role, action, clip):
        self.ensure_open()
        field = self.clip_fields.get((role, action))
        if field is None:
            raise ValueError('请选择可编辑的必杀或抓投状态。')
        if clip is not None:
            field.encode(clip)
            if not self.valid_action_clip(role, action, clip):
                raise ValueError('目标片段必须在该角色适用的 ANM 和 BOM 中都有记录。')
        before = self.snapshot()
        self.set_native(field, clip)
        self.refresh_actions()
        self.remember(before)
        return self.edit_status()

    def edit_text(self, role, action, text):
        self.ensure_open()
        selected(self.roles, role)
        if not isinstance(text, str) or (action is not None and action not in self.actions[role]):
            raise ValueError('备注或动作别名无效。')
        before = self.snapshot()
        fields, key = (self.notes, role) if action is None else (self.aliases, (role, action))
        if text.strip():
            fields[key] = text.strip()
        else:
            fields.pop(key, None)
        self.remember(before)
        return self.edit_status()

    def commands(self):
        self.ensure_open()
        return command_catalog(self.read_source_member(self.yum['groups'][4][3], b''))

    def transitions(self, role, action):
        self.ensure_open()
        selected(self.roles, role)
        if self.move_sets:
            plan = self.current_moves()
            phase = plan.private_defs.get((role, action))
            if phase is not None:
                _, source = phase
                return [] if source is None else state_branches(self.image, BASE, role, source)
        if action not in self.actions[role]:
            raise ValueError('请选择当前角色的动作状态。')
        rows = state_branches(self.action_image, BASE, role, action)
        if self.move_sets:
            for row in rows:
                row['target'] = plan.entry_routes.get(row['va'], row['target'])
        return rows

    def music(self, role):
        self.ensure_open()
        selected(self.roles, role)
        return music_catalog(self, role, BASE)

    def music_audio(self, member, wav=False, cancel=None):
        self.ensure_open()
        return self.shared_resources.audio((member, -1), wav, cancel)

    def form_binding(self, role, config, shape):
        case = selected(selected(self.roles, role)['cases'], config)
        form = selected(case['forms'], shape)
        bom = form['bom']
        textures = form['pom_slots']
        if bom['slot'] == '0xFE20':
            bom = case['forms'][0]['bom']
        if len(textures) == 1 and textures[0]['slot'] == '0xFE40':
            textures = case['forms'][0]['pom_slots']
        return case, form, bom, textures

    def node_bindings(self, case, shape):
        descriptor = int(case['descriptor_va'], 16) - BASE
        counts = int(case['counts_va'], 16) - BASE
        image = self.action_image
        table = u32(image, u32(image, descriptor + 60) - BASE + 4 * shape) - BASE
        count = s16(image, u32(image, counts + 32) - BASE + 2 * shape)
        # A600 stores the ANM track-to-node binding and the shadow node's matrix source.
        return list(struct.iter_unpack('<3h', image[table:table + count * 6]))

    def remap(self, case, shape):
        return {track: node for _, node, track in self.node_bindings(case, shape)
                if track >= 0 and node >= 0}

    def anim_bank(self, case, clip):
        for encounter in case['encounter_anm']:
            if encounter['first_clip'] <= clip < encounter['last_clip_exclusive']:
                return encounter['id'], clip - encounter['first_clip'], encounter['encounter_role']
        return case['anm_banks'][0]['id'], clip, None

    def rage_animation(self, role, case, move):
        if role != 6:
            return None
        if self.move_sets and (role, move['id']) in self.current_moves().states:
            return None
        target = ZOFFY_RAGE_ENTRIES.get(move['callbacks'][0])
        if target is not None:
            return target, target
        member, slot, _ = self.anim_bank(case, move['clip'])
        if member in STORY_BANKS and slot == 6:
            # AttachAnimationBankRange changes the ANM pointer for index 6.
            # The state and BOM interpreter still see index 6.
            return 22, move['clip']
        return None

    def hand_slots(self, role, shape, bone_count):
        # 0880B648 gives 08877804 two 48-byte hand tables for each role.
        if shape >= 2:
            return []
        pos = 0x08945E4C - BASE + 96 * role + 48 * shape
        nodes = struct.unpack_from('<12i', self.image, pos)
        if not all(-1 <= n < bone_count for n in nodes):
            return []
        return [nodes[:6], nodes[6:]]

    def animation_last(self, member, slot):
        if self.move_sets and (data := self.current_moves().animations.get(member, {}).get(slot)):
            return anm_header(data)[3]
        count, _, parts = self.bank(member, b'ANM\0')
        if 0 <= slot < count:
            try:
                return anm_header(self.asset_edits.get((member, slot), parts[slot]))[3]
            except ValueError:
                pass
        return None

    def combo_followups(self, case, bom, shape, move):
        if move['callbacks'][1] not in COMBO_UPDATES:
            return {}
        count, _, scripts = self.bank(bom, b'BOM\0')
        start = move['clip']
        opponent = self.anim_bank(case, start)[2]
        followups = defaultdict(dict)
        pending = deque([(start, (), {start})])
        while pending:
            clip, inputs, seen = pending.popleft()
            overlay = self.planned_script(bom, clip)
            if overlay is None and not 0 <= clip < count:
                continue
            last = self.animation_last(*self.anim_bank(case, clip)[:2])
            if last is None:
                continue
            edits = self.edits.get((bom, clip), {})
            for record in event_records(scripts[clip] if overlay is None else pack_script(overlay)):
                patch = edits.get(record.index)
                raw = patch.edited if patch else record.raw
                if record.overlap or len(raw) < 10 or raw[0] != 3 or not raw[1] & (1 << shape):
                    continue
                frame, command, window, target = struct.unpack_from('<4h', raw, 2)
                # 08889124 matches the command, then 08899E74 passes the target to SelectActionAnimation.
                if not (0 <= frame <= last and window > 0 and 0 <= command < len(COMMAND_NAMES)
                        and target >= 0 and target not in seen):
                    continue
                member, slot, target_opponent = self.anim_bank(case, target)
                if target_opponent not in (None, opponent) or self.animation_last(member, slot) is None:
                    continue
                route = (*inputs, COMMAND_NAMES[command])
                choices = followups[target]
                if route not in choices:
                    choices[route] = clip, frame
                    pending.append((target, route, seen | {target}))
        return followups

    def damage_records(self, bom, clip, shape):
        count, _, scripts = self.bank(bom, b'BOM\0')
        overlay = self.planned_script(bom, clip)
        if overlay is None and not 0 <= clip < count:
            return []
        edits = self.edits.get((bom, clip), {})
        hits = []
        for record in event_records(scripts[clip] if overlay is None else pack_script(overlay)):
            if record.overlap or not record.raw[1] & (1 << shape):
                continue
            raw = edits[record.index].edited if record.index in edits else record.raw
            damage = base_damage(raw)
            if damage is not None:
                hits.append({'frame': s16(raw, 2), 'value': damage, 'op': raw[0]})
        return sorted(hits, key=lambda hit: hit['frame'])

    def move_catalog(self, role, case, bom, shape=0):
        bom_count, _, scripts = self.bank(bom['id'], b'BOM\0')
        rows, combos, encounter_rows, rage_rows = [], [], [], []
        timings = {}
        damage = {}

        def catalog_row(move, target, name, inputs, bom_clip=None):
            member, clip, opponent = self.anim_bank(case, target)
            bom_clip = target if bom_clip is None else bom_clip
            if (member, clip) not in timings:
                timings[member, clip] = self.animation_last(member, clip)
            if bom_clip not in damage:
                damage[bom_clip] = self.damage_records(bom['id'], bom_clip, shape)
            records = self.planned_script(bom['id'], bom_clip)
            size = len(records) if records is not None else (
                u32(scripts[bom_clip], 0) if bom_clip < bom_count and len(scripts[bom_clip]) >= 4 else 0)
            return {**move, 'clip': target, 'anm': member, 'slot': clip, 'opponent': opponent,
                         'name': name, 'inputs': inputs, 'damage': damage[bom_clip], 'bom_clip': bom_clip,
                         'events': size,
                         'last': timings[member, clip], 'edited': (member, clip) in self.asset_edits or bool(self.move_sets and
                             self.current_moves().animation_keys.get((member,clip)) in self.move_anm_edits),
                         'bom': bom['id'] if records is not None or bom_clip < bom_count else None}

        states = self.actions[role]
        encounters = defaultdict(list)
        if self.move_sets:
            plan = self.current_moves()
            states = {**states, **{state: plan.preview_state(role, state)
                                 for target, state in plan.states if target == role}}
            for (target, action, source_clip), clip in sorted(plan.animation_aliases.items()):
                if target == role:
                    encounters[action].append((source_clip, clip))
        for action, move in states.items():
            move = self.action_with_move(role, move)
            mix = self.move_source(role, action)
            fallback = self.action_title(role, move)
            if action >= FIRST_PRIVATE_STATE and mix and mix['role'] == LEO_FORWARD[0]:
                origin = mix['state']
                if origin in LEO_STAGES:
                    fallback = '飞踢 · ' + LEO_STAGES[origin]
                if origin in LEO_THROW_STAGES:
                    fallback = '双截棍 · ' + LEO_THROW_STAGES[origin]
            if action >= FIRST_PRIVATE_STATE and mix:
                titles = {(2, 181): '杰克抓取 · 正面', (2, 182): '杰克抓取 · 反面',
                          (2, 163): '杰克 · 长矛演出', (2, 164): '杰克 · 收招',
                          (11, 180): '艾雷王 · 电击投技演出',
                          (8, 163): '梦比优斯 · 光线演出', (8, 164): '梦比优斯 · 收招',
                          (12, 180): '布莱克王 · 双方演出',
                          (15, 163): '帝国 · 光线演出', (15, 164): '帝国 · 收招'}
                fallback = titles.get((mix['role'], mix['state']), fallback)
                if mix['role'] == TARO_DOWN[0] and mix['state'] in TARO_PHASES:
                    fallback = '泰罗 · ' + TARO_PHASES[mix['state']]
            name = self.aliases.get((role, action), fallback)
            inputs = self.input_paths[role].get(action, ())
            native = self.clip_fields.get((role, action))
            followups = self.combo_followups(case, bom['id'], shape, move)
            rows.append({**catalog_row(move, move['clip'], name, inputs),
                         'input': self.entry_commands[role].get(action),
                         'throw': action in THROWS or move['category'] == 11,
                         'clip_edited': native is not None and native.offset in self.native_edits,
                         'clip_editable': native is not None and mix is None, 'has_combo': bool(followups),
                         'move_source': mix})
            for target, routes in followups.items():
                prefix = inputs or (name or f'状态 {action:03d}',)
                suffix, (source, frame) = next(iter(routes.items()))
                steps = (*prefix, *suffix)
                combos.append({**catalog_row(move, target, '', steps), 'followup': True,
                               'combo_source': source, 'combo_frame': frame,
                               'combo_paths': [SEQUENCE.join((*prefix, *route)) for route in routes]})
            for source, target in encounters.get(action, ()):
                encounter_rows.append({**catalog_row(move, target, f'{name} · 来源 ANM {source:03d}', inputs),
                                       'encounter_variant': True, 'move_source': mix})
            rage = self.rage_animation(role, case, move)
            if rage:
                target, event_clip = rage
                rage_rows.append({**rows[-1], **catalog_row(move, target, name, inputs, event_clip),
                                  'rage': True, 'clip_editable': False, 'has_combo': False})
        extras = []
        regular_clips = {row['clip'] for row in (*rows, *combos)}
        for target in range(bom_count):
            if target in regular_clips:
                continue
            hits = damage.get(target)
            if hits is None:
                hits = self.damage_records(bom['id'], target, shape)
            if not hits:
                continue
            damage[target] = hits
            row = catalog_row(loose_animation(target), target, '伤害片段', ())
            if row['last'] is not None:
                extras.append(row)
        by_state = {row['id']:row for row in rows}
        for root in rows:
            if root['category'] != 5:
                continue
            sequence = skill_phases(self,role,root['id'])
            choices = editable_skill_states(self,role,root['id'],sequence)
            root['edit_phases'] = [i for i in choices if i in by_state]
            stages = [by_state[i] for i in sequence if i in by_state]
            if len(stages) < 2:
                continue
            root['phases'] = [row['id'] for row in stages]
            title = root['name'] or ' / '.join(root['inputs']) or f"状态 {root['id']}"
            for n, row in enumerate(stages[1:], 2):
                label = f'{title} · 第 {n} 段'
                if not row['name']:
                    row['name'] = label
                elif label not in row['inputs']:
                    row['inputs'] = (*row['inputs'], label)
        return rows + combos + encounter_rows + extras + rage_rows

    def action_animation(self, role, case, bom, shape, selection):
        action, target = selection, None
        if (isinstance(selection, list) and len(selection) == 2
                and all(type(value) is int for value in selection)):
            action, target = selection
        if action == -1 and target is not None and self.damage_records(bom, target, shape):
            return loose_animation(target), target
        move = None
        if type(action) is int:
            move = (self.current_moves().preview_state(role, action) if self.move_sets
                    else self.native_actions[role].get(action))
        if move is None:
            raise ValueError('这个动作没有指定 ANM 片段。')
        move = self.action_with_move(role, move)
        if move['clip'] is None:
            raise ValueError('这个动作没有指定 ANM 片段。')
        if target is None or target == move['clip']:
            return move, move['clip']
        if self.move_sets:
            plan = self.current_moves()
            recipe = plan.recipes.get((role, target))
            if recipe is not None and recipe.state == action and target != move['clip']:
                return {**move, 'encounter_variant': True}, target
        rage = self.rage_animation(role, case, move)
        if rage is not None and target == rage[0]:
            return {**move, 'rage': True, 'bom_clip': rage[1]}, target
        if target not in self.combo_followups(case, bom, shape, move):
            raise ValueError('当前形态没有这条连招后续片段。')
        return move, target

    def animation_users(self, key):
        member, _ = key
        users = self.anm_uses.get(key, Counter()).copy()
        for role, metadata in enumerate(self.roles):
            for config, case in enumerate(metadata['cases']):
                if not any(bank['id'] == member for bank in (*case['anm_banks'], *case['encounter_anm'])):
                    continue
                first_clip = next((bank['first_clip'] for bank in case['encounter_anm'] if bank['id'] == member), 0)
                for shape in range(len(case['forms'])):
                    bom = self.form_binding(role, config, shape)[2]['id']
                    for move in self.actions[role].values():
                        clips = self.combo_followups(case, bom, shape, move)
                        users[role, config, shape] += sum(self.anim_bank(case, clip)[:2] == key for clip in clips)
                    if not users[role, config, shape] and self.damage_records(bom, first_clip + key[1], shape):
                        users[role, config, shape] = 1
        return +users

    def asset_binding(self, role, config, shape, kind, index):
        case, form, bom, textures = self.form_binding(role, config, shape)
        if kind == 'mdl':
            return form['mdl']['id'], None
        if kind == 'pom':
            if type(index) is int and index >= len(textures):
                entries = sorted(self.private_texture_usage(role, config, shape))
                return self.texture_copy(case, selected(entries, index - len(textures)))
            return selected(textures, index)['id'], None
        if kind == 'anm':
            _, clip = self.action_animation(role, case, bom['id'], shape, index)
            return self.anim_bank(case, clip)[:2]
        raise ValueError('请先选择要操作的资源。')

    def private_texture_usage(self, role, config, shape):
        case = self.roles[role]['cases'][config]
        if self.move_sets or self.resumed:
            return self.current_moves().move_textures(case, shape)
        if self.installed_palettes:
            bank = case['anm_banks'][0]['id']
            return {key: {p for p in range(mask.bit_length()) if mask & (1 << p)}
                    for (key, carrier), masks in self.installed_palettes.items()
                    if key[0] == role and carrier == bank and (mask := masks[shape]) is not None}
        return self.legacy_texture_usage(role, config, shape) if self.installed_move_textures else {}

    def texture_copy(self, case, key):
        bank = case['anm_banks'][0]['id']
        return (*key, bank) if self.keeps_move_texture(key) and bank in self.installed_move_textures.get(key, {}) else key

    def keeps_move_texture(self, key, assignments=None):
        if not self.resumed:
            return True
        choices = self.move_sets if assignments is None else assignments
        return key[:2] in self._recovery.roots and self._recovery.roots[key[:2]] == choices.get(key[:2])

    def move_texture_data(self, key, original=False):
        if not original and key in self.move_texture_edits:
            return self.move_texture_edits[key]
        if len(key) == 4:
            bank = key[3]
            offset, size = self.installed_move_textures[key[:3]][bank]
            with self.path.open('rb') as handle:
                handle.seek(self.member_offset(bank) + offset)
                data = handle.read(size)
            if len(data) != size or not data.startswith(b'POM\0'):
                raise ValueError('ISO 里的配招贴图越过 ANM 资源边界。')
            return data
        return self.read_source_member(key[2], b'POM\0')

    def asset_bytes(self, kind, key, original=False):
        if kind == 'pom' and len(key) >= 3:
            return self.move_texture_data(key, original)
        if kind == 'anm' and self.move_sets:
            plan = self.current_moves()
            if key in plan.animation_keys:
                return plan.animation_defaults[key] if original else plan.animations[key[0]][key[1]]
        if not original and key in self.asset_edits:
            return self.asset_edits[key]
        member, slot = key
        if slot is None:
            return self.read_source_member(member, ASSET_MAGIC[kind])
        count, _, clips = self.bank(member, b'ANM\0')
        if not 0 <= slot < count:
            raise ValueError(f'ANM {member} 中没有片段 {slot}。')
        return clips[slot]

    def asset_info(self, kind, key, size=None, form=None):
        if kind == 'pom' and len(key) >= 3:
            role, root, member = key[:3]
            copies = self.installed_move_textures.get(key[:3], {})
            location = ({'carrier': key[3], 'carrier_offset': copies[key[3]][0], 'copies': len(copies)}
                        if len(key) == 4 else {})
            return {**location, 'kind': kind, 'member': member, 'clip': None, 'size': len(self.move_texture_data(key)),
                    'edited': any(k[:3] == key[:3] for k in self.move_texture_edits), 'forms': 0, 'move_texture': True,
                    'move_state': root,
                    'label': f'配招 {self.entry_commands[role].get(root, root)} · POM {member}'}
        if kind == 'anm' and self.move_sets:
            logical = self.current_moves().animation_keys.get(key)
            if logical is not None:
                return {'kind':kind,'member':key[0],'clip':key[1],'size':len(self.asset_bytes(kind,key)),
                        'edited':logical in self.move_anm_edits,'label':f'配招 ANM {key[1]}',
                        'forms':sum(len(case['forms']) for case in self.roles[logical[0]]['cases']
                                    if case['anm_banks'][0]['id'] == key[0]), 'actions':1}
        member, subslot = key
        edited = key in self.asset_edits
        if size is None:
            size = len(self.asset_edits[key]) if edited else self.yum['files'][member][1]
        if kind == 'anm':
            users = self.animation_users(key)
        elif kind == 'pom':
            users = self.pom_forms[member]
        else:
            users = self.model_forms[member]
        info = {'kind': kind, 'member': member, 'clip': subslot, 'size': size,
                'edited': edited, 'forms': len(users)}
        if kind != 'anm':
            info['paired'] = member in self.model_atlases or any(member in group for group in self.model_atlases.values())
        if kind == 'anm' and form is not None:
            info['actions'] = users[form]
        return info

    def appearance_assets(self, member, textures, materials):
        rows = [self.asset_info('mdl', (member, None))]
        for slot, texture in enumerate(textures):
            item = texture['id']
            layout = get_pom_layout(self.read_member(item, b'POM\0', 64))
            active = sorted({m['variant'] for m in materials if m['texture'] == slot})
            rows.append({**self.asset_info('pom', (item, None)), 'slot': slot,
                         'width': layout['width'], 'height': layout['height'],
                         'palettes': layout['palette_count'], 'active_palettes': active})
        return rows

    def move_texture_assets(self, role, config, shape, first_slot):
        case = self.roles[role]['cases'][config]
        usage = self.private_texture_usage(role, config, shape)
        rows = []
        for index, key in enumerate(sorted(usage), first_slot):
            physical = self.texture_copy(case, key)
            layout = get_pom_layout(self.move_texture_data(physical))
            palettes = usage[key]
            active = sorted(palettes or ())
            if palettes is not None and not active and layout['palette_count'] == 1:
                active = [0]
            rows.append({**self.asset_info('pom', physical), 'slot': index,
                         'width': layout['width'], 'height': layout['height'],
                         'palettes': layout['palette_count'],
                         'palette_unknown': palettes is None,
                         'active_palettes': active})
        return rows

    def export_asset(self, role, config, shape, kind, index, palette, extension, path):
        self.ensure_open()
        key = self.asset_binding(role, config, shape, kind, index)
        data = self.asset_bytes(kind, key)
        if kind == 'pom' and extension == '.png':
            data = texture_png(data, palette)
        elif extension != '.' + kind:
            raise ValueError('导出扩展名与当前资源不一致。')
        self.ensure_open()
        return write_asset(path, extension, data)

    def _check_model_materials(self, member, model, pending=None):
        pending = pending or {}
        for selection in self.model_forms[member]:
            textures = self.form_binding(*selection)[3]
            for material in model['materials']:
                slot, palette = material['texture'], material['variant']
                if slot < 0:
                    continue
                if slot >= len(textures):
                    raise ValueError(f'新 MDL 使用了形态中不存在的贴图槽 {slot}。')
                texture = textures[slot]['id']
                header = pending.get(texture)
                layout = get_pom_layout(header[:64] if header else self.read_member(texture, b'POM\0', 64))
                if palette >= layout['palette_count']:
                    raise ValueError(f'新 MDL 需要贴图槽 {slot} 的色表 {palette}，请先导入对应 POM。')

    def _check_texture_materials(self, member, data, pending_model=None):
        layout = pom_layout(data)
        for selection in self.pom_forms[member]:
            case, form, _, textures = self.form_binding(*selection)
            slots = {slot for slot, t in enumerate(textures) if t['id'] == member}
            mdl = form['mdl']['id']
            model_bytes = (pending_model[1] if pending_model and mdl == pending_model[0]
                           else self.read_member(mdl, b'MDL\0'))
            model = read_mdl(model_bytes)
            if any(m['texture'] in slots and m['variant'] >= layout['palette_count'] for m in model['materials']):
                raise ValueError('该 POM 缺少当前模型使用的色表。')
            role = selection[0] if form['mdl']['id'] == case['forms'][0]['mdl']['id'] else MODEL_ROLES.get(form['mdl']['id'])
            timer = color_timer(self.image, BASE, role)
            if timer is None:
                continue
            normal = next((copies for key, _, _, copies, _ in palette_modes(self.image, BASE, role)
                           if key == 'normal'), {})
            source = normal.get(timer)
            if source is None or not slots.intersection((timer[0], source[0])):
                continue
            if source[0] >= len(textures):
                continue
            pair = [layout if textures[slot]['id'] == member else
                    get_pom_layout(self.read_member(textures[slot]['id'], b'POM\0', 64))
                    for slot in (timer[0], source[0])]
            if (pair[0]['color_entries'] > pair[1]['color_entries']
                    or pair[0]['palette_count'] <= timer[1] or pair[1]['palette_count'] < 4):
                raise ValueError('该 POM 与原生计时器色表复制不兼容。请保留计时器槽及其来源槽的原生色表规格。')

    def _check_animation(self, key, data):
        member, slot = key
        inherited = self.resumed and key in self._recovery.clip_keys
        _, offsets, clips = (read_anm_bank(self.read_image_member(member, b'ANM\0')) if inherited
                             else self.bank(member, b'ANM\0'))
        if type(slot) is not int or not 0 <= slot < len(clips) or not clips[slot]:
            raise ValueError('目标动画库中没有这个片段。')
        allowed = set(self.bank_tracks(member))
        if inherited:
            allowed.update(clip_ids(clips[slot]))
        if clip_ids(data) - allowed:
            for role, config, shape in self.animation_users(key):
                allowed.update(self.remap(self.roles[role]['cases'][config], shape))
        validate_clip(data, allowed)
        if member in STORY_BANKS:
            protect_story_clip(offsets, len(data), slot)

    def keyframes(self, role, config, shape, action, logical, channel):
        self.ensure_open()
        data = self.asset_bytes('anm', self.asset_binding(role, config, shape, 'anm', action))
        return read_keys(data, logical, channel)

    def edit_keys(self, role, config, shape, action, logical, channel, rows):
        self.ensure_open()
        key = self.asset_binding(role, config, shape, 'anm', action)
        original = self.asset_bytes('anm', key, original=True)
        data = write_keys(self.asset_bytes('anm', key), logical, channel, rows)
        return self._commit_asset('anm', key, original, data)

    def edit_timing(self, role, config, shape, action, frames, rate, horizontal=1.0):
        self.ensure_open()
        key = self.asset_binding(role, config, shape, 'anm', action)
        original = self.asset_bytes('anm', key, original=True)
        data = change_timing(self.asset_bytes('anm', key), frames, rate, horizontal)
        return self._commit_asset('anm', key, original, data)

    def edit_asset(self, role, config, shape, kind, index, palette, path, palette_only=False):
        self.ensure_open()
        key = self.asset_binding(role, config, shape, kind, index)
        if palette_only and kind != 'pom':
            raise ValueError('只换色表需要选择贴图。')
        if kind == 'pom' and len(key) >= 3:
            return self.edit_private_texture(key, palette, path, palette_only)
        original = self.asset_bytes(kind, key, original=True)
        refreshed = False
        adjusted = 0
        if path is None:
            data = original
        else:
            path = Path(path)
            extension = path.suffix.lower()
            if palette_only and extension != '.png':
                raise ValueError('只换色表请选择 PNG 文件。')
            converted_input = (kind, extension) in {('pom', '.png'), ('mdl', '.obj'), ('mdl', '.fbx')}
            if extension != '.' + kind and not converted_input:
                raise ValueError(f'请选择用于 {kind.upper()} 的资源文件。')
            data = path.read_bytes()
            if kind == 'pom' and (extension == '.png' or data != original):
                data = replace_texture(original, self.asset_bytes(kind, key), data, extension, palette, palette_only)
            elif kind == 'mdl' and data != original:
                selection = role, config, shape
                if extension in ('.obj', '.fbx'):
                    geometry, rigid_parts, skin = (read_fbx(data) if extension == '.fbx'
                                                   else (read_obj(path, data), (), None))
                    target = self.model_rig(read_mdl(original), selection)
                    if skin is not None:
                        geometry, poms, warnings = stage_fbx_textures(
                            self, path, geometry, skin, target, selection, BASE, IMPORT_ROTATIONS)
                        data = mesh_to_mdl(geometry, original, target, rigid_parts, skin)
                        return {**self.commit_model_bundle(key[0], original, data, poms, path.name),
                                'rig_refreshed': True, 'textures': list(poms), 'texture_warnings': warnings}
                    reference = self.part_reference(target, selection, key[0])
                    data = mesh_to_mdl(geometry, original, target, rigid_parts, skin, reference)
                    refreshed = True
                else:
                    data, refreshed, adjusted = self.align_model(key[0], original, data, selection)
        result = self._commit_asset(kind, key, original, data, path.name if path is not None else None)
        if refreshed:
            result['rig_refreshed'] = True
        if adjusted:
            result['material_adjustments'] = adjusted
        return result

    def edit_private_texture(self, key, palette, path, palette_only):
        logical = key[:3]
        copies = self.installed_move_textures.get(logical) if self.keeps_move_texture(logical) else None
        changes = self.move_texture_edits.copy()
        file = Path(path) if path is not None else None
        data = file.read_bytes() if file is not None else None
        for physical in ([(*logical, bank) for bank in copies] if copies else [logical]):
            original = self.move_texture_data(physical, original=True)
            current = self.move_texture_data(physical)
            edited = original
            if file is not None and (file.suffix.lower() != '.pom' or data != original or palette_only):
                edited = replace_texture(original, current, data, file.suffix.lower(), palette, palette_only)
            if copies:
                if len(edited) > len(original):
                    raise ValueError(f'此 POM 需要 {len(edited)} B，镜像内预留 {len(original)} B。可在原配招工程中导入并重新另存 ISO。')
                edited += original[len(edited):]
            if edited == original:
                changes.pop(physical, None)
            else:
                changes[physical] = edited
        before = self.snapshot()
        self.move_texture_edits = changes
        self.remember(before)
        return {'name': file.name if file else None, 'asset': self.asset_info('pom', key), 'edits': self.edit_status()}

    def model_parts(self, binding, role, case, member):
        appearance_role = role if member == case['forms'][0]['mdl']['id'] else MODEL_ROLES.get(member)
        if all(track in binding for track in LEO_WEAPON_TRACKS):
            return [{'key': 'nunchaku', 'name': '双截棍', 'nodes': [binding[t] for t in LEO_WEAPON_TRACKS],
                     'activeTrack': LEO_WEAPON_TRACKS[0]}]
        nodes = [binding[track] for track in (77, 78) if appearance_role == 5 and track in binding]
        return [{'key': 'nunchaku', 'name': '双截棍', 'nodes': nodes}] if nodes else []

    def model_rig(self, model, selection):
        role, config, shape = selection
        case, form, _, _ = self.form_binding(*selection)
        bindings = self.node_bindings(case, shape)
        tracks = {track: node for _, node, track in bindings if track >= 0 and node >= 0}
        hands = self.hand_slots(role, shape, len(model['bones']))
        parts = self.model_parts(tracks, role, case, form['mdl']['id'])
        member = form['mdl']['id']
        appearance_role = role if member == case['forms'][0]['mdl']['id'] else MODEL_ROLES.get(member)
        timer = color_timer(self.image, BASE, appearance_role)
        sources = ((shadow, node) for shadow, node, _ in bindings if shadow >= 0 and node >= 0)
        return Rig(model, tracks, hands, (node for part in parts for node in part['nodes']), timer, sources)

    def part_reference(self, rig, selection, current):
        """Recover native pieces removed by an earlier import from the same bind."""
        missing = missing_hands(rig)
        lamp = rig.timer is not None and not timer_faces(rig)
        if not missing and not lamp:
            return None
        role = selection[0]
        layout = topology(rig.model)
        bind = [bone['raw'][:96] for bone in rig.bones]
        for member, placements in self.model_forms.items():
            if member == current or not any(p[0] == role for p in placements):
                continue
            header = self.read_source_member(member, b'MDL\0', 32)
            if struct.unpack_from('<H', header, 14)[0] != len(rig.bones):
                continue
            model = read_mdl(self.read_source_member(member, b'MDL\0'))
            if topology(model) != layout or [b['raw'][:96] for b in model['bones']] != bind:
                continue
            reference = self.model_rig(model, selection)
            if missing.intersection(missing_hands(reference)) or (lamp and not timer_faces(reference)):
                continue
            return reference
        return None

    def source_model_selection(self, data, model):
        count = len(model['bones'])
        donor_layout = topology(model)
        donor_bind = [bytes.fromhex(node['raw'])[:48] for node in model['bones']]
        candidates = []
        for member, placements in sorted(self.model_forms.items()):
            header = self.read_source_member(member, b'MDL\0', 32)
            if struct.unpack_from('<H', header, 14)[0] != count:
                continue
            raw = self.read_source_member(member, b'MDL\0')
            placement = min(placements, key=lambda p: (p[2] != 0, p[1] != 0, p))
            if raw == data:
                # Story stand-ins reuse the exact MDL with a different hand
                # table. Prefer the character's own appearance as the donor.
                candidates.append((False, 0, placement[2] != 0, placement[1] != 0, placement))
                continue
            table, = struct.unpack_from('<I', raw, 20)
            if donor_layout == tuple(struct.unpack_from('<3h', raw, table + n*64 + 50) for n in range(count)):
                delta = sum(raw[table+n*64:table+n*64+48] != bind for n, bind in enumerate(donor_bind))
                candidates.append((True, delta, placement[2] != 0, placement[1] != 0, placement))
        return min(candidates)[-1] if candidates else None

    def fit_model_materials(self, member, materials):
        layouts = []
        for placement in sorted(self.model_forms[member]):
            textures = self.form_binding(*placement)[3]
            layouts.append([get_pom_layout(self.read_member(t['id'], b'POM\0', 64))['palette_count']
                            for t in textures])
        palettes = [min(counts) for counts in zip(*layouts)]
        result = []
        adjusted = 0
        for material in materials:
            slot, palette = material['texture'], material['variant']
            if slot >= len(palettes):
                material = {**material, 'texture': -1, 'variant': 0}
                adjusted += 1
            elif slot >= 0 and palette >= palettes[slot]:
                material = {**material, 'variant': 0}
                adjusted += 1
            result.append(material)
        return result, adjusted

    def align_model(self, target_member, original, data, selection):
        source, target = read_mdl(data), read_mdl(original)
        materials, adjusted = self.fit_model_materials(target_member, source['materials'])
        same_bind = (topology(source) == topology(target)
                     and all(a['raw'][:96] == b['raw'][:96] for a, b in zip(source['bones'], target['bones'])))
        if same_bind:
            if adjusted:
                patched = bytearray(data)
                for i, material in enumerate(materials):
                    struct.pack_into('<iI', patched, 32 + 32*i + 20, material['texture'], material['variant'])
                data = bytes(patched)
            return data, False, adjusted
        origin = self.source_model_selection(data, source)
        donor = self.model_rig(source, origin) if origin is not None else Rig(source, {})
        native = self.model_rig(target, selection)
        return retarget_model(original, native, data, donor, materials), True, adjusted

    def _commit_asset(self, kind, key, original, data, name=None):
        member, _ = key
        if kind == 'anm' and self.move_sets:
            logical = self.current_moves().animation_keys.get(key)
            if logical is not None:
                self.ensure_open()
                validate_clip(data,clip_ids(original))
                before = self.snapshot()
                if data == original:
                    self.move_anm_edits.pop(logical,None)
                else:
                    self.move_anm_edits[logical] = data
                self.remember(before)
                return {'name':name,'asset':self.asset_info(kind,key),'edits':self.edit_status()}
        if kind == 'mdl':
            self._check_model_materials(member, compatible_model(original, data))
        elif kind == 'pom':
            self._check_texture_materials(member, data)
        else:
            self._check_animation(key, data)
        self.ensure_open()
        before = self.snapshot()
        if data == original:
            self.asset_edits.pop(key, None)
            model = member if kind == 'mdl' and member in self.model_atlases else None
            if kind == 'pom':
                model = next((item for item, textures in self.model_atlases.items() if member in textures), None)
            if model is not None:
                textures = self.model_atlases.pop(model)
                self.asset_edits.pop((model, None), None)
                for texture in textures:
                    self.asset_edits.pop((texture, None), None)
                self.texture.cache_clear()
                self.node_count.cache_clear()
        else:
            self.asset_edits[key] = data
        if kind == 'pom':
            self.texture.cache_clear()
        if kind == 'mdl':
            self.node_count.cache_clear()
        self.remember(before)
        return {'name': name,
                'asset': self.asset_info(kind, key, len(data)), 'edits': self.edit_status()}

    def commit_model_bundle(self, member, original, model, poms, name):
        self._check_model_materials(member, compatible_model(original, model), poms)
        for texture, raw in poms.items():
            self._check_texture_materials(texture, raw, (member, model))
        self.ensure_open()
        before = self.snapshot()
        for unused in set(self.model_atlases.pop(member, ())) - poms.keys():
            self.asset_edits.pop((unused, None), None)
        if poms:
            self.model_atlases[member] = tuple(sorted(poms))
            self.asset_edits.update({(texture, None):raw for texture, raw in poms.items()})
        self.asset_edits[member, None] = model
        self.texture.cache_clear()
        self.node_count.cache_clear()
        self.remember(before)
        return {'name':name, 'asset':self.asset_info('mdl', (member, None)), 'edits':self.edit_status()}

    def preview_geometry(self, mdl, poms, appearance_role=None, overrides=None):
        bones = [{k: bone[k] for k in ('parent', 'rotation', 'translation', 'scale')} for bone in mdl['bones']]
        meshes, triangles = model_meshes(mdl)
        used_materials = {index for mesh in meshes for index, _ in mesh['batches']}
        materials = []
        images = []
        seen = {}
        notes = []

        def texture_index(slot, palette, source_slot=None, source_palette=None):
            if not 0 <= slot < len(poms) or poms[slot] is None:
                return None
            source = None
            if source_slot is not None:
                if not 0 <= source_slot < len(poms) or poms[source_slot] is None:
                    return None
                source = poms[source_slot]
            key = poms[slot], palette, source, source_palette
            if key not in seen:
                try:
                    data = (overrides or {}).get(poms[slot])
                    if data is None or source is not None:
                        images.append(self.texture(*key))
                    else:
                        w, h, png = render_pom_png(data, auto_color_key=False, opaque=True, palette_index=palette)
                        images.append({'id': poms[slot], 'palette': palette, 'width': w, 'height': h,
                                       'uri': 'data:image/png;base64,' + base64.b64encode(png).decode('ascii')})
                    seen[key] = len(images) - 1
                except ValueError as exc:
                    if source is None and str(exc) not in notes:
                        notes.append(str(exc))
                    seen[key] = None
            return seen[key]

        for index, desc in enumerate(mdl['materials']):
            slot, variant = desc['texture'], desc['variant']
            texture = texture_index(slot, variant) if index in used_materials else None
            if index in used_materials and slot >= 0 and (slot >= len(poms) or poms[slot] is None):
                message = f'材质槽 {slot} 没有绑定贴图。'
                if message not in notes:
                    notes.append(message)
            diffuse = [((desc['diffuse'] >> shift) & 255) / 255 for shift in (0, 8, 16, 24)]
            materials.append({'texture': texture, 'tint': diffuse, 'kind': desc['kind'],
                              'blend': desc['flags'] & 0xFF})
        appearances = []
        for key, name, callback, palettes, lamps in palette_modes(self.image, BASE, appearance_role):
            overrides = {}
            for index in sorted(used_materials):
                desc = mdl['materials'][index]
                slot, variant = desc['texture'], desc['variant']
                if (slot, variant) in palettes and palettes[slot, variant] != (slot, variant):
                    texture = texture_index(slot, variant, *palettes[slot, variant])
                    if texture is not None:
                        overrides[index] = texture
                if lamps and slot == 1 and variant in lamps[1]:
                    cycle = [texture_index(slot, variant, 2, n) for n in range(lamps[0], lamps[0] + 4)]
                    if all(value is not None for value in cycle):
                        overrides[index] = cycle
            if key == 'normal' or overrides:
                appearances.append({'key': key, 'name': name, 'va': callback, 'textures': overrides})
        return {'bones': bones, 'materials': materials, 'textures': images, 'meshes': meshes,
                'triangles': triangles, 'notes': notes, 'appearances': appearances}

    def model(self, role, config, shape):
        self.ensure_open()
        case, form, bom, textures = self.form_binding(role, config, shape)
        mdl_id = form['mdl']['id']
        raw = self.read_member(mdl_id, b'MDL\0')
        bindings = self.node_bindings(case, shape)
        if self.move_sets:
            plan = self.current_moves()
            if (weapon := plan.weapon_forms.get((int(case['descriptor_va'], 16), shape))):
                raw = plan.weapon_models[mdl_id]
                bindings = weapon[3]
        mdl = read_mdl(raw)
        appearance_role = role if mdl_id == case['forms'][0]['mdl']['id'] else MODEL_ROLES.get(mdl_id)
        binding = {track: node for _, node, track in bindings if track >= 0 and node >= 0}
        poms = [texture['id'] for texture in textures]
        if all(track in binding for track in LEO_WEAPON_TRACKS):
            poms.append(LEO_WEAPON_POM)
        weapon_key = self.texture_copy(case, (role, 160, LEO_WEAPON_POM))
        weapon = (self.move_texture_data(weapon_key) if len(weapon_key) == 4
                  else self.move_texture_edits.get(weapon_key))
        overrides = {LEO_WEAPON_POM: weapon} if weapon and LEO_WEAPON_POM in poms else None
        geometry = self.preview_geometry(mdl, poms, appearance_role, overrides)
        if not all(0 <= node < len(geometry['bones']) for node in binding.values()):
            raise ValueError('形态的骨架映射超出 MDL 节点目录。')
        items = self.preview_items(role, case)
        parts = self.model_parts(binding, role, case, mdl_id)
        shadows = {shadow: node for shadow, node, _ in bindings
                   if 0 <= shadow < len(geometry['bones']) and 0 <= node < len(geometry['bones'])}
        skeleton = set(binding.values()) - shadows.keys()
        for node in tuple(skeleton):
            parent = geometry['bones'][node]['parent']
            while parent >= 0 and parent not in shadows:
                skeleton.add(parent)
                parent = geometry['bones'][parent]['parent']
        forms = []
        for index in range(len(case['forms'])):
            _, item, timeline, poms = self.form_binding(role, config, index)
            forms.append({'shape': index, 'mdl': item['mdl']['id'], 'anm': case['anm_banks'][0]['id'],
                          'bom': timeline['id'], 'poms': [pom['id'] for pom in poms]})
        return {'role': role, 'config': config, 'shape': shape, 'mdl': mdl_id,
                'group': case['group'], 'voice': case['voice_bank'].get('id'), **geometry,
                'rig': binding, 'skeleton': sorted(skeleton),
                'hands': self.hand_slots(role, shape, len(geometry['bones'])),
                'parts': parts, 'items': items, 'moves': self.move_catalog(role, case, bom, shape),
                'forms': forms, 'note': self.notes.get(role, ''),
                'assets': [*self.appearance_assets(mdl_id, textures, mdl['materials']),
                           *self.move_texture_assets(role, config, shape, len(textures))]}

    def preview_items(self, role, case):
        items = list(item_models(self.image, BASE, case, self.yum['groups'][case['group']]))
        slot = max((item['slot'] for item in items), default=-1) + 1
        origins = {state: (source, original) for (target, state), (source, original) in self.move_sets.items()
                   if target == role}
        origins.update({state: (source, original) for (target, state), (source, original, _) in self.installed_moves.items()
                        if target == role})
        parts = {(2, 162): (1, '杰克 · 长矛'), (14, 163): (0, '希波利特 · 捕获舱'),
                 (16, 160): (0, '巴巴尔 · 锁链')}
        for state, origin in sorted(origins.items()):
            if origin in parts:
                index, name = parts[origin]
                source = self.roles[origin[0]]['cases'][0]
                stock = item_models(self.image, BASE, source, self.yum['groups'][source['group']])
                asset = next(item for item in stock if item['slot'] == index)
                items.append({**asset, 'slot': slot, 'name': name, 'scope': (role, state)})
                slot += 1
            elif origin == (12, 163):
                _, form, _, textures = self.form_binding(*BLACK_SCENE_SOURCE)
                items.append({'slot': slot, 'name': '布莱克王 · 演出模型', 'scope': (role, state),
                              'member': form['mdl']['id'], 'poms': [texture['id'] for texture in textures]})
                slot += 1
        return items

    def item_model(self, role, config, shape, slot):
        self.ensure_open()
        case = self.form_binding(role, config, shape)[0]
        item = next((entry for entry in self.preview_items(role, case) if entry['slot'] == slot), None)
        if item is None:
            raise ValueError('当前出场配置没有此道具模型。')
        mdl = read_mdl(self.read_member(item['member'], b'MDL\0'))
        scope = item.get('scope')
        overrides = {}
        if scope:
            for member in item['poms']:
                key = self.texture_copy(case, (*scope, member))
                if len(key) == 4 or key in self.move_texture_edits:
                    overrides[member] = self.move_texture_data(key)
        return {**item, **self.preview_geometry(mdl, item['poms'], overrides=overrides),
                'rig': {}, 'hands': [], 'parts': []}

    def texture_preview(self, role, config, shape, slot, palette):
        self.ensure_open()
        key = self.asset_binding(role, config, shape, 'pom', slot)
        if len(key) >= 3:
            return 'data:image/png;base64,' + base64.b64encode(texture_png(self.move_texture_data(key), palette)).decode('ascii')
        return self.texture(key[0], palette, opaque=False)['uri']

    def sound_code(self, event):
        if event in self.sound_extensions:
            return self.sound_extensions[event] & ~VOICE
        if 0 <= event < SOUND_COUNT:
            return u32(self.image, SOUND_TABLE - BASE + 4 * event)
        return None

    def read_sound_bank(self, member):
        return self.read_member(member, b'pBAV')

    def sound_in_case(self, case, cue):
        return cue not in self.sound_extensions or (case.get('voice_bank') or {}).get('id') in self.sound_members.get(cue,())

    def _sound_sample(self, case, role, cue):
        code = self.sound_code(cue)
        if code is None or not self.sound_in_case(case,cue):
            return None
        bank, program, tone = (code >> 16) & 255, (code >> 8) & 255, code & 255
        if bank == 7 + role:
            member = case['voice_bank'].get('id')
        elif bank in (0, 1):
            member = self.yum['groups'][1][3] + bank  # 088BCC88 loads YUM group 1 in bank order.
        elif bank == 6:
            member = self.yum['groups'][2][3] + 1   # 0880B368 binds group 2, slot 1.
        else:
            member = None
        if member is not None:
            data = self.sound_bank(member)
            sample = find_sample(data, program, tone)
            if sample:
                return member, data, sample
        return None

    def sound_preview(self, role, config, cue):
        self.ensure_open()
        case = selected(selected(self.roles, role)['cases'], config)
        ref = self._sound_sample(case, role, cue)
        if ref is None:
            raise ValueError('当前配置中没有可试听的采样。')
        _, data, (_, offset, size, rate) = ref
        wav = vag_wave(data[offset:offset + size], rate)
        return 'data:audio/wav;base64,' + base64.b64encode(wav).decode('ascii')

    def _event_words(self, record, member, last, template_count):
        opcode, mask = record.raw[:2]
        models = self.bom_models[member]
        nodes = 0
        if opcode in SHAPE_TEMPLATE_OFFSETS or opcode == 15:
            nodes = min((self.node_count(mdl) for form, mdl in models if mask & (1 << form)), default=0)
        return event_words(record.raw, record.overlap, last, nodes, template_count, self.sound_count)

    def _motion_events(self, case, role, member, clip, last, edit):
        count, offsets, parts = self.bank(member, b'BOM\0')
        if clip >= count:
            if edit is not None:
                raise ValueError('此动作没有 BOM 事件脚本。')
            return []

        key = member, clip
        templates = [self.template_edits.get((member, i), data) for i, data in enumerate(parts[count:])]
        records = {item.index: item for item in event_records(parts[clip])}
        schemas = {i: self._event_words(record, member, last, len(templates)) for i, record in records.items()}
        pending = self.edits.get(key, {})
        if edit is not None:
            event_id, updates = edit
            if type(event_id) is not int or not schemas.get(event_id):
                raise ValueError('请选择可编辑的事件。')
            record = records[event_id]
            prior = pending.get(event_id)
            current = prior.edited if prior else record.raw
            edited = record.raw if updates is None else change_words(current, schemas[event_id], updates, templates)
            if (updates is not None and edited[0] in (14,15) and edited[4:6] != current[4:6]
                    and not self.sound_in_case(case,s16(edited,4))):
                raise ValueError('这个新增音色属于其他出场配置。')
            pending = pending.copy()
            if edited == record.raw:
                pending.pop(event_id, None)
            else:
                pending[event_id] = Patch(offsets[clip] + record.offset, record.raw, edited)

        events = []
        for event_id, record in records.items():
            patch = pending.get(event_id)
            raw = patch.edited if patch else record.raw
            event = decode_event(raw, templates, self.sound_code)
            if record.overlap:
                event.pop('damage', None)
                event['fields'].pop('基础伤害', None)
            event.update(id=event_id, offset=offsets[clip] + record.offset,
                         raw=raw.hex(' '), overlap=record.overlap, edited=patch is not None,
                         editable=[field.describe(raw) for field in schemas[event_id]])
            if event['kind'] == 'audio':
                fields = event['fields']
                cue = fields.get('音效编号', -1)
                if (ref := self._sound_sample(case, role, cue)) is not None:
                    member_id, _, (sample, _, _, _) = ref
                    fields['采样来源'] = f'YUM {member_id}'
                    fields['采样号'] = sample
                    event['audition'] = cue
            events.append(event)
        if edit is not None:
            self.ensure_open()
            before = self.snapshot()
            if pending:
                self.edits[key] = pending
            else:
                self.edits.pop(key, None)
            self.remember(before)
        return sorted(events, key=lambda event: (event['frame'], event['id']))

    def list_templates(self, role, config, shape):
        self.ensure_open()
        member = self.form_binding(role, config, shape)[2]['id']
        count, offsets, parts = self.bank(member, b'BOM\0')
        templates = [self.template_edits.get((member, i), data) for i, data in enumerate(parts[count:])]
        return {'member': member, 'items': [
            {**shape_data, 'edited': (member, i) in self.template_edits, 'offset': offsets[count + i]}
            for i in range(len(templates))
            if (shape_data := read_shape(templates, i, -1, 'body')) and 'radius' in shape_data]}

    def edit_template(self, role, config, shape, index, values):
        self.ensure_open()
        member = self.form_binding(role, config, shape)[2]['id']
        count, _, parts = self.bank(member, b'BOM\0')
        original = selected(parts[count:], index)
        kind = u32(original, 0)
        length = {1: 4, 2: 7}.get(kind)
        if length is None or len(original) < 4 + length * 4:
            raise ValueError('请选择可编辑的球或胶囊模板。')
        if values is None:
            raw = original
        else:
            if not isinstance(values, list) or len(values) != length or not all(
                    type(v) in (int, float) and -3.4e38 <= v <= 3.4e38 for v in values) or values[-1] <= 0:
                raise ValueError('模板坐标须为有限数值，半径须大于零。')
            raw = original[:4] + struct.pack(f'<{length}f', *values) + original[4 + length * 4:]
            if 'radius' not in read_shape([raw], 0, -1, 'body'):
                raise ValueError('该半径无法以原生精度存储。')
        before = self.snapshot()
        if raw == original:
            self.template_edits.pop((member, index), None)
        else:
            self.template_edits[member, index] = raw
        self.remember(before)
        return self.edit_status()

    def edit_status(self):
        moves = (sum(self._recovery.roots.get(k) != self.move_sets.get(k)
                     for k in self._recovery.roots.keys() | self.move_sets.keys()) if self.resumed else len(self.move_sets))
        return {'count': sum(len(events) for events in self.edits.values()) + len(self.move_event_edits),
                'assets': len(self.asset_edits) + len(self.move_anm_edits) + len({key[:3] for key in self.move_texture_edits}),
                'native': len(self.native_edits) + len(self.movement_rates), 'templates': len(self.template_edits),
                'notes': len(self.notes) + len(self.aliases), 'moves': moves, 'ai': len(self.ai_edits),
                'undo': bool(self.undo), 'redo': bool(self.redo),
                'unsaved': self.snapshot() != self.saved}

    def bom_content(self, member):
        data = bytearray(self.read_member(member, b'BOM\0'))
        count, offsets, _ = self.bank(member, b'BOM\0')
        for (resource, index), edit in self.template_edits.items():
            if resource == member and not (self.resumed and (resource,index) in self._recovery.shape_keys):
                start = offsets[count + index]
                data[start:start + len(edit)] = edit
        for (resource, _), events in self.edits.items():
            if resource == member:
                for event in events.values():
                    start = event.offset
                    end = start + len(event.original)
                    if data[start:end] != event.original:
                        raise ValueError('判定或帧事件与当前 BOM 不一致。')
                    data[start:end] = event.edited
        return bytes(data)

    def export(self, destination):
        patches = []
        rebuilt_boms = {member for (member, slot), data in self.asset_edits.items()
                        if slot is None and data.startswith(b'BOM\0')}
        bases = self._recovery.members if self.resumed else {}
        rebuilt_boms.update(member for member in bases if member in self.bom_models)
        for (member, _), events in self.edits.items():
            if member in rebuilt_boms:
                continue
            base = self.member_offset(member)
            patches.extend(replace(patch, offset=base + patch.offset) for patch in events.values())
        for off, data in self.native_edits.items():
            patches.append(Patch(self.eboot['offset'] + off, self.eboot_raw[off:off + len(data)], data))
        for (member, index), data in self.template_edits.items():
            if member in rebuilt_boms:
                continue
            count, offsets, parts = self.bank(member, b'BOM\0')
            patches.append(Patch(self.member_offset(member) + offsets[count + index], parts[count + index], data))
        replacements = {member:self.bom_content(member) if member in rebuilt_boms else data
                        for member,data in bases.items()}
        clips = defaultdict(dict)
        for (member, slot), data in self.asset_edits.items():
            if slot is None:
                replacements[member] = self.bom_content(member) if member in rebuilt_boms else data
            elif not (self.resumed and (member,slot) in self._recovery.clip_keys):
                clips[member][slot] = data
        for member, edits in clips.items():
            bank = self.read_source_member(member, b'ANM\0')
            if member in STORY_BANKS:
                replacements[member] = replace_story_bank(bank, edits)
            elif self.installed_moves:
                replacements[member] = replace_anm_clips(bank, edits)
            else:
                replacements[member] = replace_bank(bank, b'ANM\0', edits)
        for key, edited in self.move_texture_edits.items():
            if len(key) != 4 or self.resumed:
                continue
            bank = key[3]
            offset, size = self.installed_move_textures[key[:3]][bank]
            original = self.move_texture_data(key, original=True)
            if len(edited) != size:
                raise ValueError('镜像内配招贴图的写入长度不匹配。')
            if bank in replacements:
                data = replacements[bank]
                if data[offset:offset + size] != original:
                    raise ValueError('配招贴图与该动画库中的其他修改重叠。')
                replacements[bank] = data[:offset] + edited + data[offset + size:]
            else:
                patches.append(Patch(self.member_offset(bank) + offset, original, edited))
        eboot = None
        if self.move_sets:
            plan = Compilation(self, self.move_sets)
            eboot = plan.emit(replacements, patches)
        elif self.movement_rates or self.ai_edits or self.sound_extensions or self.pre_ai_eboot is not self.eboot_raw:
            eboot = bytearray(self.pre_ai_eboot)
            for offset, data in self.native_edits.items():
                eboot[offset:offset + len(data)] = data
            eboot = bytes(eboot)
        if eboot is not None:
            if self.ai_edits:
                eboot = install_ai(eboot, self.elf_layout, self.canonical_eboot, self.ai_scripts, self.ai_edits)
            if self.sound_extensions:
                eboot = install_sound(eboot, self.elf_layout, self.canonical_eboot, self.sound_extensions,self.sound_members)
            eboot = ground_travel.install_rates(eboot, self.elf_layout, self.canonical_eboot, len(self.roles), self.movement_rates)
            start = self.eboot['offset']
            patches = [patch for patch in patches if not start <= patch.offset < start + len(eboot)]
            patches.append(Patch(start, self.eboot_raw, eboot))
        result = save_iso(self, destination, patches, replacements)
        # Notes and aliases live in a project file.
        self.saved.update({name: getattr(self, name).copy() for name in WORKSPACE_FIELDS if name not in ('notes', 'aliases')})
        self.saved['resumed'] = self.resumed
        return {**result, 'edits': self.edit_status()}

    def motion(self, role, config, shape, action, edit=None):
        self.ensure_open()
        case, _, bom, _ = self.form_binding(role, config, shape)
        selected_move, clip = self.action_animation(role, case, bom['id'], shape, action)
        if self.move_sets and (preview := self.current_moves().preview(role, config, shape, selected_move['id'], clip)):
            return self.move_motion(role, config, shape, selected_move, clip, preview, edit)
        anm, slot, opponent = self.anim_bank(case, clip)
        count, anm_offsets, parts = self.bank(anm, b'ANM\0')
        if not 0 <= slot < count:
            raise ValueError(f'ANM {anm} 中没有片段 {slot}。')
        asset = anm, slot
        data = self.asset_edits.get(asset, parts[slot])
        animation = decode_anm(data)
        member = bom['id']
        bom_clip = selected_move.get('bom_clip', clip)
        events = self._motion_events(case, role, member, bom_clip, animation['last'], edit)
        alias = self.aliases.get((role, selected_move['id']), '')
        resolved = {**selected_move, 'clip': clip,
                    'followup': isinstance(action, list) and selected_move['id'] >= 0 and not selected_move.get('rage'),
                    'name': alias or selected_move.get('name') or self.action_title(role, selected_move), 'alias': alias,
                    'move_source': self.move_source(role, selected_move['id'])}
        result = {'action': resolved, 'anm': anm, 'slot': slot, 'opponent': opponent, 'bom': member, 'bom_clip': bom_clip,
                'animation': animation, 'events': events, 'edits': self.edit_status(),
                'asset': self.asset_info('anm', asset, len(data), (role, config, shape)),
                'anm_iso': self.member_offset(anm) + anm_offsets[slot], 'bom_iso': self.member_offset(member)}
        if edit is not None and any(event['id'] == edit[0] and (event['op'] == 3 or 'damage' in event) for event in events):
            result['moves'] = self.move_catalog(role, case, bom, shape)
        return result


    def move_motion(self, role, config, shape, selected_move, clip, preview, edit):
        move, anm, member, clip_data, records, templates = preview
        animation = decode_anm(clip_data)
        _, form, bom, _ = self.form_binding(role, config, shape)
        nodes = self.node_count(form['mdl']['id'])
        plan = self.current_moves()
        events = []
        selected_record = None
        local = 0
        for record in event_records(pack_script(records)):
            active = bool(record.raw[1] & (1 << shape))
            key = move_event_key(move, member, shape, local) if active else None
            if active:
                local += 1
            fields = move_event_words(record.raw, animation['last'], nodes, self.sound_count) if active else []
            if edit is not None and record.index == edit[0] and fields:
                selected_record = key, record.raw, fields
            entry = decode_event(record.raw, templates, self.sound_code)
            entry.update(id=record.index, offset=record.offset, edited=key in self.move_event_edits,
                         editable=[field.describe(record.raw) for field in fields],
                         raw=record.raw.hex(' '), overlap=False)
            events.append(entry)
        if edit is not None:
            if selected_record is None:
                raise ValueError('请选择配招的可编辑帧事件。')
            key, current, fields = selected_record
            original = plan.event_defaults.get(key, current)
            raw = original if edit[1] is None else change_words(current, fields, edit[1], templates)
            case = self.roles[role]['cases'][config]
            if (edit[1] is not None and raw[0] in (14,15) and raw[4:6] != current[4:6]
                    and not self.sound_in_case(case,s16(raw,4))):
                raise ValueError('这个新增音色属于其他出场配置。')
            before = self.snapshot()
            if raw == original:
                self.move_event_edits.pop(key, None)
            else:
                self.move_event_edits[key] = {str(field.offset): s16(raw, field.offset) for field in fields
                                             if s16(raw, field.offset) != s16(original, field.offset)}
            self.remember(before)
            result = self.motion(role, config, shape, [selected_move['id'], clip])
            result['moves'] = self.move_catalog(role, case, bom, shape)
            return result
        return {'action': {**selected_move, 'clip': clip, 'name': self.action_title(role, selected_move),
                           'move_source': self.move_source(role, selected_move['id'])},
                'anm': anm, 'slot': clip, 'opponent': None, 'bom': member, 'bom_clip': clip,
                'animation': animation, 'events': sorted(events, key=lambda item: (item['frame'], item['id'])),
                'edits': self.edit_status(), 'asset': self.asset_info('anm',(anm,clip)), 'anm_iso': None,
                'bom_iso': self.member_offset(member)}


def loose_animation(clip):
    return {'id': -1, 'clip': clip, 'category': 4, 'name': '伤害片段', 'extra': True,
            'va': None, 'flags': None, 'callbacks': []}


def flat(rows):
    return [value for row in rows for value in row]


def model_meshes(mdl):
    meshes = []
    triangles = 0
    for mesh in mdl['meshes']:
        if is_shadow(mdl, mesh):
            continue
        batches = {}
        seen = {}
        source = []
        attributes = []
        for material, corners in mesh['faces']:
            if mdl['materials'][material]['kind'] in (3, 8):
                continue
            target = batches.setdefault(material, [])
            for corner in corners:
                key = tuple(corner.get(field, -1) for field in ('v', 'n', 'uv', 'c'))
                if key not in seen:
                    v, n, uv, color = key
                    seen[key] = len(source)
                    source.append(v)
                    attributes.extend(mesh['normals'][n] if n >= 0 else (0, 0, 0))
                    attributes.extend(mesh['uv'][uv] if uv >= 0 else (0, 0))
                    attributes.extend(mesh['colors'][color] if color >= 0 else (1, 1, 1, 1))
                target.append(seen[key])
            triangles += 1
        if batches:
            meshes.append({'owner': mesh['owner'], 'palette': mesh['palette'], 'source': source,
                           'positions': flat(mesh['positions']), 'weights': flat(mesh['weights']),
                           'attributes': attributes, 'batches': list(batches.items())})
    return meshes, triangles
