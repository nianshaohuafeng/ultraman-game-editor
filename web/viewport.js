import {activeAt, buildRig, cameraAt, cross, dot, evaluatePose, minus, multiply, plus,
  point, sceneActorAt, scenePlacement, sceneViewport, skeletonEdges, speedMap, times, unit, visibleHands} from './player.js';

const MODEL_VERT = '#version 300 es\n' + [
  'layout(location=0) in vec3 aPosition;',
  'layout(location=1) in vec3 aNormal;',
  'layout(location=2) in vec2 aUV;',
  'layout(location=3) in vec4 aColor;',
  'uniform mat4 uView;',
  'out vec3 vNormal; out vec2 vUV; out vec4 vColor;',
  'void main() { gl_Position = uView * vec4(aPosition, 1.0); vNormal=aNormal; vUV=aUV; vColor=aColor; }',
].join('\n');
const MODEL_FRAG = '#version 300 es\n' + [
  'precision highp float;',
  'in vec3 vNormal; in vec2 vUV; in vec4 vColor;',
  'uniform sampler2D uTexture, uNextTexture;',
  'uniform float uPaletteBlend;',
  'uniform vec4 uTint;',
  'uniform bool uTextured, uLit;',
  'uniform bool uNative;',
  'uniform bool uNativeBlend;',
  'uniform ivec2 uTextureFunction;',
  'out vec4 color;',
  'void main() {',
  '  if (uNative) {',
  '    vec4 texel = uTextured ? texture(uTexture, vUV) : vec4(uTint.rgb, 128.0/255.0);',
  '    int functionId = uTextureFunction.x;',
  '    vec3 vertex = uLit ? vColor.rgb : vec3(1.0);',
  '    vec3 rgb = functionId == 1 ? texel.rgb : texel.rgb * vertex;',
  '    if (functionId >= 2) rgb += vColor.a * (128.0/255.0);',
  '    float alpha = vColor.a;',
  '    if(uTextureFunction.y != 0) {',
  '      float ta = texel.a * (255.0/128.0);',
  '      alpha = functionId == 0 ? ta * alpha : functionId == 2 ? ta + alpha : ta;',
  '    }',
  '    if (alpha <= 0.0) discard;',
  '    color = vec4(clamp(rgb, 0.0, 1.0), uNativeBlend ? clamp(alpha, 0.0, 1.0) : 1.0);',
  '    return;',
  '  }',
  '  vec4 base = uTextured ? texture(uTexture, vUV) : uTint;',
  '  if (uTextured && uPaletteBlend > 0.0) base = mix(base, texture(uNextTexture, vUV), uPaletteBlend);',
  '  float lambert = 1.0;',
  '  if (uLit && length(vNormal) > 0.0001) {',
  '    vec3 n = normalize(vNormal);',
  '    lambert = 0.62 + 0.30 * max(dot(n, normalize(vec3(-0.5,0.85,0.9))), 0.0)',
  '      + 0.12 * max(dot(n, normalize(vec3(0.9,0.25,-0.55))), 0.0);',
  '  }',
  '  float alpha = uNativeBlend ? base.a * vColor.a : 1.0;',
  '  if (alpha <= 0.0) discard;',
  '  color = vec4(base.rgb * vColor.rgb * lambert, alpha);',
  '}',
].join('\n');
const LINE_VERT = '#version 300 es\nlayout(location=0) in vec3 aPosition;\n' +
  'uniform mat4 uView;\nvoid main() { gl_Position=uView*vec4(aPosition,1.0); }';
const LINE_FRAG = '#version 300 es\nprecision mediump float;\nuniform vec4 uColor;\n' +
  'out vec4 color;\nvoid main() { color=uColor; }';
const MOVIE_VERT = '#version 300 es\nout vec2 vUV;\n' +
  'void main() { vec2 p=vec2((gl_VertexID << 1) & 2, gl_VertexID & 2);' +
  'gl_Position=vec4(2.0*p-1.0,0.0,1.0); vUV=vec2(p.x,1.0-p.y); }';
const MOVIE_FRAG = '#version 300 es\nprecision mediump float;\n' +
  'in vec2 vUV; uniform sampler2D uFrame; out vec4 color;\n' +
  'void main() { color=vec4(texture(uFrame,vUV).rgb,1.0); }';
const COLLIDER_VERT = '#version 300 es\n' + [
  'layout(location=0) in vec4 aPosition;',
  'uniform mat4 uView;',
  'uniform vec3 uFrom, uTo, uAxis, uSide, uAcross;',
  'uniform float uRadius;',
  'void main() {',
  '  vec3 p = mix(uFrom, uTo, aPosition.w)',
  '    + uRadius * (aPosition.x * uSide + aPosition.y * uAxis + aPosition.z * uAcross);',
  '  gl_Position = uView * vec4(p, 1.0);',
  '}',
].join('\n');
const COLLIDER_STYLES = [
  ['body', [0.46, 0.90, 0.70], 0.09, 0.70],
  ['extra', [0.51, 0.74, 1.00], 0.11, 0.80],
  ['attack', [1.00, 0.51, 0.47], 0.18, 0.95],
];

function program(gl, vertex, fragment, uniforms) {
  const result = gl.createProgram();
  for (const [kind, source] of [[gl.VERTEX_SHADER, vertex], [gl.FRAGMENT_SHADER, fragment]]) {
    const shader = gl.createShader(kind);
    gl.shaderSource(shader, source);
    gl.compileShader(shader);
    if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(shader));
    gl.attachShader(result, shader);
    gl.deleteShader(shader);
  }
  gl.linkProgram(result);
  if (!gl.getProgramParameter(result, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(result));
  return {program: result, u: Object.fromEntries(uniforms.map(name => [name, gl.getUniformLocation(result, name)]))};
}

