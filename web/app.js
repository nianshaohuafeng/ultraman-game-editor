import {frameIndex, frameToSecond, matchesForm, secondToFrame, speedMap} from './player.js';
import {updateEventList} from './event_list.js';
import {loadImages, Viewport} from './viewport.js';
import {makeWorkbench} from './workbench.js';
import {makeFE3} from './fe3.js';
import {makeExchange, makeMixTabs} from './exchange.js';
import {makeSharedResources} from './shared_resources.js';
import {SEQUENCE, inputQuery as moveQuery} from './input_notation.js';

const $ = id => document.getElementById(id);
const NAMES = ['MAN', 'SEV', 'JACK', 'ACE', 'TARO', 'LEO', 'ZOF', 'FATHER', 'MEB', 'BAL', 'ZET', 'ELEKING',
  'BLACK', 'BAX', 'HIP', 'TEM', 'BABAR'];
const TABS = {overview: '角色总览', attributes: '角色属性', appearance: '外观与贴图', mix: '配招', ai: '电脑 AI',
  source: '形态与资源绑定', geometry: '判定几何', animation: '动画与关键帧',
  commands: '按键与连段解析', all: '帧事件', skill: '技能参数', hit: '招式与判定', damage: '伤害记录',
  finishers: '必杀技配置', throws: '抓取与投技', states: '动作状态表',
  hands: '手型事件', audio: '音效事件表', effects: '特效事件表', music: '角色 BGM',
  resources: '通用资源', changes: '修改记录'};
const EVENT_TABS = ['all', 'skill', 'hit', 'damage', 'hands', 'audio', 'effects'];
const DISPLAY_CONTROLS = [...$('view-options').querySelectorAll('input'),
  $('shading'), $('speed'), $('loop'), $('music-loop'), $('music-volume')];
const TAB_HELP = {
  ai: '按左侧角色和战斗实际难度编辑原生脚本。随机百分比对应所选分支的通过率。修改随工程和新镜像保存。',
  attributes: '奥 0 按当前生命比例选取属性行。Q12 原值 0x1000 等于 1.0 倍。',
  finishers: '选择状态即可播放当前形态的默认片段。进入回调可以再按条件选片。',
  throws: '选择抓取或被投阶段即可在当前角色上播放。对手库片段保留角色条件。',
  animation: '选择骨骼轨道和通道。原生速率、播放长度和关键帧可分别应用。',
  appearance: '模型支持 PSP MDL、OBJ 和 FBX 导入。贴图预览使用选中的色表。',
  damage: '基础伤害按 BOM 记录列出，单位为 HP。实际扣血还受 Q12 攻击倍率、承伤倍率和命中条件影响。',
  geometry: '同一个 BOM 模板可被多个动作引用。可跳到当前动作中使用该模板的帧。',
  commands: '按键表从 YUM 读取。连段窗口来自当前动作的 BOM。',
  mix: '目标动作沿用当前角色的按键。选来源角色和动作，应用到工程后可预览，再另存新镜像。',
  music: '播放并导出当前镜像的原生 ATRAC 曲目。',
  resources: '编辑镜像内的 BGM 与通用贴图，修改可随工程保存或写入新镜像。',
};
const state = {iso: null, role: 0, config: 0, form: 0, model: null, move: undefined, motion: null, events: [], frame: 0,
  playing: false, tab: 'finishers', scope: 'frame', selectedEvent: null, formRequest: 0, motionRequest: 0,
  appearance: 'normal', part: 'body', item: null, modelImages: [], partRequest: 0,
  writing: false, assetIndex: 0, palette: 0, paletteSource: '',
  edits: {count: 0, assets: 0, unsaved: false}};
const hex = (n, digits = 2) => '0x' + n.toString(16).toUpperCase().padStart(digits, '0');
const pad = (n, count) => String(n).padStart(count, '0');
const kind = category => ({4: '攻击', 5: '技能', 11: '投技', 12: '演出'}[category] || '类别 ' + category);
const lower = s => s.trim().toLowerCase();
const moveSelection = move => move.followup || move.extra || move.rage || move.encounter_variant ? [move.id, move.clip] : move.id;
const rowSelection = row => row.dataset.followup === undefined ? Number(row.dataset.action) :
  [Number(row.dataset.action), Number(row.dataset.followup)];
const sameMove = (a, b) => String(a) === String(b);
const damageLabel = records => {
  const values = records.map(hit => hit.value);
  if (!values.length) return '—';
  const low = Math.min(...values), high = Math.max(...values);
  return low === high ? String(low) : low + '–' + high;
};
let viewport, graphicsError;
try {
  viewport = new Viewport($('viewport'));
} catch (error) {
  graphicsError = error.message;
}
const workbench = makeWorkbench({$, state, api, dom, properties, hex, message, setPlaying, selectMove, availableMoves,
  selectEvent, jump, enableHit, damageLabel, write: writeWorkspace, switchTab, playBattle, stopBattle});
const resources = {template: $('shared-resource-template'), api, write: writeWorkspace, message, state, dom, properties};
const kgfResources = makeSharedResources($('shared-resources'), resources);
const fe3Resources = makeSharedResources($('fe3-shared-resources'), resources);
const fe3 = makeFE3({$, state, api, dom, properties, hex, message, write: writeWorkspace, remember: rememberSession,
  updateExchange: () => fe3Exchange.sync(), updateResources: visible => fe3Resources.sync(visible)});
const exchangeOptions = {template: $('exchange-template'), api, write: writeWorkspace, message};
const kgfExchange = makeExchange($('kgf-exchange'), {...exchangeOptions,
  current: () => ({serial: state.iso?.id, actor: [state.role, state.config, state.form]}),
  preview: id => selectMove(id)});
const fe3Exchange = makeExchange($('fe3-exchange'), {...exchangeOptions, fe3: true,
  current: () => ({serial: state.iso?.id, actor: fe3.actor()}),
  preview: id => fe3.previewExchange(id)});
const kgfMixTabs = makeMixTabs($('mix-panel'), name => {
  if (name === 'exchange') kgfExchange.sync();
  rememberSession();
});

function message(text, isError = false) {
  $('status').textContent = text;
  $('status').dataset.error = String(isError);
}

function viewMessage(text) {
  const label = $('view-message');
  label.textContent = graphicsError || text;
  label.hidden = !label.textContent;
}

function changeSummary({count, assets, native, templates, notes, moves, ai}) {
  const parts = [];
  if (assets) parts.push(assets + ' 项资源');
  if (count) parts.push(count + ' 条事件');
  if (native) parts.push(native + ' 项配置');
  if (templates) parts.push(templates + ' 个模板');
  if (moves) parts.push(moves + ' 项配招');
  if (ai) parts.push(ai + ' 项 AI 参数');
  if (notes) parts.push(notes + ' 条备注');
  return parts.join(' / ') || '0 项修改';
}

function renderEditControls() {
  $('open').disabled = state.writing;
  $('open-project').disabled = state.writing;
  $('save-iso').disabled = state.writing || !state.iso;
  $('save-project').disabled = state.writing || !state.iso;
  $('save-package').disabled = state.writing || !state.iso;
  $('undo').disabled = state.writing || !state.edits.undo;
  $('redo').disabled = state.writing || !state.edits.redo;
  $('edit-inputs').disabled = state.writing;
  const label = $('change-count');
  const {count, assets, native, templates, notes, moves, ai, unsaved} = state.edits;
  label.hidden = !count && !assets && !native && !templates && !notes && !moves && !ai && !unsaved;
  label.textContent = changeSummary(state.edits) + (unsaved ? ' · 未另存' : ' · 已另存');
  label.dataset.unsaved = String(unsaved);
  renderAssetControls();
}

