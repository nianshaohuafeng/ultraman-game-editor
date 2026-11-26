"""Compile replacements for the FE3 barrier and shoulder candidate tables."""

from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
import struct

from ogeditor.ps2.add import ROLE_DESCRIPTORS, next_candidate, pack_words, write_candidate
from ogeditor.ps2.expand import AREA, HEADER, LOAD_PREFIX, ElfSpace, prefix_records, relocated_halfwords
from ogeditor.ps2.formats import Bom, require
from ogeditor.ps2.native import GUARD_TYPES, NativeTables, ROLE_COUNT, SHOULDER_ASSIST_WORD, SKILL_TABLES, native_offset
from ogeditor.ps2.preview import clip_index, replace_clip
from ogeditor.ps2.skill_ui import INTRO_GROUP, description_links, jump, transplant_name
from ogeditor.ps2.transfer import matched_rigs, retarget_clip, transplant_bom
from move_eboot import branch, iword, rword
from move_sound import EE_COUNT, EE_CUES


SHOULDER_CALLBACKS = (0x2CAD10, 0x2CAD60, 0x2CADF0)
# Event 350 sets the actor's common +0x984 flag. The other shoulder event-9
# handlers use role libraries, transform state, scene objects or attachments.
SHARED_SHOULDER_EVENTS = frozenset((350,))
SHOULDER_BOM_OPS = frozenset((0,2,3,4,6,9,10,11,12,13,14,16,17,18,24,25,26))

ASSIST_SELECT = 0x2CF484
ASSIST_CALL = 0x2CF4E8
ASSIST_INIT = 0x25A8E8
ASSIST_START = 0x25B2C0
ASSIST_TAG = b'OGAID3v1'
ASSIST_WORD = 0xFFFFFF00
ASSIST_HEADER_SIZE = 48
AID_BRANCHES = frozenset((0x25AF5C, 0x25B00C, 0x25AE10, 0x25AF18, 0x25AED8, 0x25B078))
SUPPORT_LINKS_TAG = b'OGSUP3v1'


@dataclass(frozen=True)
class Shoulder:
    forms: tuple
    phases: tuple


def playable_forms(work, role, skill):
    return tuple(form for form in work.forms(role).values()
                 if not form.accessory and form.id < 3 and skill.menu_mask & (1 << form.id))


def shoulder(work, role, skill, *, receiving=False):
    actions = work.action_rows(role)
    state = skill.action
    require(0 < state < len(actions), '请选择有原生肩键动作的候选。')
    forms = playable_forms(work, role, skill)
    require(forms and all(form.bom for form in forms), '肩键候选没有可用的角色模型和 BOM。')
    phases, seen = [], set()
    banks = work.binding(role)
    while state > 0:
        require(state < len(actions) and state not in seen, '肩键动作有专用状态循环。')
        seen.add(state)
        nxt, anm, flags = actions[state]
        callbacks = tuple(item['ee'] for item in work.callbacks(role,state))
        require(callbacks == SHOULDER_CALLBACKS and anm in banks, '肩键动作使用角色专用回调。')
        if receiving:
            require(sum(clip == anm for _,clip,_ in actions) == 1, '目标肩键动作片段与其他状态共用。')
        phases.append((state,anm,flags))
        state = nxt
    require(state == 0, '肩键动作没有原生回待机状态。')
    if receiving:
        anchors = {item.action for item in work.tables.skill_rows(role,2) if item.id != skill.id}
        require(seen.isdisjoint(anchors), '目标肩键片段与另一条辅助技共用。')
        for index,(nxt,_,_) in enumerate(actions):
            require(index in seen or nxt not in seen, '目标肩键动作仍被其他状态引用。')
    return Shoulder(forms,tuple(phases))


def shoulder_sound_banks(work, spec):
    banks = set()
    for form in spec.forms:
        bom = Bom(work.game.original(form.bom))
        for _, anm, _ in spec.phases:
            for event in bom.events(anm):
                if not event['selector'] & (1 << form.id):
                    continue
                op = event['op']
                require(op in SHOULDER_BOM_OPS, f'来源含需单独适配的肩键事件 {op}。')
                if op == 9:
                    require(event['args'][0] in SHARED_SHOULDER_EVENTS,
                            '来源依赖角色专用光线、道具或场景效果。')
                elif op in (16,17):
                    cue = event['args'][0]
                    require(0 <= cue < EE_COUNT, '来源肩键使用了扩展音效。')
                    bank = work.tables.pointer(EE_CUES,cue) >> 16
                    require(bank < 140, '来源肩键音效使用了专用语音处理。')
                    banks.add(bank)
    return banks - {0}


