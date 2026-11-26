import {activeAt} from './player.js';

export function setHidden(element, hidden) {
  if (element.hidden !== hidden) element.hidden = hidden;
}

function setText(element, text) {
  if (element.textContent !== text) element.textContent = text;
}

/** Update the visible rows without replacing a selected event's edit form. */
export function updateEventList(rows, {frame, currentOnly, selectedId, timeline = true,
  counter, empty, showCount, emptyText}) {
  let count = 0, record;
  for (const {item, tr, idleStatus = '—'} of rows) {
    const active = timeline && activeAt(item, frame);
    setHidden(tr, currentOnly && !active);
    if (tr.hidden) continue;
    setText(tr.cells[4], active ? (item.frame === frame ? '触发' : '生效') : idleStatus);
    const selected = item.id === selectedId;
    if (tr.classList.contains('active') !== selected) tr.classList.toggle('active', selected);
    if (selected) record = item;
    ++count;
  }
  const label = currentOnly ? '第 ' + frame + ' 帧 · ' + count + ' / ' + rows.length : String(count);
  setText(counter, showCount ? label + ' 条记录' : '');
  setHidden(empty, count !== 0);
  setText(empty, emptyText);
  return record;
}
