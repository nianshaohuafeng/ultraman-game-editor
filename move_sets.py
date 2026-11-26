"""Pack ANM, BOM and the native action records for cross-character moves."""

from collections import defaultdict, deque
from dataclasses import dataclass
import math
import struct

from animation_edit import read_anm_bank, with_bind_tracks
from appearance import item_models
from asset_transfer import clip_ids, validate_clip
from audio_preview import find_sample
from cross_timing import TAG as TIMING_TAG
from event_edits import Word, change_words, event_words
from editor_readers import read_mdl
from iso_save import move_padding
from formats import SHAPE_TEMPLATE_OFFSETS, anm_header, bom_parts, event_records, s16, sections, u32
from move_creatures import (THROW_STATES, babar_chain, hipporit_capture, hipporit_chamber,
                             install_babar_chain_update, install_contact_routes, install_throw_victims,
                             restore_babar_partner, throw_victim_clip)
from move_eboot import (A0, A1, BASE, EFFECT_BASE, SLOT_BASE, T4, T5, Z, Eboot,
                        aligned, branch, iword, jr, lw, payload_limit)
from move_runtime import graft_locator, install_cameras, keep_fighter_portraits
from move_special import FIRST_PRIVATE_STATE, charge_factory, install_borrowed_charge, install_private_states
from move_roles import (ACTION_CLIPS, BACK_SKILLS, BLACK_AUX_CLIP, BLACK_SCENE_SOURCE, INIT_FORM,
                        JACK_THROW, SCENE_EFFECT_BASE, SCENE_FACTORIES, SCENE_ROLES, black_pair_callbacks, clone_scene,
                        install_animation_aliases, install_model_forms, install_scene_effects,
                        jack_scene_factory, overlay_initializer)
from move_taro import (TARO_DOWN, TARO_FIRE_NODES, TARO_PHASES, install_taro_hit_routes,
                       taro_fire_callbacks, taro_flames, taro_timer_callbacks)
from move_throw import (LEO_THROW, LEO_THROW_STAGES, LEO_WEAPON_POM, LEO_WEAPON_TRACKS,
                        graft_weapon, install_throw_entry, install_weapon_visibility,
                        weapon_clip, weapon_state_callbacks)
from move_texture_refs import COMMON_SLOTS, DRAW_PALETTES, ITEM_SLOTS
from native_tables import state_branches


ORDINARY = {0x08898258, 0x08898328, 0x0889957C, 0x088996A0}
SKILL = {0x08898918, 0x08898ACC, 0x08898B50, 0x08898C28, 0x08899014, 0x0882DAA4, 0x08812630,
         0x0889957C, 0x0883A6A4, 0x0883A7D4, 0x0884C414, 0x0881618C, 0x0881DFC4}
STATES = {158: 159, 160: 161, 162: 163}
BABAR_DOWN = (16, 160)
# Cutscene hand-off and generic particles have no donor resource reads.
COMMON_EFFECTS = {-1, 7, 13, 29, 30}
# start, end, addiu instructions using node-array offsets, loads of the common POM array
EFFECTS = {
    17: (0x088805A4, 0x088806D0, (0x088805F8,), (0x0888062C,)),
    18: (0x088806D0, 0x088807A4, (0x0888072C,), (0x08880714,)),
    27: (0x08880FE8, 0x08881108, (0x08881034,), (0x08881054,)),
    28: (0x08881108, 0x0888119C, (0x08881130,), (0x08881140,)),
    32: (0x08881238, 0x088812D8, (0x08881264,), (0x08881280,)),
    31: (0x0888119C, 0x08881238, (0x088811C4,), (0x088811E0,)),
    20: (0x088807F8, 0x08880B48, (0x08880868,), (0x08880894,)),
    21: (0x08880B48, 0x08880EC8, (0x08880BB8,), (0x08880BE8,)),
    34: (0x088817EC, 0x08881934, (), (0x0888188C,)),
    35: (0x088816E4, 0x088817EC, (0x08881720,), (0x08881764,)),
    38: (0x08881934, 0x08881A34, (0x08881968,), (0x08881984, 0x0888199C)),
    44: (0x08881AF8, 0x08881C00, (0x08881B24,), (0x08881B58,)),
    46: (0x08881DF4, 0x08881F08, (0x08881E20,), (0x08881E54,)),
    49: (0x08882004, 0x088821DC, (0x08882074, 0x0888208C, 0x088820A4), (0x0888211C,)),
    51: (0x08882230, 0x088823A0, (0x088822D4, 0x088822D8), (0x088822B8,)),
    52: (0x088823A0, 0x08882448, (0x088823CC,), (0x088823E8,)),
    53: (0x08882448, 0x08882790, (0x088824A8,), (0x088824C4, 0x088824DC)),
    55: (0x088827E4, 0x08882994, (0x0888280C,), (0x08882900,)),
    56: (0x08882994, 0x08882A80, (0x088829C8,), (0x088829EC,)),
    59: (0x08882ADC, 0x08882BEC, (0x08882B1C,), (0x08882B3C,)),
    60: (0x08882BEC, 0x08882DA4, (0x08882C38, 0x08882D00), (0x08882C58, 0x08882D20)),
    71: (0x08883740, 0x08883814, (0x0888376C,), (0x08883788,)),
    72: (0x08883814, 0x088839EC, (0x08883840,), (0x08883954,)),
    83: (0x08883BA8, 0x08883E5C, (0x08883C74, 0x08883C78), (0x08883CD0,)),
    88: (0x08884770, 0x08884908, (0x088847C8, 0x088847D0), (0x08884808,)),
    93: (0x08884B34, 0x08884DFC, (0x08884B84,), (0x08884BBC, 0x08884CF0)),
    87: (0x08883FAC, 0x08884378, (0x08884018,), (0x0888408C, 0x088841B8, 0x088842D8)),
    92: (0x08884A04, 0x08884B34, (0x08884A60, 0x08884A78, 0x08884A88), (0x08884A38, 0x08884A5C)),
    96: (0x08884FF4, 0x08885174, (0x0888510C, 0x08885110, 0x08885168, 0x08885170), (0x08885074,)),
    100: (0x08885258, 0x088853C0, (0x088852FC, 0x08885300), (0x088852E0,)),
    109: (0x08885578, 0x08885640, (0x088855A8, 0x088855AC), ()),
}
EFFECT_STATES = {20: (0x08880A94,), 21: (0x08880E18,), 27: (0x08881078,), 35: (0x08881738,),
                 38: (0x088819B8,), 53: (0x0888270C,), 56: (0x08882A04,),
                 60: (0x08882C74, 0x08882D48), 71: (0x08883784,), 83: (0x08883DEC,), 92: (0x08884AAC,)}
# These reads take row 0 of the owner's 089450CC node table. Its IDs match
# the default fighter MDL. A borrowed effect knows the actual loaded form.
EFFECT_ROOT_LOADS = {34: 0x0888184C, 47: 0x0887479C, 66: 0x08874C34, 92: 0x08884A58}
# Small native wrappers pass their bone index in the JAL delay slot.
NODE_EFFECTS = {
    22: (0x08880EC8, 0x08885978, 0x08885B34, 0x08885AA0, 30),
    23: (0x08880EE4, 0x08885978, 0x08885B34, 0x08885AA0, 9),
    40: (0x08881AA4, 0x08885B34, 0x08885C50, 0x08885BC0, 29),
    41: (0x08881A88, 0x08885B34, 0x08885C50, 0x08885BC0, 44),
    42: (0x08881AC0, 0x08885C50, 0x08885D94, 0x08885CF0, 37),
    43: (0x08881ADC, 0x08885C50, 0x08885D94, 0x08885CF0, 22),
    97: (0x08885174, 0x08885E98, 0x08885F70, 0x08885EF0, 43),
    98: (0x08885190, 0x08885E98, 0x08885F70, 0x08885EF0, 52),
}
BEAM_TRAILS = {101: 0x088853C0, 102: 0x088853EC, 103: 0x08885418,
               104: 0x08885444, 105: 0x08885470, 107: 0x088854C8}
BODY_EVENTS = {2, 10}
NODE_FIELDS = {**{key: 4 for key in SHAPE_TEMPLATE_OFFSETS}, 0x0F: 6, 0x15: 8, 0x1B: 6}
MISSING = 0xFFFFFFFF
LEO_FORWARD = (5, 158)
LEO_STAGES = {181: '起跳', 182: '空中等待', 21: '落地', 24: '收招'}


def forward_skill_branches(game, role):
    """Find input records that go through a character-specific lead-in to state 158."""
    if 158 in game.entry_commands[role]:
        return set()
    lead = next((row['target'] for row in state_branches(game.image, BASE, role, 0)
                 if row['command'] == 33 and row['target'] != 158), None)
    if lead is None:
        return set()
    action = game.native_actions[role].get(lead)
    end = game.native_actions[role].get(int(action['parameter'], 16)) if action else None
    if end is None or int(end['parameter'], 16) != 158:
        return set()
    return {row['va'] for state in game.native_actions[role]
            for row in state_branches(game.image, BASE, role, state)
            if row['command'] == 33 and row['target'] == lead}


