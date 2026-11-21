'use strict';
/* Dev Lab Viewer UI. Vanilla JS, hash routed, hand-rolled SVG charts. Untrusted text (logs, reasons) is only
   ever inserted with textContent, never innerHTML. */

// ---------------------------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------------------------
const NS = 'http://www.w3.org/2000/svg';
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (v === true) el.setAttribute(k, '');
    else el.setAttribute(k, v);
  }
  add(el, kids);
  return el;
}
function s(tag, attrs, ...kids) {
  const el = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) if (v != null && v !== false) el.setAttribute(k, v);
  add(el, kids);
  return el;
}
function add(el, kids) {
  for (const k of kids.flat(Infinity)) {
    if (k == null || k === false) continue;
    el.append(k.nodeType ? k : document.createTextNode(String(k)));
  }
}
function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }
const DASH = '–';

async function api(path) {
  const r = await fetch('/api/' + path, { cache: 'no-store' });
  if (!r.ok) {
    let msg = r.status + '';
    try { msg = (await r.json()).error || msg; } catch (e) { /* keep status */ }
    throw new Error(msg);
  }
  return r.json();
}

function fmtInt(v) { return Number(v).toLocaleString('en-US'); }
function trimNum(x, d) { return Number(x.toFixed(d)).toString(); }
function fmtVal(v, unit) {
  if (v == null || Number.isNaN(v)) return DASH;
  const a = Math.abs(v);
  switch (unit) {
    case 'ns':
      if (a < 1e3) return trimNum(v, 0) + ' ns';
      if (a < 1e6) return trimNum(v / 1e3, 1) + ' µs';
      if (a < 1e9) return trimNum(v / 1e6, 2) + ' ms';
      return trimNum(v / 1e9, 2) + ' s';
    case 'us':
      return a < 1e3 ? trimNum(v, 0) + ' µs' : trimNum(v / 1e3, 2) + ' ms';
    case 'ms':
      if (a < 1) return trimNum(v * 1e3, 0) + ' µs';
      if (a < 1000) return trimNum(v, 2) + ' ms';
      return trimNum(v / 1e3, 2) + ' s';
    case 's':
      if (a < 120) return trimNum(v, 1) + ' s';
      return Math.floor(a / 60) + ' m ' + Math.round(a % 60) + ' s';
    case 'B':
      if (a < 1024) return trimNum(v, 0) + ' B';
      if (a < 1048576) return trimNum(v / 1024, 1) + ' KB';
      return trimNum(v / 1048576, 2) + ' MB';
    case 'MB': return trimNum(v, 0) + ' MB';
    case 'fps': return trimNum(v, 1) + ' fps';
    default:
      if (Number.isInteger(v)) return fmtInt(v);
      return trimNum(Number(v.toPrecision(4)), 6);
  }
}
function fmtDelta(d, unit) {
  if (d == null) return DASH;
  if (d === 0) return '0';
  return (d > 0 ? '+' : '-') + fmtVal(Math.abs(d), unit);
}
function fmtPct(p) {
  if (p == null || !Number.isFinite(p)) return DASH;
  return (p > 0 ? '+' : '') + p.toFixed(1) + '%';
}
function fmtAge(ts, now) {
  if (!ts) return DASH;
  const d = Math.max(0, (now || Date.now() / 1000) - ts);
  if (d < 90) return Math.round(d) + ' s';
  if (d < 5400) return Math.round(d / 60) + ' m';
  if (d < 129600) return Math.round(d / 3600) + ' h';
  return Math.round(d / 86400) + ' d';
}
function fmtDur(sec) {
  if (sec == null) return DASH;
  if (sec < 120) return trimNum(sec, 1) + ' s';
  return Math.floor(sec / 60) + ' m ' + String(Math.round(sec % 60)).padStart(2, '0') + ' s';
}
function pad2(n) { return String(n).padStart(2, '0'); }
function fmtUtc(ts, withSec) {
  if (!ts) return DASH;
  const d = new Date(ts * 1000);
  return pad2(d.getUTCMonth() + 1) + '-' + pad2(d.getUTCDate()) + ' ' + pad2(d.getUTCHours()) + ':' + pad2(d.getUTCMinutes()) +
    (withSec ? ':' + pad2(d.getUTCSeconds()) : '');
}
const sh = (x, n) => (x ? String(x).slice(0, n || 8) : DASH);

function vchip(v, raw, small) {
  const sm = small ? ' sm' : '';
  const t = v || 'UNKNOWN';
  const title = raw && raw !== v ? 'source says ' + raw : null;
  switch (t) {
    case 'PASS': return h('span', { class: 'chip outline-pass' + sm, title }, 'Pass');
    case 'FAIL': case 'ERROR': case 'TIMEOUT': return h('span', { class: 'chip fill-alert' + sm, title }, t === 'FAIL' ? 'Failed' : t);
    case 'INCOMPLETE': return h('span', { class: 'chip outline-flag' + sm, title }, 'Incomplete');
    case 'BLOCKED': return h('span', { class: 'chip plain flag', title }, 'Blocked');
    case 'RUNNING': return h('span', { class: 'chip fill-dim' + sm, title }, 'Running');
    case 'CANCELLED': return h('span', { class: 'chip plain dim', title }, 'Cancelled');
    default: return h('span', { class: 'chip plain dim', title }, 'Unknown');
  }
}
function hstate(st) {
  switch (st) {
    case 'running': return h('span', { class: 'chip fill-dim' }, 'Running');
    case 'idle': return h('span', { class: 'chip plain dim' }, 'Idle');
    case 'paused': return h('span', { class: 'chip outline-flag' }, 'Paused');
    case 'blocked': return h('span', { class: 'chip outline-flag' }, 'Refused');
    case 'unreachable': return h('span', { class: 'chip fill-alert' }, 'Unreachable');
    case 'stale': return h('span', { class: 'chip outline-flag' }, 'Stale');
    default: return h('span', { class: 'chip plain dim' }, st || 'Unknown');
  }
}
function link(href, text, attrs) { return h('a', Object.assign({ href }, attrs || {}), text); }
function runHref(id, base) { return '#/run/' + encodeURIComponent(id) + (base ? '?base=' + encodeURIComponent(base) : ''); }
function empty(msg, tall) { return h('div', { class: 'empty' + (tall ? ' tall' : '') }, msg); }
function skeleton(tall) { return h('div', { class: 'skeleton' + (tall ? ' tall' : '') }, 'Loading'); }
function announce(msg) { const r = document.getElementById('live-region'); if (r) r.textContent = msg; }
function banner(kind, word, ...kids) {
  return h('div', { class: 'banner ' + (kind || ''), role: kind === 'alert' ? 'alert' : null },
    word ? h('span', { class: 'chip ' + (kind === 'alert' ? 'fill-alert' : 'fill-flag') }, word) : null, ...kids);
}
function kvs(pairs) {
  const dl = h('dl', { class: 'kvs' });
  for (const [k, v] of pairs) {
    if (v == null || v === '' || (Array.isArray(v) && !v.length)) continue;
    dl.append(h('dt', {}, k), h('dd', {}, v));
  }
  return dl;
}