async function api(method, ...args) {
  const response = await window.pywebview.api[method](...args);
  if (response.error !== undefined) throw new Error(response.error);
  return response.value;
}

function currentView() {
  if (state.iso?.game === 'fe3') return fe3.view();
  const {role, config, form, frame, tab, scope, appearance, part, selectedEvent, move: action} = state;
  return {role, config, form, frame, tab, scope, appearance, part, selectedEvent, action,
    aiLevel:Number($('ai-level').value), mixPane:kgfMixTabs.value};
}

const displayKey = control => control.id || control.dataset.option || control.dataset.colliderLayer;
let sessionReady = false, lastSession, sessionRevision = 0, sessionQueued = false;

function rememberSession() {
  if (!sessionReady || state.writing || sessionQueued) return;
  sessionQueued = true;
  queueMicrotask(() => {
    sessionQueued = false;
    if (state.writing) return;
    const display = Object.fromEntries(DISPLAY_CONTROLS.map(control => [
      displayKey(control), control.type === 'checkbox' ? control.checked : control.value,
    ]));
    display.nav = $('toggle-nav').getAttribute('aria-expanded') === 'true';
    display.help = !$('editor-help').hidden;
    display.combos = $('move-combos').getAttribute('aria-pressed') === 'true';
    const args = [state.iso?.id ?? null, state.iso ? currentView() : null, display];
    const snapshot = JSON.stringify(args);
    if (snapshot === lastSession) return;
    lastSession = snapshot;
    api('remember_session', args[0], ++sessionRevision, args[1], args[2]).catch(error => {
      if (lastSession !== snapshot) return;
      lastSession = undefined;
      message('无法记录上次设置 · ' + error.message, true);
    });
  });
}

function restoreDisplay(display) {
  for (const control of DISPLAY_CONTROLS) {
    const value = display?.[displayKey(control)];
    if (control.type === 'checkbox') {
      if (typeof value !== 'boolean') continue;
      control.checked = value;
    } else if (control.type === 'range') {
      if (typeof value !== 'string' || !value || !Number.isFinite(Number(value))) continue;
      control.value = value;
      control.dispatchEvent(new Event('input', {bubbles: true}));
    } else {
      if (![...control.options].some(option => option.value === value)) continue;
      control.value = value;
    }
    control.dispatchEvent(new Event('change', {bubbles: true}));
  }
  if (typeof display?.nav === 'boolean') toggleNavigation(display.nav);
  if (typeof display?.help === 'boolean') toggleHelp(display.help);
  if (typeof display?.combos === 'boolean') $('move-combos').setAttribute('aria-pressed', String(display.combos));
}

function dom(type, text, className = '') {
  const item = document.createElement(type);
  item.textContent = text;
  if (className) item.className = className;
  return item;
}

function choose(rows, field) {
  for (const button of rows) {
    const active = Object.values(button.dataset).includes(field);
    button.classList.toggle('active', active);
    button.setAttribute('aria-pressed', String(active));
  }
}

function renderRoles() {
  const current = $('roles').scrollTop;
  const filter = lower($('role-search').value);
  const roles = [];
  state.iso?.roles.forEach((role, i) => {
    const id = pad(i, 2);
    if (filter && !lower([role.name, id, NAMES[i]].join(' ')).includes(filter)) return;
    const button = dom('button', '');
    button.type = 'button';
    button.dataset.role = i;
    button.setAttribute('aria-pressed', String(i === state.role));
    if (i === state.role) button.classList.add('active');
    button.append(dom('span', id, 'role-id'), dom('span', role.name), dom('span', NAMES[i], 'role-code'));
    roles.push(button);
  });
  $('roles').replaceChildren(...roles);
  if (!roles.length && state.iso) $('roles').append(dom('p', '没有匹配的角色', 'empty'));
  $('roles').scrollTop = current;
}

function selectedConfig() {
  return state.iso.roles[state.role].cases[state.config];
}

function renderSelectors() {
  const list = state.iso?.roles[state.role].cases || [];
  $('config').replaceChildren(...list.map((entry, index) => new Option(
    entry.parameter === -1 ? '默认出场' :
      '参数 ' + pad(entry.parameter, 2) + (entry.opponent == null ? ' · 组 ' + entry.group : ' · 对' + state.iso.roles[entry.opponent].name),
    index)));
  $('config').value = state.config;
  $('config').disabled = list.length <= 1;
  const group = list[state.config];
  const shapes = group?.forms || [];
  $('shape').replaceChildren(...shapes.map((id, i) => new Option('形态 ' + i + ' · MDL ' + id, i)));
  $('shape').value = state.form;
  $('shape').disabled = shapes.length <= 1;
  $('role-source').textContent = group ? 'YUM 组 ' + pad(group.group, 3) : '';
  renderModelSelectors();
}

function renderModelSelectors() {
  const model = state.model;
  const appearances = model?.appearances.length ? model.appearances : [{key: 'normal', name: '原始'}];
  if (model && !appearances.some(appearance => appearance.key === state.appearance)) state.appearance = 'normal';
  $('model-appearance').replaceChildren(...appearances.map(appearance => new Option(appearance.name, appearance.key)));
  $('model-appearance').value = state.appearance;
  $('model-appearance').disabled = appearances.length < 2 || Boolean(state.item);
  const parts = [new Option('角色本体', 'body')];
  if (model?.parts.length) {
    parts.push(new Option('本体与附加部件', 'all'), ...model.parts.map(part => new Option(part.name, part.key)));
  }
  for (const item of model?.items || []) parts.push(new Option(item.name ?? ('道具 ' + (item.slot + 1) + ' · MDL ' + item.member), 'item:' + item.slot));
  $('model-part').replaceChildren(...parts);
  $('model-part').value = state.part;
  $('model-part').disabled = parts.length < 2;
}

async function selectModelPart(key) {
  stopBattle();
  if (!state.model) {
    $('model-part').value = state.part;
    return;
  }
  const request = ++state.partRequest;
  setPlaying(false);
  if (key.startsWith('item:')) {
    try {
      const slot = Number(key.slice(5));
      const model = await api('read_item_model', state.iso.id, state.role, state.config, state.form, slot);
      const images = viewport ? await loadImages(model.textures) : [];
      if (request !== state.partRequest) return;
      state.item = model;
      viewport?.setModel(model, images);
      if (model.notes.length) message(model.notes.join(' · '));
    } catch (error) {
      if (request === state.partRequest) {
        $('model-part').value = state.part;
        message(error.message, true);
      }
      return;
    }
  } else {
    if (state.item) {
      state.item = null;
      viewport?.setModel(state.model, state.modelImages);
      viewport?.setMotion(state.motion?.animation || null, state.events);
    }
    viewport?.setAppearance(state.appearance);
    viewport?.setPart(key);
  }
  state.part = key;
  renderModelSelectors();
  frameAt(state.frame);
  rememberSession();
}

function availableMoves() {
  const moves = state.model?.moves || [];
  if (state.appearance !== 'rage') return moves.filter(move => !move.rage);
  const variants = moves.filter(move => move.rage);
  const states = new Set(variants.map(move => move.id));
  const clips = new Set(variants.map(move => move.clip));
  return moves.filter(move => move.rage || (move.extra ? !clips.has(move.clip) : !states.has(move.id)));
}

