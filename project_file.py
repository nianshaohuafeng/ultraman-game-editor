"""Keep the editor's saved settings and native asset changes in a project file."""

import hashlib
import json
from pathlib import Path
import struct
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile

from event_edits import Patch, change_words
from move_sound import validate_extension, load_sound_members
from sound_eboot import read_codes
from fe3_sound import validated as fe3_sound_codes
from formats import bom_parts, event_records, read_shape
from kgf_readers.mdl_reader import read_mdl
from asset_transfer import compatible_model, pom_layout, replace_texture
from iso_save import new_output
from move_sets import Compilation
from ai_script import load_ai_edits
from ground_travel import load_rates
from fe3_game import FE3Game, ROSTER, integer
from fe3_native import MIN_RATING_VALUE
from fe3_transfer import compile_transfers


PROJECT = '.kgfp'
PACKAGE = '.kgfpatch'


class ProjectBaseError(ValueError):
    """The candidate image could not be verified as the source of this project."""


def iso_digest(game):
    game.ensure_open()
    with game.path.open('rb') as handle:
        result = hashlib.file_digest(handle, 'sha256').hexdigest()
    game.ensure_open()
    return result


def write_project(game, path, view, portable=False, replace_existing=False):
    path = Path(path)
    if isinstance(game, FE3Game):
        return write_fe3_project(game, path, view, portable, replace_existing)
    state = game.snapshot()
    assets = []
    for index, ((member, slot), _) in enumerate(sorted(state['asset_edits'].items())):
        assets.append([member, slot, f'assets/{index}.bin'])
    animations = [[*key,f'moves/animation-{i}.anm'] for i,key in enumerate(sorted(state['move_anm_edits']))]
    move_textures = [[*key, f'moves/texture-{index}.pom']
                     for index, key in enumerate(sorted(state['move_texture_edits']))]
    manifest = {
        'format': 1, 'source': None if portable else str(game.path),
        'source_sha256': iso_digest(game), 'view': view, 'assets': assets,
        'events': [[member, clip, index, patch.edited.hex()]
                   for (member, clip), patches in state['edits'].items() for index, patch in patches.items()],
        'native': [[offset, data.hex()] for offset, data in state['native_edits'].items()],
        'templates': [[member, index, data.hex()] for (member, index), data in state['template_edits'].items()],
        'notes': list(state['notes'].items()),
        'aliases': [[role, action, value] for (role, action), value in state['aliases'].items()],
        'model_atlases': sorted(state['model_atlases'].items()),
        'move_sets': [[role, action, donor, move] for (role, action), (donor, move) in state['move_sets'].items()],
        'move_events': [[*key, words] for key, words in state['move_event_edits'].items()],
        'move_textures': move_textures, 'move_animations': animations,
        'movement_rates': [[*key, value] for key, value in sorted(state['movement_rates'].items())],
        'ai': [[*key, value] for key, value in sorted(state['ai_edits'].items())],
        'sound_extensions': [code for _,code in sorted(state['sound_extensions'].items())],
        'sound_members': sorted(state['sound_members'].items()),
    }
    if game.resumed:
        manifest['resumed_moves'] = True
        if game.resumed_shape_refs:
            manifest['resumed_shape_refs'] = [[*key,value] for key,value in sorted(game.resumed_shape_refs.items())]
    with new_output(path, PACKAGE if portable else PROJECT, replace_existing) as temp:
        with ZipFile(temp, 'w', compression=ZIP_DEFLATED) as archive:
            archive.writestr('project.json', json.dumps(manifest, ensure_ascii=False))
            for member, slot, item in assets:
                archive.writestr(item, state['asset_edits'][member, slot])
            for *key, name in animations:
                archive.writestr(name,state['move_anm_edits'][tuple(key)])
            for *key, name in move_textures:
                archive.writestr(name, state['move_texture_edits'][tuple(key)])
        with ZipFile(temp) as archive:
            if archive.testzip() is not None:
                raise ValueError('工程文件读回校验失败。')
        game.ensure_open()
    game.saved = state
    return {'name': path.name, 'path': str(path), 'edits': game.edit_status()}


