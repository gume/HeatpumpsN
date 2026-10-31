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
  slowLoading: false,
  counters: [],
  counterPersistence: null,
  sensorDiscovery: [],
  sensorDiscoveryLoading: false,
  sensorRoles: null,
  sensorRoleLoading: false,
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
  lastCountersFetch: 0,
  lastRuntimeFetch: 0,
  eventsSeq: 0,
  accessSeq: 0,
  otaBusy: false
};

const LIVE_ROUTES = new Set(['dashboard', 'diagnostics', 'test']);
const SETTINGS_ROUTES = new Set(['settings/safety', 'settings/hardware', 'settings/control', 'settings/network', 'settings/firmware', 'settings/security', 'settings/system']);

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

function fmtEpoch(epoch) {
  const n = num(epoch);
  if (n === null || n <= 0) return '—';
  const date = new Date(n * 1000);
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString();
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
  let response;
  try {
    response = await hpnAuth.authFetch(path, options);
  } catch (err) {
    if (err.auth) showLogin();
    throw err;
  }
  if (response.status === 401 || response.status === 423) {
    showLogin(response.status === 423 ? 'Too many failed logins; wait 30 s.' : 'Session expired. Log in again.');
  }
  if (options.unseal && response.ok) return response.json();
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
// Requests that carry secrets (passwords, Wi-Fi/MQTT credentials) are sealed
// with the session's encryption key; the controller rejects them otherwise.
const apiSealed = (method, path, body) => api(path, { method, seal: JSON.stringify(body) });

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

async function connectWs() {
  if (state.ws && (state.ws.readyState === WebSocket.OPEN || state.ws.readyState === WebSocket.CONNECTING)) {
    return;
  }
  if (!hpnAuth.user) return;
  let ws;
  try {
    // The handshake cannot carry headers: the signed credentials go in the URL
    // (single-use nonce, so a captured URL cannot be replayed).
    ws = new WebSocket(await hpnAuth.websocketUrl('/ws'));
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
  state.slowLoading = true;
  try {
    const data = await apiGet('/api/log/slow');
    state.slow = Array.isArray(data.samples) ? data.samples : [];
    state.lastSlowFetch = Date.now();
    return state.slow;
  } catch (err) {
    console.warn('Slow log fetch failed', err);
    return state.slow;
  } finally {
    state.slowLoading = false;
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
    state.lastCountersFetch = Date.now();
    return state.counters;
  } catch (err) {
    toast(`Could not load relay counters: ${err.message}`, 'err');
    return state.counters;
  }
}

async function discoverSensors() {
  if (state.sensorDiscoveryLoading) return;
  state.sensorDiscoveryLoading = true;
  renderCurrent();
  try {
    const data = await apiGet('/api/sensors/discover');
    state.sensorDiscovery = Array.isArray(data.sensors) ? data.sensors : [];
    toast(
      state.sensorDiscovery.length
        ? `${state.sensorDiscovery.length} sensor(s) found on the 1-Wire bus.`
        : 'No temperature sensors found.',
      state.sensorDiscovery.length ? 'ok' : 'warn',
      4000
    );
  } catch (err) {
    toast(`Sensor discovery failed: ${err.message}`, 'err', 6000);
  } finally {
    state.sensorDiscoveryLoading = false;
    renderCurrent();
  }
}

async function verifySensorRoles() {
  if (state.sensorRoleLoading) return;
  state.sensorRoleLoading = true;
  renderCurrent();
  try {
    const data = await apiGet('/api/sensors/roles');
    state.sensorRoles = data;
    toast(data.summary || 'Sensor role check complete.', data.plausible ? 'ok' : 'warn', 4500);
  } catch (err) {
    toast(`Sensor role check failed: ${err.message}`, 'err', 6000);
  } finally {
    state.sensorRoleLoading = false;
    renderCurrent();
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
    : (state.route.startsWith('log/') || state.route === 'diagnostics') ? 'diagnostics' : null;
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

function circuitCard(key, label, circuit, powerW = null) {
  const c = circuit || {};
  const afterRun = num(c.afterRun, 0);
  const on = !!num(c.phys, 0);
  const demand = !!num(c.demand ?? c.cmd, 0);
  const demandAge = num(c.demandAge, 0);
  // The heating switch is the HA-facing demand; the pump can legitimately stay
  // on after the switch turns off because the board owns the after-run.
  const pill = mismatchPill(demand, on);

  const rows = `
    <div class="metric-row"><span class="k">Heating switch</span><span class="v">${demand ? 'ON' : 'OFF'}</span></div>
    <div class="metric-row"><span class="k">Pump</span><span class="v">${on ? 'ON' : 'OFF'}</span></div>
    <div class="metric-row"><span class="k">${afterRun ? 'After-run' : 'Demand age'}</span><span class="v">${afterRun ? fmtAge(afterRun) : (demandAge ? fmtAge(demandAge) : '—')}</span></div>
    ${powerW !== null && powerW !== undefined ? `<div class="metric-row"><span class="k">Power</span><span class="v">${fmtNum(powerW, 1)} W</span></div>` : ''}
  `;

  return `<div class="circuit">
    <div class="circuit-head"><strong>${esc(label)}</strong>${pill}</div>
    <div class="circuit-body">${rows}</div>
  </div>`;
}

function heaterCard(heater) {
  const on = !!num(heater.phys, 0);
  const requested = !!num(heater.requested ?? heater.cmd, 0);
  const rows = `
    <div class="metric-row"><span class="k">Control</span><span class="v">Automatic (board)</span></div>
    <div class="metric-row"><span class="k">Request</span><span class="v">${requested ? 'ON' : 'OFF'}</span></div>
    <div class="metric-row"><span class="k">Relay</span><span class="v">${on ? 'ON' : 'OFF'}</span></div>
    <div class="metric-row"><span class="k">Reason</span><span class="v">${esc(heater.reason || '—')}</span></div>
  `;

  return `<div class="circuit">
    <div class="circuit-head"><strong>Heater</strong>${statePill(on, on ? 'ON' : 'OFF')}</div>
    <div class="circuit-body">${rows}</div>
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
    return emptyCard('No temperature sensor data yet.');
  }
  return `<div class="table-wrap"><table>
    <thead><tr><th>Temperature sensor</th><th>Value</th><th>Age</th><th>State</th><th>Errors</th></tr></thead>
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
  const hasFlag = (name) => flags.includes(name);
  // Physical LED mapping (plans/controller node/02_hardware_and_l0.md §1.2).
  // LED 0 only turns red for critical alarms; ordinary warnings stay yellow
  // via the system/LED 0 path below.
  const critical = hasFlag('overtemp_hard') || hasFlag('sensor_fault_mix') || hasFlag('welded_relay');
  const pumpFault = hasFlag('welded_relay') || hasFlag('pump_dry_run');
  const mqttDown = !!mqtt.enabled && !mqtt.connected;
  const heaterOn = !!num(heater.phys, 0);
  const floorOn = !!num((circuits.floor || {}).phys, 0);
  const rad1On = !!num((circuits.rad1 || {}).phys, 0);
  const rad2On = !!num((circuits.rad2 || {}).phys, 0);
  const divergence = String(system.divergence || 'none');
  const flushActive = String(system.flush || 'idle') === 'active';

  let systemLed;
  if (critical) {
    systemLed = { color: 'red', text: 'Alarm', note: `error ${num(safety.errorCode, 0)}` };
  } else if (!wifi.connected || mqttDown) {
    systemLed = {
      color: 'yellow',
      text: 'Degraded',
      note: !wifi.connected ? 'Wi-Fi down' : 'MQTT down'
    };
  } else {
    systemLed = { color: 'green', text: 'Online', note: 'Wi-Fi + MQTT' };
  }

  let boilerLed;
  if (heaterOn) {
    boilerLed = { color: 'orange', text: 'Heating', note: 'heater ON' };
  } else if (system.dhwActive) {
    boilerLed = { color: 'cyan', text: 'DHW', note: 'hot-water priority' };
  } else if (divergence === 'anti_short_cycle') {
    boilerLed = { color: 'purple', text: 'Hold-off', note: 'anti-short-cycle' };
  } else {
    boilerLed = { color: 'off', text: 'Standby', note: 'heater OFF' };
  }

  let floorLed;
  if (hasFlag('overtemp_hard')) {
    floorLed = { color: 'red', text: 'Over-temperature', note: 'hard cutoff' };
  } else if (flushActive) {
    floorLed = { color: 'cyan', text: 'Flushing', note: 'start-up flush' };
  } else if (divergence === 'floor_delta_t_collapsed') {
    floorLed = { color: 'magenta', text: 'ΔT pause', note: 'floor divergence' };
  } else if (floorOn) {
    floorLed = { color: 'green', text: 'Running', note: 'pump ON' };
  } else {
    floorLed = { color: 'off', text: 'Idle', note: 'pump OFF' };
  }

  const pumpLed = (on) => pumpFault
    ? { color: 'red', text: 'Fault', note: 'pump fault' }
    : on
      ? { color: 'green', text: 'Running', note: 'pump ON' }
      : { color: 'off', text: 'Idle', note: 'pump OFF' };

  // Roles in plan order; the board decides where each LED sits.
  const roles = [
    { name: 'System', ...systemLed },
    { name: 'Heater', ...boilerLed },
    { name: 'Floor', ...floorLed },
    { name: 'Loop 1', ...pumpLed(rad1On) },
    { name: 'Loop 2', ...pumpLed(rad2On) }
  ];
  const positions = Array.isArray(live.ledPositions) && live.ledPositions.length === 5 ? live.ledPositions : [0, 1, 2, 3, 4];
  return roles
    .map((role, i) => ({ ...role, position: positions[i], label: `LED ${positions[i]} · ${role.name}` }))
    .sort((a, b) => a.position - b.position);
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
  const floor = circuits.floor || {};
  const mixerPos = Math.max(0, Math.min(100, num(floor.mixerPos, 0)));
  const mixerTarget = Math.max(0, Math.min(100, num(floor.mixerTarget, 0)));
  const mixerMax = Math.max(1, Math.min(100, num(floor.mixerMaxPct, 100)));
  const encoderPos = Math.max(0, Math.min(100, num(floor.encoderPos, 0)));
  const mixerCmdLabel = floor.mixerEmergency
    ? 'emergency close'
    : (!floor.mixerMotion ? 'idle' : (num(floor.mixerDir, 0) > 0 ? 'opening' : 'closing'));
  const recent = state.events.slice(-6).reverse();
  const mpc = live.mpc || {};
  const mpcLabels = { mpc: 'Predictive', legacy: 'Legacy PI', homing: 'Fallback: homing', cautious: 'Fallback: cautious' };
  const mpcTone = mpc.mode === 'mpc' ? 'chip-ok' : (mpc.mode === 'legacy' ? '' : 'chip-err');
  const mpcRows = mpc.mode ? `
          <div class="metric-row"><span class="k">Controller</span><span class="v">${esc(mpcLabels[mpc.mode] || mpc.mode)}${mpc.homing ? ' (stroke running)' : ''}</span></div>
          ${mpc.mode === 'legacy' ? '' : `
          <div class="metric-row"><span class="k">Parameter set</span><span class="v">${esc(mpc.scenario || '—')}</span></div>
          <div class="metric-row"><span class="k">Model bias</span><span class="v">${fmtNum(mpc.bias, 2)} K</span></div>
          ${mpc.boost ? '<div class="metric-row"><span class="k">Heat-up boost</span><span class="v">active</span></div>' : ''}
          <div class="metric-row"><span class="k">Last anomaly</span><span class="v">${esc(mpc.anomaly || 'none')} (${num(mpc.anomalies, 0)} total, ${num(mpc.retries, 0)} retries)</span></div>
          ${mpc.mode === 'cautious' ? `<div class="metric-row"><span class="k">Stable for</span><span class="v">${Math.round(num(mpc.stableS, 0) / 60)} / 30 min</span></div>` : ''}`}` : '';

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
          <div><h2>Heating loops</h2><p>Switch demand, physical pump and board-owned after-run</p></div>
          <span class="chip">${esc(modeLabel(mode.current))}</span>
        </div>
        <div class="circuit-grid">
          ${circuitCard('rad1', 'Heating loop 1', circuits.rad1, power.rad1)}
          ${circuitCard('rad2', 'Heating loop 2', circuits.rad2, power.rad2)}
          ${circuitCard('floor', 'Floor heating', circuits.floor, power.floor)}
          ${heaterCard(heater)}
        </div>
      </section>

      <section class="card readonly accent-top">
        <div class="card-head">
          <div><h2>Mixer</h2><p>Hydraulic temperatures, power and position</p></div>
          <div>
            ${mpc.mode ? `<span class="chip ${mpcTone}">${esc(mpcLabels[mpc.mode] || mpc.mode)}</span>` : ''}
            <span class="chip">${esc(floor.encoderValid ? 'encoder valid' : 'encoder invalid')}</span>
          </div>
        </div>
        <div class="temp-grid mixer-temp-grid">
          ${tempTile('Mixer hot in', temps.mixerHotIn ?? temps.boilerSupply, tempHealth.mixerHotIn ?? tempHealth.boilerSupply)}
          ${tempTile('Mixer cold in / return', temps.mixerColdIn ?? temps.return, tempHealth.mixerColdIn ?? tempHealth.return)}
          ${tempTile('Mixer forward', temps.mixerForward, tempHealth.mixerForward)}
        </div>
        <div class="mixer-metrics">
          <div class="metric-row"><span class="k">Mixer power</span><span class="v">${fmtNum(power.mixer, 1)} W</span></div>
          <div class="metric-row"><span class="k">Estimated position</span><span class="v">${mixerPos.toFixed(1)}%</span></div>
          <div class="metric-row"><span class="k">Target position</span><span class="v">${mixerTarget.toFixed(1)}%</span></div>
          <div class="metric-row"><span class="k">Minimum opening</span><span class="v">${num(floor.mixerMinPct, 0)}%</span></div>
          <div class="metric-row"><span class="k">Maximum opening</span><span class="v">${mixerMax}%${floor.mixerMaxDynamic ? ` (dynamic, absolute ${num(floor.mixerMaxAbsPct, 100)}%)` : ''}</span></div>
          ${floor.mixerOvershoot ? '<div class="metric-row"><span class="k">Overshoot reaction</span><span class="v">active</span></div>' : ''}
          <div class="metric-row"><span class="k">Command</span><span class="v">${mixerCmdLabel}</span></div>
          ${mpcRows}
          <div class="bar"><span style="width:${mixerPos.toFixed(1)}%"></span></div>
          <div class="metric-row"><span class="k">Encoder position</span><span class="v">${encoderPos.toFixed(1)}% ${floor.encoderValid ? '' : '⚠ invalid'}</span></div>
        </div>
      </section>
    </div>

    <div class="grid cols-2">
      <section class="card readonly">
        <div class="card-head">
          <div><h2>Safety</h2><p>Active flags and controller error code</p></div>
          <span class="chip ${(safety.flags || []).length ? 'chip-warn' : 'chip-ok'}">${(safety.flags || []).length} active</span>
        </div>
        ${safetyFlags(safety.flags)}
        <div class="metric-row" style="margin-top:10px"><span class="k">Error code</span><span class="v">${esc(safety.errorCode ?? 0)}</span></div>
        <div class="metric-row"><span class="k">Error text</span><span class="v">${esc(safety.errorText || 'No error')}</span></div>
        <div class="metric-row"><span class="k">Divergence</span><span class="v">${esc(system.divergence || 'none')}</span></div>
        <div class="metric-row"><span class="k">Flush</span><span class="v">${esc(system.flush || 'idle')}</span></div>
      </section>

      <section class="card readonly">
        <div class="card-head">
          <div><h2>Recent events</h2><p>Latest entries from the in-RAM event log</p></div>
          <a class="btn btn-sm" href="#log/events">Open log</a>
        </div>
        ${recent.length ? recent.map(eventRowCompact).join('') : emptyCard('No events yet.')}
      </section>
    </div>

    <section class="card readonly">
      <div class="card-head">
        <div><h2>Status LEDs</h2><p>Board LED semantics from plan 02 §1.2</p></div>
        <span class="chip">5 LEDs</span>
      </div>
      <div class="status-leds">
        ${dashboardLeds(live).map((led) => `<div class="status-led-row" title="${esc(led.note)}">
          <span class="status-led ${esc(led.color)}"></span>
          <div class="status-led-label"><strong>${esc(led.label)}</strong><small>${esc(led.text)} · ${esc(led.note)}</small></div>
        </div>`).join('')}
      </div>
    </section>
  </div>`;
}

/* ------------------------------------------------------------------------- */
/* Temperature sensor health                                                 */
/* ------------------------------------------------------------------------- */

function renderSensors() {
  const live = state.live;
  if (!live) return loadingCard('Waiting for live state…');

  const sensors = live.sensors || [];
  const bad = sensors.filter((s) => s.state !== 'ok').length;

  return `<div class="stack">
    <div class="banner ${bad ? 'warn' : 'ok'}">
      ${bad ? `${bad} temperature sensor${bad === 1 ? ' is' : 's are'} stale, disconnected or in error.` : 'All configured temperature sensors are valid.'}
    </div>
    <section class="card readonly accent-top">
      <div class="card-head">
        <div><h2>Temperature sensor health</h2><p>Last value, age and consecutive error count per 1-Wire sensor</p></div>
        <span class="chip ${bad ? 'chip-warn' : 'chip-ok'}">${sensors.length - bad}/${sensors.length} healthy</span>
      </div>
      ${sensorTable(sensors)}
      <p class="small faint" style="margin-top:10px">Offsets are not yet configurable from the portal. The displayed values are raw board readings.</p>
    </section>
  </div>`;
}

/* ------------------------------------------------------------------------- */
/* Diagnostics: link state, sensor health and relay counters                   */
/* ------------------------------------------------------------------------- */

function linkChip(text, ok, warn = false) {
  const cls = ok ? 'chip-ok' : (text === 'disabled' ? '' : (warn ? 'chip-warn' : 'chip-err'));
  return `<span class="chip ${cls}">${esc(text)}</span>`;
}

function renderConnectivity() {
  const status = state.status;
  if (!status) return loadingCard('Waiting for controller status…');

  const wifi = status.wifi || {};
  const mqtt = status.mqtt || {};
  const ntp = status.ntp || {};

  const wifiState = wifi.connected ? 'connected' : (wifi.configured ? (wifi.state || 'disconnected') : 'disabled');
  const mqttState = mqtt.enabled ? (mqtt.state || (mqtt.connected ? 'connected' : 'disconnected')) : 'disabled';
  const ntpState = ntp.synced ? 'synchronized' : (ntp.enabled ? 'waiting for sync' : 'disabled');

  const wifiDetail = wifi.connected
    ? `${esc(wifi.ip || '—')} · ${fmtNum(wifi.rssi, 0)} dBm`
    : (wifi.configured ? 'station not associated' : 'no SSID configured');
  const mqttDetail = mqtt.enabled
    ? `${esc(mqtt.broker || '—')}:${esc(mqtt.port ?? '—')}`
    : 'broker not configured';
  const mqttCounters = `${num(mqtt.connectCount, 0)} connect(s) · ${num(mqtt.disconnectCount, 0)} disconnect(s) · ${num(mqtt.errors, 0)} error(s)`;
  const mqttError = mqtt.lastError ? ` · last error ${esc(mqtt.lastError)}` : '';
  const ntpDetail = ntp.enabled
    ? `${esc(ntp.server || '—')}${ntp.synced ? ` · ${esc(fmtEpoch(ntp.epoch))}` : ''}`
    : 'time is not synchronised';

  return `<section class="card readonly accent-top">
    <div class="card-head">
      <div><h2>Connectivity</h2><p>Wi-Fi, MQTT broker and time source</p></div>
    </div>
    <div class="kv">
      <dt>Wi-Fi</dt>
      <dd>${linkChip(wifiState, !!wifi.connected)} ${wifiDetail}</dd>
      <dt>MQTT</dt>
      <dd>${linkChip(mqttState, !!mqtt.connected)} ${mqttDetail}</dd>
      <dt>MQTT counters</dt>
      <dd class="${mqtt.connected ? '' : 'muted'}">${mqttCounters}${mqttError}</dd>
      <dt>NTP</dt>
      <dd>${linkChip(ntpState, !!ntp.synced, true)} ${ntpDetail}</dd>
      <dt>Time zone</dt>
      <dd>${esc(ntp.timezone || '—')}</dd>
    </div>
    <p class="small faint" style="margin-top:10px">The badge in the header shows the live Wi-Fi and MQTT state; this card refreshes with the 30-second status poll.</p>
  </section>`;
}

function renderControllerInfo() {
  const status = state.status;
  if (!status) return loadingCard('Waiting for controller status…');
  return `<section class="card readonly">
    <div class="card-head">
      <div><h2>Controller</h2><p>Runtime information and the last reset cause</p></div>
      <span class="chip">READ ONLY</span>
    </div>
    <div class="kv">
      <dt>Uptime</dt>
      <dd>${fmtUptime(status.uptime || 0)}</dd>
      <dt>Last reset</dt>
      <dd>${esc(status.resetReason || '—')}</dd>
      <dt>Free heap</dt>
      <dd>${fmtBytes(status.freeHeap)}${status.minFreeHeap !== undefined ? ` · min ${fmtBytes(status.minFreeHeap)}` : ''}</dd>
      <dt>CPU load</dt>
      <dd>${status.cpuLoad !== undefined ? `${num(status.cpuLoad, 0).toFixed(2)} %` : 'n/a'}${status.taskCount !== undefined ? ` · ${status.taskCount} tasks` : ''}</dd>
      <dt>Firmware</dt>
      <dd>${esc(status.version || '?')} · ${esc(status.build || '?')}</dd>
    </div>
  </section>`;
}

function renderPowerMeters() {
  const live = state.live;
  if (!live) return loadingCard('Waiting for live state…');

  const power = live.power || {};
  // `enabled` is one flag per CurrentChannel (floor, rad1, rad2, mixer). A
  // controller that predates the field is assumed to have all meters fitted.
  const enabled = Array.isArray(power.enabled) ? power.enabled : [true, true, true, true];
  const sense = power.senseAvailable !== false;
  const rows = [
    ['Floor pump', power.floor, enabled[0] !== false],
    ['Radiator 1', power.rad1, enabled[1] !== false],
    ['Radiator 2', power.rad2, enabled[2] !== false],
    ['Mixer', power.mixer, enabled[3] !== false]
  ];
  const fitted = rows.filter(([, , on]) => on).length;

  const detail = (watts, on) => {
    if (!on) return '<span class="faint">meter not fitted</span>';
    if (!sense) return `<span class="faint">${fmtNum(watts, 1)} W · front end unavailable</span>`;
    return `${fmtNum(watts, 1)} W`;
  };

  return `<section class="card readonly accent-top">
    <div class="card-head">
      <div><h2>Power meters</h2><p>Four optional current channels, checked individually</p></div>
      <span class="chip ${sense && fitted ? 'chip-ok' : 'chip-warn'}">${fitted}/4 fitted</span>
    </div>
    <div class="kv">
      ${rows.map(([label, watts, on]) => `<dt>${esc(label)}</dt><dd>${detail(watts, on)}</dd>`).join('')}
      <dt>Front end</dt>
      <dd>${sense ? 'measuring' : '<span class="faint">no usable current signal (protection skipped)</span>'}</dd>
    </div>
    <p class="small faint" style="margin-top:10px">A meter that is not fitted is never sampled and always reports 0 W. Dry-run and welded-relay detection only consider fitted channels.</p>
  </section>`;
}

function renderDiagnostics() {
  return `<div class="stack">
    ${renderConnectivity()}
    ${renderControllerInfo()}
    ${renderSensors()}
    ${renderPowerMeters()}
    ${renderCounters()}
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
      <span class="boiler"><i></i>Mixer hot in</span>
      <span class="mix"><i></i>Mixer forward</span>
      <span class="ret"><i></i>Mixer cold in / return</span>
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
        <span class="chip ${state.slowLoading ? 'chip-warn' : ''}">${state.slowLoading ? 'loading…' : `${state.slow.length} samples`}</span>
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
        ${metricCard('Mixer hot in', fmtTemp(latest?.boiler), 'latest sample')}
        ${metricCard('Mixer forward', fmtTemp(latest?.mix), 'latest sample')}
        ${metricCard('Mixer cold in / return', fmtTemp(latest?.ret), 'latest sample')}
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
        <div><h2>Relays</h2><p>Cycle counts and estimated runtime for each relay</p></div>
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
  const mixerCmdLabel = floor.mixerEmergency
    ? 'emergency close'
    : (!floor.mixerMotion ? 'idle' : (num(floor.mixerDir, 0) > 0 ? 'opening' : 'closing'));
  const mixerMax = Math.max(1, Math.min(100, num(floor.mixerMaxPct, 100)));

  if (!test.active) {
    return `<div class="stack">
      <section class="card guarded accent-top">
        <div class="card-head">
          <div><h2>Test Mode is off</h2><p>Manual output control is guarded and time-limited.</p></div>
          <span class="chip chip-err">GUARDED</span>
        </div>
        <div class="banner warn">
          <strong>Before activating:</strong> Test Mode writes manual commands through the same intake path as HA.
          It is intended for bench verification only. L1 safety still retains final authority over every output,
          including the heater.
        </div>
        <ul class="small muted" style="margin:12px 0 16px 18px; padding:0">
          <li>In normal operation the board owns the heater and follows the three heating switches.</li>
          <li>Test Mode is the only place where the heater can be commanded manually.</li>
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
        ${testOutputRow('heater', 'Heater (manual)', heater.cmd, heater.phys, 'Board-managed outside Test Mode; requires at least one pump ON here.')}
        ${testOutputRow('pump_floor', 'Floor pump', floor.cmd, floor.phys, 'Floor heating circuit pump')}
        ${testOutputRow('pump_rad1', 'Heating loop 1 pump', circuits.rad1?.cmd, circuits.rad1?.phys, 'Heating loop 1')}
        ${testOutputRow('pump_rad2', 'Heating loop 2 pump', circuits.rad2?.cmd, circuits.rad2?.phys, 'Heating loop 2')}
        <div class="banner info" style="margin-top:12px">Divergence: <span class="mono">${esc(system.divergence || 'none')}</span></div>
      </section>

      <section class="card guarded">
        <div class="card-head">
          <div><h2>Mixer</h2><p>Nudge the motor ±2% or store a target position</p></div>
          <span class="chip">${fmtNum(floor.mixerPos, 1)}%</span>
        </div>
        <div class="metric-row"><span class="k">Estimated position</span><span class="v">${fmtNum(floor.mixerPos, 1)}%</span></div>
        <div class="metric-row"><span class="k">Target position</span><span class="v">${fmtNum(floor.mixerTarget, 1)}%</span></div>
        <div class="metric-row"><span class="k">Minimum opening</span><span class="v">${fmtNum(floor.mixerMinPct, 0)}%</span></div>
        <div class="metric-row"><span class="k">Maximum opening</span><span class="v">${mixerMax}%${floor.mixerMaxDynamic ? ` (dynamic)` : ''}</span></div>
        ${floor.mixerOvershoot ? '<div class="metric-row"><span class="k">Overshoot reaction</span><span class="v">active</span></div>' : ''}
        <div class="metric-row"><span class="k">Command</span><span class="v">${mixerCmdLabel}</span></div>
        <div class="metric-row"><span class="k">Encoder</span><span class="v">${fmtNum(floor.encoderPos, 1)}% ${floor.encoderValid ? '' : '⚠'}</span></div>

        <div class="row" style="margin-top:12px">
          <button class="btn btn-sm" type="button" data-action="test-mixer" data-mixer="open">Open +2%</button>
          <button class="btn btn-sm" type="button" data-action="test-mixer" data-mixer="stop">Stop</button>
          <button class="btn btn-sm" type="button" data-action="test-mixer" data-mixer="close">Close −2%</button>
        </div>

        <div class="field" style="margin-top:12px">
          <label for="mixer-target">Target position</label>
          <div class="range-row">
            <input id="mixer-target" type="range" min="0" max="${mixerMax}" step="1" value="${Math.min(Math.round(num(floor.mixerTarget, 0)), mixerMax)}" aria-label="Mixer target position">
            <span class="unit">%</span>
            <button class="btn btn-sm" type="button" data-action="test-target">Store</button>
          </div>
          <small>Stores the target. The servo drives continuously to it and stops on arrival; changing the target mid-move reverses immediately. L1 never opens past ${mixerMax}%.</small>
        </div>
      </section>
    </div>

    <section class="card guarded">
      <div class="card-head">
        <div><h2>Test sequences</h2><p>Scripted bench checks</p></div>
        <span class="chip chip-warn">${esc(test.sequence || 'none')}</span>
      </div>
      <div class="sequence-grid">
        <div class="sequence-card">
          <h4>Anti-seize</h4>
          <p>Runs the floor, loop 1 and loop 2 pumps for 30 seconds each, then stops them.</p>
          <button class="btn btn-sm btn-primary" type="button" data-action="test-sequence" data-sequence="anti-seize">Run sequence</button>
        </div>
        <div class="sequence-card">
          <h4>Mixer sweep</h4>
          <p>Drives fully open, then fully closed, using the dead-reckoned position.</p>
          <button class="btn btn-sm" type="button" data-action="test-sequence" data-sequence="mixer-sweep">Run sequence</button>
        </div>
        <div class="sequence-card">
          <h4>Endstop home</h4>
          <p>Drives fully closed and re-zeroes the dead-reckoned position.</p>
          <button class="btn btn-sm" type="button" data-action="test-sequence" data-sequence="endstop-home">Run sequence</button>
        </div>
        <div class="sequence-card">
          <h4>Status LEDs</h4>
          <p>All LEDs step together through red, green, blue, white, yellow, cyan, magenta, orange and purple, 1 s each, until stopped or Test Mode ends. The status display is hidden meanwhile.</p>
          <button class="btn btn-sm ${test.leds ? 'btn-danger' : ''}" type="button" data-action="test-leds" data-on="${test.leds ? 'false' : 'true'}">${test.leds ? 'Stop LED test' : 'Start LED test'}</button>
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

function fieldNumber(name, label, value, min, max, step, help, dataType = 'int', unit = '') {
  const v = num(value, min);
  return `<div class="field">
    <label for="f_${esc(name)}">${esc(label)}</label>
    <div class="number-row">
      <input id="f_${esc(name)}" data-field="${esc(name)}" data-type="${esc(dataType)}" type="number"
             min="${min}" max="${max}" step="${step}" value="${v}">
      <span class="unit">${esc(unit || '')}</span>
    </div>
    <small>${esc(help || '')}</small>
  </div>`;
}

// A number field with its polarity tick on the same row. Used for the output
// GPIOs and the expander bits so "active low" sits next to the pin it applies
// to instead of in a separate block.
function fieldNumberPolarity(name, polarityName, label, value, activeLow, help, min = 0, max = 255) {
  return `<div class="field">
    <label for="f_${esc(name)}">${esc(label)}</label>
    <div class="pin-row">
      <input id="f_${esc(name)}" data-field="${esc(name)}" data-type="int" type="number"
             min="${min}" max="${max}" step="1" value="${num(value, max)}">
      <label class="checkbox-row">
        <input type="checkbox" data-field="${esc(polarityName)}" ${activeLow ? 'checked' : ''}>
        <span>active low</span>
      </label>
    </div>
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

function fieldHex(name, label, value, help) {
  const byte = Math.max(0, Math.min(255, Number(value) || 0));
  return `<div class="field">
    <label for="f_${esc(name)}">${esc(label)}</label>
    <input id="f_${esc(name)}" data-field="${esc(name)}" data-type="hex" type="text"
           value="0x${byte.toString(16).toUpperCase().padStart(2, '0')}"
           autocomplete="off" spellcheck="false">
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

function fieldSelect(name, label, value, options, help, dataType = 'int') {
  return `<div class="field">
    <label for="f_${esc(name)}">${esc(label)}</label>
    <select id="f_${esc(name)}" data-field="${esc(name)}" data-type="${esc(dataType)}">
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

function sensorIdOptions(current) {
  const ids = [...new Set([
    current,
    ...(state.sensorDiscovery || []).map((sensor) => sensor.id)
  ].filter(Boolean))];
  return [{ value: '', label: '— unassigned —' }, ...ids.map((id) => ({ value: id, label: id }))];
}

function sensorDiscoveryTable() {
  const sensors = state.sensorDiscovery || [];
  if (state.sensorDiscoveryLoading) {
    return `<div class="empty">Scanning the 1-Wire bus…</div>`;
  }
  if (!sensors.length) {
    return `<div class="empty">No discovery result yet. Press “Discover sensors” to scan the bus.</div>`;
  }
  return `<div class="table-wrap"><table>
    <thead><tr><th>Temperature sensor ID</th><th>Temperature</th><th>State</th></tr></thead>
    <tbody>${sensors.map((sensor) => `<tr>
      <td class="mono">${esc(sensor.id)}</td>
      <td class="mono">${fmtTemp(sensor.temperature)}</td>
      <td><span class="chip ${sensor.valid ? 'chip-ok' : 'chip-warn'}">${sensor.valid ? 'valid' : 'no reading'}</span></td>
    </tr>`).join('')}</tbody>
  </table></div>`;
}

function sensorRoleTable() {
  const roles = state.sensorRoles;
  if (!roles) return '';
  const checks = roles.checks || {};
  const mark = (ok) => ok ? '<span class="chip chip-ok">ok</span>' : '<span class="chip chip-warn">inverted</span>';
  const temps = roles.temperatures || {};
  return `<div class="table-wrap" style="margin-top:10px"><table>
    <thead><tr><th>Check</th><th>Result</th></tr></thead>
    <tbody>
      <tr><td>Heating active</td><td><span class="chip ${roles.heating_active ? 'chip-ok' : ''}">${roles.heating_active ? 'yes' : 'no'}</span></td></tr>
      <tr><td>All readings valid</td><td><span class="chip ${roles.all_valid ? 'chip-ok' : 'chip-warn'}">${roles.all_valid ? 'yes' : 'no'}</span></td></tr>
      <tr><td>Boiler (${fmtTemp(temps.boiler)}) ≥ forward (${fmtTemp(temps.mix)})</td><td>${mark(checks.boiler_ge_mix)}</td></tr>
      <tr><td>Forward (${fmtTemp(temps.mix)}) ≥ return (${fmtTemp(temps.return)})</td><td>${mark(checks.mix_ge_return)}</td></tr>
      <tr><td>Boiler (${fmtTemp(temps.boiler)}) ≥ return (${fmtTemp(temps.return)})</td><td>${mark(checks.boiler_ge_return)}</td></tr>
    </tbody>
  </table></div>
  <p class="small faint" style="margin-top:6px">${esc(roles.summary || '')}. Tolerance ${fmtNum(roles.tolerance_c, 1)} K.</p>`;
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
            ${fieldRange('mix_max_open_pct', 'Maximum mixer opening', cfg.mix_max_open_pct, 10, 100, 1, '%', 'L1 never drives the mixer above this limit. Lower it if a sudden temperature rise needs a faster close.')}
            ${fieldNumber('boiler_min_off_min', 'Boiler minimum off time', cfg.boiler_min_off_ms / 60000, 0, 60, 0.5, 'Minimum boiler off time before it may be re-energized.', 'number', 'min')}
            ${fieldNumber('demand_timeout_s', 'Demand timeout', cfg.demand_timeout_s, 10, 3600, 1, 'Time without a repeated ON command on a heating switch before that circuit demand is dropped.', 'int', 's')}
            ${fieldNumber('pump_stop_after_heating_s', 'Pump after-run', cfg.pump_stop_after_heating_s, 0, 3600, 1, 'Board-managed pump run-on after that circuit demand ends.', 'int', 's')}
            ${fieldNumber('long_pause_min', 'Long-pause threshold', cfg.long_pause_s / 60, 0, 1440, 1, 'Time before a long-pause divergence is reported.', 'number', 'min')}
          </div>
        </div>

        <div class="form-section">
          <h3>Mixer operating window</h3>
          <p class="small faint">The minimum applies only to normal floor-demand control. Safety closes, end-stop homing, DHW and no-demand closes can always reach 0 %.</p>
          <div class="form-grid">
            ${fieldRange('mix_min_open_pct', 'Minimum mixer opening', cfg.mix_min_open_pct, 0, 100, 1, '%', 'Normal floor-heating minimum. Set to 0 to allow full closing during control.')}
            ${fieldCheckbox('mix_max_dynamic', 'Dynamic maximum', cfg.mix_max_dynamic, 'Select a lower maximum above the boiler-temperature switch point, with hysteresis.')}
            ${fieldRange('mix_max_below_pct', 'Maximum below switch', cfg.mix_max_below_pct, 10, 100, 1, '%', 'Ceiling when the boiler-side temperature is below the switch point.')}
            ${fieldRange('mix_max_above_pct', 'Maximum above switch', cfg.mix_max_above_pct, 10, 100, 1, '%', 'Ceiling when the boiler-side temperature is above the switch point.')}
            ${fieldRange('mix_max_switch_c', 'Dynamic switch temperature', cfg.mix_max_switch_c, 0, 90, 0.5, '°C', 'Boiler-side temperature that selects the above/below maximum.')}
            ${fieldRange('mix_max_hysteresis_c', 'Dynamic hysteresis', cfg.mix_max_hysteresis_c, 0.5, 10, 0.1, 'K', 'Hysteresis around the switch point to prevent chattering.')}
          </div>
        </div>

        <div class="form-section">
          <h3>Forward-temperature overshoot</h3>
          <p class="small faint">Soft reaction around setpoint. This is a warning, not the L1 hard over-temperature rule.</p>
          <div class="form-grid">
            ${fieldRange('mix_overshoot_delta_c', 'Overshoot delta', cfg.mix_overshoot_delta_c, 0.5, 10, 0.1, 'K', 'React when forward temperature reaches setpoint + delta.')}
            ${fieldRange('mix_overshoot_recovery_c', 'Overshoot recovery', cfg.mix_overshoot_recovery_c, 0, 10, 0.1, 'K', 'Release the reaction below setpoint − recovery.')}
            ${fieldRange('mix_overshoot_target_pct', 'Overshoot target opening', cfg.mix_overshoot_target_pct, 0, 100, 1, '%', 'Opening held while the overshoot reaction is latched. It bypasses the normal minimum.')}
          </div>
        </div>

        <div class="form-section">
          <h3>Sensor role plausibility</h3>
          <p class="small faint">Measured only while the heater and floor pump are running: boiler ≥ forward ≥ return within the tolerance. Warning-only.</p>
          <div class="form-grid">
            ${fieldCheckbox('sensor_role_check_enabled', 'Enable role check', cfg.sensor_role_check_enabled, 'Raise a warning when the three sensor roles look inverted during heating.')}
            ${fieldRange('sensor_role_tolerance_c', 'Role tolerance', cfg.sensor_role_tolerance_c, 0.5, 20, 0.5, 'K', 'Allowed inversion before the order is considered suspect.')}
          </div>
        </div>

        <div class="form-section">
          <h3>Inferred DHW (optional)</h3>
          <p class="small faint">Use only when no DHW meter is configured. A boiler-temperature drop while the heater is on is treated as DHW charging. The meter signal remains authoritative when present.</p>
          <div class="form-grid">
            ${fieldCheckbox('dhw_infer_enabled', 'Infer DHW from boiler temperature', cfg.dhw_infer_enabled, 'Treat a configured boiler-temperature drop as DHW priority.')}
            ${fieldRange('dhw_infer_drop_c', 'DHW detection drop', cfg.dhw_infer_drop_c, 1, 20, 0.5, 'K', 'Drop from the recent boiler peak that starts DHW inference.')}
            ${fieldNumber('dhw_infer_window_s', 'DHW peak window', cfg.dhw_infer_window_s, 10, 3600, 10, 'How long a boiler peak remains a valid baseline.', 'int', 's')}
            ${fieldRange('dhw_infer_recovery_c', 'DHW recovery band', cfg.dhw_infer_recovery_c, 0, 20, 0.5, 'K', 'Recovery below the pre-drop peak ends DHW inference.')}
            ${fieldNumber('dhw_infer_max_s', 'Maximum DHW charge', cfg.dhw_infer_max_s, 60, 86400, 60, 'An inferred charge longer than this raises a warning and releases the floor.', 'int', 's')}
          </div>
        </div>

        <div class="form-section">
          <h3>Maintenance and Test Mode</h3>
          <div class="form-grid">
            ${fieldNumber('anti_seize_interval_days', 'Anti-seize interval', cfg.anti_seize_interval_s / 86400, 1, 90, 1, 'Time between anti-seize runs.', 'number', 'days')}
            ${fieldNumber('test_mode_max_min', 'Test Mode maximum', cfg.test_mode_max_s / 60, 1, 120, 1, 'Time before Test Mode expires automatically.', 'number', 'min')}
            ${fieldNumber('sensor_error_threshold', 'Sensor error threshold', cfg.sensor_error_threshold, 1, 10, 1, 'Consecutive failed reads before a sensor is marked in error.', 'int', 'reads')}
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
  const bitHelp = 'Output bit 0–7. Every assigned bit must be unique.';
  const muxHelp = 'Expander bit 0–7, or 255 when the line is not connected.';
  const pumpPower = Array.isArray(cfg.has_pump_power) ? cfg.has_pump_power : [false, false, false];

  return `<div class="stack">
    <div class="banner warn">Writable configuration. GPIO, driver and wiring changes require a reboot to take effect. Timing and control constants live under <a href="#settings/control">Control &amp; Timing</a>.</div>
    <form class="settings-form writable" data-section="hardware">
      <section class="card writable accent-top">
        <div class="card-head"><div><h2>Board wiring</h2><p>How the two mixer channels are wired</p></div><span class="chip chip-warn">REBOOT REQUIRED</span></div>

        <div class="form-section">
          <h3>Mixer</h3>
          <div class="form-grid">
            ${fieldSelect('mixer_wiring', 'Mixer wiring', cfg.mixer_wiring, [
              { value: 0, label: 'Direction + motion (HomeNet Heating control v0.1)' },
              { value: 1, label: 'Open + close (HomeNet Heating control v1.x)' }
            ], 'The first mixer channel is open/direction; the second is close/motion.')}
          </div>
          <p class="small faint">HomeNet Heating control v0.1 uses a direction bit plus a motion enable. HomeNet Heating control v1.x uses separate, interlocked open and close relays. Both map to the same two logical channels.</p>
        </div>
      </section>

      <section class="card writable accent-top">
        <div class="card-head"><div><h2>Output driver</h2><p>How the relay and mixer channels are reached</p></div><span class="chip chip-warn">REBOOT REQUIRED</span></div>

        <div class="form-section">
          <div class="form-grid">
            ${fieldSelect('output_expander', 'Output driver', cfg.output_expander, [
              { value: 0, label: 'Direct GPIO' },
              { value: 1, label: '74HC595 (HomeNet Heating control v0.1)' },
              { value: 2, label: 'PCF8574 (HomeNet Heating control v1.x)' }
            ], 'Only the sections that belong to the selected driver are shown. Values for the other drivers are kept.')}
          </div>
        </div>

        <div class="form-section" data-driver-section="0">
          <h3>Relay and mixer GPIOs</h3>
          <div class="form-grid">
            ${fieldNumberPolarity('gpio_heater', 'output_active_low_heater', 'Heater GPIO', cfg.gpio_heater, cfg.output_active_low_heater, pinHelp)}
            ${fieldNumberPolarity('gpio_pump_rad1', 'output_active_low_pump_rad1', 'Heating loop 1 pump GPIO', cfg.gpio_pump_rad1, cfg.output_active_low_pump_rad1, pinHelp)}
            ${fieldNumberPolarity('gpio_pump_rad2', 'output_active_low_pump_rad2', 'Heating loop 2 pump GPIO', cfg.gpio_pump_rad2, cfg.output_active_low_pump_rad2, pinHelp)}
            ${fieldNumberPolarity('gpio_pump_floor', 'output_active_low_pump_floor', 'Floor pump GPIO', cfg.gpio_pump_floor, cfg.output_active_low_pump_floor, pinHelp)}
            ${fieldNumberPolarity('gpio_mix_open', 'output_active_low_mix_direction', 'Mixer channel 1 (open / direction) GPIO', cfg.gpio_mix_open, cfg.output_active_low_mix_direction, pinHelp)}
            ${fieldNumberPolarity('gpio_mix_close', 'output_active_low_mix_motion', 'Mixer channel 2 (close / motion) GPIO', cfg.gpio_mix_close, cfg.output_active_low_mix_motion, pinHelp)}
          </div>
        </div>

        <div class="form-section" data-driver-section="1">
          <h3>74HC595 wiring</h3>
          <div class="form-grid">
            ${fieldNumber('expander_data_pin', 'Serial data (SER/DS) GPIO', cfg.expander_data_pin, 0, 255, 1, pinHelp, 'int')}
            ${fieldNumber('expander_clock_pin', 'Shift clock (SRCLK/SHCP) GPIO', cfg.expander_clock_pin, 0, 255, 1, pinHelp, 'int')}
            ${fieldNumber('expander_latch_pin', 'Latch clock (RCLK/STCP) GPIO', cfg.expander_latch_pin, 0, 255, 1, pinHelp, 'int')}
            ${fieldNumberPolarity('expander_oe_pin', 'expander_oe_active_low', 'Output enable (/OE) GPIO', cfg.expander_oe_pin, cfg.expander_oe_active_low, '255 when /OE is hard-wired on the board.')}
          </div>
        </div>

        <div class="form-section" data-driver-section="2">
          <h3>PCF8574 wiring</h3>
          <div class="form-grid">
            ${fieldHex('expander_address', 'I2C address (7-bit)', cfg.expander_address, '0x20–0x27 for a PCF8574, 0x38–0x3F for a PCF8574A. Other 0x08–0x77 addresses are accepted for compatible expanders.')}
            ${fieldNumber('i2c_sda', 'I2C SDA GPIO', cfg.i2c_sda, 0, 255, 1, pinHelp, 'int')}
            ${fieldNumber('i2c_scl', 'I2C SCL GPIO', cfg.i2c_scl, 0, 255, 1, pinHelp, 'int')}
          </div>
          <p class="small faint">The I2C bus is also reserved for the future local mixer encoder.</p>
        </div>

        <div class="form-section" data-driver-section="1 2">
          <h3>Output bit map</h3>
          <div class="form-grid">
            ${fieldNumberPolarity('expander_bit_heater', 'output_active_low_heater', 'Heater bit', cfg.expander_bit_heater, cfg.output_active_low_heater, bitHelp, 0, 7)}
            ${fieldNumberPolarity('expander_bit_pump_rad1', 'output_active_low_pump_rad1', 'Heating loop 1 pump bit', cfg.expander_bit_pump_rad1, cfg.output_active_low_pump_rad1, bitHelp, 0, 7)}
            ${fieldNumberPolarity('expander_bit_pump_rad2', 'output_active_low_pump_rad2', 'Heating loop 2 pump bit', cfg.expander_bit_pump_rad2, cfg.output_active_low_pump_rad2, bitHelp, 0, 7)}
            ${fieldNumberPolarity('expander_bit_pump_floor', 'output_active_low_pump_floor', 'Floor pump bit', cfg.expander_bit_pump_floor, cfg.output_active_low_pump_floor, bitHelp, 0, 7)}
            ${fieldNumberPolarity('expander_bit_mix_direction', 'output_active_low_mix_direction', 'Mixer channel 1 (open / direction) bit', cfg.expander_bit_mix_direction, cfg.output_active_low_mix_direction, bitHelp, 0, 7)}
            ${fieldNumberPolarity('expander_bit_mix_motion', 'output_active_low_mix_motion', 'Mixer channel 2 (close / motion) bit', cfg.expander_bit_mix_motion, cfg.output_active_low_mix_motion, bitHelp, 0, 7)}
          </div>
          <p class="small faint">Each output has its own polarity because a board may mix active-high and active-low relay drivers. The current-MUX select bits always stay active high.</p>
        </div>

        <div class="form-section" data-driver-section="1 2">
          <h3>Current MUX select bits</h3>
          <div class="form-grid">
            ${fieldNumber('expander_bit_mux_s0', 'Current MUX S0 bit', cfg.expander_bit_mux_s0, 0, 255, 1, muxHelp, 'int')}
            ${fieldNumber('expander_bit_mux_s1', 'Current MUX S1 bit', cfg.expander_bit_mux_s1, 0, 255, 1, muxHelp, 'int')}
          </div>
          <p class="small faint">Only the analogue MUX front end uses these bits. 255 means the line is not connected.</p>
        </div>

        <div class="form-section" data-driver-section="2">
          <h3>Interlock fault input</h3>
          <div class="form-grid">
            ${fieldNumberPolarity('expander_bit_mix_fault', 'mix_fault_active_low', 'Fault input bit', cfg.expander_bit_mix_fault, cfg.mix_fault_active_low, 'Bit 0–7, or 255 when the board has no fault output.', 0, 255)}
          </div>
          <p class="small faint">HomeNet Heating control v1.x drives P7 high when both mixer channels are requested. A healthy interlock therefore reads low, so <span class="mono">active low</span> stays unticked.</p>
        </div>
      </section>

      <section class="card writable accent-top">
        <div class="card-head"><div><h2>Status LEDs</h2><p>Addressable LED chain</p></div><span class="chip">READ ONLY COUNT</span></div>

        <div class="form-section">
          <h3>LED chain</h3>
          <div class="form-grid">
            ${fieldNumber('gpio_led_data', 'LED data GPIO', cfg.gpio_led_data, 0, 255, 1, pinHelp, 'int')}
            <div class="field">
              <label>LED count</label>
              <div class="metric-row"><span class="k">Fitted by the board profile</span><span class="v">${esc(cfg.num_leds ?? 0)} LEDs</span></div>
              <small>The LED count is fixed by the carrier profile and is not user-selectable.</small>
            </div>
          </div>
        </div>
      </section>

      <section class="card writable accent-top">
        <div class="card-head"><div><h2>Temperature sensor identities</h2><p>1-Wire bus and the three temperature roles</p></div><span class="chip chip-warn">REBOOT REQUIRED</span></div>

        <div class="form-section">
          <h3>1-Wire bus</h3>
          <div class="form-grid">
            ${fieldNumber('gpio_onewire', '1-Wire data GPIO', cfg.gpio_onewire, 0, 255, 1, pinHelp, 'int')}
          </div>
        </div>

        <div class="form-section">
          <h3>Temperature sensor roles</h3>
          <div class="row between" style="margin-bottom:10px">
            <p class="small faint">Scan the 1-Wire bus, then assign each temperature role from the discovered list.</p>
            <span>
              <button class="btn btn-sm" type="button" data-action="discover-sensors" ${state.sensorDiscoveryLoading ? 'disabled' : ''}>${state.sensorDiscoveryLoading ? 'Scanning…' : 'Discover sensors'}</button>
              <button class="btn btn-sm" type="button" data-action="verify-sensor-roles" ${state.sensorRoleLoading ? 'disabled' : ''}>${state.sensorRoleLoading ? 'Checking…' : 'Verify roles'}</button>
            </span>
          </div>
          ${sensorDiscoveryTable()}
          ${sensorRoleTable()}
          <div class="form-grid" style="margin-top:12px">
            ${fieldSelect('sensor_id_boiler', 'Mixer hot in temperature sensor', cfg.sensor_id_boiler, sensorIdOptions(cfg.sensor_id_boiler), 'Temperature sensor mounted on the boiler side of the mixer.', 'string')}
            ${fieldSelect('sensor_id_ret', 'Mixer cold in / return temperature sensor', cfg.sensor_id_ret, sensorIdOptions(cfg.sensor_id_ret), 'Temperature sensor on the cold inlet / return side.', 'string')}
            ${fieldSelect('sensor_id_mix', 'Mixer forward temperature sensor', cfg.sensor_id_mix, sensorIdOptions(cfg.sensor_id_mix), 'Temperature sensor on the mixed flow outlet.', 'string')}
          </div>
        </div>
      </section>

      <section class="card writable accent-top">
        <div class="card-head"><div><h2>Current measurement</h2><p>Pump and mixer current front end</p></div><span class="chip chip-warn">REBOOT REQUIRED</span></div>

        <div class="form-section">
          <h3>Front end</h3>
          <div class="form-grid">
            ${fieldSelect('current_sense', 'Current sensing', cfg.current_sense, [
              { value: 0, label: 'None' },
              { value: 1, label: 'Analogue MUX + ADC (HomeNet Heating control v0.1)' },
              { value: 2, label: 'ADS1115 (HomeNet Heating control v1.x)' }
            ], 'Selects the current-sensor front end. Settings for the other options are kept.')}
          </div>
          <p class="small faint" data-current-sense-section="1">The MUX select bits are configured with the output driver above.</p>
        </div>

        <div class="form-section" data-current-sense-section="2">
          <h3>ADS1115</h3>
          <div class="form-grid">
            ${fieldHex('ads1115_address', 'ADS1115 I2C address (7-bit)', cfg.ads1115_address, '0x48–0x4B for the four address straps.')}
            ${fieldCheckbox('tlv9064_present', 'TLV9064 sensor assembly fitted', cfg.tlv9064_present, 'The TLV9064 current-sensor assembly is an optional external part. When it is absent, current-based protection stays off.')}
          </div>
          <p class="small faint">ADS1115 inputs: A0 pump 1, A1 pump 2, A2 mixer, A3 pump 3 / floor. The mixer uses one sensor ring around both motor wires.</p>
        </div>

        <div class="form-section" data-current-sense-section="1 2">
          <h3>Pump current calibration</h3>
          <p class="small faint">Both front ends share the same calibration. Power is computed as <span class="mono">(Vrms − offset) × W/V</span>. Calibrate against a clamp meter before enabling protection on a board.</p>
          <div class="form-grid">
            ${fieldNumber('pump_current_w_per_v', 'Watts per volt', cfg.pump_current_w_per_v, 1, 5000, 0.1, 'Slope of the current-sense front end.', 'number', 'W/V')}
            ${fieldNumber('pump_current_offset_v', 'Zero-current offset', cfg.pump_current_offset_v, 0, 1, 0.001, 'RMS voltage reported with no pump current.', 'number', 'V')}
            ${fieldCheckbox('pump_power_check_enabled', 'Enable current-based protection', cfg.pump_power_check_enabled, 'Use the measured current for dry-run and welded-relay detection. Disable while the sensor is uncalibrated or absent.')}
          </div>
        </div>

        <div class="form-section" data-current-sense-section="1 2">
          <h3>Power meters</h3>
          <p class="small faint">Every meter is optional and enabled per channel. A channel without a meter is never sampled and reports 0 W; it takes no part in dry-run or welded-relay detection.</p>
          <div class="form-grid">
            ${fieldCheckbox('has_pump_power', 'Floor pump meter', pumpPower[0], 'Current ring on the floor circuit pump.', 0)}
            ${fieldCheckbox('has_pump_power', 'Radiator 1 meter', pumpPower[1], 'Current ring on radiator circuit 1.', 1)}
            ${fieldCheckbox('has_pump_power', 'Radiator 2 meter', pumpPower[2], 'Current ring on radiator circuit 2.', 2)}
            ${fieldCheckbox('has_mixer_power', 'Mixer meter', !!cfg.has_mixer_power, 'One ring around both mixer motor wires.')}
          </div>
        </div>
      </section>

      ${formActions('Save hardware settings')}
    </form>
  </div>`;
}

function renderSettingsControl() {
  const cfg = state.config.hardware;
  if (!cfg) return loadingCard('Loading control settings…');

  return `<div class="stack">
    <div class="banner warn">Writable configuration. Tick and timing changes may require a reboot; other values take effect on the next control cycle.</div>
    <form class="settings-form writable" data-section="hardware">
      <section class="card writable accent-top">
        <div class="card-head"><div><h2>Control &amp; timing</h2><p>Mixer constants, encoder and control-loop cadence</p></div><span class="chip chip-warn">NVS</span></div>

        <div class="form-section">
          <h3>Mixer and control constants</h3>
          <div class="form-grid">
            ${fieldRange('setpoint_default', 'Default setpoint', cfg.setpoint_default, 5, 80, 0.5, '°C', 'Fallback setpoint before HA supplies one.')}
            ${fieldRange('deadband_pos', 'Position deadband', cfg.deadband_pos, 0, 0.2, 0.005, '0–1', 'Mixer position deadband.')}
            ${fieldRange('deadband_temp', 'Temperature deadband', cfg.deadband_temp, 0.1, 5, 0.1, 'K', 'Temperature deadband around the setpoint.')}
            ${fieldRange('recalc_threshold', 'Recalculation threshold', cfg.recalc_threshold, 0.01, 1, 0.01, 'K', 'Minimum temperature change before recalculating.')}
            ${fieldNumber('travel_time_s', 'Full travel time', cfg.travel_time_ms / 1000, 1, 600, 1, 'Time from fully closed to fully open.', 'number', 's')}
          </div>
        </div>

        <div class="form-section">
          <h3>Cadence</h3>
          <div class="form-grid">
            ${fieldNumber('tick_ms', 'Control tick', cfg.tick_ms, 100, 60000, 100, 'Control loop period. Reboot required.', 'int', 'ms')}
            ${fieldNumber('temp_poll_interval_ms', 'Temperature poll', cfg.temp_poll_interval_ms, 100, 60000, 100, 'Interval between sensor polls. Reboot required.', 'int', 'ms')}
          </div>
        </div>

        <div class="form-section">
          <h3>Mixer control law (observer)</h3>
          <p class="small faint">Feedforward + PI defaults derived from the analysis dataset. Observer node will own these once it is online.</p>
          <div class="form-grid">
            ${fieldNumber('delay_s', 'Transport delay', cfg.delay_s, 0, 600, 1, 'Dead time between mixer movement and temperature response.', 'number', 's')}
            ${fieldNumber('mix_ff_offset', 'Feedforward offset', cfg.mix_ff_offset, -20, 20, 0.05, 'Analysis model intercept (K).', 'number', 'K')}
            ${fieldNumber('ff_boiler', 'Hot-side feedforward', cfg.ff_boiler, -2, 2, 0.01, 'Gain on the hot input (mixer hot in).', 'number', '')}
            ${fieldNumber('ff_return', 'Cold-side feedforward', cfg.ff_return, -2, 2, 0.01, 'Gain on the cold input (mixer cold in / return).', 'number', '')}
            ${fieldNumber('rad_coupling_ff', 'Radiator coupling', cfg.rad_coupling_ff, -10, 10, 0.1, 'Temperature offset when a radiator pump is running.', 'number', 'K')}
            ${fieldNumber('kp', 'Proportional gain', cfg.kp, 0, 5, 0.01, 'Position correction per Kelvin of predicted error.', 'number', '')}
            ${fieldNumber('ki', 'Integral gain', cfg.ki, 0, 1, 0.001, 'Integrator correction per Kelvin-second.', 'number', '')}
            ${fieldNumber('start_flush_ms', 'Warm-up flush', cfg.start_flush_ms / 1000, 0, 600, 1, 'Floor pump flush duration after a cold start.', 'number', 's')}
            ${fieldRange('flush_max_pos', 'Flush position cap', cfg.flush_max_pos, 0, 1, 0.01, '0–1', 'Mixer position cap during warm-up flush.')}
            ${fieldNumber('flush_delta_t_ok', 'Flush delta-T ok', cfg.flush_delta_t_ok, 0, 20, 0.1, 'Early flush completion when the return has stabilised.', 'number', 'K')}
            ${fieldNumber('min_useful_delta_t', 'Useful delta-T', cfg.min_useful_delta_t, 0, 20, 0.1, 'Floor delta-T below this indicates a saturated slab.', 'number', 'K')}
            ${fieldNumber('floor_pause_duration_s', 'Floor pause', cfg.floor_pause_duration_s, 0, 86400, 10, 'Planned pause after a floor delta-T collapse.', 'int', 's')}
          </div>
        </div>

        <div class="form-section">
          <h3>Encoder</h3>
          <div class="form-grid">
            ${fieldSelect('pos_source', 'Encoder position source', cfg.pos_source, [
              { value: 0, label: 'HA generic (0)' },
              { value: 1, label: 'I2C encoder (1)' }
            ], 'Selects the position feedback source.')}
            ${fieldNumber('encoder_check_interval_s', 'Encoder check interval', cfg.encoder_check_interval_s, 60, 86400, 60, 'Interval between encoder plausibility checks.', 'int', 's')}
            ${fieldNumber('encoder_check_pulses', 'Encoder check pulses', cfg.encoder_check_pulses, 1, 10000, 1, 'Pulses used for the plausibility check.', 'int', 'pulses')}
            ${fieldNumber('encoder_staleness_s', 'Encoder staleness', cfg.encoder_staleness_s, 10, 86400, 10, 'Time before the encoder is considered stale.', 'int', 's')}
          </div>
        </div>

        ${formActions('Save control settings')}
      </section>
    </form>
  </div>`;
}

/* ------------------------------------------------------------------------- */
/* Settings: Network & MQTT                                                   */
/* ------------------------------------------------------------------------- */

function signalWord(rssi) {
  if (rssi >= -55) return 'excellent';
  if (rssi >= -67) return 'good';
  if (rssi >= -75) return 'fair';
  return 'weak';
}

function renderWifiScan() {
  const scan = state.wifiScan;
  if (!scan) {
    return '<p class="small faint">Scan lists nearby 2.4 GHz networks with their signal strength; pick one to fill in the SSID. The current connection stays up.</p>';
  }
  if (scan.running) return '<p class="small faint">Scanning… (a few seconds)</p>';
  if (scan.error) return `<p class="small warn">${esc(scan.error)}</p>`;
  if (!scan.networks?.length) return '<p class="small faint">No networks found.</p>';
  return `<div class="wifi-list">${scan.networks.map((n) => `
    <button type="button" class="wifi-net" data-action="wifi-pick" data-ssid="${esc(n.ssid)}">
      <span class="wifi-ssid">${esc(n.ssid)}</span>
      <span class="wifi-meta">${esc(n.rssi)} dBm · ${signalWord(n.rssi)} · ch ${esc(n.channel)}${n.secure ? '' : ' · open'}</span>
    </button>`).join('')}</div>`;
}

async function scanWifi() {
  const box = () => $('#wifi-scan');
  try {
    state.wifiScan = await apiPost('/api/wifi/scan');
    if (box()) box().innerHTML = renderWifiScan();
    const deadline = Date.now() + 20000;
    while (state.wifiScan.running && Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, 1500));
      state.wifiScan = await apiGet('/api/wifi/scan');
    }
    if (state.wifiScan.running) state.wifiScan = { error: 'The scan did not finish.' };
  } catch (err) {
    state.wifiScan = { error: err.message };
  }
  if (box()) box().innerHTML = renderWifiScan();
}

function renderSettingsNetwork() {
  const cfg = state.config.network;
  if (!cfg) return loadingCard('Loading network settings…');
  const wifi = state.live?.wifi || state.status?.wifi || {};
  const mqtt = state.live?.mqtt || state.status?.mqtt || {};
  const ntp = state.status?.ntp || {};
  const mqttState = mqtt.connected ? `connected ${esc(mqtt.broker || '')}` : esc(mqtt.state || (mqtt.enabled ? 'offline' : 'disabled'));
  const ntpState = ntp.synced ? 'synchronized' : (ntp.enabled ? 'waiting for sync' : 'disabled');

  return `<div class="stack">
    <div class="banner warn">Writable configuration. Wi-Fi, NTP and timezone changes require a reboot; MQTT reconnects automatically after saving.</div>
    <form class="settings-form writable" data-section="network">
      <section class="card writable accent-top">
        <div class="card-head">
          <div><h2>Network &amp; MQTT</h2><p>Station Wi-Fi and broker connection</p></div>
          <span class="chip ${wifi.connected ? 'chip-ok' : 'chip-warn'}">${wifi.connected ? `connected ${esc(wifi.ip || '')}` : 'not connected'}</span>
        </div>

        <div class="form-section">
          <div class="row between">
            <h3>Wi-Fi station</h3>
            <button class="btn btn-sm" type="button" data-action="wifi-scan">Scan networks</button>
          </div>
          <div id="wifi-scan">${renderWifiScan()}</div>
          <div class="form-grid">
            ${fieldText('wifi_ssid', 'Wi-Fi SSID', cfg.wifi_ssid, 'Station network name.', 'text', 'Testing')}
            ${fieldPassword('wifi_password', 'Wi-Fi password', cfg.has_wifi_password, 'Leave blank to keep the stored password.')}
            ${fieldText('ap_ssid', 'Fallback AP SSID', cfg.ap_ssid, 'Opened when the station cannot connect; portal at http://192.168.4.1/.', 'text', 'HeatpumpsN-Setup')}
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

        <div class="form-section">
          <div class="row between">
            <h3>Time synchronization</h3>
            <span class="chip ${ntp.synced ? 'chip-ok' : (ntp.enabled ? 'chip-warn' : '')}">${esc(ntpState)}</span>
          </div>
          <div class="form-grid">
            ${fieldCheckbox('ntp_enabled', 'Enable NTP', cfg.ntp_enabled, 'Synchronize the controller wall clock over SNTP.')}
            ${fieldText('ntp_server', 'NTP server', cfg.ntp_server, 'Hostname or IP address of the NTP server.', 'text', 'pool.ntp.org')}
            ${fieldText('timezone', 'POSIX timezone', cfg.timezone, 'POSIX TZ string, for example CET-1CEST,M3.5.0,M10.5.0/3.', 'text', 'CET-1CEST,M3.5.0,M10.5.0/3')}
          </div>
        </div>

        <div class="form-section">
          <div class="row between">
            <h3>MQTT telemetry logs</h3>
            <span class="chip">RAM → MQTT</span>
          </div>
          <p class="small faint">Log records are always kept in RAM circular buffers and are never written to flash. Enable streaming to publish new records as JSON lines. An empty topic uses the base-derived default.</p>
          <div class="form-grid">
            ${fieldCheckbox('mqtt_log_enabled', 'Publish logs to MQTT', cfg.mqtt_log_enabled, 'Stream new slow-log samples and event records while the broker is connected.')}
            ${fieldText('mqtt_log_slow_topic', 'Slow log topic', cfg.mqtt_log_slow_topic, 'Full topic override. Leave empty for <base>/log/slow.', 'text', '')}
            ${fieldText('mqtt_log_event_topic', 'Event log topic', cfg.mqtt_log_event_topic, 'Full topic override. Leave empty for <base>/log/event.', 'text', '')}
          </div>
        </div>

        <div class="form-section">
          <div class="row between">
            <h3>MQTT meters</h3>
            <span class="chip">SUBSCRIBE</span>
          </div>
          <p class="small faint">The controller subscribes to these Home Assistant discovery topics, reads <span class="mono">state_topic</span>, and uses the value as mixer position feedback and DHW priority signal.</p>
          <div class="form-grid">
            ${fieldText('mixer_meter_device', 'Mixer meter device', cfg.mixer_meter_device, 'HA device name, for example mixermeter.', 'text', 'mixermeter')}
            ${fieldText('mixer_meter_field', 'Mixer meter field', cfg.mixer_meter_field, 'HA object/field name, for example mixer_value.', 'text', 'mixer_value')}
            ${fieldSelect('mixer_meter_scale', 'Mixer value scale', cfg.mixer_meter_scale, [
              { value: 0, label: 'Percent (0–100)' },
              { value: 1, label: 'Fraction (0–1)' }
            ], 'How to convert the MQTT payload into a 0–1 position.')}
            ${fieldText('mixer_meter_topic', 'Mixer state topic override', cfg.mixer_meter_topic, 'Optional: subscribe directly instead of using discovery.', 'text', '')}
            ${fieldText('dhw_meter_device', 'DHW meter device', cfg.dhw_meter_device, 'HA device name, for example ems-esp.', 'text', 'ems-esp')}
            ${fieldText('dhw_meter_entity', 'DHW entity', cfg.dhw_meter_entity, 'HA entity/object name, for example boiler_wwcharging.', 'text', 'boiler_wwcharging')}
            ${fieldText('dhw_meter_field', 'DHW payload field', cfg.dhw_meter_field, 'Key in the payload, for example tapwateractive or dhw.charging.', 'text', 'tapwateractive')}
            ${fieldText('dhw_meter_topic', 'DHW state topic override', cfg.dhw_meter_topic, 'Optional: subscribe directly instead of using discovery.', 'text', '')}
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
    <div class="banner info">Uploads need the admin login. The image's SHA-256 is signed with the request and checked against the received bytes before anything is activated.</div>

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
        <div class="form-grid">
          <div class="field">
            <label for="ota-target">Image</label>
            <select id="ota-target">
              <option value="app">Application (firmware.bin)</option>
              <option value="fs">Portal files (littlefs.bin)</option>
            </select>
          </div>
          <div class="field">
            <label for="ota-file">Image file (.bin)</label>
            <input id="ota-file" type="file" accept=".bin,application/octet-stream">
            <small>Do not power off during the update.</small>
          </div>
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

    <section class="card writable accent-top">
      <div class="card-head"><div><h2>Configuration backup</h2><p>Save or restore every portal setting as JSON</p></div><span class="chip chip-warn">SECRETS</span></div>
      <p class="small faint">The backup includes Wi-Fi and MQTT credentials in plain text. Store the file somewhere safe.</p>
      <div class="form-actions" style="margin-top:14px">
        <button class="btn btn-primary" type="button" data-action="config-export">Save configuration</button>
        <button class="btn" type="button" data-action="config-import">Restore configuration</button>
        <input id="config-file" type="file" accept=".json,application/json" hidden>
      </div>
    </section>
  </div>`;
}

/* ------------------------------------------------------------------------- */
/* Settings: Security                                                         */
/* ------------------------------------------------------------------------- */

async function refreshSecurity() {
  try {
    state.security = await apiGet('/api/security');
  } catch (err) {
    toast(`Could not load security settings: ${err.message}`, 'err');
  }
  return state.security;
}

function renderSettingsSecurity() {
  const sec = state.security;
  if (!sec) return loadingCard('Loading security settings…');
  const cmd = sec.commands || {};
  const users = sec.users || {};
  const defaults = Object.entries(users).filter(([, u]) => u.defaultPassword).map(([n]) => n);
  return `<div class="stack">
    <div class="banner info">Requests are signed with a key derived from your password and a single-use challenge from the controller; passwords and other secrets never cross the network in clear. See docs/security.md.</div>
    ${defaults.length ? `<div class="banner err">Factory password still set for: ${defaults.map(esc).join(', ')}. Change it below.</div>` : ''}

    <section class="card writable accent-top">
      <div class="card-head"><div><h2>Users and passwords</h2><p>admin changes settings; monitor can only look</p></div><span class="chip">logged in: ${esc(hpnAuth.user || '?')}</span></div>
      <div class="grid cols-2">
        <div class="field"><label for="pw-user">User</label>
          <select id="pw-user">${Object.keys(users).map((n) => `<option value="${esc(n)}">${esc(n)} (${esc(users[n].role)})</option>`).join('')}</select></div>
        <div class="stack">
          <div class="field"><label for="pw-new">New password</label><input id="pw-new" type="password" autocomplete="new-password"><small>At least 8 characters.</small></div>
          <div class="field"><label for="pw-confirm">Repeat new password</label><input id="pw-confirm" type="password" autocomplete="new-password"></div>
        </div>
      </div>
      <div class="form-actions"><button class="btn btn-primary" type="button" data-action="sec-password">Set password</button>
        <span class="small faint">Changing your own password logs you out.</span></div>
    </section>

    <section class="card writable accent-top">
      <div class="card-head"><div><h2>MQTT command secret</h2><p>MPC parameter and mode commands over MQTT must carry an HMAC with this secret (Home Assistant commands are not affected)</p></div>
        <span class="chip ${cmd.secretSet ? 'chip-ok' : 'chip-warn'}">${cmd.secretSet ? 'set' : 'not set'}</span></div>
      ${cmd.secretSet ? `<div class="metric-row"><span class="k">Fingerprint (SHA-256, first 8 bytes)</span><span class="v mono">${esc(cmd.fingerprint)}</span></div>
      <div class="metric-row"><span class="k">Last accepted sequence</span><span class="v mono">${esc(cmd.lastSeq)}</span></div>` : ''}
      <div class="field" style="margin-top:10px">
        <label for="cmd-secret">New secret</label>
        <input id="cmd-secret" type="password" autocomplete="off">
        <small>16-64 characters. The same value goes to the observer; it is never shown again.</small>
      </div>
      <div class="form-actions">
        <button class="btn btn-primary" type="button" data-action="sec-secret">Set secret</button>
        ${cmd.secretSet ? '<button class="btn btn-danger" type="button" data-action="sec-secret-remove">Remove secret</button>' : ''}
      </div>
    </section>
  </div>`;
}

async function securityAction(action, button) {
  try {
    if (action === 'sec-password') {
      const user = $('#pw-user').value;
      const next = $('#pw-new').value;
      if (next.length < 8) { toast('Use at least 8 characters.', 'warn'); return; }
      if (next !== $('#pw-confirm').value) { toast('The new passwords differ.', 'warn'); return; }
      // The device stores keys derived from the password, never the password.
      const salt = hpnAuth.toHex(crypto.getRandomValues(new Uint8Array(16)));
      const keys = hpnAuth.deriveKeys(next, salt, 10000);
      state.security = await apiSealed('POST', '/api/security/password',
        { user, salt, k_auth: hpnAuth.toHex(keys.kAuth), k_enc: hpnAuth.toHex(keys.kEnc) });
      toast(`Password of ${user} changed.`, 'ok');
      if (user === hpnAuth.user) { hpnAuth.logout(); showLogin('Password changed. Log in with the new password.'); return; }
    } else if (action === 'sec-secret' || action === 'sec-secret-remove') {
      const secret = action === 'sec-secret' ? $('#cmd-secret').value : '';
      if (action === 'sec-secret' && !secret) { toast('Enter a secret first.', 'warn'); return; }
      if (action === 'sec-secret-remove' && !(await confirmDialog('Remove secret?', 'MQTT commands will be accepted without a signature.', 'Remove', true))) return;
      state.security = await apiSealed('POST', '/api/security/secret', { secret });
      toast(secret ? 'Secret set.' : 'Secret removed.', 'ok');
    }
    renderCurrent();
  } catch (err) {
    toast(err.message, 'err', 6000);
  }
}

/* ------------------------------------------------------------------------- */
/* Login                                                                      */
/* ------------------------------------------------------------------------- */

function showLogin(message = '') {
  if ($('#login-overlay')) {
    if (message) $('#login-msg').textContent = message;
    return;
  }
  const el = document.createElement('div');
  el.id = 'login-overlay';
  el.className = 'login-overlay';
  el.innerHTML = `<form class="card login-card" id="login-form">
      <div class="login-logo" role="img" aria-label="HomeNET"></div>
      <h2>HeatpumpsN controller</h2>
      <div class="field"><label for="login-user">User</label>
        <select id="login-user"><option value="monitor">monitor (read-only)</option><option value="admin">admin</option></select></div>
      <div class="field"><label for="login-pass">Password</label><input id="login-pass" type="password" autocomplete="current-password"></div>
      <div class="small" id="login-msg">${esc(message)}</div>
      <div class="form-actions"><button class="btn btn-primary" type="submit">Log in</button></div>
      <p class="small faint">Use admin only to change settings: everyday viewing needs only monitor.</p>
    </form>`;
  document.body.appendChild(el);
  $('#login-pass').focus();
  $('#login-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    $('#login-msg').textContent = 'Checking…';
    try {
      const who = await hpnAuth.login($('#login-user').value, $('#login-pass').value);
      el.remove();
      updateUserBadge(who);
      startSession();
    } catch (err) {
      $('#login-msg').textContent = err.message;
    }
  });
}

function updateUserBadge(who) {
  const badge = $('#user-badge');
  if (!badge) return;
  badge.textContent = who ? `${who.user}${who.role === 'admin' ? '' : ' · read-only'}` : '';
  document.body.classList.toggle('readonly-user', !!who && who.role !== 'admin');
}

let sessionStarted = false;
async function startSession() {
  await Promise.allSettled([refreshStatus(), refreshEvents(), refreshAccess()]);
  connectWs();
  await onRoute();
  if (!sessionStarted) {
    sessionStarted = true;
    setInterval(tick, 5000);
  }
}

/* ------------------------------------------------------------------------- */
/* Settings: System                                                           */
/* ------------------------------------------------------------------------- */

function renderSettingsSystem() {
  const cfg = state.config.system;
  if (!cfg) return loadingCard('Loading system settings…');
  const status = state.status || {};
  const ntp = status.ntp || {};
  const theme = localStorage.getItem('hpn-theme') || 'dark';

  return `<div class="stack">
    <form class="settings-form writable" data-section="system">
      <section class="card writable accent-top">
        <div class="card-head"><div><h2>System</h2><p>Hostname and presentation</p></div><span class="chip">LOCAL</span></div>

        <div class="form-section">
          <h3>Identity</h3>
          <div class="form-grid">
            ${fieldText('hostname', 'Hostname', cfg.hostname, 'DHCP/mDNS hostname. Letters, digits, hyphen and dot; reboot required.', 'text', 'heatpumpsn')}
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
      </section>

      <section class="card writable accent-top">
        <div class="card-head"><div><h2>Summer mode</h2><p>Heating demand behaviour</p></div><span class="chip">LOCAL</span></div>
        <div class="form-section">
          <div class="form-grid">
            ${fieldCheckbox('summer_mode', 'Enable summer mode', cfg.summer_mode, 'Disables heating demand while still allowing circulation and diagnostics.')}
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
        <dt>Free heap</dt><dd>${fmtBytes(status.freeHeap)}${status.minFreeHeap !== undefined ? ` · min ${fmtBytes(status.minFreeHeap)}` : ''}</dd>
        <dt>CPU load</dt><dd>${status.cpuLoad !== undefined ? `${num(status.cpuLoad, 0).toFixed(2)} %` : 'n/a'}${status.taskCount !== undefined ? ` · ${status.taskCount} tasks` : ''}</dd>
        <dt>Last reset</dt><dd>${esc(status.resetReason || '—')}</dd>
        <dt>Wi-Fi</dt><dd>${esc(status.wifi?.ip || '—')} · ${esc(status.wifi?.state || '—')}</dd>
        <dt>Hostname</dt><dd>${esc(status.hostname || cfg.hostname || '—')}</dd>
        <dt>Time</dt><dd>${ntp.synced ? `synchronized · ${esc(fmtEpoch(ntp.epoch))}` : (ntp.enabled ? 'waiting for NTP sync' : 'NTP disabled')}</dd>
        <dt>Anti-seize</dt><dd>${status.antiSeizeNextDay ? `next in ${num(status.antiSeizeInDays, 0)} day(s)` : 'not scheduled'}</dd>
      </div>
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

  const driverSelect = root.querySelector('[data-field="output_expander"]');
  const senseSelect = root.querySelector('[data-field="current_sense"]');
  if (driverSelect || senseSelect) {
    // The same polarity flag is shown next to the GPIO number (direct driver)
    // and next to the bit number (expander drivers). Only one of those blocks
    // is visible at a time, so carry the ticks across when the driver changes.
    const polarityFields = '[data-field^="output_active_low_"]';
    const mirrorPolarity = () => {
      const values = new Map();
      $$(polarityFields, root).forEach((box) => {
        if (!box.closest('[hidden]') && !values.has(box.dataset.field)) {
          values.set(box.dataset.field, box.checked);
        }
      });
      $$(polarityFields, root).forEach((box) => {
        if (values.has(box.dataset.field)) box.checked = values.get(box.dataset.field);
      });
    };

    const syncConditionalSections = () => {
      mirrorPolarity();
      $$('[data-driver-section], [data-current-sense-section]', root).forEach((section) => {
        let visible = true;
        if (section.dataset.driverSection && driverSelect) {
          visible = visible && section.dataset.driverSection.split(' ').includes(driverSelect.value);
        }
        if (section.dataset.currentSenseSection && senseSelect) {
          visible = visible && section.dataset.currentSenseSection.split(' ').includes(senseSelect.value);
        }
        section.hidden = !visible;
      });
      mirrorPolarity();
    };
    driverSelect?.addEventListener('change', syncConditionalSections);
    senseSelect?.addEventListener('change', syncConditionalSections);
    syncConditionalSections();
  }

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
    // Fields inside a collapsed conditional section are not submitted. The
    // backend keeps the stored value for any key that is absent, so switching
    // the output driver or the current front end never clears the other mode.
    if (input.closest('[hidden]')) return;
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
    if (input.dataset.type === 'hex') {
      const parsed = parseInt(String(input.value).trim().replace(/^0x/i, ''), 16);
      data[key] = Number.isInteger(parsed) ? parsed : 0;
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

function normalizeSettings(section, data) {
  if (section === 'safety') {
    if (Number.isFinite(Number(data.anti_seize_interval_days))) {
      data.anti_seize_interval_s = Math.round(Number(data.anti_seize_interval_days) * 86400);
      delete data.anti_seize_interval_days;
    }
    if (Number.isFinite(Number(data.test_mode_max_min))) {
      data.test_mode_max_s = Math.round(Number(data.test_mode_max_min) * 60);
      delete data.test_mode_max_min;
    }
    if (Number.isFinite(Number(data.boiler_min_off_min))) {
      data.boiler_min_off_ms = Math.round(Number(data.boiler_min_off_min) * 60000);
      delete data.boiler_min_off_min;
    }
    if (Number.isFinite(Number(data.long_pause_min))) {
      data.long_pause_s = Math.round(Number(data.long_pause_min) * 60);
      delete data.long_pause_min;
    }
  } else if (section === 'hardware') {
    if (Number.isFinite(Number(data.travel_time_s))) {
      data.travel_time_ms = Math.round(Number(data.travel_time_s) * 1000);
      delete data.travel_time_s;
    }
    if (Number.isFinite(Number(data.start_flush_ms))) {
      data.start_flush_ms = Math.round(Number(data.start_flush_ms) * 1000);
    }
  }
  return data;
}

async function saveSettingsForm(form) {
  const section = form.dataset.section;
  const submit = form.querySelector('button[type="submit"]');
  if (submit) submit.disabled = true;
  try {
    const body = normalizeSettings(section, collectForm(form));
    const result = section === 'network' ? await apiSealed('PUT', '/api/config/network', body)
      : await apiPut(`/api/config/${section}`, body);
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
    'Manual output control bypasses HA demand. The session is time-limited and L1 still retains final authority over every output. The heater is normally board-managed; use this only for bench verification.',
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

async function testLeds(on) {
  try {
    await apiPost('/api/test/command', { leds: on });
    toast(on ? 'LED test running: all LEDs cycle through the colours.' : 'LED test stopped; status display restored.', 'ok');
  } catch (err) {
    toast(err.message, 'err', 6000);
  }
  await delay(300);
  await refreshState();
}

async function testMixer(mixer) {
  try {
    const result = await apiPost('/api/test/command', { mixer });
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

  const params = new URLSearchParams();
  if ($('#ota-target')?.value === 'fs') params.set('target', 'fs');

  const formData = new FormData();
  formData.append('firmware', file, file.name);

  // The image hash is a signed query parameter; the controller compares it
  // with the streamed bytes before activating anything.
  setOtaProgress(0, 'Hashing image…');
  params.set('sha256', hpnAuth.toHex(hpnAuth.sha256(new Uint8Array(await file.arrayBuffer()))));
  const url = `/api/update?${params}`;
  const xhr = new XMLHttpRequest();
  xhr.open('POST', url);
  xhr.setRequestHeader('X-HPN-Auth', await hpnAuth.uploadAuth(url));
  xhr.upload.onprogress = (event) => {
    if (!event.lengthComputable) return;
    const percent = (event.loaded / event.total) * 100;
    setOtaProgress(percent, `Uploading… ${percent.toFixed(0)}%`);
  };
  xhr.onload = () => {
    state.otaBusy = false;
    hpnAuth.takeNonce({ headers: { get: (h) => xhr.getResponseHeader(h) } });
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

async function exportConfig() {
  try {
    // The export holds credentials: the controller seals it for this session.
    const data = await api('/api/config/export', { unseal: true });
    const stamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
    downloadText(`heatpumpsn-config-${stamp}.json`, JSON.stringify(data, null, 2), 'application/json;charset=utf-8');
    toast('Configuration file downloaded.', 'ok');
  } catch (err) {
    toast(`Could not export configuration: ${err.message}`, 'err', 6000);
  }
}

async function importConfigFile(file) {
  if (!file) return;
  try {
    const text = await file.text();
    let data;
    try {
      data = JSON.parse(text);
    } catch (err) {
      throw new Error('file is not valid JSON');
    }
    const result = await apiSealed('POST', '/api/config/import', data);
    toast(
      result.reboot_required
        ? 'Configuration restored. Reboot required for the new values to take effect.'
        : 'Configuration restored.',
      result.reboot_required ? 'warn' : 'ok',
      result.reboot_required ? 6500 : 4000
    );
    await Promise.all([
      refreshConfig('network'),
      refreshConfig('safety'),
      refreshConfig('hardware'),
      refreshConfig('system'),
      refreshStatus()
    ]);
    renderCurrent();
  } catch (err) {
    toast(`Could not restore configuration: ${err.message}`, 'err', 7000);
  }
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
    case 'test-leds':
      await testLeds(button.dataset.on === 'true');
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
    case 'discover-sensors':
      await discoverSensors();
      break;
    case 'verify-sensor-roles':
      await verifySensorRoles();
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
    case 'sec-password':
    case 'sec-secret':
    case 'sec-secret-remove':
      await securityAction(action, button);
      break;
    case 'wifi-scan':
      button.disabled = true;
      await scanWifi();
      button.disabled = false;
      break;
    case 'wifi-pick': {
      const input = $('#f_wifi_ssid');
      if (input) {
        input.value = button.dataset.ssid;
        input.dispatchEvent(new Event('input', { bubbles: true }));
        $('#f_wifi_password')?.focus();
      }
      break;
    }
    case 'config-export':
      await exportConfig();
      break;
    case 'config-import':
      $('#config-file')?.click();
      break;
    default:
      break;
  }
}

async function handleViewChange(event) {
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
  } else if (target.id === 'config-file') {
    const file = target.files && target.files[0];
    target.value = '';
    if (file) await importConfigFile(file);
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
    enter: () => {
      refreshSlow().then(() => {
        if (state.route === 'log/slow') renderCurrent();
      });
    }
  },
  diagnostics: {
    title: 'Diagnostics',
    subtitle: 'Link state, temperature sensors and relay counters',
    render: renderDiagnostics,
    enter: async () => {
      await Promise.all([refreshCounters(), refreshStatus()]);
      if (state.route === 'diagnostics') renderCurrent();
    }
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
    subtitle: 'Board wiring, GPIO map, LEDs and temperature sensors',
    render: renderSettingsHardware,
    enter: async () => { await refreshConfig('hardware'); }
  },
  'settings/control': {
    title: 'Control & Timing',
    subtitle: 'Mixer constants, encoder and control cadence',
    render: renderSettingsControl,
    enter: async () => { await refreshConfig('hardware'); }
  },
  'settings/network': {
    title: 'Network & MQTT',
    subtitle: 'Writable Wi-Fi, broker and time configuration',
    render: renderSettingsNetwork,
    enter: async () => { await Promise.all([refreshConfig('network'), refreshStatus()]); }
  },
  'settings/firmware': {
    title: 'Firmware Update',
    subtitle: 'OTA application upload and configuration backup',
    render: renderSettingsFirmware,
    enter: async () => { await Promise.all([refreshStatus(), refreshSecurity()]); }
  },
  'settings/security': {
    title: 'Security',
    subtitle: 'Users, passwords and the MQTT command secret',
    render: renderSettingsSecurity,
    enter: async () => { await refreshSecurity(); }
  },
  'settings/system': {
    title: 'System',
    subtitle: 'Hostname, operating mode and maintenance',
    render: renderSettingsSystem,
    enter: async () => { await Promise.all([refreshConfig('system'), refreshStatus()]); }
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
  } else if (state.route === 'diagnostics' && now - state.lastCountersFetch > 30000) {
    await refreshCounters();
    if (state.route === 'diagnostics') renderCurrent();
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
  $('#logout-btn')?.addEventListener('click', () => {
    hpnAuth.logout();
    state.ws?.close();
    updateUserBadge(null);
    showLogin('Logged out.');
  });
  if (!hpnAuth.user) {
    showLogin();
    return;
  }
  try {
    updateUserBadge(await api('/api/session'));
  } catch (err) {
    return;   // showLogin() was called by api()
  }
  startSession();
}

boot();




