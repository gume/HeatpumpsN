'use strict';

/* HeatpumpsN controller portal. Vanilla JS, no build step, served from LittleFS. */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const view = $('#view');
const sidebar = $('#sidebar');

const state = {
  live: null,
  status: null,
  config: {},
  events: [],
  access: [],
  slow: [],
  counters: [],
  counterPersistence: null,
  runtime: null,
  ws: null,
  wsConnected: false,
  route: 'dashboard',
  dirty: false,
  eventFilter: 'all',
  slowRange: 'all',
  renderTimer: null,
  lastRender: 0,
  interactionUntil: 0,
  lastStatusFetch: 0,
  lastSlowFetch: 0,
  lastEventsFetch: 0,
  lastRuntimeFetch: 0,
  eventsSeq: 0,
  accessSeq: 0,
  otaBusy: false
};

const LIVE_ROUTES = new Set(['dashboard', 'plant', 'log/sensors', 'test']);
const SETTINGS_ROUTES = new Set(['settings/safety', 'settings/hardware', 'settings/network', 'settings/firmware', 'settings/system']);

/* ------------------------------------------------------------------------- */
/* Formatting helpers                                                         */
/* ------------------------------------------------------------------------- */

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}

function num(value, fallback = null) {
  if (value === null || value === undefined || value === '') return fallback;
  const n = Number(value);
  return Number.isFinite(n) ? n : fallback;
}

function fmtTemp(value, digits = 1) {
  const n = num(value);
  return n === null ? '—' : `${n.toFixed(digits)} °C`;
}

function fmtNum(value, digits = 1) {
  const n = num(value);
  return n === null ? '—' : n.toFixed(digits);
}

function fmtBytes(bytes) {
  const n = num(bytes);
  if (n === null) return '—';
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KiB`;
  return `${(n / (1024 * 1024)).toFixed(2)} MiB`;
}

function fmtAge(seconds) {
  const s = num(seconds);
  if (s === null || s < 0) return 'never';
  if (s < 2) return 'now';
  if (s < 60) return `${Math.round(s)} s ago`;
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}

function fmtUptime(seconds) {
  let s = Math.max(0, Math.floor(num(seconds, 0)));
  const d = Math.floor(s / 86400); s %= 86400;
  const h = Math.floor(s / 3600); s %= 3600;
  const m = Math.floor(s / 60); s %= 60;
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${s}s`;
  return `${s}s`;
}

function fmtDuration(seconds) {
  const total = Math.max(0, Math.floor(num(seconds, 0)));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}

function fmtT(ms) {
  return `T+${fmtDuration(Math.floor(num(ms, 0) / 1000))}`;
}

function modeLabel(mode) {
  return ({
    heat: 'Heat',
    summer: 'Summer',
    anti_seize: 'Anti-seize',
    test: 'Test'
  }[mode] || 'Unknown');
}

function modeClass(mode) {
  return `mode-${String(mode || 'unknown').replace(/[^a-z_]/g, '')}`;
}

function statePill(on, label) {
  return `<span class="state-pill ${on ? 'state-on' : 'state-off'}">${esc(label)}</span>`;
}

function mismatchPill(cmd, phys) {
  if (!!cmd !== !!phys) {
    return `<span class="state-pill mismatch">CMD ${cmd ? 'ON' : 'OFF'} / PHYS ${phys ? 'ON' : 'OFF'}</span>`;
  }
  return statePill(!!phys, phys ? 'ON' : 'OFF');
}

function severityClass(severity) {
  return `sev-${String(severity || 'info').toLowerCase()}`;
}

function flagTone(flag) {
  if (['overtemp_hard', 'welded_relay'].includes(flag)) return 'safety';
  if (flag.startsWith('sensor_fault')) return 'err';
  if (['heater_no_pump', 'pump_dry_run', 'encoder_diverged'].includes(flag)) return 'warn';
  return 'warn';
}

function isLiveRoute(route) {
  return LIVE_ROUTES.has(route);
}

function isSettingsRoute(route) {
  return SETTINGS_ROUTES.has(route);
}

function loadingCard(message = 'Loading…') {
  return `<div class="card readonly"><div class="empty">${esc(message)}</div></div>`;
}

function emptyCard(message) {
  return `<div class="empty">${esc(message)}</div>`;
}

/* ------------------------------------------------------------------------- */
/* API                                                                        */
/* ------------------------------------------------------------------------- */

async function api(path, options = {}) {
  const response = await fetch(path, { cache: 'no-store', ...options });
  const text = await response.text();
  let data = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch (err) {
      throw new Error(text.slice(0, 240) || response.statusText);
    }
  }
  if (!response.ok) {
    throw new Error((data && data.error) || response.statusText || `HTTP ${response.status}`);
  }
  return data;
}

const apiGet = (path) => api(path);
const apiPost = (path, body) => api(path, {
  method: 'POST',
  headers: body !== undefined ? { 'Content-Type': 'application/json' } : {},
  body: body !== undefined ? JSON.stringify(body) : undefined
});
const apiPut = (path, body) => api(path, {
  method: 'PUT',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body)
});

/* ------------------------------------------------------------------------- */
/* Toast, modal and theme                                                     */
/* ------------------------------------------------------------------------- */

let toastTimer = null;

function toast(message, kind = 'info', ms = 4200) {
  const el = $('#toast');
  el.textContent = message;
  el.className = `toast ${kind}`;
  el.classList.remove('hidden');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add('hidden'), ms);
}

let modalResolve = null;

function confirmDialog(title, body, okLabel = 'Confirm', danger = true) {
  return new Promise((resolve) => {
    modalResolve = resolve;
    $('#modal-title').textContent = title;
    $('#modal-body').textContent = body;
    const ok = $('#modal-ok');
    ok.textContent = okLabel;
    ok.className = `btn ${danger ? 'btn-danger' : 'btn-primary'}`;
    $('#modal').classList.remove('hidden');
    ok.focus();
  });
}

function closeModal(result) {
  $('#modal').classList.add('hidden');
  if (modalResolve) {
    const resolve = modalResolve;
    modalResolve = null;
    resolve(result);
  }
}