// ---------------------------------------------------------------------------------------------
// charts: lines register, 2px strokes, no fill, no smoothing, legend and datum line mandatory
// ---------------------------------------------------------------------------------------------
function niceTicks(lo, hi, n) {
  if (!(hi > lo)) hi = lo + 1;
  const span = hi - lo, raw = span / n, mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 5, 10].map(m => m * mag).find(x => x >= raw) || raw;
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-6; v += step) out.push(Number(v.toPrecision(12)));
  return out;
}
function lineChart(opts) {
  // opts: series [{name, cls, x[], y[]}], unit, xUnit, height, limit
  const W = opts.width || Math.max(320, Math.min(1500, ((document.getElementById('main') || {}).clientWidth || 800) - 48)), H = opts.height || 180, L = 56, R = 8, T = 8, B = 24;
  const ser = opts.series.filter(q => q.y && q.y.some(v => v != null));
  const svg = s('svg', { class: 'chart', viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': opts.label || 'line chart' });
  if (!ser.length) return empty('No samples to draw.');
  let xs = [], ys = [];
  ser.forEach(q => { q.x.forEach(v => v != null && xs.push(v)); q.y.forEach(v => v != null && ys.push(v)); });
  if (opts.limit != null) ys.push(opts.limit);
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  const y0 = opts.yMin != null ? opts.yMin : Math.min(0, ...ys), y1 = Math.max(...ys) * 1.05 || 1;
  const px = v => L + (x1 > x0 ? (v - x0) / (x1 - x0) : 0) * (W - L - R);
  const py = v => T + (1 - (v - y0) / (y1 - y0 || 1)) * (H - T - B);
  niceTicks(y0, y1, 4).forEach(t => {
    svg.append(s('line', { class: 'gridl', x1: L, x2: W - R, y1: py(t), y2: py(t) }));
    svg.append(s('text', { x: L - 8, y: py(t) + 3, 'text-anchor': 'end' }, trimNum(t, 2)));
  });
  svg.append(s('line', { class: 'ax', x1: L, x2: L, y1: T, y2: H - B }));
  svg.append(s('line', { class: 'ax', x1: L, x2: W - R, y1: py(Math.max(0, y0)), y2: py(Math.max(0, y0)) }));
  niceTicks(x0, x1, 5).forEach(t => svg.append(s('text', { x: px(t), y: H - 8, 'text-anchor': 'middle' }, trimNum(t, 1))));
  if (opts.limit != null) svg.append(s('line', { class: 'limit', x1: L, x2: W - R, y1: py(opts.limit), y2: py(opts.limit) }));
  ser.forEach((q, i) => {
    let d = '', pen = false;
    for (let j = 0; j < q.y.length; j++) {
      if (q.y[j] == null) { pen = false; continue; }
      d += (pen ? 'L' : 'M') + px(q.x[j]).toFixed(1) + ' ' + py(q.y[j]).toFixed(1);
      pen = true;
    }
    svg.append(s('path', { class: 's ' + (q.cls || 's' + i), d }));
  });
  const wrap = h('div', {}, svg);
  const lg = h('div', { class: 'legend' });
  ser.forEach((q, i) => lg.append(h('span', {}, h('i', { class: 'sw ' + (q.cls || 's' + i) }), q.name)));
  if (opts.limit != null) lg.append(h('span', {}, h('i', { class: 'sw lim' }), 'gate ' + fmtVal(opts.limit, opts.unit)));
  lg.append(h('span', {}, 'y ' + (opts.unit || 'value') + ' · x ' + (opts.xUnit || 'sample')));
  wrap.append(lg);
  if (opts.datum) wrap.append(h('div', { class: 'datum' }, opts.datum));
  return wrap;
}
function miniLine(values, unit) {
  const W = 200, H = 40, vals = values.filter(v => v != null);
  if (vals.length < 2) return h('div', { class: 'datum' }, 'waiting for samples');
  const lo = Math.min(...vals), hi = Math.max(...vals), rng = hi - lo || 1;
  let d = '';
  values.forEach((v, i) => {
    if (v == null) return;
    d += (d ? 'L' : 'M') + (i / (values.length - 1) * W).toFixed(1) + ' ' + (H - 4 - (v - lo) / rng * (H - 8)).toFixed(1);
  });
  return s('svg', { class: 'chart', viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': 'last ' + values.length + ' samples' },
    s('line', { class: 'ax', x1: 0, x2: W, y1: H - 1, y2: H - 1 }), s('path', { class: 's s0', d }));
}
function phaseStrip(items, opts) {
  // items [{label, dur, cls}]; width proportional to duration
  opts = opts || {};
  const tot = items.reduce((a, b) => a + Math.max(b.dur || 0, 0.05), 0) || 1;
  const strip = h('div', { class: 'strip', role: 'img', 'aria-label': opts.label || 'timeline' });
  items.forEach(it => {
    const i = h('i', { class: it.cls || '', style: 'flex:' + (Math.max(it.dur || 0, 0.05) / tot * 1000).toFixed(1) + ' 1 0',
      'data-tip': it.label + ' · ' + fmtDur(it.dur) });
    strip.append(i);
  });
  return strip;
}

// tooltip for [data-tip]
const tip = h('div', { class: 'tip', hidden: true });
document.body.append(tip);
document.addEventListener('pointermove', e => {
  const t = e.target.closest && e.target.closest('[data-tip]');
  if (!t) { tip.hidden = true; return; }
  tip.textContent = t.getAttribute('data-tip');
  tip.hidden = false;
  tip.style.left = Math.min(window.innerWidth - 380, e.clientX + 12) + 'px';
  tip.style.top = (e.clientY + 16) + 'px';
});
document.addEventListener('pointerleave', () => { tip.hidden = true; });

// ---------------------------------------------------------------------------------------------
// router
// ---------------------------------------------------------------------------------------------
const main = document.getElementById('main');
let current = null;
function hashParts() {
  const raw = location.hash || '#/';
  const q = raw.indexOf('?');
  return { path: q < 0 ? raw : raw.slice(0, q), query: new URLSearchParams(q < 0 ? '' : raw.slice(q + 1)) };
}
function setQuery(path, params) {
  const q = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => { if (v != null && v !== '') q.set(k, v); });
  const next = path + (q.toString() ? '?' + q : '');
  history.replaceState(null, '', next);
}
function band(eyebrow, title, lede, crumbs) {
  return h('div', { class: 'band' },
    crumbs ? h('div', { class: 'crumbs' }, crumbs) : h('div', { class: 'k' }, eyebrow),
    h('h1', {}, title), lede ? h('p', { class: 'lede' }, lede) : null);
}
function provenance(extra) {
  return h('footer', { class: 'prov' }, 'Dev Lab Viewer · read-only · times are UTC · ' + (extra || ''));
}
const routes = [
  [/^#\/?$/, 'fleet', pageFleet],
  [/^#\/runs$/, 'runs', pageRuns],
  [/^#\/run\/(.+)$/, 'runs', pageRun],
  [/^#\/compare$/, 'compare', pageCompare],
  [/^#\/history$/, 'history', pageHistory],
];
async function render() {
  if (current && current.destroy) current.destroy();
  const { path, query } = hashParts();
  const hit = routes.find(r => r[0].test(path));
  const ctx = { timers: [], query, destroy() { this.timers.forEach(t => clearInterval(t)); this.timers = []; } };
  current = ctx;
  document.querySelectorAll('#nav a').forEach(a => {
    if (hit && a.dataset.nav === hit[1]) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
  });
  moveMarker();
  clear(main).append(skeleton(true));
  window.scrollTo(0, 0);
  if (!hit) { clear(main).append(band('', 'Not found'), empty('No such page.')); return; }
  const m = path.match(hit[0]);
  try {
    await hit[2](ctx, m[1] ? decodeURIComponent(m[1]) : null);
  } catch (e) {
    if (current !== ctx) return;
    clear(main).append(band('', 'Error'), banner('alert', 'Failed', 'Could not load this page: ' + e.message));
  }
}
function moveMarker() {
  const a = document.querySelector('#nav a[aria-current=page]'), mk = document.getElementById('marker');
  if (!a) { mk.style.width = '0'; return; }
  mk.style.width = a.offsetWidth + 'px';
  mk.style.transform = 'translateX(' + a.offsetLeft + 'px)';
}
window.addEventListener('hashchange', render);
window.addEventListener('resize', moveMarker);

// collector feed indicator in the masthead
let lastStatus = null;
async function pollStatus() {
  const feed = document.getElementById('feed');
  try {
    const st = await api('status');
    lastStatus = st;
    const upd = st.collector && st.collector.updated_ts;
    const iv = (st.collector && st.collector.poll_interval_s) || 60;
    const age = upd ? st.now - upd : null;
    const stale = age == null || age > iv * 3;
    feed.textContent = 'collector ' + (age == null ? 'no data' : fmtAge(upd, st.now) + ' ago') + ' · ' + fmtInt(st.runs) + ' runs';
    feed.className = 'feed' + (stale ? ' stale' : '');
  } catch (e) {
    feed.textContent = 'server feed failed: ' + e.message;
    feed.className = 'feed stale';
  }
}
setInterval(pollStatus, 10000);

// ---------------------------------------------------------------------------------------------
// fleet page
// ---------------------------------------------------------------------------------------------
function liveBox(host, live) {
  const box = h('div', { class: 'livebox' });
  if (!live || !live.active) { box.append(empty('Live feed is idle: the host reports no running job.')); return box; }
  const j = live.job || {};
  box.append(h('div', { class: 'rowline small' },
    h('span', { class: 'k' }, 'Live'), h('span', { class: 'mono' }, (j.recipe || 'job') + ' · '),
    h('span', { class: 'mono', 'data-since': j.started }, ''),
    h('span', { class: 'spacer' }), h('span', { class: 'datum' }, (live.source_type || 'job output') + ' · updated ' + fmtAge(live.updated_ts) + ' ago')));
  const p = live.progress;
  if (live.kind === 'harness' && p && p.t_ms && p.t_ms.length) {
    const last = live.last || {};
    const tail = a => (a || []).slice(-90);
    const mk = (label, key, unit, readout) => h('div', { class: 'mini' }, h('div', { class: 'k' }, label),
      h('div', { class: 'v' }, readout), miniLine(tail(p[key]), unit));
    box.append(h('div', { class: 'minis' },
      mk('fps', 'fps', 'fps', last.fps != null ? trimNum(last.fps, 1) : DASH),
      mk('frame p95', 'frame_ms_p95', 'ms', last.frame_ms_p95 != null ? trimNum(last.frame_ms_p95, 1) + ' ms' : DASH),
      mk('heap used', 'heap_used_mb', 'MB', last.heap_used_mb != null ? trimNum(last.heap_used_mb, 0) + ' MB' : DASH)));
    box.append(h('div', { class: 'datum' }, 'n=' + p.t_ms.length + ' samples · phase ' + (last.phase || DASH) + ' · step ' +
      (last.step != null ? last.step + ' ' + (last.step_type || '') : DASH) + (last.measure_label ? ' · measuring ' + last.measure_label : '') +
      (last.chunks_rendered != null ? ' · chunks ' + last.chunks_rendered : '')));
  }
  const tl = live.timeline || [];
  if (tl.length) {
    const items = tl.map((e, i) => ({ label: e.phase, dur: e.dwell_s != null ? e.dwell_s : 1, cls: i === tl.length - 1 ? 'cur' : '' }));
    box.append(phaseStrip(items, { label: 'phase timeline' }));
    const cur = tl[tl.length - 1];
    box.append(h('div', { class: 'datum' }, 'n=' + tl.length + ' phases · current ' + cur.phase +
      (cur.ts ? ' for ' + fmtAge(cur.ts) : '') + (cur.feature_ms != null ? ' · feature clock ' + fmtVal(cur.feature_ms, 'ms') : '')));
  }
  if (live.shots && live.shots.length) box.append(h('div', { class: 'datum' }, 'screenshots so far: ' + live.shots.length));
  (live.errors || []).forEach(e => box.append(h('div', { class: 'flag small mono' }, e)));
  if (live.log_tail && live.log_tail.length) {
    const lb = h('div', { class: 'logbox', tabindex: '0', 'aria-label': 'log tail' });
    live.log_tail.slice(-60).forEach(ln => {
      const m = ln.match(/\/(ERROR|WARN|FATAL|INFO|DEBUG)\]/);
      lb.append(h('div', { class: m ? m[1] : '' }, ln));
    });
    box.append(lb);
    setTimeout(() => { lb.scrollTop = lb.scrollHeight; }, 0);
    box.append(h('div', { class: 'datum' }, 'log tail from ' + (live.log_file || 'job output') + ' · last ' + Math.min(60, live.log_tail.length) + ' lines'));
  }
  return box;
}

function hostCard(hst, now) {
  const c = h('section', { class: 'cell', 'data-host': hst.id });
  c.append(h('h2', {}, hst.id, hstate(hst.state)));
  c.append(h('div', { class: 'datum' }, [hst.hardware, hst.platform].filter(Boolean).join(' · ') + (hst.owner_machine ? ' · owner machine, reads only' : '')));
  if (hst.native_queue) {
    const nq = hst.native_queue;
    c.append(h('div', { class: 'datum' }, nq.status === 'OBSERVED'
      ? 'Dispatch queue: ' + nq.queued_eligible + ' eligible · ' + nq.active_requests + ' active requests'
      : 'Dispatch queue: unavailable'));
    (nq.jobs || []).slice(0, 6).forEach(j => c.append(h('div', { class: 'datum mono' },
      (j.priority || 'normal') + ' · ' + j.status + ' · ' + sh(j.id, 10))));
  }
  const cand = hst.candidate;
  c.append(kvs([
    ['Candidate', cand ? h('span', {}, h('span', { class: 'ink' }, cand.label || DASH), ' · commit ' + sh(cand.commit, 9) + ' · jar ' + sh(cand.jar_sha256, 10)) : DASH],
    ['Installed', cand && cand.as_of ? cand.as_of.replace('T', ' ').slice(0, 19) + ' UTC' : null],
  ]));
  if (hst.state === 'running' && hst.job) {
    c.append(h('div', { class: 'rowline' }, h('span', { class: 'k' }, 'Running'),
      h('span', { class: 'mono' }, hst.job.recipe + ' '), h('span', { class: 'mono ink', 'data-since': hst.job.started }, ''),
      h('span', { class: 'datum' }, hst.job.request_id)));
    c.append(h('div', { class: 'livehost', 'data-live': hst.id }, liveBox(hst.id, null)));
  } else if (hst.controller && ['stale', 'blocked', 'paused'].includes(hst.state)) {
    c.append(banner('', 'Harness controller', hst.controller.reason));
  } else if (hst.state === 'stale' && hst.job) {
    c.append(banner('', 'Heartbeat lost', 'Job ' + hst.job.recipe + ' (' + hst.job.request_id + ') has not updated in over 15 s. The node keeps the slot reserved; nothing is relaunched.'));
  } else if (hst.state === 'paused') {
    c.append(banner('', 'Paused', 'Pause file present: ' + hst.paused_by + '. Dev Lab refuses new client runs until it is removed.'));
  } else if (hst.state === 'unreachable') {
    c.append(banner('alert', 'Unreachable', 'Last seen ' + fmtAge(hst.last_seen_ts, now) + ' ago. ' + (hst.evidence_error || hst.api_error || '')));
  }
  if (hst.state !== 'unreachable') {
    if (hst.evidence_error) c.append(banner('', 'Evidence feed', hst.evidence_error));
    else if (hst.api_error) c.append(banner('', 'Dev Lab API', hst.api_error));
  }
  (hst.source_errors || []).slice(0, 2).forEach(e => c.append(h('div', { class: 'flag small mono' }, e)));
  if (hst.wrapper_refusals && hst.wrapper_refusals.length) {
    c.append(h('div', { class: 'datum' }, hst.wrapper_refusals.length + ' wrapper run(s) refused or failed before an evidence dir existed'));
  }
  const gates = (hst.recipes || []).filter(r => r.kind === 'client' || r.gate === 'c39-frame-metrics');
  const ops = (hst.recipes || []).filter(r => !(r.kind === 'client' || r.gate === 'c39-frame-metrics'));
  const tbl = (rows, title) => {
    if (!rows.length) return null;
    const t = h('table', { class: 'tbl' }, h('thead', {}, h('tr', {}, h('th', {}, title), h('th', {}, 'Verdict'), h('th', {}, 'Candidate'), h('th', { class: 'num' }, 'Age'))), h('tbody', {}));
    rows.forEach(r => t.tBodies[0].append(h('tr', { class: 'click' },
      h('td', {}, link(runHref(r.run_id), r.key), r.reason && r.verdict !== 'PASS' ? h('span', { class: 'sub' }, r.reason.slice(0, 90)) : null),
      h('td', {}, vchip(r.verdict)), h('td', { class: 'mono' }, r.label || DASH), h('td', { class: 'num' }, fmtAge(r.ts, now)))));
    return h('div', { class: 'tblwrap' }, t);
  };
  const gt = tbl(gates, 'Last verdict per gate');
  if (gt) c.append(gt); else c.append(empty('No gate runs mirrored for this host yet.'));
  if (ops.length) c.append(h('details', {}, h('summary', {}, 'Candidate and control recipes (' + ops.length + ')'), tbl(ops, 'Recipe')));
  c.append(h('div', { class: 'datum' }, 'n=' + hst.run_count + ' runs mirrored · polled ' + fmtAge(hst.checked_ts, now) + ' ago'));
  return c;
}

function tickSince() {
  document.querySelectorAll('[data-since]').forEach(el => {
    const t = parseFloat(el.getAttribute('data-since'));
    el.textContent = t ? fmtDur(Date.now() / 1000 - t) : DASH;
  });
}

async function pageFleet(ctx) {
  const grid = h('div', { class: 'cells' });
  const top = h('div', {});
  const recent = h('div', { class: 'section' });
  clear(main).append(band('Fleet', 'Fleet board',
    'State of every test host, the candidate each one holds, the last verdict per gate, and live progress while a run is in flight. Evidence is mirrored read-only from the nodes.'),
    top, h('div', { class: 'section' }, grid), recent, provenance());
  let lastSig = '', lastFleet = null;
  async function loadFleet() {
    const f = await api('fleet');
    lastFleet = f;
    const sig = JSON.stringify(f.hosts) + JSON.stringify(f.collector);
    renderTop(f);
    if (sig === lastSig) return;
    lastSig = sig;
    clear(grid);
    f.hosts.forEach(hs => grid.append(hostCard(hs, f.generated_ts)));
    if (!f.hosts.length) grid.append(empty('The collector has not written any host yet. It polls every 60 s.'));
    tickSince();
    pollLive();
  }
  function renderTop(f) {
    clear(top);
    const iv = (f.collector && f.collector.poll_interval_s) || 60;
    const upd = f.collector && f.collector.updated_ts;
    const msgs = [];
    if (!upd || f.generated_ts - upd > iv * 3) msgs.push('Collector last wrote ' + (upd ? fmtAge(upd, f.generated_ts) + ' ago' : 'never') + '. Everything below is as of then.');
    f.hosts.filter(x => x.state === 'unreachable').forEach(x => msgs.push(x.id + ' did not answer: ' + (x.evidence_error || x.api_error || 'no detail')));
    if (msgs.length) top.append(banner('', 'Feed', h('ul', {}, msgs.map(m => h('li', {}, m)))));
  }
  async function pollLive() {
    if (!lastFleet) return;
    for (const hs of lastFleet.hosts) {
      const slot = grid.querySelector('[data-live="' + hs.id + '"]');
      if (!slot) continue;
      try {
        const lv = await api('live/' + encodeURIComponent(hs.id) + '/current');
        if (current !== ctx) return;
        clear(slot).append(liveBox(hs.id, lv));
        tickSince();
      } catch (e) { clear(slot).append(banner('', 'Live feed', 'failed: ' + e.message + '. Showing no live data.')); }
    }
  }
  async function loadRecent() {
    const r = await api('runs?limit=14&gates=1');
    clear(recent).append(h('h2', {}, 'Latest gate runs'), runsTable(r.runs, { compact: true }),
      h('div', { class: 'rowline', style: 'margin-top:12px' }, link('#/runs', 'All runs')));
  }
  await loadFleet();
  loadRecent().catch(e => clear(recent).append(banner('', 'Runs', e.message)));
  ctx.timers.push(setInterval(() => loadFleet().catch(e => announce('fleet refresh failed: ' + e.message)), 5000));
  ctx.timers.push(setInterval(pollLive, 2000));
  ctx.timers.push(setInterval(tickSince, 1000));
  ctx.timers.push(setInterval(() => loadRecent().catch(() => 0), 20000));
}

// ---------------------------------------------------------------------------------------------
// runs list
// ---------------------------------------------------------------------------------------------
function keyMetricCell(r) {
  const km = (r.key_metrics || [])[0];
  if (!km) return h('td', { class: 'num dim' }, DASH);
  return h('td', { class: 'num' }, fmtVal(km.value, km.unit), h('span', { class: 'sub' }, km.name));
}
function runsTable(runs, opts) {
  opts = opts || {};
  const now = Date.now() / 1000;
  const t = h('table', { class: 'tbl' }, h('thead', {}, h('tr', {},
    h('th', {}, 'Started (UTC)'), h('th', {}, 'Host'), h('th', {}, 'Gate'), h('th', {}, 'Candidate'), h('th', {}, 'Verdict'),
    h('th', { class: 'num' }, 'Duration'), h('th', { class: 'num' }, 'Key metric'), opts.compact ? null : h('th', {}, 'Reason'))), h('tbody', {}));
  if (!runs.length) return empty('No runs match. Loosen a filter, or wait for the next collector pass.');
  runs.forEach(r => {
    const tr = h('tr', { class: 'click' },
      h('td', {}, link(runHref(r.id), fmtUtc(r.started_ts)), h('span', { class: 'sub' }, fmtAge(r.started_ts, now) + ' ago')),
      h('td', { class: 'mono' }, r.host),
      h('td', {}, r.gate, r.recipe && r.recipe !== r.gate ? h('span', { class: 'sub' }, r.recipe) : null),
      h('td', { class: 'mono' }, (r.candidate && r.candidate.label) || DASH, r.candidate && r.candidate.commit ? h('span', { class: 'sub' }, sh(r.candidate.commit, 9)) : null),
      h('td', {}, vchip(r.verdict, r.verdict_raw)),
      h('td', { class: 'num' }, fmtDur(r.duration_s)), keyMetricCell(r),
      opts.compact ? null : h('td', { class: 'wrap', style: 'max-width:420px' }, r.reason || ''));
    tr.addEventListener('click', e => { if (!e.target.closest('a')) location.hash = runHref(r.id); });
    t.tBodies[0].append(tr);
  });
  return h('div', { class: 'tblwrap' }, t);
}

async function pageRuns(ctx) {
  const data = await api('runs?limit=2000');
  const all = data.runs;
  const q = ctx.query;
  const st = { host: q.get('host') || '', gate: q.get('gate') || '', verdict: q.get('verdict') || '', kind: q.get('kind') || 'gates',
    commit: q.get('commit') || '', limit: 48 };
  const uniq = k => Array.from(new Set(all.map(r => r[k]).filter(Boolean))).sort();
  const sel = (label, key, opts) => {
    const s = h('select', { id: 'f-' + key, onchange: e => { st[key] = e.target.value; st.limit = 48; draw(); } },
      h('option', { value: '' }, 'All'), opts.map(o => h('option', { value: o, selected: st[key] === o }, o)));
    return h('div', { class: 'field' }, h('label', { for: 'f-' + key }, label), s);
  };
  const out = h('div', {});
  const filters = h('div', { class: 'filters' },
    sel('Host', 'host', uniq('host')), sel('Gate', 'gate', uniq('gate')),
    sel('Verdict', 'verdict', ['PASS', 'FAIL', 'ERROR', 'TIMEOUT', 'INCOMPLETE', 'BLOCKED', 'CANCELLED', 'RUNNING']),
    h('div', { class: 'field' }, h('label', { for: 'f-kind' }, 'Show'), h('select', { id: 'f-kind', onchange: e => { st.kind = e.target.value; st.limit = 48; draw(); } },
      h('option', { value: 'gates', selected: st.kind === 'gates' }, 'Gate runs'), h('option', { value: 'all', selected: st.kind === 'all' }, 'Everything, incl. installs and pushes'))),
    h('div', { class: 'field' }, h('label', { for: 'f-commit' }, 'Candidate (label, commit or jar prefix)'),
      h('input', { id: 'f-commit', type: 'search', value: st.commit, oninput: e => { st.commit = e.target.value.trim(); st.limit = 48; draw(); } })));
  function draw() {
    setQuery('#/runs', { host: st.host, gate: st.gate, verdict: st.verdict, kind: st.kind === 'gates' ? '' : st.kind, commit: st.commit });
    let rows = all.filter(r => (!st.host || r.host === st.host) && (!st.gate || r.gate === st.gate) && (!st.verdict || r.verdict === st.verdict) &&
      (st.kind === 'all' || r.is_gate) && (!st.commit || [r.candidate && r.candidate.commit, r.candidate && r.candidate.label, r.candidate && r.candidate.jar_sha256]
        .some(x => x && x.toLowerCase().startsWith(st.commit.toLowerCase()))));
    const shown = rows.slice(0, st.limit);
    clear(out).append(runsTable(shown));
    if (rows.length > shown.length) out.append(h('div', { class: 'rowline', style: 'margin-top:12px' },
      h('button', { class: 'btn', onclick: () => { st.limit += 48; draw(); announce('showing ' + Math.min(st.limit, rows.length) + ' of ' + rows.length); } },
        'Show more · ' + (rows.length - shown.length) + ' left')));
    out.append(h('div', { class: 'datum' }, 'n=' + rows.length + ' runs match · ' + all.length + ' mirrored'));
  }
  clear(main).append(band('Runs', 'Runs', 'Every run the collector has mirrored, newest first. Gate runs carry evidence; installs, pushes and status calls are under Everything.'),
    filters, out, provenance());
  draw();
}

// ---------------------------------------------------------------------------------------------
// run page
// ---------------------------------------------------------------------------------------------
const STATE_WORD = { better: 'better', worse: 'worse', same: 'same', within_noise: 'within noise', neutral: 'info', not_comparable: 'n/c', missing_base: 'new', missing_cand: 'gone' };
function stateCell(r) {
  const cls = { better: 'st-better', worse: 'st-worse', same: 'st-same', within_noise: 'st-noise', neutral: 'st-neutral', not_comparable: 'st-nc', missing_base: 'st-missing', missing_cand: 'st-missing' }[r.state] || '';
  let txt = STATE_WORD[r.state] || '';
  const extra = [];
  if (r.state === 'within_noise' && r.noise) extra.push('±' + fmtVal(r.noise.band, r.unit) + ' n=' + r.noise.n);
  if (r.noise && r.noise.band == null && ['better', 'worse'].includes(r.state)) extra.push('noise unknown, n=' + r.noise.n + ' (<3)');
  if (r.over_limit) extra.push('over gate ' + fmtVal(r.limit, r.unit));
  return h('td', { class: cls }, txt, extra.length ? h('span', { class: 'sub' }, extra.join(' · ')) : null);
}

function envBanner(env) {
  if (!env) return null;
  const box = [];
  if (!env.comparable) {
    const hard = env.mismatches.filter(m => m.severity === 'hard');
    box.push(banner('', 'Not comparable',
      h('strong', {}, ' Deltas withheld: the two runs did not measure the same thing.'),
      h('ul', {}, hard.map(m => h('li', { class: 'mono small wrap' }, m.field + ': ' + (m.base == null ? DASH : m.base) + '  vs  ' + (m.cand == null ? DASH : m.cand))))));
  }
  const soft = env.mismatches.filter(m => m.severity === 'soft');
  if (soft.length || env.unverified.length || env.notes.length) {
    box.push(h('div', { class: 'banner dim' }, h('span', { class: 'k' }, 'Environment notes'),
      h('ul', {}, soft.map(m => h('li', { class: 'mono small wrap' }, m.field + ' differs: ' + m.base + ' vs ' + m.cand)),
        env.unverified.map(m => h('li', { class: 'mono small wrap' }, m.field + ' unverified: ' + m.note)),
        env.notes.map(n => h('li', { class: 'mono small wrap' }, n)))));
  }
  return box.length ? h('div', {}, box) : null;
}

function metricsSection(run, cmpRes, baseSel) {
  const sec = h('section', { class: 'section', id: 'metrics' }, h('h2', {}, 'Metrics'));
  const rows = cmpRes ? cmpRes.metrics : run.metrics.map(m => ({ key: (m.segment ? m.segment + ':' : '') + m.name, name: m.name, segment: m.segment, unit: m.unit,
    better: m.better, base: null, cand: m.value, limit: m.limit, role: m.role, state: null, delta: null, check: m.check }));
  if (!rows.length) { sec.append(empty('This run recorded no numeric metrics. Harness runs report them in result.json; feature-plan runs as key=value tokens in result.txt.')); return sec; }
  const metaByKey = {};
  run.metrics.forEach(m => { metaByKey[(m.segment ? m.segment + ':' : '') + m.name] = m; });
  const st = { q: '', changed: false, keyOnly: false, hideConst: true };
  const bar = h('div', { class: 'filters' },
    h('div', { class: 'field' }, h('label', { for: 'm-q' }, 'Find metric'), h('input', { id: 'm-q', type: 'search', oninput: e => { st.q = e.target.value.toLowerCase(); draw(); } })),
    cmpRes ? h('button', { class: 'btn', 'aria-pressed': 'false', onclick: e => { st.changed = !st.changed; e.currentTarget.setAttribute('aria-pressed', st.changed); draw(); } }, 'Worse and better only') : null,
    h('button', { class: 'btn', 'aria-pressed': 'true', onclick: e => { st.hideConst = !st.hideConst; e.currentTarget.setAttribute('aria-pressed', st.hideConst); draw(); } }, 'Hide gate constants'));
  const wrap = h('div', {});
  function draw() {
    const shown = rows.filter(r => (!st.q || r.key.toLowerCase().includes(st.q)) &&
      (!st.changed || r.state === 'better' || r.state === 'worse') && (!st.hideConst || r.role !== 'constant'));
    const t = h('table', { class: 'tbl dense grouped' }, h('thead', {}, h('tr', {}, h('th', { class: 'pin' }, 'Metric'),
      cmpRes ? [h('th', { class: 'num' }, 'Baseline'), h('th', { class: 'num' }, 'Candidate'), h('th', { class: 'num' }, 'Delta'), h('th', { class: 'num' }, '%'), h('th', {}, 'Reading')]
        : [h('th', { class: 'num' }, 'Value'), h('th', {}, 'Better when')])), h('tbody', {}));
    let lastGroup = null;
    shown.forEach(r => {
      const grp = r.segment || r.check || r.name.split('.')[0];
      if (grp !== lastGroup) { lastGroup = grp; t.tBodies[0].append(h('tr', { class: 'grp' }, h('td', { colspan: cmpRes ? 6 : 3 }, grp))); }
      const nm = r.name.startsWith(grp + '.') ? r.name.slice(grp.length + 1) : r.name;
      if (cmpRes) {
        t.tBodies[0].append(h('tr', {}, h('td', { class: 'pin', title: r.key }, nm), h('td', { class: 'num' }, fmtVal(r.base, r.unit)), h('td', { class: 'num ink' }, fmtVal(r.cand, r.unit)),
          h('td', { class: 'num' }, r.state === 'not_comparable' ? DASH : fmtDelta(r.delta, r.unit)), h('td', { class: 'num' }, r.state === 'not_comparable' ? DASH : fmtPct(r.pct)), stateCell(r)));
      } else {
        t.tBodies[0].append(h('tr', {}, h('td', { class: 'pin', title: r.key }, nm), h('td', { class: 'num ink' }, fmtVal(r.cand, r.unit) + (r.limit != null ? '  (gate ' + fmtVal(r.limit, r.unit) + ')' : '')),
          h('td', { class: 'dim' }, r.better || DASH)));
      }
    });
    clear(wrap).append(shown.length ? h('div', { class: 'tblwrap tall' }, t) : empty('No metric matches the filter.'),
      h('div', { class: 'datum' }, 'n=' + shown.length + ' of ' + rows.length + ' metrics' + (cmpRes ? ' · baseline ' + cmpRes.base + ' · candidate ' + cmpRes.cand : ' · run ' + run.id) +
        (cmpRes ? ' · noise band = 2 standard deviations of same-code runs, needs n>=3' : '')));
  }
  sec.append(baseSel);
  if (cmpRes) {
    const eb = envBanner(cmpRes.env);
    if (eb) sec.append(eb);
    const sm = cmpRes.summary;
    sec.append(h('div', { class: 'rowline', style: 'margin-top:12px' },
      h('span', { class: 'chip outline-pass sm' }, sm.better + ' better'), h('span', { class: 'chip outline-flag sm' }, sm.worse + ' worse'),
      h('span', { class: 'chip plain dim' }, sm.within_noise + ' within noise'), h('span', { class: 'chip plain dim' }, sm.same + ' same'),
      sm.not_comparable ? h('span', { class: 'chip plain flag' }, sm.not_comparable + ' not comparable') : null));
  }
  sec.append(bar, wrap);
  draw();
  return sec;
}

function stepsSection(run) {
  const sec = h('section', { class: 'section' }, h('h2', {}, run.steps && run.steps.length ? 'Step timeline' : 'Phase timeline'));
  if (run.steps && run.steps.length) {
    const items = run.steps.map(x => ({ label: '#' + x.index + ' ' + x.type + ' ' + x.status, dur: (x.duration_ms || 0) / 1000,
      cls: x.status === 'FAIL' ? 'bad' : x.status === 'SKIP' ? 'skip' : '' }));
    sec.append(phaseStrip(items, { label: 'harness steps' }));
    const t = h('table', { class: 'tbl dense' }, h('thead', {}, h('tr', {}, h('th', {}, '#'), h('th', {}, 'Step'), h('th', {}, 'Status'), h('th', { class: 'num' }, 'Duration'), h('th', {}, 'Message'))), h('tbody', {}));
    run.steps.forEach(x => t.tBodies[0].append(h('tr', {}, h('td', { class: 'num' }, x.index), h('td', { title: JSON.stringify(x.args || {}) }, x.type),
      h('td', {}, vchip(x.status === 'PASS' ? 'PASS' : x.status === 'FAIL' ? 'FAIL' : 'CANCELLED', null, true), h('span', { class: 'sr' }, x.status)),
      h('td', { class: 'num' }, fmtVal(x.duration_ms, 'ms')), h('td', { class: 'wrap' }, x.message || ''))));
    sec.append(h('div', { class: 'tblwrap tall', style: 'margin-top:12px' }, t), h('div', { class: 'datum' }, 'n=' + run.steps.length + ' steps · width in the strip is step duration'));
  } else if (run.timeline && run.timeline.length) {
    const tl = run.timeline;
    sec.append(phaseStrip(tl.map(e => ({ label: e.phase, dur: e.dwell_s != null ? e.dwell_s : 1 })), { label: 'feature-plan phases' }));
    const top = tl.filter(e => e.dwell_s != null).sort((a, b) => b.dwell_s - a.dwell_s).slice(0, 8);
    const max = top.length ? top[0].dwell_s : 1;
    const bars = h('div', { class: 'bars', style: 'margin-top:12px' });
    top.forEach(e => bars.append(h('span', {}, e.phase), h('span', { class: 'b', style: 'width:' + Math.max(2, e.dwell_s / max * 100) + '%' }), h('span', { class: 'num' }, fmtDur(e.dwell_s))));
    sec.append(bars, h('div', { class: 'datum' }, 'n=' + tl.length + ' phases from progress.txt · longest ' + top.length + ' shown · total ' + fmtDur(tl.reduce((a, b) => a + (b.dwell_s || 0), 0))));
  } else {
    sec.append(empty('No step list or phase timeline. A harness run writes steps to result.json; a feature-plan run writes probe/progress.txt.'));
  }
  return sec;
}

async function seriesSection(run, baseId) {
  const sec = h('section', { class: 'section' }, h('h2', {}, 'Frame-time series'));
  const labels = Object.keys(run.series || {});
  let baseRun = null;
  if (baseId) { try { baseRun = (await api('run/' + encodeURIComponent(baseId) + '?base=none')).run; } catch (e) { /* no overlay */ } }
  if (!labels.length && !(run.progress && run.progress.t_ms)) {
    sec.append(empty('This run has no frame-time series. Harness results carry one per measure segment (series in result.json, progress.jsonl); feature-plan results carry p50 and p95 summaries only, listed under Metrics.'));
    return sec;
  }
  labels.forEach(lb => {
    const s0 = run.series[lb], iv = (s0.interval_ms || 1000) / 1000;
    const series = [{ name: 'candidate ' + lb, cls: 's0', x: s0.frame_ms.map((_, i) => i * iv), y: s0.frame_ms }];
    const bs = baseRun && baseRun.series && baseRun.series[lb];
    if (bs) { const bi = (bs.interval_ms || 1000) / 1000; series.push({ name: 'baseline ' + lb, cls: 's1', x: bs.frame_ms.map((_, i) => i * bi), y: bs.frame_ms }); }
    const p95 = (run.metrics.find(m => m.segment === lb && m.name === 'frame_ms_p95') || {}).value;
    sec.append(h('h3', { style: 'margin-top:16px' }, lb), lineChart({ series, unit: 'ms', xUnit: 's', label: 'frame time ' + lb,
      datum: 'n=' + s0.frame_ms.length + ' downsampled frames · run ' + run.id + (bs ? ' · baseline ' + baseId : ' · no baseline series for this segment') +
        (p95 != null ? ' · p95 ' + fmtVal(p95, 'ms') : '') }));
  });
  const pr = run.progress;
  if (pr && pr.t_ms) {
    const x = pr.t_ms.map(v => v / 1000);
    const bp = baseRun && baseRun.progress;
    const ch = (title, key, unit) => {
      if (!pr[key]) return;
      const series = [{ name: 'candidate', cls: 's0', x, y: pr[key] }];
      if (bp && bp[key]) series.push({ name: 'baseline', cls: 's1', x: bp.t_ms.map(v => v / 1000), y: bp[key] });
      sec.append(h('h3', { style: 'margin-top:16px' }, title), lineChart({ series, unit, xUnit: 's since harness start', label: title,
        datum: 'n=' + x.length + ' one-second windows from progress.jsonl · run ' + run.id }));
    };
    ch('Frame time p95 per second', 'frame_ms_p95', 'ms'); ch('FPS per second', 'fps', 'fps'); ch('Heap used', 'heap_used_mb', 'MB');
  }
  return sec;
}

function galleryLightbox(shots, runId, start, baseRun, baseId) {
  let i = start, showBase = false;
  const box = h('div', { class: 'lightbox', role: 'dialog', 'aria-modal': 'true', 'aria-label': 'Screenshot' });
  const stage = h('div', { class: 'stage' }), title = h('span', { class: 'mono small' }, '');
  function url(rid, sh) { return '/files/' + encodeURIComponent(rid) + '/' + (sh.full || sh.thumb); }
  function show() {
    const sh = shots[i];
    title.textContent = (i + 1) + ' / ' + shots.length + ' · ' + sh.name + (sh.width ? ' · ' + sh.width + 'x' + sh.height : '') + (sh.full ? '' : ' · full size not mirrored, showing thumbnail');
    const bsh = baseRun ? (baseRun.screenshots || []).find(x => x.name === sh.name) : null;
    btnBase.disabled = !bsh;
    btnBase.setAttribute('aria-pressed', showBase && !!bsh);
    clear(stage);
    if (showBase && bsh) stage.append(h('div', { class: 'pair' }, h('figure', {}, h('div', { class: 'k' }, 'Baseline ' + baseId), h('img', { src: url(baseId, bsh), alt: 'baseline ' + sh.name })),
      h('figure', {}, h('div', { class: 'k' }, 'Candidate ' + runId), h('img', { src: url(runId, sh), alt: 'candidate ' + sh.name }))));
    else stage.append(h('img', { src: url(runId, sh), alt: sh.name }));
  }
  const btnBase = h('button', { class: 'btn', onclick: () => { showBase = !showBase; show(); } }, 'Side by side with baseline');
  const close = () => { document.removeEventListener('keydown', onKey); box.remove(); };
  function onKey(e) {
    if (e.key === 'Escape') close();
    else if (e.key === 'ArrowRight') { i = (i + 1) % shots.length; show(); }
    else if (e.key === 'ArrowLeft') { i = (i - 1 + shots.length) % shots.length; show(); }
  }
  document.addEventListener('keydown', onKey);
  box.append(h('div', { class: 'bar' }, h('button', { class: 'btn', onclick: () => { i = (i - 1 + shots.length) % shots.length; show(); } }, 'Prev'),
    h('button', { class: 'btn', onclick: () => { i = (i + 1) % shots.length; show(); } }, 'Next'), btnBase, title, h('span', { class: 'spacer' }),
    h('button', { class: 'btn accent', onclick: close }, 'Close')), stage);
  document.body.append(box);
  show();
}

async function gallerySection(run, baseId) {
  const sec = h('section', { class: 'section' }, h('h2', {}, 'Screenshots'));
  const shots = run.screenshots || [];
  if (!shots.length) {
    sec.append(empty(run.evidence && run.evidence.screenshot_files ? 'Screenshots exist on the node (' + run.evidence.screenshot_files + ') but are not mirrored yet.' : 'This run produced no screenshots.'));
    return sec;
  }
  let baseRun = null;
  if (baseId) { try { baseRun = (await api('run/' + encodeURIComponent(baseId) + '?base=none')).run; } catch (e) { /* none */ } }
  const g = h('div', { class: 'gallery' });
  shots.forEach((x, i) => g.append(h('button', { onclick: () => galleryLightbox(shots, run.id, i, baseRun, baseId), 'aria-label': 'Open ' + x.name },
    h('img', { src: '/files/' + encodeURIComponent(run.id) + '/' + x.thumb, loading: 'lazy', alt: x.name, width: 320, height: 200 }), x.name)));
  sec.append(g, h('div', { class: 'datum' }, 'n=' + shots.length + ' screenshots · run ' + run.id + (baseRun ? ' · ' + (baseRun.screenshots || []).length + ' in baseline for side by side' : '') +
    (shots.some(x => !x.full) ? ' · some full-size copies skipped by the per-run storage cap' : '')));
  return sec;
}

function crashSection(run) {
  const c = run.crash;
  if (!c) return null;
  const body = [];
  body.push(kvs([
    ['Exception', c.exception ? h('span', { class: 'ink' }, c.exception) : 'fatal marker without a Java exception'],
    ['Message', c.message], ['Root cause', c.root_cause],
    ['Culprit guess', c.culprit && c.culprit.mod ? h('span', {}, h('span', { class: 'ink' }, c.culprit.mod), ' (' + c.culprit.kind + ') · ' + c.culprit.evidence) : (c.culprit && c.culprit.evidence)],
    ['At log line', c.line],
  ]));
  if (c.markers && c.markers.length) body.push(h('div', { class: 'datum' }, 'markers: ' + c.markers.map(m => m.marker + ' @' + m.line).join(', ')));
  if (c.frames && c.frames.length) body.push(h('details', {}, h('summary', {}, 'First frames'), h('div', { class: 'logbox' }, c.frames.join('\n'))));
  const head = h('div', { class: 'rowline' }, c.fatal ? h('span', { class: 'chip fill-alert' }, 'Crash marker') : h('span', { class: 'chip plain dim' }, 'Exception in log, no crash marker'));
  const inner = h('div', { class: 'panel' }, h('div', { class: 'bd' }, head, ...body));
  const open = c.fatal || ['FAIL', 'ERROR', 'TIMEOUT', 'INCOMPLETE'].includes(run.verdict);
  const sec = h('section', { class: 'section' }, h('h2', {}, 'Crash signature'));
  sec.append(open ? inner : h('details', {}, h('summary', {}, 'First exception in the log (the run did not crash)'), inner));
  sec.append(h('div', { class: 'datum' }, 'first exception in the log, with a guess at the responsible mod or mixin from frame jar names and mixin text. A guess, not a diagnosis.'));
  return sec;
}

async function logDiffSection(run, baseId) {
  const sec = h('section', { class: 'section', id: 'logdiff' }, h('h2', {}, 'Log diff against baseline'));
  if (!baseId) { sec.append(empty('No baseline selected. Pick one above to see which warnings and errors are new.')); return sec; }
  if (!run.log) { sec.append(empty('This run has no mirrored log, so there is nothing to diff.')); return sec; }
  const body = h('div', {}, skeleton());
  sec.append(body);
  api('logdiff?base=' + encodeURIComponent(baseId) + '&cand=' + encodeURIComponent(run.id)).then(d => {
    clear(body);
    if (d.error) { body.append(empty('Cannot diff: ' + d.error + (d.base_has_log === false ? ' (baseline has no log)' : '') + '.')); return; }
    body.append(h('div', { class: 'rowline' },
      h('span', { class: 'chip ' + (d.new_total ? 'outline-flag' : 'outline-pass') }, d.new_total + ' new'),
      h('span', { class: 'mono small' }, d.new_occurrences + ' occurrences of ' + d.new_total + ' new signatures · ' + d.resolved_total + ' resolved · ' + d.increased_total + ' increased')));
    body.append(h('div', { class: 'datum' }, 'WARN, ERROR and exception lines only, after replacing timestamps, numbers, hex ids, uuids and run directories. Baseline ' + d.base_signatures + ' signatures, candidate ' + d.cand_signatures + '.'));
    const list = (items, title, openFirst, withBase) => {
      if (!items.length) return null;
      const wrap = h('div', {});
      let lim = 25;
      const draw = () => {
        clear(wrap);
        const tbl = h('div', { class: 'tblwrap', style: 'margin-top:8px' }, h('table', { class: 'tbl dense' }, h('tbody', {},
          items.slice(0, lim).map(g => h('tr', {}, h('td', { class: 'num', style: 'width:72px' }, withBase ? g.base_count + ' to ' + g.count : g.count + 'x'),
            h('td', { style: 'width:96px' }, h('span', { class: 'chip sm ' + (g.level === 'ERROR' || g.level === 'FATAL' ? 'outline-flag' : 'plain dim') }, g.level)),
            h('td', { class: 'sig' }, g.sig, h('details', {}, h('summary', {}, 'example line ' + (g.first_line || '')), h('div', { class: 'logbox' }, g.example || ''))))))));
        wrap.append(tbl);
        if (items.length > lim) wrap.append(h('div', { style: 'margin-top:8px' }, h('button', { class: 'btn', onclick: () => { lim += 25; draw(); } }, 'Show more · ' + (items.length - lim) + ' left')));
      };
      draw();
      return h('details', { open: openFirst }, h('summary', {}, title + ' (' + items.length + ')'), wrap);
    };
    const n = list(d.new, 'New in candidate', true, false);
    body.append(n || empty('No WARN, ERROR or exception signature is new relative to the baseline log.'));
    const inc = list(d.increased, 'Present in both, much more frequent now', false, true); if (inc) body.append(inc);
    const res = list(d.resolved, 'In baseline only (gone)', false, false); if (res) body.append(res);
  }).catch(e => clear(body).append(banner('', 'Log diff', e.message)));
  return sec;
}

function rawLogSection(run) {
  const sec = h('section', { class: 'section' }, h('h2', {}, 'Log'));
  if (!run.log) { sec.append(empty('No log mirrored for this run. ' + (run.evidence && run.evidence.dir ? 'Source dir: ' + run.evidence.dir : ''))); return sec; }
  const st = { level: 'WARN', grep: '' };
  const out = h('div', {});
  const draw = async () => {
    clear(out).append(skeleton());
    try {
      const v = await api('log/' + encodeURIComponent(run.id) + '?tail=300&level=' + st.level + (st.grep ? '&grep=' + encodeURIComponent(st.grep) : ''));
      const lb = h('div', { class: 'logbox tall', tabindex: '0' });
      v.lines.forEach(l => lb.append(h('div', { class: l.level || '' }, l.n + '  ' + l.text)));
      clear(out).append(lb, h('div', { class: 'datum' }, 'last ' + v.lines.length + ' of ' + v.total + ' entries at ' + st.level + ' or above · ' + run.log.lines + ' lines, ' + fmtVal(run.log.bytes, 'B') + ' on the node'));
      lb.scrollTop = lb.scrollHeight;
    } catch (e) { clear(out).append(banner('', 'Log', e.message)); }
  };
  const c = run.log.counts || {};
  sec.append(h('div', { class: 'rowline small mono' }, 'n=' + run.log.lines + ' lines · ' + c.error + ' errors · ' + c.warn + ' warnings · ' + c.exceptions + ' exception heads'));
  const d = h('details', {}, h('summary', {}, 'Open the log'),
    h('div', { class: 'filters' },
      h('div', { class: 'field' }, h('label', { for: 'lg-l' }, 'Level'), h('select', { id: 'lg-l', onchange: e => { st.level = e.target.value; draw(); } },
        ['TRACE', 'DEBUG', 'INFO', 'WARN', 'ERROR'].map(l => h('option', { value: l, selected: l === st.level }, l + ' and above')))),
      h('div', { class: 'field' }, h('label', { for: 'lg-g' }, 'Contains'), h('input', { id: 'lg-g', type: 'search', onchange: e => { st.grep = e.target.value; draw(); } }))), out);
  d.addEventListener('toggle', () => { if (d.open && !out.firstChild) draw(); });
  sec.append(d);
  return sec;
}

function checksSection(run) {
  if (!run.checks || !run.checks.length) return null;
  const sec = h('section', { class: 'section' }, h('h2', {}, 'Checks'));
  const counts = {};
  run.checks.forEach(c => { counts[c.status] = (counts[c.status] || 0) + 1; });
  const t = h('table', { class: 'tbl dense' }, h('thead', {}, h('tr', {}, h('th', {}, 'Status'), h('th', {}, 'Check'), h('th', {}, 'Detail'))), h('tbody', {}));
  const order = { FAIL: 0, INCOMPLETE: 1, LIMIT: 2 };
  run.checks.slice().sort((a, b) => (order[a.status] ?? 3) - (order[b.status] ?? 3)).forEach(c => t.tBodies[0].append(h('tr', {},
    h('td', {}, c.status === 'PASS' ? vchip('PASS', null, true) : c.status === 'FAIL' ? vchip('FAIL', null, true) : c.status === 'INCOMPLETE' ? vchip('INCOMPLETE', null, true) : h('span', { class: 'chip plain dim' }, c.status)),
    h('td', { class: 'pin' }, c.name), h('td', { class: 'wrap dim', style: 'max-width:640px' }, (c.detail || '').slice(0, 280)))));
  sec.append(h('div', { class: 'tblwrap tall' }, t), h('div', { class: 'datum' }, 'n=' + run.checks.length + ' checks · ' + Object.entries(counts).map(([k, v]) => v + ' ' + k.toLowerCase()).join(' · ')));
  return sec;
}

async function pageRun(ctx, id) {
  const baseQ = ctx.query.get('base') || 'auto';
  const d = await api('run/' + encodeURIComponent(id) + '?base=' + encodeURIComponent(baseQ));
  const run = d.run, env = run.environment || {}, c = run.candidate || {}, scen = env.scenario || {};
  const failed = ['FAIL', 'ERROR', 'TIMEOUT', 'INCOMPLETE', 'BLOCKED'].includes(run.verdict);
  const head = band('', run.gate + ' on ' + run.host, null, [link('#/runs', 'Runs'), h('span', { class: 'dim' }, '/'), h('span', { class: 'mono' }, run.id)]);
  head.append(h('div', { class: 'rowline' }, vchip(run.verdict, run.verdict_raw), h('span', { class: 'mono' }, fmtUtc(run.started_ts, true) + ' UTC · ' + fmtDur(run.duration_s)),
    h('span', { class: 'mono dim' }, run.recipe ? 'recipe ' + run.recipe : ''), h('span', { class: 'mono dim' }, 'source ' + run.source)));
  const page = [head];
  if (failed && run.reason) page.push(h('div', { class: 'banner ' + (['FAIL', 'ERROR', 'TIMEOUT'].includes(run.verdict) ? 'alert' : '') },
    h('span', { class: 'chip ' + (['FAIL', 'ERROR', 'TIMEOUT'].includes(run.verdict) ? 'fill-alert' : 'fill-flag') }, run.verdict), h('span', { class: 'wrap' }, run.reason)));
  (run.notes || []).filter(n => n.startsWith('evidence says')).forEach(n => page.push(banner('', 'Job overrides evidence', n)));
  if (run.mirror && !run.mirror.complete && run.verdict !== 'RUNNING') page.push(h('div', { class: 'banner dim' }, h('span', { class: 'k' }, 'Mirror'),
    ' ' + run.mirror.pending + ' file(s) not mirrored yet' + ((run.mirror.failed || []).length ? ': ' + run.mirror.failed.map(f => f.rel + ' (' + f.err + ')').join('; ') : '. They are picked up on the next collector pass.')));
  const matrixOk = env.matched_comparison_ok === false;
  if (matrixOk) page.push(banner('', 'Source flag', 'This record says it is not usable for a matched comparison (usable_for_matched_comparison=false). Deltas against any other run are withheld.'));

  const split = h('div', { class: 'split section' });
  split.append(h('div', { class: 'panel' }, h('div', { class: 'hd' }, h('h3', {}, 'Candidate')), h('div', { class: 'bd' }, kvs([
    ['Label', c.label], ['Commit', c.commit], ['Jar sha256', c.jar_sha256], ['Mod id', c.mod_id], ['Request', run.request_id], ['Job', run.job_id],
    ['Recipe', run.recipe], ['Job state', run.job && (run.job.state + (run.job.exit_code != null ? ', exit ' + run.job.exit_code : '') + (run.job.error_type ? ', ' + run.job.error_type : ''))],
    ['Evidence dir', run.evidence && run.evidence.dir],
    ['Note', c.inferred ? 'candidate inferred ' + c.inferred : null]]))));
  split.append(h('div', { class: 'panel' }, h('div', { class: 'hd' }, h('h3', {}, 'Environment')), h('div', { class: 'bd' }, kvs([
    ['GL renderer', env.gl_renderer], ['GL version', env.gl_version], ['OS', env.os], ['CPU', env.cpu], ['Java', env.java],
    ['Minecraft', env.minecraft ? env.minecraft + (env.loader ? ' ' + env.loader + ' ' + (env.loader_version || '') : '') : null],
    ['Window', env.window ? env.window.join(' x ') : null], ['Max heap', env.max_heap], ['Scenario', scen.name], ['Fixture', scen.fixture],
    ['Gate definition', scen.gates], ['Client manifest', env.manifest_sha256 ? sh(env.manifest_sha256, 16) : null],
    ['Settings', env.settings ? Object.entries(env.settings).map(([k, v]) => k + '=' + JSON.stringify(v)).join('  ') : null],
    ['Mods', env.mods ? env.mods.length + ' loaded' : null]]),
    env.mods && env.mods.length ? h('details', {}, h('summary', {}, 'Mod list'), h('div', { class: 'logbox tall' }, env.mods.map(m => m.id + ' ' + m.version).join('\n'))) : null)));
  page.push(split);

  // baseline picker
  const base = d.base;
  const opts = [h('option', { value: 'auto', selected: baseQ === 'auto' }, 'Automatic: ' + (baseQ === 'auto' && d.compare ? d.compare.base : 'latest earlier pass of a different build')),
    h('option', { value: 'none', selected: baseQ === 'none' }, 'No baseline')];
  d.base_candidates.forEach(b => opts.push(h('option', { value: b.id, selected: baseQ === b.id }, fmtUtc(b.started_ts) + ' · ' + ((b.candidate && b.candidate.label) || DASH) + ' · ' + b.verdict)));
  const baseSel = h('div', { class: 'filters', style: 'margin-top:0' }, h('div', { class: 'field' }, h('label', { for: 'basesel' }, 'Baseline run (same host and gate)'),
    h('select', { id: 'basesel', onchange: e => { location.hash = runHref(id, e.target.value === 'auto' ? null : e.target.value); } }, opts)),
    d.compare && d.compare.base_summary ? h('div', { class: 'datum' }, 'baseline ' + d.compare.base + ' · ' + fmtUtc(d.compare.base_summary.started_ts) + ' · candidate ' + ((d.compare.base_summary.candidate || {}).label || DASH) + ' · ' + d.compare.base_summary.verdict) : null);
  page.push(metricsSection(run, d.compare, baseSel));
  page.push(stepsSection(run));
  const slots = { series: h('div', {}, skeleton()), gallery: h('div', {}, skeleton()), crash: crashSection(run), diff: h('div', {}, skeleton()) };
  page.push(slots.series, slots.crash, slots.gallery, slots.diff, rawLogSection(run), checksSection(run));
  const files = (run.evidence && run.evidence.inline_files) || [];
  if (files.length) page.push(h('section', { class: 'section' }, h('h2', {}, 'Evidence files'), h('div', { class: 'rowline' },
    files.map(f => h('a', { class: 'btn', href: '/files/' + encodeURIComponent(run.id) + '/files/' + encodeURIComponent(f), target: '_blank', rel: 'noopener' }, f))),
    h('div', { class: 'datum' }, 'copied unchanged from the node')));
  (run.notes || []).filter(n => !n.startsWith('evidence says')).length && page.push(h('section', { class: 'section' }, h('h2', {}, 'Notes from the run'),
    h('ul', { class: 'small mono' }, run.notes.filter(n => !n.startsWith('evidence says')).map(n => h('li', { class: 'wrap' }, n)))));
  page.push(provenance('run ' + run.id));
  clear(main).append(...page.filter(Boolean));
  const swap = (slot, node) => { if (current === ctx) slot.replaceWith(node); };
  seriesSection(run, base).then(n => swap(slots.series, n));
  gallerySection(run, base).then(n => swap(slots.gallery, n));
  logDiffSection(run, base).then(n => swap(slots.diff, n));
}

// ---------------------------------------------------------------------------------------------
// compare
// ---------------------------------------------------------------------------------------------
async function pageCompare(ctx) {
  const cands = (await api('candidates')).candidates;
  const q = ctx.query;
  const st = { base: q.get('base') || '', cand: q.get('cand') || '' };
  const dl = h('datalist', { id: 'cand-list' }, cands.map(c => h('option', { value: c.label || sh(c.commit) }, sh(c.commit, 9) + ' on ' + c.hosts.join(', ') + ' (' + c.runs + ' runs)')));
  const out = h('div', { class: 'section' });
  const inp = (label, key) => h('div', { class: 'field', style: 'flex:1;min-width:220px' }, h('label', { for: 'c-' + key }, label),
    h('input', { id: 'c-' + key, type: 'text', list: 'cand-list', value: st[key], placeholder: 'label, commit prefix or jar prefix', autocomplete: 'off',
      oninput: e => { st[key] = e.target.value.trim(); } }));
  const go = () => { setQuery('#/compare', st); run(); };
  const form = h('form', { class: 'filters', onsubmit: e => { e.preventDefault(); go(); } },
    inp('Baseline candidate', 'base'), inp('Candidate under test', 'cand'), h('button', { class: 'btn accent', type: 'submit' }, 'Compare'),
    h('button', { class: 'btn', type: 'button', onclick: () => { [st.base, st.cand] = [st.cand, st.base]; clear(form); location.hash = '#/compare?base=' + encodeURIComponent(st.base) + '&cand=' + encodeURIComponent(st.cand); render(); } }, 'Swap'));
  clear(main).append(band('Compare', 'Compare candidates',
    'Pick a baseline and a candidate commit. Each host and gate shows both verdicts and the key metric deltas. When the renderer, host, scenario or settings differ between the two runs, the mismatch is shown and the deltas are withheld.'),
    dl, form, out, provenance());
  async function run() {
    if (!st.base || !st.cand) { clear(out).append(empty('Choose two candidates to compare. Recent candidates: ' + cands.slice(0, 6).map(c => c.label || sh(c.commit)).join(', ') + '.')); return; }
    clear(out).append(skeleton(true));
    let d;
    try { d = await api('compare?base=' + encodeURIComponent(st.base) + '&cand=' + encodeURIComponent(st.cand)); } catch (e) { clear(out).append(banner('', 'Compare', e.message)); return; }
    if (d.mode === 'runs') { location.hash = runHref(d.cand.id, d.base.id); return; }
    clear(out);
    out.append(h('div', { class: 'rowline' }, h('span', { class: 'k' }, 'Baseline'), h('span', { class: 'mono ink' }, d.base.label + ' (' + d.base.runs + ' gate runs)'),
      h('span', { class: 'k' }, 'Candidate'), h('span', { class: 'mono ink' }, d.cand.label + ' (' + d.cand.runs + ' gate runs)')));
    if (!d.cells.length) { out.append(empty('Neither candidate has a gate run on any host. Check the spelling against the Runs page candidate column.')); return; }
    const hosts = d.hosts, gates = d.gates;
    const get = (h2, g) => d.cells.find(c => c.host === h2 && c.gate === g);
    const t = h('table', { class: 'tbl mx' }, h('thead', {}, h('tr', {}, h('th', { class: 'pin' }, 'Host'), gates.map(g => h('th', {}, g)))), h('tbody', {}));
    hosts.forEach(hn => {
      const tr = h('tr', {}, h('td', { class: 'pin mono ink' }, hn));
      gates.forEach(g => {
        const c = get(hn, g), td = h('td', {});
        if (!c) { td.append(h('span', { class: 'dim' }, DASH)); tr.append(td); return; }
        const box = h('div', { class: 'cellbox' });
        box.append(h('div', { class: 'rowline' }, c.base ? vchip(c.base.verdict, c.base.verdict_raw, true) : h('span', { class: 'chip plain dim' }, 'No baseline run'),
          h('span', { class: 'arrow' }, '→'), c.cand ? vchip(c.cand.verdict, c.cand.verdict_raw, true) : h('span', { class: 'chip plain dim' }, 'No candidate run')));
        if (c.base && c.cand) {
          if (c.env && !c.env.comparable) {
            box.append(h('div', { class: 'banner', style: 'margin-top:0' }, h('span', { class: 'chip fill-flag sm' }, 'Not comparable'),
              h('ul', {}, c.env.mismatches.filter(m => m.severity === 'hard').slice(0, 4).map(m => h('li', { class: 'mono small wrap' }, m.field + ': ' + m.base + ' vs ' + m.cand)))));
          } else {
            const dg = h('div', { class: 'delta' });
            c.deltas.forEach(r => {
              if (r.state === 'missing_base' || r.state === 'missing_cand') return;
              dg.append(h('span', { class: 'dim', title: r.key }, r.name.replace(/^.*\./, '') + (r.segment ? ' ' + r.segment : '')),
                h('span', { class: 'st-' + ({ within_noise: 'noise', not_comparable: 'nc' }[r.state] || r.state) }, fmtDelta(r.delta, r.unit) + ' ' + fmtPct(r.pct) + (STATE_WORD[r.state] ? ' ' + STATE_WORD[r.state] : '')));
            });
            if (!dg.firstChild) dg.append(h('span', { class: 'dim' }, 'No shared key metric.'));
            box.append(dg);
            if (c.env && c.env.unverified.length) box.append(h('div', { class: 'datum' }, 'unverified: ' + c.env.unverified.map(u => u.field).join(', ')));
          }
          box.append(h('div', { class: 'rowline small' }, link(runHref(c.cand.id, c.base.id), 'Run, metrics and log diff')));
        } else if (c.cand) box.append(h('div', { class: 'rowline small' }, link(runHref(c.cand.id), 'Candidate run')));
        else if (c.base) box.append(h('div', { class: 'rowline small' }, link(runHref(c.base.id), 'Baseline run')));
        box.append(h('div', { class: 'datum' }, 'n=' + c.n_base_runs + ' baseline, ' + c.n_cand_runs + ' candidate runs'));
        td.append(box); tr.append(td);
      });
      t.tBodies[0].append(tr);
    });
    out.append(h('div', { class: 'tblwrap', style: 'margin-top:16px' }, t),
      h('div', { class: 'datum' }, 'cell = latest finished run per candidate on that host and gate · delta = candidate minus baseline · better and worse use each metric’s own direction'));
  }
  run();
}

// ---------------------------------------------------------------------------------------------
// history
// ---------------------------------------------------------------------------------------------
function sparkline(group, m) {
  const runs = group.runs;
  const pts = runs.map((r, i) => ({ i, r, v: r.metrics[m.key] })).filter(p => p.v != null);
  if (pts.length < 1) return empty('No value recorded for this metric.');
  const W = 560, H = 72, L = 8, R = 8, T = 8, B = 8;
  const vals = pts.map(p => p.v), lo = Math.min(...vals), hi = Math.max(...vals), rng = hi - lo || Math.abs(hi) || 1;
  const px = i => L + (runs.length > 1 ? i / (runs.length - 1) : 0.5) * (W - L - R);
  const py = v => T + (1 - (v - lo) / rng) * (H - T - B);
  const svg = s('svg', { class: 'chart', viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': m.name + ' over ' + runs.length + ' candidates' });
  svg.append(s('line', { class: 'ax', x1: L, x2: W - R, y1: H - 2, y2: H - 2 }));
  let d = '';
  pts.forEach((p, k) => { d += (k ? 'L' : 'M') + px(p.i).toFixed(1) + ' ' + py(p.v).toFixed(1); });
  svg.append(s('path', { class: 's s1', d }));
  pts.forEach(p => {
    const bad = ['FAIL', 'ERROR', 'TIMEOUT'].includes(p.r.verdict);
    const cls = 'pt' + (bad ? ' bad' : p.r.verdict === 'INCOMPLETE' ? ' part' : '');
    const x = px(p.i), y = py(p.v);
    const sq = s('rect', { class: cls, x: x - 4, y: y - 4, width: 8, height: 8, tabindex: '0', role: 'link', 'aria-label': (p.r.label || p.r.id) + ' ' + fmtVal(p.v, m.unit) + ' ' + p.r.verdict,
      'data-tip': (p.r.label || sh(p.r.jar)) + ' · ' + fmtUtc(p.r.ts) + ' · ' + fmtVal(p.v, m.unit) + ' · ' + p.r.verdict });
    sq.addEventListener('click', () => { location.hash = runHref(p.r.id); });
    sq.addEventListener('keydown', e => { if (e.key === 'Enter') location.hash = runHref(p.r.id); });
    svg.append(sq);
  });
  return svg;
}

async function pageHistory(ctx) {
  const data = await api('history?limit=24');
  const out = h('div', {});
  clear(main).append(band('History', 'History',
    'Key metrics over the last 24 candidates for each host and gate, oldest on the left. A hollow square is an incomplete run and a pink square a failed one; both stay in the line so a bad candidate is not smoothed away.'),
    out, provenance());
  const groups = data.groups.filter(g => g.metrics.length);
  const skipped = data.groups.filter(g => !g.metrics.length);
  if (!groups.length) { out.append(empty('No gate run has key metrics yet.')); return; }
  groups.forEach(g => {
    const sec = h('section', { class: 'section' }, h('h2', {}, g.host + ' · ' + g.gate), h('div', { class: 'datum' }, 'n=' + g.n + ' candidates · ' +
      (g.runs[0].label || DASH) + ' to ' + (g.runs[g.runs.length - 1].label || DASH)));
    const t = h('table', { class: 'tbl' }, h('thead', {}, h('tr', {}, h('th', {}, 'Metric'), h('th', {}, 'Candidates, oldest to newest'), h('th', { class: 'num' }, 'Latest'), h('th', { class: 'num' }, 'Min'), h('th', { class: 'num' }, 'Max'), h('th', { class: 'num' }, 'n'))), h('tbody', {}));
    g.metrics.forEach(m => {
      const vals = g.runs.map(r => r.metrics[m.key]).filter(v => v != null);
      const last = vals[vals.length - 1];
      t.tBodies[0].append(h('tr', {}, h('td', {}, m.name, h('span', { class: 'sub' }, (m.better ? m.better + ' is better' : 'neutral') + (m.unit ? ' · ' + m.unit : ''))),
        h('td', { style: 'min-width:260px' }, sparkline(g, m)), h('td', { class: 'num ink' }, fmtVal(last, m.unit)), h('td', { class: 'num' }, fmtVal(Math.min(...vals), m.unit)),
        h('td', { class: 'num' }, fmtVal(Math.max(...vals), m.unit)), h('td', { class: 'num' }, vals.length)));
    });
    sec.append(h('div', { class: 'tblwrap' }, t), h('div', { class: 'legend' }, h('span', {}, h('i', { class: 'sw s1' }), 'metric across candidates'), h('span', {}, 'filled square: pass or other'),
      h('span', {}, 'pink square: failed'), h('span', {}, 'hollow square: incomplete')));
    out.append(sec);
  });
  if (skipped.length) out.append(h('div', { class: 'datum' }, 'no key metrics for: ' + skipped.map(g => g.host + '/' + g.gate).join(', ')));
}

// ---------------------------------------------------------------------------------------------
pollStatus();
render();