def installed_assist(value):
    source = value - ASSIST_WORD
    return source if 0 <= source < ROLE_COUNT else None


def assist_source(work, role, action):
    source = role if action == SHOULDER_ASSIST_WORD else installed_assist(action)
    if source is not None and work.tables.pointer(0x48A690,source) in AID_BRANCHES:
        return source
    return None


def checked_source(work, role, dest, donor, src):
    require(role != donor and dest.kind == src.kind, '请选择其他角色的同类原生技能。')
    require(dest.menu_candidate and src.menu_candidate and dest.id == dest.words[2] and src.id == src.words[2],
            '来源或目标的原生候选编号已变化。')
    if dest.kind == 0:
        require(src.action in GUARD_TYPES and src.action != 0, '请选择有效的来源屏障类型。')
        return None
    if src.action == SHOULDER_ASSIST_WORD or installed_assist(src.action) is not None:
        source = assist_source(work,donor,src.action)
        require(source is not None, '来源使用专用援护模型或专用能力。')
        require((dest.action == SHOULDER_ASSIST_WORD or installed_assist(dest.action) is not None)
                and work.tables.has_shoulder_assist(role), '请选择目标的原生援护候选。')
        return source
    source = shoulder(work,donor,src)
    target = shoulder(work,role,dest,receiving=True)
    require(tuple(flags for _,_,flags in source.phases) == tuple(flags for _,_,flags in target.phases),
            '双方肩键动作的阶段或原生控制属性不同。')
    return source, target, shoulder_sound_banks(work,source)


def support_target_form(work, role, shape):
    require(work.tables.skill_menu_enabled(role), '当前角色使用固定技能配置。')
    require(type(shape) is int and 0 <= shape < 3, '请选择可参战的模型形态。')
    require(not work.form(role,shape).accessory, '请选择角色本体形态。')


def added_support_action(work, role, donor, src):
    require(role != donor and src.menu_candidate and src.id == src.words[2], '请选择其他角色的原生技能。')
    if src.kind == 0:
        require(src.action in GUARD_TYPES and src.action != 0, '请选择有效的来源屏障。')
        return src.action
    source = assist_source(work,donor,src.action)
    require(src.kind == 2 and source is not None, '新增辅助候选需使用通用属性强化援护。')
    require(work.tables.has_shoulder_assist(role), '目标角色没有可调用的援护对象。')
    return ASSIST_WORD+source


def source_choices(game, role, candidate, donor, kind=None, shape=None):
    from ogeditor.ps2.workbench import Workbench
    from ogeditor.ps2.guard import intake_rig, supported_absorption

    work = Workbench(game,preview=False)
    dest = game.skill_record(role,candidate) if candidate is not None else None
    group = dest.kind if dest else kind
    require(group in (0,2) and type(donor) is int and 0 <= donor < ROLE_COUNT,
            '请选择防御技或辅助技的来源角色。')
    if dest is None:
        support_target_form(work,role,shape)
        next_candidate(game.workbench.tables,role)
    result = []
    for src in work.tables.skill_rows(donor,group):
        reason = ''
        extra = {}
        try:
            details = checked_source(work,role,dest,donor,src) if dest else added_support_action(work,role,donor,src)
            if src.kind == 0:
                (native_role, _), native_skill, sequence = supported_absorption(work, donor, src.id)
                if sequence is not None:
                    for target_shape in (tuple(form.id for form in playable_forms(work, role, dest)) if dest else (shape,)):
                        intake_rig(work, role, target_shape, native_role, native_skill)
                    extra['scene'] = True
            if isinstance(details,tuple):
                source,target,_ = details
                for form in target.forms:
                    matched_rigs(game, preferred_form(source.forms,form.id), form)
        except ValueError as error:
            reason = str(error)
        result.append({'id':src.id,'name':src.label,'reason':reason,**extra})
    return result


def preferred_form(forms, shape):
    return next((form for form in forms if form.id == shape), forms[0])


