"""Follow the default PSP battle-state transitions when an animation completes."""
import struct
from move_eboot import BASE

# These update callbacks request the state's +0x0C successor on the
# ordinary battle path. Some wait for outstanding effects before continuing.
END_UPDATES = {0x08899534, 0x088995B8, 0x088987E0, 0x08898980, 0x08898BB8,
               0x0881610C, 0x08816368, 0x08830298}

def completion_update(image, address):
    seen = set()
    while address not in END_UPDATES:
        offset = address - BASE
        if address in seen or not 0 <= offset <= len(image) - 28:
            return False
        seen.add(address)
        words = struct.unpack_from('<7I', image, offset)
        if (words[:2] != (0x27BDFFF0, 0xAFBF0000) or words[2] >> 26 != 3 or
                words[3:] != (0, 0x8FBF0000, 0x03E00008, 0x27BD0010)):
            return False
        address = (words[2] & 0x03FFFFFF) << 2
    return True

def skill_phases(game, role, action):
    states = game.native_actions[role]
    plan = game.current_moves() if game.move_sets else None
    phases = []
    state = action
    while state not in phases:
        move = plan.preview_state(role,state) if plan else states.get(state)
        if not move:
            break
        move = game.action_with_move(role,move)
        if move['clip'] is None or (not phases and move['category'] != 5):
            break
        phases.append(state)
        if not completion_update(game.image,int(move['callbacks'][1],16)):
            break
        state = int(move['parameter'],16)
        if state in (0,1,0xffff):
            break
    return phases

def editable_skill_states(game, role, action, phases):
    """Include known player-input/contact branches as separate edit targets."""
    from ogeditor.psp.move_sets import LEO_FORWARD, LEO_STAGES, BABAR_DOWN
    from ogeditor.psp.role_effects import BACK_SKILLS, JACK_THROW
    from ogeditor.psp.move_taro import TARO_DOWN, TARO_PHASES
    from ogeditor.psp.move_throw import LEO_THROW, LEO_THROW_STAGES
    source = game.move_source(role, action)
    key = (source['role'], source['state']) if source else (role, action)
    special = {LEO_FORWARD: LEO_STAGES, LEO_THROW: (*LEO_THROW_STAGES,161),
               TARO_DOWN: TARO_PHASES, JACK_THROW:(161,181,182), BABAR_DOWN:(161,181)}
    branches = special.get(key, BACK_SKILLS.get(key, ()))
    result = list(phases) or [action]
    if not branches:
        return result
    for original in branches:
        if not source:
            target = original
        elif game.move_sets:
            target = next((state for (r,state), m in game.current_moves().states.items()
                           if r == role and m.root == action and (m.source,m.original) == (key[0],original)),None)
        else:
            matches = [state for (r,state), data in game.installed_moves.items()
                       if r == role and data[:2] == (key[0],original)]
            target = matches[0] if len(matches) == 1 else None
        if target is not None and target not in result:
            result.append(target)
    return result
