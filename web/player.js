// Column-major matrices, using the PSP model's Y-up coordinates.
export const identity = () => new Float32Array([1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]);

export function multiply(a, b) {
  const result = new Float32Array(16);
  for (let c = 0; c < 16; c += 4) {
    for (let r = 0; r < 4; r++) {
      result[c + r] = a[r] * b[c] + a[r + 4] * b[c + 1] + a[r + 8] * b[c + 2] + a[r + 12] * b[c + 3];
    }
  }
  return result;
}

export function rigidInverse(m) {
  const result = new Float32Array([m[0], m[4], m[8], 0, m[1], m[5], m[9], 0, m[2], m[6], m[10], 0, 0, 0, 0, 1]);
  const p = point(result, [-m[12], -m[13], -m[14]]);
  result.set(p, 12);
  return result;
}

export const point = (m, [x, y, z]) => [
  m[0] * x + m[4] * y + m[8] * z + m[12],
  m[1] * x + m[5] * y + m[9] * z + m[13],
  m[2] * x + m[6] * y + m[10] * z + m[14],
];
export const plus = (a, b) => a.map((v, i) => v + b[i]);
export const minus = (a, b) => a.map((v, i) => v - b[i]);
export const times = (a, n) => a.map(v => v * n);
export const dot = (a, b) => a.reduce((sum, v, i) => sum + v * b[i], 0);
export const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
export const unit = a => times(a, 1 / (Math.hypot(...a) || 1));

function restRotation([x, y, z]) {
  const [sx, sy, sz] = [Math.sin(x / 2), Math.sin(y / 2), Math.sin(z / 2)];
  const [cx, cy, cz] = [Math.cos(x / 2), Math.cos(y / 2), Math.cos(z / 2)];
  return [sx * cy * cz - cx * sy * sz, cx * sy * cz + sx * cy * sz,
    cx * cy * sz - sx * sy * cz, cx * cy * cz + sx * sy * sz];
}

function trs([x, y, z, w], [sx, sy, sz], [px, py, pz]) {
  const xx = x * x, yy = y * y, zz = z * z;
  const xy = x * y, xz = x * z, yz = y * z, wx = w * x, wy = w * y, wz = w * z;
  return new Float32Array([
    (1 - 2 * (yy + zz)) * sx, 2 * (xy + wz) * sx, 2 * (xz - wy) * sx, 0,
    2 * (xy - wz) * sy, (1 - 2 * (xx + zz)) * sy, 2 * (yz + wx) * sy, 0,
    2 * (xz + wy) * sz, 2 * (yz - wx) * sz, (1 - 2 * (xx + yy)) * sz, 0,
    px, py, pz, 1,
  ]);
}

export function sample(keys, stride, frame) {
  let low = 0;
  let high = keys.length / stride - 1;
  const row = n => keys.slice(n * stride + 1, (n + 1) * stride);
  if (high <= 0 || frame < keys[0]) return row(0);
  if (frame >= keys[high * stride]) return row(high);
  while (high - low > 1) {
    const middle = (high + low) >>> 1;
    if (frame < keys[middle * stride]) high = middle;
    else low = middle;
  }
  const f0 = keys[low * stride], f1 = keys[high * stride];
  const t = (frame - f0) / (f1 - f0);
  const a = row(low), b = row(high);
  if (stride !== 5) return a.map((v, i) => v + (b[i] - v) * t);
  let cos = dot(a, b);
  if (cos < 0) {
    cos = -cos;
    for (let i = 0; i < 4; i++) b[i] = -b[i];
  }
  if (cos > 0.9999) return unit(a.map((v, i) => v + (b[i] - v) * t));
  const angle = Math.acos(cos);
  const easeA = Math.sin((1 - t) * angle) / Math.sin(angle);
  const easeB = Math.sin(t * angle) / Math.sin(angle);
  return a.map((v, i) => v * easeA + b[i] * easeB);
}

export function buildRig(model) {
  const nodes = model.bones.map(bone => ({
    parent: bone.parent,
    rotation: restRotation(bone.rotation),
    scale: bone.scale,
    position: bone.translation,
  }));
  const logical = new Map(model.nodeChannels || Object.entries(model.rig).map(([id, node]) => [node, Number(id)]));
  const bind = [], relatives = [], inverses = [], roots = [];
  nodes.forEach((node, i) => {
    const local = trs(node.rotation, node.scale, node.position);
    const parent = node.parent;
    bind.push(parent < 0 ? local : multiply(bind[parent], local));
    roots.push(parent < 0 ? i : roots[parent]);
    relatives.push(multiply(rigidInverse(bind[roots[i]]), bind[i]));
    inverses.push(rigidInverse(relatives[i]));
  });
  return {nodes, logical, bind, relatives, inverses, root: model.rig[0]};
}

