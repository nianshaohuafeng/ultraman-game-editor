"""Append additional FE3 moves without reusing a stock candidate or action."""

from collections import defaultdict
import struct

from fe3_expand import ElfSpace, erx_append, relocated_halfwords
from fe3_formats import Bom, align, require
from fe3_native import CharacterProgram, RATING_TABLE
from fe3_preview import clip_index
from fe3_scene import camera_bank, scene_address
from fe3_skill_ui import NAME_ROWS, stage_finisher_ui
from fe3_transfer import (audio_slots, finisher, matched_rigs, retarget_clip, scene_camera_slots,
                          script_patch, transplant_bom)


CANDIDATE_TABLES = (0x3C5030, 0x3C5C60, 0x3C6890, RATING_TABLE)
NATIVE_FINISHERS = 0x3E4E78
ROLE_DESCRIPTORS = (0x3E38C0, 0x3F0BC0, 0x3F0C60)
MENU_UNLOCK_CALL = 0x240B08
STOCK_UNLOCK = 0x272258
AVAILABILITY_PTR = 0x78


def pack_words(values):
    return struct.pack(f'<{len(values)}I',*(value & 0xFFFFFFFF for value in values))


def next_candidate(tables, role):
    tables.descriptor(role)
    masks = tables.at(CANDIDATE_TABLES[0]+80*role,'<20i')
    require(-1 in masks and masks.index(-1) < NAME_ROWS, '该角色已用完原生技能名称栏。')
    return masks.index(-1)


def append_clip(bank, clip):
    clips = clip_index(bank)
    added, = clip_index(clip)
    count = len(clips) + 1
    start = align(8 + 4 * count,16)
    entries = [bank[row.start:row.start + row.size] for row in clips]
    entries.append(clip[added.start:added.start + added.size])
    offsets = []
    payload = bytearray()
    for raw in entries:
        offsets.append(start+len(payload))
        payload.extend(raw)
        payload.extend(bytes(-len(payload) % 16))
    header = b'ANM\0' + pack_words((count,*offsets))
    return header.ljust(start,b'\0') + bytes(payload)


def update_animation_banks(space, tables, role, key, start, end, candidate):
    addresses = {tables.pointer(table,role) for table in ROLE_DESCRIPTORS}
    installed = {}
    for address in sorted(addresses - {0,0xFFFFFFFF}):
        desc = tables.words(address,21)
        sizes = list(tables.words(desc[20],14))
        lists = [tables.words(desc[i],sizes[i-4]) for i in range(9,13)]
        starts = list(tables.words(desc[13],sum(map(len,lists))))
        require(any(resource == key[0] and at == start
                    for resource,at in zip(sum(lists,()),starts)),
                '该角色的另一套出战资源使用不同的必杀动作库。')
        more = max(0,candidate+1-sizes[7])
        group_end = sizes[5]+sizes[6]+sizes[7]
        starts[group_end:group_end] = [start]*more
        lists[2] += (key[0],)*more
        sizes[7] += more
        sizes[13] = end
        # The new ANM must load when its bit is set. If the sound slot already
        # existed, put the group in the last, unconditional resource family.
        if not more and lists[2][candidate] != key[0]:
            if not (lists[3] and lists[3][-1] == key[0] and starts[-1] == start):
                lists[3] += (key[0],)
                starts.append(start)
                sizes[8] += 1
                space.patch_word(address+48,space.allocate(pack_words(lists[3])))
        for field, values in ((11,lists[2]),(13,starts),(20,sizes)):
            raw = pack_words(values)
            pointer = installed.get(raw)
            if pointer is None:
                pointer = installed[raw] = space.allocate(raw)
            space.patch_word(address+4*field,pointer)


def select_audio_table(space, rows):
    at = space.allocate(pack_words(rows))
    hi,lo = relocated_halfwords(at)
    for address,opcode in ((0x225CD0,0x3C090000),(0x225D5C,0x3C0F0000)):
        space.patch_word(address,opcode|hi)
    for address,opcode in ((0x225CEC,0x8D2C0000),(0x225D18,0x252C0000),(0x225D64,0x25EF0000)):
        space.patch_word(address,opcode|lo)