function buffer(gl, target, data, usage = gl.STATIC_DRAW) {
  const id = gl.createBuffer();
  gl.bindBuffer(target, id);
  gl.bufferData(target, data, usage);
  return id;
}

export function loadImages(textures) {
  return Promise.all(textures.map(({uri, id}) => new Promise((resolve, reject) => {
    const image = new Image();
    image.onload = () => resolve(image);
    image.onerror = () => reject(new Error('无法加载 YUM ' + id + ' 的贴图。'));
    image.src = uri;
  })));
}

function uploadTexture(gl, image, native = false) {
  const texture = gl.createTexture();
  gl.bindTexture(gl.TEXTURE_2D, texture);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, native ? gl.LINEAR : gl.LINEAR_MIPMAP_LINEAR);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.REPEAT);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.REPEAT);
  gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, image);
  if (!native) gl.generateMipmap(gl.TEXTURE_2D);
  return texture;
}

function indexBuffers(gl, batches) {
  return batches.map(([material, faces]) => {
    const edges = new Uint32Array(faces.length * 2);
    for (let i = 0; i < faces.length; i += 3) {
      const [a, b, c] = [faces[i], faces[i + 1], faces[i + 2]];
      edges.set([a, b, b, c, c, a], i * 2);
    }
    return {material, count: faces.length, faces: buffer(gl, gl.ELEMENT_ARRAY_BUFFER, new Uint32Array(faces)),
      edges: buffer(gl, gl.ELEMENT_ARRAY_BUFFER, edges)};
  });
}

function meshBuffers(gl, mesh, rig) {
  const vao = gl.createVertexArray();
  gl.bindVertexArray(vao);
  const out = new Float32Array(6 * mesh.source.length);
  const dynamic = buffer(gl, gl.ARRAY_BUFFER, out, gl.DYNAMIC_DRAW);
  for (const [location, offset] of [[0, 0], [1, 12]]) {
    gl.enableVertexAttribArray(location);
    gl.vertexAttribPointer(location, 3, gl.FLOAT, false, 24, offset);
  }
  const attr = mesh.source.flatMap((_, i) => mesh.attributes.slice(i * 9 + 3, (i + 1) * 9));
  const fixed = buffer(gl, gl.ARRAY_BUFFER, new Float32Array(attr));
  gl.enableVertexAttribArray(2);
  gl.vertexAttribPointer(2, 2, gl.FLOAT, false, 24, 0);
  const vertexColors = mesh.lighting ? new Float32Array(4 * mesh.source.length) : null;
  const colorBuffer = vertexColors ? buffer(gl, gl.ARRAY_BUFFER, vertexColors, gl.DYNAMIC_DRAW) : null;
  gl.enableVertexAttribArray(3);
  gl.vertexAttribPointer(3, 4, gl.FLOAT, false, vertexColors ? 16 : 24, vertexColors ? 0 : 8);
  const batches = indexBuffers(gl, mesh.batches);
  gl.bindVertexArray(null);

  if (mesh.controlBones) {
    const vertices = mesh.controlBones.length + mesh.subdivision.length;
    return {...mesh, vao, dynamic, fixed, out, batches, vertexColors, colorBuffer,
      deformed: new Float32Array(3 * vertices), normals: new Float32Array(3 * vertices),
      triangles: mesh.batches.flatMap(([, faces]) => faces.map(i => 3 * mesh.source[i]))};
  }

  const positions = [];
  const influences = [];
  const palette = mesh.palette;
  const relative = rig.relatives[mesh.owner];
  for (let i = 0; i < mesh.positions.length; i += 3) {
    const p = mesh.positions.slice(i, i + 3);
    const weights = palette.flatMap((node, slot) => {
      const weight = mesh.weights[i / 3 * palette.length + slot];
      return weight > 0 ? [node, weight] : [];
    });
    influences.push(weights);
    positions.push(weights.length ? point(relative, p) : p);
  }
  return {...mesh, vao, dynamic, fixed, out, batches, positions, influences,
    weightedPoint: new Float32Array(positions.length * 3),
    transforms: new Float32Array(positions.length * 16)};
}

export function skinControlMesh(mesh, world, morph) {
  const {deformed, normals, triangles, out} = mesh;
  mesh.controlBones.forEach((node, i) => {
    const offset = 3 * i;
    const local = morph?.[mesh.controlOffsets[i]] || mesh.positions.slice(offset, offset + 3);
    deformed.set(point(world[node], local), offset);
  });
  mesh.subdivision.forEach((terms, i) => {
    const index = 3 * (i + mesh.controlBones.length);
    for (let component = 0; component < 3; component++) {
      let sum = 0;
      for (let t = 0; t < terms.length; t += 2) sum += deformed[3 * terms[t] + component] * terms[t + 1];
      deformed[index + component] = sum;
    }
  });
  if (mesh.lighting) {
    shadeControlMesh(mesh, world);
    return;
  }
  normals.fill(0);
  for (let i = 0; i < triangles.length; i += 3) {
    const a = triangles[i], b = triangles[i + 1], c = triangles[i + 2];
    const abx = deformed[b] - deformed[a], acx = deformed[c] - deformed[a];
    const aby = deformed[b + 1] - deformed[a + 1], acy = deformed[c + 1] - deformed[a + 1];
    const abz = deformed[b + 2] - deformed[a + 2], acz = deformed[c + 2] - deformed[a + 2];
    const x = aby * acz - abz * acy, y = abz * acx - abx * acz, z = abx * acy - aby * acx;
    for (const vertex of [a, b, c]) {
      normals[vertex] += x;
      normals[vertex + 1] += y;
      normals[vertex + 2] += z;
    }
  }
  mesh.source.forEach((vertex, i) => {
    const a = 3 * vertex, b = 6 * i;
    out[b] = deformed[a];
    out[b + 1] = deformed[a + 1];
    out[b + 2] = deformed[a + 2];
    out[b + 3] = normals[a];
    out[b + 4] = normals[a + 1];
    out[b + 5] = normals[a + 2];
  });
}