function applyTheme(theme) {
  const stored = theme || localStorage.getItem('hpn-theme') || 'dark';
  const resolved = stored === 'auto'
    ? (window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark')
    : stored;
  document.documentElement.dataset.theme = resolved;
  localStorage.setItem('hpn-theme', stored);
  const select = $('#theme-select');
  if (select) select.value = stored;
}

function setDirty(dirty) {
  state.dirty = !!dirty;
  $('#dirty-badge').classList.toggle('hidden', !state.dirty);
}

/* ------------------------------------------------------------------------- */
/* WebSocket and connection state                                             */
/* ------------------------------------------------------------------------- */

function updateConnection() {
  const dot = $('#ws-dot');
  const label = $('#ws-state');
  const status = $('#conn-status');
  dot.className = `dot ${state.wsConnected ? 'dot-on' : 'dot-off'}`;
  label.textContent = state.wsConnected ? 'live' : 'reconnecting…';
  status.textContent = state.wsConnected
    ? 'WebSocket: connected · live updates'
    : 'WebSocket: reconnecting…';
}

function connectWs() {
  if (state.ws && (state.ws.readyState === WebSocket.OPEN || state.ws.readyState === WebSocket.CONNECTING)) {
    return;
  }
  const protocol = location.protocol === 'https:' ? 'wss' : 'ws';
  let ws;
  try {
    ws = new WebSocket(`${protocol}://${location.host}/ws`);
  } catch (err) {
    setTimeout(connectWs, 2500);
    return;
  }
  state.ws = ws;

  ws.onopen = () => {
    state.wsConnected = true;
    updateConnection();
  };

  ws.onclose = () => {
    state.wsConnected = false;
    state.ws = null;
    updateConnection();
    setTimeout(connectWs, 2200);
  };

  ws.onerror = () => {
    try { ws.close(); } catch (err) { /* ignore */ }
  };

  ws.onmessage = (event) => {
    try {
      const live = JSON.parse(event.data);
      if (!live || typeof live !== 'object') return;
      state.live = live;
      state.wsConnected = true;
      updateHeader();
      updateConnection();
      if (isLiveRoute(state.route)) scheduleLiveRender();
    } catch (err) {
      console.warn('Ignoring malformed WebSocket message', err);
    }
  };
}

/* ------------------------------------------------------------------------- */
/* Data loaders                                                               */
/* ------------------------------------------------------------------------- */

async function refreshStatus() {
  try {
    state.status = await apiGet('/api/status');
    state.lastStatusFetch = Date.now();
    updateBuildInfo();
    return state.status;
  } catch (err) {
    console.warn('Status fetch failed', err);
    return null;
  }
}

async function refreshState() {
  try {
    state.live = await apiGet('/api/state');
    updateHeader();
    if (isLiveRoute(state.route)) renderCurrent();
    return state.live;
  } catch (err) {
    toast(`Could not refresh state: ${err.message}`, 'err');
    return null;
  }
}

async function refreshConfig(section) {
  try {
    state.config[section] = await apiGet(`/api/config/${section}`);
    return state.config[section];
  } catch (err) {
    toast(`Could not load ${section} settings: ${err.message}`, 'err');
    return null;
  }
}

async function refreshEvents() {
  try {
    const data = await apiGet('/api/log/events?after=0');
    state.events = Array.isArray(data.entries) ? data.entries : [];
    state.eventsSeq = num(data.seq, 0);
    state.lastEventsFetch = Date.now();
    return state.events;
  } catch (err) {
    console.warn('Event log fetch failed', err);
    return state.events;
  }
}

async function refreshAccess() {
  try {
    const data = await apiGet('/api/log/access?after=0');
    state.access = Array.isArray(data.entries) ? data.entries : [];
    state.accessSeq = num(data.seq, 0);
    return state.access;
  } catch (err) {
    console.warn('Access log fetch failed', err);
    return state.access;
  }
}

async function refreshSlow() {
  try {
    const data = await apiGet('/api/log/slow');
    state.slow = Array.isArray(data.samples) ? data.samples : [];
    state.lastSlowFetch = Date.now();
    return state.slow;
  } catch (err) {
    console.warn('Slow log fetch failed', err);
    return state.slow;
  }
}

async function refreshCounters() {
  try {
    const data = await apiGet('/api/counters');
    state.counters = Array.isArray(data.counters) ? data.counters : [];
    state.counterPersistence = {
      enabled: !!data.persistenceEnabled,
      saveIntervalS: num(data.saveIntervalS, 0)
    };
    return state.counters;
  } catch (err) {
    toast(`Could not load relay counters: ${err.message}`, 'err');
    return state.counters;
  }
}

async function refreshRuntime() {
  try {
    state.runtime = await apiGet('/api/runtime');
    state.lastRuntimeFetch = Date.now();
    return state.runtime;
  } catch (err) {
    toast(`Could not load runtime parameters: ${err.message}`, 'err');
    return state.runtime;
  }
}

/* ------------------------------------------------------------------------- */
/* Header, navigation and routing                                             */
/* ------------------------------------------------------------------------- */

function updateHeader() {
  const live = state.live;
  if (!live) return;

  const mode = live.mode?.current || 'unknown';
  const modeBadge = $('#mode-badge');
  modeBadge.textContent = modeLabel(mode);
  modeBadge.className = `badge ${modeClass(mode)}`;

  const test = live.test || {};
  const testBadge = $('#test-badge');
  if (test.active) {
    testBadge.textContent = `Test ${fmtDuration(test.remaining)}`;
    testBadge.classList.remove('hidden');
  } else {
    testBadge.classList.add('hidden');
  }

  const wifi = live.wifi || {};
  const wifiBadge = $('#wifi-badge');
  if (wifi.connected) {
    const rssi = num(wifi.rssi, 0);
    wifiBadge.textContent = `Wi-Fi ${rssi} dBm`;
    wifiBadge.className = `badge ${rssi > -75 ? 'badge-ok' : 'badge-warn'}`;
  } else {
    wifiBadge.textContent = 'Wi-Fi off';
    wifiBadge.className = 'badge badge-err';
  }

  const mqtt = live.mqtt || {};
  const mqttBadge = $('#mqtt-badge');
  mqttBadge.textContent = mqtt.connected ? 'MQTT on' : 'MQTT off';
  mqttBadge.className = `badge ${mqtt.connected ? 'badge-ok' : ''}`;
}

function updateBuildInfo() {
  if (!state.status) return;
  const s = state.status;
  const fs = s.filesystem || {};
  const fsText = fs.total ? ` · LittleFS ${fmtBytes(fs.used)}/${fmtBytes(fs.total)}` : '';
  $('#build-info').textContent = `v${s.version || '?'} · ${s.build || ''} · ${s.chip || ''} · heap ${fmtBytes(s.freeHeap)}${fsText}`;
}

function updateNav() {
  $$('#nav [data-route]').forEach((link) => {
    link.classList.toggle('active', link.dataset.route === state.route);
  });

  const groupName = state.route.startsWith('settings/') ? 'settings'
    : state.route.startsWith('log/') ? 'diagnostics' : null;
  if (groupName) {
    const group = $(`#nav .nav-group[data-group="${groupName}"]`);
    if (group) {
      group.classList.remove('collapsed');
      const toggle = $('.nav-group-toggle', group);
      if (toggle) toggle.setAttribute('aria-expanded', 'true');
    }
  }
}

function closeSidebar() {
  if (window.matchMedia('(max-width: 900px)').matches) sidebar.classList.remove('open');
}

function routeFromHash() {
  const raw = location.hash.replace(/^#\/?/, '');
  return routes[raw] ? raw : 'dashboard';
}

async function onRoute() {
  const next = routeFromHash();

  if (state.dirty && next !== state.route) {
    const leave = await confirmDialog(
      'Discard unsaved changes?',
      'You have unsaved settings. Leave this page and discard them?',
      'Discard',
      true
    );
    if (!leave) {
      history.replaceState(null, '', `#${state.route}`);
      return;
    }
    setDirty(false);
  }

  state.route = next;
  const route = routes[next];
  $('#page-title').textContent = route.title;
  $('#page-subtitle').textContent = route.subtitle;
  document.title = `${route.title} · HeatpumpsN`;
  updateNav();

  if (route.enter) {
    await route.enter();
  }
  renderCurrent();
  closeSidebar();
}

function renderCurrent() {
  const route = routes[state.route];
  if (!route) return;
  view.innerHTML = route.render();
  bindFormBehaviors(view);
  if (route.after) route.after();
}

function scheduleLiveRender() {
  if (state.renderTimer) return;
  const now = Date.now();
  if (state.route === 'test' && now < state.interactionUntil) {
    state.renderTimer = setTimeout(() => {
      state.renderTimer = null;
      scheduleLiveRender();
    }, 500);
    return;
  }
  const delay = Math.max(0, 500 - (now - state.lastRender));
  state.renderTimer = setTimeout(() => {
    state.renderTimer = null;
    state.lastRender = Date.now();
    if (isLiveRoute(state.route)) {
      const route = routes[state.route];
      view.innerHTML = route.render();
      bindFormBehaviors(view);
      if (route.after) route.after();
    }
  }, delay);
}

/* ------------------------------------------------------------------------- */
/* Shared render fragments                                                    */
/* ------------------------------------------------------------------------- */

function metricCard(label, value, sub = '', tone = '') {
  return `<div class="card pad-sm">
    <div class="metric">
      <div class="metric-label">${esc(label)}</div>
      <div class="metric-value ${tone ? `tone-${tone}` : ''}">${esc(value)}</div>
      <div class="metric-sub">${sub}</div>
    </div>
  </div>`;
}

function tempTile(label, value, health) {
  const valid = health ? health.valid !== false : num(value) !== null;
  const healthState = health?.state || (valid ? 'ok' : 'stale');
  const tone = valid ? '' : (healthState === 'error' || healthState === 'disconnected' ? 'bad' : 'stale');
  const age = health?.age;
  return `<div class="temp-tile ${tone}">
    <div class="label">${esc(label)}</div>
    <div class="value">${fmtTemp(value)}</div>
    <div class="meta">
      <span>${esc(valid ? 'valid' : healthState)}</span>
      <span>·</span>
      <span>${age === undefined || age < 0 ? 'never' : fmtAge(age)}</span>
    </div>
  </div>`;
}

function circuitCard(key, label, circuit) {
  const c = circuit || {};
  const afterRun = num(c.afterRun, 0);
  let extra = '';
  if (key === 'floor') {
    const pos = Math.max(0, Math.min(100, num(c.mixerPos, 0)));
    const enc = Math.max(0, Math.min(100, num(c.encoderPos, 0)));
    extra = `<div class="metric-row"><span class="k">Mixer position</span><span class="v">${pos.toFixed(1)}%</span></div>
      <div class="bar"><span style="width:${pos.toFixed(1)}%"></span></div>
      <div class="metric-row"><span class="k">Encoder</span><span class="v">${enc.toFixed(1)}% ${c.encoderValid ? '' : '⚠'}</span></div>`;
  }
  return `<div class="circuit">
    <div class="circuit-head"><strong>${esc(label)}</strong>${mismatchPill(c.cmd, c.phys)}</div>
    <div class="circuit-body">
      <div class="metric-row"><span class="k">Command</span><span class="v">${c.cmd ? 'ON' : 'OFF'}</span></div>
      <div class="metric-row"><span class="k">Physical</span><span class="v">${c.phys ? 'ON' : 'OFF'}</span></div>
      <div class="metric-row"><span class="k">After-run</span><span class="v">${afterRun ? fmtAge(afterRun) : '—'}</span></div>
      ${extra}
    </div>
  </div>`;
}

function safetyFlags(flags) {
  if (!Array.isArray(flags) || flags.length === 0) {
    return `<div class="banner ok">No active safety flags.</div>`;
  }
  return `<div class="row">${flags.map((flag) =>
    `<span class="chip chip-${flagTone(flag)}">${esc(flag)}</span>`).join('')}</div>`;
}

function eventRowCompact(entry) {
  return `<div class="metric-row">
    <span class="k"><span class="${severityClass(entry.severity)}">${esc(entry.severity)}</span> ${esc(entry.detail)}</span>
    <span class="v">${fmtT(entry.ts)}</span>
  </div>`;
}

function sensorTable(sensors) {
  if (!Array.isArray(sensors) || sensors.length === 0) {
    return emptyCard('No sensor data yet.');
  }
  return `<div class="table-wrap"><table>
    <thead><tr><th>Sensor</th><th>Value</th><th>Age</th><th>State</th><th>Errors</th></tr></thead>
    <tbody>${sensors.map((s) => {
      const tone = s.state === 'ok' ? 'chip-ok' : s.state === 'stale' ? 'chip-warn' : 'chip-err';
      return `<tr>
        <td><strong>${esc(s.label)}</strong><div class="small faint mono">${esc(s.id)}</div></td>
        <td class="mono">${fmtTemp(s.val)}</td>
        <td class="mono">${s.age === undefined || s.age < 0 ? 'never' : fmtAge(s.age)}</td>
        <td><span class="chip ${tone}">${esc(s.state)}</span></td>
        <td class="mono">${esc(s.errors ?? 0)}</td>
      </tr>`;
    }).join('')}</tbody>
  </table></div>`;
}

/* ------------------------------------------------------------------------- */
/* Dashboard                                                                  */
/* ------------------------------------------------------------------------- */

function dashboardLeds(live) {
  const wifi = live.wifi || {};
  const mqtt = live.mqtt || {};
  const heater = live.heater || {};
  const circuits = live.circuits || {};
  const safety = live.safety || {};
  const system = live.system || {};
  const flags = Array.isArray(safety.flags) ? safety.flags : [];
  const latched = Array.isArray(safety.latched) ? safety.latched : [];
  const errorCode = num(safety.errorCode, 0);
  const critical = latched.length > 0 || errorCode !== 0;
  const warning = flags.length > 0;
  const heaterOn = !!num(heater.phys, 0);
  const floorOn = !!num((circuits.floor || {}).phys, 0);
  const rad1On = !!num((circuits.rad1 || {}).phys, 0);
  const rad2On = !!num((circuits.rad2 || {}).phys, 0);
  const divergence = String(system.divergence || 'none');

  let systemLed;
  if (critical) {
    systemLed = { color: 'red', text: 'Alarm', note: `error ${errorCode}` };
  } else if (!wifi.connected) {
    systemLed = { color: 'red', text: 'Offline', note: 'Wi-Fi down' };
  } else if (warning) {
    systemLed = { color: 'yellow', text: 'Warning', note: flags[0] };
  } else if (!mqtt.connected) {
    systemLed = { color: 'yellow', text: 'Wi-Fi only', note: 'MQTT off' };
  } else {
    systemLed = { color: 'green', text: 'Online', note: 'Wi-Fi + MQTT' };
  }

  let floorLed;
  if (divergence === 'floor_delta_t_collapsed') {
    floorLed = { color: 'magenta', text: 'ΔT pause', note: 'floor divergence' };
  } else if (floorOn) {
    floorLed = { color: 'green', text: 'Running', note: 'pump ON' };
  } else {
    floorLed = { color: 'off', text: 'Idle', note: 'pump OFF' };
  }

  const pumpLed = (on) => on
    ? { color: 'orange', text: 'Running', note: 'pump ON' }
    : { color: 'off', text: 'Idle', note: 'pump OFF' };

  return [
    { label: 'LED 0 · System', ...systemLed },
    heaterOn
      ? { label: 'LED 1 · Boiler', color: 'orange', text: 'Heating', note: 'heater ON' }
      : { label: 'LED 1 · Boiler', color: 'off', text: 'Standby', note: 'heater OFF' },
    { label: 'LED 2 · Floor', ...floorLed },
    { label: 'LED 3 · Rad 1', ...pumpLed(rad1On) },
    { label: 'LED 4 · Rad 2', ...pumpLed(rad2On) }
  ];
}

function renderDashboard() {
  const live = state.live;
  if (!live) {
    return `<div class="stack">
      <div class="banner info">Waiting for the controller's live state. If this persists, check the Wi-Fi link and the WebSocket connection.</div>
      ${loadingCard('Waiting for live state…')}
    </div>`;
  }

  const mode = live.mode || {};
  const wifi = live.wifi || {};
  const test = live.test || {};
  const circuits = live.circuits || {};
  const temps = live.temps || {};
  const tempHealth = live.tempHealth || {};
  const heater = live.heater || {};
  const safety = live.safety || {};
  const system = live.system || {};
  const power = live.power || {};
  const recent = state.events.slice(-6).reverse();

  const modeTone = mode.current === 'test' ? 'err' : mode.current === 'summer' ? 'ok' : 'warn';
  const wifiTone = wifi.connected ? (num(wifi.rssi, 0) > -75 ? 'ok' : 'warn') : 'err';

  return `<div class="stack">
    <div class="grid auto">
      ${metricCard('Mode', modeLabel(mode.current), `source: ${esc(mode.source || '—')}`, modeTone)}
      ${metricCard('Uptime', fmtUptime(live.uptime), `heap ${fmtBytes(system.freeHeap)}`)}
      ${metricCard('Wi-Fi', wifi.connected ? `${num(wifi.rssi, 0)} dBm` : 'offline', esc(wifi.connected ? (wifi.ip || '') : (wifi.state || '')), wifiTone)}
      ${metricCard('Test Mode', test.active ? 'ACTIVE' : 'off', test.active ? `remaining ${fmtDuration(test.remaining)}` : 'guarded', test.active ? 'err' : '')}
    </div>

    <div class="grid cols-2">
      <section class="card readonly accent-top">
        <div class="card-head">
          <div><h2>Circuits</h2><p>Command versus physical pump state</p></div>
          <span class="chip">${esc(modeLabel(mode.current))}</span>
        </div>
        <div class="circuit-grid">
          ${circuitCard('rad1', 'Radiator 1', circuits.rad1)}
          ${circuitCard('rad2', 'Radiator 2', circuits.rad2)}
          ${circuitCard('floor', 'Floor loop', circuits.floor)}
        </div>
      </section>

      <section class="card readonly accent-top">
        <div class="card-head">
          <div><h2>Temperatures</h2><p>Raw readings with age and validity</p></div>
          <span class="chip ${Object.values(tempHealth).every((h) => h.valid) ? 'chip-ok' : 'chip-warn'}">${Object.values(tempHealth).filter((h) => h.valid).length}/${Object.keys(tempHealth).length || 0} valid</span>
        </div>
        <div class="temp-grid">
          ${tempTile('Boiler supply', temps.boilerSupply, tempHealth.boilerSupply)}
          ${tempTile('Mixer forward', temps.mixerForward, tempHealth.mixerForward)}
          ${tempTile('Return', temps.return, tempHealth.return)}
          ${tempTile('Mixer cold in', temps.mixerColdIn, tempHealth.mixerColdIn)}
        </div>
      </section>
    </div>

    <div class="grid cols-2">
      <section class="card readonly">
        <div class="card-head">
          <div><h2>Heater</h2><p>Final authority remains with L1 safety</p></div>
          ${mismatchPill(heater.cmd, heater.phys)}
        </div>
        <div class="kv">
          <dt>Reason</dt><dd>${esc(heater.reason || '—')}</dd>
          <dt>Demand age</dt><dd>${num(heater.demandAge, 0) ? fmtAge(heater.demandAge) : '—'}</dd>
          <dt>Divergence</dt><dd>${esc(system.divergence || 'none')}</dd>
          <dt>Flush</dt><dd>${esc(system.flush || 'idle')}</dd>
        </div>
      </section>

      <section class="card readonly">
        <div class="card-head">
          <div><h2>Safety</h2><p>Active flags and controller error code</p></div>
          <span class="chip ${(safety.flags || []).length ? 'chip-warn' : 'chip-ok'}">${(safety.flags || []).length} active</span>
        </div>
        ${safetyFlags(safety.flags)}
        <div class="metric-row" style="margin-top:10px"><span class="k">Error code</span><span class="v">${esc(safety.errorCode ?? 0)}</span></div>
      </section>
    </div>

    <section class="card readonly">
      <div class="card-head">
        <div><h2>Status LEDs</h2><p>Carrier LED semantics from plan 02 §1.2</p></div>
        <span class="chip">5 LEDs</span>
      </div>
      <div class="status-leds">
        ${dashboardLeds(live).map((led) => `<div class="status-led-row" title="${esc(led.note)}">
          <span class="status-led ${esc(led.color)}"></span>
          <div class="status-led-label"><strong>${esc(led.label)}</strong><small>${esc(led.text)} · ${esc(led.note)}</small></div>
        </div>`).join('')}
      </div>
    </section>

    <div class="grid cols-2">
      <section class="card readonly">
        <div class="card-head"><div><h2>Pump power</h2><p>Estimated electrical draw</p></div></div>
        <div class="metric-row"><span class="k">Floor pump</span><span class="v">${fmtNum(power.floor, 0)} W</span></div>
        <div class="metric-row"><span class="k">Radiator 1</span><span class="v">${fmtNum(power.rad1, 0)} W</span></div>
        <div class="metric-row"><span class="k">Radiator 2</span><span class="v">${fmtNum(power.rad2, 0)} W</span></div>
        <div class="metric-row"><span class="k">Mixer</span><span class="v">${fmtNum(power.mixer, 0)} W</span></div>
      </section>

      <section class="card readonly">
        <div class="card-head">
          <div><h2>Recent events</h2><p>Latest entries from the in-RAM event log</p></div>
          <a class="btn btn-sm" href="#log/events">Open log</a>
        </div>
        ${recent.length ? recent.map(eventRowCompact).join('') : emptyCard('No events yet.')}
      </section>
    </div>
  </div>`;
}

/* ------------------------------------------------------------------------- */
/* Plant                                                                      */
/* ------------------------------------------------------------------------- */

function renderPlant() {
  const live = state.live;
  if (!live) return loadingCard('Waiting for live state…');

  const circuits = live.circuits || {};
  const temps = live.temps || {};
  const tempHealth = live.tempHealth || {};
  const heater = live.heater || {};
  const sensors = live.sensors || [];
  const floor = circuits.floor || {};
  const mixerPos = Math.max(0, Math.min(100, num(floor.mixerPos, 0)));
  const encoderPos = Math.max(0, Math.min(100, num(floor.encoderPos, 0)));

  return `<div class="stack">
    <div class="banner info">Read-only reference. Setpoints and mode are owned by Home Assistant; loop tuning is owned by the observer.</div>

    <section class="card readonly accent-top">
      <div class="card-head"><div><h2>Circuits</h2><p>Pumps and after-run timers</p></div></div>
      <div class="circuit-grid">
        ${circuitCard('rad1', 'Radiator 1', circuits.rad1)}
        ${circuitCard('rad2', 'Radiator 2', circuits.rad2)}
        ${circuitCard('floor', 'Floor loop', circuits.floor)}
      </div>
    </section>

    <div class="grid cols-2">
      <section class="card readonly">
        <div class="card-head"><div><h2>Mixer temperatures</h2><p>Hot inlet, cold inlet and mixed flow</p></div></div>
        <div class="temp-grid">
          ${tempTile('Hot inlet', temps.mixerHotIn, tempHealth.boilerSupply)}
          ${tempTile('Cold inlet', temps.mixerColdIn, tempHealth.mixerColdIn)}
          ${tempTile('Forward', temps.mixerForward, tempHealth.mixerForward)}
          ${tempTile('Return', temps.return, tempHealth.return)}
        </div>
        <div style="margin-top:14px">
          <div class="metric-row"><span class="k">Estimated position</span><span class="v">${mixerPos.toFixed(1)}%</span></div>
          <div class="bar"><span style="width:${mixerPos.toFixed(1)}%"></span></div>
          <div class="metric-row" style="margin-top:8px"><span class="k">Encoder position</span><span class="v">${encoderPos.toFixed(1)}% ${floor.encoderValid ? '' : '⚠ invalid'}</span></div>
        </div>
      </section>

      <section class="card readonly">
        <div class="card-head"><div><h2>Heater</h2><p>Command, physical state and reason</p></div>${mismatchPill(heater.cmd, heater.phys)}</div>
        <div class="kv">
          <dt>Reason</dt><dd>${esc(heater.reason || '—')}</dd>
          <dt>Demand age</dt><dd>${num(heater.demandAge, 0) ? fmtAge(heater.demandAge) : '—'}</dd>
          <dt>Divergence</dt><dd>${esc(live.system?.divergence || 'none')}</dd>
          <dt>Flush state</dt><dd>${esc(live.system?.flush || 'idle')}</dd>
          <dt>Error code</dt><dd>${esc(live.system?.errorCode ?? 0)}</dd>
        </div>
        <div style="margin-top:14px">${safetyFlags(live.safety?.flags || [])}</div>
      </section>
    </div>

    <section class="card readonly">
      <div class="card-head"><div><h2>Sensor health</h2><p>Raw values and age from the carrier</p></div></div>
      ${sensorTable(sensors)}
      <p class="small faint" style="margin-top:10px">Sensor offsets are not yet part of Config; values are raw.</p>
    </section>
  </div>`;
}

/* ------------------------------------------------------------------------- */
/* Sensor health                                                              */
/* ------------------------------------------------------------------------- */

function renderSensors() {
  const live = state.live;
  if (!live) return loadingCard('Waiting for live state…');

  const sensors = live.sensors || [];
  const bad = sensors.filter((s) => s.state !== 'ok').length;

  return `<div class="stack">
    <div class="banner ${bad ? 'warn' : 'ok'}">
      ${bad ? `${bad} sensor${bad === 1 ? '' : 's'} are stale, disconnected or in error.` : 'All configured sensors are valid.'}
    </div>
    <section class="card readonly accent-top">
      <div class="card-head">
        <div><h2>Sensor health</h2><p>Last value, age and consecutive error count</p></div>
        <span class="chip ${bad ? 'chip-warn' : 'chip-ok'}">${sensors.length - bad}/${sensors.length} healthy</span>
      </div>
      ${sensorTable(sensors)}
      <p class="small faint" style="margin-top:10px">Offsets are not yet configurable from the portal. The displayed values are raw carrier readings.</p>
    </section>
  </div>`;
}

/* ------------------------------------------------------------------------- */
/* Event log and portal access log                                            */
/* ------------------------------------------------------------------------- */

function filteredEvents() {
  if (state.eventFilter === 'all') return state.events;
  return state.events.filter((entry) => entry.severity === state.eventFilter);
}

function renderEventLog() {
  const events = filteredEvents().slice().reverse();
  const access = state.access.slice().reverse().slice(0, 30);

  return `<div class="stack">
    <section class="card readonly accent-top">
      <div class="card-head">
        <div><h2>Event log</h2><p>Controller events, safety notices and Test Mode actions</p></div>
        <span class="chip">${state.events.length} buffered</span>
      </div>

      <div class="row between" style="margin-bottom:12px">
        <div class="row">
          <label class="small muted" for="event-filter">Severity</label>
          <select id="event-filter" class="btn btn-sm" style="min-height:32px">
            ${['all', 'info', 'warn', 'error', 'safety'].map((sev) =>
              `<option value="${sev}" ${state.eventFilter === sev ? 'selected' : ''}>${sev === 'all' ? 'All' : sev}</option>`).join('')}
          </select>
          <button class="btn btn-sm" type="button" data-action="refresh-events">Refresh</button>
          <button class="btn btn-sm" type="button" data-action="export-events">Export CSV</button>
        </div>
        <span class="small faint">Timestamps are controller uptime</span>
      </div>

      ${events.length ? `<div class="table-wrap"><table>
        <thead><tr><th>Time</th><th>Severity</th><th>Code</th><th>Detail</th><th>Latched</th></tr></thead>
        <tbody>${events.map((entry) => `<tr>
          <td class="mono nowrap">${fmtT(entry.ts)}</td>
          <td><span class="${severityClass(entry.severity)}">${esc(entry.severity)}</span></td>
          <td class="mono">${esc(entry.code)}</td>
          <td>${esc(entry.detail)}</td>
          <td>${entry.latched ? '<span class="chip chip-safety">yes</span>' : '<span class="faint">no</span>'}</td>
        </tr>`).join('')}</tbody>
      </table></div>` : emptyCard('No events match the current filter.')}
    </section>

    <section class="card readonly">
      <div class="card-head">
        <div><h2>Portal access log</h2><p>Latest HTTP and WebSocket access records (in RAM)</p></div>
        <span class="chip">${state.access.length} buffered</span>
      </div>
      ${access.length ? `<div class="table-wrap"><table>
        <thead><tr><th>Time</th><th>Method</th><th>Path</th><th>Client</th><th>Status</th></tr></thead>
        <tbody>${access.map((entry) => `<tr>
          <td class="mono nowrap">${fmtT(entry.ts)}</td>
          <td class="mono">${esc(entry.method)}</td>
          <td class="mono">${esc(entry.path)}</td>
          <td class="mono">${esc(entry.ip)}</td>
          <td class="mono">${entry.status === 0 ? '—' : esc(entry.status)}</td>
        </tr>`).join('')}</tbody>
      </table></div>` : emptyCard('No access records yet.')}
      <p class="small faint" style="margin-top:10px">The access log is in RAM and holds the latest 64 requests. WebSocket status 101 means the upgrade succeeded; 0 is a disconnect.</p>
    </section>
  </div>`;
}

/* ------------------------------------------------------------------------- */
/* Slow log                                                                   */
/* ------------------------------------------------------------------------- */

function filteredSlowSamples() {
  const all = state.slow;
  if (state.slowRange === 'all' || all.length === 0) return all;
  const windowMs = Number(state.slowRange) * 1000;
  const latest = num(all[all.length - 1].ts, 0);
  return all.filter((sample) => num(sample.ts, 0) >= latest - windowMs);
}

function renderSlowChart(samples) {
  if (!samples.length) {
    return `<div class="chart-wrap"><div class="empty">No slow-log samples yet. The controller records a sample every 10 seconds.</div></div>`;
  }

  const width = 900;
  const height = 280;
  const pad = { l: 44, r: 14, t: 14, b: 28 };
  const series = [
    { key: 'boiler', cls: 'series-boiler' },
    { key: 'mix', cls: 'series-mix' },
    { key: 'ret', cls: 'series-ret' }
  ];

  const values = [];
  for (const sample of samples) {
    for (const s of series) {
      const v = num(sample[s.key]);
      if (v !== null) values.push(v);
    }
  }
  let min = values.length ? Math.min(...values) : 0;
  let max = values.length ? Math.max(...values) : 100;
  if (max - min < 5) {
    const mid = (max + min) / 2;
    min = mid - 2.5;
    max = mid + 2.5;
  }
  const margin = (max - min) * 0.12;
  min -= margin;
  max += margin;

  const x = (index) => pad.l + (samples.length === 1 ? 0 : (index / (samples.length - 1)) * (width - pad.l - pad.r));
  const y = (value) => pad.t + (1 - ((value - min) / (max - min))) * (height - pad.t - pad.b);

  const grid = [];
  for (let i = 0; i <= 4; i += 1) {
    const gy = pad.t + (i / 4) * (height - pad.t - pad.b);
    const label = max - (i / 4) * (max - min);
    grid.push(`<line class="grid-line" x1="${pad.l}" y1="${gy.toFixed(1)}" x2="${width - pad.r}" y2="${gy.toFixed(1)}"/>`);
    grid.push(`<text x="4" y="${(gy + 3).toFixed(1)}">${label.toFixed(1)}</text>`);
  }

  const lines = series.map((s) => {
    const points = [];
    samples.forEach((sample, index) => {
      const v = num(sample[s.key]);
      if (v !== null) points.push(`${x(index).toFixed(1)},${y(v).toFixed(1)}`);
    });
    return points.length ? `<polyline class="${s.cls}" points="${points.join(' ')}"/>` : '';
  }).join('');

  const labelIndexes = [...new Set([0, Math.floor((samples.length - 1) / 2), samples.length - 1])];
  const xLabels = labelIndexes.map((index) =>
    `<text x="${x(index).toFixed(1)}" y="${height - 7}" text-anchor="middle">${fmtT(samples[index].ts)}</text>`).join('');

  return `<div class="chart-wrap">
    <svg class="chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="Slow log temperature chart">
      ${grid.join('')}
      <line class="axis" x1="${pad.l}" y1="${height - pad.b}" x2="${width - pad.r}" y2="${height - pad.b}"/>
      ${lines}
      ${xLabels}
    </svg>
    <div class="legend">
      <span class="boiler"><i></i>Boiler supply</span>
      <span class="mix"><i></i>Mixer forward</span>
      <span class="ret"><i></i>Return</span>
    </div>
  </div>`;
}

function renderSlowLog() {
  const samples = filteredSlowSamples();
  const latest = samples.length ? samples[samples.length - 1] : null;

  return `<div class="stack">
    <section class="card readonly accent-top">
      <div class="card-head">
        <div><h2>Slow log</h2><p>10-second temperature and mixer-position samples (in RAM)</p></div>
        <span class="chip">${state.slow.length} samples</span>
      </div>

      <div class="row between" style="margin-bottom:12px">
        <div class="row">
          <label class="small muted" for="slow-range">Window</label>
          <select id="slow-range" class="btn btn-sm" style="min-height:32px">
            <option value="600" ${state.slowRange === '600' ? 'selected' : ''}>Last 10 min</option>
            <option value="1800" ${state.slowRange === '1800' ? 'selected' : ''}>Last 30 min</option>
            <option value="3600" ${state.slowRange === '3600' ? 'selected' : ''}>Last 60 min</option>
            <option value="all" ${state.slowRange === 'all' ? 'selected' : ''}>All</option>
          </select>
          <button class="btn btn-sm" type="button" data-action="refresh-slow">Refresh</button>
          <button class="btn btn-sm" type="button" data-action="export-slow">Export CSV</button>
        </div>
        <span class="small faint">Timestamps are controller uptime</span>
      </div>

      <div class="grid cols-4" style="margin-bottom:14px">
        ${metricCard('Boiler supply', fmtTemp(latest?.boiler), 'latest sample')}
        ${metricCard('Mixer forward', fmtTemp(latest?.mix), 'latest sample')}
        ${metricCard('Return', fmtTemp(latest?.ret), 'latest sample')}
        ${metricCard('Mixer position', latest ? `${(num(latest.pos, 0) * 100).toFixed(1)}%` : '—', 'estimated')}
      </div>

      ${renderSlowChart(samples)}
    </section>
  </div>`;
}

/* ------------------------------------------------------------------------- */
/* Relay counters                                                             */
/* ------------------------------------------------------------------------- */

function renderCounters() {
  const counters = state.counters;
  const persistence = state.counterPersistence || {};
  const persistenceLabel = persistence.enabled
    ? `NVS hourly${persistence.saveIntervalS ? ` (${fmtDuration(persistence.saveIntervalS)})` : ''}`
    : 'RAM only (emulation)';

  return `<div class="stack">
    <section class="card readonly accent-top">
      <div class="card-head">
        <div><h2>Relay counters</h2><p>Cycle counts and estimated runtime</p></div>
        <div class="row">
          <span class="chip ${persistence.enabled ? 'chip-ok' : 'chip-warn'}">${persistenceLabel}</span>
          <button class="btn btn-sm" type="button" data-action="refresh-counters">Refresh</button>
          <button class="btn btn-sm btn-danger" type="button" data-action="counter-reset" data-counter="all">Reset all</button>
        </div>
      </div>

      ${counters.length ? `<div class="grid auto">${counters.map((counter) => `<div class="card pad-sm">
        <div class="card-head">
          <div><h3>${esc(counter.label)}</h3><p class="small faint mono">${esc(counter.id)}</p></div>
          <span class="chip ${counter.persisted ? 'chip-ok' : 'chip-warn'}">${counter.persisted ? 'NVS' : 'RAM'}</span>
        </div>
        <div class="metric">
          <div class="metric-value sm">${esc(counter.cycles ?? 0)}</div>
          <div class="metric-sub">cycles</div>
        </div>
        <div class="metric-row"><span class="k">Runtime</span><span class="v">${fmtNum(counter.runtimeHours, 2)} h</span></div>
        <div class="metric-row"><span class="k">Last change</span><span class="v">${counter.lastOperated ? fmtT(counter.lastOperated) : '—'}</span></div>
        <div style="margin-top:10px"><button class="btn btn-sm" type="button" data-action="counter-reset" data-counter="${esc(counter.id)}">Reset</button></div>
      </div>`).join('')}</div>` : emptyCard('No counters yet.')}

      <p class="small faint" style="margin-top:12px">${persistence.enabled
        ? 'Counters are saved to NVS at most once per hour, and only when a cycle count or runtime value changed. A manual reset is saved promptly.'
        : 'Emulation builds deliberately keep counters in RAM only; they reset on reboot and never touch NVS.'}</p>
    </section>
  </div>`;
}

/* ------------------------------------------------------------------------- */
/* Test Mode                                                                  */
/* ------------------------------------------------------------------------- */

function testOutputRow(output, label, cmd, phys, note) {
  return `<div class="control-row">
    <div class="control-label"><strong>${esc(label)}</strong><small>${esc(note || '')}</small></div>
    <div class="row">
      ${mismatchPill(cmd, phys)}
      <button class="toggle-btn" type="button" data-action="test-toggle" data-output="${esc(output)}"
              data-on="${cmd ? 'true' : 'false'}" aria-pressed="${cmd ? 'true' : 'false'}">${cmd ? 'ON' : 'OFF'}</button>
    </div>
  </div>`;
}

function renderTest() {
  const live = state.live;
  if (!live) return loadingCard('Waiting for live state…');

  const test = live.test || {};
  const circuits = live.circuits || {};
  const floor = circuits.floor || {};
  const heater = live.heater || {};
  const safety = live.safety || {};
  const system = live.system || {};

  if (!test.active) {
    return `<div class="stack">
      <section class="card guarded accent-top">
        <div class="card-head">
          <div><h2>Test Mode is off</h2><p>Manual output control is guarded and time-limited.</p></div>
          <span class="chip chip-err">GUARDED</span>
        </div>
        <div class="banner warn">
          <strong>Before activating:</strong> Test Mode writes manual commands through the same intake path as HA.
          It is intended for bench verification only. L1 safety still retains final authority over the heater.
        </div>
        <ul class="small muted" style="margin:12px 0 16px 18px; padding:0">
          <li>Heater commands are refused unless at least one pump is commanded ON.</li>
          <li>The session expires automatically at the configured limit (2 h by default).</li>
          <li>Commands update <span class="mono">*_intended</span>; on the bench, L2 copies them to <span class="mono">*_requested</span>.</li>
        </ul>
        <button class="btn btn-danger btn-lg" type="button" data-action="test-activate">Activate Test Mode</button>
      </section>
    </div>`;
  }

  return `<div class="stack">
    <section class="card guarded accent-top">
      <div class="test-status">
        <div>
          <div class="metric-label">Test Mode active</div>
          <div class="countdown">${fmtDuration(test.remaining)}</div>
          <div class="metric-sub">The controller deactivates Test Mode automatically when the countdown reaches zero.</div>
        </div>
        <div class="row">
          <span class="chip ${test.sequence !== 'none' ? 'chip-warn' : ''}">sequence: ${esc(test.sequence || 'none')}</span>
          <button class="btn btn-danger" type="button" data-action="test-deactivate">Deactivate</button>
        </div>
      </div>
    </section>

    <div class="grid cols-2">
      <section class="card guarded">
        <div class="card-head">
          <div><h2>Manual outputs</h2><p>Command state versus physical relay state</p></div>
          <span class="chip chip-err">GUARDED</span>
        </div>
        ${testOutputRow('heater', 'Heater', heater.cmd, heater.phys, 'Requires at least one pump ON; L1 refuses otherwise.')}
        ${testOutputRow('pump_floor', 'Floor pump', floor.cmd, floor.phys, 'Floor loop pump')}
        ${testOutputRow('pump_rad1', 'Radiator 1 pump', circuits.rad1?.cmd, circuits.rad1?.phys, 'Radiator circuit 1')}
        ${testOutputRow('pump_rad2', 'Radiator 2 pump', circuits.rad2?.cmd, circuits.rad2?.phys, 'Radiator circuit 2')}
        <div class="banner info" style="margin-top:12px">Divergence: <span class="mono">${esc(system.divergence || 'none')}</span></div>
      </section>

      <section class="card guarded">
        <div class="card-head">
          <div><h2>Mixer</h2><p>Pulse the motor or store a target position</p></div>
          <span class="chip">${fmtNum(floor.mixerPos, 1)}%</span>
        </div>
        <div class="metric-row"><span class="k">Estimated position</span><span class="v">${fmtNum(floor.mixerPos, 1)}%</span></div>
        <div class="metric-row"><span class="k">Target position</span><span class="v">${fmtNum(floor.mixerTarget, 1)}%</span></div>
        <div class="metric-row"><span class="k">Encoder</span><span class="v">${fmtNum(floor.encoderPos, 1)}% ${floor.encoderValid ? '' : '⚠'}</span></div>

        <div class="row" style="margin-top:12px">
          <label class="small muted" for="mix-pulse">Pulse</label>
          <select id="mix-pulse" class="btn btn-sm" style="min-height:32px">
            <option value="500">500 ms</option>
            <option value="1000" selected>1000 ms</option>
          </select>
          <button class="btn btn-sm" type="button" data-action="test-mixer" data-mixer="open">Open</button>
          <button class="btn btn-sm" type="button" data-action="test-mixer" data-mixer="stop">Stop</button>
          <button class="btn btn-sm" type="button" data-action="test-mixer" data-mixer="close">Close</button>
        </div>

        <div class="field" style="margin-top:12px">
          <label for="mixer-target">Target position</label>
          <div class="range-row">
            <input id="mixer-target" type="range" min="0" max="100" step="1" value="${Math.round(num(floor.mixerTarget, 0))}" aria-label="Mixer target position">
            <span class="unit">%</span>
            <button class="btn btn-sm" type="button" data-action="test-target">Store</button>
          </div>
          <small>Stores the target only. The pulse engine is bench-only and does not run closed-loop position control.</small>
        </div>
      </section>
    </div>

    <section class="card guarded">
      <div class="card-head">
        <div><h2>Test sequences</h2><p>Scripted bench checks; some are not implemented yet</p></div>
        <span class="chip chip-warn">${esc(test.sequence || 'none')}</span>
      </div>
      <div class="sequence-grid">
        <div class="sequence-card">
          <h4>Anti-seize</h4>
          <p>Runs floor, rad1 and rad2 pumps for 30 seconds each, then stops them.</p>
          <button class="btn btn-sm btn-primary" type="button" data-action="test-sequence" data-sequence="anti-seize">Run sequence</button>
        </div>
        <div class="sequence-card">
          <h4>Mixer sweep</h4>
          <p>Full open/close sweep with endstop timing. Not implemented in this build.</p>
          <button class="btn btn-sm" type="button" disabled>Pending</button>
        </div>
        <div class="sequence-card">
          <h4>Endstop home</h4>
          <p>Drive to the closed endstop and re-zero the encoder. Not implemented.</p>
          <button class="btn btn-sm" type="button" disabled>Pending</button>
        </div>
        <div class="sequence-card">
          <h4>Heater + pump</h4>
          <p>Verifies the heater/pump interlock. Not implemented as a scripted sequence.</p>
          <button class="btn btn-sm" type="button" disabled>Pending</button>
        </div>
      </div>
      <div class="banner warn" style="margin-top:12px">
        ${(safety.flags || []).length ? `Active flags: ${esc((safety.flags || []).join(', '))}` : 'No active safety flags.'}
      </div>
    </section>
  </div>`;
}

/* ------------------------------------------------------------------------- */
/* Form field helpers                                                         */
/* ------------------------------------------------------------------------- */

function fieldRange(name, label, value, min, max, step, unit, help, dataType = 'number') {
  const v = num(value, min);
  return `<div class="field">
    <label for="f_${esc(name)}">${esc(label)}</label>
    <div class="range-row">
      <input type="range" min="${min}" max="${max}" step="${step}" value="${v}" data-sync="${esc(name)}" aria-label="${esc(label)}">
      <input id="f_${esc(name)}" data-field="${esc(name)}" data-type="${esc(dataType)}" type="number"
             min="${min}" max="${max}" step="${step}" value="${v}">
      <span class="unit">${esc(unit || '')}</span>
    </div>
    <small>${esc(help || '')}</small>
  </div>`;
}

function fieldNumber(name, label, value, min, max, step, help, dataType = 'int') {
  const v = num(value, min);
  return `<div class="field">
    <label for="f_${esc(name)}">${esc(label)}</label>
    <input id="f_${esc(name)}" data-field="${esc(name)}" data-type="${esc(dataType)}" type="number"
           min="${min}" max="${max}" step="${step}" value="${v}">
    <small>${esc(help || '')}</small>
  </div>`;
}

function fieldText(name, label, value, help, type = 'text', placeholder = '') {
  return `<div class="field">
    <label for="f_${esc(name)}">${esc(label)}</label>
    <input id="f_${esc(name)}" data-field="${esc(name)}" type="${esc(type)}" value="${esc(value ?? '')}"
           placeholder="${esc(placeholder)}" autocomplete="off" spellcheck="false">
    <small>${esc(help || '')}</small>
  </div>`;
}

function fieldPassword(name, label, hasValue, help) {
  return `<div class="field">
    <label for="f_${esc(name)}">${esc(label)}</label>
    <input id="f_${esc(name)}" data-field="${esc(name)}" data-type="password" type="password" value=""
           placeholder="${hasValue ? 'unchanged' : 'not set'}" autocomplete="new-password">
    <small>${esc(help || '')}</small>
  </div>`;
}

function fieldSelect(name, label, value, options, help) {
  return `<div class="field">
    <label for="f_${esc(name)}">${esc(label)}</label>
    <select id="f_${esc(name)}" data-field="${esc(name)}" data-type="int">
      ${options.map((option) => `<option value="${esc(option.value)}" ${String(option.value) === String(value) ? 'selected' : ''}>${esc(option.label)}</option>`).join('')}
    </select>
    <small>${esc(help || '')}</small>
  </div>`;
}

function fieldCheckbox(name, label, checked, help, index = null) {
  return `<div class="field">
    <label>${esc(label)}</label>
    <label class="checkbox-row">
      <input type="checkbox" data-field="${esc(name)}" ${index !== null ? `data-index="${esc(index)}"` : ''} ${checked ? 'checked' : ''}>
      <span>${esc(help || '')}</span>
    </label>
  </div>`;
}

function formActions(label, extra = '') {
  return `<div class="form-actions">
    <button class="btn btn-primary" type="submit">${esc(label)}</button>
    ${extra}
    <span class="spacer"></span>
    <span class="small faint">NVS is written only on Save.</span>
  </div>`;
}

/* ------------------------------------------------------------------------- */
/* Settings: Safety & Timeouts                                                */
/* ------------------------------------------------------------------------- */

function renderSettingsSafety() {
  const cfg = state.config.safety;
  if (!cfg) return loadingCard('Loading safety settings…');

  return `<div class="stack">
    <div class="banner info">Writable configuration. Safety values take effect immediately after Save; they are persisted to NVS.</div>
    <form class="settings-form writable" data-section="safety">
      <section class="card writable accent-top">
        <div class="card-head"><div><h2>Safety &amp; timeouts</h2><p>Supervision bounds and timing constants</p></div><span class="chip chip-warn">NVS</span></div>

        <div class="form-section">
          <h3>Temperature limits</h3>
          <div class="form-grid">
            ${fieldRange('t_max_hard', 'Hard maximum', cfg.t_max_hard, 20, 90, 0.5, '°C', 'Immediate heater cut-off and hard over-temperature alarm.')}
            ${fieldRange('t_max_warn', 'High warning', cfg.t_max_warn, 20, 90, 0.5, '°C', 'Warning threshold; must remain below the hard maximum.')}
            ${fieldRange('t_overtemp_hysteresis', 'Over-temp hysteresis', cfg.t_overtemp_hysteresis, 0.5, 10, 0.1, 'K', 'Recovery band after an over-temperature pause.')}
            ${fieldRange('grad_limit', 'Gradient limit', cfg.grad_limit, 0.05, 5, 0.05, 'K/min', 'Maximum acceptable boiler supply gradient.')}
          </div>
        </div>

        <div class="form-section">
          <h3>Mixer and demand</h3>
          <div class="form-grid">
            ${fieldRange('mix_safe_pos', 'Safe mixer position', cfg.mix_safe_pos, 0, 1, 0.01, '0–1', 'Position used when the mixer must be parked safely.')}
            ${fieldNumber('boiler_min_off_ms', 'Boiler minimum off time', cfg.boiler_min_off_ms, 0, 3600000, 1000, 'Milliseconds.', 'int')}
            ${fieldNumber('demand_timeout_s', 'Demand timeout', cfg.demand_timeout_s, 10, 3600, 1, 'Seconds without a fresh HA demand.', 'int')}
            ${fieldNumber('pump_stop_after_heating_s', 'Pump after-run', cfg.pump_stop_after_heating_s, 0, 3600, 1, 'Seconds pumps keep running after heating stops.', 'int')}
            ${fieldNumber('long_pause_s', 'Long-pause threshold', cfg.long_pause_s, 0, 86400, 1, 'Seconds before a long-pause divergence is reported.', 'int')}
          </div>
        </div>

        <div class="form-section">
          <h3>Maintenance and Test Mode</h3>
          <div class="form-grid">
            ${fieldNumber('anti_seize_interval_s', 'Anti-seize interval', cfg.anti_seize_interval_s, 3600, 7776000, 3600, 'Seconds between anti-seize runs.', 'int')}
            ${fieldNumber('test_mode_max_s', 'Test Mode maximum', cfg.test_mode_max_s, 60, 7200, 60, 'Seconds before Test Mode expires automatically.', 'int')}
            ${fieldNumber('sensor_error_threshold', 'Sensor error threshold', cfg.sensor_error_threshold, 1, 10, 1, 'Consecutive failed reads before a sensor is marked in error.', 'int')}
          </div>
        </div>

        ${formActions('Save safety settings')}
      </section>
    </form>
  </div>`;
}

/* ------------------------------------------------------------------------- */
/* Settings: Hardware & Pins                                                  */
/* ------------------------------------------------------------------------- */

function renderSettingsHardware() {
  const cfg = state.config.hardware;
  if (!cfg) return loadingCard('Loading hardware settings…');

  const pinHelp = 'GPIO number, or 255 for unassigned.';
  const pumpPower = Array.isArray(cfg.has_pump_power) ? cfg.has_pump_power : [false, false, false];

  return `<div class="stack">
    <div class="banner warn">Writable configuration. Pin and tick changes require a reboot to take effect.</div>
    <form class="settings-form writable" data-section="hardware">
      <section class="card writable accent-top">
        <div class="card-head"><div><h2>Hardware &amp; pins</h2><p>Actuator constants, GPIO map and sensor identities</p></div><span class="chip chip-warn">REBOOT REQUIRED</span></div>

        <div class="form-section">
          <h3>Mixer and control constants</h3>
          <div class="form-grid">
            ${fieldRange('setpoint_default', 'Default setpoint', cfg.setpoint_default, 5, 80, 0.5, '°C', 'Fallback setpoint before HA supplies one.')}
            ${fieldRange('deadband_pos', 'Position deadband', cfg.deadband_pos, 0, 0.2, 0.005, '0–1', 'Mixer position deadband.')}
            ${fieldRange('deadband_temp', 'Temperature deadband', cfg.deadband_temp, 0.1, 5, 0.1, 'K', 'Temperature deadband around the setpoint.')}
            ${fieldRange('recalc_threshold', 'Recalculation threshold', cfg.recalc_threshold, 0.01, 1, 0.01, 'K', 'Minimum temperature change before recalculating.')}
            ${fieldNumber('min_pulse_ms', 'Minimum pulse', cfg.min_pulse_ms, 10, 1000, 1, 'Milliseconds.', 'int')}
            ${fieldNumber('max_pulse_ms', 'Maximum pulse', cfg.max_pulse_ms, 10, 10000, 1, 'Milliseconds.', 'int')}
            ${fieldNumber('pulse_cooldown_ms', 'Pulse cooldown', cfg.pulse_cooldown_ms, 0, 60000, 1, 'Milliseconds between pulses.', 'int')}
            ${fieldNumber('travel_time_ms', 'Full travel time', cfg.travel_time_ms, 1000, 600000, 1000, 'Milliseconds from closed to open.', 'int')}
            ${fieldNumber('tick_ms', 'Control tick', cfg.tick_ms, 100, 60000, 100, 'Milliseconds. Reboot required.', 'int')}
            ${fieldNumber('temp_poll_interval_ms', 'Temperature poll', cfg.temp_poll_interval_ms, 100, 60000, 100, 'Milliseconds between sensor polls.', 'int')}
            ${fieldNumber('encoder_check_interval_s', 'Encoder check interval', cfg.encoder_check_interval_s, 60, 86400, 60, 'Seconds between encoder plausibility checks.', 'int')}
            ${fieldNumber('encoder_check_pulses', 'Encoder check pulses', cfg.encoder_check_pulses, 1, 10000, 1, 'Pulses used for the plausibility check.', 'int')}
            ${fieldNumber('encoder_staleness_s', 'Encoder staleness', cfg.encoder_staleness_s, 10, 86400, 10, 'Seconds before the encoder is considered stale.', 'int')}
            ${fieldSelect('pos_source', 'Position source', cfg.pos_source, [
              { value: 0, label: 'HA generic (0)' },
              { value: 1, label: 'I2C encoder (1)' }
            ], 'Selects the position feedback source.')}
          </div>
        </div>

        <div class="form-section">
          <h3>GPIO map</h3>
          <div class="form-grid">
            ${fieldNumber('gpio_pump_floor', 'Floor pump GPIO', cfg.gpio_pump_floor, 0, 255, 1, pinHelp, 'int')}
            ${fieldNumber('gpio_pump_rad1', 'Radiator 1 pump GPIO', cfg.gpio_pump_rad1, 0, 255, 1, pinHelp, 'int')}
            ${fieldNumber('gpio_pump_rad2', 'Radiator 2 pump GPIO', cfg.gpio_pump_rad2, 0, 255, 1, pinHelp, 'int')}
            ${fieldNumber('gpio_heater', 'Heater GPIO', cfg.gpio_heater, 0, 255, 1, pinHelp, 'int')}
            ${fieldNumber('gpio_mix_open', 'Mixer open GPIO', cfg.gpio_mix_open, 0, 255, 1, pinHelp, 'int')}
            ${fieldNumber('gpio_mix_close', 'Mixer close GPIO', cfg.gpio_mix_close, 0, 255, 1, pinHelp, 'int')}
            ${fieldNumber('gpio_onewire', 'OneWire GPIO', cfg.gpio_onewire, 0, 255, 1, pinHelp, 'int')}
            ${fieldNumber('gpio_led_data', 'LED data GPIO', cfg.gpio_led_data, 0, 255, 1, pinHelp, 'int')}
            ${fieldNumber('num_leds', 'Number of LEDs', cfg.num_leds, 0, 8, 1, '0 disables the LED ring.', 'int')}
            ${fieldNumber('i2c_sda', 'I2C SDA GPIO', cfg.i2c_sda, 0, 255, 1, pinHelp, 'int')}
            ${fieldNumber('i2c_scl', 'I2C SCL GPIO', cfg.i2c_scl, 0, 255, 1, pinHelp, 'int')}
          </div>
        </div>

        <div class="form-section">
          <h3>Sensor identities</h3>
          <div class="form-grid">
            ${fieldText('sensor_id_boiler', 'Boiler sensor ID', cfg.sensor_id_boiler, 'Optional unique ID used in logs and UI.', 'text', 'boiler')}
            ${fieldText('sensor_id_ret', 'Return sensor ID', cfg.sensor_id_ret, 'Optional unique ID used in logs and UI.', 'text', 'return')}
            ${fieldText('sensor_id_mix', 'Mixer sensor ID', cfg.sensor_id_mix, 'Optional unique ID used in logs and UI.', 'text', 'mix')}
          </div>
        </div>

        <div class="form-section">
          <h3>Pump power feedback</h3>
          <div class="form-grid">
            ${fieldCheckbox('has_pump_power', 'Floor pump power sensor', pumpPower[0], 'Enable if a current/power sensor is fitted.', 0)}
            ${fieldCheckbox('has_pump_power', 'Radiator 1 power sensor', pumpPower[1], 'Enable if a current/power sensor is fitted.', 1)}
            ${fieldCheckbox('has_pump_power', 'Radiator 2 power sensor', pumpPower[2], 'Enable if a current/power sensor is fitted.', 2)}
          </div>
        </div>

        ${formActions('Save hardware settings')}
      </section>
    </form>
  </div>`;
}

/* ------------------------------------------------------------------------- */
/* Settings: Network & MQTT                                                   */
/* ------------------------------------------------------------------------- */

function renderSettingsNetwork() {
  const cfg = state.config.network;
  if (!cfg) return loadingCard('Loading network settings…');
  const wifi = state.live?.wifi || state.status?.wifi || {};
  const mqtt = state.live?.mqtt || state.status?.mqtt || {};
  const mqttState = mqtt.connected ? `connected ${esc(mqtt.broker || '')}` : esc(mqtt.state || (mqtt.enabled ? 'offline' : 'disabled'));

  return `<div class="stack">
    <div class="banner warn">Writable configuration. Wi-Fi changes require a reboot; MQTT reconnects automatically after saving.</div>
    <form class="settings-form writable" data-section="network">
      <section class="card writable accent-top">
        <div class="card-head">
          <div><h2>Network &amp; MQTT</h2><p>Station Wi-Fi and broker connection</p></div>
          <span class="chip ${wifi.connected ? 'chip-ok' : 'chip-warn'}">${wifi.connected ? `connected ${esc(wifi.ip || '')}` : 'not connected'}</span>
        </div>

        <div class="form-section">
          <h3>Wi-Fi station</h3>
          <div class="form-grid">
            ${fieldText('wifi_ssid', 'Wi-Fi SSID', cfg.wifi_ssid, 'Station network name.', 'text', 'Testing')}
            ${fieldPassword('wifi_password', 'Wi-Fi password', cfg.has_wifi_password, 'Leave blank to keep the stored password.')}
            ${fieldText('ap_ssid', 'Fallback AP SSID', cfg.ap_ssid, 'Reserved for the future provisioning portal.', 'text', 'HeatpumpsN-Setup')}
            ${fieldPassword('ap_password', 'Fallback AP password', cfg.has_ap_password, 'Leave blank to keep the stored password.')}
          </div>
        </div>

        <div class="form-section">
          <div class="row between">
            <h3>MQTT broker</h3>
            <span class="chip ${mqtt.connected ? 'chip-ok' : (mqtt.enabled ? 'chip-warn' : '')}">${mqttState}</span>
          </div>
          <div class="form-grid">
            ${fieldText('mqtt_host', 'Broker host', cfg.mqtt_host, 'Hostname or IP address. Leave empty to disable MQTT.', 'text', 'homeassistant.local')}
            ${fieldNumber('mqtt_port', 'Broker port', cfg.mqtt_port, 1, 65535, 1, 'Usually 1883.', 'int')}
            ${fieldText('mqtt_user', 'Username', cfg.mqtt_user, 'Optional broker username.', 'text', '')}
            ${fieldPassword('mqtt_pass', 'Password', cfg.has_mqtt_password, 'Leave blank to keep the stored password.')}
            ${fieldText('mqtt_base_topic', 'Base topic', cfg.mqtt_base_topic, 'Topic prefix used by HA discovery and state publishing.', 'text', 'heatpumpsn/controller')}
          </div>
        </div>

        ${formActions('Save network settings', `<button class="btn" type="button" data-action="mqtt-test">Test MQTT</button>`)}
        <p class="small faint">Home Assistant discovery is published under <span class="mono">homeassistant/</span>. Commands use <span class="mono">&lt;base&gt;/cmd/...</span> and state uses <span class="mono">&lt;base&gt;/state/...</span>. Leave the broker host empty to disable MQTT.</p>
      </section>
    </form>
  </div>`;
}

/* ------------------------------------------------------------------------- */
/* Settings: Firmware update                                                  */
/* ------------------------------------------------------------------------- */

function renderSettingsFirmware() {
  const status = state.status;
  if (!status) return loadingCard('Loading firmware information…');
  const fs = status.filesystem || {};
  const usedPct = fs.total ? Math.min(100, (fs.used / fs.total) * 100) : 0;

  return `<div class="stack">
    <div class="banner warn">OTA updates the application image only. LittleFS assets are uploaded separately with <span class="mono">pio remote run --target uploadfs</span>.</div>

    <section class="card writable accent-top">
      <div class="card-head"><div><h2>Firmware update</h2><p>Upload a compiled application image over HTTP</p></div><span class="chip">${esc(status.version || '?')}</span></div>
      <div class="grid cols-2">
        <div class="kv">
          <dt>Version</dt><dd>${esc(status.version || '?')}</dd>
          <dt>Build</dt><dd>${esc(status.build || '?')}</dd>
          <dt>Chip</dt><dd>${esc(status.chip || '?')} · ${esc(status.cores || '?')} cores</dd>
          <dt>SDK</dt><dd>${esc(status.sdk || '?')}</dd>
          <dt>Free heap</dt><dd>${fmtBytes(status.freeHeap)}</dd>
        </div>
        <div>
          <div class="metric-label">LittleFS usage</div>
          <div class="metric-value sm">${usedPct.toFixed(1)}%</div>
          <div class="bar" style="margin:8px 0"><span style="width:${usedPct.toFixed(1)}%"></span></div>
          <div class="small faint">${fmtBytes(fs.used)} used of ${fmtBytes(fs.total)}</div>
        </div>
      </div>

      <div class="form-section" style="margin-top:16px">
        <h3>Upload</h3>
        <div class="field">
          <label for="ota-file">Application image (.bin)</label>
          <input id="ota-file" type="file" accept=".bin,application/octet-stream">
          <small>Select the firmware binary produced by PlatformIO. Do not power off during the update.</small>
        </div>
        <div class="progress hidden" id="ota-progress-wrap">
          <div class="bar"><span id="ota-progress" style="width:0%"></span></div>
          <div class="small faint" id="ota-progress-text">Preparing…</div>
        </div>
        <div class="form-actions">
          <button class="btn btn-danger" type="button" data-action="ota-upload">Upload and reboot</button>
          <span class="small faint">The controller validates the image and reboots on success.</span>
        </div>
      </div>
    </section>
  </div>`;
}

/* ------------------------------------------------------------------------- */
/* Settings: System                                                           */
/* ------------------------------------------------------------------------- */

function renderSettingsSystem() {
  const cfg = state.config.system;
  if (!cfg) return loadingCard('Loading system settings…');
  const status = state.status || {};
  const theme = localStorage.getItem('hpn-theme') || 'dark';

  return `<div class="stack">
    <form class="settings-form writable" data-section="system">
      <section class="card writable accent-top">
        <div class="card-head"><div><h2>System</h2><p>Local mode and presentation</p></div><span class="chip">LOCAL</span></div>

        <div class="form-section">
          <h3>Operating mode</h3>
          <div class="form-grid">
            ${fieldCheckbox('summer_mode', 'Summer mode', cfg.summer_mode, 'Disables heating demand while still allowing circulation and diagnostics.')}
            <div class="field">
              <label for="theme-select">Theme</label>
              <select id="theme-select">
                <option value="dark" ${theme === 'dark' ? 'selected' : ''}>Dark</option>
                <option value="light" ${theme === 'light' ? 'selected' : ''}>Light</option>
                <option value="auto" ${theme === 'auto' ? 'selected' : ''}>Follow system</option>
              </select>
              <small>Stored in this browser only; not written to the controller.</small>
            </div>
          </div>
        </div>

        ${formActions('Save system settings')}
      </section>
    </form>

    <section class="card readonly">
      <div class="card-head"><div><h2>Controller information</h2><p>Read-only status</p></div></div>
      <div class="kv">
        <dt>Firmware</dt><dd>${esc(status.version || '?')} · ${esc(status.build || '?')}</dd>
        <dt>Chip</dt><dd>${esc(status.chip || '?')} · ${esc(status.cores || '?')} cores</dd>
        <dt>Uptime</dt><dd>${fmtUptime(status.uptime || 0)}</dd>
        <dt>Free heap</dt><dd>${fmtBytes(status.freeHeap)}</dd>
        <dt>Wi-Fi</dt><dd>${esc(status.wifi?.ip || '—')} · ${esc(status.wifi?.state || '—')}</dd>
      </div>
      <div class="banner info" style="margin-top:12px">Hostname, NTP and timezone are not yet part of Config, so they are not exposed as editable fields.</div>
    </section>

    <section class="card guarded">
      <div class="card-head"><div><h2>Maintenance</h2><p>Reboot and destructive actions</p></div><span class="chip chip-err">GUARDED</span></div>
      <div class="row">
        <button class="btn" type="button" data-action="reboot">Reboot controller</button>
        <button class="btn btn-danger" type="button" data-action="factory-reset">Factory reset</button>
      </div>
      <p class="small faint" style="margin-top:10px">Factory reset clears the NVS configuration and relay counters, then reboots. The event log is in RAM and is lost on reboot.</p>
    </section>
  </div>`;
}

/* ------------------------------------------------------------------------- */
/* Runtime parameters (read-only)                                             */
/* ------------------------------------------------------------------------- */

function renderRuntime() {
  const runtime = state.runtime;
  if (!runtime) return loadingCard('Loading runtime parameters…');
  const ha = runtime.ha || {};
  const observer = runtime.observer || {};

  return `<div class="stack">
    <div class="banner info">Read-only reference. HA owns the HA fields; the observer owns loop tuning. The portal does not command the heating process.</div>

    <div class="grid cols-2">
      <section class="card readonly accent-top">
        <div class="card-head"><div><h2>HA-owned</h2><p>Last known values from Home Assistant</p></div><span class="chip">🔒 read-only</span></div>
        <div class="kv">
          <dt>Floor setpoint shadow</dt><dd>${fmtTemp(ha.floor_setpoint_shadow)}</dd>
          <dt>Summer mode</dt><dd>${ha.summer_mode ? 'on' : 'off'}</dd>
          <dt>Last received</dt><dd>${ha.last_received_ms ? fmtT(ha.last_received_ms) : 'never'}</dd>
          <dt>Source</dt><dd>${esc(ha.source || '—')}</dd>
        </div>
      </section>

      <section class="card readonly accent-top">
        <div class="card-head"><div><h2>Observer-owned</h2><p>Loop tuning and flush parameters</p></div><span class="chip ${observer.healthy ? 'chip-ok' : 'chip-warn'}">${observer.healthy ? 'healthy' : 'unavailable'}</span></div>
        <div class="kv">
          <dt>Version</dt><dd>${esc(observer.version || '—')}</dd>
          <dt>Delay</dt><dd>${fmtNum(observer.delay_s, 2)} s</dd>
          <dt>Gain</dt><dd>${fmtNum(observer.gain, 3)}</dd>
          <dt>Feed-forward boiler</dt><dd>${fmtNum(observer.ff_boiler, 3)}</dd>
          <dt>Feed-forward return</dt><dd>${fmtNum(observer.ff_return, 3)}</dd>
          <dt>Kp / Ki</dt><dd>${fmtNum(observer.kp, 3)} / ${fmtNum(observer.ki, 3)}</dd>
          <dt>Radiator coupling FF</dt><dd>${fmtNum(observer.rad_coupling_ff, 3)}</dd>
          <dt>Start flush</dt><dd>${esc(observer.start_flush_ms)} ms</dd>
          <dt>Flush max position</dt><dd>${fmtNum(observer.flush_max_pos, 2)}</dd>
          <dt>Flush ΔT OK</dt><dd>${fmtNum(observer.flush_delta_t_ok, 2)} K</dd>
          <dt>Minimum useful ΔT</dt><dd>${fmtNum(observer.min_useful_delta_t, 2)} K</dd>
          <dt>Floor pause duration</dt><dd>${esc(observer.floor_pause_duration_s)} s</dd>
        </div>
      </section>
    </div>
  </div>`;
}

/* ------------------------------------------------------------------------- */
/* Forms and actions                                                          */
/* ------------------------------------------------------------------------- */

function bindFormBehaviors(root) {
  $$('input[type="range"][data-sync]', root).forEach((range) => {
    const target = root.querySelector(`[data-field="${range.dataset.sync}"]`);
    if (!target) return;
    range.addEventListener('input', () => { target.value = range.value; });
    target.addEventListener('input', () => { range.value = target.value; });
  });

  $$('form.settings-form', root).forEach((form) => {
    const markDirty = (event) => {
      if (event?.target?.id === 'theme-select') return;
      setDirty(true);
      form.classList.add('dirty');
    };
    form.addEventListener('input', markDirty);
    form.addEventListener('change', markDirty);
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      await saveSettingsForm(form);
    });
  });
}

