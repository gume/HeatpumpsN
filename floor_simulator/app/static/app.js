"use strict";
/* Floor HIL Bench dashboard: control cards -> /api/control/*, live feed <- /ws. */

const $ = (s, el = document) => el.querySelector(s);
const fmt = (v, d = 1) => (v === null || v === undefined || Number.isNaN(v) ? "—" : Number(v).toFixed(d));
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

const MAX_ROWS = 43200;
let rows = [];
let rangeS = 3600;
let snap = null;
let charts = [];

const CIRCUIT_LABELS = {
  floor_heating: ["Floor heating", "pump_floor"],
  heating_loop_1: ["Radiator loop 1", "pump_rad1"],
  heating_loop_2: ["Radiator loop 2", "pump_rad2"],
};

// ------------------------------------------------------------------ API
async function post(path, body) {
  try {
    const r = await fetch(path, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body || {}) });
    if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
    return await r.json();
  } catch (e) {
    toast(e.message);
    throw e;
  }
}
function toast(msg) {
  const t = document.createElement("div");
  t.className = "toast";
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 5000);
}

// ------------------------------------------------------------------ charts
function chartDefs() {
  const dash = [6, 4];
  const relayNames = ["Heater call", "Floor pump", "Rad 1 pump", "Rad 2 pump", "DHW", "Burner", "Mixer motor"];
  const relayKeys = ["heater", "floor", "rad1", "rad2", "dhw", "burner", "mix_motion"];
  return [
    {
      el: "#c-temp", height: 280,
      series: [
        ["T boiler (primary)", "t_boiler", css("--s2")],
        ["T incoming sensor", "t_inc_sensor", css("--s4")],
        ["T forward sensor", "t_fwd_sensor", css("--s1")],
        ["Setpoint", "setpoint", css("--text-2"), { dash, width: 1.5 }],
        ["T return", "t_ret", css("--s3")],
        ["T slab", "t_slab", css("--s5")],
        ["T room", "t_room", css("--s7")],
        ["T outdoor", "t_out", css("--s6")],
        ["Controller reads fwd", "ctl_mix", css("--s8"), { dash: [2, 3], width: 1.5 }],
      ],
      unit: "°C",
    },
    {
      el: "#c-mix", height: 170, range: [0, 100],
      series: [
        ["Valve (emulated carrier)", "pos_phys", css("--s1")],
        ["Controller estimate", "pos_est", css("--s2"), { dash }],
        ["Controller target", "pos_target", css("--s7"), { stepped: true, width: 1.5 }],
      ],
      unit: "%",
    },
    {
      el: "#c-relay", height: 220, range: [0, relayKeys.length],
      series: relayKeys.map((k, i) => [relayNames[i], (r) => (r[k] ? 1 : 0) * 0.7 + (relayKeys.length - 1 - i) + 0.15,
        css(`--s${(i % 8) + 1}`), { stepped: true, raw: k }]),
      relay: relayNames,
    },
    {
      el: "#c-power", height: 150,
      series: [
        ["Floor pump", "pw_floor", css("--s1"), { stepped: true }],
        ["Rad 1 pump", "pw_rad1", css("--s2"), { stepped: true }],
        ["Rad 2 pump", "pw_rad2", css("--s3"), { stepped: true }],
        ["Mixer", "pw_mixer", css("--s4"), { stepped: true }],
      ],
      unit: "W",
    },
  ];
}

function buildCharts() {
  charts.forEach((c) => c.u.destroy());
  charts = [];
  const sync = uPlot.sync("hil");
  const axisStroke = css("--text-2");
  const grid = { stroke: css("--grid"), width: 1 };
  for (const def of chartDefs()) {
    const host = $(def.el);
    host.innerHTML = "";
    const width = Math.max(host.clientWidth, 280);
    const series = [{ value: (u, v) => (v == null ? "—" : new Date(v * 1000).toLocaleTimeString()) }];
    for (const [label, , stroke, o = {}] of def.series) {
      series.push({
        label, stroke, width: o.width || 2, dash: o.dash,
        paths: o.stepped ? uPlot.paths.stepped({ align: 1 }) : undefined,
        value: def.relay
          ? (u, v) => (v == null ? "—" : (v % 1) > 0.5 ? "on" : "off")
          : (u, v) => (v == null ? "—" : `${fmt(v, 1)} ${def.unit}`),
        spanGaps: false,
        points: { show: false },
      });
    }
    const axes = [
      { stroke: axisStroke, grid, ticks: grid },
      def.relay
        ? { stroke: axisStroke, grid: { show: false }, size: 90,
            splits: () => def.relay.map((_, i) => def.relay.length - 1 - i + 0.5),
            values: (u, splits) => splits.map((s) => def.relay[def.relay.length - 1 - Math.floor(s)]) }
        : { stroke: axisStroke, grid, ticks: grid, size: 50 },
    ];
    const opts = {
      width, height: def.height, series, axes,
      scales: { x: { time: true }, y: def.range ? { range: def.range } : { auto: true } },
      cursor: { sync: { key: sync.key }, drag: { x: true, y: false } },
      legend: { live: true },
    };
    const u = new uPlot(opts, [[], ...def.series.map(() => [])], host);
    charts.push({ u, def, host });
  }
  redraw();
}