def read_manifest(path):
    try:
        with ZipFile(path) as archive:
            result = json.loads(archive.read('project.json'))
        if isinstance(result, dict) and result.get('format') == 2 and result.get('game') == 'fe3':
            if all(key in result for key in ('source', 'source_sha256', 'view', 'assets', 'native')):
                return result
            raise ValueError('格斗进化3 工程记录不完整。')
        keys = ('source_sha256', 'source', 'view', 'assets', 'events', 'native', 'templates', 'notes', 'aliases')
        if not isinstance(result, dict) or result.get('format') != 1 or any(key not in result for key in keys):
            raise ValueError('工程版本不受支持。')
        return result
    except (BadZipFile, KeyError, UnicodeDecodeError):
        raise ValueError('无法读取此 KGF 工程。') from None


def parse_project(game, path, manifest):
    if isinstance(game, FE3Game) != (manifest.get('game') == 'fe3'):
        raise ProjectBaseError('工程和镜像分属不同游戏。')
    try:
        checksum = iso_digest(game)
    except (OSError, ValueError) as exc:
        raise ProjectBaseError(str(exc)) from exc
    if checksum != manifest['source_sha256']:
        raise ProjectBaseError('所选 ISO 与工程记录的基底不同。')
    if isinstance(game, FE3Game):
        parse_fe3_project(game, path, manifest)
    else:
        _parse_kgf_project(game, path, manifest)
    game.ensure_open()