export function evaluatePose(rig, animation, frame, rootMotion) {
  if (!animation) return rig.bind;
  const world = [];
  rig.nodes.forEach((node, id) => {
    const logical = rig.logical.get(id);
    const track = animation.channels[logical];
    let {rotation, scale, position} = node;
    if (track) {
      rotation = sample(animation.tracks[track[0]], 5, frame);
      if (animation.localScale) scale = sample(animation.tracks[track[2]], 4, frame);
      else if (!animation.fe3) scale = [1, 1, 1];
      const path = animation.tracks[track[1]];
      if (logical === 0 && (!animation.fe3 || id === rig.root)) {
          position = sample(path, 4, frame);
          if (!rootMotion) {
            const first = animation.first ?? 0, last = animation.last;
            const start = sample(path,4,first), end = sample(path,4,last);
            const progress = Math.min(1,Math.max(0,(frame-first) / Math.max(1,last-first)));
            position[0] -= (end[0]-start[0])*progress;
            position[2] -= (end[2]-start[2])*progress;
          }
      } else if (animation.localTranslation) {
        position = sample(path, 4, frame);
      }
    }
    const local = trs(rotation, scale, position);
    world.push(node.parent < 0 ? local : multiply(world[node.parent], local));
  });
  return world;
}

export function visibleHands(hands, events, frame, form = 0) {
  const hidden = new Set(hands.flat().filter(node => node >= 0));
  const choices = hands.map(group => group.findIndex(node => node >= 0));
  for (const event of events) {
    if (event.frame > frame) break;
    if (event.hand && matchesForm(event, form)) choices[event.hand[0]] = event.hand[1];
  }
  hands.forEach((group, side) => hidden.delete(group[choices[side]]));
  return hidden;
}

export function skeletonEdges(actor) {
  if (!actor?.world || actor.sceneVisible === false || actor.nativeVisible === false) return [];
  const edges = [];
  for (const node of actor.skeleton) {
    const parent = actor.rig.nodes[node].parent;
    if (actor.skeleton.has(parent) && !actor.hidden.has(node) && !actor.hidden.has(parent)) {
      edges.push(...actor.world[parent].slice(12,15), ...actor.world[node].slice(12,15));
    }
  }
  return edges;
}

export function sceneViewport(width, height, native) {
  if (!native) return [0,0,width,height];
  const scale = Math.min(width/640,height/448);
  const w = Math.max(1,Math.round(640*scale)), h = Math.max(1,Math.round(448*scale));
  return [Math.floor((width-w)/2),Math.floor((height-h)/2),w,h];
}

export function scenePlacement(offset, reverse) {
  const direction = reverse ? -1 : 1;
  return [direction,0,0,0, 0,1,0,0, 0,0,direction,0, ...offset,1];
}

export function movieFrameAt(cue, frame, media) {
  const elapsed = cue.paused ? 0 : Math.max(0,frame-cue.frame)*media.rate/60;
  return Math.max(0,Math.min(media.frames-1,Math.floor(cue.first+elapsed+1e-6)));
}

export function frameIndex(frame) {
  return Math.floor(frame + 1e-7);
}

export function activeAt(event, frame) {
  const tick = frameIndex(frame);
  return tick === event.frame || (event.frame < tick && tick < event.until);
}

export function matchesForm(event, form) {
  return Boolean(event.mask & (1 << form));
}

export function speedMap(animation, events) {
  const result = [{frame: 0, second: 0, rate: animation.rate}];
  for (const event of events) {
    if (!event.rate || event.frame < 0 || event.frame >= animation.last) continue;
    const prev = result.at(-1);
    const rate = event.rate * animation.rate;
    if (event.frame === prev.frame) prev.rate = rate;
    else result.push({frame: event.frame, second: prev.second + (event.frame - prev.frame) / (60 * prev.rate), rate});
  }
  return result;
}

export function frameToSecond(map, frame) {
  const segment = map.findLast(item => item.frame <= frame) || map[0];
  return segment.second + (frame - segment.frame) / (60 * segment.rate);
}

export function secondToFrame(map, second) {
  const segment = map.findLast(item => item.second <= second) || map[0];
  return segment.frame + (second - segment.second) * 60 * segment.rate;
}

export function sceneActorAt(clips, frame) {
  const clip = clips.findLast(item => item.frame <= frame);
  if (!clip) return null;
  const {animation, rates} = clip;
  const first = animation.first ?? 0;
  const elapsed = clip.held ? 0 : (frame - clip.frame) / 60;
  const seconds = frameToSecond(rates, first) + elapsed;
  return {...clip, local: Math.min(animation.last, secondToFrame(rates, seconds))};
}

function cameraChannel(keys, time, mode) {
  if (!keys.length) return 0;
  if (time < keys[0][0]) return keys[0][1];
  if (time >= keys.at(-1)[0]) return keys.at(-1)[1];
  const next = keys.findIndex(([t]) => t > time);
  const [t0, y0, b0 = 0] = keys[next - 1], [t1, y1, b1 = 0] = keys[next];
  const h = t1 - t0, u = time - t0, slope = (y1 - y0) / h;
  if (mode === 1 && keys.length > 2 && h < 0.0166666657) return y1;
  return y0 + u * (slope - h * (2 * b0 + b1) + u * (3 * b0 + u * (b1 - b0) / h));
}

export function cameraAt(cuts, frame) {
  const cut = cuts.findLast(item => item.frame <= frame);
  if (!cut?.curve) return null;
  const curve = cut.curve;
  const time = Math.min(curve.seconds, cut.start + (cut.paused ? 0 : (frame - cut.frame) / 60));
  const values = curve.channels.map(keys => cameraChannel(keys, time, curve.mode));
  return {actor:cut.actor,eye:values.slice(0,3),target:values.slice(3,6),
    roll:values[6]*Math.PI/180,projection:curve.projection,eyeHeight:cut.eyeHeight};
}