function matchingMove(moves, selection) {
  const base = Array.isArray(selection) ? selection[0] : selection;
  return moves.find(move => sameMove(moveSelection(move), selection)) ||
    (base === -1 ? moves.find(move => move.rage && move.clip === selection[1]) :
      moves.find(move => !move.followup && !move.encounter_variant && move.id === base));
}

function actionTable() {
  const filter = moveQuery($('move-search').value);
  const combosOnly = $('move-combos').getAttribute('aria-pressed') === 'true';
  const moves = availableMoves().sort((a, b) => a.clip - b.clip || a.id - b.id);
  const rows = [];
  for (const move of moves) {
    if (combosOnly && !move.followup && !move.has_combo) continue;
    const inputs = move.combo_paths?.join(' / ') || move.inputs.join(SEQUENCE);
    const tag = move.rage ? '暴走' : move.encounter_variant ? '对手分支' :
      move.followup ? '连段后续' : move.has_combo ? '默认片段' : '';
    const name = [...new Set([move.name, inputs].filter(Boolean))].join(' · ') || kind(move.category);
    const damage = damageLabel(move.damage);
    const terms = [move.id, '状态 ' + pad(move.id, 3), hex(move.id), move.clip, 'ANM ' + pad(move.clip, 3),
      name, tag, kind(move.category), tag && '连招', move.damage.length && '伤害'];
    if (filter && !terms.some(term => moveQuery(String(term)).includes(filter))) continue;
    const tr = dom('tr', '');
    tr.dataset.action = move.id;
    tr.dataset.anm = move.clip;
    if (Array.isArray(moveSelection(move))) tr.dataset.followup = move.clip;
    tr.tabIndex = 0;
    const source = (move.extra ? '' : '状态 ' + pad(move.id, 3) + ' · ' + move.va + ' · ') +
      'YUM ' + move.anm + ' / ANM ' + move.clip;
    tr.title = move.followup ? name + '\n' + source + '\n接自 ANM ' + pad(move.combo_source, 3) +
      ' · 第 ' + move.combo_frame + ' 帧开放输入' : name + '\n' + source;
    if (sameMove(state.move, moveSelection(move))) tr.className = 'active';
    const description = dom('td', name, 'move-name');
    if (tag) description.append(dom('span', ' [' + tag + ']', 'move-tag'));
    if (move.edited) description.append(dom('span', ' · 已替换', 'move-tag'));
    if (move.move_source) description.append(dom('span', ' · 配招 ' + move.move_source.name, 'move-tag'));
    const damageCell = dom('td', damage, move.damage.length ? 'mono damage-value' : 'mono');
    damageCell.title = move.damage.map(hit => '第 ' + hit.frame + ' 帧 · ' + hit.value + ' HP · ' + hex(hit.op)).join('\n');
    tr.append(dom('td', pad(move.clip, 3), 'mono'), description, dom('td', move.last == null ? '—' : move.last + 1, 'mono'),
      damageCell, dom('td', move.events, 'mono'));
    rows.push(tr);
  }
  $('moves').replaceChildren(...rows);
  $('move-combos').disabled = !state.model;
  $('move-empty').textContent = state.model ? (combosOnly ? '没有匹配的连招' : '没有匹配的动作') : '请选择角色';
  $('move-empty').hidden = Boolean(rows.length);
}

function renderContext() {
  const motion = state.motion;
  if (!state.model) {
    $('editor-context').textContent = '请选择动作';
    $('footer-source').textContent = '';
    return;
  }
  const name = state.iso.roles[state.role].name;
  const parts = [pad(state.role, 2) + ' ' + name + ' · ' + NAMES[state.role], '形态 ' + state.form];
  if (motion) {
    if (!motion.action.extra) parts.push('状态 ' + pad(motion.action.id, 3));
    parts.push('ANM ' + pad(motion.action.clip, 3));
  }
  if (motion?.action.followup) parts.push('连段后续');
  if (motion?.action.encounter_variant) parts.push('对手分支');
  if (motion?.action.rage) parts.push('暴走');
  $('editor-context').textContent = parts.join(' · ');
  $('footer-source').textContent = 'YUM ' + state.model.group + '  ·  MDL ' + state.model.mdl +
    (motion ? '  ·  BOM ' + motion.bom : '');
}

function properties(node, pairs) {
  node.replaceChildren(...pairs.flatMap(([label, text]) => [
    dom('dt', label), dom('dd', Array.isArray(text) ? text.join(' / ') : text ?? '—'),
  ]));
}

function isAssetTab() {
  return state.tab === 'appearance' || state.tab === 'animation';
}

function currentAsset() {
  if (state.tab === 'animation') return state.motion?.asset;
  if (state.tab === 'appearance') return state.model?.assets[state.assetIndex];
}

function renderAssetControls() {
  const asset = currentAsset();
  for (const id of ['import-asset', 'export-asset', 'export-texture-png', 'asset-palette']) {
    $(id).disabled = state.writing || !asset;
  }
  $('restore-asset').disabled = state.writing || !asset?.edited;
  $('import-palette').hidden = asset?.kind !== 'pom';
  $('import-palette').disabled = state.writing;
  $('restore-asset').textContent = asset?.paired ? '还原模型和贴图' : '还原';
  $('import-fe3-model').hidden = asset?.kind !== 'mdl';
  $('import-fe3-model').disabled = state.writing;
}

function renderAssets() {
  const appearance = state.tab === 'appearance';
  const assets = state.model?.assets || [];
  if (assets.length && state.assetIndex >= assets.length) state.assetIndex = 0;
  $('appearance-table').hidden = !appearance || !assets.length;
  $('asset-rows').replaceChildren(...(appearance ? assets.map((asset, index) => {
    const row = dom('tr', '', index === state.assetIndex ? 'active' : '');
    row.dataset.asset = index;
    row.tabIndex = 0;
    const title = asset.label || (asset.kind === 'mdl' ? '模型' : '贴图槽 ' + asset.slot);
    const spec = asset.kind === 'mdl' ? 'MDL' : asset.width + ' × ' + asset.height;
    row.append(dom('td', title + (asset.edited ? ' · 已替换' : '')),
      dom('td', asset.member, 'mono'), dom('td', spec, 'mono'));
    return row;
  }) : []));

  const asset = currentAsset();
  $('asset-empty').hidden = Boolean(asset);
  $('asset-empty').textContent = appearance ? '请选择角色和形态' : '从动作列表选择一个动作';
  $('asset-detail').hidden = !asset;
  renderAssetControls();
  if (!asset) {
    $('texture-preview').hidden = true;
    return;
  }

  const motion = state.motion;
  const texture = asset.kind === 'pom';
  const name = {mdl: '模型', pom: '贴图槽 ' + asset.slot,
    anm: 'ANM ' + pad(motion?.action.clip ?? 0, 3)}[asset.kind];
  $('asset-title').textContent = (asset.label || name) + (asset.edited ? ' · 已替换' : '');
  const fields = [['格式', asset.kind.toUpperCase()], [asset.move_texture ? '来源 POM' : 'YUM 成员', asset.member],
    ['大小', asset.size.toLocaleString() + ' B']];
  if (asset.carrier != null) fields.push(['当前配置的镜像位置', 'YUM ' + asset.carrier + ' + ' + hex(asset.carrier_offset)]);
  if (texture) fields.push(['尺寸', asset.width + ' × ' + asset.height],
    [asset.move_texture ? '游戏使用色表' : '模型使用色表',
      asset.active_palettes.length ? asset.active_palettes.join('、') : (asset.palette_unknown ? '旧镜像未记录' : '—')]);
  if (asset.kind === 'anm') fields.push(['库内片段', motion.slot], ['播放末帧', motion.animation.last]);
  properties($('asset-fields'), fields);

  const notes = [];
  if (asset.copies > 1) notes.push('修改将同步到本招的 ' + asset.copies + ' 份出场资源。');
  if (asset.forms > 1) notes.push('此资源在 ' + asset.forms + ' 个出场配置或形态中共用。');
  if (asset.kind === 'anm') {
    if (asset.actions > 1) notes.push('当前形态有 ' + asset.actions + ' 个动作共用此片段。');
    const late = state.events.filter(event => event.frame > motion.animation.last).length;
    if (late) notes.push(late + ' 条 BOM 事件位于播放末帧之后。');
  }
  $('asset-note').textContent = notes.join(' ');
  $('asset-note').hidden = !notes.length;
  $('asset-palette-row').hidden = !texture;
  $('export-texture-png').hidden = !texture;
  $('export-asset').textContent = '导出 ' + asset.kind.toUpperCase();
  if (texture) {
    const source = [state.role, state.config, state.form, asset.slot, asset.member, asset.move_state].join('/');
    if (state.paletteSource !== source || state.palette >= asset.palettes) {
      state.palette = asset.active_palettes[0] ?? 0;
      state.paletteSource = source;
    }
    $('asset-palette').replaceChildren(...Array.from({length: asset.palettes}, (_, i) =>
      new Option(asset.move_texture && asset.active_palettes.includes(i) ? i + '（游戏使用）' : i, i)));
    $('asset-palette').value = state.palette;
  }
  renderTexture();
}