def _parse_kgf_project(game, path, manifest):
    try:
        if manifest.get('resumed_moves'):
            game.resume_move_workspace()
            game.move_sets.clear()
            game.move_event_edits.clear()
        if 'movement_rates' in manifest:
            game.movement_rates = load_rates(manifest['movement_rates'], len(game.roles))
        if 'ai' in manifest:
            game.ai_edits = load_ai_edits(game.ai_scripts, manifest['ai']) if manifest['ai'] != [] else {}
        if 'sound_extensions' in manifest:
            sounds = read_codes(manifest['sound_extensions'])
            if not game.sound_extensions.items() <= sounds.items():
                raise ValueError('工程声音索引与基底镜像不一致。')
            game.sound_extensions = sounds
        voices = {v['id'] for role in game.roles for case in role['cases'] if (v := case.get('voice_bank')) and 'id' in v}
        for offset, data in manifest['native']:
            field = game.native_fields[offset]
            raw = bytes.fromhex(data)
            game.set_native(field, struct.unpack(field.fmt, raw)[0])
        for (role, action), field in game.clip_fields.items():
            if field.offset in game.native_edits and not game.valid_action_clip(role, action, game.native_value(field)):
                raise ValueError('工程里的动作片段引用无效。')
        game.refresh_actions()
        with ZipFile(path) as archive:
            for member, slot, item in manifest['assets']:
                data = archive.read(item)
                if slot is None and member in voices:
                    validate_extension(game.read_source_member(member,b'pBAV'),data)
                elif slot is None and member in game.model_forms:
                    compatible_model(game.read_source_member(member, b'MDL\0'), data)
                elif slot is None and member in game.pom_forms:
                    pom_layout(data)
                elif slot is None and (member, -1) in game.shared_resources.entries:
                    data = game.shared_resources.validate_psp((member, -1), data)
                elif slot is None and member in game.bom_models:
                    original = game.read_source_member(member, b'BOM\0')
                    count, _, old = bom_parts(original)
                    scripts, _, new = bom_parts(data)
                    if scripts != count or len(new) < len(old):
                        raise ValueError('工程 BOM 的脚本和判定模板编号无效。')
                    for index in range(len(old), len(new)):
                        geometry = read_shape(new[count:], index - count, -1, 'attack')
                        if not geometry or 'radius' not in geometry:
                            raise ValueError('工程 BOM 包含无效的新判定模板。')
                elif type(slot) is int and member in game.native_anm_members:
                    game._check_animation((member, slot), data)
                else:
                    raise ValueError('工程包含无效的资源目标。')
                game.asset_edits[member, slot] = data
        game.clear_bank_cache()
        game.sound_bank.cache_clear()
        for member, slot in game.asset_edits:
            if slot is not None:
                continue
            if member in game.model_forms:
                game._check_model_materials(member, read_mdl(game.asset_edits[member, slot]))
            elif member in game.pom_forms:
                game._check_texture_materials(member, game.asset_edits[member, slot])
        paired = set()
        for model, textures in manifest.get('model_atlases', []):
            textures = [textures] if type(textures) is int else textures
            if (not isinstance(textures, list) or not textures
                    or any(type(item) is not int for item in textures)
                    or model not in game.model_forms or model in game.model_atlases
                    or (model,None) not in game.asset_edits or set(textures) & paired
                    or len(set(textures)) != len(textures)
                    or any((item,None) not in game.asset_edits or item not in game.pom_forms for item in textures)
                    or any(not set(textures) <= {item['id'] for item in game.form_binding(*form)[3]}
                           for form in game.model_forms[model])):
                raise ValueError('工程里的转换模型与贴图槽不一致。')
            paired.update(textures)
            game.model_atlases[model] = tuple(textures)
        for member, index, encoded in manifest['templates']:
            if member not in game.bom_models:
                raise ValueError('工程的判定模板目标无效。')
            count, _, parts = game.bank(member, b'BOM\0')
            inherited = game.resumed and (member,index) in game._recovery.shape_keys
            if inherited:
                count, _, parts = bom_parts(game.read_image_member(member,b'BOM\0'))
            if type(index) is not int or not 0 <= index < len(parts) - count:
                raise ValueError('工程的判定模板编号无效。')
            original = parts[count + index]
            raw = bytes.fromhex(encoded)
            geometry = read_shape([raw], 0, -1, 'body')
            length = {1: 20, 2: 32}.get(geometry['type']) if geometry else None
            if not length or 'radius' not in geometry or len(raw) != len(original) or raw[:4] != original[:4] or raw[length:] != original[length:]:
                raise ValueError('工程的判定模板数据无效。')
            game.template_edits[member, index] = raw
        for member, clip, index, encoded in manifest['events']:
            if member not in game.bom_models:
                raise ValueError('工程的 BOM 事件目标无效。')
            count, offsets, parts = game.bank(member, b'BOM\0')
            if type(clip) is not int or not 0 <= clip < count:
                raise ValueError('工程的 BOM 脚本编号无效。')
            records = {r.index: r for r in event_records(parts[clip])}
            record = records[index]
            raw = bytes.fromhex(encoded)
            fields = game._event_words(record, member, 32767, len(parts) - count)
            updates = {str(f.offset): struct.unpack_from('<h', raw, f.offset)[0] for f in fields}
            if not fields or change_words(record.raw, fields, updates, parts[count:]) != raw:
                raise ValueError('工程包含超出可编辑字段的 BOM 事件。')
            game.edits.setdefault((member, clip), {})[index] = Patch(offsets[clip] + record.offset, record.raw, raw)
        for role, value in manifest['notes']:
            game.edit_text(role, None, value)
        for role, action, value in manifest['aliases']:
            game.edit_text(role, action, value)
        for role, action, donor, move in manifest.get('move_sets', []):
            if (game.installed_moves or any(type(num) is not int for num in (role, action, donor, move))
                    or not 0 <= role < len(game.roles) or not 0 <= donor < len(game.roles)
                    or (role, action) in game.move_sets):
                raise ValueError('工程的配招记录无效或重复。')
            game.move_sets[role, action] = donor, move
        for *key, words in manifest.get('move_events', []):
            if len(key) != 8 or any(type(value) is not int or value < 0 for value in key) or not isinstance(words, dict):
                raise ValueError('工程的配招事件记录无效。')
            if tuple(key[:2]) not in game.move_sets:
                raise ValueError('工程的配招事件缺少对应的动作。')
            game.move_event_edits[tuple(key)] = words
        if 'resumed_shape_refs' in manifest:
            if not game.resumed:
                raise ValueError('工程的镜像判定引用缺少配招基底。')
            valid = {key for events in game._recovery.shape_events.values() for key in events}
            for *fields, index in manifest['resumed_shape_refs']:
                key = tuple(fields)
                if key not in valid or key[:2] not in game.move_sets or type(index) is not int:
                    raise ValueError('工程的独立判定引用无效。')
                game.resumed_shape_refs[key] = index
        with ZipFile(path) as archive:
            for *fields, name in manifest.get('move_animations', []):
                key = tuple(fields)
                if len(key) != 6 or any(type(x) is not int for x in key) or key in game.move_anm_edits:
                    raise ValueError('工程配招动画索引无效。')
                game.move_anm_edits[key] = archive.read(name)
            for *fields, name in manifest.get('move_textures', []):
                key = tuple(fields)
                if (len(key) not in (3, 4) or any(type(n) is not int for n in key)
                        or key in game.move_texture_edits or not 0 <= key[2] < len(game.yum['files'])):
                    raise ValueError('工程的配招贴图缺少对应的动作或资源。')
                data = archive.read(name)
                if len(key) == 4:
                    location = game.installed_move_textures.get(key[:3], {}).get(key[3])
                    if location is None or len(data) != location[1]:
                        raise ValueError('工程内的配招贴图与基底镜像的出场配置不匹配。')
                    pom_layout(data)
                elif key[:2] in game.move_sets:
                    original = game.move_texture_data(key, True)
                    data = replace_texture(original, original, data, '.pom', 0)
                else:
                    raise ValueError('工程的配招贴图缺少对应的动作。')
                game.move_texture_edits[key] = data
        game.sound_members = load_sound_members(game,manifest.get('sound_members',()),game.sound_members)
        game.move_preview = Compilation(game, game.move_sets)
        if set(game.move_anm_edits) - set(game.move_preview.animation_keys.values()):
            raise ValueError('工程配招动画超出当前动作范围。')
        if set(game.move_event_edits) - game.move_preview.event_defaults.keys():
            raise ValueError('工程的配招事件记录超出当前动作范围。')
        textures = {key for entries in game.move_preview.texture_keys.values() for key in entries}
        if any(len(key) == 3 and key not in textures for key in game.move_texture_edits):
            raise ValueError('工程的配招贴图记录超出当前动作范围。')
    except (BadZipFile, KeyError, TypeError, IndexError, struct.error) as exc:
        raise ValueError('工程数据不完整或编号无效。') from exc
    game.undo.clear()
    game.redo.clear()
    game.saved = game.snapshot()


