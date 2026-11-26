"""Desktop resource and timeline editor for KGF v7 and PS2 FE3."""

import argparse
import base64
import json
import os
from pathlib import Path
import struct
import subprocess
from threading import Event, RLock
from zlib import error as PngError

from assets import Game, HERE
from asset_transfer import write_asset
from iso_save import new_output
from project_file import PACKAGE, PROJECT, ProjectBaseError, parse_project, read_manifest, write_project
from fe3_game import FE3Game, open_game
from fe3_audio import audio as fe3_audio, library as fe3_library
from fe3_movie import movie_preview
from kgf_scene import battle_preview, camera_options
from cross_model import convert_model
from cross_move import cross_choices, cross_forms, is_fe3, transfer_move
from bulk_motion import MotionSet
from move_sound import SoundLibrary, wav_voice
from audio_preview import vag_wave
from shared_resources import data_uri


READ_ERRORS = (OSError, ValueError, IndexError, struct.error, PngError)
ASSET_FILTERS = {
    'mdl': 'PSP 模型 (*.mdl)', 'pom': 'PSP 贴图 (*.pom)',
    'anm': 'PSP 动作片段 (*.anm)', 'png': 'PNG 图像 (*.png)',
}


class ViewerApi:
    def __init__(self, initial_path=None, session_path=None):
        self._initial_path = initial_path
        self._session_path = session_path or (
            Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData/Local')
            / '奥格资源编辑器融合版本/session.json'
        )
        try:
            preference = self._session_path
            if session_path is None and not preference.exists():
                preference = preference.parent.parent / 'KGF v7 修改器/session.json'
            session = json.loads(preference.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            session = {}
        self._session = session if isinstance(session, dict) else {}
        self._session_revision = 0
        self._window = None
        self._game = None
        self._cross_game = None
        self._source_request = 0
        self._voice_files = {}
        self._voice_request = 0
        self._serial = 0
        self._open_request = 0
        self._pending_open = None
        self._asset_directory = None
        self._opened_project = None
        self._project_path = None
        self._music_decode = None
        self._lock = RLock()

    def _install(self, path):
        return self._accept(open_game(path))

    def _accept(self, game, project=None, view=None):
        catalog = game.catalog()
        if self._music_decode:
            self._music_decode.set()
        self._game = game
        self._cross_game = None
        self._voice_files.clear()
        self._serial += 1
        self._pending_open = None
        self._opened_project = project
        self._project_path = project if project and project.suffix.lower() == PROJECT else None
        return {'id': self._serial, **catalog, 'view': view}

    def initialize(self):
        with self._lock:
            result = {'document': None, 'display': self._session.get('display', {})}
            try:
                if self._game:
                    result['document'] = {'id': self._serial, **self._game.catalog(), 'view': self._session.get('view')}
                else:
                    last_iso = self._session.get('iso')
                    if self._initial_path is not None:
                        result['document'] = self._install(self._initial_path)
                    elif isinstance(last_iso, str) and last_iso:
                        game = open_game(last_iso)
                        saved_project = self._session.get('project')
                        project = Path(saved_project) if isinstance(saved_project, str) and saved_project else None
                        view = self._session.get('view')
                        if project:
                            manifest = read_manifest(project)
                            parse_project(game, project, manifest)
                            if not isinstance(view, dict):
                                view = manifest['view']
                        result['document'] = self._accept(game, project, view)
            except READ_ERRORS as exc:
                result['error'] = ('上次打开的文件无法恢复 · ' if self._initial_path is None else '') + str(exc)
            return {'value': result}

    def remember_session(self, serial, revision, view, display):
        with self._lock:
            if serial != (self._serial if self._game else None) or revision <= self._session_revision:
                return {'value': None}
            session = {**self._session, 'display': display}
            if self._game and view is not None:
                session.update(iso=str(self._game.path), view=view,
                               project=str(self._opened_project) if self._opened_project else None)
            if session != self._session:
                try:
                    self._session_path.parent.mkdir(parents=True, exist_ok=True)
                    with new_output(self._session_path, '.json', replace_existing=True) as temp:
                        temp.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding='utf-8')
                except (OSError, ValueError) as exc:
                    return {'error': str(exc)}
                self._session = session
            self._session_revision = revision
            return {'value': True}

    def _dialog_directory(self, project=False):
        path = self._opened_project if project else None
        if path is None:
            saved = self._session.get('iso')
            path = self._game.path if self._game else self._initial_path or (
                Path(saved) if isinstance(saved, str) and saved else None)
        return path.parent if path and path.parent.is_dir() else Path.cwd()

    def open_iso(self, unapplied=False):
        return self._choose_document(False, unapplied)

    def open_project(self, unapplied=False):
        return self._choose_document(True, unapplied)

    def _choose_document(self, project, unapplied):
        import webview

        with self._lock:
            self._open_request += 1
            request, serial = self._open_request, self._serial
            self._pending_open = None
            directory = self._dialog_directory(project)
        filters = ('资源工程 (*.kgfp)', '资源改动包 (*.kgfpatch)') if project else ('游戏 ISO (*.iso)',)
        try:
            chosen = self._window.create_file_dialog(webview.FileDialog.OPEN, directory=str(directory),
                                                     allow_multiple=False, file_types=filters)
            if not chosen:
                return {'value': None}
            path = Path(chosen[0]).resolve(strict=True)
            with self._lock:
                if request != self._open_request or serial != self._serial:
                    return {'value': None}
                snapshot = self._game.snapshot() if self._game else None
                self._pending_open = request, serial, path, project, snapshot
                if self._game and (self._game.edit_status()['unsaved'] or unapplied):
                    return {'value': {'confirmOpen': request, 'name': path.name}}
            return self.finish_open(request, True)
        except READ_ERRORS as exc:
            return {'error': str(exc)}

    def finish_open(self, request, discard):
        with self._lock:
            pending = self._pending_open
            if pending is None or request != pending[0]:
                return {'value': None}
            _, serial, path, project, snapshot = pending
            self._pending_open = None
            if discard is not True or serial != self._serial:
                return {'value': None}
            sources = [self._game.path if self._game else None, self._initial_path, self._session.get('iso')]
        try:
            if project:
                game, view = self._read_project(path, sources)
                if game is None:
                    return {'value': None}
            else:
                game, view = open_game(path), None
            with self._lock:
                if request != self._open_request or serial != self._serial:
                    return {'value': None}
                if self._game and snapshot != self._game.snapshot():
                    return {'error': '打开文件期间当前工程发生修改，请重新选择。'}
                return {'value': self._accept(game, path if project else None, view)}
        except READ_ERRORS as exc:
            return {'error': str(exc)}

    def _call(self, serial, operation, *selection):
        with self._lock:
            if self._game is None or serial != self._serial:
                return {'error': '请从当前打开的 ISO 重新选择角色。'}
            try:
                return {'value': operation(self._game, *selection)}
            except READ_ERRORS as exc:
                return {'error': str(exc)}

    def open_move_source(self, serial, current=False):
        with self._lock:
            if not self._game or serial != self._serial:
                return {'error': '请从当前打开的 ISO 重新选择角色。'}
            self._source_request += 1
            request = self._source_request
            game, directory = self._game, self._dialog_directory()
        try:
            if not current:
                import webview

                chosen = self._window.create_file_dialog(webview.FileDialog.OPEN,
                    directory=str(directory), allow_multiple=False,
                    file_types=('游戏 ISO (*.iso)',))
                if not chosen:
                    return {'value': None}
                source = open_game(chosen[0])
            else:
                source = game
            with self._lock:
                if serial != self._serial or request != self._source_request:
                    return {'value': None}
                catalog = source.catalog()
                self._cross_game = source
                return {'value': {'name':catalog['name'], 'fe3':is_fe3(source),
                                  'roles':[{'id':i, 'name':role['name']} for i, role in enumerate(catalog['roles'])]}}
        except READ_ERRORS as exc:
            with self._lock:
                if serial != self._serial or request != self._source_request:
                    return {'value': None}
            return {'error': str(exc)}

    def move_source_forms(self, serial, role):
        return self._call(serial, lambda _: cross_forms(self._move_source(), role))

    def _voice_choices(self, game, source, receiver, donor):
        return {'source':SoundLibrary(source,*donor[:2]).voices(),
                'target':SoundLibrary(game,*receiver[:2]).voices()}

    def move_voice_choices(self, serial, receiver, donor):
        return self._call(serial,lambda game:self._voice_choices(game,self._move_source(),receiver,donor))

    def move_exchange_choices(self, serial, receiver, donor, mode):
        def list_choices(game):
            source = self._move_source()
            return {'receiver':cross_choices(game, *receiver, mode, True),
                    'donor':cross_choices(source, *donor, mode, False),
                    'voices':self._voice_choices(game,source,receiver,donor)}

        return self._call(serial, list_choices)

    def motion_set_choices(self, serial, receiver, donor):
        return self._call(serial, lambda game: MotionSet(game,self._move_source(),receiver,donor).listing())

    def apply_motion_set(self, serial, receiver, donor, overrides):
        def apply(game):
            kit = MotionSet(game,self._move_source(),receiver,donor)
            result = kit.apply(overrides)
            result['name'] = f"替换 {result['changed']} 个片段，原已相同 {result['same']} 个，保留 {result['kept']} 个"
            return result
        return self._call(serial, apply)

    def listen_move_voice(self, serial, receiver, donor, choice):
        def listen(game):
            if choice.get('side') == 'file':
                sample = self._recording(choice)
                return 'data:audio/wav;base64,'+base64.b64encode(vag_wave(sample[2],sample[3])).decode('ascii')
            if choice['side'] == 'source':
                game,actor = self._move_source(),donor
            else:
                actor = receiver
            library = SoundLibrary(game,*actor[:2])
            if choice['cue'] not in library.voices():
                raise ValueError('请选择当前音库的一条战吼。')
            return library.listen(choice['cue'])
        return self._call(serial,listen)

    def _recording(self, choice):
        sample = self._voice_files.get(choice.get('token'))
        if sample is None:
            raise ValueError('请重新选择这段 WAV。')
        return sample

    def release_move_voice(self, serial, token):
        with self._lock:
            if serial == self._serial:
                self._voice_files.pop(token,None)
            return {'value':None}

    def pick_move_voice(self, serial, receiver):
        import webview
        with self._lock:
            if serial != self._serial or self._game is None:
                return {'error':'请选择当前镜像中的角色。'}
            self._voice_request += 1
            request = self._voice_request
            library = SoundLibrary(self._game,*receiver[:2])
            sample = library.recording_template()
            if sample is None:
                return {'error':'此角色配置没有可作为录音模板的音色。'}
        try:
            paths = self._window.create_file_dialog(webview.FileDialog.OPEN,allow_multiple=False,
                                                    file_types=('PCM 录音 (*.wav)',))
            if not paths:
                return {'value':None}
            recording = wav_voice(paths[0],sample)
            with self._lock:
                if serial != self._serial or request != self._voice_request:
                    return {'value':None}
                self._voice_files[request] = recording
                return {'value':{'name':Path(paths[0]).name,'token':request}}
        except READ_ERRORS as exc:
            return {'error':str(exc)}

    def exchange_move(self, serial, receiver, donor, mode, voice='source', voice_only=False):
        def apply_exchange(game):
            source = self._move_source()
            if mode == 'model':
                return convert_model(game, source, receiver, donor)
            selected_voice = voice
            if isinstance(voice,dict) and voice.get('side') == 'file':
                selected_voice = {'recording':self._recording(voice)}
            elif isinstance(voice,dict) and voice.get('side') not in ('source','target'):
                raise ValueError('请选择已有战吼或本地 WAV。')
            return transfer_move(game, source, receiver, donor, mode, selected_voice, voice_only)

        return self._call(serial, apply_exchange)

    def _move_source(self):
        if self._cross_game is None:
            raise ValueError('请先选择配招来源镜像。')
        self._cross_game.ensure_open()
        return self._cross_game

    def read_model(self, serial, role, config, shape):
        return self._call(serial, Game.model, role, config, shape)

    def read_item_model(self, serial, role, config, shape, item):
        return self._call(serial, Game.item_model, role, config, shape, item)

    def read_motion(self, serial, role, config, shape, action):
        return self._call(serial, Game.motion, role, config, shape, action)

    def edit_event(self, serial, role, config, shape, action, event_id, updates):
        return self._call(serial, Game.motion, role, config, shape, action, (event_id, updates))

    def read_battle(self, serial, role, config, shape, action, opponent, camera):
        return self._call(serial, battle_preview, role, config, shape, action, opponent, camera)

    def read_battle_cameras(self, serial, role, config, action):
        return self._call(serial, camera_options, role, config, action)

    def read_sound(self, serial, role, config, cue):
        return self._call(serial, Game.sound_preview, role, config, cue)

    def read_texture(self, serial, role, config, shape, slot, palette):
        return self._call(serial, Game.texture_preview, role, config, shape, slot, palette)

    def read_attributes(self, serial, role):
        return self._call(serial, Game.actor_attributes, role)

    def edit_attributes(self, serial, role, band, values):
        return self._call(serial, Game.edit_attributes, role, band, values)

    def edit_energy(self, serial, role, values):
        return self._call(serial, Game.edit_energy, role, values)

    def edit_action_clip(self, serial, role, action, clip):
        return self._call(serial, Game.edit_action_clip, role, action, clip)

    def read_move_sets(self, serial, role, donor):
        return self._call(serial, Game.move_choices, role, donor)

    def edit_move_set(self, serial, role, state, donor=None, original=None):
        return self._call(serial, Game.edit_move_set, role, state, donor, original)

    def read_commands(self, serial):
        return self._call(serial, Game.commands)

    def read_ai(self, serial, role, difficulty, group):
        return self._call(serial, Game.read_ai, role, difficulty, group)

    def edit_ai(self, serial, role, difficulty, updates):
        return self._call(serial, Game.edit_ai, role, difficulty, updates)

    def read_transitions(self, serial, role, action):
        return self._call(serial, Game.transitions, role, action)

    def read_keys(self, serial, role, config, shape, action, track, channel):
        return self._call(serial, Game.keyframes, role, config, shape, action, track, channel)

    def edit_keys(self, serial, role, config, shape, action, track, channel, rows):
        return self._call(serial, Game.edit_keys, role, config, shape, action, track, channel, rows)

    def edit_timing(self, serial, role, config, shape, action, frames, rate, horizontal=1.0):
        return self._call(serial, Game.edit_timing, role, config, shape, action, frames, rate, horizontal)

    def read_templates(self, serial, role, config, shape):
        return self._call(serial, Game.list_templates, role, config, shape)

    def edit_template(self, serial, role, config, shape, index, values):
        return self._call(serial, Game.edit_template, role, config, shape, index, values)

    def edit_text(self, serial, role, action, value):
        return self._call(serial, Game.edit_text, role, action, value)

    def history(self, serial, redo=False):
        return self._call(serial, lambda game: game.step_history(redo))

    def _fe3_call(self, serial, method, *args):
        def operation(game):
            if not isinstance(game, FE3Game):
                raise ValueError('请先打开格斗进化3 ISO。')
            return method(game, *args)

        return self._call(serial, operation)

    def fe3_parts(self, serial):
        return self._fe3_call(serial, FE3Game.list_parts)

    def fe3_profile(self, serial, role, shape):
        return self._fe3_call(serial, lambda game: game.workbench.profile(role, shape))

    def fe3_model(self, serial, role, shape):
        return self._fe3_call(serial, lambda game: game.workbench.model(role, shape))

    def fe3_animation(self, serial, role, shape, clip):
        return self._fe3_call(serial, lambda game: game.workbench.animation(role, shape, clip))

    def fe3_music(self, serial, role):
        return self._fe3_call(serial, fe3_library, role)

    def fe3_listen(self, serial, track):
        def play(game, cancel):
            if not isinstance(game, FE3Game):
                raise ValueError('请先打开格斗进化3 ISO。')
            return fe3_audio(game, track, cancel=cancel)
        return self._listen(serial, play)

    def _listen(self, serial, read):
        with self._lock:
            if self._game is None or serial != self._serial:
                return {'error': '请先打开要试听的 ISO。'}
            if self._music_decode:
                self._music_decode.set()
            game = self._game
            cancel = self._music_decode = Event()
        try:
            game.ensure_open()
            data = read(game, cancel)
            return {'value': data_uri('audio/mpeg', data) if data is not None and not cancel.is_set() else None}
        except READ_ERRORS as exc:
            return {'error':str(exc)}
        finally:
            with self._lock:
                if self._music_decode is cancel:
                    self._music_decode = None

    def fe3_stop_music(self, serial):
        return self.stop_music(serial)

    def stop_music(self, serial):
        with self._lock:
            if serial == self._serial and self._music_decode:
                self._music_decode.set()
        return {'value':True}

    def fe3_export_music(self, serial, track):
        def run(game, index):
            import webview
            chosen = self._window.create_file_dialog(webview.FileDialog.SAVE, directory=str(game.path.parent),
                save_filename=f'FE3_BGM_{index:03d}.wav', file_types=('WAV 音频 (*.wav)',))
            if not chosen:
                return None
            result = write_asset(chosen[0], '.wav', fe3_audio(game, index, True))
            return {'name':result['name'], 'edits':game.edit_status()}
        return self._fe3_call(serial, run, track)

    def fe3_details(self, serial, resource, member):
        return self._fe3_call(serial, FE3Game.details, resource, member)

    def fe3_texture(self, serial, resource, member, palette):
        return self._fe3_call(serial, FE3Game.texture, resource, member, palette)

    def fe3_events(self, serial, resource, member, script):
        return self._fe3_call(serial, FE3Game.events, resource, member, script)

    def fe3_attributes(self, serial, role):
        return self._fe3_call(serial, FE3Game.attributes, role)

    def fe3_defense(self, serial, role):
        return self._fe3_call(serial, FE3Game.defense, role)

    def fe3_binding(self, serial, role, shape):
        return self._fe3_call(serial, FE3Game.get_binding_rows, role, shape)

    def fe3_edit_binding(self, serial, role, shape, values):
        return self._fe3_call(serial, FE3Game.edit_binding, role, shape, values)

    def fe3_edit_defense(self, serial, role, shape, value):
        return self._fe3_call(serial, FE3Game.edit_defense, role, shape, value)

    def fe3_jump(self, serial, role):
        return self._fe3_call(serial, FE3Game.jump, role)

    def fe3_charge(self, serial):
        return self._fe3_call(serial, FE3Game.charge)

    def fe3_edit_charge(self, serial, values):
        return self._fe3_call(serial, FE3Game.edit_charge, values)

    def fe3_edit_jump(self, serial, role, values):
        return self._fe3_call(serial, FE3Game.edit_jump, role, values)

    def fe3_edit_attributes(self, serial, role, band, value):
        return self._fe3_call(serial, FE3Game.edit_attributes, role, band, value)

    def fe3_edit_skill(self, serial, role, candidate, values):
        return self._fe3_call(serial, FE3Game.edit_skill, role, candidate, values)

    def fe3_edit_support_skill(self, serial, role, candidate, value):
        return self._fe3_call(serial, FE3Game.edit_support_skill, role, candidate, value)

    def fe3_support_sources(self, serial, role, candidate, donor, kind=None, shape=None):
        from fe3_support import source_choices
        return self._fe3_call(serial, source_choices, role, candidate, donor, kind, shape)

    def fe3_transfer_support_skill(self, serial, role, candidate, donor=None, donor_candidate=None):
        return self._fe3_call(serial, FE3Game.transfer_support_skill, role, candidate, donor, donor_candidate)

    def fe3_add_support_skill(self, serial, role, shape, donor, donor_candidate):
        return self._fe3_call(serial, FE3Game.add_support_skill,role,shape,donor,donor_candidate)

    def fe3_remove_support_skill(self, serial, role, candidate):
        return self._fe3_call(serial, FE3Game.remove_support_skill,role,candidate)

    def fe3_edit_skill_rating(self, serial, role, candidate, grade):
        return self._fe3_call(serial, FE3Game.edit_skill_rating, role, candidate, grade)

    def fe3_skills(self, serial, role):
        return self._fe3_call(serial, lambda game: game.workbench.skills(role))

    def fe3_copy_finisher_settings(self, serial, role, candidate, donor, donor_candidate):
        return self._fe3_call(serial, FE3Game.copy_finisher_settings, role, candidate, donor, donor_candidate)

    def fe3_transfer_finisher(self, serial, role, candidate, donor=None, donor_candidate=None):
        return self._fe3_call(serial, FE3Game.transfer_finisher, role, candidate, donor, donor_candidate)

    def fe3_add_finisher(self, serial, role, shape, donor, donor_candidate):
        return self._fe3_call(serial, FE3Game.add_finisher,role,shape,donor,donor_candidate)

    def fe3_remove_finisher(self, serial, role, candidate):
        return self._fe3_call(serial, FE3Game.remove_finisher,role,candidate)

    def fe3_battle_preview(self, serial, role, shape, kind, number, opponent, donor=None, donor_candidate=None, camera=-1):
        return self._fe3_call(serial, lambda game: game.workbench.battle_preview(
            role, shape, kind, number, opponent, donor, donor_candidate, camera))

    def fe3_scene_movie(self, serial, number):
        with self._lock:
            game = self._game
            if not isinstance(game, FE3Game) or serial != self._serial:
                return {'error': '请从当前打开的 ISO 重新选择影片。'}
        try:
            game.ensure_open()
            result = movie_preview(game, number)
            game.ensure_open()
            return {'value':result}
        except (*READ_ERRORS, subprocess.TimeoutExpired) as exc:
            return {'error':str(exc)}

    def fe3_throws(self, serial, role, opponent):
        return self._fe3_call(serial, lambda game: game.workbench.throw_rows(role, opponent))

    def fe3_throw_view(self, serial, role, shape, opponent):
        return self._fe3_call(serial, lambda game: game.workbench.throw_view(role, shape, opponent))

    def fe3_export_throws(self, serial, role, shape, opponent):
        def run(game):
            import webview
            records = game.workbench.throw_view(role, shape, opponent)
            names = self._window.create_file_dialog(
                webview.FileDialog.SAVE, directory=str(self._asset_directory or game.path.parent),
                save_filename=f'FE3_抓取分析_{role:02d}_{opponent:02d}.json', file_types=('JSON (*.json)',))
            if not names:
                return None
            result = write_asset(names[0], '.json', json.dumps(
                {'role':role,'shape':shape,'opponent':opponent,**records},ensure_ascii=False,indent=2).encode('utf-8'))
            return {'name':result['name'], 'edits':game.edit_status()}
        return self._fe3_call(serial, run)

    def fe3_edit_throw(self, serial, role, state, animation):
        return self._fe3_call(serial, FE3Game.edit_throw, role, state, animation)

    def fe3_input_rules(self, serial):
        return self._fe3_call(serial, lambda game: list(game.workbench.rules.values()))

    def fe3_edit_combo(self, serial, resource, member, clip, index, values):
        return self._fe3_call(serial, FE3Game.edit_combo, resource, member, clip, index, values)

    def fe3_batch(self, serial, role, shape, mode, scope, factor):
        return self._fe3_call(serial, FE3Game.batch, role, shape, mode, scope, factor)

    def fe3_edit_event(self, serial, resource, member, script, event, frame, damage):
        return self._fe3_call(serial, FE3Game.edit_event, resource, member, script, event, frame, damage)

    def fe3_edit_event_record(self, serial, resource, member, script, event, change):
        return self._fe3_call(serial, FE3Game.edit_event_record, resource, member, script, event, change)

    def fe3_edit_clip(self, serial, resource, member, clip, frames, first, last, rate):
        return self._fe3_call(serial, FE3Game.edit_clip, resource, member, clip, frames, first, last, rate)

    def fe3_keys(self, serial, resource, member, clip, track, channel):
        return self._fe3_call(serial, FE3Game.keyframes, resource, member, clip, track, channel)

    def fe3_edit_key(self, serial, resource, member, clip, track, channel, row, frame, values):
        return self._fe3_call(serial, FE3Game.edit_key, resource, member, clip, track, channel, row, frame, values)

    def fe3_edit_key_rows(self, serial, resource, member, clip, track, channel, rows):
        return self._fe3_call(serial, FE3Game.edit_key_rows, resource, member, clip, track, channel, rows)

    def fe3_clip_file(self, serial, resource, member, clip, mode):
        def run(game):
            import webview
            directory = str(self._asset_directory or game.path.parent)
            filters = ('PS2 ANM (*.anm)',)
            if mode == 'export':
                data = game.clip_export(resource, member, clip)
                names = self._window.create_file_dialog(
                    webview.FileDialog.SAVE, directory=directory, save_filename=f'FE3_{resource:04X}_{clip:03d}.anm',
                    file_types=filters)
                if not names:
                    return None
                result = write_asset(names[0], '.anm', data)
                self._asset_directory = Path(names[0]).parent
                return {'name':result['name'], 'edits':game.edit_status()}
            if mode != 'import':
                raise ValueError('请选择导入或导出单个 ANM。')
            names = self._window.create_file_dialog(webview.FileDialog.OPEN, directory=directory,
                                                    allow_multiple=False, file_types=filters)
            if not names:
                return None
            self._asset_directory = Path(names[0]).parent
            return game.clip_import(resource, member, clip, Path(names[0]).read_bytes())
        return self._fe3_call(serial, run)

    def _fe3_file(self, game, resource, member, mode, palette, palette_only):
        import webview

        part = game.part(resource, member)
        directory = str(self._asset_directory or game.path.parent)
        if mode == 'restore':
            return game.replace_asset(resource, member, None)
        if mode == 'import':
            filters = (f'PS2 {part.kind.upper()} (*.{part.kind};*.enc)',)
            if part.kind == 'pom':
                filters = (ASSET_FILTERS['png'],) if palette_only else ('PS2 POM 或 PNG (*.pom;*.png;*.enc)',)
            names = self._window.create_file_dialog(webview.FileDialog.OPEN, directory=directory,
                                                    allow_multiple=False, file_types=filters)
            if not names:
                return None
            self._asset_directory = Path(names[0]).parent
            return game.replace_asset(resource, member, names[0], palette, palette_only)
        extension, data = game.asset_export(resource, member, mode, palette)
        stem = f'FE3_{resource:04X}' + (f'_{member:03d}' if member >= 0 else '')
        if mode == 'png':
            stem += f'_CLUT{palette}'
        names = self._window.create_file_dialog(webview.FileDialog.SAVE, directory=directory,
                    save_filename=stem + extension, file_types=(f'原生资源 (*{extension})',))
        if not names:
            return None
        result = write_asset(names[0], extension, data)
        self._asset_directory = Path(names[0]).parent
        return {'name':result['name'], 'edits':game.edit_status()}

    def fe3_file(self, serial, resource, member, mode, palette, palette_only=False):
        return self._fe3_call(serial, self._fe3_file, resource, member, mode, palette, palette_only)

    def read_music(self, serial, role):
        return self._call(serial, Game.music, role)

    def audition_music(self, serial, member):
        return self._listen(serial, lambda game, cancel: game.shared_resources.audio((member, -1), cancel=cancel))

    def _export_music(self, game, member):
        import webview

        chosen = self._window.create_file_dialog(webview.FileDialog.SAVE, directory=str(game.path.parent),
                                                save_filename=f'KGF_BGM_YUM{member}.wav',
                                                file_types=('WAV 音频 (*.wav)',))
        return write_asset(chosen[0], '.wav', game.music_audio(member, True)) if chosen else None

    def export_music(self, serial, member):
        return self._call(serial, self._export_music, member)

    def list_shared_resources(self, serial):
        return self._call(serial, lambda game: game.shared_resources.catalog())

    def shared_details(self, serial, key):
        return self._call(serial, lambda game: game.shared_resources.details(tuple(key)))

    def shared_texture(self, serial, key, palette):
        return self._call(serial, lambda game: game.shared_resources.texture(tuple(key), palette))

    def shared_listen(self, serial, key):
        return self._listen(serial, lambda game, cancel: game.shared_resources.audio(tuple(key), cancel=cancel))

    def shared_copy_track(self, serial, key, source):
        return self._call(serial, lambda game: game.shared_resources.copy_track(tuple(key), tuple(source)))

    def shared_file(self, serial, key, mode, palette=0):
        return self._call(serial, self._shared_file, tuple(key), mode, palette)

    def _shared_file(self, game, key, mode, palette):
        import webview

        shared = game.shared_resources
        row = shared.row(key)
        kind = row['kind']
        directory = str(self._asset_directory or game.path.parent)
        if mode == 'restore':
            return shared.restore(key)
        if mode in ('import', 'palette'):
            if mode == 'palette' and kind != 'pom':
                raise ValueError('请选择需要换色的 POM。')
            filters = {'pom': (ASSET_FILTERS['png'],) if mode == 'palette' else ('贴图 (*.pom;*.png)',),
                       'png': (ASSET_FILTERS['png'],),
                       'bgm': ('原生 PS2 音轨 (*.bin)',) if shared.ps2 else ('ATRAC3plus 音轨 (*.at3;*.wav)',)}
            chosen = self._window.create_file_dialog(webview.FileDialog.OPEN, directory=directory,
                allow_multiple=False, file_types=filters[kind])
            if not chosen:
                return None
            status = shared.import_file(key, chosen[0], palette, palette_only=mode == 'palette')
            self._asset_directory = Path(chosen[0]).parent
            return {'name': Path(chosen[0]).name, 'edits': status}
        if mode not in ('native', 'png', 'wav'):
            raise ValueError('请选择有效的导出格式。')
        extension = '.' + mode if mode != 'native' else {
            'bgm': '.bin' if shared.ps2 else '.at3', 'pom': '.pom', 'png': '.png'}[kind]
        source = f'FE3_{key[0]:04X}' + (f'_{key[1]}' if key[1] >= 0 else '') if shared.ps2 else f'KGF_YUM_{key[0]}'
        chosen = self._window.create_file_dialog(webview.FileDialog.SAVE, directory=directory,
            save_filename=source + extension, file_types=(f'{extension[1:].upper()} 文件 (*{extension})',))
        if not chosen:
            return None
        extension, data = shared.export(key, mode, palette)
        result = write_asset(chosen[0], extension, data)
        self._asset_directory = Path(chosen[0]).parent
        return {'name': result['name'], 'edits': game.edit_status()}

    def _save_project(self, game, view, portable):
        import webview

        path = None if portable else self._project_path
        update = path is not None
        if not update:
            suffix = PACKAGE if portable else PROJECT
            filters = ('资源改动包 (*.kgfpatch)',) if portable else ('资源工程 (*.kgfp)',)
            chosen = self._window.create_file_dialog(webview.FileDialog.SAVE, directory=str(game.path.parent),
                                                    save_filename=game.path.stem + suffix, file_types=filters)
            if not chosen:
                return None
            path = Path(chosen[0]).resolve()
        result = write_project(game, path, view, portable, update)
        if not portable:
            self._opened_project = path
            self._project_path = path
        return result

    def save_project(self, serial, view, portable=False):
        return self._call(serial, self._save_project, view, portable)

    def _read_project(self, path, sources):
        import webview

        manifest = read_manifest(path)
        paths = (Path(item) for item in (manifest['source'], *sources) if item and isinstance(item, (str, Path)))
        for source in dict.fromkeys(paths):
            if not source.is_file():
                continue
            try:
                candidate = open_game(source)
            except READ_ERRORS:
                continue
            try:
                parse_project(candidate, path, manifest)
            except ProjectBaseError:
                continue
            return candidate, manifest['view']
        chosen = self._window.create_file_dialog(webview.FileDialog.OPEN, directory=str(path.parent),
                                                 allow_multiple=False, file_types=('工程的原始 ISO (*.iso)',))
        if not chosen:
            return None, None
        candidate = open_game(chosen[0])
        parse_project(candidate, path, manifest)
        return candidate, manifest['view']

    def _asset_file(self, game, role, config, shape, kind, index, palette, export_as, palette_only=False):
        import webview

        game.asset_binding(role, config, shape, kind, index)
        directory = str(self._asset_directory or game.path.parent)
        context = role, config, shape, kind, index, palette
        if export_as is None:
            filters = (ASSET_FILTERS[kind],)
            if palette_only:
                if kind != 'pom':
                    raise ValueError('请选择需要换色的贴图。')
                filters = (ASSET_FILTERS['png'],)
            elif kind == 'pom':
                filters += (ASSET_FILTERS['png'],)
            elif kind == 'mdl':
                filters = ('模型 (*.mdl;*.obj;*.fbx)', ASSET_FILTERS[kind],
                           'Wavefront OBJ (*.obj)', 'FBX 模型 (*.fbx)')
            chosen = self._window.create_file_dialog(webview.FileDialog.OPEN, directory=directory,
                                                     allow_multiple=False, file_types=filters)
            operation = game.edit_asset
        else:
            if export_as != kind and (kind, export_as) != ('pom', 'png'):
                raise ValueError('请选择与资源匹配的格式。')
            if kind == 'anm':
                part = f'ANM{index[1]:03d}' if isinstance(index, list) else f'动作{index}'
            else:
                part = '模型' if kind == 'mdl' else f'贴图槽{index}'
            if export_as == 'png':
                part += f'_色表{palette}'
            name = f"{game.roles[role]['label']}_配置{config}_形态{shape}_{part}.{export_as}"
            chosen = self._window.create_file_dialog(webview.FileDialog.SAVE, directory=directory,
                                                     save_filename=name, file_types=(ASSET_FILTERS[export_as],))
            operation = game.export_asset
            context += ('.' + export_as,)
        if not chosen:
            return None
        path = Path(chosen[0])
        result = operation(*context, path, palette_only=True) if palette_only else operation(*context, path)
        self._asset_directory = path.parent
        return result

    def import_asset(self, serial, role, config, shape, kind, index, palette, palette_only=False):
        return self._call(serial, self._asset_file, role, config, shape, kind, index, palette, None, palette_only)

    def export_asset(self, serial, role, config, shape, kind, index, palette, export_as):
        return self._call(serial, self._asset_file, role, config, shape, kind, index, palette, export_as)

    def restore_asset(self, serial, role, config, shape, kind, index, palette):
        return self._call(serial, Game.edit_asset, role, config, shape, kind, index, palette, None)

    def export_iso(self, serial):
        import webview

        with self._lock:
            if self._game is None or serial != self._serial:
                return {'error': '请先打开要另存的 ISO。'}
            try:
                game = self._game
                if isinstance(game, FE3Game):
                    label = '资源编辑'
                elif game.move_sets or game.installed_moves:
                    label = '配招编辑'
                elif game.native_edits or game.template_edits:
                    label = '修改版'
                else:
                    label = '资源编辑' if game.asset_edits else '事件编辑'
                stem = game.path.stem + '_' + label
                name = stem + '.iso'
                suffix = 2
                while (game.path.parent / name).exists():
                    name = f'{stem}_{suffix}.iso'
                    suffix += 1
                chosen = self._window.create_file_dialog(webview.FileDialog.SAVE, directory=str(game.path.parent),
                                                         save_filename=name, file_types=('游戏 ISO (*.iso)',))
                if not chosen:
                    return {'value': None}
                return {'value': game.export(chosen[0])}
            except READ_ERRORS as exc:
                return {'error': str(exc)}


def main():
    import webview

    parser = argparse.ArgumentParser(description='奥格资源编辑器融合版本')
    parser.add_argument('iso', nargs='?', type=Path)
    api = ViewerApi(parser.parse_args().iso)
    api._window = webview.create_window('奥格资源编辑器融合版本', str(HERE / 'web/index.html'), js_api=api,
                                        width=1510, height=930, min_size=(1000, 680),
                                        background_color='#101b2c', text_select=True)
    icon = HERE / 'KGF-v7.ico'
    webview.start(gui='edgechromium', http_server=True, icon=str(icon) if icon.is_file() else None)


if __name__ == '__main__':
    main()