// The preview uses a fixed three-light rig. The color slots, normal rings
// and per-channel light coefficients below are the ones used by the MDL.
const FE3_LIGHTS = [
  [unit([-0.45, 0.75, 0.8]), 0.56],
  [unit([0.8, 0.35, 0.65]), 0.34],
  [unit([0.15, 0.5, -1]), 0.24],
];

function shadeControlMesh(mesh, world) {
  const {lighting, deformed, out, colorIds} = mesh;
  const normals = lighting.authored.map(([node, x, y, z]) => {
    const m = world[node];
    return unit([m[0]*x + m[4]*y + m[8]*z, m[1]*x + m[5]*y + m[9]*z, m[2]*x + m[6]*y + m[10]*z]);
  });
  for (const [center, ...ring] of lighting.rings) {
    const origin = deformed.subarray(center * 3, center * 3 + 3);
    let normal = [0, 0, 0], last = minus(deformed.subarray(ring[0] * 3, ring[0] * 3 + 3), origin);
    for (let i = 1; i < ring.length; i++) {
      const index = ring[i] * 3;
      const edge = minus(deformed.subarray(index, index + 3), origin);
      normal = plus(normal, cross(last, edge));
      last = edge;
    }
    normals.push(unit(normal));
  }
  const zero = [0, 0, 0];
  const colors = [...lighting.rgba];
  const directions = colors.map(() => zero);
  for (const [index, base, r, g, b] of lighting.lit) {
    const n = normals[index];
    const diffuse = FE3_LIGHTS.reduce((sum, [light, level]) => sum + Math.max(dot(light, n), 0) * level, 0);
    const c = lighting.rgba[base];
    colors.push([r, g, b].map((gain, i) => Math.min(255, Math.max(0, Math.trunc(128 * (c[i] + gain * diffuse)))) / 128).concat(c[3]));
    directions.push(n);
  }
  for (const [a, b] of lighting.pairs) {
    colors.push(colors[a].map((v, i) => Math.trunc(64 * (v + colors[b][i])) / 128));
    directions.push(unit(plus(directions[a], directions[b])));
  }
  mesh.source.forEach((index, i) => {
    const at = index * 3;
    const color = colorIds[i];
    out.set(deformed.subarray(at, at + 3), 6 * i);
    out.set(directions[color], 6 * i + 3);
    mesh.vertexColors?.set(colors[color], i * 4);
  });
}

export function skinMesh(mesh, world, skin, relative) {
  const {transforms, positions, influences, owner, attributes, out, weightedPoint} = mesh;
  for (let vertex = 0; vertex < positions.length; vertex++) {
    const weights = influences[vertex];
    const matrix = transforms.subarray(16 * vertex, 16 * (vertex + 1));
    if (!weights.length) {
      matrix.set(world[owner]);
    } else {
      matrix.fill(0);
      for (let i = 0; i < weights.length; i += 2) {
        const transform = skin[weights[i]], w = weights[i + 1];
        for (let n = 0; n < 16; n++) matrix[n] += transform[n] * w;
      }
    }
    weightedPoint.set(point(matrix, positions[vertex]), 3 * vertex);
  }
  for (let i = 0; i < mesh.source.length; i++) {
    const o = i * 6, source = mesh.source[i], a = i * 9;
    const m = 16 * source, v = 3 * source;
    let x = attributes[a], y = attributes[a + 1], z = attributes[a + 2];
    if (influences[source].length) {
      const nx = relative[0] * x + relative[4] * y + relative[8] * z;
      const ny = relative[1] * x + relative[5] * y + relative[9] * z;
      z = relative[2] * x + relative[6] * y + relative[10] * z;
      x = nx;
      y = ny;
    }
    out[o] = weightedPoint[v];
    out[o + 1] = weightedPoint[v + 1];
    out[o + 2] = weightedPoint[v + 2];
    out[o + 3] = transforms[m] * x + transforms[m + 4] * y + transforms[m + 8] * z;
    out[o + 4] = transforms[m + 1] * x + transforms[m + 5] * y + transforms[m + 9] * z;
    out[o + 5] = transforms[m + 2] * x + transforms[m + 6] * y + transforms[m + 10] * z;
  }
}

function cameraMatrix(center, distance, yaw, pitch, aspect, size) {
  const offset = times([Math.cos(pitch) * Math.sin(yaw), Math.sin(pitch), Math.cos(pitch) * Math.cos(yaw)], distance);
  return viewProjection(plus(center, offset), center, 0, 1 / Math.tan(Math.PI * 0.095), aspect, size);
}

