export function makeMixTabs(host, onChange) {
  const buttons = [...host.querySelectorAll('[data-mix-tab]')];
  const panels = host.querySelectorAll('[data-mix-pane]');
  let value;
  function select(name) {
    value = buttons.some(button => button.dataset.mixTab === name) ? name : 'local';
    for (const button of buttons) {
      const selected = button.dataset.mixTab === value;
      button.classList.toggle('active', selected);
      button.setAttribute('aria-pressed', String(selected));
    }
    for (const panel of panels) panel.hidden = panel.dataset.mixPane !== value;
  }
  for (const button of buttons) button.addEventListener('click', () => {
    if (button.dataset.mixTab === value) return;
    select(button.dataset.mixTab);
    onChange(value);
  });
  select('local');
  return {select, get value() { return value; }};
}

export function makeExchange(host, {template, fe3, current, api, write, preview, message}) {
  host.replaceChildren(template.content.cloneNode(true));
  const field = name => host.querySelector('[data-' + name + ']');
  const fields = ['role', 'config', 'shape', 'origin', 'receiver'];
  const selection = name => field(name).value === '' ? null : JSON.parse(field(name).value);
  const options = (name, rows, choice = selection(name)) => {
    const wanted = JSON.stringify(choice);
    field(name).replaceChildren(...rows.map(row => {
      const option = new Option(row.name, JSON.stringify(row.id));
      if (row.paths?.length > 1) option.title = row.paths.join('\n');
      if (row.has_hits === false) option.dataset.poseOnly = 'true';
      return option;
    }));
    if (rows.some(row => JSON.stringify(row.id) === wanted)) field(name).value = wanted;
    field(name).disabled = rows.length === 0;
  };
  let donor = null, layouts = [], pending = false, revision = 0, target = '', preferred;
  let bulk, overrides = {};
  function selectedBulkRow() {
    return bulk?.target.find(r => r.id === field('set-target').value);
  }
  function showBulkRow() {
    const row = selectedBulkRow();
    if (!row) return;
    const edited = Object.hasOwn(overrides,row.id);
    field('set-source').value = (edited ? overrides[row.id] : row.source) ?? '';
    field('set-source').disabled = pending || row.locked;
    field('set-reason').textContent = edited ? '手动指定 · 原匹配依据为' + row.reason : row.reason;
  }
  field('set-target').onchange = showBulkRow;
  field('set-source').onchange = () => {
    const row = selectedBulkRow();
    if (!row || row.locked) return;
    const source = field('set-source').value || null;
    if (source === row.source) delete overrides[row.id];
    else overrides[row.id] = source;
    showBulkRow();
  };
  const audio = new Audio();
  let audition = 0, voiceRequest = 0, voiceFile;
  function voiceOptions(voices = {}) {
    const select = field('voice'), previous = select.value;
    select.replaceChildren(new Option('跟随来源动作','source'),new Option('保留当前战吼','target'),new Option('静音战吼','mute'));
    for (const [side, label] of [['source','来源角色'],['target','目标角色']]) {
      const group = document.createElement('optgroup');
      group.label = label;
      for (const cue of voices[side] || []) group.append(new Option(label + ' · 战吼 ' + cue, JSON.stringify({side,cue})));
      if (group.children.length) select.append(group);
    }
    if (voiceFile) {
      const saved = new Option(voiceFile.name, JSON.stringify({side:'file',token:voiceFile.token}));
      saved.dataset.recording = 'true';
      select.append(saved);
    }
    select.value = [...select.querySelectorAll('option')].some(o => o.value === previous) ? previous : 'source';
  }
  function cancelAudio() { ++audition; audio.pause(); audio.removeAttribute('src'); }
  function releaseFile(file) {
    if (file) api('release_move_voice',file.serial,file.token).catch(() => {});
  }
  async function refreshVoices() {
    if (!donor || ['model','all'].includes(field('mode').value) || selection('shape') === null) return;
    const id = current(), view = revision, request = ++voiceRequest;
    try {
      const voices = await api('move_voice_choices',id.serial,id.actor,[selection('role'),selection('config')]);
      if (request !== voiceRequest || view !== revision || id.serial !== current().serial) return;
      const previous = field('voice').value;
      voiceOptions(voices);
      if (field('voice').value !== previous) cancelAudio();
      controls();
    } catch (error) { if (view === revision && request === voiceRequest) message(error.message,true); }
  }
  voiceOptions();
  field('wav').onclick = async () => {
    const id = current(), request = revision;
    try {
      const picked = await api('pick_move_voice',id.serial,id.actor);
      if (!picked) return;
      const file = {...picked,serial:id.serial};
      if (request !== revision || id.serial !== current().serial) {
        releaseFile(file);
        return;
      }
      cancelAudio();
      releaseFile(voiceFile);
      voiceFile = file;
      const select = field('voice');
      select.querySelector('[data-recording]')?.remove();
      const choice = new Option(picked.name,JSON.stringify({side:'file',token:picked.token}));
      choice.dataset.recording = 'true';
      select.append(choice); select.value = choice.value; controls();
    } catch(error) { if (request === revision) message(error.message,true); }
  };
  field('voice').onchange = () => { cancelAudio(); controls(); };
  field('listen').onclick = async () => {
    cancelAudio();
    const token = audition, id = current();
    try {
      const choice = JSON.parse(field('voice').value);
      const uri = await api('listen_move_voice',id.serial,id.actor,[selection('role'),selection('config')],choice);
      if (token === audition && id.serial === current().serial) { audio.src = uri; await audio.play(); }
    } catch (error) { if (token === audition) message(error.message,true); }
  };
  if (fe3) field('mode').querySelector('[value="model"]').remove();

  function controls() {
    const mode = field('mode').value;
    const model = mode === 'model';
    const whole = mode === 'all';
    const usable = donor && (!model || donor.fe3);
    field('source').textContent = donor?.name || '请选择来源 ISO';
    field('origin-field').hidden = field('receiver-field').hidden = model || whole;
    field('voice-panel').hidden = model || whole || !usable;
    field('set-panel').hidden = !whole || !bulk;
    field('set-target').disabled = pending;
    if (whole && bulk) showBulkRow();
    field('voice').disabled = pending || !usable;
    field('wav').disabled = pending || !usable;
    field('listen').disabled = pending || !field('voice').value.startsWith('{');
    const origin = Array.from(field('origin').options).find(option => option.value === field('origin').value);
    const routes = mode === 'hit' && origin?.title ? origin.title.split('\n').slice(1) : [];
    field('routes').hidden = !routes.length;
    field('route-text').textContent = routes.join('\n');
    const instruction = {
      all: bulk ? ('覆盖目标角色各出场配置的 ' + bulk.target.length + ' 个动画片段，其中 ' +
        bulk.target.filter(r => r.reason === '姿势近似').length + ' 项按姿势近似匹配。' +
        (bulk.target.some(r => r.locked) ? '共用或配招占用的 ' + bulk.target.filter(r => r.locked).length + ' 项保留。' : '') +
        '目标 BOM 中的判定、声音和特效帧随新时长对齐。') :
        '选择来源角色和形态以生成动作对照。',
      model: donor && !donor.fe3 ? '模型来源请选择奥3 ISO。' : '目标为左侧当前奥0形态。转换后的 MDL 和 POM 随工程一起保存。',
      skill: '移植动作、声音和直接攻击判定。演出回调由目标游戏执行。',
      motion: '同步来源的姿势、根位移、时长、变速和声音。',
      hit: '同步来源的动作、声音和攻击判定。接续输入沿用目标动作。'
    };
    field('note').textContent = instruction[mode];
    if (!whole && !model && donor && !pending && selection('shape') !== null) {
      if (selection('receiver') === null) field('note').textContent = '当前形态没有可替换的同类动作。';
      else if (selection('origin') === null) field('note').textContent = '来源形态没有可读取的同类动作。';
      else if (mode === 'hit' && (origin?.dataset.poseOnly ||
        field('receiver').selectedOptions?.[0]?.dataset.poseOnly)) {
        field('note').textContent = '目标动作将使用来源片段的直接攻击窗口。';
      }
    }
    field('open').disabled = field('current').disabled = pending;
    for (const name of fields) field(name).disabled = pending || !usable || !field(name).options.length;
    field('apply').disabled = pending || !usable || selection('shape') === null ||
      (whole ? !bulk : !model && (selection('origin') === null || selection('receiver') === null));
    field('apply').textContent = whole ? (pending ? '正在处理整套动作…' : '一键替换全部动作') : '应用到工程';
    field('apply-voice').disabled = model || whole || field('apply').disabled;
    field('mode').disabled = pending;
  }

  async function listMoves() {
    const mode = field('mode').value;
    const request = ++revision;
    cancelAudio();
    bulk = null;
    overrides = {};
    if (!donor || mode === 'model' || selection('shape') === null) {
      options('origin', []);
      options('receiver', []);
      pending = false;
      controls();
      return;
    }
    pending = true;
    controls();
    const id = current();
    try {
      if (mode === 'all') {
        const plan = await api('motion_set_choices',id.serial,id.actor,
          [selection('role'),selection('config'),selection('shape')]);
        if (request !== revision || id.serial !== current().serial) return;
        bulk = plan;
        field('set-target').replaceChildren(...plan.target.map(r => new Option(r.name,r.id)));
        field('set-source').replaceChildren(new Option('保留目标片段',''),
          ...plan.source.map(r => new Option(r.name,r.id)));
        return;
      }
      const moves = await api('move_exchange_choices', id.serial, id.actor,
        [selection('role'), selection('config'), selection('shape')], mode);
      if (request !== revision) return;
      voiceOptions(moves.voices);
      options('origin', moves.donor);
      options('receiver', moves.receiver, preferred ?? selection('receiver'));
      preferred = undefined;
    } catch (error) {
      if (request === revision) {
        options('origin', []);
        options('receiver', []);
        message(error.message, true);
      }
    } finally {
      if (request === revision) {
        pending = false;
        controls();
      }
    }
  }

  function listForms() {
    const config = layouts.find(row => row.id === selection('config'));
    options('shape', config?.forms || []);
    return listMoves();
  }

  async function listConfigs() {
    const request = ++revision;
    pending = true;
    for (const name of ['config', 'shape', 'origin']) options(name, []);
    controls();
    try {
      const forms = await api('move_source_forms', current().serial, selection('role'));
      if (request !== revision) return;
      layouts = forms;
      options('config', layouts, 0);
      await listForms();
    } catch (error) {
      if (request === revision) {
        pending = false;
        for (const name of fields.slice(1)) options(name, []);
        controls();
        message(error.message, true);
      }
    }
  }

  async function open(useCurrent) {
    if (pending) return;
    const request = ++revision;
    pending = true;
    controls();
    try {
      const result = await api('open_move_source', current().serial, useCurrent);
      if (!result || request !== revision) return;
      donor = result;
      options('role', donor.roles, current().actor[0]);
      await listConfigs();
    } catch (error) {
      if (request === revision) message(error.message, true);
    } finally {
      if (request === revision) {
        pending = false;
        controls();
      }
    }
  }

  field('open').onclick = () => open(false);
  field('current').onclick = () => open(true);
  field('config').onchange = listForms;
  field('shape').onchange = field('mode').onchange = listMoves;
  field('role').onchange = listConfigs;
  field('origin').onchange = field('receiver').onchange = controls;
  async function apply(voiceOnly) {
    if (field(voiceOnly ? 'apply-voice' : 'apply').disabled) return;
    const mode = field('mode').value;
    const id = current();
    const request = ++revision;
    const receiver = [...id.actor, selection('receiver')];
    const source = [selection('role'), selection('config'), selection('shape'), selection('origin')];
    if (mode === 'model') {
      receiver.pop();
      source.pop();
    }
    pending = true;
    controls();
    try {
      if (mode === 'all') {
        message('正在转换整套动作并校验动画库…');
        const result = await write('apply_motion_set',[id.actor,source.slice(0,3),overrides],'整套动作已处理');
        if (result && id.serial === current().serial && request === revision) await listMoves();
        return;
      }
      const voice = field('voice').value;
      const args = mode === 'model' ? [receiver,source,mode] : [receiver,source,mode,voice.startsWith('{') ? JSON.parse(voice) : voice];
      if (voiceOnly) args.push(true);
      const result = await write('exchange_move', args, voiceOnly ? '已处理战吼' : mode === 'model' ? '已转换模型与贴图' : '已应用配招');
      if (result && mode !== 'model' && request === revision && id.serial === current().serial && String(id.actor) === String(current().actor)) {
        await refreshVoices();
        if (!voiceOnly && !result.voiceOnly && request === revision) await preview(result.animation);
      }
    } catch (error) {
      if (request === revision) message(error.message, true);
    } finally {
      if (request === revision) {
        pending = false;
        controls();
      }
    }
  }
  host.querySelector('form').onsubmit = event => { event.preventDefault(); return apply(false); };
  field('apply-voice').onclick = () => apply(true);
  controls();
  return {
    refreshVoices,
    sync() {
      const id = current();
      const key = [id.serial, ...id.actor].join(':');
      if (key === target) return;
      target = key;
      if (donor && selection('shape') !== null) listMoves();
    },
    reset() {
      ++revision;
      cancelAudio();
      releaseFile(voiceFile);
      voiceFile = null;
      voiceOptions();
      target = '';
      donor = null;
      bulk = null;
      overrides = {};
      layouts = [];
      pending = false;
      preferred = undefined;
      for (const name of fields) options(name, []);
      controls();
    },
    showModel() {
      field('mode').value = 'model';
      listMoves();
      field('open').focus();
    },
    showAttacks(move) {
      if (pending) return;
      preferred = move;
      field('mode').value = 'hit';
      host.scrollIntoView({block: 'nearest'});
      return open(true);
    }
  };
}
