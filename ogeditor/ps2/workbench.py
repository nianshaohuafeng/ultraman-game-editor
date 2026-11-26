"""Connect the FE3 native role tables with assets and the editing workspace."""

from collections import defaultdict, deque
from functools import cached_property, lru_cache
import math
import struct

from ogeditor.ps2.formats import Bom, damage_fields, require, word
from ogeditor.ps2.native import fill_frames
from ogeditor.ps2.names import atlas_name, atlas_rows
from ogeditor.ps2.native import CharacterProgram, GUARD_TYPES, INPUT_RESOURCE, NativeTables, SHOULDER_ASSIST, SHOULDER_SKILLS, input_rules
from ogeditor.ps2.preview import clip_index, make_model, motion, vector
from ogeditor.ps2.scene import camera_choices, finisher_scene, throw_scene
from ogeditor.ps2.skill_ui import INTRO_GROUP, description_links
from ogeditor.input_notation import SEQUENCE, input_arrows


THROW_STATES = {
    110: '抓取尝试', 111: '抓取尝试结束', 112: '抓取成立 · 施放方 · 方向 0', 113: '抓取成立 · 被抓方 · 方向 0',
    120: '抓取成立 · 施放方 · 方向 1', 121: '抓取成立 · 被抓方 · 方向 1',
    122: '抓取维持 · 施放方', 123: '抓取维持 · 被抓方',
}

MOVEMENT_CONTEXT = {name: input_arrows(label) for name, label in {
    'RUN_F':'向前跑', 'RUN_B':'向后跑', 'RUN_U':'向上跑', 'RUN_D':'向下跑',
    'JUMP_N':'跳跃', 'JUMP_F':'向前跳跃', 'JUMP_B':'向后跳跃',
    'CROUCH_N':'下蹲', 'CROUCH_F':'前下蹲', 'CROUCH_B':'后下蹲',
    'DASH_F':'向前短冲', 'DASH_B':'向后短冲', 'DASH_U':'向上短冲', 'DASH_D':'向下短冲',
    'ROLLING_F':'前滚', 'ROLLING_B':'后滚',
}.items()}


def command_context(actions, commands, rules, shape):
    """Follow ERX movement entries and their automatic continuation states."""
    entry = {}
    for source in (0, 1):
        for edge in commands.get(source, ()):
            rule = rules.get(edge['command'])
            label = MOVEMENT_CONTEXT.get(rule['name']) if rule else None
            if label and not edge['excluded'] & (1 << shape):
                entry.setdefault(edge['to'], label)
    result = {0:'站立', 1:'起身', **entry}
    for start, label in entry.items():
        state = actions[start][0]
        while 0 <= state < len(actions) and state not in result:
            result[state] = label
            state = actions[state][0]
    return result


def combo_paths(direct, combos):
    paths = defaultdict(dict)
    for start, keys in direct.items():
        for key in keys:
            pending = deque([(start, key, {start})])
            queued = set()
            while pending:
                source, name, seen = pending.popleft()
                for link in combos.get(source, ()):
                    target = link['target']
                    if target in seen:
                        continue
                    followup = name + SEQUENCE + link['label']
                    if (target, followup) in queued:
                        continue
                    queued.add((target, followup))
                    paths[target][followup] = None
                    pending.append((target, followup, seen | {target}))
    return paths


def uses_shape(event, form):
    # 0x204320 loads the event mask as s8; SRLV at 0x204328 uses the model slot.
    mask = event['selector']
    return ((mask if mask < 128 else mask - 256) >> (form.id & 31)) & 1


def combo_link(event, available, inputs):
    if event['op'] != 5:
        return None
    command, duration, target = event['args'][:3]
    if target not in available:
        return None
    return {'command':command, 'label':inputs.get(command, f'输入 #{command}'), 'target':target,
            'id':event['id'], 'offset':event['offset'], 'start':event['frame'], 'duration':duration,
            'end':event['frame'] + duration, 'selector':event['selector']}


