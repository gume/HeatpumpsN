'use strict';

/* HeatpumpsN board-emulation page. Served only by HPN_IO_EMULATION builds. */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const root = $('#emu-root');

const state = {
  enabled: false,
  snapshot: null,
  error: null,
  timer: null,
  interactingUntil: 0
};

const TEMP_LABELS = {
  boiler: 'Boiler supply',
  return: 'Floor return',
  mix: 'Mixer forward'
};

const POWER_LABELS = {
  floor: 'Floor pump',
  rad1: 'Radiator 1 pump',
  rad2: 'Radiator 2 pump',
  mixer: 'Mixer motor'
};

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

function fmtNum(value, digits = 0) {
  const n = num(value);
  return n === null ? '—' : n.toFixed(digits);
}

function fmtTemp(value) {
  const n = num(value);
  return n === null ? '—' : `${n.toFixed(1)} °C`;
}

let toastTimer = null;

function toast(message, kind = 'info', ms = 4000) {
  const el = $('#toast');
  el.textContent = message;
  el.className = `toast ${kind}`;
  el.classList.remove('hidden');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add('hidden'), ms);
}

async function api(path, options = {}) {
  const response = await fetch(path, { cache: 'no-store', ...options });
  const text = await response.text();
  let data = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch (err) {
      throw new Error(text.slice(0, 200) || response.statusText);
    }
  }
  if (!response.ok) {
    throw new Error((data && data.error) || response.statusText || `HTTP ${response.status}`);
  }
  return data;
}

function setStatus(text, kind = '') {
  const badge = $('#emu-status');
  badge.textContent = text;
  badge.className = `badge ${kind}`;
}

function snapshotMap(snapshot) {
  const temps = {};
  for (const item of snapshot.temperatures || []) {
    temps[item.role] = item;
  }
  return temps;
}

function relayPill(on) {
  return `<span class="state-pill ${on ? 'state-on' : 'state-off'}">${on ? 'ON' : 'OFF'}</span>`;
}

function renderDisabled() {
  setStatus('hardware build', 'badge-err');
  root.innerHTML = `<section class="card readonly emu-404">
    <div class="card-head"><div><h2>Board Emulation is not available</h2><p>This controller was built without <span class="mono">HPN_IO_EMULATION</span>.</p></div><span class="chip chip-err">DISABLED</span></div>
    <p class="muted">The emulation page and its <span class="mono">/api/sim</span> routes exist only in the simulated build. Use the normal portal for hardware builds.</p>
    <div class="form-actions" style="justify-content:center;margin-top:16px"><a class="btn btn-primary" href="/">Back to portal</a></div>
  </section>`;
}

function renderError(message) {
  setStatus('offline', 'badge-err');
  root.innerHTML = `<section class="card readonly emu-404">
    <div class="card-head"><div><h2>Emulation model unavailable</h2><p>${esc(message)}</p></div><span class="chip chip-err">OFFLINE</span></div>
    <p class="muted">Check that the controller is reachable and that this is an emulation build.</p>
    <div class="form-actions" style="justify-content:center;margin-top:16px">
      <button class="btn btn-primary" type="button" data-sim-retry>Retry</button>
      <a class="btn" href="/">Back to portal</a>
    </div>
  </section>`;
}

function renderTemperatureControls(temps) {
  return Object.keys(TEMP_LABELS).map((role) => {
    const item = temps[role] || { value: 0, valid: true };
    const value = num(item.value, 0);
    return `<div class="emu-control">
      <div class="row between"><strong>${esc(TEMP_LABELS[role])}</strong><span class="small faint mono">${esc(role)}</span></div>
      <div class="range-row">
        <input type="range" min="-20" max="120" step="0.5" value="${value}" data-temp-range="${esc(role)}" aria-label="${esc(TEMP_LABELS[role])} slider">
        <input type="number" min="-20" max="120" step="0.5" value="${value}" data-temp-value="${esc(role)}" aria-label="${esc(TEMP_LABELS[role])} value">
        <span class="unit">°C</span>
      </div>
      <div class="row between">
        <label class="checkbox-row"><input type="checkbox" data-temp-valid="${esc(role)}" ${item.valid !== false ? 'checked' : ''}> valid</label>
        <button class="btn btn-sm" type="button" data-sim-temp="${esc(role)}">Apply</button>
      </div>
    </div>`;
  }).join('');
}

function renderPowerControls(power) {
  return Object.keys(POWER_LABELS).map((channel) => `<div class="emu-control">
    <div class="row between"><strong>${esc(POWER_LABELS[channel])}</strong><span class="small faint mono">${esc(channel)}</span></div>
    <div class="range-row">
      <input type="range" min="0" max="2000" step="10" value="${num(power[channel], 0)}" data-power-range="${esc(channel)}" aria-label="${esc(POWER_LABELS[channel])} slider">
      <input type="number" min="0" max="2000" step="10" value="${num(power[channel], 0)}" data-power-value="${esc(channel)}" aria-label="${esc(POWER_LABELS[channel])} watts">
      <span class="unit">W</span>
    </div>
    <div class="row" style="justify-content:flex-end"><button class="btn btn-sm" type="button" data-sim-power="${esc(channel)}">Apply</button></div>
  </div>`).join('');
}

