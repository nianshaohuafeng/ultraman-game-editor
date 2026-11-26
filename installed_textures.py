"""Private POMs embedded in the ANM members of a v7 move-set ISO."""

from collections import defaultdict

from appearance import item_models
from formats import event_records, s16
from move_eboot import BASE
from move_roles import BACK_SKILLS, BLACK_SCENE_SOURCE, JACK_THROW
from move_taro import TARO_DOWN, TARO_PHASES
from move_texture_refs import DRAW_PALETTES, ITEM_SLOTS
from move_throw import LEO_THROW, LEO_THROW_STAGES, LEO_WEAPON_POM


def installed_locations(meta, roles, files):
    rows = meta.get('textures', [])
    masks = meta.get('texture_palettes')
    if masks is not None and (not isinstance(masks, list) or len(masks) != len(rows)):
        raise ValueError('镜像的配招色表记录与贴图目录不一致。')
    copies, usage, spans = {}, {}, defaultdict(list)
    for i, row in enumerate(rows):
        if not isinstance(row, list) or len(row) != 6 or any(type(n) is not int or n < 0 for n in row):
            raise ValueError('ISO 里的配招贴图引用无效。')
        role, state, member, bank, offset, size = row
        if role >= len(roles) or member >= len(files) or bank >= len(files):
            raise ValueError('配招贴图的角色或 YUM 成员无效。')
        cases = [case for case in roles[role]['cases'] if case['anm_banks'][0]['id'] == bank]
        key = role, state, member
        if not cases or size < 64 or offset + size > files[bank][1] or bank in copies.get(key, {}):
            raise ValueError('配招贴图的出场配置或镜像位置无效。')
        copies.setdefault(key, {})[bank] = offset, size
        spans[bank].append((offset, offset + size))
        if masks is not None:
            entry = masks[i]
            if (not isinstance(entry, list) or len(entry) != max(len(c['forms']) for c in cases)
                    or any(n is not None and (type(n) is not int or n < 0 or n.bit_length() > 256) for n in entry)):
                raise ValueError('镜像的配招形态或色表编号无效。')
            usage[key, bank] = entry
    for intervals in spans.values():
        intervals.sort()
        if any(left[1] > right[0] for left, right in zip(intervals, intervals[1:])):
            raise ValueError('镜像中的独立配招贴图位置重叠。')
    return copies, usage


def legacy_usage(game, role, config, shape):
    """Older exports store physical copies but omit draw masks. Follow the mounted BOM."""
    from scan_assets import read_mdl

    case, _, bom, _ = game.form_binding(role, config, shape)
    bank = case['anm_banks'][0]['id']
    roots = defaultdict(set)
    for (actor, root, member), locations in game.installed_move_textures.items():
        if actor == role and bank in locations:
            roots[root].add(member)
    if not roots:
        return {}
    total, _, scripts = game.bank(bom['id'], b'BOM\0')
    result = {}
    for root, members in roots.items():
        origin = game.installed_moves.get((role, root))
        if origin is None:
            continue
        source, move, _ = origin
        donor = game.roles[source]['cases'][0]
        source_poms = [row['id'] for row in donor['common_poms']]
        original = source, move
        phases = set(BACK_SKILLS.get(original, ()))
        if original == LEO_THROW:
            phases.update(LEO_THROW_STAGES)
        elif original == JACK_THROW:
            phases.update((181, 182))
        elif original == TARO_DOWN:
            phases.update(TARO_PHASES)
        elif original == (16, 160):
            phases.add(181)
        states = {root} | {st for (actor, st), (owner, part, _) in game.installed_moves.items()
                           if actor == role and owner == source and part in phases and st not in roots}
        work = list(states)
        while work:
            record = game.actions[role][work.pop()]
            follow = int(record['parameter'], 16)
            if (follow not in states and follow not in roots and (role, follow) in game.installed_moves
                    and game.installed_moves[role, follow][0] == source):
                states.add(follow)
                work.append(follow)
        pending = [game.actions[role][st]['clip'] for st in states]
        seen, effects = set(), set()
        while pending:
            clip = pending.pop()
            if clip in seen or not 0 <= clip < total:
                continue
            seen.add(clip)
            for record in event_records(scripts[clip]):
                raw = record.raw
                if not raw[1] & (1 << shape):
                    continue
                if raw[0] == 3:
                    pending.append(s16(raw, 8))
                elif raw[0] == 8:
                    factory = s16(raw, 4)
                    effects.add(game.installed_effect_sources.get(factory, factory))
        if 73 in effects:  # The parent spawns the 72 projectile.
            effects.add(72)
        for effect in effects:
            for slot, palettes in DRAW_PALETTES.get(effect, {}).items():
                if slot < len(source_poms) and source_poms[slot] in members:
                    result.setdefault((role, root, source_poms[slot]), set()).update(palettes)

        # Accessory MDLs can reference a body POM as well as a private item POM.
        slots = {ITEM_SLOTS[effect] for effect in effects if effect in ITEM_SLOTS}
        models = [(item['member'], item['poms']) for item in
                  item_models(game.image, BASE, donor, game.yum['groups'][donor['group']])
                  if item['slot'] in slots]
        if original == (12, 163):
            _, form, _, textures = game.form_binding(*BLACK_SCENE_SOURCE)
            models.append((form['mdl']['id'], [tex['id'] for tex in textures]))
        for mdl_id, textures in models:
            if not members.intersection(textures):
                continue
            model = read_mdl(game.read_member(mdl_id, b'MDL\0'))
            materials = {idx for mesh in model['meshes'] for idx, _ in mesh['faces']}
            for idx in materials:
                mat = model['materials'][idx]
                slot = mat['texture']
                if 0 <= slot < len(textures) and textures[slot] in members:
                    result.setdefault((role, root, textures[slot]), set()).add(mat['variant'])
        if original == LEO_THROW and LEO_WEAPON_POM in members:
            model = read_mdl(game.read_member(case['forms'][shape]['mdl']['id'], b'MDL\0'))
            body_poms = game.form_binding(role, config, shape)[3]
            for mesh in model['meshes']:
                for idx in {material for material, _ in mesh['faces']}:
                    mat = model['materials'][idx]
                    if mat['texture'] == len(body_poms):
                        result.setdefault((role, root, LEO_WEAPON_POM), set()).add(mat['variant'])
        # Where an old callback carries no recoverable draw evidence, leave the CLUT unknown.
        if not any(key[:2] == (role, root) for key in result):
            result.update({(role, root, member): None for member in members})
    return result