def make_visible(space, tables, candidates, prior):
    masks_at = struct.unpack_from('<I',space.elf,AVAILABILITY_PTR)[0] if prior else 0
    if masks_at:
        masks = list(tables.words(masks_at,39))
    else:
        masks = [0]*39
        masks_at = space.allocate(bytes(4*len(masks)))
        space.patch_file(AVAILABILITY_PTR,pack_words((masks_at,)))
        hi,lo = relocated_halfwords(masks_at)
        start = space.allocate(pack_words((
            0x00047880,                 # sll   t7,a0,2
            0x3C0E0000|hi,              # lui   t6,hi(masks)
            0x01EE7821,                 # addu  t7,t7,t6
            0x8DEE0000|lo,              # lw    t6,lo(masks)(t7)
            0x00AE7006,                 # srlv  t6,t6,a1
            0x31CE0001,                 # andi  t6,t6,1
            0x11C00003,0,               # beqz  t6,stock
            0x03E00008,0x24020001,      # jr    ra; li v0,1
            0x08000000 | STOCK_UNLOCK//4,0,
        )))
        require(tables.words(MENU_UNLOCK_CALL,1)[0] == 0x0C000000|STOCK_UNLOCK//4,
                '原生必杀自定义候选调用不匹配。')
        space.patch_word(MENU_UNLOCK_CALL,0x0C000000|start//4)
    for role,candidate in candidates:
        masks[role] |= 1 << candidate
    space.patch(masks_at,pack_words(masks))


def write_candidate(space, role, candidate, shape, kind, value, rating=0):
    for table,word in zip(CANDIDATE_TABLES,(1<<shape,kind,value,rating)):
        space.patch_word(table+80*role+4*candidate,word)


def append_skills(game, work, plan, additions, audio, markers):
    from fe3_support import append_support_candidates, install_assist_dispatch, stage_support_links

    tables = work.tables
    space = ElfSpace(game.elf,game.extension)
    audio_slots_by_skill, _, original_audio = audio_slots(tables)
    new_audio = defaultdict(dict)
    old_sizes = {}
    support_links, assist_roles = {}, set()
    by_role = defaultdict(list)
    for (role,candidate),(donor,owner_id,shape) in sorted(additions.items()):
        by_role[role].append((candidate,donor,owner_id,shape))
    for role, moves in by_role.items():
        start_candidate = next_candidate(tables,role)
        require([row[0] for row in moves] == list(range(start_candidate,start_candidate+len(moves)))
                and moves[-1][0] < NAME_ROWS, '新增技能候选号须连续且在原生名称栏内。')
        for table in CANDIDATE_TABLES:
            space.patch_word(table+80*role+4*(moves[-1][0]+1),-1)
        finishers, support = [], []
        for move in moves:
            src = game.skill_records.get((move[1],move[2]))
            require(src and src.menu_candidate,'请选择来源角色的原生技能候选。')
            (finishers if src.kind == 1 else support).append(move)
        if support:
            append_support_candidates(game,work,space,plan,role,support,support_links,assist_roles)
        if not finishers:
            continue
        stock_rows = tables.skill_rows(role)
        require(stock_rows and all(0 <= row.value < 20 for row in stock_rows),
                '该角色的原生必杀记录布局尚未识别。')
        program = work.program(role)
        template = next((row.action for row in stock_rows if scene_address(work,role,row.action) is not None
                         and program.actions()[row.action][0] == -1),None)
        require(template is not None,'该角色没有可复用的通用必杀演出回调。')
        base = tables.pointer(NATIVE_FINISHERS,role)
        finisher_table = [tables.words(base+20*index,5) for index in range(1+max(row.value for row in stock_rows))]
        desc,sizes = tables.descriptor(role)
        marker, = tables.at(desc[18]+4*(sizes[11]-1),'<i')
        require(marker in markers and marker not in old_sizes,'接收角色没有独立的原生必杀音库段。')
        old_sizes[marker] = sizes[7]
        bank, start = max(tables.banks(role),key=lambda row:row[1])
        key = bank,-1
        original = plan.resources.get(key,game.original(key))
        upper = sizes[13]
        require(start+len(clip_index(original)) == upper, '原生 ANM 的末尾资源范围不连续。')
        next_camera = len(game.children[camera_bank(work,role)])
        new_rows = []
        for index,(candidate,donor,src_id,shape) in enumerate(finishers):
            require(role != donor,'请选择其他来源角色。')
            src, source_form, _, _, rows, anm = finisher(work,donor,src_id)
            target_form = work.form(role,shape)
            original_form = tables.forms(game,role).get(shape)
            require(original_form and not target_form.accessory and 0 <= shape < 3 and target_form.bom and
                    (target_form.model,target_form.bom) == (original_form.model,original_form.bom),
                    '请选择使用原生角色本体和 BOM 的形态。')
            rig_in,rig_out = matched_rigs(game,source_form,target_form)
            source_key, source_index = work.binding(donor)[anm]
            source_bank = game.original(source_key)
            order = clip_index(source_bank)[source_index].tracks
            incoming = retarget_clip(source_bank,source_index,order,rig_in,rig_out)
            plan.resources[key] = append_clip(plan.resources.get(key,original),incoming)
            source_bom = Bom(game.original(source_form.bom))
            target_bom = Bom(plan.resources.get(target_form.bom,game.original(target_form.bom)))
            require(target_bom.scripts <= upper+index,'接收角色的 BOM 已占用这个新增动作号。')
            nodes = {entry[0]:rig_out[channel][0] for channel,entry in rig_in.items()}
            new_clip, = clip_index(incoming)
            upper_frame = new_clip.last_frame
            face = next((program.actions()[row.action][1] for row in stock_rows if row.menu_mask & (1<<shape)
                         and program.actions()[row.action][1] >= 0),None)
            frames = work.animation(role,shape,face)['animation']['last'] if face is not None else upper_frame
            plan.resources[target_form.bom] = transplant_bom(source_bom,anm,source_form,target_bom,upper+index,
                                                             target_form,nodes,frames,upper_frame,face_slot=face)
            Bom(plan.resources[target_form.bom]).validate()
            cameras = {}
            for member in scene_camera_slots(rows):
                camera_key = camera_bank(work,donor), member
                part = game.part(*camera_key)
                require(part.kind == 'cmsp' and not part.compressed, '来源镜头不是原生 CMSP 子项。')
                cameras[member] = next_camera
                plan.cameras[camera_bank(work,role),next_camera] = (part.name,game.original(camera_key))
                next_camera += 1
            state = len(program.actions()) + index
            new_rows.append((template,upper+index,script_patch(rows,len(rows),cameras,src.action,state,nodes)))
            stage_finisher_ui(game, plan, role, candidate, donor, src_id, src.label)
            words = (state,src.words[1],candidate,*src.words[3:])
            finisher_table.append(words)
            write_candidate(space,role,candidate,shape,1,len(finisher_table)-1,tables.finisher_rating(donor,src_id))
            slot = audio_slots_by_skill.get((donor,src_id))
            sound = original_audio[slot] if slot is not None else 140
            new_audio[marker][candidate] = sound
            plan.additions[role,candidate] = (donor,src_id,shape)
            plan.skill_words[role,candidate] = words
        offset = space.allocate(b''.join(pack_words(words) for words in finisher_table))
        space.patch_word(NATIVE_FINISHERS+4*role,offset)
        update_animation_banks(space,tables,role,key,start,upper+len(finishers),finishers[-1][0])
        data = bytearray(program.data)
        for offset,patch in plan.scripts.get(role,{}).items():
            data[offset:offset+len(patch)] = patch
        ordinal = tables.command_ordinal(role)
        plan.modules[role] = erx_append(CharacterProgram(bytes(data),ordinal),ordinal,new_rows)
    shift = 0
    expected_markers = {}
    for marker, index in sorted(markers.items(),key=lambda item:item[1]):
        expected_markers[marker] = index+shift
        slots = new_audio.get(marker)
        if not slots:
            continue
        base = index+1+shift
        old_size = old_sizes[marker]
        extra = max(0,max(slots)+1-old_size)
        audio[base+old_size:base+old_size] = [140]*extra
        shift += extra
        for index,value in slots.items():
            audio[base+index] = value
    require(all(audio.index(marker) == index for marker,index in expected_markers.items()),
            '新增音库编号与原生角色定位值重叠。')
    if new_audio:
        select_audio_table(space,audio)
        # The relocated table already includes replacement audio changes.
        plan.audio.clear()
    if assist_roles:
        install_assist_dispatch(space,assist_roles)
    if support_links:
        stage_support_links(space,game.elf,support_links)
    make_visible(space,tables,plan.additions,game.extension)
    plan.elf = space.finish()