def can_mix_move(move):
    return move['category'] in (4, 5) or move['category'] == 11 and move['id'] in THROW_STATES


def ordinary_recovery(moves, move):
    # 08899534 / 08899658 call 088999A0 at animation end. It reads
    # the signed halfword at +0C and requests that state's own animation.
    if move['category'] != 4 or move['callbacks'][1] not in ('0x08899534', '0x08899658'):
        return False
    state = int(move['parameter'], 16)
    follow = moves.get(state)
    return (state not in (0, 1, 0xFFFF) and follow is not None and follow['category'] == 0
            and follow['callbacks'] == ['0x0889957C', '0x08899534', '0x088995B0']
            and int(follow['parameter'], 16) in (0, 1, 0xFFFF))


def donor_status(game, role, action, actions=None):
    moves = game.actions[role] if actions is None else actions
    move = moves[action]
    entry = int(move['callbacks'][0], 16)
    leo = (role, action) == LEO_FORWARD
    throw = (role, action) == LEO_THROW
    special = (role, action)
    phases = LEO_STAGES if leo else (*LEO_THROW_STAGES, 161) if throw else BACK_SKILLS.get(special)
    if special == JACK_THROW:
        phases = (161, 181, 182)
    if special == BABAR_DOWN:
        phases = (161, 181)
    if special == TARO_DOWN:
        phases = tuple(TARO_PHASES)
    if move['category'] == 11 and action in THROW_STATES:
        phases = ()
    if phases is None and entry not in (ORDINARY if move['category'] == 4 else SKILL):
        return '专用状态回调待适配'
    family = [move['clip']]
    recovery = int(move['parameter'], 16)
    if phases is not None:
        family.extend(moves[phase]['clip'] for phase in phases)
        family.extend(clip for phase in (action, *phases) for clip in ACTION_CLIPS.get((role, phase), ()))
    elif recovery not in (0, 1, 0xFFFF):
        follow = moves.get(recovery)
        skill_follow = (move['category'] == 5 and recovery == STATES.get(action) and follow is not None
                        and int(follow['parameter'], 16) in (0, 1, 0xFFFF))
        if not skill_follow and not ordinary_recovery(moves, move):
            return '专用收招阶段待适配'
        family.append(follow['clip'])
    _, _, bom, _ = game.form_binding(role, 0, 0)
    count, _, scripts = game.bank(bom['id'], b'BOM\0')
    if entry == 0x0883A6A4:
        family.extend([clip + 2 for clip in family])
    supported = {*COMMON_EFFECTS, *EFFECTS, *NODE_EFFECTS, *BEAM_TRAILS, *SCENE_ROLES,
                 25, 26, 45, 65, 67, 68, 70, 73, 75, 76, 82, 90, 94, 95, 110, 111}
    for clip in family:
        if not 0 <= clip < count or not scripts[clip]:
            return '缺少对应的 BOM'
        if any(record.raw[0] == 8 and s16(record.raw, 4) not in supported
               for record in event_records(scripts[clip]) if len(record.raw) >= 6):
            return '专用技能特效待适配'
    return ''


def append_bytes(data, value, alignment=16):
    offset = aligned(len(data), alignment)
    data.extend(bytes(offset - len(data)))
    data.extend(value)
    return offset


def expand_anm(data, clips):
    count, offsets, base = read_anm_bank(data)
    if not clips:
        return bytearray(data)
    size = max(clips) + 1
    if count > min(clips) or size > 4096:
        raise ValueError('配招 ANM 编号和原动作重叠。')
    end = 8 + 4 * size
    out = bytearray(data)
    for index, part in enumerate(base):
        if offsets[index] < end:
            offsets = (*offsets[:index], append_bytes(out, part), *offsets[index + 1:])
    positions = [*offsets, *([MISSING] * (size - count))]
    for index, part in clips.items():
        positions[index] = append_bytes(out, part)
    struct.pack_into('<I', out, 4, size)
    struct.pack_into(f'<{size}I', out, 8, *positions)
    return out


def expand_bom(data, clips, templates):
    count, offsets, _ = bom_parts(data)
    if not clips:
        return data
    size = max(clips) + 1
    if count > min(clips) or size > 4096:
        raise ValueError('配招 BOM 编号和原事件重叠。')
    current = 12 + len(offsets) * 4
    extra = 4 * (size - count + len(templates))
    growth = aligned(extra)
    out = bytearray(data[:current] + bytes(growth) + data[current:])
    if extra != growth and data.startswith(TIMING_TAG,current):
        start = min((off for off in offsets if off != MISSING),default=len(data))
        out[current+extra:start+growth] = data[current:start] + bytes(growth-extra)
    offsets = [off + growth if off != MISSING else off for off in offsets]
    positions = [*offsets[:count], *([MISSING] * (size - count)), *offsets[count:]]
    for index, script in clips.items():
        positions[index] = append_bytes(out, script, 4)
    positions.extend(append_bytes(out, template, 4) for template in templates)
    struct.pack_into('<II', out, 4, size, len(positions) - size)
    struct.pack_into(f'<{len(positions)}I', out, 12, *positions)
    return bytes(out)


def pack_script(records):
    out = bytearray(4 + 4 * len(records))
    struct.pack_into('<I', out, 0, len(records))
    for index, raw in enumerate(records):
        struct.pack_into('<I', out, 4 + 4 * index, append_bytes(out, raw, 4))
    return bytes(out)


@dataclass(frozen=True)
class Move:
    target: int
    state: int
    source: int
    original: int
    clip: int
    source_clip: int
    next_state: int
    root: int = -1

    def record(self, address=None):
        row = [self.target, self.state, self.clip, self.source, self.original, self.source_clip]
        return row if address is None else [*row, address]



def move_event_key(move, member, shape, index):
    return move.target, move.root, move.source, move.original, move.source_clip, member, shape, index


def move_event_words(raw, last, node_count, sound_count):
    if raw[0] == 8:
        return [Word(2, '触发帧', 0, last)]
    # The target clip and template numbers are allocated when the project compiles.
    reserved = {SHAPE_TEMPLATE_OFFSETS.get(raw[0])}
    if raw[0] == 3:
        reserved.add(8)
    return [field for field in event_words(raw, False, last, node_count, 0, sound_count)
            if field.offset not in reserved]

