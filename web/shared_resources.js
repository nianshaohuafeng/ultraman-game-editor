const PAGE_SIZE = 60;
const keyOf = row => [row.id, row.member];
const same = (a, b) => a?.id === b?.id && a?.member === b?.member;

export function makeSharedResources(host, {template, api, write, message, state, dom, properties}) {
  host.replaceChildren(template.content.cloneNode(true));
  const field = name => host.querySelector('[data-' + name + ']');
  const audio = field('player');
  const call = (method, ...args) => api(method, serial, ...args);
  let serial, edits, active = false, rows = [], selected, page = 0;
  let listRequest = 0, detailRequest = 0, audioRequest = 0, decoding = false;

  function stop() {
    ++audioRequest;
    if (decoding) call('stop_music').catch(() => {});
    decoding = false;
    audio.pause();
    audio.removeAttribute('src');
    audio.hidden = true;
    field('play').disabled = false;
  }

  function drawRows() {
    const filter = field('query').value.trim().toLowerCase().split(/\s+/).filter(Boolean);
    const music = field('kind').value === 'bgm';
    const items = rows.filter(row => (row.kind === 'bgm') === music &&
      filter.every(word => [row.name, row.location, String(row.id), row.kind].join(' ').toLowerCase().includes(word)));
    const last = Math.max(0, Math.ceil(items.length / PAGE_SIZE) - 1);
    page = Math.min(page, last);
    field('count').textContent = items.length.toLocaleString() + ' 项 · ' + (page + 1) + ' / ' + (last + 1);
    field('prev').disabled = page === 0;
    field('next').disabled = page >= last;
    field('rows').replaceChildren(...items.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE).map(row => {
      const line = dom('tr', '', same(row, selected) ? 'active' : '');
      const button = dom('button', row.location, 'resource-link');
      button.type = 'button';
      line.onclick = () => {
        if (state.writing || same(row, selected)) return;
        stop();
        selected = row;
        field('palette').replaceChildren();
        drawRows();
        drawDetails();
      };
      line.append(dom('td'), dom('td', row.name + (row.edited ? ' · 已修改' : '')), dom('td', row.kind.toUpperCase()));
      line.firstChild.append(button);
      line.title = row.name;
      return line;
    }));
  }

  async function showTexture(request) {
    const uri = await call('shared_texture', keyOf(selected), Number(field('palette').value || 0));
    if (!active || request !== detailRequest) return;
    field('image').src = uri;
    field('image-box').hidden = false;
  }

  async function drawDetails() {
    const request = ++detailRequest;
    field('detail').hidden = true;
    field('image-box').hidden = true;
    if (!selected) return;
    const key = keyOf(selected);
    try {
      const data = await call('shared_details', key);
      if (!active || request !== detailRequest) return;
      const music = data.kind === 'bgm';
      field('title').textContent = data.name + ' · ' + data.location;
      const info = [['格式', data.format], ['大小', data.size.toLocaleString() + ' B']];
      if (music) info.push(['时长', data.seconds.toFixed(2) + ' 秒']);
      else info.push(['尺寸', data.width + ' × ' + data.height]);
      properties(field('info'), info);
      field('audio-tools').hidden = !music;
      if (music) {
        const prior = field('donor').value;
        const options = rows.filter(row => row.kind === 'bgm' && !same(row, selected));
        field('donor').replaceChildren(new Option('请选择来源曲目', ''),
          ...options.map(row => new Option(row.name + ' · ' + row.location, JSON.stringify(keyOf(row)))));
        if (options.some(row => JSON.stringify(keyOf(row)) === prior)) field('donor').value = prior;
        field('copy').disabled = !field('donor').value;
      }
      const palette = Number(field('palette').value || 0);
      field('palette').replaceChildren(...Array.from({length: data.palette_count || 0},
        (_, i) => new Option('色表 ' + i, i)));
      if (palette < data.palette_count) field('palette').value = String(palette);
      field('palette-field').hidden = data.kind !== 'pom';
      field('native').textContent = '导出 ' + data.native.slice(1).toUpperCase();
      field('png').hidden = data.kind !== 'pom';
      field('recolor').hidden = data.kind !== 'pom';
      field('restore').disabled = !data.edited;
      field('detail').hidden = false;
      if (!music) await showTexture(request);
    } catch (error) {
      if (active && request === detailRequest) message(error.message, true);
    }
  }

  async function refresh() {
    const request = ++listRequest;
    try {
      const data = await call('list_shared_resources');
      if (!active || request !== listRequest) return;
      rows = data;
      if (selected) selected = rows.find(row => same(row, selected));
      drawRows();
      drawDetails();
    } catch (error) {
      if (active && request === listRequest) {
        edits = undefined;
        message(error.message, true);
      }
    }
  }

  function changeList(clearSelection) {
    page = 0;
    if (clearSelection) {
      stop();
      selected = undefined;
      field('palette').replaceChildren();
      drawDetails();
    }
    drawRows();
  }

  function sync(visible) {
    host.hidden = !visible;
    if (!visible) {
      if (active) stop();
      active = false;
      edits = undefined;
      ++detailRequest;
      ++listRequest;
      return;
    }
    active = true;
    if (serial !== state.iso.id) {
      serial = state.iso.id;
      edits = undefined;
      rows = [];
      field('query').value = '';
      changeList(true);
    }
    if (edits === state.edits) return;
    edits = state.edits;
    refresh();
  }

  const file = (mode, label) => async () => {
    if (!selected || state.writing) return;
    if (mode === 'import' || mode === 'restore') stop();
    if (await write('shared_file', [keyOf(selected), mode, Number(field('palette').value || 0)], label, false)) {
      sync(true);
    }
  };

  field('kind').onchange = () => changeList(true);
  field('query').oninput = () => changeList(false);
  field('prev').onclick = () => { --page; drawRows(); };
  field('next').onclick = () => { ++page; drawRows(); };
  field('palette').onchange = () => {
    const request = ++detailRequest;
    showTexture(request).catch(error => {
      if (active && request === detailRequest) message(error.message, true);
    });
  };
  field('donor').onchange = () => { field('copy').disabled = !field('donor').value; };
  field('copy').onclick = async () => {
    if (!selected || !field('donor').value || state.writing) return;
    stop();
    if (await write('shared_copy_track', [keyOf(selected), JSON.parse(field('donor').value)],
      '已替换 BGM', false)) sync(true);
  };
  field('import').onclick = file('import', '已导入资源');
  field('recolor').onclick = file('palette', '已更新所选色表');
  field('native').onclick = file('native', '已导出资源');
  field('png').onclick = file('png', '已导出 PNG');
  field('wav').onclick = file('wav', '已导出 WAV');
  field('restore').onclick = file('restore', '已还原资源');
  field('play').onclick = async () => {
    if (!selected || state.writing) return;
    stop();
    const request = ++audioRequest;
    field('play').disabled = decoding = true;
    try {
      const src = await call('shared_listen', keyOf(selected));
      if (src && active && request === audioRequest) {
        audio.src = src;
        audio.hidden = false;
        await audio.play();
      }
    } catch (error) {
      if (active && request === audioRequest) message(error.message, true);
    } finally {
      if (request === audioRequest) field('play').disabled = decoding = false;
    }
  };
  field('stop').onclick = stop;
  return {sync};
}