function collectForm(form) {
  const data = {};
  $$('[data-field]', form).forEach((input) => {
    const key = input.dataset.field;
    if (key === 'has_pump_power') {
      if (!Array.isArray(data.has_pump_power)) data.has_pump_power = [false, false, false];
      data.has_pump_power[Number(input.dataset.index || 0)] = input.checked;
      return;
    }
    if (input.type === 'checkbox') {
      data[key] = input.checked;
      return;
    }
    if (input.dataset.type === 'int') {
      data[key] = parseInt(input.value, 10);
      return;
    }
    if (input.dataset.type === 'number') {
      data[key] = Number(input.value);
      return;
    }
    if (input.dataset.type === 'bool') {
      data[key] = input.value === 'true';
      return;
    }
    if (input.dataset.type === 'password') {
      if (input.value !== '') data[key] = input.value;
      return;
    }
    data[key] = input.value;
  });
  return data;
}

async function saveSettingsForm(form) {
  const section = form.dataset.section;
  const submit = form.querySelector('button[type="submit"]');
  if (submit) submit.disabled = true;
  try {
    const result = await apiPut(`/api/config/${section}`, collectForm(form));
    setDirty(false);
    form.classList.remove('dirty');
    toast(
      result.reboot_required
        ? 'Settings saved. Reboot required for the new values to take effect.'
        : 'Settings saved to NVS.',
      result.reboot_required ? 'warn' : 'ok',
      result.reboot_required ? 6500 : 3500
    );
    await refreshConfig(section);
    renderCurrent();
  } catch (err) {
    toast(err.message, 'err', 6000);
  } finally {
    if (submit) submit.disabled = false;
  }
}

