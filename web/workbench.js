import {makeAIPanel} from './ai_panel.js';

const extraPanels = {
  overview: 'overview', attributes: 'attributes', finishers: 'linked', throws: 'linked',
  states: 'linked', commands: 'commands', geometry: 'geometry', music: 'music', changes: 'changes', mix: 'mix', ai: 'ai',
};

const bandName = band => (band * 12.5) + '% – ' + ((band + 1) * 12.5) + '%';
const time = seconds => {
  const value = Math.max(0, Math.floor(seconds || 0));
  return String(Math.floor(value / 60)).padStart(2, '0') + ':' + String(value % 60).padStart(2, '0');
};

export function makeWorkbench({$, state, api, dom, properties, hex, message, setPlaying, selectMove,
  selectEvent, jump, enableHit, damageLabel, write, switchTab, availableMoves, playBattle, stopBattle}) {
  let generation = 0;
  let attributeBand = 7;
  let attributes;
  let linkedMove;
  let battleKey, recipientKey, lastEffect;
  let commandMode = 'branches';
  let commandCatalog;
  let branches = [];
  let selectedCommand;
  let geometry;
  let template;
  let keySelection;
  let keysGeneration = 0;
  let mix;
  let mixRole = null;
  let atlas;
  let musicLibrary;
  let selectedMusic;
  let loadedMusic;
  let musicRequest = 0;
  const audio = new Audio();

  const call = (method, ...args) => api(method, state.iso.id, ...args);
  const form = () => [state.role, state.config, state.form];
  const currentMove = () => state.motion?.action.id;
  const cast = () => [...form(), state.move];
  const roleName = () => state.iso.roles[state.role].name;
  const ai = makeAIPanel({$, state, dom, hex, rows, write, read: (...args) => call('read_ai', ...args)});

  function mixOptions(element, items, label, previous) {
    element.replaceChildren(...items.map(item => {
      const option = dom('option', label(item));
      option.value = item.id;
      option.disabled = Boolean(item.reason);
      return option;
    }));
    if (items.some(item => item.id === previous && !item.reason)) element.value = previous;
  }

  function renderMix() {
    if (!mix) return;
    const sticker = move => `${String(move.id).padStart(3, '0')} · ${move.name}`;
    const selected = Number($('mix-target').value);
    const source = Number($('mix-source').value);
    mixOptions($('mix-target'), mix.targets, sticker, selected);
    const target = mix.targets.find(row => row.id === Number($('mix-target').value));
    const bound = mix.bindings.find(item => item.state === target?.id);
    mixOptions($('mix-source'), mix.sources.filter(row => row.category === target?.category),
      item => sticker(item) + (item.reason ? ' · ' + item.reason : ''), source);
    $('mix-apply').disabled = !target || !$('mix-source').value ||
      $('mix-source').selectedOptions[0]?.disabled;
    $('mix-restore').disabled = !bound;
    $('mix-restore').hidden = !bound;
    $('mix-empty').hidden = mix.bindings.length > 0;
    $('mix-history-table').hidden = mix.bindings.length === 0;
    $('mix-preview').disabled = !target;
    $('mix-status').textContent = bound ? `当前配招来自${state.iso.roles[bound.role].name}，状态 ${bound.move}` :
      '选择攻击或技能。应用后可预览 ANM 和帧事件，另存镜像写入游戏。';
    rows('mix-rows', mix.bindings.map(item => ({id: item.state,
      cells: [item.state, state.iso.roles[item.role].name, item.move]})), 'action', id => {
      $('mix-target').value = id;
      renderMix();
    }, target?.id);
  }

  function rows(id, contents, key, onClick, selected) {
    $(id).replaceChildren(...contents.map(({id, cells}) => {
      const row = dom('tr', '', selected === id ? 'active' : '');
      row.dataset[key] = id;
      row.tabIndex = 0;
      row.append(...cells.map(value => dom('td', value ?? '—')));
      row.addEventListener('click', () => { if (!state.writing) onClick(id); });
      row.addEventListener('keydown', event => {
        if (event.key === 'Enter') {
          event.stopPropagation();
          row.click();
        }
      });
      return row;
    }));
  }

  function renderOverview() {
    const model = state.model;
    $('overview-title').textContent = roleName();
    properties($('overview-fields'), [['出场配置', state.iso.roles[state.role].cases.length],
      ['当前配置形态', model.forms.length], ['动作状态', availableMoves().filter(move => !move.followup && !move.extra).length], ['YUM 组', model.group],
      ['模型与语音库', [model.mdl, model.voice]], ['节点 / 三角面', [model.bones.length, model.triangles]]]);
    $('role-note').value = model.note;
  }

  function renderAttributes() {
    const decorate = value => hex(value, 4) + ' · ' + (value / attributes.q12).toFixed(3) + '×';
    rows('attribute-rows', attributes.rows.map(row => ({
      id: row.band, cells: [bandName(row.band), decorate(row.attack), decorate(row.defense), row.speed, row.travel],
    })), 'band', id => { attributeBand = id; renderAttributes(); }, attributeBand);
    const row = attributes.rows[attributeBand];
    $('attribute-form').hidden = false;
    $('attribute-band').textContent = '生命比例 ' + bandName(attributeBand);
    $('attack-value').value = hex(row.attack, 4);
    $('defense-value').value = hex(row.defense, 4);
    $('regen-value').value = row.speed;
    $('walk-value').value = row.travel;
    $('restore-attributes').disabled = !row.edited;
    updateAttributeAnalysis();
  }

  function updateAttributeAnalysis() {
    if (!attributes) return;
    const value = Number($('defense-value').value.trim());
    const ratio = value / attributes.q12;
    const row = attributes.rows[attributeBand];
    const life = Number.isFinite(ratio) && ratio > 0 ? (attributes.base_hp / ratio).toFixed(1) : '—';
    $('attribute-analysis').textContent = '受伤 ' + (Number.isFinite(ratio) ? ratio.toFixed(3) + '×' : '—') +
      ' · 原生起始生命 ' + attributes.base_hp + ' · 单段等效生命 ' + life +
      ' · 受伤倍率地址 ' + hex(row.address, 8);
  }

  function renderLinked() {
    const filter = $('linked-search').value.trim().toLowerCase();
    const list = availableMoves().filter(move => !move.followup && !move.extra && (state.tab === 'states' ||
      (state.tab === 'finishers' ? move.category === 5 : move.throw)) &&
      (!filter || [move.id, hex(move.id), move.clip, move.name].join(' ').toLowerCase().includes(filter)));
    const picked = list.find(move => move.id === currentMove());
    rows('linked-rows', list.map(move => ({id: move.id, cells: [
      (move.name || '动作 ' + move.id) + (move.clip_edited ? ' · 已改' : ''),
      move.id, move.clip, damageLabel(move.damage), move.last, move.va,
    ]})), 'action', id => selectMove(id, true), picked?.id);
    $('linked-empty').hidden = Boolean(list.length);
    $('linked-detail').hidden = !picked;
    linkedMove = picked;
    $('linked-battle').hidden = !picked || state.tab === 'states';
    if (!picked) return;
    const moves = availableMoves().filter(m => !m.followup && !m.extra && !m.encounter_variant);
    const stages = (picked.edit_phases || picked.phases || []).filter(id => moves.some(m => m.id === id));
    $('linked-phases').hidden = stages.length < 2;
    if (stages.length > 1) {
      const selector = $('linked-phase');
      const previous = selector.value;
      selector.replaceChildren(...stages.map(id => {
        const part = moves.find(m => m.id === id);
        const step = picked.phases?.indexOf(id) ?? -1;
        const label = step >= 0 ? '第 ' + (step + 1) + ' 段' : (id === picked.id ? '起手' : (part.name || '条件分段'));
        return new Option(label + ' · 状态 ' + id + ' · ANM ' + part.clip,id);
      }));
      if (previous && stages.includes(Number(previous))) selector.value = previous;
    }
    $('linked-title').textContent = picked.name || '状态 ' + picked.id;
    properties($('linked-fields'), [['状态 / 类别', [picked.id, picked.category]],
      ['状态标志', picked.flags], ['ANM 成员 / 片段', [picked.anm, picked.slot]],
      ['基础伤害', picked.damage.length ? picked.damage.map(hit => hit.frame + ' 帧 · ' + hit.value + ' HP').join(' / ') : '—'],
      ['待机输入', picked.input || '—'],
      ['进入回调', picked.callbacks[0] || '—'],
      ['对手条件', picked.opponent == null ? '通用' : state.iso.roles[picked.opponent].name]]);
    $('linked-clip-label').hidden = !picked.clip_editable;
    $('linked-clip').disabled = !picked.clip_editable;
    $('linked-clip').value = picked.clip;
    $('apply-linked').hidden = !picked.clip_editable;
    $('restore-linked').hidden = !picked.clip_editable;
    $('restore-linked').disabled = !picked.clip_edited;
    $('linked-name-label').hidden = $('linked-apply-name').hidden = state.tab !== 'finishers';
    $('linked-name').placeholder = picked.name || '必杀技名称';
    $('linked-name').value = state.motion?.action.alias || '';
    if (state.tab !== 'states') {
      const roleOptions = state.iso.roles.map((item, id) => ({id, name:item.name}));
      const opponent = Number($('linked-opponent').value || 4);
      const recipient = Number($('linked-recipient').value || (state.role === 0 ? 1 : 0));
      mixOptions($('linked-opponent'), roleOptions, item => item.name, opponent);
      mixOptions($('linked-recipient'), roleOptions.filter(item => item.id !== state.role), item => item.name, recipient);
      loadBattleOptions(picked);
      loadRecipient(picked);
    }
  }

  async function loadBattleOptions(move) {
    const key = [state.iso.id, state.role, state.config, move.id].join('/');
    if (battleKey === key) return;
    battleKey = key;
    $('linked-play-battle').disabled = true;
    try {
      const rows = await call('read_battle_cameras', state.role, state.config, move.id);
      if (battleKey !== key) return;
      mixOptions($('linked-camera'), [{id:-1,name:'原生镜头'}, {id:-2,name:'双方视角'}, ...rows], item => item.name, -1);
      $('linked-play-battle').disabled = false;
    } catch (error) {
      if (battleKey === key) { battleKey = null; message(error.message, true); }
    }
  }

  async function loadRecipient(move) {
    const recipient = Number($('linked-recipient').value);
    const key = [state.iso.id, state.role, move.id, recipient].join('/');
    if (recipientKey === key) return;
    recipientKey = key;
    const button = $('linked-send-move');
    button.disabled = true;
    try {
      const data = await call('read_move_sets', recipient, state.role);
      if (recipientKey !== key) return;
      const source = data.sources.find(item => item.id === move.id);
      const targets = data.targets.filter(item => item.category === source?.category);
      mixOptions($('linked-recipient-move'), targets, item => item.name, move.id);
      button.disabled = !source || Boolean(source.reason) || !targets.length;
      button.title = source?.reason ?? '';
    } catch (error) {
      if (recipientKey === key) { recipientKey = null; message(error.message, true); }
    }
  }

  function showBattle() {
    $('linked-stop-battle').disabled = false;
    const items = state.battle.events.map(event => {
      const button = dom('button', Math.floor(event.frame) + ' 帧 · ' + event.name);
      button.type = 'button';
      button.addEventListener('click', () => jump(event.frame));
      return button;
    });
    $('linked-scene-events').replaceChildren(...items);
    $('linked-scene').open = true;
    lastEffect = undefined;
  }

  function battleFrame(frame) {
    $('linked-stop-battle').disabled = !state.battle;
    if (!state.battle) {
      $('linked-scene-events').replaceChildren();
      $('linked-scene-current').textContent = '';
      $('linked-scene-textures').replaceChildren();
      lastEffect = undefined;
      return;
    }
    const current = state.battle.events.findLast(event => event.frame <= frame);
    $('linked-scene-current').textContent = current ? '帧 ' + Math.floor(frame) + ' · ' + current.name : '帧 ' + Math.floor(frame);
    const effect = state.battle.events.findLast(event => event.textures?.length && event.frame <= frame);
    if (effect === lastEffect) return;
    lastEffect = effect;
    $('linked-scene-textures').replaceChildren(...(effect?.textures ?? []).map(texture => {
      const figure = dom('figure');
      const image = dom('img');
      image.src = texture.uri;
      image.alt = 'POM ' + texture.id + ' · 色表 ' + texture.palette;
      figure.append(image, dom('figcaption', image.alt));
      return figure;
    }));
  }

  function renderCommands() {
    const native = commandMode === 'input';
    const transition = commandMode === 'branches';
    for (const btn of $('command-modes').querySelectorAll('button')) {
      const active = btn.dataset.commandMode === commandMode;
      btn.classList.toggle('active', active);
      btn.setAttribute('aria-pressed', String(active));
    }
    const headings = transition ? ['输入', '目标状态', '默认 ANM', '条件位', '优先级'] :
      native ? ['编号', '按键', '原生名称', '步骤'] : ['触发帧', '窗口长度', '命令编号', '目标 ANM'];
    const head = dom('tr', '');
    head.append(...headings.map(value => dom('th', value)));
    $('command-head').replaceChildren(head);
    const list = transition ? branches : native ? commandCatalog || [] : state.events.filter(event => event.op === 3);
    const moves = new Map(availableMoves().filter(move => !move.followup && !move.extra).map(move => [move.id, move]));
    rows('command-rows', list.map((item, index) => transition ? {id: index, cells: [
      item.input, item.target === -1 ? '保持' : item.target, moves.get(item.target)?.clip, hex(item.flags, 6), item.priority,
    ]} : ({id: item.id, cells: native ?
      [item.id, item.name, item.symbol, item.steps.length] :
      [item.frame, item.fields['窗口长度'], item.fields['命令编号'], item.fields['目标 ANM']]})),
    'command', id => {
      if (transition) {
        const branch = list[id];
        if (moves.has(branch.target)) selectMove(branch.target, true);
        else {
          $('command-detail').hidden = false;
          properties($('command-fields'), [['分支来源', hex(branch.va, 8)], ['目标', branch.target === -1 ? '保持当前状态' : branch.target]]);
        }
      } else if (native) {
        selectedCommand = id;
        renderCommands();
      } else selectEvent(id);
    }, native ? selectedCommand : undefined);
    $('command-empty').textContent = transition ? '当前状态没有专用输入分支' :
      native ? '正在读取原生命令表' : '当前动作没有连段窗口';
    $('command-empty').hidden = Boolean(list.length);
    const pick = native && list.find(item => item.id === selectedCommand);
    $('command-detail').hidden = !pick;
    if (pick) properties($('command-fields'), [['命令来源', 'YUM 组 4 · ' + hex(pick.offset, 4)],
      ...pick.steps.map((row, i) => ['原生步骤 ' + (i + 1), row.map(n => hex(n, 4)).join(' ')])]);
    $('reset-command-state').hidden = !transition;
  }

  function renderGeometry() {
    const list = geometry.items;
    rows('geometry-rows', list.map(item => ({id: item.template, cells: [item.template,
      (item.type === 1 ? '球' : '胶囊') + (item.edited ? ' · 已改' : ''), Number(item.radius.toFixed(4)), hex(item.offset, 6)]})),
    'template', id => { template = id; renderGeometry(); }, template);
    const record = list.find(item => item.template === template);
    $('geometry-detail').hidden = !record;
    if (!record) return;
    $('geometry-title').textContent = 'BOM ' + geometry.member + ' · 模板 ' + record.template + ' · 共用几何';
    const fields = [...record.center, ...(record.to || []), record.radius];
    const names = ['中心 X', '中心 Y', '中心 Z', ...(record.to ? ['终点 X', '终点 Y', '终点 Z'] : []), '半径'];
    $('geometry-values').replaceChildren(...fields.map((value, i) => {
      const label = dom('label', names[i]);
      const input = dom('input', '');
      input.type = 'number';
      input.step = 'any';
      input.required = true;
      input.value = value;
      label.append(input);
      return label;
    }));
    $('restore-geometry').disabled = !record.edited;
    $('preview-geometry').disabled = !templateEvent();
  }

  function templateEvent() {
    return state.events.find(event => event.shape?.template === template && event.shape.radius &&
      event.frame >= 0 && event.frame <= state.motion?.animation.last);
  }

  function renderChanges() {
    const edits = state.edits;
    properties($('change-fields'), [['镜像', state.iso.name], ['帧事件修改', edits.count || 0],
      ['ANM / MDL / POM 替换', edits.assets || 0], ['原生角色及状态字段', edits.native || 0],
      ['判定模板', edits.templates || 0], ['配招', edits.moves || 0], ['AI 参数', edits.ai || 0],
      ['角色备注及动作别名', edits.notes || 0],
      ['工作状态', edits.unsaved ? '有修改待保存' : '无待保存修改']]);
  }

  function renderMusic() {
    const defaultTrack = musicLibrary.default;
    if (!musicLibrary.library.some(track => track.member === selectedMusic)) selectedMusic = defaultTrack.member;
    rows('music-default', [{id: defaultTrack.member,
      cells: ['角色开战曲', defaultTrack.id, defaultTrack.member, time(defaultTrack.seconds)]}],
    'member', id => { selectedMusic = id; renderMusic(); }, selectedMusic);
    $('music-library').replaceChildren(...musicLibrary.library.map(track => new Option(
      (track.id == null ? 'ATRAC' : 'BGM ' + String(track.id).padStart(2, '0')) + ' · YUM ' + track.member +
      ' · ' + time(track.seconds), track.member)));
    $('music-library').value = selectedMusic;
    $('music-title').textContent = roleName() + ' · YUM ' + selectedMusic;
    renderMusicTime();
  }

  function renderMusicTime() {
    const seek = $('music-scrub');
    const duration = Number.isFinite(audio.duration) ? audio.duration : 0;
    seek.max = duration;
    seek.value = audio.currentTime || 0;
    seek.disabled = !duration;
    $('music-time').textContent = time(audio.currentTime) + ' / ' + time(duration);
    $('pause-music').disabled = !loadedMusic;
  }

  async function playMusic() {
    if (state.writing || selectedMusic == null) return;
    const request = ++musicRequest;
    const member = selectedMusic;
    const token = state.iso.id + '/' + member;
    try {
      if (loadedMusic !== token) {
        message('正在解码 BGM · YUM ' + member);
        const uri = await call('audition_music', member);
        if (request !== musicRequest || member !== selectedMusic) return;
        audio.src = uri;
        loadedMusic = token;
      }
      audio.loop = $('music-loop').checked;
      audio.volume = Number($('music-volume').value);
      await audio.play();
      message('正在试听 · YUM ' + member);
      renderMusicTime();
    } catch (error) {
      if (request === musicRequest) message(error.message, true);
    }
  }

  function stopMusic() {
    musicRequest++;
    audio.pause();
    if (Number.isFinite(audio.duration)) audio.currentTime = 0;
    renderMusicTime();
  }

  function renderKeyRows(values, selected = null) {
    $('key-rows').replaceChildren(...values.map((row, index) => {
      const tr = dom('tr', '', selected === index ? 'active' : '');
      tr.dataset.key = index;
      tr.addEventListener('click', () => {
        if (state.writing) return;
        keySelection = index;
        for (const other of $('key-rows').children) other.classList.toggle('active', other === tr);
        $('delete-key').disabled = $('key-rows').children.length < 2;
        jump(Number(tr.firstElementChild.firstElementChild.value));
      });
      tr.append(...row.map((value, i) => {
        const td = dom('td', '');
        const input = dom('input', '');
        input.type = 'number';
        input.step = i === 0 && Number($('key-channel').value) === 0 ? 1 : 'any';
        input.value = value;
        input.setAttribute('aria-label', ['帧', 'X', 'Y', 'Z'][i] + ' · ' + (index + 1));
        td.append(input);
        return td;
      }));
      return tr;
    }));
    $('delete-key').disabled = selected === null || values.length < 2;
    $('apply-keys').disabled = values.length === 0;
    $('add-key').disabled = values.length === 0;
  }

  function keyValues() {
    return Array.from($('key-rows').children, row => Array.from(row.querySelectorAll('input'), input => input.valueAsNumber));
  }

  async function fetchKeys() {
    const request = ++keysGeneration;
    if (state.tab !== 'animation' || !state.motion) return;
    const selected = [Number($('key-track').value), Number($('key-channel').value)];
    keySelection = null;
    renderKeyRows([]);
    try {
      const values = await call('read_keys', ...cast(), ...selected);
      if (request === keysGeneration && state.tab === 'animation') renderKeyRows(values);
    } catch (error) {
      if (request === keysGeneration) message(error.message, true);
    }
  }

  function renderAnimation() {
    const motion = state.motion;
    $('keys-panel').hidden = !motion;
    if (!motion) return;
    $('action-alias').value = motion.action.alias;
    $('action-alias').title = motion.action.followup ? '此别名用于连招的起手状态' : '';
    $('action-alias').disabled = motion.action.extra;
    $('save-alias').disabled = motion.action.extra;
    $('animation-frames').value = motion.animation.last + 1;
    $('animation-rate').value = motion.animation.rate;
    $('animation-horizontal').value = 1;
    const previous = $('key-track').value;
    const tracks = Object.keys(motion.animation.channels);
    $('key-track').replaceChildren(...tracks.map(track => new Option(track + ' → 节点 ' +
      (state.model.rig[track] ?? '—'), track)));
    if (tracks.includes(previous)) $('key-track').value = previous;
    fetchKeys();
  }

  async function render() {
    const ticket = ++generation;
    const panel = extraPanels[state.tab];
    for (const name of new Set(Object.values(extraPanels))) {
      $(name + '-panel').hidden = !state.model || name !== panel;
    }
    $('keys-panel').hidden = state.tab !== 'animation' || !state.motion;
    if (!state.model) return;
    try {
      if (state.tab === 'animation') renderAnimation();
      if (panel === 'overview') renderOverview();
      if (panel === 'linked') renderLinked();
      if (panel === 'changes') renderChanges();
      if (panel === 'ai') await ai.render();
      if (panel === 'mix') {
        const current = state.role;
        const sameRole = mixRole === current;
        const prior = sameRole ? Number($('mix-donor').value) : (current === 0 ? 1 : 0);
        mixOptions($('mix-donor'), state.iso.roles.map((role, id) => ({id, name: role.name})),
          role => role.name, prior);
        $('mix-apply').disabled = true;
        const data = await call('read_move_sets', current, Number($('mix-donor').value));
        if (ticket !== generation) return;
        mix = data;
        mixRole = current;
        if (!sameRole && data.targets.some(item => item.id === currentMove())) {
          mixOptions($('mix-target'), data.targets, item => item.name, currentMove());
        }
        renderMix();
      }
      if (panel === 'attributes') {
        $('energy-form').hidden = true;
        $('attribute-form').hidden = true;
        $('attribute-rows').replaceChildren();
        const data = await call('read_attributes', state.role);
        if (ticket !== generation) return;
        attributes = data;
        data.energy.forEach((value,i) => {
          const rounded = Number(value.toPrecision(7));
          $('energy-' + i).value = Math.fround(rounded) === value ? rounded : value;
        });
        $('energy-reset').disabled = !data.energyEdited;
        $('energy-form').hidden = false;
        renderAttributes();
      }
      if (panel === 'geometry') {
        $('geometry-detail').hidden = true;
        $('geometry-rows').replaceChildren();
        const data = await call('read_templates', ...form());
        if (ticket !== generation) return;
        if (geometry?.member !== data.member) template = null;
        geometry = data;
        renderGeometry();
      }
      if (panel === 'commands') {
        if (commandMode === 'branches') {
          const data = currentMove() >= 0 ? await call('read_transitions', state.role, currentMove()) : [];
          if (ticket !== generation) return;
          branches = data;
        } else if (commandMode === 'input' && !commandCatalog) {
          const data = await call('read_commands');
          if (ticket !== generation) return;
          commandCatalog = data;
        }
        renderCommands();
      }
      if (panel === 'music') {
        const key = state.iso.id + '/' + state.role;
        if (atlas !== key) {
          $('music-default').replaceChildren();
          $('music-library').replaceChildren();
          $('music-title').textContent = '正在读取曲库';
          stopMusic();
          selectedMusic = null;
          const data = await call('read_music', state.role);
          if (ticket !== generation) return;
          atlas = key;
          musicLibrary = data;
        }
        renderMusic();
      }
    } catch (error) {
      if (ticket === generation) message(error.message, true);
    }
  }

  return {
    manages: tab => Boolean(extraPanels[tab]),
    render,
    showBattle,
    battleFrame,
    invalidateBattleOptions() {
      battleKey = recipientKey = null;
    },
    reset() {
      ai.reset();
      mix = null;
      mixRole = null;
      battleKey = recipientKey = null;
      commandCatalog = null;
      atlas = null;
      loadedMusic = null;
      selectedMusic = null;
      stopMusic();
      audio.removeAttribute('src');
    },
    bind() {
      ai.bind(error => message(error.message, true));
      $('mix-donor').addEventListener('change', () => render());
      $('mix-target').addEventListener('change', renderMix);
      $('mix-form').addEventListener('submit', async event => {
        event.preventDefault();
        if ($('mix-apply').disabled || state.writing) return;
        const target = Number($('mix-target').value);
        await write('edit_move_set', [state.role, target, Number($('mix-donor').value), Number($('mix-source').value)],
          '配招已应用到工程');
      });
      $('mix-restore').addEventListener('click', () => {
        if (!$('mix-restore').disabled) write('edit_move_set', [state.role, Number($('mix-target').value)], '已恢复原动作');
      });
      $('mix-preview').addEventListener('click', () => {
        if (!state.writing && $('mix-target').value) selectMove(Number($('mix-target').value), true);
      });
      $('save-note').addEventListener('click', () => write('edit_text', [state.role, null, $('role-note').value], '已保存角色备注'));
      $('attribute-form').addEventListener('submit', event => {
        event.preventDefault();
        const parse = id => {
          const value = $(id).value.trim();
          return /^(?:0x[0-9a-f]+|\d+)$/i.test(value) ? Number(value) : NaN;
        };
        const attack = parse('attack-value'), defense = parse('defense-value');
        if (!Number.isInteger(attack) || !Number.isInteger(defense)) {
          message('Q12 原值请输入整数或以 0x 开头的十六进制数。', true);
          return;
        }
        write('edit_attributes', [state.role, attributeBand, {attack, defense, speed: $('regen-value').valueAsNumber, travel: $('walk-value').valueAsNumber}], '已应用角色属性');
      });
      $('defense-value').addEventListener('input', updateAttributeAnalysis);
      $('energy-form').onsubmit = event => {
        event.preventDefault();
        write('edit_energy', [state.role, [0,1,2,3].map(i => $('energy-' + i).valueAsNumber)], '已应用技能耗能');
      };
      $('energy-reset').onclick = () => write('edit_energy', [state.role,null], '已恢复镜像耗能');
      $('restore-attributes').addEventListener('click', () => write('edit_attributes', [state.role, attributeBand, null], '已重置生命分段'));
      $('linked-search').addEventListener('input', () => { if (state.model) renderLinked(); });
      $('edit-linked-phase').onclick = async () => {
        await selectMove(Number($('linked-phase').value), false);
        switchTab('all','clip');
      };
      $('linked-play-battle').addEventListener('click', () => {
        if (linkedMove) playBattle(linkedMove.id, Number($('linked-opponent').value), Number($('linked-camera').value))
          .catch(error => message(error.message, true));
      });
      $('linked-stop-battle').addEventListener('click', stopBattle);
      $('linked-recipient').addEventListener('change', () => { if (linkedMove) loadRecipient(linkedMove); });
      $('linked-send-move').addEventListener('click', () => {
        if (linkedMove) write('edit_move_set', [Number($('linked-recipient').value),
          Number($('linked-recipient-move').value), state.role, linkedMove.id], '已配置到目标角色');
      });
      $('linked-apply-name').addEventListener('click', () => {
        if (linkedMove) write('edit_text', [state.role, linkedMove.id, $('linked-name').value], '必杀技名称已写入工程');
      });
      $('linked-detail').addEventListener('submit', event => {
        event.preventDefault();
        if (linkedMove?.clip_editable) write('edit_action_clip', [state.role, linkedMove.id, $('linked-clip').valueAsNumber], '已更新默认 ANM 片段');
      });
      $('restore-linked').addEventListener('click', () => {
        if (linkedMove) write('edit_action_clip', [state.role, linkedMove.id, null], '已重置默认 ANM 片段');
      });
      $('jump-linked-event').addEventListener('click', () => switchTab('all', 'clip'));
      $('command-modes').addEventListener('click', event => {
        const button = event.target.closest('[data-command-mode]');
        if (!button || state.writing) return;
        commandMode = button.dataset.commandMode;
        render();
      });
      $('reset-command-state').addEventListener('click', () => { if (!state.writing) selectMove(0); });
      $('geometry-detail').addEventListener('submit', event => {
        event.preventDefault();
        const values = Array.from($('geometry-values').querySelectorAll('input'), input => input.valueAsNumber);
        write('edit_template', [...form(), template, values], '已应用判定模板');
      });
      $('restore-geometry').addEventListener('click', () => write('edit_template', [...form(), template, null], '已重置判定模板'));
      $('preview-geometry').addEventListener('click', () => {
        const event = templateEvent();
        if (event) {
          enableHit(event.shape.layer);
          jump(event.frame);
        }
      });
      $('save-alias').addEventListener('click', () => write('edit_text', [state.role, currentMove(), $('action-alias').value], '已保存动作别名'));
      $('animation-timing').addEventListener('submit', event => {
        event.preventDefault();
        write('edit_timing', [...cast(), $('animation-frames').valueAsNumber,
          $('animation-rate').valueAsNumber, $('animation-horizontal').valueAsNumber], '已更新 ANM 播放参数');
      });
      $('key-track').addEventListener('change', fetchKeys);
      $('key-channel').addEventListener('change', fetchKeys);
      $('add-key').addEventListener('click', () => {
        if (state.writing) return;
        setPlaying(false);
        const values = keyValues();
        const sample = values[keySelection ?? 0];
        let frame = Math.round(state.frame);
        while (values.some(row => row[0] === frame)) frame++;
        const row = [frame, ...sample.slice(1)];
        values.push(row);
        values.sort((a, b) => a[0] - b[0]);
        keySelection = values.indexOf(row);
        renderKeyRows(values, keySelection);
      });
      $('delete-key').addEventListener('click', () => {
        if (keySelection == null || state.writing) return;
        const values = keyValues();
        values.splice(keySelection, 1);
        keySelection = null;
        renderKeyRows(values);
      });
      $('apply-keys').addEventListener('click', () => write('edit_keys',
        [...cast(), Number($('key-track').value), Number($('key-channel').value), keyValues()], '已更新当前轨道关键帧'));
      $('music-library').addEventListener('change', () => {
        selectedMusic = Number($('music-library').value);
        stopMusic();
        renderMusic();
      });
      $('play-music').addEventListener('click', playMusic);
      $('pause-music').addEventListener('click', () => {
        if (audio.paused) playMusic();
        else audio.pause();
      });
      $('stop-music').addEventListener('click', stopMusic);
      $('music-loop').addEventListener('change', event => { audio.loop = event.target.checked; });
      $('music-volume').addEventListener('input', event => { audio.volume = Number(event.target.value); });
      $('music-scrub').addEventListener('input', event => { audio.currentTime = Number(event.target.value); });
      $('export-music').addEventListener('click', () => {
        if (selectedMusic != null) write('export_music', [selectedMusic], '已导出 WAV', false);
      });
      for (const name of ['timeupdate', 'loadedmetadata', 'durationchange']) audio.addEventListener(name, renderMusicTime);
    },
  };
}