function renderStage(snapshot, temps) {
  const relays = snapshot.relays || {};
  const power = snapshot.power || {};
  const mixer = snapshot.mixer || {};
  const leds = Array.isArray(snapshot.leds) ? snapshot.leds : [];
  const outputByte = num(snapshot.outputByte, 0);

  return `<section class="emu-stage">
    <div class="emu-block emu-heater ${relays.heater ? 'on' : ''}">
      <h3>Heater relay</h3>
      <div class="coil">♨</div>
      ${relayPill(!!relays.heater)}
      <div class="metric-sub" style="margin-top:6px">Final authority: L1 interlock</div>
    </div>

    <div class="emu-pumps">
      <div class="emu-block emu-pump ${relays.floor ? 'on' : ''}">
        <h3>Floor pump</h3>
        <div class="fan">✱</div>
        ${relayPill(!!relays.floor)}
        <div class="metric-sub">${fmtNum(power.floor, 0)} W</div>
      </div>
      <div class="emu-block emu-pump ${relays.rad1 ? 'on' : ''}">
        <h3>Radiator 1</h3>
        <div class="fan">✱</div>
        ${relayPill(!!relays.rad1)}
        <div class="metric-sub">${fmtNum(power.rad1, 0)} W</div>
      </div>
      <div class="emu-block emu-pump ${relays.rad2 ? 'on' : ''}">
        <h3>Radiator 2</h3>
        <div class="fan">✱</div>
        ${relayPill(!!relays.rad2)}
        <div class="metric-sub">${fmtNum(power.rad2, 0)} W</div>
      </div>
    </div>

    <div class="emu-block emu-mixer">
      <h3>Mixer motor</h3>
      <div class="row between">
        ${relayPill(!!mixer.motion)}
        <span class="chip">${mixer.open ? 'open command' : 'close command'}</span>
      </div>
      <div class="mixer-bar" style="margin-top:10px">
        <span class="${mixer.motion ? 'moving' : ''}" style="left:${mixer.open ? '50%' : '0%'};width:50%"></span>
      </div>
      <div class="mixer-labels"><span>closed</span><span>open</span></div>
      <p class="emu-note">${mixer.motion ? 'Motor energized. Direction is simulated from the relay pair.' : 'Motor idle.'}</p>
    </div>

    <div class="grid cols-2" style="margin-top:16px">
      <div class="emu-block">
        <h3>Relay outputs</h3>
        <div class="emu-relays">
          <div class="emu-relay ${relays.heater ? 'on' : ''}"><span>Heater</span><span class="lamp"></span></div>
          <div class="emu-relay ${relays.floor ? 'on' : ''}"><span>Floor</span><span class="lamp"></span></div>
          <div class="emu-relay ${relays.rad1 ? 'on' : ''}"><span>Radiator 1</span><span class="lamp"></span></div>
          <div class="emu-relay ${relays.rad2 ? 'on' : ''}"><span>Radiator 2</span><span class="lamp"></span></div>
        </div>
      </div>
      <div class="emu-block">
        <h3>LED ring</h3>
        <div class="emu-leds">
          ${leds.length ? leds.map((rgb, index) => `<span class="emu-led" title="LED ${index}" style="background:rgb(${num(rgb[0], 0)},${num(rgb[1], 0)},${num(rgb[2], 0)})"></span>`).join('') : '<span class="small faint">No LEDs configured.</span>'}
        </div>
        <div class="metric-row" style="margin-top:8px"><span class="k">LED count</span><span class="v">${num(snapshot.ledCount, 0)}</span></div>
      </div>
    </div>

    <div class="emu-block" style="margin-top:16px">
      <h3>Carrier readout</h3>
      <div class="emu-readout">
        <div class="kv"><dt>Mux channel</dt><dd>${num(snapshot.muxChannel, 0)}</dd></div>
        <div class="kv"><dt>Output byte</dt><dd>0x${outputByte.toString(16).padStart(2, '0')} (${outputByte})</dd></div>
        <div class="kv"><dt>Output enable</dt><dd>${snapshot.outputEnabled ? 'asserted' : 'deasserted'}</dd></div>
        <div class="kv"><dt>Model revision</dt><dd>${num(snapshot.revision, 0)}</dd></div>
      </div>
    </div>
  </section>`;
}