async function activateTest() {
  const ok = await confirmDialog(
    'Activate Test Mode?',
    'Manual output control bypasses HA demand. The session is time-limited and L1 still retains final authority over the heater. Use it only for bench verification.',
    'Activate',
    true
  );
  if (!ok) return;
  try {
    await apiPost('/api/test/activate');
    toast('Test Mode activated.', 'warn');
    await delay(300);
    await refreshState();
  } catch (err) {
    toast(err.message, 'err');
  }
}

async function deactivateTest() {
  const ok = await confirmDialog(
    'Deactivate Test Mode?',
    'All manual commands will be cleared and the controller will return to its configured mode.',
    'Deactivate',
    true
  );
  if (!ok) return;
  try {
    await apiPost('/api/test/deactivate');
    toast('Test Mode deactivated.', 'ok');
    await delay(300);
    await refreshState();
  } catch (err) {
    toast(err.message, 'err');
  }
}

async function testToggle(button) {
  const output = button.dataset.output;
  const current = button.getAttribute('aria-pressed') === 'true';
  const next = !current;
  try {
    const result = await apiPost('/api/test/command', { output, on: next });
    toast(`Requested ${output} ${next ? 'ON' : 'OFF'}${result.note ? ` (${result.note})` : ''}.`, 'ok');
  } catch (err) {
    toast(err.message, 'err', 6000);
  }
  await delay(300);
  await refreshState();
}