class Compilation:
    def __init__(self, game, assignments):
        self.game = game
        self.assignments = assignments
        self.states = {}
        self.animations = defaultdict(dict)
        self.animation_keys = {}
        self.animation_defaults = {}
        self.scripts = defaultdict(dict)
        self.templates = defaultdict(list)
        self.clones = {}
        self.slots = []
        self.effects = []
        self.anm_tails = defaultdict(dict)
        self.descriptors = {}
        self.items = {}
        self.projections = {}
        self.next_clips = {}
        self.recipes = {}
        self.rigs = {}
        self.bind_poses = defaultdict(lambda: defaultdict(set))
        self.electric_roles = set()
        self.charge_anchors = {}
        self.private_defs = {}
        self.public_flags = defaultdict(dict)
        self.entry_routes = {}
        self.leo_throws = {}
        self.weapon_models = {}
        self.weapon_forms = {}
        self.jack_throws = {}
        self.taro_phases = {}
        self.taro_fire_enter = self.taro_fire_check = None
        self.normal_throws = {}
        self.contact_routes = defaultdict(dict)
        self.scene_states = {}
        self.effect_sources = {}
        self.overlays = {}
        self.overlay_clips = {}
        self.overlay_roots = {}
        self.animation_aliases = {}
        self.scene_effects = []
        self.camera_banks = {}
        self.camera_slices = {}
        self.camera_bindings = {}
        self.locator_models = {}
        self.locators = {}
        self.texture_keys = defaultdict(set)
        self.texture_usage = defaultdict(dict)
        self.event_defaults = {}
        if not assignments:
            return
        self.native = Eboot(game.pre_ai_eboot, game.base_layout, payload_limit(game.canonical_eboot,game.elf_layout))
        for (target, state), (source, original) in sorted(assignments.items()):
            self.root = state
            self.plan_move(target, state, source, original)
        if self.taro_phases:
            self.taro_fire_enter, self.taro_fire_check = taro_fire_callbacks(self.native)
        if self.leo_throws:
            self.prepare_defenders(5, (135, 136, 137))
        if self.jack_throws:
            self.prepare_defenders(2, (133, 134))
        if any(source == 12 for _, source in self.scene_states):
            self.prepare_defenders(12, (142,))
        if any(source == 11 for _, source in self.scene_states):
            self.prepare_defenders(11, (141,))
        if any(source == 14 for _, source in self.scene_states):
            self.prepare_defenders(14, (143,))
        if (0x08808374, 18) in self.contact_routes:
            self.prepare_defenders(16, (144,))
        if self.normal_throws:
            self.prepare_normal_defenders()
        forms = list(self.target_forms())
        for selection, binding in forms:
            target, config, shape = selection
            case, form, _, _ = binding
            rig = self.native_rig(selection, form)
            poses = self.bind_poses[case['anm_banks'][0]['id']]
            for track, node in rig.tracks.items():
                bone = rig.bones[node]
                angles = tuple(round(((math.degrees(a) + 180) % 360) * 65535 / 360)
                               for a in bone['rotation'])
                poses[track].add((angles, tuple(bone['translation']), tuple(bone['scale'])))
            if target in self.leo_throws:
                self.load_weapon_form(target, config, shape, binding, rig)
        for (target, config, shape), binding in forms:
            self.compile_form(target, config, shape, binding)
        if game.resumed:
            game._recovery.apply_resources(self)
        for bank, clips in self.animations.items():
            role = game.bank_owners[bank]
            for slot, original in clips.items():
                move = self.recipes.get((role, slot))
                if move is None:
                    continue
                key = role, move.root, move.source, move.original, move.source_clip, bank
                self.animation_keys[bank,slot] = key
                self.animation_defaults[bank,slot] = original
                if key in game.move_anm_edits:
                    edited = game.move_anm_edits[key]
                    validate_clip(edited, clip_ids(original))
                    clips[slot] = edited

    def target_forms(self):
        for role in sorted(self.next_clips):
            seen = set()
            for config, case in enumerate(self.game.roles[role]['cases']):
                if case['descriptor_va'] in seen:
                    continue
                seen.add(case['descriptor_va'])
                for shape in range(len(case['forms'])):
                    yield (role, config, shape), self.game.form_binding(role, config, shape)

    def original_move(self, role, state):
        found = self.game.actions[role].get(state)
        if found is None:
            raise ValueError('所选角色没有这个动作状态。')
        return found

    def new_clip(self, role):
        if role not in self.next_clips:
            values = []
            for config, case in enumerate(self.game.roles[role]['cases']):
                counts = int(case['counts_va'], 16) - BASE
                values.append(s16(self.game.image, counts + 36))
                for shape in range(len(case['forms'])):
                    _, _, bom, _ = self.game.form_binding(role, config, shape)
                    values.append(self.game.bank(bom['id'], b'BOM\0')[0])
            self.next_clips[role] = max(values)
        value = self.next_clips[role]
        self.next_clips[role] = value + 1
        return value

    def plan_move(self, target, state, source, original):
        recipient = self.original_move(target, state)
        donor = self.original_move(source, original)
        if recipient['category'] != donor['category'] or not can_mix_move(donor) or not can_mix_move(recipient):
            raise ValueError('请选择同一类型的攻击或技能。')
        if (target, state) == (source, original):
            raise ValueError('本来的动作可用「还原配招」恢复。')
        if donor['category'] == 11:
            self.plan_normal_throw(target, state, source, original)
            return
        if (source, original) == BABAR_DOWN:
            self.plan_babar_down(target, state)
            return
        if (source, original) == TARO_DOWN:
            self.plan_taro_down(target, state)
            return
        if (source, original) == LEO_FORWARD:
            self.plan_leo_forward(target, state)
            return
        if (source, original) == LEO_THROW:
            self.plan_leo_throw(target, state)
            return
        if (source, original) == JACK_THROW:
            self.plan_jack_throw(target, state)
            return
        if (source, original) in BACK_SKILLS:
            self.plan_back_skill(target, state, source, original)
            return
        category = donor['category']
        next_source = int(donor['parameter'], 16)
        next_target = next_source
        linked = False
        if next_source not in (0, 1, 0xFFFF):
            if ordinary_recovery(self.game.actions[source], donor):
                next_target = self.private_state(target, source, next_source)
            elif category == 5 and next_source == STATES.get(original) and state in STATES:
                next_target = STATES[state]
            else:
                raise ValueError('这个动作还依赖专用状态，当前无法移植。')
            linked = True
        enter = int(donor['callbacks'][0], 16)
        if enter not in (ORDINARY if category == 4 else SKILL):
            raise ValueError('这个动作的专用回调尚未对接通用配招。')
        family = [(state, original, next_target)]
        if linked:
            follow = self.original_move(source, next_source)
            if next_target not in self.game.native_actions[target] and (target,next_target) not in self.private_defs:
                raise ValueError('目标角色没有可供这个技能使用的收招状态。')
            if int(follow['parameter'], 16) not in (0, 1, 0xFFFF):
                raise ValueError('技能收招依赖额外的状态。')
            family.append((next_target, next_source, int(follow['parameter'], 16)))
        if enter == 0x0883A6A4:
            # Seven alternates the root and recovery clip with the adjacent pair.
            self.reserve(target, family, source, True)
        else:
            self.reserve(target, family, source, False)

    def private_state(self, target, source, original, inputs=None):
        state = FIRST_PRIVATE_STATE + sum(role == target for role, _ in self.private_defs)
        move = self.original_move(source, original)
        self.private_defs[target, state] = int(move['flags'], 16), inputs
        return state

    def preview_state(self, role, state):
        original = self.game.native_actions[role].get(state)
        if original is not None:
            return original
        move = self.states.get((role, state))
        if move is None:
            return None
        donor = self.original_move(move.source, move.original)
        return {**donor, 'id': state, 'va': '待写入', 'clip': move.clip,
                'parameter': f'0x{move.next_state:04X}'}

    def plan_leo_forward(self, role, state):
        source = LEO_FORWARD[0]
        image = self.game.image
        branch_row = next((row for row in state_branches(image, BASE, role, 0)
                           if row['target'] == state and 33 <= row['command'] <= 35), None)
        if branch_row is None:
            raise ValueError('请给雷欧飞踢选择能从站立直接按出的技能槽。')
        jump, wait, fall, recover = (self.private_state(role, source, phase, 21 if phase == 21 else None)
                                     for phase in LEO_STAGES)
        self.reserve(role, [(state, 158, fall), (jump, 181, wait), (wait, 182, state),
                            (fall, 21, recover), (recover, 24, 0)], source, False)
        self.public_flags[role][state] = int(self.original_move(source, 158)['flags'], 16)
        for origin in self.game.native_actions[role]:
            for row in state_branches(image, BASE, role, origin):
                if row['target'] == state and row['command'] == branch_row['command']:
                    self.entry_routes[row['va']] = jump
        # A separate ground and airborne record, if present, leaves the airborne
        # entry directly on the firing phase as in Leo's native move.
        grounded = {row['va'] for row in state_branches(image, BASE, role, 0)}
        for origin in (19, 20, 21):
            for row in state_branches(image, BASE, role, origin):
                if row['va'] not in grounded:
                    self.entry_routes.pop(row['va'], None)

    def plan_leo_throw(self, role, state):
        source, root = LEO_THROW
        if state != root:
            raise ValueError('双截棍投技需要放在「下 + ○」的技能槽。')
        phases = list(LEO_THROW_STAGES)
        states = [self.private_state(role, source, phase) for phase in phases]
        self.leo_throws[role] = states[0]
        self.public_flags[role][root] = int(self.original_move(source, root)['flags'], 16)
        chain = list(zip(states, phases, [*states[1:], 0]))
        self.reserve(role, [(root, root, root + 1), (root + 1, root + 1, 0), *chain], source, False)

    def plan_jack_throw(self, role, state):
        source, root = JACK_THROW
        if state != root:
            raise ValueError('杰克的抓取需要放在「下 + ○」的技能槽。')
        grab_front, grab_back = (self.private_state(role, source, phase) for phase in (181, 182))
        self.jack_throws[role] = grab_front, grab_back
        self.public_flags[role][root] = int(self.original_move(source, root)['flags'], 16)
        self.reserve(role, [(root, root, root + 1), (root + 1, root + 1, 0),
                            (grab_front, 181, 0), (grab_back, 182, 0)], source, False)
        self.reserve_encounter_aliases(role, source, (grab_front, grab_back))

    def plan_babar_down(self, role, state):
        if state != 160:
            raise ValueError('巴巴尔的命中追加需要放在「下 + ○」的技能槽。')
        pair = self.private_state(role, 16, 181)
        self.contact_routes[0x08808374, 18][role, state] = pair
        self.public_flags[role][state] = int(self.original_move(16, 160)['flags'], 16)
        self.reserve(role, [(state, 160, 161), (161, 161, 0), (pair, 181, 0)], 16, False)

    def plan_taro_down(self, role, state):
        source, root = TARO_DOWN
        if state != root:
            raise ValueError('泰罗的蓄力火焰需要放在「下 + ○」的技能槽。')
        linked = {phase: self.private_state(role, source, phase) for phase in TARO_PHASES}
        targets = {root: state, **linked}
        self.taro_phases[role] = linked
        transitions = []
        for phase, target in targets.items():
            donor = self.original_move(source, phase)
            follows = int(donor['parameter'], 16)
            transitions.append((target, phase, targets.get(follows, follows)))
            if phase in linked:
                commands = tuple((targets.get(row['target'], row['target']), row['command'],
                                  row['flags'], row['priority'])
                                 for row in state_branches(self.game.image, BASE, source, phase))
                if commands:
                    self.private_defs[role, target] = int(donor['flags'], 16), commands
        self.public_flags[role][state] = int(self.original_move(source, root)['flags'], 16)
        self.contact_routes[0x088083AC, 18][role, linked[183]] = linked[186]
        self.reserve(role, transitions, source, False)

    def plan_normal_throw(self, role, state, source, original):
        self.normal_throws[role, state] = source, original
        self.public_flags[role][state] = int(self.original_move(source, original)['flags'], 16)
        self.reserve(role, [(state, original, 0)], source, False)
        self.reserve_encounter_aliases(role, source, (state,))

    def prepare_normal_defenders(self):
        if (6, 176) in self.normal_throws.values():
            self.prepare_defenders(6, (132,))
        for receiver, definition in enumerate(self.game.roles):
            for donor, state in set(self.normal_throws.values()):
                clip = throw_victim_clip(self.game.image, receiver, donor, state)
                if clip < 0:
                    raise ValueError('这个投技缺少接收角色对应的受身片段。')
                for case in definition['cases']:
                    self.add_defender_clip(case, clip, donor)

    def plan_back_skill(self, role, state, source, original):
        if not any(row['command'] == 34 and row['target'] == state
                   for row in state_branches(self.game.image, BASE, role, 0)):
            raise ValueError('这个演出技能需要放在接收角色的「后 + ○」槽。')
        phases = BACK_SKILLS[source, original]
        private = [self.private_state(role, source, phase) for phase in phases]
        self.scene_states[role, source] = dict(zip(phases, private))
        self.public_flags[role][state] = int(self.original_move(source, original)['flags'], 16)
        targets = {original: state, **self.scene_states[role, source]}
        transitions = []
        for donor_state, receiving_state in targets.items():
            next_phase = int(self.original_move(source, donor_state)['parameter'], 16)
            transitions.append((receiving_state, donor_state, targets.get(next_phase, next_phase)))
        if source == 12:
            # The cutscene selects the paired attack after its own hit check.
            transitions[0] = state, original, 0xFFFF
            self.overlay_clips[role] = self.new_clip(role)
            self.overlay_roots[role] = state
        elif source == 14:
            transitions[0] = state, original, 0
            self.contact_routes[0x08808038, 16][role, state] = private[0]
        self.reserve(role, transitions, source, False)
        self.reserve_encounter_aliases(role, source, private)

    def reserve_encounter_aliases(self, role, source, states):
        for state in states:
            move = self.states[role, state]
            for source_clip in ACTION_CLIPS.get((source, move.original), ()):
                clip = self.new_clip(role)
                self.animation_aliases[role, state, source_clip] = clip
                self.recipes[role, clip] = Move(role, state, source, move.original, clip,
                                                source_clip, move.next_state, move.root)

    def prepare_defenders(self, donor, states):
        # Defense states keep their native IDs and BOM. Fill the encounter clips
        # when the normal opponent bank would belong to another character.
        for role, metadata in enumerate(self.game.roles):
            for case in metadata['cases']:
                for state in states:
                    self.add_defender_clip(case, self.original_move(role, state)['clip'], donor)

    def add_defender_clip(self, case, clip, source):
        main = case['anm_banks'][0]['id']
        bank, slot, opponent = self.game.anim_bank(case, clip)
        if opponent is None:
            return
        if opponent != source:
            raise ValueError('受身动作缺少对应角色的原生对手动画。')
        _, _, parts = self.game.bank(bank, b'ANM\0')
        data = parts[slot]
        anm_header(data)
        self.animations[main][clip] = data

    def load_weapon_form(self, role, config, shape, binding, target_rig):
        case, form, _, textures = binding
        _, source_form, _, _ = self.game.form_binding(5, 0, 0)
        donor = self.native_rig((5, 0, 0), source_form)
        member = form['mdl']['id']
        bindings = self.game.node_bindings(case, shape)
        if set(LEO_WEAPON_TRACKS) & target_rig.tracks.keys():
            raise ValueError('目标形态的双截棍轨道与原动作重叠。')
        first = len(target_rig.bones)
        hand = self.map_node(donor, target_rig, 53)
        raw = self.game.read_member(member, b'MDL\0')
        source = self.game.read_member(source_form['mdl']['id'], b'MDL\0')
        graft = graft_weapon(target_rig.model, raw, donor.model, source, hand, len(textures))
        if self.weapon_models.setdefault(member, graft) != graft:
            raise ValueError('共用的 MDL 存在两套不同的双截棍挂接。')
        read_mdl(graft)
        palette = donor.model['materials'][8]['variant']
        slot = self.move_texture_slot(case, role, 160, LEO_WEAPON_POM, shape=shape, palettes=(palette,))
        slots = [entry['slot'] for entry in textures] + [slot]
        bindings.extend((-1, first + i, track) for i, track in enumerate(LEO_WEAPON_TRACKS))
        self.weapon_forms[int(case['descriptor_va'], 16), shape] = case, first, slots, bindings

    def reserve(self, target, family, source, alternate):
        for is_alt in range(2 if alternate else 1):
            for state, original, next_state in family:
                donor = self.original_move(source, original)
                clip = self.new_clip(target)
                source_clip = donor['clip'] + 2 * is_alt
                move = Move(target, state, source, original, clip, source_clip, next_state, self.root)
                self.recipes[target, clip] = move
                if is_alt:
                    continue
                key = target, state
                if key in self.states:
                    raise ValueError('两项配招占用了同一个发动或收招状态。')
                self.states[key] = move

    def native_rig(self, selection, form):
        if selection not in self.rigs:
            member = form['mdl']['id']
            self.rigs[selection] = self.game.model_rig(
                read_mdl(self.game.read_member(member, b'MDL\0')), selection)
        return self.rigs[selection]

    @staticmethod
    def map_node(from_rig, to_rig, node):
        if node < 0:
            return node
        if node >= len(from_rig.bones):
            raise ValueError(f'来源动作引用了无效的 MDL 节点 {node}。')
        for side, slots in enumerate(from_rig.hands):
            if node in slots:
                nodes = to_rig.hands[side]
                dst = nodes[slots.index(node)]
                if dst >= 0:
                    return dst
        tracks = {index: track for track, index in from_rig.tracks.items()}
        track = tracks.get(node)
        if track in to_rig.tracks and track not in (77, 78, 79, 80):
            return to_rig.tracks[track]
        if track in (30, 31):
            for head in (14, 13, 10):
                if head in to_rig.tracks:
                    return to_rig.tracks[head]
        if track is not None and track >= 25 and track not in (30, 31):
            raise ValueError(f'原动作需要独立动画节点 {track}，当前模型缺少可移植部件。')
        ancestor = from_rig.bones[node]['parent']
        while ancestor >= 0:
            track = tracks.get(ancestor)
            if track in to_rig.tracks and track <= 22:
                return to_rig.tracks[track]
            ancestor = from_rig.bones[ancestor]['parent']
        if track in (13, 14):
            return to_rig.tracks.get(13, to_rig.tracks[10])
        raise ValueError(f'来源 MDL 节点 {node} 无法映射到目标骨架。')

    def clip_data(self, case, clip, encounter=False):
        member, slot, opponent = self.game.anim_bank(case, clip)
        if opponent is not None and not encounter:
            raise ValueError('这个动作使用了对手专属的动画库。')
        count, _, parts = self.game.bank(member, b'ANM\0')
        if not 0 <= slot < count or not parts[slot]:
            raise ValueError('来源角色缺少需要的 ANM。')
        data = parts[slot]
        anm_header(data)
        return data

    def original_events(self, member, clip, stock=False):
        count, _, parts = self.game.bank(member, b'BOM\0')
        if not 0 <= clip < count or not parts[clip]:
            raise ValueError(f'YUM {member} 没有这个动作的 BOM。')
        patches = {} if stock else self.game.edits.get((member, clip), {})
        for event in event_records(parts[clip]):
            yield event, patches[event.index].edited if event.index in patches else event.raw

    def append_template(self, dest, src, index):
        count, _, parts = self.game.bank(src, b'BOM\0')
        if not 0 <= index < len(parts) - count or not parts[count + index]:
            raise ValueError('来源动作引用了无效的碰撞模板。')
        data = parts[count + index]
        count, _, target = self.game.bank(dest, b'BOM\0')
        templates = self.templates[dest]
        if data in templates:
            return len(target) - count + templates.index(data)
        templates.append(data)
        return len(target) - count + len(templates) - 1

    def original_body(self, role, case, bom, shape):
        idle = self.game.actions[role][0]['clip']
        for _, raw in self.original_events(bom, idle):
            if raw[0] in BODY_EVENTS and raw[1] & (1 << shape):
                yield raw[:1] + bytes([1 << shape]) + raw[2:]

    def effect_atlas(self, target_case, source_case, move, effect, shape):
        counts = int(target_case['counts_va'], 16)
        stock = s16(self.game.image, counts - BASE + 8)
        members = tuple(texture['id'] for texture in source_case['common_poms'])
        used = COMMON_SLOTS[effect]
        if any(index >= len(members) for index in used):
            raise ValueError('技能缺少来源特效贴图。')
        banks = self.descriptors.setdefault(target_case['descriptor_va'], (target_case, {}))[1]
        key = move.target, move.root, members
        palettes = DRAW_PALETTES[effect]
        slots = tuple(self.move_texture_slot(target_case, move.target, move.root, member,
                                             shape=shape, visible=index in used, palettes=palettes.get(index, ()))
                      for index, member in enumerate(members))
        if key not in banks:
            start = stock + sum(len(poms) for _, poms in banks.values())
            banks[key] = start, slots
        return banks[key][0]

    def move_texture_slot(self, case, role, root, member, *, shape=None, visible=True, palettes=()):
        key = role, root, member
        descriptor = case['descriptor_va']
        self.texture_keys[descriptor].add(key)
        if visible:
            forms = self.texture_usage[descriptor].setdefault(key, {})
            forms.setdefault(shape, set()).update(palettes)
        return self.resource_slot(case, member, b'POM\0', key)

    def move_textures(self, case, shape):
        usage = self.texture_usage.get(case['descriptor_va'], {})
        return {key: forms.get(None, set()) | forms.get(shape, set())
                for key, forms in usage.items() if None in forms or shape in forms}

    def model_texture_slots(self, case, model, members, role, root, shape=None):
        mdl = read_mdl(self.game.read_member(model, b'MDL\0'))
        palette_uses = defaultdict(set)
        for index in {material for mesh in mdl['meshes'] for material, _ in mesh['faces']}:
            material = mdl['materials'][index]
            palette_uses[material['texture']].add(material['variant'])
        return tuple(self.move_texture_slot(case, role, root, member, shape=shape,
                                            visible=slot in palette_uses, palettes=palette_uses.get(slot, ()))
                     for slot, member in enumerate(members))

    def resource_slot(self, case, member, magic, texture=None):
        anm = case['anm_banks'][0]
        tails = self.anm_tails[anm['id']]
        key = member, magic, texture
        if key not in tails:
            tails[key] = len(self.slots)
            self.slots.append((anm['slot'], None))
        return SLOT_BASE + tails[key]

    def item_count(self, case):
        counts = int(case['counts_va'], 16) - BASE
        count = s16(self.game.image, counts)
        if count == 1:
            descriptor = int(case['descriptor_va'], 16) - BASE
            models = u32(self.game.image, descriptor + 4) - BASE
            if s16(self.game.image, models) == -1:
                return 0
        return count

    def borrow_item(self, source_case, target_case, index, move, shape):
        group = self.game.yum['groups'][source_case['group']]
        item = next((model for model in item_models(self.game.image, BASE, source_case, group)
                     if model['slot'] == index), None)
        if item is None or any(member is None for member in item['poms']):
            raise ValueError('来源角色的附属模型缺少 MDL 或 POM。')
        key = item['member'], tuple(item['poms']), move.target, move.root
        models = self.items.setdefault(target_case['descriptor_va'], (target_case, {}))[1]
        model_slot = self.resource_slot(target_case, key[0], b'MDL\0')
        slots = self.model_texture_slots(target_case, key[0], key[1], move.target, move.root, shape)
        if key not in models:
            models[key] = self.item_count(target_case) + len(models), model_slot, slots
        return models[key][0]

    def grip_node(self, case, shape, node, source_rig, target_rig):
        bone = source_rig.bones[node]
        parent = self.map_node(source_rig, target_rig, bone['parent'])
        member = case['forms'][shape]['mdl']['id']
        return self.add_locator(member, bone, parent)

    def add_locator(self, member, bone, parent):
        key = member, parent, bone['raw'][:96]
        if key not in self.locators:
            raw = self.locator_models.get(member) or self.weapon_models.get(member)
            if raw is None:
                raw = self.game.read_member(member, b'MDL\0')
            index, model = graft_locator(raw, bytes.fromhex(bone['raw']), parent)
            self.locators[key] = index
            self.locator_models[member] = model
        return self.locators[key]

    def effect_node(self, case, shape, source_rig, target_rig, node):
        if node < 0 or source_rig is target_rig:
            return node
        node = source_rig.matrix_sources.get(node, node)
        bone = source_rig.bones[node]
        parent = bone['parent']
        if node in source_rig.driven_nodes or parent < 0:
            return self.map_node(source_rig, target_rig, node)
        parent = self.effect_node(case, shape, source_rig, target_rig, parent)
        member = case['forms'][shape]['mdl']['id']
        return self.add_locator(member, bone, parent)

    def baxium_shot(self, atlas):
        key = ('baxium shot', atlas)
        if key not in self.clones:
            self.clones[key] = self.native.clone(0x088846D0, 0x08884770,
                                                 texture_words=(0x08884704,), slot=atlas)
        return self.clones[key]

    def eye_slugger(self, source_case, target_case, source_rig, target_rig, shape, move):
        # Seven's effect object owns the mount from launch until the animation changes.
        # The source body also has a second copy of the slugger on its head.
        # Foreign bodies only need the separate object and its hand mount.
        node = self.grip_node(target_case, shape, 37, source_rig, target_rig)
        slot = self.borrow_item(source_case, target_case, ITEM_SLOTS[25], move, shape)
        key = (25, node, slot)
        if key in self.clones:
            return self.clones[key]
        eboot = self.native
        visibility = eboot.code([lw(T4, 52, A0), lw(T5, 4, T4),
                                 branch(4, T5, Z, 2), 0, iword(40, T4, A1, 116), jr(), 0])
        activate = eboot.clone(0x0883CEF0, 0x0883CF34, immediates=((0x0883CF14, slot * 176),),
                               calls={0x0883D044: visibility})
        follow = eboot.clone(0x0883CF34, 0x0883D044, node_words=((0x0883D008, node),))
        update = eboot.clone(0x0883CDA0, 0x0883CE18,
                             calls={0x0883CEF0: activate, 0x0883CF34: follow, 0x0883D044: visibility})
        render = eboot.clone(0x0883CE18, 0x0883CEF0, node_words=((0x0883CECC, node),))
        vt = eboot.allocate(bytes(24))
        stock = 0x0893D8A8
        for offset, override in ((0, None), (4, None), (8, None), (12, update), (16, render)):
            eboot.linked(vt + offset, override or u32(self.game.image, stock - BASE + offset), 2)
        factory = eboot.clone(0x08880F54, 0x08880FD8, pointers=((0x08880FA0, 0x08880FA4, vt),))
        self.clones[key] = EFFECT_BASE + len(self.effects)
        self.effects.append(factory)
        return self.clones[key]

    def clone_effect(self, effect, source_case, target_case, source_rig, target_rig, move, shape):
        if effect in COMMON_EFFECTS:
            return effect
        if effect in (70, 71, 72, 73) and move.target != 11:
            self.electric_roles.add(move.target)
        if effect == 70:
            if move.target == 11:
                return effect
            root = target_rig.tracks.get(0, 0)
            tracks = (0, 2, 7, 8, 3, 4, 5, 10, 14, 17, 21)
            anchors = [target_rig.tracks.get(track, target_rig.tracks.get(13, root) if track == 14 else root)
                       for track in tracks]
            self.charge_anchors[int(target_case['descriptor_va'], 16), shape] = anchors
            key = effect, self.effect_atlas(target_case, source_case, move, effect, shape)
            if key not in self.clones:
                self.clones[key] = EFFECT_BASE + len(self.effects)
                self.effects.append(charge_factory(self.native, key[1]))
            return self.clones[key]
        if effect == 73:
            shot = self.clone_effect(72, source_case, target_case, source_rig, target_rig, move, shape)
            key = effect, shot
            if key not in self.clones:
                self.clones[key] = EFFECT_BASE + len(self.effects)
                self.effects.append(self.native.clone(
                    0x088839EC, 0x08883A60, calls={0x08883814: self.effects[shot - EFFECT_BASE]}))
            return self.clones[key]
        if effect in SCENE_ROLES and move.source == SCENE_ROLES[effect]:
            return self.scene_skill_effect(effect, source_case, target_case, source_rig, target_rig, move, shape)
        if effect == 25:
            if move.source == move.target:
                return effect
            return self.eye_slugger(source_case, target_case, source_rig, target_rig, shape, move)
        if effect == 26:
            return effect
        if effect in BEAM_TRAILS:
            start = BEAM_TRAILS[effect]
            node = self.effect_node(target_case, shape, source_rig, target_rig, 52)
            atlas = self.effect_atlas(target_case, source_case, move, effect, shape)
            key = effect, node, atlas, move.state
            if key not in self.clones:
                helper = self.native.clone(0x08885F70, 0x08886004,
                    texture_words=(0x08885F98,), slot=atlas, immediates=((0x08885F94, node),))
                state = move.state if move.state >= 0 else move.original
                self.clones[key] = EFFECT_BASE + len(self.effects)
                self.effects.append(self.native.clone(start, start + 44,
                    calls={0x08885F70: helper}, immediates=((start + 28, state),)))
            return self.clones[key]
        if effect in (94, 95, 110, 111):
            return self.creature_effect(effect, source_case, target_case, source_rig, target_rig, move, shape)
        if effect == 45 and move.target in self.taro_phases:
            atlas = self.effect_atlas(target_case, source_case, move, effect, shape)
            anchors = tuple((site, self.effect_node(target_case, shape, source_rig, target_rig,
                                                   (u32(self.game.image, site - BASE) & 0xFFFF) // 544))
                            for site in TARO_FIRE_NODES)
            first = self.taro_phases[move.target][181]
            key = effect, anchors, atlas, first
            if key not in self.clones:
                code = taro_flames(self.native, self.game.image, anchors, atlas, self.taro_fire_check, first)
                self.clones[key] = EFFECT_BASE + len(self.effects)
                self.effects.append(code)
            return self.clones[key]
        if move.source == 11 and effect in (75, 76, 82):
            if effect == 82:
                return effect
            atlas = self.effect_atlas(target_case, source_case, move, effect, shape)
            key = effect, atlas
            if key not in self.clones:
                helper = self.native.clone(0x08885D94, 0x08885E24, texture_words=(0x08885DDC,), slot=atlas)
                start = 0x08883AB4 if effect == 75 else 0x08883AD8
                code = self.native.clone(start, start + 36, calls={0x08885D94: helper})
                self.clones[key] = EFFECT_BASE + len(self.effects)
                self.effects.append(code)
            return self.clones[key]
        if effect == 90:
            atlas = self.effect_atlas(target_case, source_case, move, effect, shape)
            node = self.effect_node(target_case, shape, source_rig, target_rig, 31)
            key = effect, atlas, node
            if key not in self.clones:
                shine = self.native.clone(0x08884378, 0x088846D0, texture_words=(0x08884494, 0x088845B4), slot=atlas)
                code = self.native.clone(0x08883F58, 0x08883FAC,
                                         immediates=((0x08883F60, node), (0x08883F90, node)),
                                         calls={0x08884378: shine, 0x088846D0: self.baxium_shot(atlas)})
                self.clones[key] = EFFECT_BASE + len(self.effects)
                self.effects.append(code)
            return self.clones[key]
        if effect in (65, 67, 68):
            atlas = self.effect_atlas(target_case, source_case, move, effect, shape)
            if effect == 65:
                start, end = 0x08882FDC, 0x08883008
                nodes = tuple(self.effect_node(target_case, shape, source_rig, target_rig, node)
                              for node in (45, 33))
                state = move.state if move.state >= 0 else move.original
                key = effect, nodes, atlas, state
                if key not in self.clones:
                    helper = self.native.clone(0x08883008, 0x08883310, texture_words=(0x08883098,), slot=atlas,
                                               immediates=((0x088832AC, state),))
                    code = self.native.clone(start, end, immediates=tuple(zip((0x08882FEC, 0x08882FF0), nodes)),
                                             calls={0x08883008: helper})
                    self.clones[key] = EFFECT_BASE + len(self.effects)
                    self.effects.append(code)
                return self.clones[key]
            node = self.effect_node(target_case, shape, source_rig, target_rig, 28 if effect == 67 else 27)
            start = 0x08883364 if effect == 67 else 0x088833B8
            key = effect, node, atlas
            if key not in self.clones:
                first = self.native.clone(0x0888340C, 0x088835C0, texture_words=(0x08883534,), slot=atlas)
                second = self.native.clone(0x08883614, 0x088836B4, texture_words=(0x08883640,), slot=atlas)
                sites = tuple((site, node) for site in range(start, start+84, 4)
                              if u32(self.game.image, site - BASE) == (0x24070000 | (28 if effect == 67 else 27)))
                code = self.native.clone(start, start+84, immediates=sites,
                                         calls={0x0888340C: first, 0x08883614: second})
                self.clones[key] = EFFECT_BASE + len(self.effects)
                self.effects.append(code)
            return self.clones[key]
        if effect in NODE_EFFECTS:
            start, helper, end, texture_site, node = NODE_EFFECTS[effect]
            atlas = self.effect_atlas(target_case, source_case, move, effect, shape)
            node = self.effect_node(target_case, shape, source_rig, target_rig, node)
            key = effect, node, atlas
            if key not in self.clones:
                callee = self.native.clone(helper, end, texture_words=(texture_site,), slot=atlas)
                factory = self.native.clone(start, start + 28, immediates=((start + 12, node),),
                                            calls={helper: callee})
                self.clones[key] = EFFECT_BASE + len(self.effects)
                self.effects.append(factory)
            return self.clones[key]
        if effect not in EFFECTS:
            raise ValueError(f'技能特效 {effect} 仍依赖原角色的专用资源。')
        start, end, node_sites, texture_sites = EFFECTS[effect]
        targets = []
        for site in node_sites:
            field = u32(self.game.image, site - BASE) & 0xFFFF
            node = field // 544
            if effect == 49 and site == 0x0888208C:
                # Leo takes the second point of the firing direction from the
                # forearm's shadow node, whose motion source is logical track 21.
                node = source_rig.tracks[21]
            targets.append((site, self.effect_node(target_case, shape, source_rig, target_rig, node)))
        atlas = self.effect_atlas(target_case, source_case, move, effect, shape) if texture_sites else 0
        changes = []
        if move.state >= 0:
            for site in EFFECT_STATES.get(effect, ()):
                if u32(self.game.image, site - BASE) & 0xFFFF == move.original:
                    changes.append((site, move.state))
        if effect == 87:
            changes.append((0x08883FB0, self.effect_node(target_case, shape, source_rig, target_rig, 31)))
        roots = ((EFFECT_ROOT_LOADS[effect], target_rig.tracks[0]),) if effect in EFFECT_ROOT_LOADS else ()
        key = effect, tuple(targets), atlas, tuple(changes), roots
        if key not in self.clones:
            calls = {0x088846D0: self.baxium_shot(atlas)} if effect == 87 else None
            code = self.native.clone(start, end, targets, texture_sites, atlas, immediates=changes,
                                     node_loads=roots, calls=calls)
            self.clones[key] = EFFECT_BASE + len(self.effects)
            self.effects.append(code)
        return self.clones[key]

    def creature_effect(self, effect, source_case, case, source_rig, target_rig, move, shape):
        eboot = self.native
        if effect in (94, 95, 111):
            atlas = self.effect_atlas(case, source_case, move, effect, shape)
        if effect == 94:
            root = target_rig.tracks[0]
            key = effect, move.target, root, atlas
            if key not in self.clones:
                factory = hipporit_capture(eboot, self.game.image, move.target, root, atlas)
        elif effect == 95:
            slot = self.borrow_item(source_case, case, ITEM_SLOTS[effect], move, shape)
            key = effect, slot, atlas
            if key not in self.clones:
                factory = hipporit_chamber(eboot, self.game.image, slot, atlas)
        elif effect == 110:
            slot = self.borrow_item(source_case, case, ITEM_SLOTS[effect], move, shape)
            node = self.grip_node(case, shape, 57, source_rig, target_rig)
            routes = self.contact_routes.get((0x08808374, 18), {})
            phase = routes.get((move.target, move.root), 181)
            key = effect, slot, node, phase
            if key not in self.clones:
                factory = babar_chain(eboot, self.game.image, slot, node, phase)
        else:
            node = self.effect_node(case, shape, source_rig, target_rig, 50)
            root = target_rig.tracks[0]
            key = effect, node, root, atlas
            if key not in self.clones:
                factory = eboot.clone(0x08885708, 0x088857F0, ((0x08885760, node),), (0x0888575C,), atlas,
                                      node_loads=((0x08885758, root),))
        if key not in self.clones:
            self.clones[key] = EFFECT_BASE + len(self.effects)
            self.effects.append(factory)
        return self.clones[key]

    def scene_skill_effect(self, effect, source_case, case, source_rig, target_rig, move, shape):
        eboot = self.native
        role = move.target
        states = self.scene_states[role, move.source]
        event, overlay, body_node = None, None, None
        if effect == 84:
            overlay = len(case['forms']), self.overlay_clips[role]
        elif effect == 74:
            self.electric_roles.add(role)
        else:
            atlas = self.effect_atlas(case, source_case, move, effect, shape)
            if effect == 33:
                slot = self.borrow_item(source_case, case, ITEM_SLOTS[effect], move, shape)
                node = self.grip_node(case, shape, 15, source_rig, target_rig)
                factory_key = effect, slot, atlas, node
                if factory_key not in self.clones:
                    factory = jack_scene_factory(eboot, self.game.image, node, atlas, slot)
                    self.clones[factory_key] = SCENE_EFFECT_BASE + len(self.scene_effects)
                    self.scene_effects.append(factory)
            else:
                start, end, anchors, textures = SCENE_FACTORIES[effect]
                nodes = tuple((site, self.effect_node(case, shape, source_rig, target_rig,
                                                     (u32(self.game.image, site - BASE) & 0xFFFF) // 544))
                              for site in anchors)
                if effect in (24, 89):
                    body_node = self.map_node(source_rig, target_rig, 21 if effect == 24 else 28)
                roots = ((EFFECT_ROOT_LOADS[effect], target_rig.tracks[0]),) if effect in EFFECT_ROOT_LOADS else ()
                factory_key = effect, atlas, nodes, roots
                if factory_key not in self.clones:
                    factory = eboot.clone(start, end, nodes, textures, atlas, node_loads=roots)
                    self.clones[factory_key] = SCENE_EFFECT_BASE + len(self.scene_effects)
                    self.scene_effects.append(factory)
            event = self.clones[factory_key]
        camera = self.camera_bank(case, move.source)
        key = 'scene', effect, tuple(states.items()), event, overlay, camera, body_node
        if key not in self.clones:
            self.clones[key] = EFFECT_BASE + len(self.effects)
            self.effects.append(clone_scene(eboot, self.game.image, effect, states, event, overlay, camera, body_node))
        return self.clones[key]

    def compile_black_overlay(self, target, case, bom):
        donor, form, source_bom, textures = self.game.form_binding(*BLACK_SCENE_SOURCE)
        desc = int(case['descriptor_va'], 16)
        member = form['mdl']['id']
        mdl = self.resource_slot(case, member, b'MDL\0')
        poms = self.model_texture_slots(case, member, (entry['id'] for entry in textures),
                                        target, self.overlay_roots[target])
        shape = BLACK_SCENE_SOURCE[2]
        self.overlays[desc] = case, mdl, poms, self.game.node_bindings(donor, shape)
        clip = self.overlay_clips[target]
        member = case['anm_banks'][0]['id']
        self.animations[member][clip] = self.clip_data(donor, BLACK_AUX_CLIP)
        records = self.scripts[bom['id']].setdefault(clip, [])
        mask = 1 << len(case['forms'])
        if any(raw[1] == mask for raw in records):
            return
        for event, original in self.original_events(source_bom['id'], BLACK_AUX_CLIP, stock=True):
            if not original[1] & (1 << shape):
                continue
            if event.overlap or original[0] not in (2, 10, 14, 16, 30):
                raise ValueError('布莱克王演出模型的 BOM 事件超出已适配范围。')
            raw = bytearray(original)
            raw[1] = mask
            if raw[0] in BODY_EVENTS:
                offset = SHAPE_TEMPLATE_OFFSETS[raw[0]]
                template = self.append_template(bom['id'], source_bom['id'], s16(raw, offset))
                struct.pack_into('<h', raw, offset, template)
            if raw[0] != 14 or self.sound_available(case, s16(raw, 4)):
                records.append(bytes(raw))

    def camera_bank(self, case, source):
        descriptor = int(case['descriptor_va'], 16)
        key = descriptor, source
        if key not in self.camera_slices:
            donor = self.game.roles[source]['cases'][0]['camera_assets']
            _, slots = self.camera_banks.setdefault(descriptor, (case, []))
            counts = int(case['counts_va'], 16) - BASE
            first = s16(self.game.image, counts + 10) + len(slots)
            if first + len(donor) > 256:
                raise ValueError('此出场配置的配招镜头编号超出范围。')
            slots.extend(self.resource_slot(case, entry['id'], b'CMSP') for entry in donor)
            self.camera_slices[key] = first, len(donor)
        return self.camera_slices[key]

    def compile_form(self, target, config, shape, binding):
        case, form, bom, _ = binding
        member = case['anm_banks'][0]['id']
        dest = bom['id']
        target_rig = self.native_rig((target, config, shape), form)
        if shape == 0:
            descriptor = int(case['descriptor_va'], 16)
            for move in self.states.values():
                if move.target == target and self.original_move(move.source, move.original)['category'] in (5, 11):
                    self.camera_bindings[descriptor, move.state] = self.camera_bank(case, move.source)
        if target in self.overlay_clips and shape == 0:
            self.compile_black_overlay(target, case, bom)
        queue = deque(move for (role, _), move in self.recipes.items() if role == target)
        while queue:
            move = queue.popleft()
            is_throw = (move.source == LEO_THROW[0] and target in self.leo_throws
                        and (move.original in LEO_THROW_STAGES or move.original in (160, 161)))
            source_shape = 0 if is_throw else min(shape, len(self.game.roles[move.source]['cases'][0]['forms']) - 1)
            source_case, source_form, source_bom, _ = self.game.form_binding(move.source, 0, source_shape)
            source_rig = self.native_rig((move.source, 0, source_shape), source_form)
            old_clip = move.source_clip
            new_clip = move.clip
            clips = self.animations[member]
            if new_clip not in clips:
                alias = old_clip in ACTION_CLIPS.get((move.source, move.original), ())
                data = self.clip_data(source_case, old_clip, encounter=alias)
                if is_throw and move.original in LEO_THROW_STAGES:
                    data = weapon_clip(data)
                # EvaluateAnmPose only overwrites the listed tracks. Give
                # recipient-only joints the same bind pose in game and preview.
                clips[new_clip] = with_bind_tracks(data, self.bind_poses[member])
            data = clips[new_clip]
            records = self.scripts[dest].setdefault(new_clip, [])
            if any(raw[1] & (1 << shape) for raw in records):
                continue
            first_event = len(records)
            for event, original in self.original_events(source_bom['id'], old_clip, stock=True):
                opcode, mask = original[:2]
                if not mask & (1 << source_shape) or opcode in BODY_EVENTS:
                    continue
                if event.overlap or opcode > 0x20:
                    raise ValueError('来源动作包含结构异常的帧事件。')
                raw = bytearray(original)
                raw[1] = 1 << shape
                if opcode in NODE_FIELDS:
                    offset = NODE_FIELDS[opcode]
                    struct.pack_into('<h', raw, offset,
                                     self.map_node(source_rig, target_rig, s16(raw, offset)))
                if opcode in SHAPE_TEMPLATE_OFFSETS:
                    offset = SHAPE_TEMPLATE_OFFSETS[opcode]
                    index = self.append_template(dest, source_bom['id'], s16(raw, offset))
                    struct.pack_into('<h', raw, offset, index)
                if opcode == 8:
                    original_fx = s16(raw, 4)
                    new_fx = self.clone_effect(original_fx, source_case, case, source_rig, target_rig, move, shape)
                    self.effect_sources[new_fx] = original_fx
                    struct.pack_into('<h', raw, 4, new_fx)
                if opcode == 3:
                    next_clip = s16(raw, 8)
                    if next_clip >= 0:
                        key = (target, move.root, move.source, next_clip)
                        if key not in self.projections:
                            linked = Move(target, -1, move.source, move.original,
                                          self.new_clip(target), next_clip, 0, move.root)
                            self.projections[key] = linked
                            self.recipes[target, linked.clip] = linked
                            queue.append(linked)
                        struct.pack_into('<h', raw, 8, self.projections[key].clip)
                if opcode in (14, 15) and not self.sound_available(case, s16(raw, 4)):
                    continue
                records.append(bytes(raw))
            records.extend(self.original_body(target, case, dest, shape))
            last = anm_header(data)[3]
            for index in range(first_event, len(records)):
                raw = records[index]
                key = move_event_key(move, dest, shape, index - first_event)
                updates = self.game.move_event_edits.get(key)
                if updates:
                    self.event_defaults[key] = raw
                    fields = move_event_words(raw, last, len(target_rig.bones), self.game.sound_count)
                    records[index] = change_words(raw, fields, updates, ())

    def sound_available(self, case, cue):
        if not self.game.sound_in_case(case,cue):
            return False
        code = self.game.sound_code(cue)
        if code is None or (code >> 16) not in (7, 8):
            return True
        voice = case.get('voice_bank')
        if voice is None:
            return False
        return find_sample(self.game.sound_bank(voice['id']), (code >> 8) & 255, code & 255) is not None

    def preview(self, role, config, shape, state, clip):
        move = self.recipes.get((role, clip))
        root = self.states.get((role, state))
        if move is None or root is None or (move.state != state and (move.state != -1 or move.root != root.root)):
            return None
        case, _, bom, _ = self.game.form_binding(role, config, shape)
        member = case['anm_banks'][0]['id']
        dest = bom['id']
        count, _, parts = self.game.bank(dest, b'BOM\0')
        templates = [self.game.template_edits.get((dest, index), data)
                     for index, data in enumerate(parts[count:])]
        return move, member, dest, self.animations[member][clip], self.scripts[dest][clip], [
            *templates, *self.templates[dest],
        ]

    def emit(self, replacements, patches):
        game, eboot = self.game, self.native
        bases = []
        for member in self.weapon_models.keys() | self.locator_models.keys():
            raw = game.read_member(member, b'MDL\0')
            bases.append([member,struct.unpack_from('<H', raw, 14)[0],len(raw)])
        replacements.update(self.weapon_models)
        replacements.update(self.locator_models)
        weapon_callbacks = {}
        black_callbacks = None
        overlays, attachments = install_model_forms(eboot, game, self.overlays, self.weapon_forms)
        initialize = overlay_initializer(eboot, overlays) if overlays else INIT_FORM
        if overlays:
            black_callbacks = black_pair_callbacks(eboot)
        if self.leo_throws:
            visibility = install_weapon_visibility(eboot, attachments, initialize)
            weapon_callbacks = weapon_state_callbacks(eboot, game, visibility)
        elif overlays:
            eboot.redirect(0x0888C968, initialize, call=True)
        if self.leo_throws or self.jack_throws or self.taro_phases:
            taro_grabs = {role: (phases[184], phases[186]) for role, phases in self.taro_phases.items()}
            install_throw_entry(eboot, game, self.leo_throws, self.jack_throws, taro_grabs)
        taro_timeouts = {role: taro_timer_callbacks(eboot, phases[186]) for role, phases in self.taro_phases.items()}
        if self.taro_phases:
            install_taro_hit_routes(eboot, {role: phases[183] for role, phases in self.taro_phases.items()})
        install_animation_aliases(eboot, self.animation_aliases)
        install_scene_effects(eboot, self.scene_effects)
        install_contact_routes(eboot, self.contact_routes)
        if (0x08808374, 18) in self.contact_routes:
            install_babar_chain_update(eboot)
        install_throw_victims(eboot, game.image, self.normal_throws)
        babar_exit = restore_babar_partner(eboot) if any(donor == 16 for _, donor in self.scene_states) else None
        for member, clips in self.animations.items():
            data = replacements.get(member)
            if data is None:
                data = game.read_source_member(member, b'ANM\0')
            bases.append([member,u32(data,4),len(data)])
            expanded = expand_anm(data, clips)
            for (asset, magic, texture), slot in self.anm_tails.get(member, {}).items():
                anchor, _ = self.slots[slot]
                if texture:
                    key = (*texture,member) if (game.keeps_move_texture(texture, self.assignments)
                          and member in game.installed_move_textures.get(texture, {})) else texture
                    data = game.move_texture_data(key)
                else:
                    data = game.read_member(asset, magic)
                # A zero flag uploads to the stock TBP/CBP addresses, which
                # belong to the original fighters and HUD. Borrowed v8 POMs
                # keep their palette and pixels in the loaded group instead.
                if magic == b'POM\0' and s16(data, 12) >= 8 and not data[14]:
                    data = data[:14] + b'\x01' + data[15:]
                offset = append_bytes(expanded, data)
                self.slots[slot] = anchor, offset
            replacements[member] = bytes(expanded)
        for member, scripts in self.scripts.items():
            base = game.member_offset(member)
            raw = replacements.get(member)
            data = bytearray(raw if raw is not None else game.read_source_member(member, b'BOM\0'))
            stock_size = game.yum['files'][member][1]
            rest = []
            for patch in patches:
                rel = patch.offset - base
                if 0 <= rel < stock_size:
                    if data[rel:rel + len(patch.original)] != patch.original:
                        raise ValueError('配招事件与同时修改的原 BOM 字节重叠。')
                    data[rel:rel + len(patch.original)] = patch.edited
                else:
                    rest.append(patch)
            patches[:] = rest
            bases.append([member,*struct.unpack_from('<2I',data,4),len(data)])
            replacements[member] = expand_bom(bytes(data), {i: pack_script(raw) for i, raw in scripts.items()},
                                              self.templates[member])
        for descriptor, (case, atlases) in self.descriptors.items():
            ptr = int(descriptor, 16) + 12
            counts = int(case['counts_va'], 16) + 8
            stock = s16(game.image, counts - BASE)
            original = u32(game.image, ptr - BASE) - BASE
            slots = [slot for _, poms in atlases.values() for slot in poms]
            new = eboot.allocate(bytes(game.image[original:original + stock * 2]) +
                                 struct.pack(f'<{len(slots)}H', *slots))
            eboot.linked(ptr, new, 2)
            eboot.half(counts, stock + len(slots))
        keep_fighter_portraits(eboot, game.image, self.descriptors)
        install_cameras(eboot, game.image, self.camera_banks, self.camera_bindings)
        self.emit_items()
        for role, count in self.next_clips.items():
            for descriptor in game.roles[role]['cases']:
                eboot.half(int(descriptor['counts_va'], 16) + 36, count)
        private = install_private_states(eboot, game, self.private_defs, self.public_flags)
        for move in self.states.values():
            va = private.get((move.target, move.state))
            if va is None:
                va = int(game.native_actions[move.target][move.state]['va'], 16)
            donor = game.actions[move.source][move.original]
            callbacks = (weapon_callbacks.get(move.original)
                         if move.source == LEO_THROW[0] and move.state >= FIRST_PRIVATE_STATE else None)
            if (move.source, move.original) == (12, 180) and move.state >= FIRST_PRIVATE_STATE:
                callbacks = black_callbacks
            if callbacks is None:
                callbacks = tuple(int(donor['callbacks'][key], 16) for key in (1, 0, 2))
            if move.source == TARO_DOWN[0] and move.root == TARO_DOWN[1] and move.target in taro_timeouts:
                update, enter, exit = callbacks
                callbacks = (taro_timeouts[move.target].get(update, update),
                             self.taro_fire_enter if move.original == TARO_DOWN[1] else enter, exit)
            if (move.source, move.original) == (16, 163) and babar_exit is not None:
                callbacks = (*callbacks[:2], babar_exit)
            for i, callback in enumerate(callbacks):
                eboot.linked(va + i * 4, callback, 2)
            eboot.half(va + 12, move.next_state)
            eboot.half(va + 14, move.clip)
        for role, state in self.assignments:
            if state == 158:
                for address in forward_skill_branches(game, role):
                    eboot.word(address, state)
        for address, state in self.entry_routes.items():
            eboot.word(address, state)
        eboot.install_anm()
        eboot.install_effects(self.effects)
        eboot.install_slots(self.slots)
        install_borrowed_charge(eboot, game.image, self.electric_roles, self.charge_anchors)
        for patch in patches:
            rel = patch.offset - game.eboot['offset']
            if 0 <= rel < len(game.eboot_raw) and rel not in eboot.changed:
                eboot.out[rel:rel + len(patch.edited)] = patch.edited
        moves = [move.record(private.get((move.target, move.state))) for move in self.states.values()]
        textures, palette_masks = [], []
        cases_by_bank = {(role, case['anm_banks'][0]['id']): case for role, fighter in enumerate(game.roles)
                         for case in fighter['cases']}
        for bank, items in self.anm_tails.items():
            for (_, magic, key), slot in items.items():
                if magic != b'POM\0' or not key:
                    continue
                physical = (*key,bank) if (game.keeps_move_texture(key, self.assignments)
                           and bank in game.installed_move_textures.get(key, {})) else key
                textures.append([*key, bank, self.slots[slot][1], len(game.move_texture_data(physical))])
                case = cases_by_bank[key[0], bank]
                forms = self.texture_usage[case['descriptor_va']].get(key, {})
                masks = [None] * len(case['forms'])
                for shape in range(len(masks)):
                    if None in forms or shape in forms:
                        masks[shape] = sum(1 << p for p in forms.get(None, set()) | forms.get(shape, set()))
                palette_masks.append(masks)
        def metadata(ranges):
            result = {'moves':moves, 'ranges':list(ranges.items()), 'effects':list(self.effect_sources.items()),
                      'textures':textures}
            # Restoring a borrowed state must also restore any earlier native
            # clip edit hidden underneath that state's patched executable bytes.
            before = bytearray(eboot.raw)
            for offset, value in game.native_edits.items():
                before[offset:offset + len(value)] = value
            original = [[offset, before[offset:offset + length].hex()] for offset, length in ranges.items()
                        if before[offset:offset + length] != game.canonical_eboot[offset:offset + length]]
            if original:
                result['before'] = original
            return result
        return eboot.finish(moves, metadata=metadata,
                            optional_metadata={**({'texture_palettes':palette_masks} if textures else {}),
                              'roots':sorted(self.assignments), 'bases':sorted(bases),
                              'padding':sorted(move_padding(game,replacements).items())})

    def emit_items(self):
        eboot, image = self.native, self.game.image
        for name, (case, models) in self.items.items():
            descriptor = int(name, 16)
            counts = int(case['counts_va'], 16)
            count = self.item_count(case)
            length = count + len(models)
            original_models = u32(image, descriptor - BASE + 4) - BASE
            old_poms = u32(image, descriptor - BASE + 8) - BASE
            old_counts = u32(image, counts - BASE + 4) - BASE
            mdl_data = bytes(image[original_models:original_models + 2 * count]) if count else b''
            pom_counts = bytes(image[old_counts:old_counts + 2 * count]) if count else b''
            pointers = [u32(image, old_poms + 4 * i) for i in range(count)]
            for _, model, poms in models.values():
                mdl_data += struct.pack('<H', model)
                pom_counts += struct.pack('<H', len(poms))
                pointers.append(eboot.allocate(struct.pack(f'<{len(poms)}H', *poms)) if poms else 0)
            pom_array = eboot.allocate(bytes(4 * length))
            for index, pointer in enumerate(pointers):
                if pointer:
                    eboot.linked(pom_array + 4 * index, pointer, 2)
            eboot.linked(descriptor + 4, eboot.allocate(mdl_data), 2)
            eboot.linked(descriptor + 8, pom_array, 2)
            eboot.linked(counts + 4, eboot.allocate(pom_counts), 2)
            eboot.half(counts, length)