let textureRequest = 0;
async function renderTexture() {
  const request = ++textureRequest;
  const asset = currentAsset();
  $('texture-preview').hidden = true;
  if (asset?.kind !== 'pom') return;
  try {
    const uri = await api('read_texture', state.iso.id, state.role, state.config, state.form, asset.slot, state.palette);
    if (request === textureRequest && state.tab === 'appearance') {
      $('texture-image').src = uri;
      $('texture-preview').hidden = false;
    }
  } catch (error) {
    if (request === textureRequest) message(error.message, true);
  }
}

function renderSource() {
  const model = state.model, motion = state.motion;
  if (!model) {
    $('binding-rows').replaceChildren();
    properties($('source-fields'), [['资源', '请选择角色']]);
    return;
  }
  $('binding-rows').replaceChildren(...model.forms.map(form => {
    const tr = dom('tr', '', form.shape === state.form ? 'active' : '');
    tr.dataset.form = form.shape;
    tr.tabIndex = 0;
    tr.append(...[form.shape, form.mdl, form.anm, form.bom, form.poms.join(' / ')].map(n => dom('td', n, 'mono')));
    return tr;
  }));
  const data = [['角色', state.iso.roles[state.role].name], ['YUM 组', model.group], ['MDL', model.mdl],
    ['贴图', model.textures.map(pom => 'YUM ' + pom.id + ' / 色表 ' + pom.palette)],
    ['MDL 节点', model.bones.length], ['角色语音库', model.voice]];
  if (motion) data.push(
    ['动作表地址', motion.action.va], ['类别 / 标志', [kind(motion.action.category), motion.action.flags]],
    ['进入 / 更新 / 退出', motion.action.callbacks.map(x => x || '—')],
    ['ANM 成员 / 本地片段', [motion.anm, motion.slot]],
    ['ANM 镜像偏移', motion.anm_iso == null ? '待写入' : hex(motion.anm_iso, 8)],
    ['合并片段', motion.action.clip], ['出场对手', motion.opponent == null ? '—' : state.iso.roles[motion.opponent].name],
    ['播放末帧 / 曲线末帧', [motion.animation.last, motion.animation.curve]], ['ANM 基础速率', motion.animation.rate],
    ['BOM 成员 / 脚本', [motion.bom, motion.bom_clip]]);
  if (motion?.action.move_source) {
    const source = motion.action.move_source;
    data.push(['配招来源', `${source.name} · 状态 ${source.state} · ANM ${source.clip}`]);
  }
  properties($('source-fields'), data);
}

let eventListSource, eventListTab, eventRows = [], detailKey = null;
const sound = new Audio();
function hasDraft() {
  return state.iso?.game !== 'fe3' && Boolean($('edit-fields').querySelector('[data-changed="true"]'));
}

function renderEventEditor(event) {
  $('event-edit').hidden = !event?.editable.length;
  const fields = (event?.editable || []).map(field => {
    const label = dom('label', field.label);
    label.title = field.help ? hex(field.offset) + ' · ' + field.help : hex(field.offset);
    const input = dom('input', '');
    input.type = 'number';
    input.step = 1;
    input.required = true;
    input.min = Math.min(field.value, field.min);
    input.max = Math.max(field.value, field.max);
    input.name = field.offset;
    input.defaultValue = field.value;
    label.append(input);
    return label;
  });
  $('edit-fields').replaceChildren(...fields);
  $('apply-event').disabled = true;
  $('restore-event').disabled = !event?.edited;
  $('audition-event').hidden = event?.audition == null;
  $('audition-event').disabled = false;
}

function renderEventDetail(event) {
  const key = event ? [state.motionRequest, event.id, event.raw].join('/') : null;
  if (key === detailKey) return;
  sound.pause();
  detailKey = key;
  $('record-title').textContent = event ? event.name + ' · 帧 ' + event.frame : '事件详情';
  $('record-empty').hidden = Boolean(event);
  $('raw-details').hidden = !event;
  $('raw-bytes').textContent = event?.raw || '';
  properties($('record-fields'), event ? [
    ['操作码 / 形态掩码', [hex(event.op), hex(event.mask)]],
    ...Object.entries(event.fields),
    ['BOM 成员内偏移', hex(event.offset, 6)],
    ['镜像偏移', state.motion.anm_iso == null ? '待写入' : hex(state.motion.bom_iso + event.offset, 8)],
    ...(event.overlap ? [['原目录', '此记录与相邻事件的字节区间相交']] : []),
  ] : []);
  renderEventEditor(event);
}

function matchesTab(event) {
  return state.tab === 'all' || event.kind === state.tab ||
    (state.tab === 'damage' && event.damage !== undefined) ||
    (state.tab === 'hands' && event.op === 16) || (state.tab === 'effects' && event.op === 21);
}

function renderEvents() {
  if (!EVENT_TABS.includes(state.tab)) return;
  const frame = frameIndex(state.frame);
  if (eventListSource !== state.events || eventListTab !== state.tab) {
    eventListSource = state.events;
    eventListTab = state.tab;
    eventRows = state.events.filter(matchesTab).map(event => {
      const tr = dom('tr', '');
      tr.dataset.event = event.id;
      tr.tabIndex = 0;
      const name = dom('td', event.name, event.edited ? 'event-name edited' : 'event-name');
      name.dataset.kind = event.kind;
      tr.append(dom('td', event.frame, 'mono'), name, dom('td', event.damage ?? '—', 'mono'),
        dom('td', hex(event.op), 'mono'), dom('td', ''));
      const idleStatus = event.until > event.frame ? event.frame + '–' + (event.until - 1) : '—';
      return {item:event, tr, idleStatus};
    });
    $('event-rows').replaceChildren(...eventRows.map(({tr}) => tr));
  }
  const currentOnly = state.scope === 'frame';
  const selected = updateEventList(eventRows, {
    frame, currentOnly, selectedId:state.selectedEvent, showCount:Boolean(state.motion),
    counter:$('event-count'), empty:$('event-empty'),
    emptyText:!state.motion ? '请选择动作' : currentOnly ? '当前帧没有此类事件' : '此动作没有此类事件',
  });
  renderEventDetail(selected);
}

