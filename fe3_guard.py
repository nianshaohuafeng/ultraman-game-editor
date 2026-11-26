"""Port recoil animations and native self-healing defence scenes."""

from collections import defaultdict
from dataclasses import dataclass
import struct

from fe3_add import ROLE_DESCRIPTORS, append_clip, pack_words
from fe3_expand import ElfSpace, erx_append, prefix_records, relocated_halfwords
from fe3_formats import Bom, require
from fe3_native import CharacterProgram, NativeTables
from fe3_preview import clip_index, motion
from fe3_scene import camera_bank, script_rows
from fe3_skill_ui import jump
from fe3_transfer import scene_camera_slots, transplant_bom
from move_eboot import (A0, A1, A2, A3, RA, SP, T4, T5, T6, T7, V0, Z,
                        addiu, addu, branch, iword, jr, lw, rword, sll, sw)
from move_sound import EE_COUNT, EE_CUES


PHASES = (101, 102)
GUARD_TYPE = 2
META = b'OGGRD3v1'
META_HEAD = struct.Struct('<8s7I')
META_ROW = struct.Struct('<4B2h6I')
SCENE_OPS = frozenset((0, 2, 4, 5, 6, 8, 10, 16, 17, 18, 23, 27, 30, 38, 40, 41, 112))
ACTOR_ANIMATION = 0x2CB910
PLACE_ACTOR = 0x2B8FC0
BEGIN_SCENE = 0x2CB1F8
SCENE_FLAGS = 0x222BF0
S0, T8 = 16, 24


@dataclass(frozen=True)
class Absorption:
    animations: tuple
    scene: tuple
    film_update: tuple


@dataclass(frozen=True)
class GuardRoute:
    candidate: int
    donor: int
    skill: int
    shape: int
    animations: tuple
    callbacks: tuple


def native_trap(tables, program, target):
    trap, ordinal = program.at(target, '<2I', b'.text')
    if trap == 0x4D and ordinal >> 16 == 0x2400:
        return tables.pointer(0x483620, ordinal & 0xFFFF)
    return None


def absorption(work, donor, skill):
    """Only claim a portable sequence if the source uses the shared scene interpreter."""
    if skill.kind != 0 or skill.action != GUARD_TYPE:
        return None
    program = work.program(donor)
    start, finish = (program.actions()[state][1] for state in PHASES)
    if start < 0:
        return None
    fallback, _ = installed_absorptions(program)
    update, enter, _ = fallback[3:]
    words = program.at(enter, '<22I', b'.text')
    script = None
    high = low = None
    for word in words:
        if word == jr():
            break
        if word >> 16 == 0x3C05:
            high = word & 0xFFFF
        elif word >> 16 == 0x24A5:
            low = (word & 0x7FFF) - (word & 0x8000)
        elif word >> 26 == 2:
            target = (word & 0x3FFFFFF) << 2
            if high is not None and low is not None and native_trap(work.tables, program, target) == BEGIN_SCENE:
                script = high * 65536 + low
            break
    if script is None:
        return None
    rows = script_rows(program, script)
    if not rows or rows[-1][1] != 27 or not any(op == 23 and args[0] == 0 and args[1] > 0 for _, op, args, _ in rows):
        return None
    for _, op, args, address in rows:
        if op not in SCENE_OPS or (op == 30 and args[:2] != [0, finish]) or any(
                address <= pointer < address + 32 for pointer in program.relocations):
            return None
    film_update = ()
    if finish < 0:
        # The film-only variant waits for scene flag 4 before releasing the
        # battle clock. These are its only outbound calls.
        code = list(program.at(update, '<39I', b'.text'))
        calls = [index for index, word in enumerate(code) if word >> 26 == 3]
        if (calls != [8, 19] or code[-2:] != [jr(), addiu(SP, SP, 16)]
                or [native_trap(work.tables, program, (code[i] & 0x3FFFFFF) << 2) for i in calls]
                != [SCENE_FLAGS, ACTOR_ANIMATION]):
            return None
        film_update = tuple(code)
    return Absorption((start, finish), tuple(rows), film_update)


def original_callbacks(program):
    return tuple(word for state in PHASES
                 for word in program.at(program.action_table + 24 * state, '<3I'))