def stage_motion(game, work, plan, role, donor, source, target, reserved):
    rigs = [(form, source_form, *matched_rigs(game,source_form,form)) for form in target.forms
            for source_form in (preferred_form(source.forms,form.id),)]
    for (_, src_anm, _),(_, anm, _) in zip(source.phases,target.phases):
        key,index = work.binding(role)[anm]
        require((key,index) not in reserved, '两条辅助配招共用目标动作片段。')
        reserved.add((key,index))
        src_key,src_index = work.binding(donor)[src_anm]
        bank = plan.resources.get(key,game.original(key))
        old = clip_index(bank)[index]
        moved = []
        for form,source_form,rig_in,rig_out in rigs:
            incoming = retarget_clip(game.original(src_key),src_index,old.tracks,rig_in,rig_out)
            moved.append(incoming)
            node_map = {value[0]:rig_out[track][0] for track,value in rig_in.items()}
            base = Bom(plan.resources.get(form.bom,game.original(form.bom)))
            bom = transplant_bom(Bom(game.original(source_form.bom)),src_anm,source_form,base,anm,
                                 form,node_map,old.last_frame,clip_index(incoming)[0].last_frame)
            Bom(bom).validate()
            plan.resources[form.bom] = bom
        require(all(blob == moved[0] for blob in moved), '目标多个形态需要不同的骨骼适配。')
        plan.resources[key] = replace_clip(bank,index,moved[0])


def stage_support_name(game, work, plan, role, candidate, donor, src):
    tables = work.tables
    name_in, name_out = tables.skill_name_source(donor), tables.skill_name_source(role)
    if name_in and name_out:
        before = plan.resources.get(name_out,game.original(name_out))
        plan.resources[name_out] = transplant_name(game.original(name_in),src.id,before,candidate)
        links,_ = description_links(game.elf,game.extension)
        source = links.get((donor,src.id)) or tables.skill_description_source(donor,src.id)
        part = game.part(INTRO_GROUP,source[0])
        require(part.kind == 'pom' and part.compressed and 0 <= source[1] < 4,
                '来源辅助技没有可用的原生介绍图块。')
        plan.descriptions[role,candidate] = source
    plan.names[role,candidate] = src.label


def add_sound_banks(space, by_role):
    tables = NativeTables(space.elf)
    for role,banks in sorted(by_role.items()):
        addresses = {tables.pointer(table,role) for table in ROLE_DESCRIPTORS} - {0,0xFFFFFFFF}
        for address in sorted(addresses):
            descriptor = tables.words(address,21)
            sizes = list(tables.words(descriptor[20],14))
            current = tables.words(descriptor[18],sizes[11])
            missing = sorted(banks - set(current))
            if not missing:
                continue
            require(current and all(min(tables.at(0x42F5B8+32*bank,'<2i')) >= 0 for bank in missing),
                    '来源辅助音效没有可加载的音库。')
            require(all(tables.pointer(table,r) != address for table in ROLE_DESCRIPTORS
                        for r in range(ROLE_COUNT) if r != role), '目标角色的音库描述符与其他角色共用。')
            # 0x225D04 uses the last word as the marker for finisher sounds.
            sizes[11] += len(missing)
            space.patch_word(address+72,space.allocate(pack_words((*current[:-1],*missing,current[-1]))))
            space.patch_word(address+80,space.allocate(pack_words(sizes)))


def install_assist_dispatch(space, roles):
    tables = NativeTables(space.elf)
    selector = tables.words(ASSIST_SELECT,2)
    launcher, = tables.words(ASSIST_CALL,1)
    if (selector,launcher) == ((0x54EF0028,0x928F0010),jump(ASSIST_START,True)):
        header = space.allocate(ASSIST_TAG + bytes(ASSIST_HEADER_SIZE-len(ASSIST_TAG)))
        hi,lo = relocated_halfwords(header + len(ASSIST_TAG))
        zero,a0,a1,a2,a3,t0,t6,t7,sp,ra = 0,4,5,6,7,8,14,15,29,31
        args = (
            iword(9,a3,t0,0x100), iword(11,t0,t7,ROLE_COUNT), branch(5,t7,zero,11), 0,
            iword(35,a0,t0,-0xC68), iword(11,t0,t7,ROLE_COUNT), branch(4,t7,zero,5), iword(15,zero,t6,hi),
            rword(t0,t6,t6,0x21), iword(36,t6,t7,lo),
            branch(5,t7,zero,3), 0, jump(ASSIST_START), 0,
            iword(9,sp,sp,-32), iword(63,sp,ra,16), iword(43,sp,a0,0),iword(43,sp,a1,4),iword(43,sp,a2,8),
            jump(ASSIST_INIT,True),rword(t0,zero,a1,0x21),iword(35,sp,a0,0),iword(35,sp,a1,4),iword(35,sp,a2,8),
            iword(55,sp,ra,16),jump(ASSIST_START),iword(9,sp,sp,32),
        )
        launch = space.allocate(pack_words(args))
        select = space.allocate(pack_words((
            iword(9,a3,t6,0x100),iword(11,t6,t6,ROLE_COUNT),branch(5,t6,zero,4),0,
            iword(9,zero,t7,-2),branch(5,a3,t7,3),0,jump(0x2CF48C),0,jump(0x2CF528),0x928F0010,
        )))
        space.patch(ASSIST_SELECT,pack_words((jump(select),0)))
        space.patch_word(ASSIST_CALL,jump(launch,True))
    else:
        launch = (launcher & 0x3FFFFFF) << 2
        header = launch-ASSIST_HEADER_SIZE
        at = native_offset(space.elf,header,len(ASSIST_TAG))
        require(launcher >> 26 == 3 and selector[0] >> 26 == 2 and selector[1] == 0
                and LOAD_PREFIX <= header < LOAD_PREFIX+AREA
                and bytes(space.elf[at:at+len(ASSIST_TAG)]) == ASSIST_TAG, '奥3援护分派入口无法识别。')
    for role in roles:
        space.patch(header+len(ASSIST_TAG)+role,b'\x01')