function redraw() {
  if (!charts.length) return;
  const now = rows.length ? rows[rows.length - 1].ts : Date.now() / 1000;
  const from = now - rangeS;
  let i0 = rows.length;
  while (i0 > 0 && rows[i0 - 1].ts >= from) i0--;
  const view = rows.slice(i0);
  const xs = view.map((r) => r.ts);
  for (const { u, def } of charts) {
    const data = [xs];
    for (const [, key] of def.series) {
      data.push(view.map((r) => {
        const v = typeof key === "function" ? key(r) : r[key];
        return v === undefined ? null : v;
      }));
    }
    u.setData(data);
  }
}

function resizeCharts() {
  for (const { u, host, def } of charts) u.setSize({ width: Math.max(host.clientWidth, 280), height: def.height });
}

// ------------------------------------------------------------------ rendering
function tile(key, value, unit, sub, color) {
  return `<div class="tile"><div class="k">${color ? `<i style="background:${color}"></i>` : ""}${key}</div>
    <div class="v">${value}<small>${unit || ""}</small></div>${sub ? `<div class="d">${sub}</div>` : ""}</div>`;
}

function chip(id, state, text) {
  const el = $(id);
  el.className = `chip ${state}`;
  if (text !== undefined) el.lastChild.textContent = text;
}