def write_fe3_project(game, path, view, portable, replace_existing):
    state = game.snapshot()
    assets = [[*key, f'fe3/{index}.bin'] for index, key in enumerate(sorted(state['assets']))]
    document = {'format':2, 'game':'fe3', 'source':None if portable else str(game.path),
                'source_sha256':iso_digest(game), 'view':view, 'assets':assets, 'native':list(state['native'].items()),
                'skills':[[*key, values] for key, values in sorted(state['skills'].items())],
                'ratings':[[*key, value] for key, value in sorted(state['ratings'].items())],
                'throws':[[*key, anm] for key, anm in sorted(state['throws'].items())],
                'bindings':sorted(state['bindings'].items()), 'jump':sorted(state['jump'].items()),
                'charge_frames':game.charge()['frames'] if state['charge'] else None,
                'additions':[[*key,*source] for key,source in sorted(state['additions'].items())],
                'sound_extensions':[value for _,value in sorted(state['sounds'].items())],
                'transfers':[[*key,*source] for key,source in sorted(state['transfers'].items())]}
    with new_output(path, PACKAGE if portable else PROJECT, replace_existing) as temp:
        with ZipFile(temp, 'w', compression=ZIP_DEFLATED) as archive:
            archive.writestr('project.json', json.dumps(document, ensure_ascii=False))
            for resource, member, name in assets:
                archive.writestr(name, state['assets'][resource, member])
        with ZipFile(temp) as archive:
            if archive.testzip() is not None:
                raise ValueError('格斗进化3 工程读回校验失败。')
        game.ensure_open()
    game.saved = state
    return {'name':path.name, 'path':str(path), 'edits':game.edit_status()}