async function testMixer(mixer) {
  try {
    const pulse = Number($('#mix-pulse')?.value || 1000);
    const result = await apiPost('/api/test/command', { mixer, pulse_ms: pulse });
    toast(`Mixer ${mixer}${result.note ? ` (${result.note})` : ''}.`, 'ok');
  } catch (err) {
    toast(err.message, 'err', 6000);
  }
  await delay(300);
  await refreshState();
}

async function testTarget() {
  const slider = $('#mixer-target');
  if (!slider) return;
  const position = Number(slider.value) / 100;
  try {
    const result = await apiPost('/api/test/command', { mixer: 'target', position });
    toast(`Mixer target stored at ${(position * 100).toFixed(0)}%${result.note ? ` (${result.note})` : ''}.`, 'ok');
  } catch (err) {
    toast(err.message, 'err', 6000);
  }
  await delay(300);
  await refreshState();
}

async function runTestSequence(id) {
  const ok = await confirmDialog(
    'Run test sequence?',
    `Run the "${id}" sequence now? It will command pumps and/or the mixer through the Test Mode path.`,
    'Run sequence',
    true
  );
  if (!ok) return;
  try {
    await apiPost(`/api/test/sequence/${id}`);
    toast(`Sequence "${id}" started.`, 'warn');
  } catch (err) {
    toast(err.message, 'err', 6000);
  }
  await delay(300);
  await refreshState();
}