function renderSection() {
  choose($('sections').querySelectorAll('button'), state.tab);
  $('editor-title').textContent = TABS[state.tab];
  $('workspace-section').textContent = $('sections').querySelector('[data-tab="' + state.tab + '"]')
    ?.closest('.nav-group').querySelector('.nav-label').textContent || '动作与战斗';
  $('editor-help').textContent = TAB_HELP[state.tab] || '从动作和资源列表选择记录后查看详细参数。';
  $('source-panel').hidden = state.tab !== 'source';
  $('asset-panel').hidden = !isAssetTab();
  $('event-panel').hidden = !EVENT_TABS.includes(state.tab);
  if (state.tab === 'source') renderSource();
  else if (isAssetTab()) renderAssets();
  else if (EVENT_TABS.includes(state.tab)) renderEvents();
  kgfResources.sync(state.tab === 'resources' && Boolean(state.iso));
  workbench.render();
  if (state.tab === 'mix') kgfExchange.sync();
}

function switchTab(tab, scope) {
  if (!Object.hasOwn(TABS, tab)) return;
  if (state.battle && tab !== state.tab) stopBattle();
  if (state.tab !== tab) {
    $('linked-search').value = '';
    document.querySelector('.inspector').scrollTop = 0;
  }
  state.tab = tab;
  if (scope) {
    state.scope = scope;
    choose($('scope').querySelectorAll('button'), scope);
  }
  renderSection();
  rememberSession();
}

function enableHit(layer) {
  const layerToggle = $('collider-key').querySelector('[data-collider-layer="' + layer + '"]');
  if (layerToggle && !layerToggle.checked) {
    layerToggle.checked = true;
    layerToggle.dispatchEvent(new Event('change', {bubbles: true}));
  }
  const cb = $('view-options').querySelector('[data-option="hit"]');
  if (!cb.checked) {
    cb.checked = true;
    cb.dispatchEvent(new Event('change', {bubbles: true}));
  }
}

function markers() {
  const priority = {other: 0, skill: 1, audio: 2, hit: 3, camera: 4, effects: 5};
  const frames = new Map();
  const last = lastFrame();
  for (const event of state.battle?.events ?? state.events) {
    if (event.frame < 0 || event.frame > last) continue;
    const prev = frames.get(event.frame);
    if (!prev || priority[prev.kind] < priority[event.kind]) frames.set(event.frame, event);
  }
  const buttons = Array.from(frames, ([frame, event]) => {
    const button = dom('button', '');
    button.type = 'button';
    if (state.battle) button.onclick = () => jump(frame, false);
    else button.dataset.event = event.id;
    button.dataset.kind = event.kind;
    button.style.left = 100 * frame / (last || 1) + '%';
    button.title = '第 ' + frame + ' 帧 · ' + event.name;
    button.setAttribute('aria-label', button.title);
    return button;
  });
  $('timeline-markers').replaceChildren(...buttons);
}

const lastFrame = () => Math.ceil(state.battle?.last ?? state.motion?.animation.last ?? 0);
const firstFrame = () => state.battle ? 0 : state.motion?.animation.first ?? 0;

function frameAt(frame) {
  const last = lastFrame();
  state.frame = Math.min(last, Math.max(0, Number.isFinite(frame) ? frame : 0));
  const integer = frameIndex(state.frame);
  $('scrub').value = state.frame;
  if (document.activeElement !== $('frame-number')) $('frame-number').value = integer;
  $('last-frame').textContent = last;
  $('frame-label').textContent = state.motion ? pad(integer, 3) : '—';
  const model = state.item || state.model;
  const part = state.model?.parts.find(item => item.key === state.part);
  $('view-stats').textContent = !model ? '' : state.item
    ? '道具 MDL ' + model.member + '  ·  ' + model.triangles.toLocaleString() + ' 三角面'
    : (part ? part.name : model.bones.length + ' 节点  ·  ' + model.triangles.toLocaleString() + ' 三角面') +
      (state.motion ? '  ·  帧 ' + state.frame.toFixed(1) : '');
  viewport?.setFrame(state.frame);
  if (state.battle) workbench.battleFrame(state.frame);
  else renderEvents();
}

let lastTick, frameRequest;
function tick(time) {
  if (!state.playing || !state.motion) return;
  if (lastTick === undefined) lastTick = time;
  const step = Math.min(0.05, (time - lastTick) / 1000) * Number($('speed').value);
  lastTick = time;
  const last = lastFrame();
  const map = !state.battle && $('native-rate').checked ? state.speedMap : [{frame: 0, second: 0, rate: 1}];
  const start = frameToSecond(map,firstFrame());
  const end = frameToSecond(map, last);
  let seconds = frameToSecond(map, state.frame) + step;
  if (seconds >= end) {
    if ($('loop').checked && end > start) seconds = start + (seconds - start) % (end - start);
    else {
      frameAt(last);
      setPlaying(false);
      return;
    }
  }
  frameAt(secondToFrame(map, seconds));
  frameRequest = requestAnimationFrame(tick);
}

function setPlaying(play) {
  if (state.playing === play) return;
  state.playing = play;
  $('play-text').textContent = play ? '暂停' : '播放';
  $('play-icon').textContent = play ? 'Ⅱ' : '▶';
  if (play) {
    if (state.frame < firstFrame() || state.frame >= lastFrame()) frameAt(firstFrame());
    lastTick = undefined;
    frameRequest = requestAnimationFrame(tick);
  } else {
    cancelAnimationFrame(frameRequest);
    rememberSession();
  }
}

function setPlayerEnabled(enabled) {
  for (const id of ['play', 'prev', 'next', 'scrub', 'frame-number']) $(id).disabled = !enabled;
  const last = String(enabled ? lastFrame() : 0);
  $('scrub').max = last;
  $('frame-number').max = last;
}

function togglePlay() {
  if (!state.writing && lastFrame() > 0) setPlaying(!state.playing);
}

function jump(frame, remember = true) {
  if (!state.motion) return;
  setPlaying(false);
  frameAt(frame);
  $('frame-number').value = frameIndex(state.frame);
  if (remember) rememberSession();
}

function selectEvent(id, seekTrigger = true) {
  const event = state.events.find(item => item.id === id);
  if (!event || state.writing) return;
  setPlaying(false);
  state.selectedEvent = id;
  if (!matchesTab(event)) state.tab = 'all';
  if (seekTrigger && event.frame >= 0 && event.frame <= state.motion.animation.last) jump(event.frame);
  renderSection();
  rememberSession();
}

function useMotion(motion) {
  state.motion = motion;
  state.events = motion.events.filter(event => matchesForm(event, state.form));
  state.speedMap = speedMap(motion.animation, state.events);
}

let battleRequest = 0;

function stopBattle() {
  ++battleRequest;
  if (!state.battle) return;
  setPlaying(false);
  state.battle = null;
  workbench.battleFrame(0);
  viewport?.setMotion(state.motion?.animation ?? null, state.events);
  setPlayerEnabled(Boolean(state.motion));
  markers();
  frameAt(0);
  viewMessage('');
}