function viewProjection(eye, target, roll, f, aspect, size) {
  const back = unit(minus(eye, target));
  const side = unit(cross(Math.abs(back[1]) > .9999 ? [0, 0, 1] : [0, 1, 0], back));
  const vertical = cross(back, side), c = Math.cos(roll), s = Math.sin(roll);
  const right = plus(times(side, c), times(vertical, s));
  const up = plus(times(vertical, c), times(side, -s));
  const view = new Float32Array([
    right[0], up[0], back[0], 0, right[1], up[1], back[1], 0,
    right[2], up[2], back[2], 0, -dot(right, eye), -dot(up, eye), -dot(back, eye), 1,
  ]);
  const near = size * 0.009, far = size * 55;
  const k = 1 / (near - far);
  const proj = new Float32Array([f / aspect, 0, 0, 0, 0, f, 0, 0,
    0, 0, (near + far) * k, -1, 0, 0, 2 * near * far * k, 0]);
  return multiply(proj, view);
}

function fitMeshes(meshes, hidden) {
  const lo = [Infinity, Infinity, Infinity], hi = [-Infinity, -Infinity, -Infinity];
  for (const mesh of meshes) {
    if (hidden.has(mesh.owner)) continue;
    for (let i = 0; i < mesh.out.length; i += 6) {
      for (let n = 0; n < 3; n++) {
        lo[n] = Math.min(lo[n], mesh.out[i + n]);
        hi[n] = Math.max(hi[n], mesh.out[i + n]);
      }
    }
  }
  const size = Math.max(hi[1] - lo[1], (hi[0] - lo[0]) * 0.8, (hi[2] - lo[2]) * 0.8);
  return Number.isFinite(size) && size > 0 ? {center: times(plus(lo, hi), 0.5), floor: lo[1], size} :
    {center: [0, 1, 0], floor: 0, size: 2};
}

function orbitGrid(view) {
  const out = [];
  const {center: [x, , z], floor, size} = view;
  const y = floor - size * 0.008, reach = size * 1.55;
  const steps = 22;
  for (let i = -steps; i <= steps; i++) {
    const d = i / steps * reach;
    out.push(x - reach, y, z + d, x + reach, y, z + d, x + d, y, z - reach, x + d, y, z + reach);
  }
  return out;
}

function colliderMesh(gl) {
  const sides = 24, steps = 6;
  const rings = 2 * (steps + 1);
  const vertices = [], faces = [], outline = [], secondEquator = [];
  for (let row = 0; row < rings; row++) {
    const end = row > steps ? 1 : 0;
    const latitude = (row - steps - end) * Math.PI / (2 * steps);
    const height = Math.sin(latitude), width = Math.cos(latitude);
    for (let column = 0; column < sides; column++) {
      const angle = column * 2 * Math.PI / sides;
      vertices.push(width * Math.cos(angle), height, width * Math.sin(angle), end);
      const current = row * sides + column, next = row * sides + (column + 1) % sides;
      if (row === steps) outline.push(current, next);
      if (row === steps + 1) secondEquator.push(current, next);
      if (row < rings - 1) {
        faces.push(current, next, current + sides, next, next + sides, current + sides);
        if (column % (sides / 4) === 0) outline.push(current, current + sides);
      }
    }
  }
  const vao = gl.createVertexArray();
  gl.bindVertexArray(vao);
  buffer(gl, gl.ARRAY_BUFFER, new Float32Array(vertices));
  gl.enableVertexAttribArray(0);
  gl.vertexAttribPointer(0, 4, gl.FLOAT, false, 0, 0);
  const faceBuffer = buffer(gl, gl.ELEMENT_ARRAY_BUFFER, new Uint16Array(faces));
  const edgeBuffer = buffer(gl, gl.ELEMENT_ARRAY_BUFFER, new Uint16Array([...outline, ...secondEquator]));
  gl.bindVertexArray(null);
  return {vao, faceBuffer, edgeBuffer, faceCount: faces.length, sphereEdges: outline.length,
    capsuleEdges: outline.length + secondEquator.length};
}

