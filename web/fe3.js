import {loadImages, Viewport} from './viewport.js';
import {frameIndex, frameToSecond, movieFrameAt, secondToFrame, speedMap} from './player.js';
import {setHidden, updateEventList} from './event_list.js';
import {SEQUENCE, inputQuery as searchable} from './input_notation.js';
import {makeMixTabs} from './exchange.js';

const PAGE = 96;
const NAVIGATION = [
  ['角色与模型', 'resource-nav', '⊞', [
    ['overview', '角色总览'], ['hp', '角色属性'], ['appearance', '外观与贴图'], ['bindings', '形态与资源绑定'],
  ]],
  ['动作与战斗', '', '▸', [
    ['animation', '动画与关键帧'], ['commands', '按键与连段解析'], ['hit', '招式与判定'],
    ['other-skills', '防御与辅助'], ['skills', '必杀技配置'], ['mix', '配招'], ['throws', '抓取与投技'], ['states', '状态转移表'],
  ]],
  ['镜头与声音', 'sound-nav', '♪', [
    ['events', '镜头·手型·声音'], ['audio', '音效事件表'], ['effects', '特效事件表'], ['music', '角色 BGM'],
  ]],
  ['镜像资源', 'system-nav', '⊞', [['shared-resources', '通用资源']]],
  ['系统与校验', 'system-nav', '✓', [['report', '修改校验与报告'], ['assets', '全部资源']]],
];
const TABS = Object.fromEntries(NAVIGATION.flatMap(([group, , , tabs]) => tabs.map(([id, title]) => [id, {group, title}])));
const EVENT_TABS = new Set(['hit', 'events', 'audio', 'effects']);
const HIT_EVENTS = new Set([0,1,2,3,4,6,8,9,10,11,12,13,15,24,25,26,28]);
const EMPTY_EVENTS = [];
const ASSET_TABS = new Set(['appearance', 'animation', 'assets']);
const PANEL_IDS = ['overview','bindings','resource-panel','asset-detail','frame-events','command-panel',
  'skills','support','charge','mix','throws','states','hp','batch','jump','music','report'];
const eventKind = ({op}) => [16,17].includes(op) ? 'audio' : HIT_EVENTS.has(op) ? 'hit' :
  op === 5 ? 'combo' : [18,19,20,21,22,23,27].includes(op) ? 'skill' : '';
const EVENT_KINDS = {hit:'hit', audio:'audio', effects:'skill'};
const matchesEventTab = (item, tab) => !EVENT_KINDS[tab] || eventKind(item) === EVENT_KINDS[tab];
const pad = (id, width = 3) => String(id).padStart(width, '0');
const same = (a, b) => a && b && a.id === b.id && a.member === b.member;
const partKey = part => [part.id, part.member];
const labelTime = seconds => pad(Math.floor(seconds / 60), 2) + ':' + pad(Math.floor(seconds) % 60, 2);

