import assert from 'node:assert/strict';
import test from 'node:test';
import {updateEventList} from '../web/event_list.js';

// Count assignments as well as changes so needless work on a paused frame is visible.
function element() {
  let hidden = false, text = '', active = false;
  return {
    writes:0,
    get hidden() { return hidden; },
    set hidden(value) { this.writes++; hidden = value; },
    get textContent() { return text; },
    set textContent(value) { this.writes++; text = value; },
    classList:{
      contains() { return active; },
      toggle(name, value) { assert.equal(name, 'active'); active = value; },
    },
  };
}

function fixture() {
  const tr = element();
  tr.cells = [null, null, null, null, element()];
  const row = {item:{id:0, frame:31, until:42}, tr, idleStatus:'31–41'};
  const options = {frame:31, currentOnly:true, selectedId:0, timeline:true,
    counter:element(), empty:element(), showCount:true, emptyText:'没有记录'};
  return {row, tr, options, render:() => updateEventList([row], options)};
}

test('trigger, duration, exclusive end and clip-wide range retain the selected record', () => {
  const {row, tr, options, render} = fixture();
  assert.equal(render(), row.item);
  assert.equal(tr.cells[4].textContent, '触发');
  options.frame = 41;
  assert.equal(render(), row.item);
  assert.equal(tr.cells[4].textContent, '生效');
  const writes = [tr, tr.cells[4], options.counter, options.empty].map(node => node.writes);
  render();
  assert.deepEqual([tr, tr.cells[4], options.counter, options.empty].map(node => node.writes), writes);
  options.frame = 42;
  assert.equal(render(), undefined);
  assert.equal(tr.hidden, true);
  assert.equal(options.counter.textContent, '第 42 帧 · 0 / 1 条记录');
  options.currentOnly = false;
  assert.equal(render(), row.item);
  assert.equal(tr.cells[4].textContent, '31–41');
});

test('raw PS2 BOM rows can be selected independently of playback', () => {
  const {row, tr, options, render} = fixture();
  delete row.idleStatus;
  options.timeline = options.currentOnly = false;
  assert.equal(render(), row.item);
  assert.equal(tr.cells[4].textContent, '—');
  assert.equal(options.counter.textContent, '1 条记录');
  assert.equal(options.empty.hidden, true);
});