def shape_for(bom, event):
    op = event['op']
    if op not in (0, 1, 2, 3, 4, 12, 24):
        return None
    args = event['args']
    template = args[{0:9, 24:14}.get(op, 2)]
    lifetime = args[1]
    node = args[0]
    if node < 0 or not 0 <= template < bom.shapes:
        return None
    at = bom.offsets[bom.scripts + template]
    kind = word(bom.data, at)
    length = 6 if kind == 2 else 3
    xyz = struct.unpack_from(f'<{length}f', bom.data, at + 4)
    radius, = struct.unpack_from('<f', bom.data, at + 4 + length * 4)
    if not math.isfinite(radius) or not all(map(math.isfinite, xyz)) or radius <= 0:
        return None
    return {'node':node, 'template':template, 'radius':radius, 'center':xyz[:3],
            'to':xyz[3:] if kind == 2 else None,
            'layer':'attack' if op in (0, 24) else 'extra' if op in (1, 2, 12) else 'body',
            'duration':max(0, lifetime)}


@lru_cache(maxsize=128)
def morph_pose(data, index):
    """Resolve one PS2 VAU sample into the MDL offsets written by 0x32B198."""
    count, directory = struct.unpack_from('<2I', data, 8)
    if not 0 <= index < count:
        return {}
    _, time, _, block = struct.unpack_from('<IfII', data, directory + index * 16)
    begin = word(data, 16 + block * 4)
    mode, mapping, samples = struct.unpack_from('<3I', data, begin + 16)
    tracks = word(data, begin + 36)
    require(mode == 2 and math.isfinite(time) and time >= 0, 'FE3 VAU 采样方式无效。')
    sample_offset = begin + samples
    values = sample_offset + 32 * tracks
    address = begin + mapping
    index_left = int(time)
    fraction = time - index_left
    result = {}
    for track in range(tracks):
        at = values + 3 * (track + tracks * index_left)
        q = tuple(data[at:at+3])
        if fraction:
            q = tuple(a + fraction * (b - a) for a, b in zip(q, data[at + 3*tracks:at + 3*tracks + 3]))
        origin = vector(data, sample_offset + 32 * track)
        scale = vector(data, sample_offset + 32 * track + 16)
        value = tuple(a + b * v / 255 for a, b, v in zip(origin, scale, q))
        repeats = word(data, address)
        address += 4
        for _ in range(repeats):
            result[word(data, address)] = value
            address += 4
    return result