async function playBattle(action, target, camera) {
  if (!viewport || !state.model || state.writing) return;
  if (state.item || state.part !== 'body') await selectModelPart('body');
  if (state.motion?.action.id !== action) await selectMove(action);
  const request = ++battleRequest, form = state.formRequest, selection = state.motionRequest;
  setPlaying(false);
  const current = () => request === battleRequest && form === state.formRequest && selection === state.motionRequest;
  const preview = await api('read_battle', state.iso.id, state.role, state.config, state.form, action, target, camera);
  if (!current()) return;
  const opponent = preview.actors[1].length ? await api('read_model', state.iso.id, target, 0, 0) : null;
  const images = opponent ? await loadImages(opponent.textures) : [];
  if (!current()) return;
  state.battle = preview;
  viewport.setBattleScene(preview, opponent, images);
  viewMessage(preview.name + ' · 同步预览');
  workbench.showBattle();
  setPlayerEnabled(true);
  markers();
  frameAt(0);
  if (lastFrame() > 0) setPlaying(true);
}

async function selectMove(selection, play = false) {
  if (!state.model || selection === undefined) return;
  const move = matchingMove(availableMoves(), selection);
  if (!move) return;
  stopBattle();
  selection = moveSelection(move);
  if ($('model-part').value.startsWith('item:')) selectModelPart('body');
  if (play && !Array.isArray(selection) && move.phases?.length > 1) {
    const form = state.formRequest;
    await selectMove(selection, false);
    if (form === state.formRequest && state.motion && sameMove(state.move, selection)) {
      const target = Number($('linked-opponent').value || (state.role === 4 ? 0 : 4));
      await playBattle(selection, target, -1);
    }
    return;
  }
  if (state.motion && sameMove(state.move, selection)) {
    if (play && state.motion.animation.last > 0) setPlaying(true);
    return;
  }
  const request = ++state.motionRequest;
  const form = state.formRequest;
  setPlaying(false);
  state.motion = null;
  state.events = [];
  state.move = selection;
  state.selectedEvent = null;
  renderEventDetail(null);
  viewport?.setMotion(null, []);
  setPlayerEnabled(false);
  markers();
  frameAt(0);
  renderContext();
  renderSection();
  for (const row of $('moves').children) {
    const active = sameMove(rowSelection(row), selection);
    row.classList.toggle('active', active);
    if (active) row.scrollIntoView({block: 'nearest'});
  }
  message(Array.isArray(selection) ? '正在读取 ANM ' + pad(selection[1], 3) : '正在读取动作 ' + pad(selection, 3));
  try {
    const motion = await api('read_motion', state.iso.id, state.role, state.config, state.form, selection);
    if (request !== state.motionRequest || form !== state.formRequest) return;
    useMotion(motion);
    viewport?.setMotion(motion.animation, state.events);
    setPlayerEnabled(true);
    markers();
    renderContext();
    renderSection();
    frameAt(firstFrame());
    message(graphicsError || state.model.notes.join(' · ') || '就绪', Boolean(graphicsError));
    rememberSession();
    if (play && motion.animation.last > 0) setPlaying(true);
  } catch (error) {
    if (request === state.motionRequest && form === state.formRequest) message(error.message, true);
  }
}

async function loadForm(preferred) {
  stopBattle();
  if (!state.iso) return;
  const request = ++state.formRequest;
  state.motionRequest++;
  state.partRequest++;
  setPlaying(false);
  state.motion = null;
  state.model = null;
  state.part = 'body';
  state.item = null;
  state.modelImages = [];
  state.move = undefined;
  state.events = [];
  state.selectedEvent = null;
  renderEventDetail(null);
  setPlayerEnabled(false);
  renderSelectors();
  actionTable();
  viewport?.clear();
  viewMessage('正在准备模型和贴图');
  message('正在读取角色资源');
  markers();
  frameAt(0);
  renderContext();
  renderSection();
  try {
    const model = await api('read_model', state.iso.id, state.role, state.config, state.form);
    if (request !== state.formRequest) return;
    const images = viewport ? await loadImages(model.textures) : [];
    if (request !== state.formRequest) return;
    viewport?.setModel(model, images);
    state.model = model;
    state.modelImages = images;
    renderModelSelectors();
    viewport?.setAppearance(state.appearance);
    viewMessage('');
    renderContext();
    actionTable();
    renderSection();
    const moves = availableMoves();
    const move = matchingMove(moves, preferred) || moves[0];
    if (move) await selectMove(moveSelection(move));
    rememberSession();
  } catch (error) {
    if (request !== state.formRequest) return;
    viewMessage(error.message);
    message(error.message, true);
  }
}

async function useISO(result) {
  if (!result) return;
  if (state.iso?.game === 'fe3') fe3.leave();
  stopBattle();
  setPlaying(false);
  viewport?.clear();
  state.modelImages = [];
  workbench.reset();
  kgfExchange.reset();
  fe3Exchange.reset();
  kgfResources.sync(false);
  fe3Resources.sync(false);
  state.iso = result;
  const view = result.view || {};
  const fe3Game = result.game === 'fe3';
  $('kgf-workspace').hidden = fe3Game;
  $('fe3-workspace').hidden = !fe3Game;
  if (fe3Game) {
    ++state.formRequest;
    ++state.motionRequest;
    ++state.partRequest;
    ++textureRequest;
    state.model = state.motion = null;
    state.edits = result.edits;
    $('iso-name').textContent = result.name;
    $('iso-name').title = result.path;
    $('footer-source').textContent = 'PS2 · SLPS_254.41';
    renderEditControls();
    await fe3.load(view);
    return;
  }
  const index = (value, max) => Number.isInteger(value) && value >= 0 && value < max ? value : 0;
  kgfMixTabs.select(view.mixPane);
  state.role = index(view.role, result.roles.length);
  state.config = index(view.config, result.roles[state.role].cases.length);
  state.form = index(view.form, selectedConfig().forms.length);
  state.appearance = view.appearance || 'normal';
  if (Object.hasOwn(TABS, view.tab)) state.tab = view.tab;
  if (Number.isInteger(view.aiLevel) && view.aiLevel >= 0 && view.aiLevel <= 3) $('ai-level').value = view.aiLevel;
  state.scope = view.scope === 'clip' ? 'clip' : 'frame';
  choose($('scope').querySelectorAll('button'), state.scope);
  state.assetIndex = 0;
  state.paletteSource = '';
  state.edits = result.edits || {count: 0, assets: 0, unsaved: false};
  renderEditControls();
  $('iso-name').textContent = result.name;
  $('iso-name').title = result.path;
  $('role-search').value = '';
  $('move-search').value = '';
  renderRoles();
  $('roles').querySelector('.active')?.scrollIntoView({block: 'nearest'});
  await loadForm(view.action);
  if (view.part && view.part !== 'body' && [...$('model-part').options].some(option => option.value === view.part)) {
    await selectModelPart(view.part);
  }
  if (state.events.some(event => event.id === view.selectedEvent)) state.selectedEvent = view.selectedEvent;
  if (Number.isFinite(view.frame)) jump(view.frame);
  renderSection();
}

async function openISO(method) {
  if (state.writing) return;
  state.writing = true;
  renderEditControls();
  try {
    let result = await api(method, hasDraft());
    if (result?.confirmOpen !== undefined) {
      const dialog = $('switch-document');
      $('switch-filename').textContent = result.name;
      dialog.returnValue = 'cancel';
      const decision = new Promise(resolve => dialog.addEventListener('close',
        () => resolve(dialog.returnValue === 'discard'), {once: true}));
      dialog.showModal();
      result = await api('finish_open', result.confirmOpen, await decision);
    }
    await useISO(result);
  } catch (error) {
    message(error.message, true);
  } finally {
    state.writing = false;
    renderEditControls();
    rememberSession();
  }
}

