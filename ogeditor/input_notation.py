"""Display directions relative to a fighter facing right."""

import re


SEQUENCE = ' ⟶ '
DIRECTIONS = {
    '前上': '↗', '后上': '↖', '前下': '↘', '后下': '↙',
    '前': '→', '后': '←', '上': '↑', '下': '↓',
}
_DIRECTION = re.compile('向?(' + '|'.join(DIRECTIONS) + ')')


def input_arrows(label):
    return _DIRECTION.sub(lambda match: DIRECTIONS[match[1]], label)