export class Viewport {
  constructor(canvas) {
    this.canvas = canvas;
    this.gl = canvas.getContext('webgl2', {antialias: true, alpha: false});
    const gl = this.gl;
    if (!gl) throw new Error('当前图形环境未提供 WebGL 2。');
    this.modelProgram = program(gl, MODEL_VERT, MODEL_FRAG,
      ['uView', 'uTexture', 'uNextTexture', 'uPaletteBlend', 'uTint', 'uTextured', 'uLit', 'uNative',
        'uNativeBlend', 'uTextureFunction']);
    this.lineProgram = program(gl, LINE_VERT, LINE_FRAG, ['uView', 'uColor']);
    this.movieProgram = program(gl, MOVIE_VERT, MOVIE_FRAG, ['uFrame']);
    this.movieTexture = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, this.movieTexture);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    this.lineVao = gl.createVertexArray();
    gl.bindVertexArray(this.lineVao);
    this.lineBuffer = buffer(gl, gl.ARRAY_BUFFER, 0, gl.DYNAMIC_DRAW);
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, 3, gl.FLOAT, false, 0, 0);
    gl.bindVertexArray(null);
    this.colliderProgram = program(gl, COLLIDER_VERT, LINE_FRAG,
      ['uView', 'uFrom', 'uTo', 'uAxis', 'uSide', 'uAcross', 'uRadius', 'uColor']);
    this.colliderMesh = colliderMesh(gl);
    this.colliderLayers = {body: true, extra: true, attack: true};
    this.colliderVolumes = null;
    this.neutralTexture = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, this.neutralTexture);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, 1, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE, new Uint8Array([255, 255, 255, 255]));
    gl.clearColor(0.061, 0.094, 0.149, 1);
    gl.enable(gl.DEPTH_TEST);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
    this.options = {bones: false, hit: false, wire: false, texture: true, light: true, root: true};
    this.meshes = [];
    this.visibleMeshes = [];
    this.textures = [];
    this.events = [];
    this.frame = 0;
    this.sceneCamera = true;
    this.fit = {center: [0, 1, 0], size: 2, floor: 0};
    this.resetView();
    this.bindMouse();
    new ResizeObserver(() => this.draw()).observe(canvas);
  }

  bindMouse() {
    this.canvas.addEventListener('contextmenu', event => event.preventDefault());
    this.canvas.addEventListener('pointerdown', event => {
      this.drag = {x: event.clientX, y: event.clientY, pan: event.button !== 0 || event.shiftKey};
      this.canvas.setPointerCapture(event.pointerId);
      this.canvas.classList.add('dragging');
    });
    this.canvas.addEventListener('lostpointercapture', () => {
      this.drag = null;
      this.canvas.classList.remove('dragging');
    });
    this.canvas.addEventListener('pointermove', event => {
      if (!this.drag) return;
      const dx = event.clientX - this.drag.x, dy = event.clientY - this.drag.y;
      this.drag.x = event.clientX;
      this.drag.y = event.clientY;
      this.sceneCamera = false;
      if (this.drag.pan) {
        const amount = this.distance * 0.0012;
        const right = [Math.cos(this.yaw), 0, -Math.sin(this.yaw)];
        this.center = plus(this.center, plus(times(right, -dx * amount), [0, dy * amount, 0]));
      } else {
        this.yaw -= dx * 0.009;
        this.pitch = Math.max(-1.42, Math.min(1.42, this.pitch + dy * 0.009));
      }
      this.draw();
    });
    this.canvas.addEventListener('wheel', event => {
      event.preventDefault();
      this.sceneCamera = false;
      const next = this.distance * Math.exp(event.deltaY * 0.0012);
      this.distance = Math.min(this.fit.size * 16, Math.max(this.fit.size * 0.24, next));
      this.draw();
    }, {passive: false});
  }

  resetView() {
    this.center = [...this.fit.center];
    this.distance = this.fit.size * 1.94;
    if (this.opponent) {
      this.center = plus(this.center, times(this.opponent.offset, .5));
      this.distance *= 1.24;
    }
    this.yaw = this.scene && this.opponent ? Math.PI * 0.44 : -0.2;
    this.pitch = 0.09;
    this.sceneCamera = true;
    this.draw();
  }

  freeModel(meshes, textures) {
    const gl = this.gl;
    for (const mesh of meshes) {
      gl.deleteVertexArray(mesh.vao);
      for (const id of [mesh.dynamic, mesh.fixed, mesh.colorBuffer, ...mesh.batches.flatMap(batch => [batch.faces, batch.edges])]) {
        gl.deleteBuffer(id);
      }
    }
    for (const texture of textures) gl.deleteTexture(texture);
  }

  clear() {
    this.scene = null;
    this.movie = null;
    this.clearOpponent(false);
    this.freeModel(this.meshes, this.textures);
    this.meshes = [];
    this.visibleMeshes = [];
    this.textures = [];
    this.rig = null;
    this.model = null;
    this.world = null;
    this.animation = null;
    this.events = [];
    this.appearance = null;
    this.colliderVolumes = null;
    this.motionOrigin = null;
    this.draw();
  }

  setModel(model, images) {
    this.clear();
    this.rig = buildRig(model);
    this.model = model;
    this.skeleton = new Set(model.skeleton || []);
    this.textures = images.map(image => uploadTexture(this.gl, image, model.fe3));
    this.meshes = model.meshes.map(mesh => meshBuffers(this.gl, mesh, this.rig));
    this.appearance = model.appearances.find(appearance => appearance.key === 'normal');
    this.setFrame(0, false);
    this.setPart('body');
  }

  setOpponent(model, images, animation, events, offset = [this.fit.size * .63, 0, 0]) {
    this.clearOpponent(false);
    const rig = buildRig(model);
    const meshes = model.meshes.map(mesh => meshBuffers(this.gl, mesh, rig));
    const attachments = new Set(model.parts.filter(part => !part.activeTrack).flatMap(part => part.nodes));
    this.opponent = {model,rig,animation,events,meshes,skeleton:new Set(model.skeleton || []),
      visibleMeshes:meshes.filter(mesh => !attachments.has(mesh.owner)), offset,
      appearance:model.appearances.find(item => item.key === 'normal'),
      textures:images.map(image => uploadTexture(this.gl, image, model.fe3)), hidden:new Set()};
    this.center = plus(this.center, times(offset, .5));
    this.distance *= 1.24;
    this.setFrame(this.frame);
  }

  clearOpponent(redraw = true) {
    if (!this.opponent) return;
    this.freeModel(this.opponent.meshes, this.opponent.textures);
    this.center = minus(this.center, times(this.opponent.offset, .5));
    this.distance /= 1.24;
    this.opponent = null;
    if (redraw) this.draw();
  }

  setAppearance(key) {
    this.appearance = this.model?.appearances.find(appearance => appearance.key === key);
    this.draw();
  }

  setPart(key) {
    if (!this.rig) return;
    const part = this.model.parts.find(part => part.key === key);
    const attachments = new Set(this.model.parts.filter(part => !part.activeTrack).flatMap(part => part.nodes));
    this.part = key;
    this.visibleMeshes = this.meshes.filter(mesh => part ? part.nodes.includes(mesh.owner) :
      key === 'all' || !attachments.has(mesh.owner));
    this.setFrame(this.frame, false);
    this.fit = fitMeshes(this.visibleMeshes, this.hidden);
    this.grid = orbitGrid(this.fit);
    this.resetView();
  }

  setMotion(animation, events) {
    if (this.scene) {
      this.scene = null;
      this.movie = null;
      this.clearOpponent(false);
      this.yaw = this.orbitBeforeScene.yaw;
      this.pitch = this.orbitBeforeScene.pitch;
      this.orbitBeforeScene = null;
    }
    this.animation = animation;
    this.events = events;
    this.motionOrigin = null;
    this.setFrame(animation?.first ?? 0, false);
    if (this.rig && animation) {
      const fit = fitMeshes(this.visibleMeshes, this.hidden);
      this.center = plus(this.center, minus(fit.center, this.fit.center));
      this.distance *= fit.size / this.fit.size;
      this.fit = fit;
      this.grid = orbitGrid(fit);
      this.motionOrigin = this.world[this.rig.root]?.slice(12, 15);
    }
    this.draw();
  }

  setBattleScene(scene, opponent, images) {
    if (!this.scene) this.orbitBeforeScene = {yaw:this.yaw, pitch:this.pitch};
    this.scene = {...scene, actors:scene.actors.map(clips => clips.map(clip => ({
      ...clip, rates:speedMap(clip.animation, clip.events),
    })))};
    this.motionOrigin = null;
    this.sceneCamera = true;
    this.clearOpponent(false);
    if (opponent && scene.actors[1].length) {
      const gap = scene.pairedFacing ? 0 : (scene.gap ?? 0.92) * this.fit.size;
      this.setOpponent(opponent, images, null, [], [0, 0, gap]);
      this.yaw = Math.PI * 0.44;
    }
    this.setFrame(0);
  }

  setFrame(frame, redraw = true) {
    if (!this.rig) return;
    this.frame = frame;
    this.colliderVolumes = null;
    const current = this.scene ? sceneActorAt(this.scene.actors[0], frame) : null;
    this.poseActor(this, current?.animation ?? this.animation, current?.events ?? this.events, current?.local ?? frame);
    this.sceneVisible = this.scene?.visibility[0]?.findLast(([at]) => at <= frame)?.[1] ?? true;
    if (this.opponent) {
      const target = this.opponent;
      const segment = this.scene ? sceneActorAt(this.scene.actors[1], frame) : null;
      const anm = segment?.animation ?? target.animation;
      const first = anm?.first ?? 0;
      const at = segment?.local ?? Math.min(anm?.last ?? 0, first + frame - (this.animation?.first ?? 0));
      this.poseActor(target,anm,segment?.events ?? target.events,at,
        scenePlacement(target.offset,this.scene?.pairedFacing !== 'same'));
      target.sceneVisible = (!this.scene || Boolean(segment)) &&
        (this.scene?.visibility[1]?.findLast(([at]) => at <= frame)?.[1] ?? true);
    }
    if (redraw) this.draw();
  }

  poseActor(actor, animation, events, frame, placement) {
    const {model, rig} = actor;
    let world = evaluatePose(rig, animation, frame, this.options.root);
    if (placement) world = world.map(matrix => multiply(placement, matrix));
    actor.world = world;
    actor.placement = placement;
    actor.localFrame = frame;
    actor.localEvents = events;
    actor.hidden = visibleHands(model.hands,events,Math.floor(frame),model.shape);
    const switches = this.scene?.nodeVisibility?.[actor === this ? 0 : 1] || [];
    for (const [at,node,visible] of switches) {
      if (at > this.frame) break;
      if (visible) actor.hidden.delete(node);
      else actor.hidden.add(node);
    }
    actor.nativeVisible = true;
    if (actor !== this || this.part === 'body') {
      for (const part of model.parts) {
        if (part.activeTrack && !animation?.channels[part.activeTrack]) {
          for (const node of part.nodes) actor.hidden.add(node);
        }
      }
    }
    if (model.fe3) this.updateControlMeshes(actor, world, frame, events);
    else {
      const gl = this.gl;
      const skin = world.map((matrix, i) => multiply(matrix, rig.inverses[i]));
      for (const mesh of actor.meshes) {
        skinMesh(mesh, world, skin, rig.relatives[mesh.owner]);
        gl.bindBuffer(gl.ARRAY_BUFFER, mesh.dynamic);
        gl.bufferSubData(gl.ARRAY_BUFFER, 0, mesh.out);
      }
    }
  }

  updateControlMeshes(actor, world, frame, events) {
    const gl = this.gl, morph = {};
    actor.nativeVisible = true;
    for (const event of events) {
      if (event.frame > frame) break;
      if (event.morph) Object.assign(morph, event.morph);
      if (event.visible !== undefined) actor.nativeVisible = event.visible;
    }
    for (const mesh of actor.meshes) {
      if (!mesh.controlBones) continue;
      skinControlMesh(mesh, world, morph);
      gl.bindBuffer(gl.ARRAY_BUFFER, mesh.dynamic);
      gl.bufferSubData(gl.ARRAY_BUFFER, 0, mesh.out);
      if (mesh.colorBuffer) {
        gl.bindBuffer(gl.ARRAY_BUFFER, mesh.colorBuffer);
        gl.bufferSubData(gl.ARRAY_BUFFER, 0, mesh.vertexColors);
      }
    }
  }

  lines(matrix, points, color, overlay = false) {
    if (!points?.length) return;
    const gl = this.gl, {program, u} = this.lineProgram;
    if (overlay) gl.disable(gl.DEPTH_TEST);
    gl.depthMask(false);
    gl.useProgram(program);
    gl.uniformMatrix4fv(u.uView, false, matrix);
    gl.uniform4fv(u.uColor, color);
    gl.bindVertexArray(this.lineVao);
    gl.bindBuffer(gl.ARRAY_BUFFER, this.lineBuffer);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(points), gl.DYNAMIC_DRAW);
    gl.drawArrays(gl.LINES, 0, points.length / 3);
    gl.depthMask(true);
    gl.enable(gl.DEPTH_TEST);
  }

  drawMovie() {
    const video = this.movie;
    if (!video || video.readyState < 2) return false;
    const gl = this.gl, {program, u} = this.movieProgram;
    gl.useProgram(program);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, this.movieTexture);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGB, gl.RGB, gl.UNSIGNED_BYTE, video);
    gl.uniform1i(u.uFrame, 0);
    gl.bindVertexArray(null);
    gl.depthMask(false);
    gl.disable(gl.DEPTH_TEST);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
    gl.enable(gl.DEPTH_TEST);
    gl.depthMask(true);
    return true;
  }

  draw() {
    const {canvas, gl} = this;
    const ratio = Math.min(devicePixelRatio || 1, 2);
    const width = Math.max(1, Math.round(canvas.clientWidth * ratio));
    const height = Math.max(1, Math.round(canvas.clientHeight * ratio));
    if (canvas.width !== width || canvas.height !== height) {
      canvas.width = width;
      canvas.height = height;
    }
    gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
    if (!this.rig) return;
    const screen = sceneViewport(width,height,this.model.fe3 && this.scene && (this.sceneCamera || this.movie));
    gl.viewport(...screen);
    const aspect = screen[2]/screen[3];
    const center = this.motionOrigin
      ? plus(this.center, minus(this.world[this.rig.root].slice(12, 15), this.motionOrigin)) : this.center;
    let eye, aim, view;
    const cut = this.sceneCamera && this.scene && cameraAt(this.scene.cameras, this.frame);
    if (cut) {
      const anchor = cut.actor === 1 && this.opponent?.placement;
      const position = value => anchor ? point(anchor,value) : value;
      eye = position(cut.eye);
      aim = position(cut.target);
      if (cut.eyeHeight !== undefined && eye[1] < cut.eyeHeight) {
        // 0x23B1C0 evaluates the viewing angles before clamping the eye.
        const raiseBy = cut.eyeHeight-eye[1];
        eye = plus(eye,[0,raiseBy,0]);
        aim = plus(aim,[0,raiseBy,0]);
      }
    }
    if (eye && Math.hypot(...minus(eye, aim)) > 0.01) {
      view = viewProjection(eye, aim, cut.roll, cut.projection / (this.model.fe3 ? 224 : 136),aspect,this.fit.size);
    } else view = cameraMatrix(center, this.distance,this.yaw,this.pitch,aspect,this.fit.size);
    if (!this.drawMovie()) this.lines(view, this.grid, [0.26, 0.37, 0.53, 0.49]);
    const {program, u} = this.modelProgram;
    gl.useProgram(program);
    gl.uniformMatrix4fv(u.uView, false, view);
    gl.uniform1i(u.uTexture, 0);
    gl.uniform1i(u.uNextTexture, 1);
    const packets = [];
    this.drawMeshes(this, u, packets);
    if (this.opponent) this.drawMeshes(this.opponent, u, packets);
    if (packets.length) {
      const facing = eye ? unit(minus(eye, aim)) : [Math.sin(this.yaw)*Math.cos(this.pitch), Math.sin(this.pitch),
        Math.cos(this.yaw)*Math.cos(this.pitch)];
      for (const packet of packets) {
        const verts = packet.mesh.out;
        let depth = 0;
        for (let at = 0; at < verts.length; at += 6) {
          depth += verts[at]*facing[0] + verts[at+1]*facing[1] + verts[at+2]*facing[2];
        }
        packet.depth = depth / (verts.length / 6);
      }
      packets.sort((a,b) => a.depth - b.depth);
      gl.depthMask(false);
      for (const packet of packets) {
        gl.bindVertexArray(packet.mesh.vao);
        gl.blendFunc(gl.SRC_ALPHA, packet.alpha === 0x48 ? gl.ONE : gl.ONE_MINUS_SRC_ALPHA);
        this.drawBatch(packet, u);
      }
      gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
      gl.depthMask(true);
    }
    gl.activeTexture(gl.TEXTURE0);
    if (this.options.normals && this.model.fe3) {
      const segments = [], length = this.fit.size * .023;
      for (const mesh of this.visibleMeshes) {
        for (let at = 0; at < mesh.out.length; at += 72) {
          const p = mesh.out.subarray(at, at + 3), n = mesh.out.subarray(at + 3, at + 6);
          if (dot(n,n) > .01) segments.push(...p,...plus(p,times(n,length)));
        }
      }
      this.lines(view, segments, [0.43, 0.85, 0.94, 0.72]);
    }
    if (this.options.bones && (this.part === 'body' || this.part === 'all')) {
      this.lines(view,skeletonEdges(this),[0.35,0.87,0.92,0.83],true);
      this.lines(view,skeletonEdges(this.opponent),[0.93,0.65,0.35,0.83],true);
    }
    if (this.options.hit && (this.part === 'body' || this.part === 'all')) this.drawColliders(view);
    gl.bindVertexArray(null);
  }

  drawMeshes(actor, u, packets) {
    if (actor.sceneVisible === false || actor.nativeVisible === false) return;
    const gl = this.gl;
    const wire = this.options.wire;
    // The v7 rage callbacks advance one CLUT segment in 64 native frames.
    const phase = Math.floor(actor.localFrame * 64);
    const phaseIndex = (phase >> 12) & 3;
    const cosine = Math.trunc(Math.cos((phase & 4095) * Math.PI / 2048) * 4096);
    const blend = ((cosine >> 1) + 2048) / 4096;
    for (const mesh of actor.visibleMeshes) {
      if (actor.hidden.has(mesh.owner) || mesh.drawNodes?.some(node => actor.hidden.has(node))) continue;
      gl.bindVertexArray(mesh.vao);
      for (const batch of mesh.batches) {
        const material = actor.model.materials[batch.material];
        const alpha = wire ? null : material.gs?.alpha ??
          (material.blend === 1 ? 0x44 : material.blend === 2 ? 0x48 : null);
        const packet = {actor, mesh, batch, material, phaseIndex, blend, wire, alpha};
        if (alpha) packets.push(packet);
        else this.drawBatch(packet, u);
      }
    }
  }

  drawBatch({actor, batch, material, phaseIndex, blend, wire, alpha}, u) {
    const gl = this.gl;
    const override = actor.appearance?.textures[batch.material];
    const cycle = Array.isArray(override) ? override : null;
    const textureIndex = cycle ? cycle[phaseIndex] : override ?? material.texture;
    const texture = this.options.texture && !wire ? actor.textures[textureIndex] : null;
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, texture || this.neutralTexture);
    gl.activeTexture(gl.TEXTURE1);
    gl.bindTexture(gl.TEXTURE_2D, texture && cycle ? actor.textures[cycle[(phaseIndex + 1) & 3]] : this.neutralTexture);
    gl.uniform1f(u.uPaletteBlend, texture && cycle ? blend : 0);
    gl.uniform1i(u.uTextured, Boolean(texture));
    gl.uniform1i(u.uLit, this.options.light && !wire && [1, 2, 6, 7].includes(material.kind));
    gl.uniform1i(u.uNative, Boolean(material.gs && !wire));
    gl.uniform1i(u.uNativeBlend, Boolean(alpha));
    if (material.gs) gl.uniform2i(u.uTextureFunction, material.gs.tfx, material.gs.tcc);
    gl.uniform4fv(u.uTint, wire ? [0.42, 0.75, 0.86, 1] : material.tint);
    gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, wire ? batch.edges : batch.faces);
    gl.drawElements(wire ? gl.LINES : gl.TRIANGLES, batch.count * (wire ? 2 : 1), gl.UNSIGNED_INT, 0);
  }

  collectColliders() {
    const layers = {body: [], extra: [], attack: []};
    for (const actor of [this, this.opponent]) {
      if (!actor || actor.sceneVisible === false || actor.nativeVisible === false) continue;
      const seen = new Set(), frame = Math.floor(actor.localFrame + 1e-7);
      for (const event of actor.localEvents) {
        const shape = event.shape;
        if (!shape?.radius || !activeAt(event, frame) || actor.hidden.has(shape.node)) continue;
        const node = shape.node < 0 ? actor.placement : actor.world[shape.node];
        if (shape.node >= 0 && !node) continue;
        const key = [shape.layer, shape.node, shape.template].join('/');
        if (seen.has(key)) continue;
        seen.add(key);
        const from = node ? point(node, shape.center) : shape.center;
        const to = shape.to ? (node ? point(node, shape.to) : shape.to) : from;
        const offset = minus(to, from);
        const capsule = dot(offset, offset) > 1e-8;
        const axis = capsule ? unit(offset) : [0, 1, 0];
        const side = unit(cross(axis, Math.abs(axis[1]) > 0.9 ? [1, 0, 0] : [0, 1, 0]));
        layers[shape.layer].push({from, to, axis, side, across: cross(axis, side), radius: shape.radius, capsule});
      }
    }
    return layers;
  }

  drawColliders(view) {
    const layers = this.colliderVolumes ??= this.collectColliders();
    const gl = this.gl, {program, u} = this.colliderProgram;
    const mesh = this.colliderMesh;
    gl.useProgram(program);
    gl.uniformMatrix4fv(u.uView, false, view);
    gl.bindVertexArray(mesh.vao);
    gl.disable(gl.DEPTH_TEST);
    gl.depthMask(false);
    gl.enable(gl.CULL_FACE);
    for (const [layer, color, fill, stroke] of COLLIDER_STYLES) {
      if (!this.colliderLayers[layer]) continue;
      for (const volume of layers[layer]) {
        gl.uniform3fv(u.uFrom, volume.from);
        gl.uniform3fv(u.uTo, volume.to);
        gl.uniform3fv(u.uAxis, volume.axis);
        gl.uniform3fv(u.uSide, volume.side);
        gl.uniform3fv(u.uAcross, volume.across);
        gl.uniform1f(u.uRadius, volume.radius);
        gl.uniform4f(u.uColor, ...color, fill);
        gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, mesh.faceBuffer);
        gl.drawElements(gl.TRIANGLES, mesh.faceCount, gl.UNSIGNED_SHORT, 0);
        gl.uniform4f(u.uColor, ...color, stroke);
        gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, mesh.edgeBuffer);
        gl.drawElements(gl.LINES, volume.capsule ? mesh.capsuleEdges : mesh.sphereEdges, gl.UNSIGNED_SHORT, 0);
      }
    }
    gl.disable(gl.CULL_FACE);
    gl.depthMask(true);
    gl.enable(gl.DEPTH_TEST);
  }
}