def installed_absorptions(program):
    section = program.sections.get(b'.moves')
    if section is not None:
        offset, length = section[4:6]
        end = offset + length
        at = program.data.rfind(META, offset, end)
        if at >= 0:
            require(at + META_HEAD.size <= end, '防御技演出索引被截断。')
            _, *fallback, count = META_HEAD.unpack_from(program.data, at)
            start = at + META_HEAD.size
            require(count <= 54 and start + META_ROW.size * count <= end, '防御技演出索引越界。')
            routes = {}
            for candidate, donor, skill, shape, first, last, *callbacks in META_ROW.iter_unpack(
                    program.data[start:start + META_ROW.size * count]):
                key = candidate, shape
                require(key not in routes and candidate < 20 and donor < 39 and skill < 20 and shape < 3,
                        '防御技候选演出索引无效。')
                routes[key] = GuardRoute(candidate, donor, skill, shape, (first, last), tuple(callbacks))
            return tuple(fallback), routes
    return original_callbacks(program), {}


def marker(fallback, routes):
    return META_HEAD.pack(META, *fallback, len(routes)) + b''.join(
        META_ROW.pack(row.candidate, row.donor, row.skill, row.shape, *row.animations, *row.callbacks)
        for row in routes.values())


def supported_absorption(work, donor, candidate):
    from fe3_support import installed_supports

    key = donor, candidate
    seen = set()
    links = installed_supports(work.game.elf)
    while key in links:
        require(key not in seen, '防御技的来源链接存在循环。')
        seen.add(key)
        key = links[key]
    skill = work.game.skill_records.get(key)
    script = absorption(work, key[0], skill) if skill is not None else None
    return key, skill, script


def intake_rig(work, role, shape, donor, skill):
    from cross_move import Retarget, skeleton
    from fe3_support import playable_forms, preferred_form

    forms = playable_forms(work, donor, skill)
    require(forms, '来源防御技没有可用的原生形态。')
    src = preferred_form(forms, shape)
    dst = work.form(role, shape)
    require(src.bom and dst.bom, '双方防御技需要有原生 BOM。')
    bind = Retarget(skeleton(work.game, donor, 0, src.id), skeleton(work.game, role, 0, shape))
    return src, dst, bind


def guard_motion(game, work, plan, key, number, role, donor, source_anm, forms):
    from cross_move import pack_animation

    source_form, target_form, bind = forms
    in_key, in_index = work.binding(donor)[source_anm]
    out_key, out_index = work.binding(role)[work.action_rows(role)[0][1]]
    incoming, reference = game.original(in_key), game.original(out_key)
    reference_head = clip_index(reference)[out_index]
    raw = pack_animation(reference[reference_head.start:reference_head.start + reference_head.size],
                         motion(incoming, in_index), motion(reference, out_index), bind, True, sparse=True)
    clip = struct.pack('<4s3I', b'ANM\0', 1, 16, 0) + raw
    new_frame = clip_index(clip)[0].last_frame
    src = Bom(game.original(source_form.bom))
    dst = Bom(plan.resources.get(target_form.bom, game.original(target_form.bom)))
    # Resolve collision nodes against the target rig where the source uses
    # non-animated descendants of a matched track.
    nodes = {node: bind.bone(node) for node in range(len(bind.source.bones))
             if bind.source.ancestor(node, bind.common) is not None}
    plan.resources[target_form.bom] = transplant_bom(src, source_anm, source_form, dst, number, target_form,
                                                    nodes, new_frame, new_frame)
    Bom(plan.resources[target_form.bom]).validate()
    base = plan.resources.get(key, game.original(key))
    plan.resources[key] = append_clip(base, clip)
    sounds = set()
    for event in src.events(source_anm):
        if event['op'] in (16, 17) and event['selector'] & (1 << source_form.id):
            cue = event['args'][0]
            require(0 <= cue < EE_COUNT, '来源防御动作使用未适配的音效编号。')
            sounds.add(work.tables.pointer(EE_CUES, cue) >> 16)
    return sounds


def guard_scene(game, work, plan, role, donor, sequence, anm):
    source, target = camera_bank(work, donor), camera_bank(work, role)
    next_member = len(game.children[target]) + sum(group == target for group, _ in plan.cameras)
    cameras = {}
    for member in scene_camera_slots(sequence.scene):
        part = game.part(source, member)
        require(part.kind == 'cmsp' and not part.compressed, '防御技镜头资源格式无效。')
        cameras[member] = next_member
        plan.cameras[target, next_member] = (part.name, game.original(part.key))
        next_member += 1
    payload = bytearray()
    sounds = set()
    for frame, op, args, _ in sequence.scene:
        data = args.copy()
        if op == 10 and args[1] == 0 and args[0] in cameras:
            data[0] = cameras[args[0]]
        if op == 30:
            data[1] = anm
        if op in (16, 17):
            sounds.add(args[0] >> 16)
        payload.extend(struct.pack('<fi6i', frame, op, *data))
    return bytes(payload), sounds


