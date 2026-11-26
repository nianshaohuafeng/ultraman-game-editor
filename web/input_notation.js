export const SEQUENCE = ' ⟶ ';

const arrows = {
  前上: '↗', 后上: '↖', 前下: '↘', 后下: '↙', 前: '→', 后: '←', 上: '↑', 下: '↓'
};

export function inputQuery(text) {
  return text.trim().toLowerCase().replaceAll('＋', '+').replaceAll('■', '□')
    .replaceAll('方块', '□').replaceAll('三角', '△').replaceAll('圆圈', '○').replaceAll('跑动', '跑')
    .replace(/([前后])\s*\+\s*([上下])/g, '$1$2')
    .replace(/向?(前上|后上|前下|后下|前|后|上|下)/g, (_, direction) => arrows[direction])
    .replace(/([□△○×])\s*→\s*(?=[□△○×])/g, '$1')
    .replace(/\s+|⟶|->/g, '');
}