async function resetCounter(id) {
  const label = id === 'all' ? 'all relay counters' : `the "${id}" counter`;
  const persistence = state.counterPersistence || {};
  const storage = persistence.enabled ? 'The reset is saved to NVS promptly.' : 'Counters are RAM-only under emulation.';
  const ok = await confirmDialog(
    'Reset counters?',
    `Reset ${label}? ${storage} This cannot be undone.`,
    'Reset',
    true
  );
  if (!ok) return;
  try {
    await apiPost('/api/counters/reset', { counter: id || 'all' });
    toast('Relay counters reset.', 'ok');
    await refreshCounters();
    renderCurrent();
  } catch (err) {
    toast(err.message, 'err');
  }
}

async function rebootController() {
  const ok = await confirmDialog(
    'Reboot controller?',
    'The portal will disconnect for a few seconds while the controller restarts.',
    'Reboot',
    true
  );
  if (!ok) return;
  try {
    await apiPost('/api/system/reboot');
    toast('Reboot requested. The portal will reconnect shortly.', 'warn', 6000);
  } catch (err) {
    toast(err.message, 'err');
  }
}

async function factoryReset() {
  let ok = await confirmDialog(
    'Factory reset?',
    'All NVS configuration will be erased and the controller will reboot. This cannot be undone.',
    'Continue',
    true
  );
  if (!ok) return;
  ok = await confirmDialog(
    'Confirm factory reset',
    'Erase the controller configuration now?',
    'Erase and reboot',
    true
  );
  if (!ok) return;
  try {
    await apiPost('/api/system/factory-reset');
    toast('Factory reset requested. The controller is rebooting.', 'warn', 7000);
  } catch (err) {
    toast(err.message, 'err');
  }
}