def load_guard_bank(space, role, key, start, end):
    tables = NativeTables(space.elf)
    descriptors = {tables.pointer(table, role) for table in ROLE_DESCRIPTORS} - {0, 0xFFFFFFFF}
    for address in sorted(descriptors):
        descriptor = tables.words(address, 21)
        sizes = list(tables.words(descriptor[20], 14))
        starts = tables.words(descriptor[13], sum(sizes[5:9]))
        require(any((key[0], start) in zip(tables.words(descriptor[field], sizes[field - 4]),
                                          starts[sum(sizes[5:field - 4]):])
                    for field in range(9, 13)), '该角色的出战资源使用不同的动作库。')
        always = tables.words(descriptor[12], sizes[8])
        if (key[0], start) not in zip(always, starts[sum(sizes[5:8]):]):
            space.patch_word(address + 48, space.allocate(pack_words((*always, key[0]))))
            space.patch_word(address + 52, space.allocate(pack_words((*starts, start))))
            sizes[8] += 1
        sizes[13] = end
        space.patch_word(address + 80, space.allocate(pack_words(sizes)))


def actor_ptr(register, group=T4, index=T5):
    # The first instructions of 0x2A5AD8 are a direct actor lookup.
    return (lw(group, 0xC, A0), lw(index, 0x10, A0), sll(register, index, 2),
            addu(register, register, group), lw(register, 0, register))


def store64(register, offset):
    return iword(63, SP, register, offset)


def load64(register, offset):
    return iword(55, SP, register, offset)


def address_pair(register, address):
    high, low = relocated_halfwords(address)
    return iword(15, Z, register, high), addiu(register, register, low)


def emit_enter(allocate, relocations, anm, scene=None):
    words = [addiu(SP, SP, -16), store64(RA, 8), store64(S0, 0), addu(S0, A0, Z)]
    if scene is not None:
        words.append(sw(Z, 0x18, A0))
    if anm >= 0:
        words.extend((addu(A1, Z, Z), addiu(A2, Z, anm), addiu(A3, Z, 1),
                      addu(8, Z, Z), addiu(9, Z, 2), jump(ACTOR_ANIMATION, True), addu(10, Z, Z),
                      addu(A0, S0, Z), *actor_ptr(T6), addu(A1, S0, Z),
                      jump(PLACE_ACTOR, True), addiu(A0, T6, 0x550)))
    words.extend((addu(A0, S0, Z), load64(RA, 8), load64(S0, 0)))
    if scene is None:
        words.extend((sw(Z, 0x1C, A0), sw(Z, 0x24, A0), jr(), addiu(SP, SP, 16)))
        return allocate(pack_words(words))
    pair = len(words) * 4
    words.extend((*address_pair(A1, scene), jump(BEGIN_SCENE), addiu(SP, SP, 16)))
    address = allocate(pack_words(words))
    relocations.extend(((address + pair, 5), (address + pair + 4, 6)))
    return address


def emit_route(allocate, relocations, fallback, routes, field):
    # G stores one equipped-candidate bitset and guard type per player,
    # preset and form. Use the same index as the native check at 0x2CEE20.
    words = [*actor_ptr(T6), sll(T7, T5, 1), addu(T7, T7, T5), sll(T7, T7, 2),
             lw(T5, 0x1C, T6), sll(T8, T5, 1), addu(T5, T5, T8), lw(T6, 0x10, T6),
             addu(T7, T7, T5), addu(T7, T7, T6), sll(T7, T7, 2), addu(T4, T4, T7),
             lw(T5, 0xC88, T4), addiu(T7, Z, GUARD_TYPE),
             branch(5, T5, T7, 1 + 9 * len(routes)), lw(T4, 0x62C, T4)]
    jumps = []
    for row in routes:
        words.extend((addiu(T5, Z, row.shape), branch(5, T6, T5, 7), 0,
                      rword(Z, T4, T7, 2, row.candidate), iword(12, T7, T7, 1),
                      branch(4, T7, Z, 3), 0, jump(row.callbacks[field]), 0))
        jumps.append(len(words) - 2)
    jumps.append(len(words))
    words.extend((jump(fallback), 0))
    address = allocate(pack_words(words))
    relocations.extend((address + 4 * index, 4) for index in jumps)
    return address


