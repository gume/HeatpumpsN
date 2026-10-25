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
  boiler: 'Mixer hot in',
  return: 'Mixer cold in / return',
  mix: 'Mixer forward'
};

const POWER_LABELS = {
  floor: 'Floor pump',
  rad1: 'Radiator 1 pump',
  rad2: 'Radiator 2 pump',
  mixer: 'Mixer motor'
};

const POWER_DEFAULTS = {
  floor: 10,
  rad1: 10,
  rad2: 10,
  mixer: 2
};

const RELAY_LABELS = {
  heater: 'Heater',
  floor: 'Floor pump',
  rad1: 'Radiator 1',
  rad2: 'Radiator 2'
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

function renderTemperatureControls(temps, snapshot) {
  const realisticBoiler = !!snapshot.realisticBoiler;

  return Object.keys(TEMP_LABELS).map((role) => {
    const item = temps[role] || { value: 0, valid: true };
    const value = num(item.value, 0);
    const locked = role === 'boiler' && realisticBoiler;
    const disabled = locked ? 'disabled' : '';
    const min = role === 'boiler' ? 15 : -20;
    const max = role === 'boiler' ? 85 : 120;

    return `<div class="emu-control">
      <div class="row between"><strong>${esc(TEMP_LABELS[role])}</strong><span class="small faint mono">${esc(role)}</span></div>
      <div class="range-row">
        <input type="range" min="${min}" max="${max}" step="0.5" value="${value}" data-temp-range="${esc(role)}" aria-label="${esc(TEMP_LABELS[role])} slider" ${disabled}>
        <input type="number" min="${min}" max="${max}" step="0.5" value="${value}" data-temp-value="${esc(role)}" aria-label="${esc(TEMP_LABELS[role])} value" ${disabled}>
        <span class="unit">°C</span>
      </div>
      <div class="row between">
        <label class="checkbox-row"><input type="checkbox" data-temp-valid="${esc(role)}" ${item.valid !== false ? 'checked' : ''} ${disabled}> valid</label>
        <button class="btn btn-sm" type="button" data-sim-temp="${esc(role)}" ${disabled}>Apply</button>
      </div>
      ${locked ? '<div class="small faint">Realistic boiler sine is active. Turn it off to edit.</div>' : ''}
    </div>`;
  }).join('');
}

function renderPowerControls(power, powerSet, snapshot) {
  const realistic = !!snapshot.realisticPower;

  return Object.keys(POWER_LABELS).map((channel) => {
    const value = num(powerSet[channel], POWER_DEFAULTS[channel]);
    const actual = num(power[channel], 0);
    const label = realistic ? (channel === 'mixer' ? 'Moving power' : 'On power') : 'Power';

    return `<div class="emu-control">
      <div class="row between"><strong>${esc(POWER_LABELS[channel])}</strong><span class="small faint mono">${esc(channel)}</span></div>
      <div class="small faint">Actual: ${fmtNum(actual, 1)} W${realistic ? ` · ${label.toLowerCase()} ${fmtNum(value, 1)} W` : ''}</div>
      <div class="range-row">
        <input type="range" min="0" max="40" step="0.5" value="${value}" data-power-range="${esc(channel)}" aria-label="${esc(POWER_LABELS[channel])} slider">
        <input type="number" min="0" max="40" step="0.5" value="${value}" data-power-value="${esc(channel)}" aria-label="${esc(POWER_LABELS[channel])} watts">
        <span class="unit">W</span>
      </div>
      <div class="row between">
        <span class="small faint">${realistic ? label : 'Direct override'}</span>
        <button class="btn btn-sm" type="button" data-sim-power="${esc(channel)}">Apply</button>
      </div>
    </div>`;
  }).join('');
}

function relayCard(relay, on, override, watts = null) {
  return `<button class="emu-relay-card ${on ? 'is-on' : ''}" type="button" data-sim-relay="${relay}" data-on="${on ? 'true' : 'false'}" aria-pressed="${on ? 'true' : 'false'}">
    <span class="emu-relay-card-head">
      <span>${esc(RELAY_LABELS[relay] || relay)}</span>
      ${override ? `<span class="emu-manual" data-clear-relay="${esc(relay)}" title="Clear manual override">M</span>` : ''}
    </span>
    <span class="emu-relay-card-state">${on ? 'ON' : 'OFF'}</span>
    <span class="emu-relay-card-meta">${watts === null ? 'carrier relay' : `${fmtNum(watts, 1)} W`}</span>
  </button>`;
}

function renderStage(snapshot) {
  const relays = snapshot.relays || {};
  const relayOverride = snapshot.relayOverride || {};
  const power = snapshot.power || {};
  const mixer = snapshot.mixer || {};
  const outputByte = num(snapshot.outputByte, 0);

  return `<section class="emu-stage">
    <div class="emu-block">
      <h3>Relay outputs</h3>
      <div class="emu-relay-grid">
        ${relayCard('heater', !!relays.heater, !!relayOverride.heater)}
        ${relayCard('floor', !!relays.floor, !!relayOverride.floor, num(power.floor, 0))}
        ${relayCard('rad1', !!relays.rad1, !!relayOverride.rad1, num(power.rad1, 0))}
        ${relayCard('rad2', !!relays.rad2, !!relayOverride.rad2, num(power.rad2, 0))}
      </div>
      <p class="emu-note">Click a relay card to toggle it. Manual toggles override the controller; this path does not run L1 and has no heater/pump interlock.</p>
    </div>

    <div class="emu-stage-grid">
      <div class="emu-block emu-mixer">
        <h3>Mixer motor ${mixer.override ? '<span class="emu-manual" data-clear-mixer="true" title="Clear manual override">manual</span>' : ''}</h3>
        <div class="row between">
          ${relayPill(!!mixer.motion)}
          <span class="chip">${mixer.motion ? (mixer.open ? 'open command' : 'close command') : 'idle'}</span>
        </div>
        <div class="mixer-bar" style="margin-top:10px">
          <span class="${mixer.motion ? 'moving' : ''}" style="left:${mixer.open ? '50%' : '0%'};width:50%"></span>
        </div>
        <div class="mixer-labels"><span>closed</span><span>open</span></div>
        <div class="row" style="gap:6px;margin-top:10px;flex-wrap:wrap">
          <button class="btn btn-sm" type="button" data-sim-mixer="open">Open</button>
          <button class="btn btn-sm" type="button" data-sim-mixer="stop">Stop</button>
          <button class="btn btn-sm" type="button" data-sim-mixer="close">Close</button>
        </div>
        <p class="emu-note">${mixer.motion ? `Motor energized (${mixer.open ? 'opening' : 'closing'}), ${fmtNum(power.mixer, 1)} W.` : `Motor idle, ${fmtNum(power.mixer, 1)} W.`}</p>
      </div>

      <div class="emu-block">
        <h3>Carrier readout</h3>
        <div class="emu-readout">
          <div class="kv"><dt>Controller mux</dt><dd>${num(snapshot.muxChannel, 0)}</dd></div>
          <div class="kv"><dt>Controller output byte</dt><dd>0x${outputByte.toString(16).padStart(2, '0')} (${outputByte})</dd></div>
          <div class="kv"><dt>Output enable</dt><dd>${snapshot.outputEnabled ? 'asserted' : 'deasserted'}</dd></div>
          <div class="kv"><dt>Model revision</dt><dd>${num(snapshot.revision, 0)}</dd></div>
        </div>
        <p class="emu-note" style="margin-top:8px">The output byte is L0's last latch. Manual relay cards can override the emulated board without changing L0.</p>
      </div>
    </div>
  </section>`;
}

function syncLiveReadouts(snapshot) {
  const temps = snapshotMap(snapshot);
  for (const role of Object.keys(TEMP_LABELS)) {
    const item = temps[role];
    if (!item) continue;
    const value = num(item.value, 0);
    const range = root.querySelector(`[data-temp-range="${role}"]`);
    const number = root.querySelector(`[data-temp-value="${role}"]`);
    if (range && document.activeElement !== range) range.value = value;
    if (number && document.activeElement !== number) number.value = value;
  }

  const powerSet = snapshot.powerSet || {};
  for (const channel of Object.keys(POWER_LABELS)) {
    const value = num(powerSet[channel], POWER_DEFAULTS[channel]);
    const range = root.querySelector(`[data-power-range="${channel}"]`);
    const number = root.querySelector(`[data-power-value="${channel}"]`);
    if (range && document.activeElement !== range) range.value = value;
    if (number && document.activeElement !== number) number.value = value;
  }
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
  if (active && root.contains(active) &&
      active.matches('input[type="number"], input[type="range"], input[type="checkbox"]')) {
    return;
  }
  if (Date.now() < state.interactingUntil) {
    return;
  }

  const snapshot = state.snapshot;
  const temps = snapshotMap(snapshot);
  const relayOverride = snapshot.relayOverride || {};
  const overrideCount = Object.values(relayOverride).filter(Boolean).length +
    (snapshot.mixer && snapshot.mixer.override ? 1 : 0);
  const followChip = snapshot.followController
    ? (overrideCount ? `following · ${overrideCount} manual` : 'following')
    : 'manual';

  setStatus(`emulation · rev ${num(snapshot.revision, 0)}`, 'badge-ok');

  root.innerHTML = `<div class="emu-layout">
    ${renderStage(snapshot)}
    <aside class="stack">
      <section class="card writable accent-top">
        <div class="card-head"><div><h2>Emulation behavior</h2><p>Follow the controller or drive the board directly</p></div><span class="chip">${esc(followChip)}</span></div>
        <div class="stack">
          <label class="checkbox-row"><input type="checkbox" data-sim-follow ${snapshot.followController ? 'checked' : ''}> Follow controller relays</label>
          <p class="small faint">When on, L0 drives the emulated relays and mixer. Click a relay card to override one channel; clear overrides to resume following.</p>

          <label class="checkbox-row"><input type="checkbox" data-sim-realistic-power ${snapshot.realisticPower ? 'checked' : ''}> Realistic current sensors</label>
          <p class="small faint">Pump power follows its relay (default 10 W); mixer power follows motion (default 2 W). Set a different on/moving wattage below.</p>

          <label class="checkbox-row"><input type="checkbox" data-sim-realistic-boiler ${snapshot.realisticBoiler ? 'checked' : ''}> Realistic boiler supply</label>
          <p class="small faint">Placeholder sine wave around 65 °C until the logged boiler shape is available.</p>

          <div class="form-actions">
            <button class="btn btn-sm" type="button" data-sim-clear-overrides>Clear manual overrides</button>
          </div>
        </div>
      </section>

      <section class="card writable">
        <div class="card-head"><div><h2>Temperature inputs</h2><p>Feed the simulated carrier</p></div><span class="chip">3 roles</span></div>
        <div class="stack">${renderTemperatureControls(temps, snapshot)}</div>
      </section>

      <section class="card writable">
        <div class="card-head"><div><h2>Power inputs</h2><p>Current-sense channels</p></div><span class="chip">0–40 W</span></div>
        <div class="stack">${renderPowerControls(snapshot.power || {}, snapshot.powerSet || {}, snapshot)}</div>
      </section>

      <section class="card readonly">
        <div class="card-head"><div><h2>Simulation controls</h2><p>Model state and reset</p></div></div>
        <div class="banner info">This page drives the emulated carrier directly. It does not run L1 and has no heater/pump interlock.</div>
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
    syncLiveReadouts(state.snapshot);
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

root.addEventListener('change', async (event) => {
  const target = event.target;
  let payload = null;

  if (target.matches('[data-sim-follow]')) {
    payload = { followController: target.checked };
  } else if (target.matches('[data-sim-realistic-power]')) {
    payload = { realisticPower: target.checked };
  } else if (target.matches('[data-sim-realistic-boiler]')) {
    payload = { realisticBoiler: target.checked };
  }
  if (!payload) return;

  state.interactingUntil = Date.now() + 1200;
  try {
    await api('/api/sim/mode', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    state.interactingUntil = 0;
    toast('Emulation behavior updated.', 'ok');
    await refreshSim();
  } catch (err) {
    state.interactingUntil = 0;
    toast(err.message, 'err', 6000);
    await refreshSim();
  }
});

async function clearSimOverride(element) {
  const relay = element.dataset.clearRelay;
  const mixer = element.dataset.clearMixer !== undefined;
  const payload = mixer ? { mixer: true } : { relay };
  try {
    await api('/api/sim/override/clear', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    state.interactingUntil = 0;
    toast(
      mixer ? 'Mixer manual override cleared.' : `${RELAY_LABELS[relay] || relay} manual override cleared.`,
      'ok'
    );
  } catch (err) {
    toast(err.message, 'err', 6000);
  }
  await refreshSim();
}

root.addEventListener('click', async (event) => {
  const clear = event.target.closest('[data-clear-relay], [data-clear-mixer]');
  if (clear) {
    event.preventDefault();
    event.stopPropagation();
    await clearSimOverride(clear);
    return;
  }

  const button = event.target.closest('[data-sim-temp], [data-sim-power], [data-sim-relay], [data-sim-mixer], [data-sim-clear-overrides], [data-sim-reset], [data-sim-retry]');
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

  if (button.matches('[data-sim-clear-overrides]')) {
    button.disabled = true;
    try {
      await api('/api/sim/overrides/clear', { method: 'POST' });
      state.interactingUntil = 0;
      toast('Manual overrides cleared.', 'ok');
      await refreshSim();
    } catch (err) {
      state.interactingUntil = 0;
      toast(err.message, 'err', 6000);
      await refreshSim();
    }
    return;
  }

  if (button.matches('[data-sim-relay]')) {
    const relay = button.dataset.simRelay;
    const on = button.dataset.on !== 'true';
    button.disabled = true;
    try {
      await api('/api/sim/relay', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ relay, on })
      });
      state.interactingUntil = 0;
      toast(`${RELAY_LABELS[relay] || relay} ${on ? 'ON' : 'OFF'} (manual).`, 'ok');
      await refreshSim();
    } catch (err) {
      state.interactingUntil = 0;
      toast(err.message, 'err', 6000);
      await refreshSim();
    }
    return;
  }

  if (button.matches('[data-sim-mixer]')) {
    const action = button.dataset.simMixer;
    const motion = action !== 'stop';
    const open = action === 'open';
    button.disabled = true;
    try {
      await api('/api/sim/mixer', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ motion, open })
      });
      state.interactingUntil = 0;
      toast(`Mixer ${action} (manual).`, 'ok');
      await refreshSim();
    } catch (err) {
      state.interactingUntil = 0;
      toast(err.message, 'err', 6000);
      await refreshSim();
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
      state.interactingUntil = 0;
      toast(err.message, 'err', 6000);
      await refreshSim();
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
      toast(`${POWER_LABELS[channel] || channel} set to ${fmtNum(watts, 1)} W.`, 'ok');
      await refreshSim();
    } catch (err) {
      state.interactingUntil = 0;
      toast(err.message, 'err', 6000);
      await refreshSim();
    }
  }
});

init();