function setOtaProgress(percent, text) {
  const wrap = $('#ota-progress-wrap');
  const bar = $('#ota-progress');
  const label = $('#ota-progress-text');
  if (wrap) wrap.classList.remove('hidden');
  if (bar) bar.style.width = `${Math.max(0, Math.min(100, percent)).toFixed(1)}%`;
  if (label && text) label.textContent = text;
}

async function uploadFirmware() {
  const input = $('#ota-file');
  const file = input?.files?.[0];
  if (!file) {
    toast('Choose a firmware .bin file first.', 'warn');
    return;
  }
  if (state.otaBusy) return;

  const ok = await confirmDialog(
    'Upload firmware?',
    `Upload "${file.name}" (${fmtBytes(file.size)}) and reboot the controller? Do not power off during the update.`,
    'Upload and reboot',
    true
  );
  if (!ok) return;

  state.otaBusy = true;
  setOtaProgress(0, 'Uploading…');

  const formData = new FormData();
  formData.append('firmware', file, file.name);

  const xhr = new XMLHttpRequest();
  xhr.open('POST', '/api/update');
  xhr.upload.onprogress = (event) => {
    if (!event.lengthComputable) return;
    const percent = (event.loaded / event.total) * 100;
    setOtaProgress(percent, `Uploading… ${percent.toFixed(0)}%`);
  };
  xhr.onload = () => {
    state.otaBusy = false;
    let data = null;
    try { data = xhr.responseText ? JSON.parse(xhr.responseText) : null; } catch (err) { /* ignore */ }
    if (xhr.status >= 200 && xhr.status < 300 && data?.ok) {
      setOtaProgress(100, 'Update accepted. Rebooting…');
      toast('Firmware update accepted. The controller is rebooting.', 'ok', 7000);
    } else {
      setOtaProgress(0, 'Update failed.');
      toast(data?.error || `Firmware update failed (HTTP ${xhr.status}).`, 'err', 7000);
    }
  };
  xhr.onerror = () => {
    state.otaBusy = false;
    setOtaProgress(0, 'Upload failed.');
    toast('Firmware upload failed. Check the connection and try again.', 'err', 7000);
  };
  xhr.send(formData);
}