function renderSnapshot(s) {
  snap = { ...(snap || {}), ...s };
  s = snap;
  const t = s.telemetry || {};
  const p = s.plant || {};
  const d = s.demands || {};
  const b = s.boiler || {};

  $("#ctl-url").textContent = s.controller_url || "";
  chip("#chip-ctl", t.ok ? "ok" : "bad");
  $("#chip-ctl").title = t.ok ? `uptime ${t.uptime}s` : t.error || "";
  if (d.transport === "mqtt") chip("#chip-mqtt", d.connected ? "ok" : "bad", `MQTT ${d.connected ? "" : "down"}`);
  else chip("#chip-mqtt", "warn", "REST intents");
  const err = t.error_code || 0;
  chip("#chip-err", err ? "bad" : "ok");
  $("#err-text").textContent = err ? `E${err} ${t.error_text || ""}` : "No error";

  // circuits
  const cl = $("#circuits");
  if (!cl.children.length) {
    for (const [c, [label]] of Object.entries(CIRCUIT_LABELS)) {
      const row = document.createElement("label");
      row.className = "toggle";
      row.innerHTML = `<span><div>${label}</div><div class="state" data-state></div></span>
        <input type="checkbox" role="switch" data-circuit="${c}" aria-label="${label}">`;
      row.querySelector("input").addEventListener("change", (e) =>
        post("/api/control/circuit", { circuit: c, on: e.target.checked }).catch(() => (e.target.checked = !e.target.checked)));
      cl.appendChild(row);
    }
  }
  for (const [c, [, pumpKey]] of Object.entries(CIRCUIT_LABELS)) {
    const inp = cl.querySelector(`[data-circuit="${c}"]`);
    if (document.activeElement !== inp) inp.checked = !!(d.demands || {})[c];
    const ctlDemand = { pump_floor: t.demand_floor, pump_rad1: t.demand_rad1, pump_rad2: t.demand_rad2 }[pumpKey];
    inp.closest(".toggle").querySelector("[data-state]").innerHTML =
      `controller demand <b>${ctlDemand ? "on" : "off"}</b> · pump <b>${t[pumpKey] ? "running" : "off"}</b>`;
  }
  $("#ka-pause").checked = !!d.keepalive_paused;
  $("#ka-info").textContent = d.keepalive_paused ? "(paused — demands will time out at 600 s)"
    : `(next in ${Math.round(d.next_keepalive_s || 0)} s)`;

  // setpoint
  if (document.activeElement !== $("#sp")) { $("#sp").value = d.setpoint; $("#sp-out").textContent = `${fmt(d.setpoint)} °C`; }
  $("#sp-ctl").textContent = s.controller_setpoint != null ? `${fmt(s.controller_setpoint)} °C` : "—";

  // boiler
  for (const btn of $("#boiler-mode").children) btn.classList.toggle("on", btn.dataset.mode === b.mode);
  $("#static-row").style.display = b.mode === "static" ? "" : "none";
  if (document.activeElement !== $("#static-temp")) $("#static-temp").value = b.static_temp;
  const bo = s.boiler_out || {};
  $("#boiler-info").textContent = b.mode === "off" ? "Burner forced off" :
    `${bo.burner_pct > 0 ? `Burner ${fmt(bo.burner_pct, 0)} %` : "Burner off"} · heat call ${t.heater ? "on" : "off"}` +
    (b.mode === "observed" && b.burner_on ? ` · target ${fmt(b.fire_target)} °C, ${Math.round(b.phase_left_s / 60)} min left` : "");
  $("#dhw-info").textContent = b.dhw_on
    ? (b.dhw_left_s != null ? `Charging — ${Math.round(b.dhw_left_s)} s left` : "Charging — until stopped")
    : "Idle";

  // outdoor
  const o = s.outdoor || {};
  for (const [id, k] of [["#out-mean", "mean"], ["#out-amp", "amplitude"], ["#out-peak", "peak_hour"]]) {
    if (document.activeElement !== $(id) && !$(id).dataset.dirty) $(id).value = o[k];
  }
  $("#out-now").textContent = `${fmt(o.now)} °C`;

  // presets
  if (s.presets && !$("#presets").children.length) {
    for (const [k, label] of Object.entries(s.presets)) {
      const btn = document.createElement("button");
      btn.textContent = label;
      btn.dataset.preset = k;
      btn.addEventListener("click", () => post("/api/control/preset", { name: k }).then(() => { snap.preset = k; renderSnapshot({}); }));
      $("#presets").appendChild(btn);
    }
  }
  for (const btn of $("#presets").children) btn.classList.toggle("on", btn.dataset.preset === s.preset);
  if (s.injector) $("#slew").textContent = s.injector.max_rate;

  // tiles
  const inj = (s.injector || {}).value || {};
  $("#tiles").innerHTML = [
    tile("T forward", fmt(p.t_fwd_sensor), "°C", `injected ${fmt(inj.mix)} · SP ${fmt(d.setpoint)}`, css("--s1")),
    tile("T incoming", fmt(p.t_inc_sensor), "°C", `injected ${fmt(inj.boiler)}`, css("--s4")),
    tile("T return", fmt(p.t_ret), "°C", `injected ${fmt(inj.return)}`, css("--s3")),
    tile("T boiler", fmt(b.tb), "°C", b.dhw_on ? "DHW" : (s.boiler_out || {}).burner_pct > 0 ? "firing" : "idle", css("--s2")),
    tile("T slab", fmt(p.t_slab, 2), "°C", "", css("--s5")),
    tile("T room", fmt(p.t_room, 2), "°C", `outdoor ${fmt(o.now)}`, css("--s7")),
    tile("Valve", fmt(t.pos_phys ?? t.pos_est_dr, 1), "%", `est ${fmt(t.pos_est_dr)} → ${fmt(t.mix_target)}`),
    tile("Integrator", fmt(t.mixer_integrator, 3), "", `HX mode ${p.hx_mode ?? "—"}`),
  ].join("");

  // wear
  const w = s.wear || {};
  $("#wear").innerHTML = [
    tile("Total travel", fmt(w.total_travel_pct, 0), "%", "Σ|Δpos|"),
    tile("Travel rate", fmt(w.travel_rate_pct_h, 0), "%/h"),
    tile("Moves", w.moves ?? "—", "", `${fmt(w.reversals_per_h, 1)} reversals/h`),
    tile("Reversals", w.reversals ?? "—", ""),
    tile("T fwd RMSE", fmt(w.tfwd_rmse_1h, 2), "K", `rolling 1 h, ${w.rmse_samples || 0} samples`),
    tile("Relay cycles", Object.values(w.relay_cycles || {}).reduce((a, b) => a + b, 0), "", "this window"),
  ].join("");
  if (s.counters && s.counters.counters) {
    $("#counters").innerHTML = s.counters.counters.map((c) =>
      `<span>${c.label || c.id}: <b>${c.cycles}</b>${c.runtimeHours != null ? ` · ${fmt(c.runtimeHours, 2)} h` : ""}</span>`).join("");
  }
  if (s.events) renderEvents(s.events);
  if (s.session) renderSession(s.session, s.preflight || {});
}

function renderEvents(evs) {
  const tb = $("#events tbody");
  tb.innerHTML = evs.slice(-30).reverse().map((e) => `<tr>
    <td>${new Date(e.timestamp_end * 1000).toLocaleTimeString()}</td><td>${e.direction}</td>
    <td>${fmt(e.duration_s, 0)}</td><td>${fmt(e.pos_before_pct)}</td><td>${fmt(e.pos_after_pct)}</td>
    <td>${e.delta_pos_pct > 0 ? "+" : ""}${fmt(e.delta_pos_pct)}</td><td>${fmt(e.t_fwd)}</td><td>${fmt(e.setpoint)}</td>
    <td>${[e.p1_active && "R1", e.p2_active && "R2"].filter(Boolean).join(" ") || "—"}</td></tr>`).join("")
    || `<tr><td colspan="9" style="text-align:center;color:var(--muted)">No mixer moves yet</td></tr>`;
}