function render() {
  if (!state.enabled) return;

  if (state.error) {
    renderError(state.error);
    return;
  }

  if (!state.snapshot) {
    root.innerHTML = `<div class="card readonly"><div class="empty">Waiting for the simulation model…</div></div>`;
    return;
  }

  const active = document.activeElement;
  if (active && root.contains(active) && active.matches('input[type="number"], input[type="range"]')) {
    return;
  }
  if (Date.now() < state.interactingUntil) {
    return;
  }

  const snapshot = state.snapshot;
  const temps = snapshotMap(snapshot);

  setStatus(`emulation · rev ${num(snapshot.revision, 0)}`, 'badge-ok');

  root.innerHTML = `<div class="emu-layout">
    ${renderStage(snapshot, temps)}
    <aside class="stack">
      <section class="card writable accent-top">
        <div class="card-head"><div><h2>Temperature inputs</h2><p>Feed the simulated carrier</p></div><span class="chip">3 roles</span></div>
        <div class="stack">${renderTemperatureControls(temps)}</div>
      </section>

      <section class="card writable">
        <div class="card-head"><div><h2>Power inputs</h2><p>Current-sense channels</p></div><span class="chip">4 channels</span></div>
        <div class="stack">${renderPowerControls(snapshot.power || {})}</div>
      </section>

      <section class="card readonly">
        <div class="card-head"><div><h2>Simulation controls</h2><p>Model state and reset</p></div></div>
        <div class="banner info">Simulation values are not safety arbitration. L1 still enforces the heater/pump interlock and temperature limits.</div>
        <div class="form-actions" style="margin-top:12px">
          <button class="btn btn-danger" type="button" data-sim-reset>Reset simulation model</button>
          <a class="btn" href="/">Back to portal</a>
        </div>
      </section>
    </aside>
  </div>`;
}

async function refreshSim() {
  if (!state.enabled) return;
  try {
    state.snapshot = await api('/api/sim');
    state.error = null;
  } catch (err) {
    state.error = err.message;
  }
  render();
}

async function init() {
  try {
    const status = await api('/api/status');
    state.enabled = !!status.portal?.emulation;
  } catch (err) {
    state.enabled = true;
    state.error = err.message;
    renderError(err.message);
    return;
  }

  if (!state.enabled) {
    renderDisabled();
    return;
  }

  await refreshSim();
  if (state.timer) clearInterval(state.timer);
  state.timer = setInterval(refreshSim, 750);
}

function syncRange(target, value) {
  if (!target) return;
  const counterpart = target.matches('[data-temp-range]')
    ? root.querySelector(`[data-temp-value="${target.dataset.tempRange}"]`)
    : target.matches('[data-temp-value]')
      ? root.querySelector(`[data-temp-range="${target.dataset.tempValue}"]`)
      : target.matches('[data-power-range]')
        ? root.querySelector(`[data-power-value="${target.dataset.powerRange}"]`)
        : target.matches('[data-power-value]')
          ? root.querySelector(`[data-power-range="${target.dataset.powerValue}"]`)
          : null;
  if (counterpart) counterpart.value = value;
}

root.addEventListener('input', (event) => {
  const target = event.target;
  if (target.matches('[data-temp-range], [data-temp-value], [data-power-range], [data-power-value]')) {
    state.interactingUntil = Date.now() + 1500;
    syncRange(target, target.value);
  }
});

root.addEventListener('click', async (event) => {
  const button = event.target.closest('[data-sim-temp], [data-sim-power], [data-sim-reset], [data-sim-retry]');
  if (!button) return;

  if (button.matches('[data-sim-retry]')) {
    state.error = null;
    await init();
    return;
  }

  if (button.matches('[data-sim-reset]')) {
    if (!window.confirm('Reset the simulation model to its defaults?')) return;
    try {
      await api('/api/sim/reset', { method: 'POST' });
      state.interactingUntil = 0;
      toast('Simulation model reset.', 'ok');
      await refreshSim();
    } catch (err) {
      toast(err.message, 'err', 6000);
    }
    return;
  }

  if (button.matches('[data-sim-temp]')) {
    const role = button.dataset.simTemp;
    const valueInput = root.querySelector(`[data-temp-value="${role}"]`);
    const validInput = root.querySelector(`[data-temp-valid="${role}"]`);
    const value = Number(valueInput?.value);
    if (!Number.isFinite(value)) {
      toast('Enter a numeric temperature.', 'warn');
      return;
    }
    button.disabled = true;
    try {
      await api('/api/sim/temperature', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ role, value, valid: validInput ? validInput.checked : true })
      });
      state.interactingUntil = 0;
      toast(`${TEMP_LABELS[role] || role} set to ${fmtTemp(value)}.`, 'ok');
      await refreshSim();
    } catch (err) {
      toast(err.message, 'err', 6000);
    } finally {
      button.disabled = false;
    }
    return;
  }

  if (button.matches('[data-sim-power]')) {
    const channel = button.dataset.simPower;
    const valueInput = root.querySelector(`[data-power-value="${channel}"]`);
    const watts = Number(valueInput?.value);
    if (!Number.isFinite(watts)) {
      toast('Enter a numeric wattage.', 'warn');
      return;
    }
    button.disabled = true;
    try {
      await api('/api/sim/power', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ channel, watts })
      });
      state.interactingUntil = 0;
      toast(`${POWER_LABELS[channel] || channel} set to ${fmtNum(watts, 0)} W.`, 'ok');
      await refreshSim();
    } catch (err) {
      toast(err.message, 'err', 6000);
    } finally {
      button.disabled = false;
    }
  }
});

init();