@lru_cache(maxsize=8)
def installed_supports(elf):
    records = prefix_records(elf)
    if records is None:
        return {}
    end = records[0]
    at = elf.rfind(SUPPORT_LINKS_TAG,HEADER+12,end)
    if at < 0:
        return {}
    at += len(SUPPORT_LINKS_TAG)
    count, = struct.unpack_from('<I',elf,at)
    require(at+4+4*count <= end,'奥3辅助技能来源表越界。')
    rows = {}
    for role,candidate,donor,move in struct.iter_unpack('<4B',elf[at+4:at+4+4*count]):
        require(role < ROLE_COUNT and donor < ROLE_COUNT and candidate < 20 and move < 20
                and (role,candidate) not in rows, '奥3辅助技能来源表记录无效。')
        rows[role,candidate] = donor,move
    return rows


def stage_support_links(space, elf, changes):
    links = installed_supports(elf) | changes
    space.allocate(SUPPORT_LINKS_TAG + pack_words((len(links),)) + b''.join(
        struct.pack('<4B',*key,*source) for key,source in sorted(links.items())))


def append_support_candidates(game, work, space, plan, role, moves, links, assist_roles):
    tables = {}
    for candidate,donor,src_id,shape in moves:
        support_target_form(work,role,shape)
        src = game.skill_records[donor,src_id]
        kind = src.kind
        action = added_support_action(work,role,donor,src)
        if kind not in tables:
            base, size = SKILL_TABLES[kind]
            original = work.tables.skill_rows(role,kind)
            count = max((row.value for row in original),default=-1)+1
            require(size == 12 and 0 <= count < 20,'目标防御或辅助技能表布局无效。')
            pointer = work.tables.pointer(base,role)
            tables[kind] = [work.tables.words(pointer+size*i,3) for i in range(count)]
        native = tables[kind]
        words = (action,0,candidate)
        write_candidate(space,role,candidate,shape,kind,len(native))
        native.append(words)
        stage_support_name(game,work,plan,role,candidate,donor,src)
        if kind == 2:
            assist_roles.add(role)
        plan.additions[role,candidate] = donor,src_id,shape
        plan.skill_words[role,candidate] = words
        links[role,candidate] = donor,src_id
    for kind,native in tables.items():
        space.patch_word(SKILL_TABLES[kind][0]+4*role,space.allocate(b''.join(pack_words(row) for row in native)))


def compile_support(game,work,plan,transfers):
    sound_banks, assist_roles, reserved = defaultdict(set), set(), set()
    for (role,candidate),(donor,donor_candidate) in sorted(transfers.items()):
        dest = game.skill_record(role,candidate)
        src = game.skill_record(donor,donor_candidate)
        prepared = checked_source(work,role,dest,donor,src)
        if dest.kind == 0:
            plan.skill_words[role,candidate] = (src.action,*dest.words[1:])
            from ogeditor.ps2.guard import supported_absorption
            if supported_absorption(work, donor, donor_candidate)[2] is not None:
                stage_support_name(game,work,plan,role,candidate,donor,src)
            continue
        if isinstance(prepared,tuple):
            source,target,sounds = prepared
            stage_motion(game,work,plan,role,donor,source,target,reserved)
            sound_banks[role].update(sounds)
            plan.skill_words[role,candidate] = dest.words
        else:
            assist_roles.add(role)
            plan.skill_words[role,candidate] = (ASSIST_WORD+prepared,*dest.words[1:])
        stage_support_name(game,work,plan,role,candidate,donor,src)
    elf = plan.elf if plan.elf is not None else game.elf
    space = ElfSpace(elf,prefix_records(elf))
    add_sound_banks(space,sound_banks)
    if assist_roles:
        install_assist_dispatch(space,assist_roles)
    stage_support_links(space,elf,transfers)
    plan.elf = space.finish()