def parse_fe3_project(game, path, manifest):
    assets, native, skills, throws, bindings, jump, transfers, additions = {}, {}, {}, {}, {}, {}, {}, {}
    ratings = {}
    charge = game.charge_changes(manifest.get('charge_frames'))
    try:
        valid_defense = {game.form_defense_at + 6 * role + 2 * row['shape']
                         for role in range(len(ROSTER)) for row in game.defense(role)}
        for offset, value in manifest['native']:
            if (type(offset) is not int or offset % 2 or offset in native
                or (not game.hp_at <= offset < game.hp_at + 18 * len(ROSTER) and offset not in valid_defense)):
                raise ValueError('工程承伤表记录无效或重复。')
            native[offset] = integer(value, 0, 32767, 'Q7 承伤值')
        for role, state, anm in manifest.get('throws', []):
            key, value = game.throw_change(role, state, anm)
            if value is None or key in throws:
                raise ValueError('工程抓取阶段记录无效或重复。')
            throws[key] = value
        incoming = {}
        for address, raw in manifest.get('bindings', []):
            if type(address) is not int or address not in game.binding_locations or address in incoming:
                raise ValueError('工程形态绑定地址无效或重复。')
            incoming[address] = integer(raw, 0, 0xFFFFFFFF, '形态绑定值')
        for (role, slot), locations in game.binding_rows.items():
            if any(address in incoming for _,address in locations):
                words = [incoming.get(address, struct.unpack_from('<I', game.elf, game.binding_locations[address])[0])
                         for _,address in locations]
                bindings = game.binding_change(role, slot, words, bindings, validate=False)
        game.validate_bindings(bindings, bindings.keys())
        game.binding_edits = bindings
        for role, candidate, donor, move in manifest.get('transfers', []):
            key = role, candidate
            if any(type(v) is not int for v in (role,candidate,donor,move)) or key in transfers:
                raise ValueError('工程必杀配招无效或重复。')
            transfers[key] = donor, move
        for role,candidate,donor,move,shape in manifest.get('additions',[]):
            key = role,candidate
            if any(type(value) is not int for value in (role,candidate,donor,move,shape)) or key in additions:
                raise ValueError('工程新增必杀技记录无效或重复。')
            additions[key] = donor,move,shape
        game.transfers,game.additions = transfers,additions
        game.transfer_plan = compile_transfers(game,transfers,additions)
        if 'sound_extensions' in manifest:
            sounds = fe3_sound_codes(manifest['sound_extensions'])
            if not game.sound_extensions.items() <= sounds.items():
                raise ValueError('奥3工程声音索引与基底不一致。')
            game.sound_extensions = sounds
        for role, candidate, values in manifest.get('skills', []):
            key, value = game.skill_change(role, candidate, values)
            if value is None or key in skills:
                raise ValueError('工程技能记录无效或重复。')
            skills[key] = value
        for role, candidate, value in manifest.get('ratings', []):
            game.base_skill_rating(role, candidate)
            key = role, candidate
            if key in ratings:
                raise ValueError('工程必杀评级记录重复。')
            ratings[key] = integer(value, MIN_RATING_VALUE, 0x7FFFFFFF, '必杀评级基础值')
        with ZipFile(path) as archive:
            for resource, member, name in manifest['assets']:
                part = game.part(resource, member)
                if part.key in assets:
                    raise ValueError('工程资源记录重复。')
                record = archive.getinfo(name)
                baseline = game.transfer_plan.resources.get(part.key)
                expected = len(baseline) if baseline is not None else part.expanded or part.size
                if part.key in game.fighter_sound_parts:
                    assets[part.key] = archive.read(record)
                    continue
                if part.key in game.stream_parts:
                    if record.file_size < 24:
                        raise ValueError('工程的 PS2 音轨被截断。')
                elif part.kind == 'anm':
                    if not (0 < record.file_size <= part.span or
                            record.file_size > 0 and game.allows_growth(part.key)):
                        raise ValueError('工程 ANM 超过原生读取范围。')
                elif part.kind != 'bom' and record.file_size != expected:
                    raise ValueError('工程资源长度与镜像不同。')
                assets[part.key] = game.validate_import(part, archive.read(record))
        for head,body in game.fighter_sound_pairs.values():
            if (head,-1) in assets or (body,-1) in assets:
                old = game.base_data((head,-1)) + game.base_data((body,-1))
                new = assets.get((head,-1),game.base_data((head,-1))) + assets.get((body,-1),game.base_data((body,-1)))
                validate_extension(old,new)
        for role, values in manifest.get('jump', []):
            result = game.jump_change(role, values)
            if result is None or role in jump:
                raise ValueError('工程跳跃参数无效或重复。')
            jump[role] = result
    except (BadZipFile, KeyError, TypeError, IndexError, struct.error) as exc:
        raise ValueError('格斗进化3 工程数据不完整。') from exc
    game.asset_edits, game.native_edits, game.skill_edits = assets, native, skills
    game.rating_edits = ratings
    game.validate_transfer_capacity(game.transfer_plan)
    game.throw_edits = throws
    game.jump_edits = jump
    game.charge_edits = charge
    game.undo.clear()
    game.redo.clear()
    game.saved = game.snapshot()