function csvEscape(value) {
  const text = String(value ?? '');
  return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

function downloadText(filename, text, mime = 'text/plain;charset=utf-8') {
  const blob = new Blob([text], { type: mime });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function exportEvents() {
  const events = filteredEvents();
  const rows = [
    ['seq', 'uptime_s', 'severity', 'code', 'detail', 'latched'],
    ...events.map((entry) => [
      entry.seq,
      Math.floor(num(entry.ts, 0) / 1000),
      entry.severity,
      entry.code,
      entry.detail,
      entry.latched ? 1 : 0
    ])
  ];
  downloadText('hpn-events.csv', rows.map((row) => row.map(csvEscape).join(',')).join('\n'), 'text/csv;charset=utf-8');
}

function exportSlow() {
  const samples = filteredSlowSamples();
  const rows = [
    ['uptime_s', 'boiler_c', 'mixer_forward_c', 'return_c', 'mixer_position', 'heater', 'rad1', 'rad2', 'floor'],
    ...samples.map((sample) => [
      Math.floor(num(sample.ts, 0) / 1000),
      sample.boiler,
      sample.mix,
      sample.ret,
      sample.pos,
      sample.relays?.[0] ? 1 : 0,
      sample.relays?.[1] ? 1 : 0,
      sample.relays?.[2] ? 1 : 0,
      sample.relays?.[3] ? 1 : 0
    ])
  ];
  downloadText('hpn-slow-log.csv', rows.map((row) => row.map(csvEscape).join(',')).join('\n'), 'text/csv;charset=utf-8');
}

async function handleViewClick(event) {
  const button = event.target.closest('[data-action]');
  if (!button) return;
  const action = button.dataset.action;

  switch (action) {
    case 'test-activate':
      await activateTest();
      break;
    case 'test-deactivate':
      await deactivateTest();
      break;
    case 'test-toggle':
      await testToggle(button);
      break;
    case 'test-mixer':
      await testMixer(button.dataset.mixer);
      break;
    case 'test-target':
      await testTarget();
      break;
    case 'test-sequence':
      await runTestSequence(button.dataset.sequence);
      break;
    case 'counter-reset':
      await resetCounter(button.dataset.counter);
      break;
    case 'refresh-events':
      await Promise.all([refreshEvents(), refreshAccess()]);
      renderCurrent();
      toast('Logs refreshed.', 'ok', 1800);
      break;
    case 'refresh-slow':
      await refreshSlow();
      renderCurrent();
      toast('Slow log refreshed.', 'ok', 1800);
      break;
    case 'refresh-counters':
      await refreshCounters();
      renderCurrent();
      toast('Counters refreshed.', 'ok', 1800);
      break;
    case 'export-events':
      exportEvents();
      break;
    case 'export-slow':
      exportSlow();
      break;
    case 'mqtt-test': {
      try {
        const result = await apiPost('/api/mqtt/test');
        const detail = result.connected
          ? `Test message queued for ${result.broker || 'the broker'}.`
          : 'Test message queued; the broker is not connected yet.';
        toast(detail, result.connected ? 'ok' : 'warn', 4500);
      } catch (err) {
        toast(err.message, 'err', 6000);
      }
      break;
    }
    case 'reboot':
      await rebootController();
      break;
    case 'factory-reset':
      await factoryReset();
      break;
    case 'ota-upload':
      await uploadFirmware();
      break;
    default:
      break;
  }
}

function handleViewChange(event) {
  const target = event.target;
  if (target.id === 'event-filter') {
    state.eventFilter = target.value;
    renderCurrent();
  } else if (target.id === 'slow-range') {
    state.slowRange = target.value;
    renderCurrent();
  } else if (target.id === 'theme-select') {
    applyTheme(target.value);
    toast(`Theme set to ${target.value}.`, 'info', 1800);
  }
}

/* ------------------------------------------------------------------------- */
/* Routes                                                                     */
/* ------------------------------------------------------------------------- */

const routes = {
  dashboard: {
    title: 'Dashboard',
    subtitle: 'Live overview',
    render: renderDashboard
  },
  plant: {
    title: 'Plant',
    subtitle: 'Circuits, heater and mixer temperatures',
    render: renderPlant
  },
  'log/events': {
    title: 'Event Log',
    subtitle: 'Controller events and portal access records',
    render: renderEventLog,
    enter: async () => { await Promise.all([refreshEvents(), refreshAccess()]); }
  },
  'log/slow': {
    title: 'Slow Log',
    subtitle: '10-second temperature and position samples',
    render: renderSlowLog,
    enter: async () => { await refreshSlow(); }
  },
  'log/sensors': {
    title: 'Sensor Health',
    subtitle: 'Validity, age and error counts',
    render: renderSensors
  },
  'log/counters': {
    title: 'Relay Counters',
    subtitle: 'Cycles and estimated runtime',
    render: renderCounters,
    enter: async () => { await refreshCounters(); }
  },
  test: {
    title: 'Test Mode',
    subtitle: 'Guarded manual output control',
    render: renderTest
  },
  'settings/safety': {
    title: 'Safety & Timeouts',
    subtitle: 'Writable supervision bounds',
    render: renderSettingsSafety,
    enter: async () => { await refreshConfig('safety'); }
  },
  'settings/hardware': {
    title: 'Hardware & Pins',
    subtitle: 'Writable actuator and GPIO configuration',
    render: renderSettingsHardware,
    enter: async () => { await refreshConfig('hardware'); }
  },
  'settings/network': {
    title: 'Network & MQTT',
    subtitle: 'Writable Wi-Fi and broker configuration',
    render: renderSettingsNetwork,
    enter: async () => { await refreshConfig('network'); }
  },
  'settings/firmware': {
    title: 'Firmware Update',
    subtitle: 'OTA application upload',
    render: renderSettingsFirmware,
    enter: async () => { await refreshStatus(); }
  },
  'settings/system': {
    title: 'System',
    subtitle: 'Local mode, presentation and maintenance',
    render: renderSettingsSystem,
    enter: async () => { await refreshConfig('system'); }
  },
  runtime: {
    title: 'Runtime Parameters',
    subtitle: 'Read-only HA and observer values',
    render: renderRuntime,
    enter: async () => { await refreshRuntime(); }
  }
};

/* ------------------------------------------------------------------------- */
/* Background polling and boot                                                */
/* ------------------------------------------------------------------------- */

async function tick() {
  const now = Date.now();
  if (now - state.lastStatusFetch > 30000) {
    await refreshStatus();
  }

  if (state.route === 'dashboard' && now - state.lastEventsFetch > 10000) {
    await refreshEvents();
  }

  if (state.route === 'log/events') {
    const oldEvents = state.eventsSeq;
    const oldAccess = state.accessSeq;
    await Promise.all([refreshEvents(), refreshAccess()]);
    if (state.route === 'log/events' && (state.eventsSeq !== oldEvents || state.accessSeq !== oldAccess)) {
      renderCurrent();
    }
  } else if (state.route === 'log/slow') {
    await refreshSlow();
    if (state.route === 'log/slow') renderCurrent();
  } else if (state.route === 'log/counters') {
    await refreshCounters();
    if (state.route === 'log/counters') renderCurrent();
  } else if (state.route === 'runtime' && now - state.lastRuntimeFetch > 30000) {
    await refreshRuntime();
    if (state.route === 'runtime') renderCurrent();
  }
}

function setupStaticEvents() {
  $('#modal-ok').addEventListener('click', () => closeModal(true));
  $('#modal-cancel').addEventListener('click', () => closeModal(false));
  $('#modal').addEventListener('click', (event) => {
    if (event.target === $('#modal')) closeModal(false);
  });

  $('#menu-toggle').addEventListener('click', () => sidebar.classList.toggle('open'));

  $$('.nav-group-toggle').forEach((button) => {
    button.addEventListener('click', () => {
      const group = button.closest('.nav-group');
      const collapsed = group.classList.toggle('collapsed');
      button.setAttribute('aria-expanded', String(!collapsed));
    });
  });

  $$('#nav a').forEach((link) => {
    link.addEventListener('click', async (event) => {
      if (!state.dirty || link.dataset.route === state.route) {
        closeSidebar();
        return;
      }
      event.preventDefault();
      const leave = await confirmDialog(
        'Discard unsaved changes?',
        'You have unsaved settings. Leave this page and discard them?',
        'Discard',
        true
      );
      if (!leave) return;
      setDirty(false);
      location.hash = link.getAttribute('href');
    });
  });

  view.addEventListener('click', handleViewClick);
  view.addEventListener('change', handleViewChange);
  view.addEventListener('input', () => { state.interactionUntil = Date.now() + 2500; });
  view.addEventListener('click', () => { state.interactionUntil = Date.now() + 1500; });

  window.addEventListener('hashchange', onRoute);
  window.addEventListener('beforeunload', (event) => {
    if (!state.dirty) return;
    event.preventDefault();
    event.returnValue = '';
  });

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      if (!$('#modal').classList.contains('hidden')) closeModal(false);
      closeSidebar();
    }
  });

  if (window.matchMedia) {
    const media = window.matchMedia('(prefers-color-scheme: light)');
    media.addEventListener?.('change', () => {
      if ((localStorage.getItem('hpn-theme') || 'dark') === 'auto') applyTheme('auto');
    });
  }
}

async function boot() {
  applyTheme();
  setupStaticEvents();
  updateConnection();
  await Promise.allSettled([refreshStatus(), refreshEvents(), refreshAccess()]);
  connectWs();
  await onRoute();
  setInterval(tick, 5000);
}

boot();