async function writeWorkspace(method, args, success, refresh = true) {
  if (state.writing || !state.iso) return;
  if (refresh && hasDraft() && !await updateEvent()) return;
  if (refresh) {
    stopBattle();
    workbench.invalidateBattleOptions();
  }
  const action = state.move;
  const frame = state.frame;
  setPlaying(false);
  state.writing = true;
  renderEditControls();
  try {
    const result = await api(method, state.iso.id, ...args);
    if (!result) return;
    if (method === 'save_project' && !args[1]) lastSession = undefined;
    if (result.edits) state.edits = result.edits;
    else if (method !== 'export_music') state.edits = result;
    if (state.iso?.game === 'fe3') {
      if (refresh) await fe3.refresh();
    } else if (refresh) {
      await loadForm(action);
      if (state.motion) jump(frame);
    } else renderSection();
    if (method === 'history') {
      await (state.iso?.game === 'fe3' ? fe3Exchange : kgfExchange).refreshVoices();
    }
    message(result.name ? success + ' · ' + result.name : success);
    return result;
  } catch (error) {
    message(error.message, true);
  } finally {
    state.writing = false;
    renderEditControls();
    rememberSession();
  }
}

async function saveProject(portable = false) {
  if (hasDraft() && !await updateEvent()) return;
  await writeWorkspace('save_project', [currentView(), portable],
    portable ? '已导出改动包' : '已保存工程', false);
}

async function updateEvent(restore = false) {
  const event = state.events.find(item => item.id === state.selectedEvent);
  if (!event?.editable.length || state.writing) return false;
  if (!restore && !$('event-edit').reportValidity()) {
    message('请在事件详情中输入有效的整数。', true);
    return false;
  }
  const updates = restore ? null : Object.fromEntries(Array.from(
    $('edit-fields').querySelectorAll('[data-changed="true"]'), input => [input.name, input.valueAsNumber]));
  const action = state.move;
  const frame = state.frame;
  setPlaying(false);
  state.writing = true;
  renderEditControls();
  try {
    const motion = await api('edit_event', state.iso.id, state.role, state.config, state.form, action, event.id, updates);
    useMotion(motion);
    state.edits = motion.edits;
    if (motion.moves) {
      state.model.moves = motion.moves;
      actionTable();
    }
    if (viewport && !state.item) viewport.events = state.events;
    const edited = motion.events.find(item => item.id === event.id);
    renderEventDetail(edited);
    markers();
    jump(frame);
    message(restore ? '已还原此事件' : '预览已更新 · 触发帧 ' + edited.frame);
    return true;
  } catch (error) {
    message(error.message, true);
    return false;
  } finally {
    state.writing = false;
    renderEditControls();
    rememberSession();
  }
}

async function transferAsset(operation, format) {
  const asset = currentAsset();
  if (state.writing || !asset) return;
  if (operation !== 'export' && hasDraft() && !await updateEvent()) return;
  const action = state.move;
  const index = asset.kind === 'anm' ? action : asset.slot ?? null;
  const keepFrame = state.frame;
  const selection = [state.iso.id, state.role, state.config, state.form, asset.kind, index, state.palette];
  setPlaying(false);
  state.writing = true;
  renderEditControls();
  try {
    if (operation === 'export') {
      const result = await api('export_asset', ...selection, format || asset.kind);
      if (result) message('已导出 ' + result.name);
      return;
    }
    const result = operation === 'palette' ? await api('import_asset', ...selection, true) :
      await api(operation + '_asset', ...selection);
    if (!result) return;
    state.edits = result.edits;
    await loadForm(action);
    if (state.model && (action == null || state.motion)) {
      if (asset.kind !== 'anm') jump(keepFrame);
      const success = result.rig_refreshed ? '已绑定目标骨架' : '预览已更新';
      const adjusted = result.material_adjustments ? ' · 已调整 ' + result.material_adjustments + ' 个材质绑定' : '';
      const textures = result.textures?.length ? ' · 已绑定 ' + result.textures.length + ' 张 POM' : '';
      const warning = result.texture_warnings?.length ? ' · ' + result.texture_warnings.join('、') : '';
      message(operation === 'restore' ? '已还原资源并刷新预览' : success + ' · ' + result.name + adjusted + textures + warning);
    }
  } catch (error) {
    message(error.message, true);
  } finally {
    state.writing = false;
    renderEditControls();
    rememberSession();
  }
}

async function exportISO() {
  if (state.writing || !state.iso) return;
  if (!$('event-edit').hidden && hasDraft() && !await updateEvent()) return;
  state.writing = true;
  renderEditControls();
  message('正在另存 ISO');
  try {
    const result = await api('export_iso', state.iso.id);
    if (result) {
      state.edits = result.edits;
      message('已另存 ' + result.name + ' · ' + changeSummary(result.edits));
    } else message('已取消另存');
  } catch (error) {
    message(error.message, true);
  } finally {
    state.writing = false;
    renderEditControls();
  }
}

async function auditionEvent() {
  const event = state.events.find(item => item.id === state.selectedEvent);
  if (state.writing || event?.audition == null) return;
  const key = detailKey;
  $('audition-event').disabled = true;
  sound.pause();
  try {
    const uri = await api('read_sound', state.iso.id, state.role, state.config, event.audition);
    if (key !== detailKey) return;
    sound.src = uri;
    await sound.play();
  } catch (error) {
    if (key === detailKey) message(error.message, true);
  } finally {
    if (key === detailKey) $('audition-event').disabled = false;
  }
}

