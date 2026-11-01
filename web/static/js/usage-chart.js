/**
 * PrintGuard Usage Chart — printed pages per day/hour/week, split into color / black-white.
 *
 * Shared by the main dashboard ("overview") and every printer detail panel.
 *   - Range filter: presets + free from/to (Tehran time), bucket chosen by the server.
 *   - Chart type: bar (stacked) or line.   View: split (color + B/W) or total.
 *   - Colors come from CSS variables (--ug-*), so dark/light themes stay in sync with the markup.
 *
 * Public API (window.PrintGuardUsageChart):
 *   mountAll(container)         mount every unmounted `.ug-root` below container
 *   setPrinters(key, printers)  fill the printer select of a root created with data-ug-select="1"
 *   reload(key)                 force a data reload
 *   dispose(key)                free chart + timers of a root
 */
(function (global) {
  "use strict";

  const API_BASE = () => (typeof global.API === "string" ? global.API : location.protocol + "//" + location.host);
  const TZ = "Asia/Tehran";
  const REFRESH_MS = 60000;
  const PREF_KEY = "pg.usage.prefs";

  const PRESETS = [
    { id: "h24", label: "۲۴ ساعت", query: "hours=24", hours: 24 },
    { id: "d7", label: "۷ روز", query: "days=7", days: 7 },
    { id: "d30", label: "۳۰ روز", query: "days=30", days: 30 },
    { id: "d90", label: "۹۰ روز", query: "days=90", days: 90 },
    { id: "d180", label: "۶ ماه", query: "days=180", days: 180 },
    { id: "d365", label: "۱ سال", query: "days=365", days: 365 },
  ];
  const BUCKET_TEXT = {
    hour: { step: "ساعتی", per: "ساعت" },
    day: { step: "روزانه", per: "روز" },
    week: { step: "هفتگی", per: "هفته" },
  };

  const registry = Object.create(null); // key -> instance
  const nf = new Intl.NumberFormat("fa-IR");
  const fmtInt = (v) => nf.format(Math.round(Number(v) || 0));
  const fmtPct = (v) => new Intl.NumberFormat("fa-IR", { maximumFractionDigits: 1 }).format(Number(v) || 0) + "٪";

  // Labels from the server are Tehran wall-clock strings, so they are formatted as UTC (no browser-tz shift).
  const dtf = (opts) => new Intl.DateTimeFormat("fa-IR", Object.assign({ timeZone: "UTC", hourCycle: "h23" }, opts));
  const F = {
    day: dtf({ month: "short", day: "numeric" }),
    dayFull: dtf({ weekday: "long", year: "numeric", month: "long", day: "numeric" }),
    hour: dtf({ hour: "2-digit", minute: "2-digit" }),
    dayHour: dtf({ month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }),
    tehranDate: new Intl.DateTimeFormat("fa-IR", { timeZone: TZ, year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }),
  };
  const labelToDate = (label) => new Date(label.length > 10 ? label + ":00Z" : label + "T00:00:00Z");

  // ── storage (preferences only; never required) ────────────────────────────
  function loadPrefs() {
    try { return JSON.parse(localStorage.getItem(PREF_KEY) || "{}") || {}; } catch (e) { return {}; }
  }
  function savePrefs(state) {
    try {
      localStorage.setItem(PREF_KEY, JSON.stringify({ type: state.type, view: state.view, preset: state.preset }));
    } catch (e) { /* storage unavailable */ }
  }

  // ── helpers ───────────────────────────────────────────────────────────────
  function tehranInputValue(date) {
    const p = new Intl.DateTimeFormat("en-CA", {
      timeZone: TZ, hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
    }).formatToParts(date).reduce((a, x) => { a[x.type] = x.value; return a; }, {});
    return `${p.year}-${p.month}-${p.day}T${p.hour}:${p.minute}`;
  }

  function cssVar(name, fallback) {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  }

  function palette() {
    return {
      colorA: cssVar("--ug-color-a", "#00d4ff"),
      colorB: cssVar("--ug-color-b", "#e879f9"),
      bwA: cssVar("--ug-bw-a", "#f8fafc"),
      bwB: cssVar("--ug-bw-b", "#94a3b8"),
      unk: cssVar("--ug-unk", "#64748b"),
      totA: cssVar("--ug-total-a", "#00d4ff"),
      totB: cssVar("--ug-total-b", "#448aff"),
      text: cssVar("--text", "#e4e8f0"),
      text3: cssVar("--text3", "#9aa5bd"),
      grid: cssVar("--chart-grid", "rgba(120,130,160,.18)"),
      bg: cssVar("--bg2", "#151720"),
    };
  }

  const vGrad = (top, bottom) => ({
    type: "linear", x: 0, y: 0, x2: 0, y2: 1,
    colorStops: [{ offset: 0, color: top }, { offset: 1, color: bottom }],
  });
  const withAlpha = (hex, a) => {
    const m = /^#([0-9a-f]{6})$/i.exec(hex);
    if (!m) return hex;
    const n = parseInt(m[1], 16);
    return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
  };

  function esc(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  // ── markup ────────────────────────────────────────────────────────────────
  function toolbarHtml(inst) {
    const st = inst.state;
    const presetBtns = PRESETS.map((p) =>
      `<button type="button" class="ug-btn${st.mode === "preset" && st.preset === p.id ? " active" : ""}" data-ug="preset" data-id="${p.id}">${p.label}</button>`
    ).join("");
    const printerSel = inst.withSelect
      ? `<select class="form-input ug-printer" data-ug="printer" aria-label="انتخاب پرینتر"><option value="">همه پرینترها</option></select>`
      : "";
    return `
      <div class="ug-bar">
        ${printerSel}
        <div class="ug-seg" role="group" aria-label="نوع نمودار">
          <button type="button" class="ug-btn${st.type === "bar" ? " active" : ""}" data-ug="type" data-id="bar" title="نمودار میله‌ای">▮ میله‌ای</button>
          <button type="button" class="ug-btn${st.type === "line" ? " active" : ""}" data-ug="type" data-id="line" title="نمودار خطی">〰 خطی</button>
        </div>
        <div class="ug-seg" role="group" aria-label="نحوه نمایش">
          <button type="button" class="ug-btn${st.view === "split" ? " active" : ""}" data-ug="view" data-id="split" title="تفکیک رنگی و سیاه‌سفید">◐ رنگی / سیاه‌سفید</button>
          <button type="button" class="ug-btn${st.view === "total" ? " active" : ""}" data-ug="view" data-id="total" title="مجموع صفحات">Σ مجموع</button>
        </div>
      </div>
      <div class="ug-bar">
        <div class="ug-seg ug-presets" role="group" aria-label="بازه زمانی">${presetBtns}</div>
        <div class="ug-custom">
          <span class="ug-lbl">از</span>
          <input type="datetime-local" class="form-input ug-from" value="${esc(st.start)}">
          <span class="ug-lbl">تا</span>
          <input type="datetime-local" class="form-input ug-to" value="${esc(st.end)}">
          <button type="button" class="btn btn-cyan ug-apply" data-ug="apply">اعمال بازه</button>
          <span class="ug-lbl ug-tz">(به وقت تهران)</span>
        </div>
      </div>
      <div class="ug-sub" aria-live="polite"></div>
      <div class="ug-kpis"></div>
      <div class="ug-ratio" aria-hidden="true"></div>
      <div class="ug-stage">
        <div class="ug-chart" role="img" aria-label="نمودار مصرف چاپ"></div>
        <div class="ug-empty" hidden></div>
      </div>`;
  }

  // ── instance lifecycle ────────────────────────────────────────────────────
  function createInstance(root) {
    const key = root.dataset.ugKey;
    const prefs = loadPrefs();
    const preset = PRESETS.some((p) => p.id === prefs.preset) ? prefs.preset : "d30";
    const inst = {
      key, root,
      fixedIp: root.dataset.ugIp || "",
      withSelect: root.dataset.ugSelect === "1",
      state: {
        type: prefs.type === "line" ? "line" : "bar",
        view: prefs.view === "total" ? "total" : "split",
        mode: "preset", preset, start: "", end: "", ip: root.dataset.ugIp || "", zoomed: false,
      },
      data: null, token: 0, loaded: false, timer: null, io: null, printers: [], printersSig: "",
    };
    fillInputsFromPreset(inst);
    return inst;
  }

  function fillInputsFromPreset(inst) {
    const p = PRESETS.find((x) => x.id === inst.state.preset) || PRESETS[2];
    const now = new Date();
    let start;
    if (p.days) {
      // whole local days incl. today -> local midnight of the first day
      const todayTehran = tehranInputValue(now).slice(0, 10);
      const d = new Date(todayTehran + "T00:00:00Z");
      d.setUTCDate(d.getUTCDate() - (p.days - 1));
      start = d.toISOString().slice(0, 10) + "T00:00";
    } else {
      start = tehranInputValue(new Date(now.getTime() - p.hours * 3600e3));
    }
    inst.state.start = start;
    inst.state.end = tehranInputValue(now);
  }

  function mount(root) {
    if (!root || root.dataset.ugMounted) return;
    const key = root.dataset.ugKey;
    if (!key) return;
    if (registry[key] && registry[key].root !== root) dispose(key);
    const inst = createInstance(root);
    registry[key] = inst;
    root.dataset.ugMounted = "1";
    root.classList.add("ug-ready");
    root.innerHTML = toolbarHtml(inst);

    root.addEventListener("click", (e) => onClick(inst, e));
    root.addEventListener("change", (e) => {
      const t = e.target;
      if (t && t.dataset && t.dataset.ug === "printer") {
        inst.state.ip = t.value;
        inst.state.zoomed = false;
        load(inst, { silent: false });
      }
    });

    // Lazy load: only when the root is actually visible (hidden tab panels do not hit the API).
    if (typeof IntersectionObserver !== "undefined") {
      inst.io = new IntersectionObserver((entries) => {
        if (entries.some((en) => en.isIntersecting) && !inst.loaded) load(inst, { silent: false });
      });
      inst.io.observe(root);
    } else {
      load(inst, { silent: false });
    }
    inst.timer = setInterval(() => tick(inst), REFRESH_MS);
  }

  function mountAll(container) {
    (container || document).querySelectorAll(".ug-root:not([data-ug-mounted])").forEach(mount);
  }

  function dispose(key) {
    const inst = registry[key];
    if (!inst) return;
    clearInterval(inst.timer);
    if (inst.io) inst.io.disconnect();
    const el = inst.root.querySelector(".ug-chart");
    const chart = el && global.echarts ? global.echarts.getInstanceByDom(el) : null;
    if (chart) (global.PrintGuardECharts ? global.PrintGuardECharts.dispose(chart) : chart.dispose());
    delete registry[key];
  }

  function tick(inst) {
    if (!document.body.contains(inst.root)) { dispose(inst.key); return; }
    if (inst.root.offsetParent === null) return;          // hidden tab
    if (inst.state.mode !== "preset" || inst.state.zoomed) return;
    load(inst, { silent: true });
  }

  // ── events ────────────────────────────────────────────────────────────────
  function onClick(inst, e) {
    const btn = e.target.closest("[data-ug]");
    if (!btn || !inst.root.contains(btn)) return;
    const st = inst.state;
    const action = btn.dataset.ug;
    if (action === "type") {
      st.type = btn.dataset.id; markActive(inst, "type", st.type); savePrefs(st); render(inst, true);
    } else if (action === "view") {
      st.view = btn.dataset.id; markActive(inst, "view", st.view); savePrefs(st); render(inst, true);
    } else if (action === "preset") {
      st.mode = "preset"; st.preset = btn.dataset.id; st.zoomed = false;
      fillInputsFromPreset(inst);
      inst.root.querySelector(".ug-from").value = st.start;
      inst.root.querySelector(".ug-to").value = st.end;
      markActive(inst, "preset", st.preset); savePrefs(st); load(inst, { silent: false });
    } else if (action === "apply") {
      applyCustom(inst);
    }
  }

  function markActive(inst, group, id) {
    inst.root.querySelectorAll(`[data-ug="${group}"]`).forEach((b) => b.classList.toggle("active", b.dataset.id === id));
  }

  function setSub(inst, text, warn) {
    const el = inst.root.querySelector(".ug-sub");
    el.textContent = text;
    el.classList.toggle("ug-warn", !!warn);
  }

  function applyCustom(inst) {
    const from = inst.root.querySelector(".ug-from").value;
    const to = inst.root.querySelector(".ug-to").value;
    if (!from || !to) return setSub(inst, "⚠ هر دو تاریخ «از» و «تا» را انتخاب کنید.", true);
    if (from >= to) return setSub(inst, "⚠ زمان شروع باید قبل از زمان پایان باشد.", true);
    if ((new Date(to) - new Date(from)) / 86400e3 > 366) return setSub(inst, "⚠ حداکثر بازه ۳۶۶ روز است.", true);
    const st = inst.state;
    st.mode = "custom"; st.start = from; st.end = to; st.zoomed = false;
    inst.root.querySelectorAll('[data-ug="preset"]').forEach((b) => b.classList.remove("active"));
    load(inst, { silent: false });
  }

  // ── data ──────────────────────────────────────────────────────────────────
  function buildUrl(inst) {
    const st = inst.state;
    const parts = [];
    if (st.ip) parts.push("ip=" + encodeURIComponent(st.ip));
    if (st.mode === "custom") {
      parts.push("start=" + encodeURIComponent(st.start), "end=" + encodeURIComponent(st.end));
    } else {
      parts.push((PRESETS.find((p) => p.id === st.preset) || PRESETS[2]).query);
    }
    return `${API_BASE()}/api/stats/usage/series?${parts.join("&")}`;
  }

  async function load(inst, { silent }) {
    const token = ++inst.token;
    inst.loaded = true;
    if (!silent) inst.root.classList.add("ug-loading");
    let data;
    try {
      const res = await fetch(buildUrl(inst), { cache: "no-store" });
      if (!res.ok) {
        let msg = `HTTP ${res.status}`;
        try { const j = await res.json(); if (j && j.error) msg = j.error; } catch (e) { /* ignore */ }
        throw new Error(msg);
      }
      data = await res.json();
    } catch (err) {
      if (token !== inst.token) return;
      inst.root.classList.remove("ug-loading");
      if (!silent) showEmpty(inst, `خطا در دریافت داده: ${err.message}`);
      return;
    }
    if (token !== inst.token || !document.body.contains(inst.root)) return;
    inst.root.classList.remove("ug-loading");
    const sig = JSON.stringify([data.labels, data.color, data.bw, data.unknown]);
    if (silent && inst.data && inst.dataSig === sig) return;      // nothing changed
    inst.data = data;
    inst.dataSig = sig;
    render(inst, !silent);
  }

  // ── render ────────────────────────────────────────────────────────────────
  function showEmpty(inst, text) {
    const empty = inst.root.querySelector(".ug-empty");
    const chartEl = inst.root.querySelector(".ug-chart");
    empty.textContent = text;
    empty.hidden = false;
    chartEl.style.visibility = "hidden";
  }

  function hideEmpty(inst) {
    inst.root.querySelector(".ug-empty").hidden = true;
    inst.root.querySelector(".ug-chart").style.visibility = "visible";
  }

  function renderSummary(inst, data) {
    const s = data.summary || {};
    const b = BUCKET_TEXT[data.bucket] || BUCKET_TEXT.day;
    const startTxt = F.tehranDate.format(new Date(data.start));
    const endTxt = F.tehranDate.format(new Date(data.end));
    const scope = inst.fixedIp ? "" : (inst.state.ip ? `پرینتر ${inst.state.ip} · ` : "همه پرینترها · ");
    setSub(inst, `${scope}گام نمایش: ${b.step} · از ${startTxt} تا ${endTxt}`, false);

    const peak = s.peak
      ? `${fmtInt(s.peak.value)} <small>(${labelText(s.peak.label, data.bucket)})</small>`
      : "—";
    const kpi = (cls, label, value, extra) =>
      `<div class="ug-kpi ${cls}"><span class="ug-kpi-label">${label}</span><span class="ug-kpi-value">${value}</span>${extra ? `<span class="ug-kpi-extra">${extra}</span>` : ""}</div>`;
    inst.root.querySelector(".ug-kpis").innerHTML =
      kpi("total", "مجموع صفحات", fmtInt(s.total), "") +
      kpi("color", "رنگی", fmtInt(s.color), fmtPct(s.color_pct)) +
      kpi("bw", "سیاه‌سفید", fmtInt(s.bw), fmtPct(s.bw_pct)) +
      (s.unknown ? kpi("unk", "نامشخص", fmtInt(s.unknown), fmtPct(100 - (s.color_pct || 0) - (s.bw_pct || 0))) : "") +
      kpi("avg", `میانگین هر ${b.per}`, fmtInt(s.avg_per_bucket), "") +
      kpi("peak", "بیشترین", peak, "");

    const tot = s.total || 0;
    const w = (v) => (tot ? ((v * 100) / tot).toFixed(2) : 0);
    inst.root.querySelector(".ug-ratio").innerHTML = tot
      ? `<span class="seg color" style="width:${w(s.color)}%"></span><span class="seg bw" style="width:${w(s.bw)}%"></span><span class="seg unk" style="width:${w(s.unknown)}%"></span>`
      : "";
  }

  function labelText(label, bucket) {
    const d = labelToDate(label);
    if (bucket === "hour") return F.dayHour.format(d);
    if (bucket === "week") return "هفتهٔ " + F.day.format(d);
    return F.day.format(d);
  }

  function tooltipTitle(label, bucket) {
    const d = labelToDate(label);
    if (bucket === "hour") {
      const end = new Date(d.getTime() + 3600e3);
      return `${F.dayHour.format(d)} تا ${F.hour.format(end)}`;
    }
    if (bucket === "week") {
      const end = new Date(d.getTime() + 6 * 86400e3);
      return `هفته: ${F.day.format(d)} تا ${F.day.format(end)}`;
    }
    return F.dayFull.format(d);
  }

  function axisFormatter(data) {
    const n = data.labels.length;
    return (value) => {
      const d = labelToDate(value);
      if (data.bucket === "hour") return n > 24 ? F.dayHour.format(d) : F.hour.format(d);
      return F.day.format(d);
    };
  }

  function buildOption(inst, data) {
    const st = inst.state;
    const c = palette();
    const isBar = st.type === "bar";
    const split = st.view === "split";
    const n = data.labels.length;
    const hasUnknown = (data.unknown || []).some((v) => v > 0);
    const markerColor = { color: c.colorB, bw: c.bwB, unknown: c.unk, total: c.totA };

    const mk = (name, values, kind) => {
      const base = { name, type: isBar ? "bar" : "line", data: values };
      let fill;
      if (kind === "color") fill = vGrad(c.colorB, c.colorA);
      else if (kind === "bw") fill = vGrad(c.bwA, c.bwB);
      else if (kind === "total") fill = vGrad(c.totB, c.totA);
      else fill = c.unk;
      const solid = markerColor[kind];
      if (isBar) {
        return Object.assign(base, {
          stack: split ? "pages" : undefined,
          barMaxWidth: 30,
          itemStyle: Object.assign({ color: fill, borderRadius: split ? 0 : [4, 4, 0, 0] },
            kind === "unknown" ? { decal: { symbol: "rect", dashArrayX: [1, 0], dashArrayY: [3, 3], rotation: Math.PI / 4, color: "rgba(255,255,255,.35)" } } : {}),
          emphasis: { focus: "series" },
        });
      }
      return Object.assign(base, {
        smooth: 0.2,
        showSymbol: n <= 45,
        symbolSize: 6,
        lineStyle: { width: 2.4, color: kind === "unknown" ? c.unk : solid },
        itemStyle: { color: solid },
        areaStyle: kind === "unknown" ? undefined : { color: vGrad(withAlpha(solid.length === 7 ? solid : "#00d4ff", 0.32), withAlpha(solid.length === 7 ? solid : "#00d4ff", 0.02)) },
        emphasis: { focus: "series" },
      });
    };

    let series;
    if (split) {
      series = [mk("رنگی", data.color, "color"), mk("سیاه‌سفید", data.bw, "bw")];
      if (hasUnknown) series.push(mk("نامشخص", data.unknown, "unknown"));
      if (!isBar) {
        series.push({
          name: "مجموع", type: "line", data: data.total, smooth: 0.2, showSymbol: false, silent: true,
          lineStyle: { width: 1.4, type: "dashed", color: c.text3, opacity: 0.8 }, itemStyle: { color: c.text3 }, z: 1,
        });
      }
      // round only the top of the stack: unknown (if any) else B/W
      if (isBar) series[series.length - 1].itemStyle.borderRadius = [4, 4, 0, 0];
    } else {
      series = [mk("مجموع صفحات", data.total, "total")];
    }

    const tipTitle = (label) => tooltipTitle(label, data.bucket);
    return {
      animationDuration: 500,
      animationEasing: "cubicOut",
      grid: { top: 48, left: 12, right: 16, bottom: n > 31 ? 72 : 36, containLabel: true },
      legend: { top: 4, icon: "roundRect", itemWidth: 14, itemHeight: 8, textStyle: { color: c.text, fontSize: 12 }, inactiveColor: c.text3 },
      toolbox: { right: 6, top: 0, feature: { saveAsImage: { name: "print-usage", pixelRatio: 2, backgroundColor: c.bg, title: "ذخیره تصویر" } } },
      tooltip: {
        trigger: "axis",
        axisPointer: { type: isBar ? "shadow" : "line" },
        formatter: (params) => {
          if (!params || !params.length) return "";
          const i = params[0].dataIndex;
          const total = data.total[i] || 0;
          const pct = (v) => (total ? ` <span style="opacity:.65">(${fmtPct((v * 100) / total)})</span>` : "");
          const row = (dot, name, v, withPct) =>
            `<div style="display:flex;justify-content:space-between;gap:18px"><span><i style="display:inline-block;width:9px;height:9px;border-radius:50%;background:${dot};margin-left:6px"></i>${name}</span><b>${fmtInt(v)}${withPct ? pct(v) : ""}</b></div>`;
          let html = `<div style="margin-bottom:4px;font-weight:700">${tipTitle(data.labels[i])}</div>`;
          if (split) {
            html += row(markerColor.color, "رنگی", data.color[i], true);
            html += row(markerColor.bw, "سیاه‌سفید", data.bw[i], true);
            if (hasUnknown) html += row(markerColor.unknown, "نامشخص", data.unknown[i], true);
            html += `<div style="border-top:1px solid rgba(128,128,128,.35);margin-top:4px;padding-top:4px">${row("transparent", "مجموع", total, false)}</div>`;
          } else {
            html += row(markerColor.total, "مجموع صفحات", total, false);
          }
          return html;
        },
      },
      xAxis: {
        type: "category",
        data: data.labels,
        boundaryGap: isBar,
        axisLabel: { formatter: axisFormatter(data), hideOverlap: true, color: c.text3, margin: 10 },
        axisTick: { alignWithLabel: true },
      },
      yAxis: {
        type: "value",
        min: 0,
        minInterval: 1,
        axisLabel: { formatter: (v) => fmtInt(v), color: c.text3 },
        splitLine: { lineStyle: { color: c.grid, type: "dashed" } },
      },
      dataZoom: [{ type: "inside", filterMode: "none" }].concat(
        n > 31 ? [{ type: "slider", height: 20, bottom: 12, filterMode: "none" }] : []
      ),
      series,
    };
  }

  function ensureChart(inst) {
    const el = inst.root.querySelector(".ug-chart");
    if (!global.echarts) return null;
    let chart = global.echarts.getInstanceByDom(el);
    if (!chart || chart.isDisposed()) {
      chart = global.PrintGuardECharts ? global.PrintGuardECharts.init(el) : global.echarts.init(el);
    }
    // the theme refresh in echarts-wrapper re-creates the instance, so bind per instance
    if (!chart.__ugBound) {
      chart.__ugBound = true;
      chart.on("datazoom", () => {
        const z = (chart.getOption().dataZoom || [])[0] || {};
        inst.state.zoomed = z.start > 0.01 || z.end < 99.99;
      });
    }
    return chart;
  }

  function render(inst, rebuild) {
    const data = inst.data;
    if (!data) return;
    if (!global.echarts) return showEmpty(inst, "کتابخانهٔ نمودار (ECharts) بارگذاری نشده است");
    renderSummary(inst, data);
    const total = (data.summary && data.summary.total) || 0;
    if (!data.labels.length || total === 0) {
      const el = inst.root.querySelector(".ug-chart");
      const old = global.echarts.getInstanceByDom(el);
      if (old) old.clear();
      return showEmpty(inst, "در این بازه، چاپی ثبت نشده است");
    }
    hideEmpty(inst);
    const chart = ensureChart(inst);
    if (!chart) return;
    chart.resize();
    chart.setOption(buildOption(inst, data), { notMerge: !!rebuild });
  }

  // Re-render with the new palette whenever the theme class on <html> changes.
  new MutationObserver((mutations) => {
    if (!mutations.some((m) => m.attributeName === "class")) return;
    requestAnimationFrame(() => requestAnimationFrame(() => {
      Object.keys(registry).forEach((k) => {
        const inst = registry[k];
        if (inst && inst.data && document.body.contains(inst.root)) render(inst, true);
      });
    }));
  }).observe(document.documentElement, { attributes: true });

  // ── public ────────────────────────────────────────────────────────────────
  function setPrinters(key, printers) {
    const inst = registry[key];
    if (!inst || !inst.withSelect) return;
    const list = (printers || []).filter((p) => p && p.ip && p.device_type !== "sensor");
    const sig = list.map((p) => `${p.ip}|${p.name || ""}|${p.nickname || ""}`).join(",");
    if (sig === inst.printersSig) return;
    inst.printersSig = sig;
    const sel = inst.root.querySelector(".ug-printer");
    if (!sel) return;
    const current = inst.state.ip;
    sel.innerHTML = "";
    const all = document.createElement("option");
    all.value = ""; all.textContent = "همه پرینترها";
    sel.appendChild(all);
    list.forEach((p) => {
      const o = document.createElement("option");
      o.value = p.ip;
      o.textContent = `${p.nickname || p.name || p.ip} (${p.ip})`;
      sel.appendChild(o);
    });
    if (current && list.some((p) => p.ip === current)) sel.value = current;
    else { sel.value = ""; inst.state.ip = ""; }
  }

  function reload(key) {
    const inst = registry[key];
    if (inst) load(inst, { silent: false });
  }

  global.PrintGuardUsageChart = { mountAll, setPrinters, reload, dispose, _registry: registry };

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", () => mountAll(document));
  else mountAll(document);
})(window);