export function makeFE3({$, state, dom, properties, hex, api, write, message, remember, updateExchange, updateResources}) {
  const el = id => $('fe3-' + id);
  const active = () => state.iso?.game === 'fe3';
  const call = (method, ...args) => api('fe3_' + method, state.iso.id, ...args);
  const read = field => Number(el(field).value);
  const checked = field => el(field).checked;
  const run = operation => async (...args) => {
    try { await operation(...args); }
    catch (error) { if (active()) message(error.message, true); }
  };
  const apply = (method, args, status, refresh = true) => write('fe3_' + method, args, status, refresh);
  const keyText = key => key ? hex(key[0], 4) + (key[1] >= 0 ? '/' + key[1] : '') : '—';
  const navButtons = () => el('sections').querySelectorAll('[data-fe3-tab]');
  const viewFields = () => el('view-options').querySelectorAll('[data-view]');
  let role, shape, tab, profile, model, motion, move, viewport, rows = [], part, detail;
  let frame = 0, playing = false, tickAt, playhead, ticker, rates, eventId, rawEvents, palette, clip, script, currentOnly;
  let listedSource, listedTab, eventRows = [], shownEvent;
  let page, combos, band, hp, skillId, commandState, musicId;
  let track = 0, keyRow = 0, keys = [];
  let aliases = {}, rules = [], inputTab = 'states', choice, comboId = -1;
  let throwTab = 'stages', throwState = -1, opponent = 4, throwRows = [], compareRequest = 0;
  let scene, donorRole = 0, donorSkill, throwCurve = -1, donorRequest = 0;
  let mixId = -1;
  let supportGroup = 'guard', supportId, supportValue;
  let supportDonor = -1, supportDonorMove = -1, supportSources = [], supportRequest = 0;
  const sceneMedia = new Map();
  let movieId,movieCue,presentedFrame,seekFrame,videoCallback,filmWaiting = false;
  let bindingSlot;
  let fighterRequest = 0, motionRequest = 0, panelRequest = 0, musicRequest = 0, keyRequest = 0;
  const mixTabs = makeMixTabs(el('mix'), run(async name => {
    if (name !== 'local') ++compareRequest;
    if (name !== 'local' && scene && move) await selectMove(move.id);
    else await drawPanel();
    remember();
  }));
  const form = () => profile?.forms.find(item => item.id === shape);
  const find = key => key && rows.find(row => row.id === key[0] && row.member === key[1]);
  const fighter = () => state.iso.roles[role];
  const roleOptions = () => state.iso.roles.map(item => [item.id, pad(item.id, 2) + ' ' + item.name + ' · ' + item.code]);
  const aliasKey = item => [role, item?.id].join('/');
  const anmName = item => aliases[aliasKey(item)] || item.name;

  function row(values, action, selected = false) {
    const tr = dom('tr', '', selected ? 'active' : '');
    tr.append(...values.map(value => dom('td', value)));
    if (action) {
      tr.tabIndex = 0;
      tr.onclick = run(action);
      tr.onkeydown = event => { if (event.key === 'Enter') tr.click(); };
    }
    return tr;
  }

  function options(field, choices, value) {
    const select = el(field);
    select.replaceChildren(...choices.map(([id, label]) => new Option(label, id)));
    if (choices.some(([id]) => id === value)) select.value = value;
    return Number(select.value);
  }

  function context() {
    const selected = fighter();
    el('context').textContent = [pad(role, 2) + ' ' + selected.name + ' · ' + selected.code,
      form()?.name, move && 'ANM ' + pad(move.id)].filter(Boolean).join(' · ');
    el('bind-source').textContent = form() ? 'FUM ' + keyText([form().model[0], -1]) : '';
  }

  function roleList() {
    const query = el('role-search').value.trim().toLowerCase();
    el('roles').replaceChildren(...state.iso.roles.filter(item =>
      [item.id,item.name,item.code].join(' ').toLowerCase().includes(query)).map(item => {
      const button = dom('button', '', item.id === role ? 'active' : '');
      button.type = 'button';
      button.setAttribute('aria-pressed', String(item.id === role));
      button.append(dom('span', pad(item.id, 2), 'role-id'), dom('span', item.name), dom('span', item.code, 'role-code'));
      button.onclick = run(async () => {
        if (state.writing || role === item.id) return;
        role = item.id;
        shape = band = commandState = 0;
        eventId = skillId = mixId = -1;
        supportId = supportValue = undefined;
        supportDonor = supportDonorMove = -1;
        part = null;
        el('move-search').value = '';
        await loadFighter(0, 0);
        remember();
      });
      return button;
    }));
  }

  function moveList() {
    const query = searchable(el('move-search').value);
    const list = profile?.moves.filter(item => (!combos || item.combo) &&
      searchable([pad(item.id),anmName(item),...item.states,...item.commands,...item.keys].join(' ')).includes(query)) || [];
    el('combos').setAttribute('aria-pressed', String(combos));
    el('moves').replaceChildren(...list.map(item => {
      const damage = item.damage;
      const tr = row([pad(item.id),anmName(item),item.frames,damage.length > 1 ? damage[0] + '–' + damage.at(-1) : damage[0] ?? '—',
        item.events], () => selectMove(item.id), item.id === move?.id);
      tr.dataset.anm = item.id;
      tr.cells[1].classList.add('move-name');
      tr.cells[3].classList.add('damage-value');
      tr.title = '状态 ' + (item.states.join('、') || '—') + ' · 输入 ' + (item.keys.join('、') || '—');
      tr.ondblclick = run(() => selectMove(item.id, true));
      return tr;
    }));
  }

  function setPlaying(value) {
    playing = Boolean(value && motion);
    el('play').textContent = playing ? 'Ⅱ 暂停' : '▶ 播放';
    cancelAnimationFrame(ticker);
    if (playing) { tickAt = null; playhead = toTime(frame); ticker = requestAnimationFrame(tick); }
    if (motion) paintFrame();
  }

  const clock = () => scene ? {first:0, last:Math.ceil(scene.last)} : motion.animation;
  const useRates = () => !scene && checked('native-rate');
  const toTime = value => useRates() ? frameToSecond(rates, value) : value / 60;
  const toFrame = value => useRates() ? secondToFrame(rates, value) : value * 60;

  function tick(now) {
    if (!active() || !playing || !motion) { setPlaying(false); return; }
    if (filmWaiting) {
      tickAt = now;
      paintFrame();
      ticker = requestAnimationFrame(tick);
      return;
    }
    const {first, last} = clock();
    const seconds = tickAt ? Math.min((now - tickAt) / 1000, .10) : 0;
    playhead += seconds * read('speed');
    tickAt = now;
    const end = toTime(last + 1);
    if (playhead >= end) {
      if (checked('loop') && last > first) {
        const start = toTime(first);
        playhead = start + (playhead - start) % (end - start);
        pose(toFrame(playhead));
      }
      else { pose(last); setPlaying(false); remember(); }
    } else pose(toFrame(playhead));
    if (playing) ticker = requestAnimationFrame(tick);
  }

  function pose(value) {
    if (!motion || !Number.isFinite(value)) return;
    const old = frameIndex(frame);
    const {first,last} = clock();
    frame = Math.min(last, Math.max(first, value));
    const tick = frameIndex(frame);
    paintFrame();
    el('scrub').value = frame;
    el('frame').value = tick;
    el('editor-frame').textContent = pad(tick);
    el('view-stats').textContent = (model ? model.bones.length + ' 节点 · ' +
      model.triangles.toLocaleString() + ' 三角面 · ' : '') + '帧 ' + frame.toFixed(1);
    if (scene && old !== tick) sceneEvents();
    if (!scene && old !== tick && EVENT_TABS.has(tab)) eventTable();
  }

  function seek(value) { setPlaying(false); pose(value); remember(); }

  function togglePlay() {
    if (!motion) return;
    if (!playing && frame >= clock().last) pose(clock().first);
    setPlaying(!playing);
  }

  function timeline() {
    for (const id of ['play','step','back','scrub','frame']) el(id).disabled = !motion;
    el('markers').replaceChildren();
    if (!motion) {
      el('frame').value = '';
      el('editor-frame').textContent = el('last-frame').textContent = '—';
      return;
    }
    const {first,last} = clock();
    for (const id of ['scrub','frame']) {
      el(id).min = first;
      el(id).max = last;
    }
    el('last-frame').textContent = last;
    const marks = new Map();
    for (const event of scene?.events ?? motion.events) {
      if (first <= event.frame && event.frame <= last && (!marks.has(event.frame) || eventKind(event) === 'hit')) marks.set(event.frame, event);
    }
    for (const [time,event] of marks) {
      const button = dom('button');
      button.type = 'button';
      button.dataset.kind = eventKind(event);
      button.style.left = ((time-first)/Math.max(1,last-first)*100) + '%';
      button.title = time + ' 帧 · ' + event.name;
      button.setAttribute('aria-label', button.title);
      button.onclick = run(async () => {
        if (state.writing) return;
        seek(time);
        if (!scene) {
          const matching = EVENT_TABS.has(tab) ? motion.events.filter(item =>
            item.frame === time && matchesEventTab(item, tab)) : [];
          const record = matching.find(item => item.op === 0 || item.op === 24) || matching[0] || event;
          eventId = record.id;
          await setTab(matching.length ? tab : 'events');
        }
      });
      el('markers').append(button);
    }
  }

  async function selectMove(id, start = false, at, keepEvent = false) {
    const selected = profile?.moves.find(item => item.id === id);
    if (!selected || (motion && move?.id === id && !start && at === undefined && !scene)) return;
    scene = null;
    el('scene-media').hidden = true;
    stopMovie();
    ++compareRequest;
    setPlaying(false);
    const request = ++motionRequest, fighter = fighterRequest;
    move = selected;
    motion = null;
    if (!keepEvent) eventId = -1;
    context();
    timeline();
    if (EVENT_TABS.has(tab)) eventTable();
    let payload;
    try { payload = await call('animation', role, shape, id); }
    catch (error) {
      if (!active() || request !== motionRequest || fighter !== fighterRequest) return;
      move = null;
      context();
      if (EVENT_TABS.has(tab)) eventTable();
      throw error;
    }
    if (!active() || request !== motionRequest || fighter !== fighterRequest) return;
    motion = payload;
    rates = speedMap(motion.animation, motion.events);
    commandState = selected.states.find(value => profile.commands[value]?.length) ?? commandState;
    viewport?.setMotion(motion.animation, motion.events);
    if (viewport) el('view-message').textContent = '';
    timeline();
    pose(at ?? motion.animation.first);
    for (const tr of el('moves').rows) tr.classList.toggle('active', Number(tr.dataset.anm) === id);
    el('moves').querySelector('.active')?.scrollIntoView({block:'nearest'});
    context();
    await drawPanel();
    if (start && request === motionRequest && fighter === fighterRequest) setPlaying(true);
    remember();
  }

  async function loadFighter(id, at, keepEvent = false) {
    const request = ++fighterRequest;
    ++compareRequest;
    scene = null;
    el('scene-media').hidden = true;
    stopMovie();
    setPlaying(false);
    profile = move = motion = model = detail = null;
    viewport?.clear();
    timeline();
    if (EVENT_TABS.has(tab)) eventTable();
    roleList();
    moveList();
    el('view-message').textContent = '正在读取 ' + fighter().name + ' 的模型和动作';
    el('view-stats').textContent = '';
    const [source, mesh] = await Promise.allSettled([call('profile', role, shape), call('model', role, shape)]);
    if (!active() || request !== fighterRequest) return;
    if (source.status === 'rejected') {
      el('view-message').textContent = source.reason.message;
      throw source.reason;
    }
    profile = source.value;
    options('form', profile.forms.map(item => [item.id, (item.accessory ? '部件 ' + item.id + ' · ' : '') + item.name]), shape);
    context();
    moveList();
    if (mesh.status === 'fulfilled') {
      model = mesh.value;
      try {
        viewport ??= new Viewport(el('viewport'));
        for (const field of viewFields()) viewport.options[field.dataset.view] = field.checked;
        viewport.options.light = read('shading') === 1;
        viewport.options.normals = checked('normals');
        const images = await loadImages(model.textures);
        if (!active() || request !== fighterRequest) return;
        viewport.setModel(model, images);
        if (motion) {
          viewport.setMotion(motion.animation, motion.events);
          paintFrame();
        }
        el('view-message').textContent = '';
      } catch (error) {
        el('view-message').textContent = error.message;
      }
    } else el('view-message').textContent = mesh.reason.message;
    if (move) return;
    const choice = profile.moves.find(item => item.id === id) || profile.moves[0];
    if (choice) await selectMove(choice.id, false, at ?? undefined, keepEvent);
    else await drawPanel();
  }

  async function compare() {
    if (!viewport || !motion) return;
    if (scene) await selectMove(move.id);
    const request = ++compareRequest, target = read('compare-role');
    const [opponentModel, animation] = await Promise.all([
      call('model', target, 0), call('animation', target, 0, read('compare-clip'))
    ]);
    const images = await loadImages(opponentModel.textures);
    if (!active() || compareRequest !== request) return;
    viewport.setOpponent(opponentModel, images, animation.animation, animation.events);
    pose(motion.animation.first);
    setPlaying(true);
  }

  function sceneEvents() {
    if (!scene) return;
    const area = el(scene.kind + '-scene-events');
    const integer = Math.floor(frame);
    const latest = scene.events.findLast(item => item.frame <= frame);
    const current = area.querySelector('.scene-current');
    if (current) current.textContent = latest ? '帧 ' + integer + ' · ' + latest.name : '帧 ' + integer;
  }

  function showSceneEvents() {
    const area = el(scene.kind + '-scene-events');
    if (scene.kind === 'skill') el('scene-media').hidden = false;
    if (tab === 'mix') el('mix-stop').hidden = false;
    const description = dom('p', scene.kind === 'skill' && scene.source === null ? '此招由角色专用回调控制。当前预览主动作。' :
      scene.kind === 'skill' ? '原版 IPU 特效、CMSP 镜头和双方动作跟随同一时间轴。' :
      scene.paired ? '双方按投技阶段同步。' : '此阶段只播放当前演员。', 'muted');
    const list = dom('div', '', 'scene-event-list');
    const entries = scene.events.filter(item => item.bom || ![0,4,5,6,24,27,28,29].includes(item.op));
    list.append(...entries.map(item => {
      const button = dom('button', Math.floor(item.frame) + ' 帧 · ' + item.name);
      button.type = 'button';
      button.onclick = () => seek(item.frame);
      return button;
    }));
    area.replaceChildren(description, dom('p', '', 'scene-current'), list);
    sceneEvents();
  }

  function paintFrame() {
    const shown = syncMovie() ?? frame;
    if (!filmWaiting || !playing) viewport?.setFrame(shown);
  }

  function stopMovie() {
    const video = el('effect-video');
    filmWaiting = false;
    if (videoCallback !== undefined) video.cancelVideoFrameCallback(videoCallback);
    videoCallback = movieCue = presentedFrame = seekFrame = undefined;
    if (movieId !== undefined) {
      video.pause();
      video.removeAttribute('src');
      video.load();
      movieId = undefined;
    }
    if (viewport) viewport.movie = null;
  }

  function syncMovie() {
    const cue = checked('show-ipu') && scene?.movies?.findLast(item => item.frame <= frame);
    const media = cue?.id != null && sceneMedia.get(cue.id);
    if (!media) { stopMovie(); return; }
    const video = el('effect-video');
    const changed = movieCue !== cue;
    if (movieId !== cue.id) {
      stopMovie();
      movieId = cue.id;
      video.src = media.uri;
    }
    if (changed) {
      video.pause();
      movieCue = cue;
      presentedFrame = seekFrame = undefined;
    }
    if (viewport) viewport.movie = null;
    filmWaiting = video.readyState < 2 || video.seeking;
    if (filmWaiting) return;
    const wanted = movieFrameAt(cue,frame,media);
    if (seekFrame !== undefined) {
      presentedFrame = seekFrame;
      seekFrame = undefined;
    }
    const decoded = presentedFrame ?? Math.floor(video.currentTime*media.rate+1e-6);
    const forward = playing && !cue.paused && wanted < media.frames-1;
    const needsSeek = changed || (forward ? Math.abs(decoded-wanted) > 3 : decoded !== wanted);
    if (needsSeek && decoded !== wanted) {
      video.pause();
      seekFrame = wanted;
      filmWaiting = true;
      video.currentTime = (wanted+.25)/media.rate;
      return;
    }
    if (forward) {
      video.playbackRate = read('speed');
      if (video.paused) video.play().catch(() => {});
    } else video.pause();
    if (!forward && decoded !== wanted) return;
    // 0x21C928 omits movie 4's sprite while this effect is drawn in 3D.
    if (viewport && (cue.id !== 4 || decoded < 537 || decoded >= 596)) viewport.movie = video;
    if (!forward || decoded === wanted) return frame;
    // The canvas uses the frame actually presented by the decoder for both
    // performers and the CMSP sample, even when the decoder drops a picture.
    const lastInCue = scene.movies.find(item => item.frame > cue.frame)?.frame ?? scene.last+1;
    return Math.max(cue.frame,Math.min(lastInCue-1e-4,cue.frame+(decoded-cue.first)*60/media.rate));
  }

  function refreshMovie() {
    if (!active() || !scene || !viewport) return;
    paintFrame();
    const video = el('effect-video');
    if (movieId === undefined || !video.requestVideoFrameCallback || videoCallback !== undefined) return;
    videoCallback = video.requestVideoFrameCallback((_,metadata) => {
      videoCallback = undefined;
      const media = sceneMedia.get(movieId);
      if (media && !video.seeking && seekFrame === undefined) {
        presentedFrame = Math.floor(metadata.mediaTime*media.rate+.1);
      }
      refreshMovie();
    });
  }

  async function playBattle(kind, number, target, donor = null, donorCandidate = null, curve = -1) {
    if (!viewport || !motion) return;
    if (kind === 'skill' && donor === null) {
      const chosen = profile.skills.find(item => item.id === number);
      if (chosen && !(chosen.menuMask & (1 << shape))) {
        const usable = profile.forms.find(item => !item.accessory && (chosen.menuMask & (1 << item.id)));
        if (usable) {
          shape = bindingSlot = usable.id;
          await loadFighter(chosen.anm);
        }
      }
    }
    const request = ++compareRequest, fighter = fighterRequest;
    setPlaying(false);
    const current = () => active() && compareRequest === request && fighter === fighterRequest;
    const [battle, rival] = await Promise.all([
      call('battle_preview', role, shape, kind, number, target, donor, donorCandidate, curve),
      call('model', target, 0),
    ]);
    const images = battle.actors[1].length ? await loadImages(rival.textures) : [];
    if (!current()) return;
    stopMovie();
    sceneMedia.clear();
    scene = {...battle,kind};
    viewport.setBattleScene(scene,rival,images);
    el('view-message').textContent = scene.name + ' · 同步预览';
    timeline();
    pose(0);
    showSceneEvents();
    el(kind + '-scene-events').closest('details')?.setAttribute('open', '');
    const movies = [...new Set((scene.movies || []).map(cue => cue.id).filter(id => id != null))];
    if (movies.length) {
      el('view-message').textContent = '正在读取原版必杀特效';
      const results = await Promise.allSettled(movies.map(id => call('scene_movie', id)));
      if (!current()) return;
      for (const item of results) if (item.status === 'fulfilled') sceneMedia.set(item.value.id, item.value);
      const failed = results.find(item => item.status === 'rejected');
      if (failed) message(failed.reason.message, true);
      el('view-message').textContent = scene.name + ' · 同步预览';
    }
    setPlaying(scene.last > 0);
  }

  function assetRows() {
    const bindings = new Map();
    if (tab === 'appearance' && form()) {
      bindings.set(keyText(form().model), '模型');
      form().textures.forEach((key, i) => { if (key) bindings.set(keyText(key), '贴图槽 ' + i); });
      form().secondary.forEach((key, i) => {
        if (key && !bindings.has(keyText(key))) bindings.set(keyText(key), '辅助贴图 ' + i);
      });
    }
    const query = el('search').value.trim().toLowerCase(), format = el('kind').value;
    const list = rows.filter(item => (!bindings.size || bindings.has(keyText(partKey(item)))) &&
      (!format || item.kind === format) && [item.id,item.name,item.kind,keyText(partKey(item))].join(' ').toLowerCase().includes(query));
    if (bindings.size) {
      const indices = new Map([...bindings.keys()].map((key,index) => [key,index]));
      list.sort((a,b) => indices.get(keyText(partKey(a))) - indices.get(keyText(partKey(b))));
    }
    if (tab === 'appearance' && !list.some(item => same(item, part))) part = list[0];
    const last = Math.max(0, Math.ceil(list.length / PAGE) - 1);
    page = Math.max(0, Math.min(page, last));
    el('prev').disabled = page === 0;
    el('next').disabled = page === last;
    el('count').textContent = list.length.toLocaleString() + ' 项 · ' + (page + 1) + '/' + (last + 1);
    el('rows').replaceChildren(...list.slice(page * PAGE, (page + 1) * PAGE).map(item => {
      const key = keyText(partKey(item)), label = bindings.get(key);
      const tr = row([key, label ? label + ' · ' + item.name.split('/').at(-1) : item.name,
        item.kind.toUpperCase() + (item.edited ? ' · 已改' : '')], async () => {
        if (state.writing) return;
        part = item;
        palette = usedPalettes(item)[0]?.palette ?? 0;
        script = clip = 0;
        eventId = -1;
        await drawPanel();
        remember();
      }, same(item, part));
      tr.title = item.name;
      return tr;
    }));
  }

  function usedPalettes(item) {
    return model?.textures.filter(texture => texture.resource === item.id && texture.member === item.member) || [];
  }

  async function drawAsset(request) {
    for (const id of ['resource','pom','anm','bom','texture','keys']) el(id).hidden = true;
    detail = rawEvents = null;
    el('empty').hidden = Boolean(part);
    if (!part) return;
    const data = await call('details', ...partKey(part));
    if (request !== panelRequest || !active()) return;
    detail = data;
    el('resource').hidden = false;
    el('name').textContent = part.name + (part.edited ? ' · 已修改' : '');
    const pairs = [['资源',keyText(partKey(part))],['格式',part.kind.toUpperCase()],
      [part.compressed ? '解包大小' : '大小',data.size.toLocaleString() + ' B']];
    if (data.nodes) pairs.push(['节点',data.nodes],['几何片段',data.fragments]);
    if (data.palette_count) {
      pairs.push(['尺寸',data.width + ' × ' + data.height],['色表数',data.palette_count]);
      const used = usedPalettes(part);
      if (used.length) pairs.push(['模型使用色表', used.map(item => item.requested).join('、')]);
    }
    properties(el('info'), pairs);
    el('resource-note').textContent = data.texture_error || '';
    el('import').disabled = !data.editable;
    el('restore').disabled = !data.edited;
    if (data.palette_count) {
      el('pom').hidden = false;
      const used = new Set(usedPalettes(part).map(item => item.palette));
      palette = options('palette', Array.from({length:data.palette_count}, (_, i) =>
        [i,'色表 ' + i + (used.has(i) ? ' · 模型使用' : '')]), palette);
      await drawTexture();
    } else if (data.clips) {
      el('anm').hidden = false;
      clip = options('clip', data.clips.map((item,i) => [i, i + ' · ' + item.frames + ' 帧 · ' + item.tracks + ' 轨']), clip);
      showClip();
      await drawKeys();
    } else if (data.scripts?.length) {
      el('bom').hidden = false;
      script = options('script', data.scripts.map((count,i) => [i,i + ' · ' + count + ' 条记录']), script);
      await showScript();
    }
  }

  async function drawTexture() {
    const request = ++panelRequest;
    const src = await call('texture', ...partKey(part), palette);
    if (request !== panelRequest || !active()) return;
    el('texture').src = src;
    el('texture').hidden = false;
  }

  function showClip() {
    const record = detail?.clips?.[clip];
    if (record) for (const field of ['frames', 'first', 'last', 'rate']) el(field).value = record[field];
  }

  function selectKey(index, jump = false) {
    keyRow = index;
    const item = keys[index];
    if (!item) return;
    el('key-frame').value = item.frame;
    ['x','y','z'].forEach((name,i) => { el('key-' + name).value = displayKey(item.value[i]); });
    for (const [i,tr] of [...el('key-rows').rows].entries()) tr.classList.toggle('active', i === keyRow);
    if (jump && tab === 'animation') seek(item.frame);
  }

  const displayKey = value => read('channel') === 0 ? value * (2 * Math.PI / 65536) - Math.PI : value;
  const inputKey = () => ({frame:read('key-frame'), value:['key-x','key-y','key-z'].map(read).map(
    value => read('channel') === 0 ? Math.round((value + Math.PI) * 65536 / (2 * Math.PI)) : value)});

  async function drawKeys() {
    const request = ++keyRequest, panel = panelRequest;
    keys = [];
    el('keys').hidden = true;
    const available = detail?.clips?.[clip]?.channels || [];
    if (!available.length) return;
    track = options('track', available.map((id,i) => [i,id + ' → 骨骼 [' + (model?.rig[id] ?? '—') + ']']), track);
    const result = await call('keys', ...partKey(part), clip, track, read('channel'));
    if (!active() || request !== keyRequest || panel !== panelRequest) return;
    keys = result;
    el('key-rows').replaceChildren(...keys.map((item,i) => row([item.frame,...item.value.map(value => Number(displayKey(value).toFixed(9)))],
      () => selectKey(i, true))));
    el('keys').hidden = keys.length === 0;
    selectKey(Math.min(keyRow, keys.length - 1));
  }

  async function showScript() {
    const request = ++panelRequest;
    rawEvents = null;
    eventTable();
    const data = await call('events', ...partKey(part), script);
    if (request !== panelRequest || !active()) return;
    rawEvents = data;
    el('frame-events').hidden = false;
    eventTable();
  }

  function eventSource() {
    return EVENT_TABS.has(tab) ? {items:motion?.events || EMPTY_EVENTS, key:motion?.bom, script:motion?.animation.id} :
      {items:rawEvents || EMPTY_EVENTS, key:rawEvents && part && partKey(part), script};
  }

  function eventTable() {
    const source = eventSource();
    const fromClip = EVENT_TABS.has(tab);
    const atCurrentFrame = fromClip && currentOnly;
    const tick = frameIndex(frame);
    for (const id of ['current','entire']) {
      const active = (id === 'current') === currentOnly;
      const button = el(id);
      setHidden(button, !fromClip);
      if (button.classList.contains('active') !== active) button.classList.toggle('active', active);
      if (button.getAttribute('aria-pressed') !== String(active)) button.setAttribute('aria-pressed', String(active));
    }
    if (listedSource !== source.items || listedTab !== tab) {
      listedSource = source.items;
      listedTab = tab;
      eventRows = source.items.filter(item => matchesEventTab(item, tab)).map(item => {
        let range = item.frame;
        if (item.until != null) {
          const last = fromClip ? Math.min(item.until - 1, motion.animation.last) : item.until - 1;
          if (last > item.frame) range = item.frame + '–' + last;
        }
        const tr = row([range,item.name,hex(item.selector),item.base_damage ?? '—',''], () => {
          if (state.writing || tr.hidden) return;
          eventId = item.id;
          if (fromClip && !currentOnly) seek(item.frame);
          else if (fromClip) setPlaying(false);
          eventTable();
          remember();
        });
        return {item,tr};
      });
      el('events').replaceChildren(...eventRows.map(({tr}) => tr));
    }
    const record = updateEventList(eventRows, {
      frame:tick, currentOnly:atCurrentFrame, selectedId:eventId, timeline:fromClip,
      counter:el('event-count'), empty:el('event-empty'), showCount:Boolean(source.key),
      emptyText:fromClip && !motion ? (move ? '正在读取动作事件。' : '请选择动作。') :
        !source.key ? (fromClip ? '当前动作没有关联的 BOM 事件。' : '正在读取 BOM 事件。') :
        atCurrentFrame ? '当前帧没有此类事件。' : '此段没有此类事件。',
    });
    setHidden(el('event'), !record);
    if (!record || record === shownEvent) return;
    shownEvent = record;
    el('event-frame').value = record.frame;
    el('event-selector').value = record.selector;
    const fields = [], rawFields = [];
    record.params.forEach((item, index) => {
      const offset = '+' + hex(item.offset);
      if (!item.editable) {
        const raw = dom('li', offset + ' · ' + item.name + ' · ' + (item.value ?? '—'));
        raw.title = item.help;
        rawFields.push(raw);
        return;
      }
      const label = dom('label',item.name);
      label.title = offset + ' · ' + item.help;
      const field = dom('input');
      field.type = 'number'; field.step = item.type === 'h' ? 1 : 'any';
      field.required = item.value !== null;
      field.value = item.value ?? '';
      field.dataset.param = index;
      if (item.type === 'h') { field.min = -32768; field.max = 32767; }
      label.append(field);
      fields.push(label);
    });
    el('event-params').replaceChildren(...fields);
    el('event-params').hidden = fields.length === 0;
    el('event-raw').hidden = rawFields.length === 0;
    el('event-raw-values').replaceChildren(...rawFields);
    el('event-source').textContent = 'BOM ' + keyText(source.key) + ' · 脚本 ' + source.script +
      ' · 操作码 ' + hex(record.op) + (record.shape ? ' · 节点 ' + record.shape.node + ' · 形状 ' + record.shape.template : '');
    if (record.combo) el('event-source').textContent += ' · ' + record.combo.label + SEQUENCE + 'ANM ' + pad(record.combo.target);
    el('event-source').title = '原生参数 ' + record.args.join(', ');
  }

  function overview() {
    const data = form();
    const fields = [['角色',fighter().name],['镜像版本',state.iso.revision + ' · SLPS_254.41'],
      ['形态',data.name],['模型',keyText(data.model)],['BOM',keyText(data.bom)],['ANM 片段',profile.moves.length]];
    if (model) fields.push(['模型节点',model.bones.length],['预览三角面',model.triangles.toLocaleString()]);
    properties(el('overview-info'), fields);
    const notes = [];
    if (model?.polygon4Pieces) notes.push(model.polygon4Pieces + ' 个 PS2 半透明片段');
    if (model?.textures.some(image => image.palette !== image.requested)) notes.push('原生引用超出 POM 范围的色表，预览使用该槽色表 0');
    el('render-note').textContent = notes.join('。');
  }

  function subTabs(id, active) {
    for (const button of el(id).querySelectorAll('[data-pane]')) {
      const current = button.dataset.pane === active;
      button.classList.toggle('active', current);
      button.setAttribute('aria-pressed', String(current));
    }
  }

  function commands() {
    subTabs('input-tabs', inputTab);
    el('input-states').hidden = inputTab !== 'states';
    el('input-rules').hidden = inputTab !== 'rules';
    el('input-usage').hidden = inputTab !== 'usage';
    el('combo-links').hidden = inputTab !== 'windows';
    commandState = options('command-source', profile.states.map(item => [item.state,
      '状态 ' + item.state + (item.context ? ' · ' + item.context : '')]), commandState);
    const entries = (profile.commands[commandState] || []).filter(item =>
      !checked('filter-form') || !(item.excluded & (1 << shape)));
    const selected = entries.find(item => item.erx === choice);
    el('command-rows').replaceChildren(...entries.map(item => row(
      [item.usage || item.label || '#' + item.command,item.to,item.anm < 0 ? '—' : pad(item.anm),item.condition,item.priority,item.order],
      () => { choice = item.erx; commands(); }, selected === item)));
    el('follow-command').disabled = !selected || selected.anm < 0;
    el('follow-command').onclick = run(async () => {
      if (!selected || selected.anm < 0) return;
      await selectMove(selected.anm, true);
      commandState = selected.to;
      choice = null;
      commands();
      remember();
    });
    el('command-note').textContent = selected ? 'ERX ' + hex(selected.erx, 8) + ' · 条件入口 ' +
      hex(selected.handler, 8) + ' · 排除形态位 ' + hex(selected.excluded) + ' · 输入 #' + selected.command :
      entries.length ? '选择一个候选分支。' : '当前状态无匹配候选。';
    const links = profile.combos[move?.id] || [];
    el('combo-from').textContent = links.length ? 'ANM ' + pad(move?.id) + ' · 连段接续' : '当前 ANM 无匹配的连段事件';
    const link = links.find(item => item.id === comboId);
    el('combo-rows').replaceChildren(...links.map(link => row(
      [link.label,pad(link.target),link.start + '–' + link.end,hex(link.selector)],
      () => { comboId = link.id; commands(); }, comboId === link.id)));
    el('combo-edit').hidden = !link;
    if (link) {
      options('combo-command', rules.map(item => [item.id, item.id + ' · ' + item.label]), link.command);
      el('combo-start').value = link.start;
      el('combo-duration').value = link.duration;
      el('combo-target').value = link.target;
      el('combo-mask').value = link.selector;
    }
    if (inputTab === 'rules') {
      const key = options('input-rule', rules.map(item => [item.id,item.id + ' · ' + item.label]), read('input-rule'));
      const rule = rules.find(item => item.id === key);
      properties(el('rule-info'), rule ? [['原生指令',rule.name],['资源 / 偏移','0x09D1 / ' + hex(rule.offset, 4)],
        ...rule.steps.map((steps,i) => ['步骤 ' + (i + 1),steps.slice(0,4).map(v => hex(v,4)).join(' / ') +
          ' · ' + steps.slice(4).join(' / ')])] : []);
    }
    if (inputTab === 'usage') properties(el('input-usage'), [['动作',move ? anmName(move) : '—'],
      ['动作号',move?.id ?? '—'], ['引用状态',move?.states.join('、') || '—'],
      ['触发输入',move?.keys.join('、') || '—'],['连段接续',links.map(item => pad(item.target)).join('、') || '—']]);
  }

  function availableForms(mask) {
    return profile.forms.filter(item => !item.accessory && (mask & (1 << item.id)));
  }

  const selectedSupport = () => profile[supportGroup].find(item => item.id === supportId);
  const addingSupport = () => el('support-mix-mode').value === 'add';

  function supportMixStatus() {
    const chosen = selectedSupport();
    const source = supportSources.find(item => item.id === read('support-donor-move'));
    const activeChoice = chosen?.transfer ?? chosen?.added ?? chosen?.installedSupport;
    const adding = addingSupport();
    const guard = supportGroup === 'guard';
    el('support-mix-apply').disabled = !source || Boolean(source.reason);
    el('support-mix-apply').textContent = adding ? (supportGroup === 'guard' ? '新增防御技' : '新增辅助技') : '应用配招';
    el('support-mix-restore').hidden = !chosen?.transfer && !(chosen?.added && chosen.id === profile.lastAddedSkill);
    el('support-mix-restore').textContent = chosen?.added ? '移除新增技能' : '还原配招';
    el('support-mix-note').textContent = source?.reason || (guard && source
      ? (source.scene ? '移植吸收动作、演出和回血。在游戏内装备后触发。' : '移植防御判定。')
      : activeChoice ? '已借用 ' + state.iso.roles[activeChoice[0]].name
      : supportDonor >= 0 && !supportSources.length ? '来源角色没有这一类原生候选。'
      : adding ? '新增到「' + form().name + '」。另存镜像后，在游戏内装备。'
      : (supportGroup === 'guard' ? '让当前候选使用来源的屏障判定规则。' : '移植可兼容的肩键动作或属性强化援护。'));
  }

  async function listSupportSources() {
    const request = ++supportRequest, donor = supportDonor, candidate = supportId, receiving = shape;
    const adding = addingSupport();
    const recipient = role;
    const select = el('support-donor-move');
    supportSources = [];
    select.replaceChildren();
    select.disabled = true;
    supportMixStatus();
    if (el('support-mix-mode').selectedOptions[0]?.disabled) return;
    if (donor >= 0) {
      el('support-mix-note').textContent = '正在读取来源技能';
      const rows = await call('support_sources',recipient,adding ? null : candidate,donor,
        supportGroup === 'guard' ? 0 : 2,receiving);
      if (!active() || request !== supportRequest || tab !== 'other-skills'
          || role !== recipient || supportId !== candidate || shape !== receiving) return;
      supportSources = rows;
      select.replaceChildren(...rows.map(item => new Option(item.reason ? item.name + ' · 待适配' : item.name,item.id)));
      const selected = rows.find(item => item.id === supportDonorMove) ?? rows.find(item => !item.reason) ?? rows[0];
      if (selected) select.value = selected.id;
      supportDonorMove = selected?.id ?? -1;
    }
    select.disabled = !supportSources.length;
    supportMixStatus();
  }

  function supportTable() {
    const list = profile[supportGroup];
    const guard = supportGroup === 'guard';
    for (const button of el('support-tabs').querySelectorAll('button')) {
      const selected = button.dataset.supportGroup === supportGroup;
      button.classList.toggle('active', selected);
      button.setAttribute('aria-pressed', String(selected));
    }
    const chosen = list.find(item => item.id === supportId) ?? list[0];
    supportId = chosen?.id;
    const current = item => {
      if (guard && item.guardMotions?.[shape] != null) return [item.action, '吸收与回血', item.guardMotions[shape]];
      if (!guard && item.action > 0 && (item.transfer || item.installedSupport)) {
        return [item.action, '借用动作', item.anm];
      }
      return item.choices.find(([value]) => value === item.action)
        ?? [item.action, item.assistSource == null ? '现有效果 ' + item.action : '属性强化援护', item.anm];
    };
    el('support-rows').replaceChildren(...list.map(item => row(
      [item.name, current(item)[1] + ((item.transfer || item.added || item.installedSupport)
        ? ' · ' + state.iso.roles[(item.transfer ?? item.added ?? item.installedSupport)[0]].name : ''),
        availableForms(item.menuMask).map(form => form.name).join('、') || '—'], () => {
        supportId = item.id;
        supportValue = undefined;
        supportTable();
        remember();
      }, item.id === supportId)));
    el('support-table').hidden = !chosen;
    el('support-edit').hidden = !chosen;
    el('support-empty').hidden = Boolean(chosen);
    const canAdd = profile.supportMenu && shape < 3 && !form()?.accessory;
    const canReplace = Boolean(chosen && !chosen.added);
    const mix = el('support-mix'), mode = el('support-mix-mode');
    mix.hidden = !canAdd && !canReplace && !chosen?.added;
    el('support-empty').textContent = canAdd ? (guard ? '可从其他角色新增防御技。' : '可从其他角色新增辅助技。')
      : '当前出场配置使用固定技能或附属模型。';
    mode.options[0].disabled = !canReplace;
    mode.options[1].disabled = !canAdd;
    if (!canReplace && canAdd) mode.value = 'add';
    if (canReplace && !canAdd) mode.value = 'replace';
    if (!mix.hidden) {
      if (chosen?.transfer && !addingSupport()) [supportDonor,supportDonorMove] = chosen.transfer;
      supportDonor = options('support-donor',[[-1,'选择来源角色'],...roleOptions().filter(([id]) => id !== role)],supportDonor);
      if (!chosen) mix.open = true;
      if (mix.open) run(listSupportSources)();
      else supportMixStatus();
    }
    if (!chosen) return;
    const locked = Boolean(chosen.transfer || chosen.added);
    const chosenEffect = current(chosen);
    const choices = locked ? [chosenEffect] : chosen.choices.map(choice =>
      choice[0] === chosen.action ? chosenEffect : choice);
    if (!choices.some(([value]) => value === chosen.action)) choices.push(chosenEffect);
    const forms = availableForms(chosen.menuMask);
    el('support-label').textContent = guard ? '屏障类型' : '肩键效果';
    const equipping = chosen.configurable ? '在游戏内按列表中的名字装备。' : '';
    el('support-note').textContent = guard
      ? (chosen.guardMotions?.[shape] != null ? equipping + '防御成功后播放来源演出并回血。'
        : '类型 1 使用通常屏障许可。类型 2、3 使用特殊防御许可，类型 3 的眩晕增量较低。')
      : equipping + '角色动作沿用原判定和特效，援护按游戏原有条件触发。';
    options('support-effect', choices.map(([value, label, anm]) => [value, anm < 0 ? label : label + ' · ANM ' + pad(anm)]),
      supportValue ?? chosen.action);
    const select = el('support-effect');
    select.disabled = locked;
    const showSelection = () => {
      const selected = read('support-effect');
      const anm = choices.find(([value]) => value === selected)?.[2] ?? -1;
      el('support-apply').disabled = locked || selected === chosen.action;
      const preview = el('support-preview');
      preview.hidden = anm < 0 || forms.length === 0;
      preview.onclick = run(async () => {
        if (preview.hidden) return;
        if (!forms.some(item => item.id === shape)) {
          shape = bindingSlot = forms[0].id;
          await loadFighter(anm);
        }
        await selectMove(anm, true);
      });
    };
    select.onchange = () => {
      supportValue = read('support-effect');
      showSelection();
    };
    showSelection();
    el('support-restore').disabled = locked
      || chosen.words.every((value, index) => value === chosen.original[index]);
    properties(el('support-source'), [['ELF 地址',hex(chosen.va,8)],['候选号',chosen.id],
      ['原始记录',chosen.original.map(value => hex(value,8)).join(' · ')],
      ...(chosen.assistSource == null ? [] : [['援护来源',state.iso.roles[chosen.assistSource].name]])]);
  }

  async function donorChoices() {
    const request = ++donorRequest, donor = donorRole;
    for (const button of ['mix-apply','mix-preview','mix-copy']) el(button).disabled = true;
    const skills = await call('skills', donor);
    if (!active() || request !== donorRequest || tab !== 'mix' || mixTabs.value !== 'local') return;
    donorSkill = options('skill-donor-move', skills.filter(item => item.menuCandidate && !item.added && item.anm >= 0)
      .map(item => [item.id, item.name]), donorSkill);
    const replacing = el('mix-mode').value === 'replace';
    const receiving = replacing ? form() : profile.forms.find(item => item.id === read('mix-form'));
    const hasSource = el('skill-donor-move').options.length > 0 && donor !== role && receiving && !receiving.accessory;
    const hasTarget = el('mix-target-move').options.length > 0;
    el('mix-preview').disabled = !hasSource;
    el('mix-apply').disabled = !hasSource || (replacing && !hasTarget);
    el('mix-copy').disabled = !hasSource || !hasTarget;
  }

  function drawMix() {
    const replacing = el('mix-mode').value === 'replace';
    const skillRows = profile.skills.filter(item => item.menuCandidate && !item.added && item.menuMask & (1 << shape));
    el('mix-target').hidden = el('mix-copy').hidden = !replacing;
    el('mix-form-label').hidden = replacing;
    el('mix-stop').hidden = !scene;
    options('mix-form',profile.forms.filter(item => item.id < 3 && !item.accessory)
      .map(item => [item.id,item.name]),shape);
    el('mix-apply').textContent = replacing ? '应用替换' : '新增到必杀技表';
    options('mix-target-move',skillRows.map(item => [item.id, item.name]),mixId >= 0 ? mixId : skillId);
    options('skill-donor',roleOptions().filter(([id]) => id !== role),donorRole);
    donorRole = read('skill-donor');
    options('mix-opponent',roleOptions(),opponent);
    el('mix-note').hidden = replacing;
    el('mix-note').textContent = '另存镜像后，在游戏内的「必杀技自定义」装备。';
    run(donorChoices)();
  }

  function drawMixHistory() {
    const prior = profile.skills.filter(item => item.transfer || item.added);
    el('mix-empty').hidden = prior.length > 0;
    el('mix-history-table').hidden = prior.length === 0;
    el('mix-rows').replaceChildren(...prior.map(item => row([item.name,item.added ? '新增' : '替换',
      state.iso.roles[(item.added || item.transfer)[0]].name,pad(item.action) + ' / ' + pad(item.anm)], () => {
      mixId = item.id;
      drawMixHistory();
    },mixId === item.id)));
    const selected = prior.find(item => item.id === mixId);
    el('mix-reset').hidden = !selected || (selected.added && selected.id !== profile.lastAddedSkill);
    el('mix-reset').textContent = selected?.added ? '移除新增必杀技' : '还原所选配招';
    el('mix-reset').onclick = () => selected?.added
      ? apply('remove_finisher',[role,selected.id],'已移除新增必杀技')
      : apply('transfer_finisher',[role,selected.id,null,null],'已还原必杀配招');
  }

  function skillTable() {
    const list = profile.skills;
    const fixedLabel = item => item.fixedButtons?.length ? '固定 ' + item.fixedButtons.join(' ') : '固定配置';
    el('skill-rows').replaceChildren(...list.map(item => {
      const available = availableForms(item.menuMask).map(shape => shape.name).join('、') || hex(item.menuMask,1);
      const name = item.configurable ? item.name : item.name + ' · ' + fixedLabel(item);
      const tr = row([item.number,name,item.rating?.grade ?? '—',item.action,available,hex(item.va,8)], async () => {
        skillId = item.id;
        if (item.anm >= 0 && move?.id !== item.anm) await selectMove(item.anm);
        else skillTable();
        remember();
      }, skillId === item.id);
      tr.title = '默认 ANM ' + (item.anm < 0 ? '—' : pad(item.anm));
      if (item.anm >= 0) tr.ondblclick = run(() => selectMove(item.anm, true));
      return tr;
    }));
    const chosen = list.find(item => skillId === item.id);
    el('skill-edit').hidden = !chosen;
    el('skill-battle').hidden = !chosen;
    el('skill-rating').hidden = !chosen?.rating;
    el('skill-actions').hidden = !list.some(item => item.configurable);
    if (!chosen) return;
    el('skill-name').textContent = chosen.name;
    properties(el('skill-source'), [['原始记录地址',hex(chosen.va,8)],['默认 ANM',chosen.anm < 0 ? '—' : pad(chosen.anm)],
      [chosen.menuCandidate ? '候选序号' : '战斗记录编号',chosen.id],['可用形态掩码',hex(chosen.menuMask)],
      ['原值',chosen.original.map(value => hex(value,8)).join(' · ')],
      ['装配方式', chosen.configurable ? '游戏内必杀自定义' : fixedLabel(chosen)],
      ...(chosen.nameSource ? [['名称图集（配置与选人）',keyText(chosen.nameSource) + ' · 行 ' + chosen.id]] : []),
      ...(chosen.loadingNameSource ? [['加载页名称图集',keyText(chosen.loadingNameSource) + ' · 行 ' + chosen.id]] : []),
      ...(chosen.descriptionSource ? [['介绍图块',keyText(chosen.descriptionSource.slice(0,2)) + ' · 格 ' + chosen.descriptionSource[2]]] : []),

      ['达到本招门槛（常规／TAG 特殊分支）', chosen.chargeFrames.map(n => n == null ? '超出蓄力上限' : n + ' 帧').join(' ／ ')]]);
    const fieldFor = (name, index, {power = false, readonly = false, bit, hint = ''} = {}) => {
      const label = dom('label', name);
      label.title = hint;
      const field = dom('input');
      field.dataset.word = index;
      if (bit !== undefined) {
        field.type = 'checkbox';
        field.dataset.byte = bit;
        field.checked = Boolean((chosen.words[index] >>> (8 * bit)) & 255);
      } else {
        field.type = 'number';
        field.min = 0;
        field.max = power ? 32767 : 0xFFFFFFFF;
        field.step = power ? 'any' : 1;
        field.required = true;
        field.readOnly = readonly;
        field.value = power ? chosen.power : chosen.words[index];
        if (power) field.dataset.float = '1';
      }
      label.append(field);
      return label;
    };
    el('skill-fields').replaceChildren(
      fieldFor('启动状态', 0, {readonly:!chosen.menuCandidate || Boolean(chosen.added || chosen.transfer),
        hint:'+0x00 · 战斗槽位使用的启动状态。'}),
      fieldFor(chosen.menuCandidate ? '候选序号' : '战斗记录编号', 2,
        {readonly:true, hint:'+0x08 · 原生必杀记录使用的编号。'}),
      fieldFor('释放所需蓄力值', 3, {power:true, hint:'+0x0C · 蓄力槽上限为 256，超过上限时常规蓄力无法满足门槛。'}),
      fieldFor('可被类型 1 屏障同键防御', 4, {bit:0, hint:'+0x10 · 仅在防御按键匹配时读取。'}),
      fieldFor('可被类型 2／3 屏障同键防御', 4, {bit:1, hint:'+0x11 · 仅在防御按键匹配时读取。'})
    );
    el('skill-preview').disabled = chosen.anm < 0;
    el('skill-preview').onclick = run(() => selectMove(chosen.anm, true));
    el('skill-restore').disabled = chosen.words.every((value, i) => value === chosen.original[i]);
    if (chosen.rating) {
      const {grade, equippedAtOne, edited, value} = chosen.rating;
      const selector = el('skill-grade');
      selector.value = grade === '—' ? '' : grade;
      const applyRating = el('skill-rating-apply');
      selector.onchange = () => { applyRating.disabled = !selector.value || selector.value === grade; };
      applyRating.disabled = true;
      applyRating.onclick = () => apply('edit_skill_rating', [role,chosen.id,selector.value], '已应用必杀评级');
      const resetRating = el('skill-rating-reset');
      resetRating.disabled = !edited;
      resetRating.onclick = () => apply('edit_skill_rating', [role,chosen.id,null], '已恢复必杀评级');
      el('skill-rating-label').textContent = chosen.configurable ? '候选评级' : 'ELF 评级';
      el('skill-rating-note').textContent = '评级底数 ' + value + (chosen.configurable
        ? ' · 装备倍率为 1 倍时为 ' + equippedAtOne + '。装备栏按实际组合计算。'
        : ' · ' + fixedLabel(chosen) + '。原版战斗使用固定必杀记录。');
    }
    options('skill-opponent', roleOptions(), opponent);
    el('skill-scene').disabled = chosen.anm < 0;
    el('skill-scene').onclick = run(() => playBattle('skill',chosen.id,read('skill-opponent')));
  }

  function showBand() {
    const item = hp?.[band];
    if (!item) return;
    el('hp-value').value = item.raw;
    el('hp-summary').value = item.raw ? (32768 / item.raw).toFixed(2) : '';
    el('hp-source').textContent = (item.raw / 128).toFixed(4) + ' 倍 · 原值 ' + item.original + ' · ELF ' + hex(item.va, 8);
    el('hp-restore').disabled = item.raw === item.original;
  }

  async function bindings(request) {
    if (!profile.forms.some(item => item.id === bindingSlot)) bindingSlot = shape;
    const selected = profile.forms.find(item => item.id === bindingSlot);
    const member = key => key?.[1] ?? '—';
    el('bindings-rows').replaceChildren(...profile.forms.map(item => row(
      [item.id,member(item.model),member(item.vau),member(item.bom),item.textures.map(member).join('、')],
      async () => { bindingSlot = item.id; await drawPanel(); }, item.id === bindingSlot)));
    const found = await call('binding', role, bindingSlot);
    if (request !== panelRequest || !active()) return;
    el('binding-name').textContent = selected.name;
    properties(el('binding-info'), [['FUM',selected.model[0]],...found.map(item => [item.name,
      'ELF ' + hex(item.va,8) + ' · 原值 ' + (item.source >= 0xFE000000 ? hex(item.source,8) : item.source)])]);
    el('binding-fields').replaceChildren(...found.map(item => {
      const label = dom('label',item.name);
      const field = dom('input');
      field.type = 'text'; field.required = true;
      field.value = item.value >= 0xFE000000 ? hex(item.value,8) : item.value;
      label.append(field);
      return label;
    }));
    el('binding-restore').disabled = found.every(item => item.value === item.source);
  }

  async function musicTable(request) {
    const result = await call('music', role);
    if (request !== panelRequest || !active()) return;
    musicId = options('music-track', result.library.map(item =>
      [item.id,'BGM ' + pad(item.id) + ' · ' + item.resource + ' · ' + labelTime(item.seconds)]), musicId ?? result.role[0]?.id);
    const details = () => {
      const item = result.role.find(item => item.id === musicId) || result.library.find(item => item.id === musicId);
      properties(el('music-info'), item ? [['使用位置',item.label || '原盘曲库'],['资源 / ISO 偏移',
        item.resource + ' · ' + hex(item.isoOffset,8)], ['编码','SPU ADPCM · 双声道 · 44,098 Hz']] : []);
    };
    details();
    el('music-rows').replaceChildren(...result.role.map(item => {
      const tr = row([item.label,pad(item.id),item.resource,labelTime(item.seconds)], () => {
        el('music-track').value = musicId = item.id;
        details();
        for (const sibling of el('music-rows').rows) sibling.classList.toggle('active', sibling === tr);
      }, item.id === musicId);
      tr.ondblclick = run(playMusic);
      return tr;
    }));
    el('music-track').onchange = () => { musicId = read('music-track'); details(); remember(); };
  }

  async function playMusic() {
    musicId = read('music-track');
    const request = ++musicRequest;
    el('music-note').textContent = '正在解码 BGM ' + pad(musicId);
    try {
      const src = await call('listen', musicId);
      if (!active() || request !== musicRequest || !src) return;
      el('audio').src = src;
      el('audio').loop = checked('music-loop');
      await el('audio').play();
      el('music-note').textContent = '正在播放 BGM ' + pad(musicId) + ' · 双声道 SPU ADPCM · 44,098 Hz';
    } catch (error) {
      if (active() && request === musicRequest) el('music-note').textContent = error.message;
    }
  }

  function stopMusic() {
    ++musicRequest;
    el('audio').pause();
    el('audio').removeAttribute('src');
    el('audio').load();
    el('music-note').textContent = '已停止';
    if (active()) call('stop_music').catch(() => {});
  }

  async function drawThrows(request) {
    subTabs('throw-tabs', throwTab);
    opponent = options('throw-opponent', roleOptions(), opponent);
    const records = await call('throw_view', role, shape, opponent);
    if (request !== panelRequest || !active()) return;
    throwRows = records.stages;
    throwCurve = options('throw-curve', [[-1, '双方视角'], ...records.curves.map(item => [item.id,item.name])], throwCurve);
    const table = (headers, items) => {
      el('throw-head').replaceChildren(...headers.map(title => dom('th', title)));
      el('throw-rows').replaceChildren(...items);
    };
    const preview = anm => anm < 0 ? null : () => selectMove(anm, true);
    if (throwTab === 'selection') table(['起始状态','输入','目标状态','默认 ANM','判定入口'],
      records.selection.map(item => row([item.from,item.label,item.to,pad(item.anm),hex(item.handler,8)], preview(item.anm))));
    else if (throwTab === 'range') table(['状态','ANM','帧','节点','模板','半径'],
      records.range.map(item => row([item.state,pad(item.anm),item.frame,item.shape.node,item.shape.template,
        Number(item.shape.radius.toFixed(3))], () => selectMove(item.anm, false, item.frame))));
    else if (throwTab === 'camera') table(['状态','ANM','回调阶段','EE 回调','镜头调用'],
      records.camera.map(item => row([item.state,pad(item.anm),item.phase,hex(item.ee,8),'0x00235CB0'], preview(item.anm))));
    else if (throwTab === 'banks') table(['对手组资源','ANM','片段','帧','用途'],
      records.banks.map(item => row([item.asset[0],pad(item.id),item.asset[2],item.frames,anmName(item)], preview(item.id))));
    else table(['阶段','状态','动画','后续状态','选择方式'], throwRows.map(item => row(
      [item.name,item.action,item.action === 120 && item.anm !== item.default ? pad(item.default) + ' → ' + pad(item.anm) : pad(item.anm),
        item.next,item.dynamic ? '按对手选择' : '状态默认'], () => { throwState = item.action; drawThrows(panelRequest); },
      throwState === item.action)));
    const selected = throwTab === 'stages' ? throwRows.find(item => item.action === throwState) : null;
    el('throw-edit').hidden = !selected;
    if (!selected) return;
    el('throw-name').textContent = selected.name;
    properties(el('throw-source'), [['ERX 状态记录',hex(selected.erx,8)],['默认 ANM',pad(selected.default)],
      ['当前对手动画',pad(selected.anm)], ...selected.callbacks.map(({erx,ee},i) =>
        [['更新回调','进入回调','离开回调'][i],hex(erx,4) + (ee ? ' → EE ' + hex(ee,8) : '')])]);
    el('throw-default-label').hidden = selected.dynamic;
    el('throw-default').value = selected.default;
    el('throw-apply').disabled = el('throw-restore').disabled = selected.dynamic;
    el('throw-preview').onclick = run(async () => {
      if (move?.id !== selected.anm) await selectMove(selected.anm);
      await playBattle('throw', selected.action, opponent, null, null, read('throw-curve'));
    });
  }

  const helpText = {
    animation:'单个 ANM 包含当前动作的全部轨道。旋转显示为弧度，保存时量化到原生半字。',
    commands:'原生 ERX 状态候选与 BOM 连段窗口使用各自的入口。选中分支可以推进并播放默认动画。',
    skills:'每条必杀记录有五个原始字段。+0C 保存释放所需的蓄力值，菜单可用形态可在来源信息核对。',
    'other-skills':'选择防御技或肩键辅助技，按角色修改实际生效的屏障类型、辅助动作或援护。',
    throws:'对手会改变原生投技选择的动画。表中显示实际对手动画及默认项，原生回调见来源信息。',
    hp:'128 表示 1.0 倍。游戏把形态承伤和随当前生命值变化的倍率相乘。',
    appearance:'贴图预览按 GS Alpha 显示。导出的 PNG 保留原生色表字节，便于按原索引导回。',
    music:'角色音乐从原 ELF 和流式音轨索引读取，WAV 保留原音轨的双声道与采样率。',
    'shared-resources':'编辑镜像内的 BGM 与通用贴图，修改可随工程保存或写入新镜像。',
  };

  async function drawPanel() {
    const request = ++panelRequest;
    for (const id of PANEL_IDS) el(id).hidden = true;
    for (const button of navButtons()) {
      const selected = button.dataset.fe3Tab === tab;
      button.classList.toggle('active', selected);
      button.setAttribute('aria-pressed', String(selected));
    }
    el('category').textContent = TABS[tab].group;
    el('editor-title').textContent = TABS[tab].title;
    el('help').textContent = helpText[tab] || TABS[tab].title + ' · 选中条目可查看来源和原始值。';
    updateResources?.(tab === 'shared-resources');
    if (!profile) return;
    if (ASSET_TABS.has(tab)) {
      el('asset-detail').hidden = false;
      el('compare').hidden = tab !== 'animation';
      if (tab === 'animation') {
        options('compare-role', roleOptions(), el('compare-role').options.length ? read('compare-role') : 4);
        el('alias').value = aliases[aliasKey(move)] || '';
        el('clip-source').textContent = move ? 'ANM ' + pad(move.id) + ' · ' + anmName(move) + ' · ' +
          '资源 ' + move.asset[0] + ' / 片段 ' + move.asset[2] + ' · 状态 ' + (move.states.join('、') || '—') : '';
        part = find(motion?.anm);
        clip = motion?.anm[2] ?? 0;
      } else {
        el('resource-panel').hidden = false;
        assetRows();
      }
      await drawAsset(request);
    } else if (EVENT_TABS.has(tab)) {
      el('frame-events').hidden = false;
      eventTable();
    } else if (tab === 'overview') {
      el('overview').hidden = false;
      overview();
    } else if (tab === 'bindings') {
      el('bindings').hidden = false;
      await bindings(request);
    } else if (tab === 'other-skills') {
      el('support').hidden = false;
      supportTable();
    } else if (tab === 'skills') {
      el('skills').hidden = false;
      el('skill-media').append(el('scene-media'));
      skillTable();
      const charge = await call('charge');
      if (request !== panelRequest || !active()) return;
      el('charge-normal').value = charge.frames[0];
      el('charge-special').value = charge.frames[1];
      el('charge-reset').disabled = !charge.edited;
      el('charge').hidden = false;
    } else if (tab === 'mix') {
      el('mix').hidden = false;
      el('mix-media').append(el('scene-media'));
      if (mixTabs.value === 'local') drawMix();
      else if (mixTabs.value === 'history') drawMixHistory();
      else updateExchange?.();
    } else if (tab === 'throws') {
      el('throws').hidden = false;
      await drawThrows(request);
    } else if (tab === 'commands') {
      el('command-panel').hidden = false;
      commands();
    } else if (tab === 'hp') {
      const useForms = el('hp-kind').value === 'form';
      const [data,jump] = await Promise.all([call(useForms ? 'defense' : 'attributes', role), call('jump',role)]);
      if (request !== panelRequest || !active()) return;
      hp = data;
      el('hp').hidden = false;
      el('batch').hidden = false;
      el('jump').hidden = false;
      ['jump-launch','jump-gravity'].forEach((field,i) => { el(field).value = Number(jump.values[i].toFixed(6)); });
      properties(el('jump-info'), jump.va.map((va,i) => [i ? '重力表' : '初速度表',hex(va,8) + ' · ' +
        Number(jump.source[i].toFixed(6))]));
      band = options('hp-band', hp.map(item => useForms ? [item.shape,'形态 ' + item.shape] :
        [item.band,item.minimum + '–' + item.maximum + ' HP']), band);
      showBand();
    } else if (tab === 'states') {
      el('states').hidden = false;
      el('state-rows').replaceChildren(...profile.states.map(item => row([
        item.context ? item.state + ' · ' + item.context : item.state, item.anm < 0 ? '—' : pad(item.anm),
        item.next,hex(item.flags >>> 0)], item.anm < 0 ? null : () => selectMove(item.anm, true))));
    } else if (tab === 'report') {
      el('report').hidden = false;
      properties(el('report-info'), [['镜像布局',state.iso.revision],['角色',state.iso.roles.length],
        ['当前角色 ANM',profile.moves.length], ['暂存原生资源',state.edits?.assets || 0],
        ['暂存原生记录',state.edits?.native || 0],['ISO 校验','另存时重新读取 ELF、ERX 与资源并核对写回内容']]);
    } else if (tab === 'music') {
      el('music').hidden = false;
      await musicTable(request);
    }
  }

  async function changeForm(id) {
    if (state.writing || id === shape) return;
    shape = id;
    bindingSlot = id;
    part = null;
    await loadFighter(move?.id, frame);
    remember();
  }

  async function setTab(id) {
    if (state.writing) return;
    const restore = scene && id !== tab && move?.id;
    tab = id;
    if (id === 'appearance' || id === 'assets') {
      page = 0;
      el('search').value = el('kind').value = '';
    }
    if (restore !== false && restore != null) await selectMove(restore);
    else await drawPanel();
    remember();
  }

  function links() {
    for (const [group, style, symbol, tabs] of NAVIGATION) {
      const section = dom('div', '', 'nav-group ' + style);
      section.append(dom('div', group, 'nav-label'));
      for (const [id, title] of tabs) {
        const button = dom('button');
        button.type = 'button';
        button.dataset.fe3Tab = id;
        button.append(dom('b', symbol), document.createTextNode(title));
        button.onclick = run(() => setTab(id));
        section.append(button);
      }
      el('sections').append(section);
    }
    el('feature-search').oninput = () => {
      const query = el('feature-search').value.trim();
      for (const button of navButtons()) button.hidden = !button.textContent.includes(query);
      for (const group of el('sections').querySelectorAll('.nav-group')) {
        group.hidden = !group.querySelector('button:not([hidden])');
      }
    };
  }

  function toggleNav(force) {
    const body = el('editor-body');
    const open = force ?? body.classList.contains('nav-collapsed');
    body.classList.toggle('nav-collapsed', !open);
    el('toggle-nav').textContent = open ? '收起导航' : '展开导航';
    el('toggle-nav').setAttribute('aria-expanded', String(open));
  }

  function transfer(mode) {
    if (!part || !detail) return;
    apply('file', [...partKey(part), mode, palette, checked('clut-only')],
      {restore:'已还原资源',import:'已导入到工程',native:'已导出原生资源',png:'已导出 PNG'}[mode],
      mode === 'restore' || mode === 'import');
  }

  async function refresh() {
    rows = await call('parts');
    part = rows.find(item => same(part, item));
    await loadFighter(move?.id, frame, true);
  }

  return {
    async load(view) {
      role = state.iso.roles.some(item => item.id === view.fe3Role) ? view.fe3Role : 0;
      shape = view.fe3Form ?? 0;
      bindingSlot = shape;
      tab = Object.hasOwn(TABS, view.fe3Tab) ? view.fe3Tab : 'overview';
      combos = Boolean(view.fe3Combos);
      currentOnly = Boolean(view.fe3Current);
      const display = view.fe3Display || {};
      aliases = {...view.fe3Aliases};
      choice = null;
      comboId = throwState = -1;
      inputTab = view.fe3InputTab || 'states';
      throwTab = view.fe3ThrowTab || 'stages';
      opponent = view.fe3Opponent ?? 4;
      el('hp-kind').value = view.fe3HPKind || 'form';
      el('shading').value = display.shading ?? '1';
      el('normals').checked = Boolean(display.normals);
      for (const field of viewFields()) field.checked = display[field.dataset.view] ?? ['texture','root'].includes(field.dataset.view);
      el('native-rate').checked = display.native ?? true;
      el('loop').checked = display.loop ?? true;
      el('speed').value = display.speed || '1';
      el('role-search').value = '';
      el('move-search').value = view.fe3MoveSearch || '';
      el('kind').value = view.fe3Kind || '';
      el('search').value = view.fe3Search || '';
      page = Math.max(0, view.fe3Page ?? 0);
      palette = view.fe3Palette ?? 0;
      clip = view.fe3Clip ?? 0;
      script = view.fe3Script ?? 0;
      band = view.fe3Band ?? 0;
      commandState = view.fe3Command ?? 0;
      eventId = view.fe3Event ?? -1;
      skillId = view.fe3Skill ?? -1;
      mixId = -1;
      mixTabs.select(view.fe3MixPane);
      supportGroup = view.fe3SupportGroup === 'shoulder' ? 'shoulder' : 'guard';
      supportId = view.fe3SupportId;
      supportValue = undefined;
      supportDonor = supportDonorMove = -1;
      musicId = view.fe3Music;
      [rows, rules] = await Promise.all([call('parts'), call('input_rules')]);
      part = find([view.fe3Resource, view.fe3Member]);
      await loadFighter(view.fe3Move, view.fe3Frame, true);
      el('roles').querySelector('.active')?.scrollIntoView({block:'nearest'});
    },
    view() {
      return {fe3Role:role, fe3Form:shape, fe3Tab:tab, fe3Resource:part?.id, fe3Member:part?.member,
        fe3Page:page, fe3Palette:palette, fe3Clip:clip, fe3Script:script, fe3Event:eventId, fe3Band:band,
        fe3Skill:skillId, fe3Move:move?.id, fe3Frame:scene ? motion?.animation.first : frame, fe3Combos:combos, fe3Current:currentOnly,
        fe3MoveSearch:el('move-search').value, fe3Kind:el('kind').value, fe3Search:el('search').value,
        fe3Command:commandState, fe3Music:musicId, fe3Aliases:aliases, fe3InputTab:inputTab, fe3HPKind:el('hp-kind').value,
        fe3ThrowTab:throwTab, fe3Opponent:opponent, fe3MixPane:mixTabs.value,
        fe3SupportGroup:supportGroup, fe3SupportId:supportId,
        fe3Display:{...Object.fromEntries([...viewFields()].map(field => [field.dataset.view, field.checked])),
          native:checked('native-rate'), loop:checked('loop'), speed:el('speed').value, shading:el('shading').value,
          normals:checked('normals')}};
    },
    refresh,
    actor: () => [role, 0, shape],
    previewExchange: id => selectMove(id),
    leave() {
      scene = null;
      stopMovie();
      ++fighterRequest;
      ++compareRequest;
      ++motionRequest;
      ++panelRequest;
      setPlaying(false);
      stopMusic();
      viewport?.clear();
    },
    bind() {
      links();
      el('role-search').oninput = roleList;
      el('move-search').oninput = () => { moveList(); remember(); };
      el('combos').onclick = () => { combos = !combos; moveList(); remember(); };
      el('form').onchange = run(() => changeForm(read('form')));
      el('binding-edit').onsubmit = event => {
        event.preventDefault();
        const words = [...el('binding-fields').querySelectorAll('input')].map(field => Number(field.value.trim()));
        apply('edit_binding', [role,bindingSlot,words], '已应用形态绑定');
      };
      el('binding-restore').onclick = () => apply('edit_binding', [role,bindingSlot,null], '已重置形态绑定');
      el('search').oninput = el('kind').onchange = run(async () => { page = 0; await drawPanel(); remember(); });
      el('prev').onclick = () => { --page; assetRows(); remember(); };
      el('next').onclick = () => { ++page; assetRows(); remember(); };
      el('toggle-nav').onclick = () => toggleNav();
      el('toggle-help').onclick = () => {
        const expanded = el('help').hidden;
        el('help').hidden = !expanded;
        el('toggle-help').textContent = expanded ? '隐藏说明' : '显示说明';
        el('toggle-help').setAttribute('aria-expanded', String(expanded));
      };
      el('shading').onchange = () => {
        if (viewport) { viewport.options.light = read('shading') === 1; viewport.draw(); }
        remember();
      };
      el('normals').onchange = () => {
        if (viewport) { viewport.options.normals = checked('normals'); viewport.draw(); }
        remember();
      };
      for (const field of viewFields()) {
        field.onchange = () => {
          if (viewport) { viewport.options[field.dataset.view] = field.checked; viewport.setFrame(frame); }
          remember();
        };
      }
      for (const id of ['loop','speed']) el(id).onchange = remember;
      el('native-rate').onchange = () => { if (playing) setPlaying(true); remember(); };
      el('play').onclick = togglePlay;
      el('step').onclick = () => seek(frameIndex(frame) + 1);
      el('back').onclick = () => seek(frameIndex(frame) - 1);
      el('camera').onclick = () => viewport?.resetView();
      el('scrub').oninput = () => { setPlaying(false); pose(read('scrub')); };
      el('scrub').onchange = remember;
      el('frame').onchange = () => seek(read('frame'));
      el('current').onclick = () => { currentOnly = true; eventTable(); remember(); };
      el('entire').onclick = () => { currentOnly = false; eventTable(); remember(); };
      el('palette').onchange = run(async () => { palette = read('palette'); await drawTexture(); remember(); });
      el('clip').onchange = run(async () => {
        clip = read('clip');
        const entry = tab === 'animation' && profile.moves.find(item => item.asset[0] === part.id && item.asset[2] === clip);
        if (entry) await selectMove(entry.id);
        else { showClip(); await drawKeys(); }
        remember();
      });
      el('track').onchange = el('channel').onchange = run(async () => { track = read('track'); keyRow = 0; await drawKeys(); });
      el('script').onchange = run(async () => { script = read('script'); eventId = -1; await showScript(); remember(); });
      el('hp-band').onchange = () => { band = read('hp-band'); showBand(); remember(); };
      el('command-source').onchange = () => { commandState = read('command-source'); choice = null; commands(); remember(); };
      el('reset-command').onclick = () => { commandState = 0; choice = null; commands(); remember(); };
      el('filter-form').onchange = el('input-rule').onchange = commands;
      el('input-tabs').onclick = event => {
        const pane = event.target.dataset.pane;
        if (pane && profile) { inputTab = pane; commands(); remember(); }
      };
      el('throw-tabs').onclick = run(async event => {
        const pane = event.target.dataset.pane;
        if (pane && profile) { throwTab = pane; await drawPanel(); remember(); }
      });
      el('throw-opponent').onchange = run(async () => { opponent = read('throw-opponent'); await drawPanel(); remember(); });
      el('throw-edit').onsubmit = event => {
        event.preventDefault();
        apply('edit_throw', [role,throwState,read('throw-default')], '已应用阶段默认动画');
      };
      el('throw-restore').onclick = () => apply('edit_throw', [role,throwState,null], '已重置抓取阶段');
      el('throw-export').onclick = () => apply('export_throws',[role,shape,opponent],'已导出抓取分析',false);
      el('combo-edit').onsubmit = event => {
        event.preventDefault();
        if (!motion?.bom || comboId < 0) return;
        apply('edit_combo', [...motion.bom,move.id,comboId,{frame:read('combo-start'),duration:read('combo-duration'),
          command:read('combo-command'),target:read('combo-target'),selector:read('combo-mask')}], '已应用连段窗口');
      };
      el('combo-restore').onclick = () => {
        if (motion?.bom && comboId >= 0) apply('edit_combo', [...motion.bom,move.id,comboId,null], '已重置连段窗口');
      };
      el('combo-play').onclick = run(() => selectMove(read('combo-target'), true));
      for (const [name, mode] of [['import','import'],['restore','restore'],['export','native'],['export-png','png']]) {
        el(name).onclick = () => transfer(mode);
      }
      el('anm').onsubmit = event => {
        event.preventDefault();
        if (part && detail?.clips) apply('edit_clip', [...partKey(part),clip,...['frames','first','last','rate'].map(read)], '已应用 ANM 参数');
      };
      el('keys').onsubmit = event => {
        event.preventDefault();
        const entry = inputKey();
        if (part && keys[keyRow]) apply('edit_key', [...partKey(part),clip,track,read('channel'),keyRow,entry.frame,entry.value],
          '已应用关键帧');
      };
      el('add-key').onclick = () => {
        if (!part || !keys.length || !el('keys').reportValidity()) return;
        const entry = inputKey();
        if (keys.some(key => key.frame === entry.frame)) {
          message('请输入当前轨道尚未使用的帧。', true);
          return;
        }
        const values = [...keys, entry].sort((a,b) => a.frame - b.frame);
        keyRow = values.indexOf(entry);
        apply('edit_key_rows', [...partKey(part),clip,track,read('channel'),values], '已添加关键帧');
      };
      el('delete-key').onclick = () => {
        if (!part || !keys[keyRow] || keys.length <= 1) return;
        apply('edit_key_rows', [...partKey(part),clip,track,read('channel'),keys.filter((_,i) => i !== keyRow)], '已删除关键帧');
      };
      for (const mode of ['export','import']) el(mode + '-clip').onclick = () => {
        if (part && detail?.clips) apply('clip_file', [...partKey(part),clip,mode],
          mode === 'export' ? '已导出单个 ANM' : '已替换当前 ANM', mode === 'import');
      };
      el('restore-bank').onclick = () => transfer('restore');
      el('save-alias').onclick = run(async () => {
        if (!move) return;
        const name = el('alias').value.trim();
        if (name) aliases[aliasKey(move)] = name;
        else delete aliases[aliasKey(move)];
        moveList();
        await drawPanel();
        remember();
      });
      el('compare-play').onclick = run(compare);
      el('compare-close').onclick = () => { ++compareRequest; viewport?.clearOpponent(); };
      el('event').onsubmit = event => {
        event.preventDefault();
        const source = eventSource();
        if (el('event').hidden || !source.key) return;
        const params = shownEvent.params.map(item => item.value);
        for (const field of el('event-params').querySelectorAll('input[data-param]')) {
          params[Number(field.dataset.param)] = field.value === '' ? null : Number(field.value);
        }
        apply('edit_event_record', [...source.key,source.script,eventId,
          {frame:read('event-frame'),selector:read('event-selector'),params}], '已应用帧事件');
      };
      el('event-restore').onclick = () => {
        const source = eventSource();
        if (!el('event').hidden && source.key) apply('edit_event_record', [...source.key,source.script,eventId,null], '已重置帧事件');
      };
      el('hp').onsubmit = event => {
        event.preventDefault();
        if (hp) apply(el('hp-kind').value === 'form' ? 'edit_defense' : 'edit_attributes',
          [role,band,read('hp-value')], '已应用承伤倍率');
      };
      el('hp-kind').onchange = run(async () => { band = 0; await drawPanel(); remember(); });
      el('hp-value').oninput = () => { el('hp-summary').value = read('hp-value') ? (32768 / read('hp-value')).toFixed(2) : ''; };
      el('hp-summary').oninput = () => { if (read('hp-summary') > 0) el('hp-value').value = Math.round(32768 / read('hp-summary')); };
      el('hp-restore').onclick = () => apply(el('hp-kind').value === 'form' ? 'edit_defense' : 'edit_attributes',
        [role,band,null], '已恢复承伤倍率');
      el('jump').onsubmit = event => {
        event.preventDefault();
        apply('edit_jump', [role,['jump-launch','jump-gravity'].map(read)], '已应用跳跃 / 重力');
      };
      el('jump-restore').onclick = () => apply('edit_jump', [role,null], '已恢复原生跳跃参数');
      el('charge').onsubmit = event => {
        event.preventDefault();
        return apply('edit_charge', [[el('charge-normal').valueAsNumber, el('charge-special').valueAsNumber]], '已应用必杀前蓄力时间');
      };
      el('charge-reset').onclick = () => apply('edit_charge',[null],'已恢复镜像蓄力时间');
      el('batch-actions').onclick = run(async event => {
        const mode = event.target.dataset.batch;
        if (!mode) return;
        const args = [role,shape,mode,el('batch-range').value,read('batch-' + (mode === 'inspect' ? 'damage' : mode))];
        if (mode !== 'inspect') return apply('batch', args, '已应用批量修改');
        const result = await call('batch', ...args);
        properties(el('batch-results'), [['匹配的伤害字段',result.rows.length], ...result.rows.map(
          ([id, at, base, changed, name]) => ['ANM ' + pad(id) + ' · 帧 ' + at + ' · ' + name,base + ' → ' + changed])]);
      });
      el('support-tabs').onclick = event => {
        const group = event.target.dataset.supportGroup;
        if (!group || group === supportGroup || !profile) return;
        supportGroup = group;
        supportId = supportValue = undefined;
        supportTable();
        remember();
      };
      el('support-edit').onsubmit = event => {
        event.preventDefault();
        if (el('support-apply').disabled) return;
        const value = read('support-effect');
        supportValue = undefined;
        return apply('edit_support_skill',[role,supportId,value],'已应用技能效果');
      };
      el('support-restore').onclick = () => {
        supportValue = undefined;
        return apply('edit_skill', [role,supportId,null], '已恢复镜像技能效果');
      };
      el('support-mix').ontoggle = run(async () => {
        if (el('support-mix').open && !el('support-mix').hidden && tab === 'other-skills') await listSupportSources();
        else ++supportRequest;
      });
      el('support-donor').onchange = run(async () => {
        supportDonor = read('support-donor');
        supportDonorMove = -1;
        await listSupportSources();
      });
      el('support-mix-mode').onchange = run(listSupportSources);
      el('support-donor-move').onchange = () => {
        supportDonorMove = read('support-donor-move');
        supportMixStatus();
      };
      el('support-mix-apply').onclick = () => {
        if (el('support-mix-apply').disabled) return;
        return addingSupport()
          ? apply('add_support_skill',[role,shape,supportDonor,supportDonorMove],'已新增技能候选')
          : apply('transfer_support_skill',[role,supportId,supportDonor,supportDonorMove],'已应用防御与辅助配招');
      };
      el('support-mix-restore').onclick = () => selectedSupport()?.added
        ? apply('remove_support_skill',[role,supportId],'已移除新增技能')
        : apply('transfer_support_skill',[role,supportId,null,null],'已还原防御与辅助配招');
      el('skill-edit').onsubmit = event => {
        event.preventDefault();
        const chosen = profile.skills.find(item => item.id === skillId);
        if (!chosen) return;
        const values = [...chosen.words];
        for (const field of el('skill-fields').querySelectorAll('input')) {
          if (field.readOnly) continue;
          const index = Number(field.dataset.word);
          if (field.dataset.byte !== undefined) {
            const shift = 8 * Number(field.dataset.byte);
            if (field.checked !== Boolean((values[index] >>> shift) & 255)) {
              values[index] = ((values[index] & ~(255 << shift)) | (Number(field.checked) << shift)) >>> 0;
            }
          } else if (field.dataset.float) {
            const raw = new DataView(new ArrayBuffer(4));
            raw.setFloat32(0, Number(field.value), true);
            values[index] = raw.getUint32(0, true);
          } else {
            values[index] = Number(field.value);
          }
        }
        apply('edit_skill',[role,skillId,values],'已应用技能记录');
      };
      el('skill-restore').onclick = () => apply('edit_skill', [role,skillId,null], '已重置技能记录');
      el('skill-donor').onchange = run(async () => {
        donorRole = read('skill-donor');
        donorSkill = undefined;
        await donorChoices();
      });
      el('skill-donor-move').onchange = () => { donorSkill = read('skill-donor-move'); };
      el('skill-opponent').onchange = () => { opponent = read('skill-opponent'); remember(); };
      el('skill-stop').onclick = run(async () => { if (scene && move) await selectMove(move.id); });
      el('show-ipu').onchange = refreshMovie;
      el('effect-video').onloadeddata = el('effect-video').onseeked = refreshMovie;
      const showFinisherMix = () => { mixTabs.select('local'); return setTab('mix'); };
      el('skill-add').onclick = run(() => { el('mix-mode').value = 'add'; return showFinisherMix(); });
      el('skill-mix').onclick = run(showFinisherMix);
      el('mix-mode').onchange = drawMix;
      el('mix-form').onchange = run(() => changeForm(read('mix-form')));
      el('mix-target-move').onchange = () => { mixId = read('mix-target-move'); drawMix(); };
      el('mix-opponent').onchange = () => { opponent = read('mix-opponent'); remember(); };
      el('mix-edit').onsubmit = event => {
        event.preventDefault();
        if (el('mix-apply').disabled) return;
        if (el('mix-mode').value === 'replace') {
          return apply('transfer_finisher',[role,read('mix-target-move'),donorRole,donorSkill],'已应用必杀配招');
        }
        return apply('add_finisher',[role,read('mix-form'),donorRole,donorSkill],'已新增到必杀技表');
      };
      el('mix-copy').onclick = () => apply('copy_finisher_settings',
        [role,read('mix-target-move'),donorRole,donorSkill],'已复制必杀参数');
      el('mix-preview').onclick = run(async () => {
        if (el('mix-mode').value === 'add' && read('mix-form') !== shape) await changeForm(read('mix-form'));
        await playBattle('skill',-1,opponent,donorRole,donorSkill);
      });
      el('mix-stop').onclick = el('skill-stop').onclick;
      el('music-play').onclick = run(playMusic);
      el('music-pause').onclick = run(async () => {
        const audio = el('audio');
        if (!audio.getAttribute('src')) return;
        if (audio.paused) await audio.play();
        else audio.pause();
      });
      el('music-stop').onclick = stopMusic;
      el('music-loop').onchange = () => { el('audio').loop = checked('music-loop'); };
      el('music-export').onclick = () => apply('export_music',[read('music-track')],'已导出 WAV',false);
      document.addEventListener('keydown', event => {
        if (!active() || event.altKey) return;
        if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
          event.preventDefault();
          toggleNav(true);
          el('feature-search').focus();
          return;
        }
        if (event.ctrlKey || event.metaKey || event.target.matches('input, textarea, select, button, audio')) return;
        if (event.key === ' ') { event.preventDefault(); togglePlay(); }
        if (event.key === 'ArrowLeft') seek(Math.floor(frame) - 1);
        if (event.key === 'ArrowRight') seek(Math.floor(frame) + 1);
      });
    },
  };
}
