export function makeAIPanel({$, state, dom, hex, read, write, rows}) {
  let profile, loadedEdits, loadedKey, selected, revision = 0;
  const difficulty = () => Number($('ai-level').value);
  const key = () => [state.iso?.id, state.role, difficulty()].join('/');
  const ready = () => state.tab === 'ai' && !state.writing && loadedKey === key();
  const selectedRow = () => profile?.rows.find(row => row.address === selected);
  const apply = values => {
    if (ready()) return write('edit_ai', [state.role, difficulty(), values], 'AI 已应用到工程', false);
  };

  function renderDetail() {
    const row = selectedRow();
    $('ai-form').hidden = !row;
    if (!row) return;
    $('ai-rule').textContent = row.label + ' · ' + hex(row.address, 8);
    $('ai-original').textContent = '原生指令 ' + row.raw + (row.fields.length ?
      ' · 原值 ' + row.fields.map(f => f.relation ? profile.relations[f.original] : f.original).join(' / ') : '');
    const controls = row.fields.map(field => {
      const label = dom('label', field.label);
      const input = dom(field.relation ? 'select' : 'input');
      input.name = String(field.site);
      if (field.relation) {
        profile.relations.forEach((text, index) => input.add(new Option(text, index)));
      } else {
        Object.assign(input, {type:'number', min:field.min, max:field.max, step:'1', required:true});
      }
      input.value = field.value;
      label.append(input);
      return label;
    });
    if (row.chance) {
      const label = dom('label', '通过概率 %');
      const input = dom('input');
      Object.assign(input, {name:'percent', type:'number', min:0, max:100, step:'any',
        placeholder:row.chance.percent.toFixed(2)});
      label.append(input);
      controls.push(label);
    }
    $('ai-controls').replaceChildren(...controls);
    $('ai-hint').textContent = row.chance ?
      '填写百分比可直接换算本条随机条件。通过率以脚本执行到此分支为前提。' :
      row.fields.length ? '点击应用后保存到当前角色与难度。' : '此指令供对照脚本流程。';
    $('ai-apply').hidden = !row.fields.length;
    $('ai-reset').hidden = !row.edited;
  }

  function renderRows() {
    if (!profile) return;
    const group = Number($('ai-group').value);
    const kind = $('ai-kind').value;
    const term = $('ai-search').value.trim().toLowerCase();
    const matching = profile.rows.filter(row => (group < 0 || row.group === group) &&
      (kind === 'all' || row.kind === kind) &&
      (!term || [row.label, row.next, hex(row.address, 8), row.raw, ...row.args]
        .join(' ').toLowerCase().includes(term)));
    if (!matching.some(row => row.address === selected)) selected = undefined;
    $('ai-count').textContent = matching.length.toLocaleString() + ' 条指令';
    rows('ai-rows', matching.map(row => {
      const comparison = row.fields.find(field => field.relation);
      const args = (comparison ? profile.relations[comparison.value] + ' · ' : '') + row.args.join(', ');
      const value = row.chance ? row.chance.percent.toFixed(2) + '%' : args;
      return {id:row.address, cells:[profile.groups[row.group],
        row.label + (row.edited ? ' · 已修改' : ''), value, row.next || '—', hex(row.address, 8)]};
    }), 'ai', address => {
      selected = address;
      for (const row of $('ai-rows').rows) row.classList.toggle('active', Number(row.dataset.ai) === address);
      renderDetail();
    }, selected);
    renderDetail();
  }

  async function render() {
    const wanted = key();
    const edits = state.edits;
    if (wanted === loadedKey && edits === loadedEdits) return;
    const ticket = ++revision;
    if (loadedKey !== wanted) selected = undefined;
    $('ai-near-apply').disabled = true;
    $('ai-near-reset').disabled = true;
    $('ai-form').hidden = true;
    $('ai-rows').replaceChildren();
    $('ai-count').textContent = '正在读取脚本';
    const data = await read(state.role, difficulty(), -1);
    if (ticket !== revision || wanted !== key() || state.tab !== 'ai') return;
    loadedEdits = edits;
    loadedKey = wanted;
    profile = data;
    const previous = $('ai-group').value || '-1';
    $('ai-group').replaceChildren(new Option('全部脚本', -1),
      ...data.groups.map((name, i) => new Option(name + ' · ' + data.counts[i], i)));
    $('ai-group').value = previous;
    $('ai-near').value = data.near.value;
    $('ai-near-apply').disabled = false;
    $('ai-near-reset').disabled = data.near.value === data.near.original;
    renderRows();
  }

  return {
    render,
    reset() { ++revision; loadedKey = undefined; profile = undefined; selected = undefined; },
    bind(onError) {
      $('ai-level').addEventListener('change', () => render().catch(onError));
      for (const id of ['ai-group', 'ai-kind']) $(id).addEventListener('change', renderRows);
      $('ai-search').addEventListener('input', renderRows);
      $('ai-distance-form').addEventListener('submit', event => {
        event.preventDefault();
        if (profile) apply({[profile.near.site]:Number($('ai-near').value)});
      });
      $('ai-near-reset').addEventListener('click', () => {
        if (profile) apply({[profile.near.site]:null});
      });
      $('ai-form').addEventListener('submit', event => {
        event.preventDefault();
        const row = selectedRow();
        if (!row?.fields.length) return;
        const input = new FormData(event.currentTarget);
        const values = Object.fromEntries(row.fields.map(field => [field.site, Number(input.get(field.site))]));
        const percent = input.get('percent');
        if (row.chance && percent !== '') {
          values[row.chance.relation] = 2;
          values[row.chance.threshold] = Math.round(Number(percent) / 100 * (row.chance.bound + 1));
        }
        apply(values);
      });
      $('ai-reset').addEventListener('click', () => {
        const row = selectedRow();
        if (row?.fields.length) apply(Object.fromEntries(row.fields.map(field => [field.site, null])));
      });
    },
  };
}