function bindEvents() {
  $('open').addEventListener('click', () => openISO('open_iso'));
  $('open-project').addEventListener('click', () => openISO('open_project'));
  $('save-project').addEventListener('click', () => saveProject());
  $('save-package').addEventListener('click', () => saveProject(true));
  $('undo').addEventListener('click', () => writeWorkspace('history', [false], '已撤销'));
  $('redo').addEventListener('click', () => writeWorkspace('history', [true], '已重做'));
  $('save-iso').addEventListener('click', exportISO);
  $('toggle-help').addEventListener('click', () => toggleHelp($('editor-help').hidden));
  $('toggle-nav').addEventListener('click', () => toggleNavigation());
  $('feature-search').addEventListener('input', () => {
    const filter = lower($('feature-search').value);
    for (const group of $('sections').querySelectorAll('.nav-group')) {
      const buttons = [...group.querySelectorAll('button[data-tab]')];
      for (const btn of buttons) btn.hidden = !lower(TABS[btn.dataset.tab]).includes(filter);
      group.hidden = buttons.every(btn => btn.hidden);
    }
  });
  $('feature-search').addEventListener('keydown', event => {
    if (event.key === 'Enter') $('sections').querySelector('button[data-tab]:not([hidden])')?.click();
  });
  $('asset-rows').addEventListener('click', event => {
    const row = event.target.closest('[data-asset]');
    if (state.writing || !row) return;
    state.assetIndex = Number(row.dataset.asset);
    renderAssets();
  });
  $('asset-palette').addEventListener('change', () => {
    state.palette = Number($('asset-palette').value);
    renderTexture();
  });
  $('binding-rows').addEventListener('click', event => {
    const row = event.target.closest('[data-form]');
    if (state.writing || !row || state.form === Number(row.dataset.form)) return;
    state.form = Number(row.dataset.form);
    state.assetIndex = 0;
    loadForm(state.move);
  });
  $('import-asset').addEventListener('click', () => transferAsset('import'));
  $('import-palette').addEventListener('click', () => transferAsset('palette'));
  $('import-fe3-model').addEventListener('click', () => {
    kgfMixTabs.select('exchange');
    switchTab('mix');
    kgfExchange.showModel();
  });
  $('mix-combos').addEventListener('click', () => {
    kgfMixTabs.select('exchange');
    kgfExchange.showAttacks(state.move);
    rememberSession();
  });
  $('export-asset').addEventListener('click', () => transferAsset('export'));
  $('export-texture-png').addEventListener('click', () => transferAsset('export', 'png'));
  $('restore-asset').addEventListener('click', () => transferAsset('restore'));
  $('event-edit').addEventListener('submit', event => {
    event.preventDefault();
    updateEvent();
  });
  $('event-edit').addEventListener('focusin', () => setPlaying(false));
  $('edit-fields').addEventListener('input', ({target}) => {
    target.dataset.changed = String(target.valueAsNumber !== Number(target.defaultValue));
    $('apply-event').disabled = !hasDraft();
  });
  $('restore-event').addEventListener('click', () => updateEvent(true));
  $('audition-event').addEventListener('click', auditionEvent);
  $('role-search').addEventListener('input', renderRoles);
  $('move-search').addEventListener('input', actionTable);
  $('move-combos').addEventListener('click', event => {
    const button = event.currentTarget;
    button.setAttribute('aria-pressed', String(button.getAttribute('aria-pressed') !== 'true'));
    actionTable();
    rememberSession();
  });
  $('roles').addEventListener('click', event => {
    const button = event.target.closest('button[data-role]');
    if (state.writing || !button || state.role === Number(button.dataset.role)) return;
    state.role = Number(button.dataset.role);
    state.config = 0;
    state.form = 0;
    state.assetIndex = 0;
    $('move-search').value = '';
    renderRoles();
    loadForm();
  });
  $('config').addEventListener('change', () => {
    if (state.writing) {
      $('config').value = state.config;
      return;
    }
    state.config = Number($('config').value);
    state.form = Math.min(state.form, selectedConfig().forms.length - 1);
    state.assetIndex = 0;
    loadForm(state.move);
  });
  $('shape').addEventListener('change', () => {
    if (state.writing) {
      $('shape').value = state.form;
      return;
    }
    state.form = Number($('shape').value);
    state.assetIndex = 0;
    loadForm(state.move);
  });
  $('model-appearance').addEventListener('change', async event => {
    if (state.writing) {
      event.target.value = state.appearance;
      return;
    }
    state.appearance = event.target.value;
    viewport?.setAppearance(state.appearance);
    actionTable();
    const selection = state.move;
    const moves = availableMoves();
    const move = matchingMove(moves, selection) || moves[0];
    if (move && !sameMove(moveSelection(move), selection)) await selectMove(moveSelection(move));
    else renderSection();
    rememberSession();
  });
  $('model-part').addEventListener('change', event => {
    if (state.writing) event.target.value = state.part;
    else selectModelPart(event.target.value);
  });
  for (const id of ['event-rows', 'timeline-markers']) {
    $(id).addEventListener('click', event => {
      const row = event.target.closest('[data-event]');
      if (!state.writing && row && !row.hidden) {
        selectEvent(Number(row.dataset.event), id === 'timeline-markers' || state.scope === 'clip');
      }
    });
  }
  for (const type of ['click', 'dblclick']) {
    $('moves').addEventListener(type, event => {
      const row = event.target.closest('[data-action]');
      if (state.writing || !row) return;
      if (event.target.closest('.damage-value')) switchTab('damage', 'clip');
      selectMove(rowSelection(row), type === 'dblclick');
    });
  }
  $('sections').addEventListener('click', event => {
    const button = event.target.closest('[data-tab]');
    if (state.writing || !button) return;
    switchTab(button.dataset.tab, button.dataset.tab === 'damage' ? 'clip' : undefined);
  });
  $('scope').addEventListener('click', event => {
    const button = event.target.closest('[data-scope]');
    if (state.writing || !button) return;
    if (state.battle) stopBattle();
    state.scope = button.dataset.scope;
    choose($('scope').querySelectorAll('button'), state.scope);
    renderEvents();
    rememberSession();
  });
  $('view-options').addEventListener('change', event => {
    const {option, colliderLayer} = event.target.dataset;
    const checked = event.target.checked;
    if (option === 'hit') $('collider-key').hidden = !checked;
    if (!viewport) return;
    if (colliderLayer) viewport.colliderLayers[colliderLayer] = checked;
    else if (option) viewport.options[option] = checked;
    if (option === 'root') viewport.setFrame(state.frame);
    else if (option || colliderLayer) viewport.draw();
  });
  $('reset-view').addEventListener('click', () => viewport?.resetView());
  $('shading').addEventListener('change', () => {
    if (!viewport) return;
    viewport.options.light = $('shading').value === 'lit';
    viewport.draw();
  });
  for (const control of DISPLAY_CONTROLS) control.addEventListener('change', () => rememberSession());
  $('ai-level').addEventListener('change', rememberSession);
  $('play').addEventListener('click', togglePlay);
  $('prev').addEventListener('click', () => jump(frameIndex(state.frame) - 1));
  $('next').addEventListener('click', () => jump(frameIndex(state.frame) + 1));
  $('scrub').addEventListener('input', () => jump(Number($('scrub').value), false));
  $('scrub').addEventListener('change', () => rememberSession());
  $('frame-number').addEventListener('change', () => jump(Number($('frame-number').value)));
  document.addEventListener('keydown', event => {
    const field = event.target.matches('input, textarea, select');
    if ((event.ctrlKey || event.metaKey) && !event.altKey) {
      const key = event.key.toLowerCase();
      if (key === 'k' && state.iso?.game !== 'fe3') {
        event.preventDefault();
        toggleNavigation(true);
        $('feature-search').focus();
      } else if (key === 's') {
        event.preventDefault();
        saveProject();
      } else if (!field && (key === 'z' || key === 'y')) {
        event.preventDefault();
        $(key === 'y' || event.shiftKey ? 'redo' : 'undo').click();
      }
      return;
    }
    if (field || event.target.matches('button, summary') || event.altKey) return;
    if (event.key === 'Enter' && event.target.closest('tr[data-action], tr[data-event], tr[data-asset], tr[data-form]')) {
      event.target.click();
    } else if (event.key === ' ' && state.motion) {
      event.preventDefault();
      togglePlay();
    } else if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
      if (state.motion) jump(frameIndex(state.frame) + (event.key === 'ArrowLeft' ? -1 : 1));
    }
  });
  workbench.bind();
  fe3.bind();
}

function toggleNavigation(open) {
  const body = document.querySelector('.editor-body');
  const visible = open ?? body.classList.contains('nav-collapsed');
  body.classList.toggle('nav-collapsed', !visible);
  $('toggle-nav').textContent = visible ? '收起导航' : '展开导航';
  $('toggle-nav').setAttribute('aria-expanded', String(visible));
  rememberSession();
}

function toggleHelp(show) {
  $('editor-help').hidden = !show;
  $('toggle-help').textContent = show ? '收起说明' : '显示说明';
  $('toggle-help').setAttribute('aria-expanded', String(show));
  rememberSession();
}

async function start() {
  bindEvents();
  state.writing = true;
  renderEditControls();
  renderSelectors();
  try {
    const result = await api('initialize');
    restoreDisplay(result.display);
    if (result.document) await useISO(result.document);
    else viewMessage('打开奥0、奥3的 ISO 或工程后开始预览');
    if (result.error) message(result.error, true);
  } catch (error) {
    viewMessage('请选择奥0或奥3的 ISO');
    message(error.message, true);
  } finally {
    state.writing = false;
    sessionReady = true;
    renderEditControls();
    rememberSession();
  }
}

if (window.pywebview?.api?.initialize) start();
else window.addEventListener('pywebviewready', start, {once: true});