class Workbench:
    def __init__(self,game,preview=True):
        self.game = game
        self.preview = preview
        self.plan = None
        self._tables = NativeTables(game.elf)
        self.binding_values = None
        self.binding_tables = None
        self.layouts = {}
        self.programs = {}
        self.banks = {}

    @property
    def tables(self):
        if self.preview and self.plan is not self.game.transfer_plan:
            self.plan = self.game.transfer_plan
            self._tables = NativeTables(self.plan.elf or self.game.elf)
            self.layouts.clear()
            self.programs.clear()
            self.banks.clear()
            self.binding_values = None
        return self._tables

    def forms(self, role):
        tables = self.tables
        if self.binding_values is not self.game.binding_edits:
            self.binding_values = self.game.binding_edits
            self.binding_tables = self.game.tables_with_bindings(self.binding_values,tables.elf) if self.binding_values else tables
            self.layouts.clear()
        if role not in self.layouts:
            self.layouts[role] = self.binding_tables.forms(self.game, role)
        return self.layouts[role]

    def form(self, role, shape):
        require(type(shape) is int and shape in self.forms(role), '请选择角色的原生模型槽。')
        return self.forms(role)[shape]

    def program(self, role):
        self.tables.descriptor(role)
        if role not in self.programs:
            path = self.program_path(role)
            require(path in self.game.files, f'镜像缺少角色模块 {path}。')
            data = self.plan.modules.get(role) if self.plan else None
            self.programs[role] = CharacterProgram(data if data is not None else self.game.read_iso_entry(path),
                                                    self.tables.command_ordinal(role))
        return self.programs[role]

    @staticmethod
    def program_path(role):
        return f'ERX/PLAYER{role // 20 * 20:02d}/PL_{role:02d}.ERX'

    def action_rows(self, role):
        return [(nxt, self.game.throw_edits.get((role, state), anm), flags)
                for state, (nxt, anm, flags) in enumerate(self.program(role).actions())]

    def require_action(self, role, state):
        rows = self.program(role).actions()
        require(0 <= state < len(rows) and (rows[state][0] >= 0 or rows[state][1] >= 0),
                '请选择该角色已有的状态。')

    def callbacks(self, role, state):
        program = self.program(role)
        entry = program.action_table + 24 * state
        callbacks = []
        for local in program.at(entry, '<3I'):
            trap, ordinal = program.at(local, '<2I', b'.text')
            ee = self.tables.pointer(0x483620, ordinal & 0xFFFF) if trap == 0x4D and ordinal >> 16 == 0x2400 else None
            callbacks.append({'erx':local, 'ee':ee})
        return callbacks

    def action_motion(self,role,state,opponent,callbacks=None):
        rows = self.action_rows(role)
        if not 0 <= state < len(rows):
            return None
        _,anm,_ = rows[state]
        callbacks = self.callbacks(role,state) if callbacks is None else callbacks
        entry = callbacks[1]['ee']
        if entry == 0x2CAF10:
            return None
        slots = self.binding(role)
        indexed = {0x2C8218:1,0x2C8760:0}
        if entry in indexed:
            # 0x226F30 indexes the opponent bank of the actor being animated.
            _,sizes = self.tables.descriptor(role)
            if opponent < sizes[6]:
                bank = self.tables.banks(role)[sizes[5]+opponent][1]
                if bank+indexed[entry] in slots:
                    anm = bank+indexed[entry]
        elif entry == 0x2C8908 and opponent == 11:
            # Tyrant uses a dedicated held-pose slot for each opponent.
            alternate, = self.tables.at(0x492170+2*(20*role+15),'<h')
            if alternate in slots:
                anm = alternate
        if anm not in slots:
            return None
        rate_source = {0x2C8760:0x4BA6F4,0x2C8908:0x4BA6F8}
        scale = self.tables.at(rate_source[entry],'<f')[0] if entry in rate_source else 1.0
        return {'anm':anm,'rateScale':scale,
                'dynamic':entry in indexed or (entry == 0x2C8908 and opponent == 11 and anm != rows[state][1])}

    def throw_rows(self,role,opponent):
        self.tables.descriptor(opponent)
        actions = self.action_rows(role)
        result = []
        for state,label in THROW_STATES.items():
            if state >= len(actions):
                continue
            nxt,anm,flags = actions[state]
            callbacks = self.callbacks(role,state)
            motion = self.action_motion(role,state,opponent,callbacks)
            if motion is not None:
                result.append({'action':state,'name':label,'next':nxt,'default':anm,'opponent':opponent,
                               'callbacks':callbacks,'erx':self.program(role).action_table+24*state,
                               'flags':flags,**motion})
        return result

    def throw_view(self, role, shape, opponent):
        form = self.form(role, shape)
        stages = self.throw_rows(role, opponent)
        choices = self.command_rows(role)
        selection = [edge for state in (110, 112, 113, 120, 121, 122, 123, 124, 125)
                     for edge in choices.get(state, ()) if not edge['excluded'] & (1 << shape)]
        geometry = [{'state':stage['action'], 'anm':stage['anm'], 'frame':event['frame'], 'shape':event['shape']}
                    for stage in stages for event in self.events(form, stage['anm'])
                    if event.get('shape') and (event['frame'] > 0 or event['op'] in (0, 1, 24))]
        camera_callbacks = {0x2C83A8, 0x2C8850}
        camera = [{'state':stage['action'], 'anm':stage['anm'], 'ee':callback['ee'], 'phase':phase}
                  for stage in stages for phase, callback in zip(('更新', '进入', '离开'), stage['callbacks'])
                  if callback['ee'] in camera_callbacks]
        _, sizes = self.tables.descriptor(role)
        banks = self.tables.banks(role)
        related = []
        if opponent < sizes[6]:
            resource, start = banks[sizes[5] + opponent]
            key = resource, -1
            related = [{'id':start + i, 'asset':[*key, i], 'name':f'对手组片段 {i}',
                        'frames':clip.upper if clip.upper > 0 else clip.frames}
                       for i, clip in enumerate(clip_index(self.game.data(key)))]
        return {'stages':stages, 'selection':selection, 'range':geometry, 'camera':camera, 'banks':related,
                'curves':camera_choices(self, role)}

    def battle_preview(self, role, shape, kind, number, opponent, donor=None, donor_candidate=None, camera=-1):
        if kind == 'skill':
            return finisher_scene(self, role, shape, number, opponent, donor, donor_candidate)
        require(kind == 'throw', '请选择必杀技或投技。')
        return throw_scene(self, role, shape, number, opponent, camera)

    def binding(self, role):
        tables = self.tables
        if role not in self.banks:
            found = {}
            for group,start in dict.fromkeys(tables.banks(role)):
                key = group,-1
                data = self.game.data(key) if self.preview else self.game.original(key)
                for index in range(len(clip_index(data))):
                    found[start + index] = key, index
            self.banks[role] = found
        return self.banks[role]

    def model(self, role, shape):
        return make_model(self.game, self.form(role, shape))

    @cached_property
    def inputs(self):
        return {index:row['label'] for index, row in self.rules.items()}

    @cached_property
    def rules(self):
        return input_rules(self.game.original((INPUT_RESOURCE, -1)))

    def animation(self, role, shape, number):
        form = self.form(role, shape)
        slots = self.binding(role)
        require(type(number) is int and number in slots, '请在当前角色的动作列表选择 ANM。')
        key, index = slots[number]
        data = self.game.data(key)
        animated = motion(data, index)
        # The descriptor's animated hierarchy uses an explicit channel map.
        animated['id'] = number
        events = self.events(form, number)
        for event in events:
            link = combo_link(event, slots, self.inputs)
            if link:
                event['combo'] = link
                event['name'] = '连段接续'
        return {'animation':animated, 'events':events, 'anm':[ *key, index],
                'bom':form.bom, 'clip':clip_index(data)[index].fields}

    def events(self, form, script):
        if not form.bom:
            return []
        bom = Bom(self.game.data(form.bom))
        if script >= bom.scripts:
            return []
        vau = self.game.data(form.vau) if form.vau else None
        rows = sorted((event for event in bom.events(script) if uses_shape(event, form)),
                      key=lambda event: (event['frame'], event['id']))
        for event in rows:
            shape = shape_for(bom, event)
            if shape:
                event['shape'] = shape
                # 0x20CE10 removes the record at current_frame >= its stored cutoff.
                event['until'] = event['frame'] + shape['duration']
            elif event['op'] == 5:
                # 0x203388/0x214718 set the expiry of a combo input window.
                event['until'] = event['frame'] + max(0, event['args'][1])
            if event['op'] == 18 and vau:
                values = morph_pose(vau, event['args'][0])
                if values:
                    event['morph'] = values
            if event['op'] == 14:
                rate, = struct.unpack_from('<f', bom.data, event['offset'] + 4)
                if math.isfinite(rate) and rate > 0:
                    event['rate'] = rate
                    event['name'] = '播放倍率'
            if event['op'] == 21:
                event['visible'] = event['args'][0] != 1
        return rows

    def support_choices(self, role, skill):
        if skill.kind == 0:
            return [(value, label, -1) for value, label in GUARD_TYPES.items()]
        choices = {0: ('关闭肩键效果', -1)}
        if self.tables.has_shoulder_assist(role):
            choices[SHOULDER_ASSIST] = ('角色原生援护', -1)
        # A shared candidate must work in every playable form that can equip it.
        playable = sum(1 << form.id for form in self.forms(role).values() if not form.accessory and form.id < 3)
        active = skill.menu_mask & playable
        moves, clips = self.action_rows(role), self.binding(role)
        native = SHOULDER_SKILLS.get(role, {})
        for source in self.tables.skill_rows(role, 2):
            state, label = native.get(source.id, (source.words[0], source.label))
            if 0 < state < len(moves) and (active & ~source.menu_mask) == 0 and moves[state][1] in clips:
                choices.setdefault(state, (label, moves[state][1]))
        return [(value, label, anm) for value, (label, anm) in choices.items()]

    def skills(self, role, kind=1):
        moves = self.action_rows(role)
        result = []
        tables = self.tables
        choices = tables.battle_skill_rows(role, kind)
        name_source = tables.skill_name_source(role) if kind == 1 and any(skill.menu_candidate for skill in choices) else None
        atlas = atlas_rows(self.game.data(name_source)) if name_source else ()
        loading_source = tables.skill_loading_source(role) if kind == 1 and choices else None
        links, _ = description_links(tables.elf) if name_source else ({}, None)
        rates = self.game.charge()['rates'] if kind == 1 else ()
        menu_enabled = tables.skill_menu_enabled(role)
        defaults = tables.default_finishers(role) if kind == 1 else {}
        if kind != 1:
            from ogeditor.ps2.support import installed_assist, installed_supports
            installed = installed_supports(self.game.elf)
        if kind == 0:
            from ogeditor.ps2.guard import installed_absorptions
            _, guard_routes = installed_absorptions(self.program(role))
        for skill in choices:
            key = role, skill.id
            words = self.game.current_skill_words(*key)
            action, = struct.unpack('<i', struct.pack('<I', words[0]))
            anm = moves[action][1] if (kind == 1 and 0 <= action < len(moves)
                                       or kind == 2 and 0 < action < len(moves)) else -1
            power = struct.unpack('<f', struct.pack('<I', words[3]))[0] if kind == 1 else None
            label = self.game.transfer_plan.names.get(key, atlas_name(atlas if skill.menu_candidate else (),
                                                                      role, skill.id, skill.label))
            intro = links.get(key)
            if intro is None and name_source and skill.menu_candidate:
                intro = tables.skill_description_source(*key)
            configurable = menu_enabled and skill.menu_candidate
            result.append({'id':skill.id, 'number':skill.value, 'name':label, 'kind':kind,
                           'mask':words[1], 'menuMask':skill.menu_mask, 'action':action,
                           'anm':anm, 'power':power, 'words':words,
                           'rating':self.game.skill_rating(*key) if kind == 1 else None,
                           'chargeFrames':[fill_frames(rate,power) for rate in rates],
                           'original':self.game.transfer_plan.skill_words.get(key,skill.words), 'va':skill.va,
                           'menuCandidate':skill.menu_candidate, 'configurable':configurable,
                           'fixedButtons':defaults.get(skill.value, (0, ()))[1] if not configurable else (),
                           'transfer':self.game.transfers.get(key),
                           'added':self.game.additions.get(key),
                           'nameSource':name_source if skill.menu_candidate else None,
                           'loadingNameSource':loading_source,
                           'descriptionSource':(INTRO_GROUP, *intro) if intro is not None else None})
            if kind != 1:
                result[-1]['choices'] = self.support_choices(role, skill)
                if key in installed and key not in self.game.transfers and key not in self.game.skill_edits:
                    source_role, source_id = installed[key]
                    result[-1]['installedSupport'] = (source_role,source_id)
                    if kind == 2 or key not in self.game.stock_menu_candidates or any(
                            item.candidate == skill.id for item in guard_routes.values()):
                        prior = next((row for row in tables.skill_rows(source_role,kind) if row.id == source_id),None)
                        if prior:
                            result[-1]['name'] = prior.label
            if kind == 0:
                result[-1]['guardMotions'] = {item.shape: item.animations[0] for item in guard_routes.values()
                                              if item.candidate == skill.id and action == 2}
            if kind == 2:
                result[-1]['assistSource'] = installed_assist(words[0])
        return result

    def command_rows(self, role):
        program = self.program(role)
        actions = self.action_rows(role)
        return {source:[{'from':source, 'to':target, 'command':command, 'condition':condition & 255,
                         'flags':condition & ~255, 'excluded':(condition >> 8) & 7, 'priority':priority,
                         'order':order, 'erx':entry, 'anm':actions[target][1],
                         'handler':self.tables.pointer(0x409278, (condition & 255) * 2) if condition & 255 else 0,
                         'label':self.inputs.get(command, f'输入 #{command}')}
                        for order, (target, command, condition, priority, entry) in enumerate(candidates)]
                for source, candidates in program.commands().items()}

    def contextual_commands(self, role, shape):
        commands = self.command_rows(role)
        actions = self.action_rows(role)
        context = command_context(actions, commands, self.rules, shape)
        if len(actions) > 14 and actions[14][:2] == (15, -1) and self.callbacks(role, 14)[1]['ee'] == 0x2C2F40:
            context[14] = '防御硬直'
        standing = {(edge['to'], edge['command'], edge['condition']) for source in (0, 1)
                    for edge in commands.get(source, ()) if not edge['excluded'] & (1 << shape)}
        for source, entries in commands.items():
            for edge in entries:
                identity = edge['to'], edge['command'], edge['condition']
                if source in (0, 1) or (identity in standing and source not in context):
                    label = edge['label']
                else:
                    label = context.get(source, f'状态 {source}') + SEQUENCE + edge['label']
                edge['usage'] = label
        return commands, context

    def profile(self, role, shape):
        form = self.form(role, shape)
        action_rows = self.action_rows(role)
        commands, context = self.contextual_commands(role, shape)
        skills = self.skills(role)
        forms = [f for f in self.forms(role).values() if not f.accessory]
        state_forms = defaultdict(lambda: defaultdict(dict))
        for entries in commands.values():
            for edge in entries:
                for other in forms:
                    if not edge['excluded'] & (1 << other.id):
                        state_forms[edge['to']][other.id][edge['label']] = None
        form_names = {f.id:f.label for f in forms}
        variants = {state:' / '.join(form_names[i] for i in by_form) + ' · ' +
                    ' / '.join(dict.fromkeys(label for labels in by_form.values() for label in labels))
                    for state,by_form in state_forms.items() if len(by_form) < len(forms)}
        state_map, clip_labels, offered, first_commands = defaultdict(list), defaultdict(list), defaultdict(dict), defaultdict(set)
        for index, (_, clip, _) in enumerate(action_rows):
            if clip >= 0:
                state_map[clip].append(index)
        for edges in commands.values():
            for row in edges:
                if row['excluded'] & (1 << shape):
                    continue
                first_commands[row['to']].add(row['command'])
                offered[row['to']][row['usage']] = None
        for row in skills:
            if row['anm'] >= 0:
                clip_labels[row['anm']].append(row['name'])
        for action, label in THROW_STATES.items():
            if action < len(action_rows) and action_rows[action][1] >= 0:
                clip_labels[action_rows[action][1]].append(label)
        bom = Bom(self.game.data(form.bom)) if form.bom else None
        details = {}
        motions = []
        slots = self.binding(role)
        combos, participants = {}, set()
        for number, (key, index) in sorted(slots.items()):
            if key not in details:
                details[key] = clip_index(self.game.data(key))
            clip = details[key][index]
            states = state_map[number]
            inputs = sorted({cmd for state in states for cmd in first_commands[state]})
            keys = list(dict.fromkeys(key for state in states for key in offered[state]))
            label = '默认待机' if number == 0 else ' / '.join(dict.fromkeys(clip_labels[number]))
            events = [event for event in bom.events(number, parameters=False) if uses_shape(event, form)] if bom and number < bom.scripts else []
            links = [link for event in events if (link := combo_link(event, slots, self.inputs))]
            if links:
                combos[number] = links
                participants.update((number, *(link['target'] for link in links)))
            damages = [event['args'][index] for event in events for index, _ in damage_fields(event['op'])]
            motions.append({'id':number, 'name':label, 'states':states, 'commands':inputs, 'keys':keys,
                            'frames':clip.upper if clip.upper > 0 else clip.frames,
                            'events':len(events), 'damage':sorted(set(damages)), 'asset':[*key, index]})
        paths = combo_paths({row['id']:row['keys'] for row in motions if row['keys']}, combos)
        for row in motions:
            number = row['id']
            keys = row['keys']
            inherited = sorted(paths[number], key=lambda label: label.count(SEQUENCE))
            labels = [key for key in keys if not (key.startswith(('→跳跃' + SEQUENCE, '←跳跃' + SEQUENCE))
                      and '跳跃' + SEQUENCE + key.split(SEQUENCE, 1)[1] in keys)]
            restricted = [f'{variants[st]}（状态 {st}）' for st in row['states'] if st in variants]
            states = row['states']
            fallback = '动作片段 ' + f'{number:03d}'
            if states:
                fallback = ('共用状态 ' if len(states)>1 else '程序状态 ') + ' / '.join(map(str,states[:4]))
                if len(states)==1 and action_rows[states[0]][0] >= 0:
                    fallback += f' · 默认后继 {action_rows[states[0]][0]:03d}'
            row['name'] = row['name'] or (' / '.join(restricted) if restricted else ' / '.join(labels[:3]) if labels else
                inherited[0] + ' · 连段后续' if inherited else fallback)
            row['keys'] = list(dict.fromkeys([*keys, *inherited]))
            row['combo'] = number in participants or any(
                SEQUENCE in key and any(button in key.rsplit(SEQUENCE, 1)[1] for button in '□△○×')
                for key in keys)
        shapes = [{'id':item.id, 'name':item.label, 'model':item.model, 'vau':item.vau, 'bom':item.bom,
                   'textures':item.textures, 'secondary':item.secondary, 'accessory':item.accessory}
                  for item in self.forms(role).values()]
        states = [{'state':state, 'next':nxt, 'anm':anm, 'flags':flags, 'context':context.get(state)}
                  for state, (nxt, anm, flags) in enumerate(action_rows) if nxt >= 0 or anm >= 0]
        return {'forms':shapes, 'moves':motions, 'skills':skills, 'shoulder':self.skills(role, 2),
                'guard':self.skills(role, 0), 'supportMenu':self.tables.skill_menu_enabled(role),
                'lastAddedSkill':max((candidate for owner,candidate in self.game.additions if owner==role),default=-1),
                'throws':self.throw_rows(role, 0),
                'commands':commands, 'combos':combos, 'states':states}