function renderSession(m, pf) {
  const items = [
    ["Session", m.session_id], ["Git commit", m.git_commit], ["Parameters SHA-256", m.plant_parameters_sha256],
    ["Firmware", `${pf.version || "?"} (${pf.build || "?"})`], ["Tuning hash", pf.tuning_sha256 || "—"],
    ["MQTT", `${m.mqtt_broker || "none (REST intents)"} · ${m.mqtt_base}`],
    ["Started", new Date(m.started * 1000).toLocaleString()],
  ];
  $("#session").innerHTML = items.map(([k, v]) => `<dt>${k}</dt><dd>${v ?? "—"}</dd>`).join("");
}

// ------------------------------------------------------------------ wiring
function wire() {
  $("#ka-pause").addEventListener("change", (e) => post("/api/control/keepalive", { paused: e.target.checked }));
  const sp = $("#sp");
  sp.addEventListener("input", () => ($("#sp-out").textContent = `${fmt(sp.value)} °C`));
  sp.addEventListener("change", () => post("/api/control/setpoint", { value: Number(sp.value) }));
  for (const btn of $("#boiler-mode").children) {
    btn.addEventListener("click", () => post("/api/control/boiler", { mode: btn.dataset.mode, static_temp: Number($("#static-temp").value) })
      .then((st) => renderSnapshot({ boiler: st })));
  }
  $("#static-temp").addEventListener("change", (e) =>
    post("/api/control/boiler", { mode: "static", static_temp: Number(e.target.value) }).then((st) => renderSnapshot({ boiler: st })));
  $("#dhw-start").addEventListener("click", () => {
    const v = $("#dhw-dur").value;
    const body = v === "manual" ? { action: "on" } : { action: "timed", duration_s: Number(v) };
    post("/api/control/dhw", body).then((st) => renderSnapshot({ boiler: st }));
  });
  $("#dhw-stop").addEventListener("click", () => post("/api/control/dhw", { action: "off" }).then((st) => renderSnapshot({ boiler: st })));
  for (const id of ["#out-mean", "#out-amp", "#out-peak"]) $(id).addEventListener("input", () => ($(id).dataset.dirty = "1"));
  $("#out-apply").addEventListener("click", () => post("/api/control/outdoor", {
    mean: Number($("#out-mean").value), amplitude: Number($("#out-amp").value), peak_hour: Number($("#out-peak").value),
  }).then(() => { for (const id of ["#out-mean", "#out-amp", "#out-peak"]) delete $(id).dataset.dirty; }));
  $("#wear-reset").addEventListener("click", () => post("/api/control/wear/reset").then((w) => renderSnapshot({ wear: w })));
  for (const btn of $("#range").children) {
    btn.addEventListener("click", () => {
      rangeS = Number(btn.dataset.s);
      for (const b of $("#range").children) b.classList.toggle("on", b === btn);
      try { localStorage.setItem("hil.range", String(rangeS)); } catch (_) { /* storage blocked */ }
      redraw();
    });
  }
  try { rangeS = Number(localStorage.getItem("hil.range")) || rangeS; } catch (_) { /* storage blocked */ }
  for (const b of $("#range").children) b.classList.toggle("on", Number(b.dataset.s) === rangeS);
  new ResizeObserver(() => resizeCharts()).observe($(".charts-card"));
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { buildCharts(); renderSnapshot({}); });
}

function connect() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  ws.onopen = () => chip("#chip-ws", "ok");
  ws.onclose = () => { chip("#chip-ws", "bad"); setTimeout(connect, 2000); };
  ws.onmessage = (m) => {
    const msg = JSON.parse(m.data);
    if (msg.type === "tick") {
      rows.push(msg.row);
      if (rows.length > MAX_ROWS) rows.splice(0, rows.length - MAX_ROWS);
      renderSnapshot(msg.snapshot);
      redraw();
    } else if (msg.type === "snapshot") {
      renderSnapshot(msg.snapshot);
    } else if (msg.type === "mixer_event") {
      snap.events = [...(snap.events || []), msg.event].slice(-50);
      renderEvents(snap.events);
    }
  };
}

async function init() {
  wire();
  buildCharts();
  try {
    rows = await (await fetch(`/api/history?seconds=${12 * 3600}`)).json();
    renderSnapshot(await (await fetch("/api/snapshot")).json());
  } catch (e) { toast(`initial load failed: ${e.message}`); }
  redraw();
  connect();
}
init();