def compile_callbacks(allocate, relocations, fallback, kept, additions):
    routes = kept.copy()
    if additions:
        nothing = allocate(pack_words((jr(), addu(V0, Z, Z))))
        leave = allocate(pack_words((*actor_ptr(T6), addiu(T7, Z, 1), jr(), iword(40, T6, T7, 0x950))))
    for key, (donor, skill, animations, scene, update) in additions.items():
        tick = nothing
        if update:
            # 0x2CB780 with no assigned ANM and a3=0 updates these two fields
            # and returns. The receiving native state may have its own ANM.
            idle = allocate(pack_words((*actor_ptr(T6), addiu(T7, Z, 1), iword(40, A0, T7, 0x28),
                                        jr(), sw(Z, 0x1C0, T6))))
            words = list(update)
            words[8], words[19] = jump(SCENE_FLAGS, True), jump(idle, True)
            tick = allocate(pack_words(words))
            relocations.append((tick + 4 * 19, 4))
        at = allocate(scene, 32)
        first, last = animations
        callbacks = (nothing, emit_enter(allocate, relocations, first), nothing,
                     tick, emit_enter(allocate, relocations, last, at), leave)
        routes[key] = GuardRoute(key[0], donor, skill, key[1], animations, callbacks)
    allocate(marker(fallback, routes))
    picks = tuple(routes.values())
    return {state: tuple(emit_route(allocate, relocations, fallback[field], picks, field) if picks else fallback[field]
                         for field in range(3 * index, 3 * index + 3)) for index, state in enumerate(PHASES)}


def compile_guards(game, work, plan, links):
    from fe3_support import add_sound_banks

    tables = NativeTables(plan.elf or game.elf)
    active = defaultdict(dict)
    guard_roles = set()
    for (role, candidate), (donor, source) in sorted(links.items()):
        row = next((item for item in tables.skill_rows(role, 0) if item.id == candidate), None)
        if row is None:
            continue
        guard_roles.add(role)
        if plan.skill_words.get((role, candidate), row.words)[0] != GUARD_TYPE:
            continue
        (donor, source), skill, sequence = supported_absorption(work, donor, source)
        if sequence is None:
            continue
        for shape, form in work.forms(role).items():
            if shape < 3 and row.menu_mask & (1 << shape) and not form.accessory:
                try:
                    rigs = intake_rig(work, role, shape, donor, skill)
                except ValueError:
                    if (role, candidate) in plan.skill_words:
                        raise
                    continue
                active[role][candidate, shape] = donor, source, sequence, rigs
    if not guard_roles:
        return
    sounds = defaultdict(set)
    space = None
    for role in sorted(guard_roles):
        base = work.program(role)
        fallback, prior = installed_absorptions(base)
        choices = active[role]
        kept = {key: route for key, route in prior.items()
                if key in choices and choices[key][:2] == (route.donor, route.skill)}
        if set(choices) == kept.keys() == prior.keys():
            continue
        resource, start = max(tables.banks(role), key=lambda row: row[1])
        key = resource, -1
        slot = start + len(clip_index(plan.resources.get(key, game.original(key))))
        additions = {}
        for candidate_shape, (donor, source, sequence, forms) in choices.items():
            if candidate_shape in kept:
                continue
            animations = []
            for source_anm in sequence.animations:
                if source_anm < 0:
                    animations.append(-1)
                    continue
                sounds[role].update(guard_motion(game, work, plan, key, slot, role, donor, source_anm, forms))
                animations.append(slot)
                slot += 1
            script, scene_sounds = guard_scene(game, work, plan, role, donor, sequence, animations[1])
            sounds[role].update(scene_sounds)
            additions[candidate_shape] = donor, source, tuple(animations), script, sequence.film_update
        data = bytearray(plan.modules.get(role, base.data))
        if role not in plan.modules:
            for offset, patch in plan.scripts.get(role, {}).items():
                data[offset:offset + len(patch)] = patch
        program = CharacterProgram(bytes(data), base.command_ordinal)
        plan.modules[role] = erx_append(program, base.command_ordinal, [], action_callbacks=lambda allocate, relocations:
                                        compile_callbacks(allocate, relocations, fallback, kept, additions))
        if additions:
            if space is None:
                elf = plan.elf or game.elf
                space = ElfSpace(elf, prefix_records(elf))
            load_guard_bank(space, role, key, start, slot)
    if space is not None:
        add_sound_banks(space, {role: banks - {0} for role, banks in sounds.items() if banks - {0}})
        plan.elf = space.finish()
