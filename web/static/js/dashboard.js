// ══════════════════════════════════════════════════
// THEME TOGGLE (Light / Dark)
// ══════════════════════════════════════════════════
(function initTheme() {
  const saved = localStorage.getItem('dashboard-theme');
  if (saved === 'light') {
    document.documentElement.classList.add('light');
  }
})();

function toggleTheme() {
  const html = document.documentElement;
  const isLight = html.classList.toggle('light');
  localStorage.setItem('dashboard-theme', isLight ? 'light' : 'dark');
  updateThemeLabel();
  
  try {
    if (typeof Chart !== 'undefined') {
      Object.values(Chart.instances || {}).forEach(chart => {
        const gridColor = isLight ? 'rgba(183,201,245,0.5)' : 'rgba(42,50,72,0.5)';
        const textColor = isLight ? '#ffffff' : '#7b86a0';
        
        if (chart.options.scales?.y) {
          chart.options.scales.y.grid.color = gridColor;
          chart.options.scales.y.ticks.color = textColor;
        }
        if (chart.options.scales?.x) {
          chart.options.scales.x.ticks.color = textColor;
        }
        chart.update('none');
      });
    }
  } catch(e) {
    console.warn('Chart theme update skipped:', e);
  }
}

function updateThemeLabel() {
  const labelEl = document.getElementById('theme-label');
  const iconEl = document.getElementById('theme-icon');
  const isLight = document.documentElement.classList.contains('light');
  
  if (labelEl) {
    labelEl.textContent = isLight ? 'تم تاریک' : 'تم روشن';
  }
  if (iconEl) {
    iconEl.textContent = isLight ? '🌙' : '☀️';
  }
}

// ─── Topbar Dropdown Menu ────────────────────────
function toggleTopbarMenu() {
  const el = document.getElementById('topbar-more');
  if (el) el.classList.toggle('open');
}
function closeTopbarMenu() {
  const el = document.getElementById('topbar-more');
  if (el) el.classList.remove('open');
}

// ─── سیستم دراپ‌داون عمومی ───
function toggleDropdown(id) {
  const el = document.getElementById(id);
  if (el) {
    // بستن بقیه دراپ‌داون‌ها
    document.querySelectorAll('.dropdown').forEach(d => {
      if (d.id !== id) d.classList.remove('open');
    });
    el.classList.toggle('open');
  }
}

document.addEventListener('click', function(e) {
  // بستن منوی تاپ‌بار
  const topbarMore = document.getElementById('topbar-more');
  if (topbarMore && !topbarMore.contains(e.target)) topbarMore.classList.remove('open');
  
  // بستن دراپ‌داون‌های عمومی
  if (!e.target.closest('.dropdown')) {
    document.querySelectorAll('.dropdown').forEach(d => d.classList.remove('open'));
  }
});

// ══════════════════════════════════════════════════
// UTILITIES
// ══════════════════════════════════════════════════
function escapeHtml(str) {
  if (!str) return '';
  return str.replace(/[&<>]/g, function(m) {
    if (m === '&') return '&amp;';
    if (m === '<') return '&lt;';
    if (m === '>') return '&gt;';
    return m;
  });
}

const SENSOR_THRESHOLDS = {
  tempWarning: 30,
  tempCritical: 35,
  humLowWarning: 20,
  humHighWarning: 70,
  humHighCritical: 80
};

function sensorKindFromUnit(unit) {
  return unit === '°C' ? 'temperature' : 'humidity';
}

function latestSensorChange(ip, kind, port) {
  if (!Array.isArray(allEvents) || !ip || !kind || !port) return null;
  return allEvents
    .filter(e => e.printer_ip === ip && e.type === 'SENSOR_CHANGE' && e.sensor_kind === kind && Number(e.sensor_port) === Number(port))
    .sort((a, b) => String(b.timestamp || '').localeCompare(String(a.timestamp || '')))[0] || null;
}

function sensorValueMeta({ip, kind, port, value, status}) {
  const num = Number(value);
  let color = kind === 'temperature' ? 'var(--cyan)' : 'var(--blue)';
  let glow = kind === 'temperature' ? 'rgba(0,212,255,.35)' : 'rgba(100,181,246,.35)';
  if (status === 'invalid') {
    color = 'var(--red)';
    glow = 'rgba(255,82,82,.45)';
  } else if (Number.isFinite(num)) {
    if (kind === 'temperature') {
      if (num >= SENSOR_THRESHOLDS.tempCritical) { color = 'var(--red)'; glow = 'rgba(255,82,82,.45)'; }
      else if (num >= SENSOR_THRESHOLDS.tempWarning) { color = 'var(--yellow)'; glow = 'rgba(255,215,64,.45)'; }
      else { color = 'var(--green)'; glow = 'rgba(0,230,118,.35)'; }
    } else {
      if (num >= SENSOR_THRESHOLDS.humHighCritical) { color = 'var(--red)'; glow = 'rgba(255,82,82,.45)'; }
      else if (num >= SENSOR_THRESHOLDS.humHighWarning || num <= SENSOR_THRESHOLDS.humLowWarning) { color = 'var(--yellow)'; glow = 'rgba(255,215,64,.45)'; }
      else { color = 'var(--blue)'; glow = 'rgba(100,181,246,.35)'; }
    }
  }

  const change = latestSensorChange(ip, kind, port);
  let arrow = '';
  let trendColor = color;
  if (change && Number.isFinite(Number(change.prev_value)) && Number.isFinite(Number(change.new_value))) {
    const diff = Number(change.new_value) - Number(change.prev_value);
    if (diff > 0) {
      arrow = '▲';
      trendColor = kind === 'temperature' ? 'var(--red)' : 'var(--yellow)';
    } else if (diff < 0) {
      arrow = '▼';
      trendColor = 'var(--green)';
    }
  }
  return {color, glow, arrow, trendColor};
}

function sensorValueHtml({ip, kind, port, value, unit, status, fontSize = 16}) {
  const meta = sensorValueMeta({ip, kind, port, value, status});
  const shown = value !== null && value !== undefined ? `${value}${unit}` : '—';
  const arrow = meta.arrow ? `<span style="font-size:${Math.max(10, fontSize - 5)}px;color:${meta.trendColor};margin-right:4px">${meta.arrow}</span>` : '';
  return `<span class="sensor-value-live" style="font-weight:800;font-size:${fontSize}px;color:${meta.color};text-shadow:0 0 10px ${meta.glow}">${arrow}${shown}</span>`;
}

// ══════════════════════════════════════════════════
// GLOBAL VARIABLES
// ══════════════════════════════════════════════════
let allData     = [];
let allEvents   = [];
let countdown   = POLL_INT;
let countTimer  = null;
let isFirst     = true;
let serverInfo  = {};

const PAGE_SIZE = 20;
const _pgState  = {};

let chartInstance = null;
const printerChartInstances = {};

const USER_ROLE = document.body?.dataset?.currentUserRole || 'viewer';
const USER_VERIFIED = document.body?.dataset?.currentUserVerified === '1';
let USER_ALLOWED_MODULES = [];
try {
  USER_ALLOWED_MODULES = JSON.parse(document.body?.dataset?.currentUserModules || '[]');
  if (!Array.isArray(USER_ALLOWED_MODULES)) USER_ALLOWED_MODULES = [];
} catch (e) {
  USER_ALLOWED_MODULES = [];
}

function canAccessModule(moduleName) {
  return USER_ROLE === 'admin' || !USER_ALLOWED_MODULES.length || USER_ALLOWED_MODULES.includes(moduleName);
}

function canAdmin() {
  return USER_VERIFIED && USER_ROLE === 'admin';
}

function canManage() {
  return USER_VERIFIED && (USER_ROLE === 'admin' || USER_ROLE === 'manager') && canAccessModule('excel');
}

function canViewLogs() {
  return USER_VERIFIED && canAccessModule('logs');
}

function canEditPrinters() {
  return canAdmin() && canAccessModule('printers');
}

function canManualEvents() {
  return canAdmin() && canAccessModule('logs');
}

const OFFICE_GROUPS = [
  { id: 'imamat',   name: 'دفتر امامت',  subnet: '172.16.25', icon: '🏢', color: 'cyan'    },
  { id: 'soroush',  name: 'دفتر سروش',   subnet: '172.16.24', icon: '🏢', color: 'green'   },
  { id: 'falestin', name: 'دفتر فلسطین', subnet: '172.16.0',  icon: '🏢', color: 'yellow'  },
  { id: 'elahiye',  name: 'دفتر الهیه',  subnet: '172.16.32', icon: '🏢', color: 'magenta' },
  { id: 'other',    name: 'سایر',         subnet: null,        icon: '🖨',  color: 'orange'  },
];

const _groupOpen = {};

let sortableInstance = null;
let currentPrinters = [];
const STORAGE_KEY = 'printer_order';
const EXTRA_COUNTERS_ACCORDION_KEY = 'printer_extra_counters_open';
let swapPluginMounted = false;

function getDefaultPrinterOrder(printers) {
  const groups = {};
  for (const p of printers) {
    const parts = p.ip.split('.');
    const subnet = parts.slice(0, 3).join('.');
    if (!groups[subnet]) groups[subnet] = [];
    groups[subnet].push(p);
  }
  for (const subnet in groups) {
    groups[subnet].sort((a, b) => {
      const aParts = a.ip.split('.').map(Number);
      const bParts = b.ip.split('.').map(Number);
      for (let i = 0; i < 4; i++) {
        if (aParts[i] !== bParts[i]) return aParts[i] - bParts[i];
      }
      return 0;
    });
  }
  const sortedSubnets = Object.keys(groups).sort((a, b) => {
    const aParts = a.split('.').map(Number);
    const bParts = b.split('.').map(Number);
    for (let i = 0; i < 3; i++) {
      if (aParts[i] !== bParts[i]) return aParts[i] - bParts[i];
    }
    return 0;
  });
  const order = [];
  for (const subnet of sortedSubnets) {
    for (const p of groups[subnet]) {
      order.push(p.ip);
    }
  }
  return order;
}

function getPrinterOrder(printers) {
  const stored = localStorage.getItem(STORAGE_KEY);
  if (stored) {
    try {
      const order = JSON.parse(stored);
      const currentIps = new Set(printers.map(p => p.ip));
      const validOrder = order.filter(ip => currentIps.has(ip));
      const newIps = printers.filter(p => !validOrder.includes(p.ip)).map(p => p.ip);
      const finalOrder = [...validOrder, ...newIps];
      if (finalOrder.length !== order.length) {
        localStorage.setItem(STORAGE_KEY, JSON.stringify(finalOrder));
      }
      return finalOrder;
    } catch(e) {}
  }
  return getDefaultPrinterOrder(printers);
}

function sortPrintersForDisplay(printers) {
  const order = getPrinterOrder(printers);
  return [...printers].sort((a, b) => {
    const aSensor = a.device_type === 'sensor' ? 0 : 1;
    const bSensor = b.device_type === 'sensor' ? 0 : 1;
    if (aSensor !== bSensor) return aSensor - bSensor;

    const aIndex = order.indexOf(a.ip);
    const bIndex = order.indexOf(b.ip);
    if (aIndex !== bIndex) return aIndex - bIndex;

    return a.ip.localeCompare(b.ip, undefined, { numeric: true });
  });
}

function savePrinterOrder(order) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(order));
}

function resetPrinterOrder() {
  if (!canAdmin()) { toast('دسترسی ندارید', 'e'); return; }
  localStorage.removeItem(STORAGE_KEY);
  toast('ترتیب به حالت پیش‌فرض برگشت', 's');
  fetchData();
}

function renderPrinterCard(p) {
  const online = p.online;
  const deviceType = p.device_type || 'unknown';
  const badgeStyle = 'display:inline-block;padding:2px 8px;border-radius:10px;font-size:9px;margin-right:6px;font-weight:bold;color:#fff;vertical-align:middle;';

  // ═══ کارت مخصوص دماسنج ═══
  if (deviceType === 'sensor') {
    const c = p.counters || {};
    const tempPorts = Array.isArray(c.temp_ports) && c.temp_ports.length
      ? c.temp_ports
      : [{port: 1, value: c.temp1}, ...(c.temp2 !== null && c.temp2 !== undefined ? [{port: 2, value: c.temp2}] : [])];
    const humPorts = Array.isArray(c.hum_ports) && c.hum_ports.length
      ? c.hum_ports
      : [{port: 1, value: c.hum1}, ...(c.hum2 !== null && c.hum2 !== undefined ? [{port: 2, value: c.hum2}] : [])];
    const displayName = p.nickname ? `${escapeHtml(p.nickname)} (${escapeHtml(p.name)})` : escapeHtml(p.name);

    let officeName = '';
    for (const g of OFFICE_GROUPS) {
      if (g.subnet && p.ip.startsWith(g.subnet + '.')) { officeName = g.name; break; }
    }
    if (!officeName) officeName = 'سایر';

    const sensorRows = [];
    tempPorts.forEach(item => sensorRows.push(`
      <div style="display:flex; justify-content:space-between; align-items:center; padding:5px 0; border-bottom:1px solid rgba(255,255,255,0.05)">
        <span style="font-size:10px; color:var(--text3)">🌡️ دما پورت ${item.port}</span>
        ${sensorValueHtml({ip: p.ip, kind: 'temperature', port: item.port, value: item.value, unit: '°C', status: item.status, fontSize: 16})}
      </div>`));
    humPorts.forEach(item => sensorRows.push(`
      <div style="display:flex; justify-content:space-between; align-items:center; padding:5px 0; border-bottom:1px solid rgba(255,255,255,0.05)">
        <span style="font-size:10px; color:var(--text3)">💧 رطوبت پورت ${item.port}</span>
        ${sensorValueHtml({ip: p.ip, kind: 'humidity', port: item.port, value: item.value, unit: '%', status: item.status, fontSize: 16})}
      </div>`));

    return `
      <div class="overview-card oc-online sensor-banner" data-ip="${p.ip}" onclick="switchTab('${p.ip}')" style="border-top:2px solid var(--orange)">
        <div class="oc-header">
          <div>
            <div class="oc-ip" style="color:var(--orange)">${p.ip} <span style="${badgeStyle}background:#ff9800;">دماسنج</span></div>
            <div class="oc-name">${displayName}</div>
            <div class="oc-model">ECS100G · ${officeName}</div>
          </div>
          ${online === true
            ? `<div class="oc-pill pill-on"><span class="pill-dot" style="background:var(--green);box-shadow:0 0 5px var(--green)"></span>ONLINE</div>`
            : online === false
            ? `<div class="oc-pill pill-off"><span class="pill-dot" style="background:var(--red)"></span>OFFLINE</div>`
            : `<div class="oc-pill" style="border:1px solid var(--border);color:var(--text3)">—</div>`}
        </div>
        <div class="oc-sensor-data" style="margin-top:8px">
          ${sensorRows.join('')}
        </div>
        ${p.alerts?.length ? `<div class="oc-alert">⚠ ${p.alerts.map(a=>a.message).join(' | ')}</div>` : ''}
      </div>`;
  }

  // ═══ کارت پرینتر (کد قبلی) ═══
  const c = p.counters || {};
  const total = c.total || 0;
  const fc = c.full_color || 0;
  const bw = c.black_white || 0;
  const toners = p.toners || {};

  const cyanLevel = toners.cyan?.level;
  const magentaLevel = toners.magenta?.level;
  const yellowLevel = toners.yellow?.level;
  const blackLevel = toners.black?.level;

  // ✅ باگ #7: تشخیص صحیح وضعیت undefined برای نوارهای تونر
  const cyanHasData = cyanLevel !== null && cyanLevel !== undefined;
  const magentaHasData = magentaLevel !== null && magentaLevel !== undefined;
  const yellowHasData = yellowLevel !== null && yellowLevel !== undefined;
  const blackHasData = blackLevel !== null && blackLevel !== undefined;

  const cls = online === true ? 'oc-online' : (online === false ? 'oc-offline' : '');
  const warnCls = (p.alerts?.length) ? 'oc-warn' : cls;
  const displayName = p.nickname ? `${escapeHtml(p.nickname)} (${escapeHtml(p.name)})` : escapeHtml(p.name);
  
  let typeBadge = '';
  if (deviceType === 'color') {
    typeBadge = `<span style="${badgeStyle}background:#e91e63;">رنگی</span>`;
  } else if (deviceType === 'mono') {
    typeBadge = `<span style="${badgeStyle}background:#607d8b;">تک‌رنگ</span>`;
  } else if (deviceType === 'sensor') {
    typeBadge = `<span style="${badgeStyle}background:#ff9800;">دماسنج</span>`;
  } else {
    typeBadge = `<span style="${badgeStyle}background:#3a3a3a;">ناشناخته</span>`;
  }
  
  let officeName = '';
  for (const g of OFFICE_GROUPS) {
    if (g.subnet && p.ip.startsWith(g.subnet + '.')) {
      officeName = g.name;
      break;
    }
  }
  if (!officeName) officeName = 'سایر';

  // ✅ باگ #7: نمایش صحیح تونرهای نامشخص (جلوگیری از نمایش 0% وقتی مقدار null است)
  // ✅ باگ #7: نمایش صحیح تونرهای نامشخص (جلوگیری از نمایش 0% وقتی مقدار null است)
  const colorTonersHtml = `
    <div class="oc-toner-group">
      <div class="oc-toner-item">
        <div class="oc-toner-bar" style="width: ${cyanHasData ? cyanLevel : 0}%; background: ${cyanHasData ? '#00d4ff' : '#555'};"></div>
      </div>
      <div class="oc-toner-item">
        <div class="oc-toner-bar" style="width: ${magentaHasData ? magentaLevel : 0}%; background: ${magentaHasData ? '#ea80fc' : '#555'};"></div>
      </div>
      <div class="oc-toner-item">
        <div class="oc-toner-bar" style="width: ${yellowHasData ? yellowLevel : 0}%; background: ${yellowHasData ? '#ffd740' : '#555'};"></div>
      </div>
    </div>
  `;

  const blackTonerHtml = `
    <div class="oc-toner-item">
      <div class="oc-toner-bar" style="width: ${blackHasData ? blackLevel : 0}%; background: ${blackHasData ? '#9e9e9e' : '#555'};"></div>
    </div>
  `;

  return `
    <div class="overview-card ${warnCls}" data-ip="${p.ip}" onclick="switchTab('${p.ip}')">
      <div class="oc-header">
        <div>
          <div class="oc-ip">${p.ip} ${typeBadge}</div>
          <div class="oc-name">${displayName}</div>
          <div class="oc-model">${p.device?.model || 'TOSHIBA'} · ${officeName}</div>
        </div>
        ${online === true
          ? `<div class="oc-pill pill-on"><span class="pill-dot" style="background:var(--green);box-shadow:0 0 5px var(--green)"></span>ONLINE</div>`
          : online === false
          ? `<div class="oc-pill pill-off"><span class="pill-dot" style="background:var(--red)"></span>OFFLINE</div>`
          : `<div class="oc-pill" style="border:1px solid var(--border);color:var(--text3)">—</div>`}
      </div>

      <div class="oc-stat-row">
        <div class="oc-counter">
          <span class="oc-counter-val">${fmtN(total)}</span>
          <span class="oc-counter-label">کل</span>
        </div>
        <div class="oc-toner-placeholder"></div>
      </div>

      <div class="oc-stat-row">
        <div class="oc-counter">
          <span class="oc-counter-val" style="color:var(--cyan)">${fmtN(fc)}</span>
          <span class="oc-counter-label">رنگی</span>
        </div>
        <div class="oc-toner-area">${colorTonersHtml}</div>
      </div>

      <div class="oc-stat-row">
        <div class="oc-counter">
          <span class="oc-counter-val" style="color:var(--text2)">${fmtN(bw)}</span>
          <span class="oc-counter-label">BW</span>
        </div>
        <div class="oc-toner-area">${blackTonerHtml}</div>
      </div>

      ${p.alerts?.length ? `<div class="oc-alert">⚠ ${p.alerts.map(a=>a.message).join(' | ')}</div>` : ''}
    </div>
  `;
}

// ══════════════════════════════════════════════════
// FETCH & UPDATE
// ══════════════════════════════════════════════════
async function fetchData() {
  setDot('fetch');
  try {
    const [pr, st, lg] = await Promise.all([
      fetch(`${API}/api/printers`,{cache:'no-store'}).then(r=>r.json()),
      fetch(`${API}/api/status`,{cache:'no-store'}).then(r=>r.json()),
      fetch(`${API}/api/logs/all`,{cache:'no-store'}).then(r=>r.json()),
    ]);
    allData   = pr.printers || [];
    allEvents = lg.events   || [];
    serverInfo = st;
    updateMeta(pr.meta, st);
    rebuildTabs(allData);
    renderOverviewCards(allData);
    renderGlobalLog(allEvents);
    renderAllPrinterPanels(allData);
    renderAccessPanel(st);
    populatePrinterSelect();
    setDot('on');
    if (!isFirst) toast('آپدیت شد','s');
    isFirst = false;
    resetCountdown();
  } catch(e) {
    console.error(e);
    setDot('err');
    toast('خطا در اتصال به سرور','e');
  }
}

// Toner Reset Modal and handlers
let _tonerResetTargetIp = null;
function openTonerResetModal(ip) {
  _tonerResetTargetIp = ip;
  const p = (allData || []).find(x=>x.ip===ip) || {};
  const toners = p.toners || {};
  const colors = ['black','cyan','magenta','yellow'].filter(c => !!(toners[c] && (toners[c].level !== undefined || toners[c].name)) );
  if (!colors.length) { toast('هیچ اطلاعات تونری برای این دستگاه وجود ندارد','e'); return; }
  const modal = document.getElementById('modal-toner-reset');
  if (!modal) return;
  const sel = modal.querySelector('#toner-reset-color');
  const lvl = modal.querySelector('#toner-reset-level');
  sel.innerHTML = colors.map(c=>`<option value="${c}">${({black:'مشکی',cyan:'سیان',magenta:'مژنتا',yellow:'زرد'}[c])}</option>`).join('');
  lvl.value = 100;
  modal.style.display = 'block';
}

function closeTonerResetModal() {
  const modal = document.getElementById('modal-toner-reset');
  if (modal) modal.style.display = 'none';
  _tonerResetTargetIp = null;
}

async function submitTonerReset() {
  const modal = document.getElementById('modal-toner-reset');
  if (!modal) return;
  const color = modal.querySelector('#toner-reset-color').value;
  let new_level = parseInt(modal.querySelector('#toner-reset-level').value || '100');
  if (isNaN(new_level)) new_level = 100;
  new_level = Math.max(0, Math.min(100, new_level));
  if (!_tonerResetTargetIp) { toast('هدف نامشخص است','e'); return; }
  try {
    const r = await apiFetch(`${API}/api/printer/${encodeURIComponent(_tonerResetTargetIp)}/toner_reset`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ color, new_level })
    });
    const j = await r.json();
    if (r.ok) {
      toast('مقدار تونر به‌روزرسانی شد','s');
      closeTonerResetModal();
      fetchData();
    } else {
      if (j.error === 'csrf_token_invalid') {
        toast('نشست شما منقضی شده است. صفحه را تازه کنید و دوباره تلاش کنید.', 'e');
      } else {
        toast(j.error || 'خطا در به‌روزرسانی', 'e');
      }
    }
  } catch(e) {
    console.error(e);
    toast('خطا در اتصال', 'e');
  }
}

function updateMeta(meta, st) {
  document.getElementById('m-on').textContent  = meta?.online  ?? '—';
  document.getElementById('m-off').textContent = meta?.offline ?? '—';
  document.getElementById('m-poll').textContent = meta?.poll_count ? `#${meta.poll_count}` : '—';
  const lp = meta?.last_poll;
  document.getElementById('m-time').textContent = lp ? new Date(lp).toLocaleTimeString('fa-IR') : '—';
  const userEl = document.getElementById('m-user');
  if (userEl) {
    userEl.textContent = window.currentUsername || 'میهمان';
  }
}

// ══════════════════════════════════════════════════
// SIDEBAR & TABS
// ══════════════════════════════════════════════════
let activeTab = 'overview';

function getOfficeGroup(ip) {
  for (const g of OFFICE_GROUPS) {
    if (g.subnet && ip.startsWith(g.subnet + '.')) return g.id;
  }
  return 'other';
}

function rebuildTabs(printers) {
  const container = document.getElementById('sidebar-groups');
  if (!container) return;

  const totalOnline  = printers.filter(p => p.online === true).length;
  const totalOffline = printers.filter(p => p.online === false).length;
  const badge = document.getElementById('sb-ov-badge');
  if (badge) badge.textContent = totalOnline + '▲ ' + totalOffline + '▼';

  // استخراج گروه‌های پویا
  const groups = new Map();
  OFFICE_GROUPS.forEach(g => groups.set(g.id, { ...g, printers: [] }));
  
  printers.forEach(p => {
    let gid = p.group || getOfficeGroup(p.ip);
    if (!groups.has(gid)) {
      groups.set(gid, { id: gid, name: gid, icon: '🏢', color: 'cyan', printers: [] });
    }
    groups.get(gid).printers.push(p);
  });

  const order = getPrinterOrder(printers);
  container.innerHTML = '';

  groups.forEach(g => {
    let members = g.printers;
    if (!members.length) return;

    members.sort((a, b) => order.indexOf(a.ip) - order.indexOf(b.ip));

    const onCnt    = members.filter(p => p.online === true).length;
    const offCnt   = members.filter(p => p.online === false).length;
    const hasAlert = members.some(p => p.alerts && p.alerts.length > 0);

    if (_groupOpen[g.id] === undefined) {
      _groupOpen[g.id] = members.some(p => p.ip === activeTab) || g.id === 'imamat';
    }

    const groupEl = document.createElement('div');
    groupEl.className = 'sb-group' + (_groupOpen[g.id] ? ' open' : '') + (activeGroupFilter === g.id ? ' active-group' : '');
    groupEl.dataset.color = g.color;
    groupEl.id = 'sbg-' + g.id;

    const metaHtml = `
      <div class="sb-meta-group">
        ${onCnt ? `<div class="sb-meta-item"><span class="sb-meta-dot online"></span><span class="sb-meta-count">${onCnt}</span></div>` : ''}
        ${offCnt ? `<div class="sb-meta-item"><span class="sb-meta-dot offline"></span><span class="sb-meta-count">${offCnt}</span></div>` : ''}
      </div>
    `;

    const itemsHtml = members.map(p => {
      const dotCls    = p.online === true ? 'online' : (p.online === false ? 'offline' : 'unknown');
      const hasAl     = p.alerts && p.alerts.length;
      const alertIcon = hasAl ? '<span class="sb-alert-icon">⚠</span>' : '';
      const activeCls = activeTab === p.ip ? ' active' : '';
      const displayName = p.nickname ? `${escapeHtml(p.nickname)} (${escapeHtml(p.name)})` : escapeHtml(p.name);
      return `<div class="sb-item${activeCls}${hasAl?' has-alert':''}" data-tab="${p.ip}" onclick="switchTab('${p.ip}',this)">
               <span class="sb-dot ${dotCls}"></span>
               <span class="sb-item-name">${displayName}</span>
               ${alertIcon}
             </div>`;
    }).join('');

    groupEl.innerHTML = `
      <div class="sb-group-hdr${hasAlert ? ' has-alert' : ''}" onclick="filterByGroup('${g.id}')">
        <span class="sb-arrow" onclick="event.stopPropagation();toggleSbGroup('${g.id}')">▶</span>
        <span class="sb-group-icon">${g.icon}</span>
        <span class="sb-group-name">${g.name}</span>
        <span class="sb-group-meta">${metaHtml}</span>
      </div>
      <div class="sb-group-body">${itemsHtml}</div>
    `;

    container.appendChild(groupEl);
  });

  // بروزرسانی لیست گروه‌ها در مودال افزودن
  const groupSelect = document.getElementById('add-group-select');
  if (groupSelect) {
    const currentVal = groupSelect.value;
    groupSelect.innerHTML = '<option value="">(انتخاب گروه...)</option>';
    groups.forEach(g => {
      groupSelect.innerHTML += `<option value="${g.id}">${g.name}</option>`;
    });
    groupSelect.value = currentVal;
  }

  const ovBtn = document.querySelector('.sb-overview');
  if (ovBtn) ovBtn.classList.toggle('active', activeTab === 'overview');
}

let activeGroupFilter = null;

function filterByGroup(groupId) {
  activeTab = 'overview'; // فیلتر در نمای کلی اعمال می‌شود
  activeGroupFilter = groupId;
  
  document.querySelectorAll('.sb-item, .sb-overview').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.sb-group').forEach(g => g.classList.remove('active-group'));
  
  const gEl = document.getElementById('sbg-' + groupId);
  if (gEl) gEl.classList.add('active-group');

  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  document.getElementById('panel-overview').classList.add('active');
  
  renderOverviewCards(allData);
}

function toggleSbGroup(id) {
  _groupOpen[id] = !_groupOpen[id];
  const el = document.getElementById('sbg-' + id);
  if (el) el.classList.toggle('open', _groupOpen[id]);
}

function destroyPrinterDailyChart(ip) {
  const existing = printerChartInstances[ip];
  if (!existing) return;
  try {
    if (typeof existing.destroy === 'function') {
      existing.destroy();
    }
  } catch (e) {
    console.error('Printer chart destroy error:', e);
  }
  delete printerChartInstances[ip];
}

function schedulePrinterDailyChartLoad(ip, delay = 50, force = false) {
  if (!ip || ip === 'overview') return;
  const panel = document.getElementById('panel-' + ip.replace(/\./g, '-'));
  if (!panel || !panel.classList.contains('active')) return;
  const chartCanvas = panel.querySelector('.printer-daily-chart-canvas');
  if (!chartCanvas) return;
  if (!force && (chartCanvas.dataset.dailyChartLoaded || chartCanvas.dataset.dailyChartLoading)) return;

  chartCanvas.dataset.dailyChartLoading = '1';
  delete chartCanvas.dataset.dailyChartLoaded;

  setTimeout(() => {
    loadPrinterDailyChart(ip)
      .then(() => {
        const currentCanvas = document.getElementById(`printer-daily-chart-${ip.replace(/\./g, '-')}`);
        if (currentCanvas) {
          currentCanvas.dataset.dailyChartLoaded = '1';
          delete currentCanvas.dataset.dailyChartLoading;
        }
      })
      .catch(err => {
        const currentCanvas = document.getElementById(`printer-daily-chart-${ip.replace(/\./g, '-')}`);
        if (currentCanvas) {
          delete currentCanvas.dataset.dailyChartLoading;
        }
        console.error('Printer chart load failed:', err);
      });
  }, delay);
}

function switchTab(id, el) {
  activeTab = id;
  
  // اگر روی یک پرینتر کلیک شد، فیلتر گروه را پاک کن
  if (id !== 'overview' && !id.startsWith('group:')) {
    activeGroupFilter = null;
  }

  document.querySelectorAll('.sb-item, .sb-overview').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.sb-group').forEach(g => g.classList.remove('active-group'));

  if (id === 'overview') {
    activeGroupFilter = null;
    const ovBtn = document.querySelector('.sb-overview');
    if (ovBtn) ovBtn.classList.add('active');
  } else if (el) {
    el.classList.add('active');
  } else {
    const found = document.querySelector('[data-tab="' + id + '"]');
    if (found) found.classList.add('active');
  }

  if (id !== 'overview') {
    const p = allData.find(x => x.ip === id);
    const gid = p?.group || getOfficeGroup(id);
    if (!_groupOpen[gid]) {
      _groupOpen[gid] = true;
      const gEl = document.getElementById('sbg-' + gid);
      if (gEl) gEl.classList.add('open');
    }
  }

  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  const panelId = id.replace(/\./g, '-');
  const panel = document.getElementById('panel-' + panelId) || document.getElementById('panel-overview');
  if (panel) {
    panel.classList.add('active');
    if (id !== 'overview') schedulePrinterDailyChartLoad(id);
    if (id === 'overview' || activeGroupFilter) renderOverviewCards(allData);
  }
}

// ══════════════════════════════════════════════════
// OVERVIEW CARDS
// ══════════════════════════════════════════════════
function renderOverviewCards(printers) {
  const grid = document.getElementById('overview-grid');
  if (!printers.length) {
    grid.innerHTML = '<div style="padding:60px;text-align:center;color:var(--text3);font-family:var(--mono)">پرینتری تعریف نشده</div>';
    if (sortableInstance) { try { sortableInstance.destroy(); } catch(e) {} sortableInstance = null; }
    return;
  }

  currentPrinters = printers;
  
  // اعمال فیلتر بر اساس گروه
  let displayPrinters = printers;
  if (activeGroupFilter) {
    displayPrinters = printers.filter(p => {
        let gid = p.group || getOfficeGroup(p.ip);
        return gid === activeGroupFilter;
    });
  }

  const orderedPrinters = sortPrintersForDisplay(displayPrinters);
  grid.innerHTML = orderedPrinters.map(p => renderPrinterCard(p)).join('');

  if (sortableInstance) {
    try {
      sortableInstance.destroy();
    } catch(e) {
      console.warn("Sortable destroy error:", e);
    }
    sortableInstance = null;
  }

  // فقط در صورتی که فیلتر فعال نباشد اجازه جابجایی می‌دهیم
  if (!activeGroupFilter) {
    sortableInstance = new Sortable(grid, {
        animation: 400,
        easing: 'cubic-bezier(0.22, 1, 0.36, 1)',
        sort: true,
        swap: true,
        swapThreshold: 0.65,
        fallbackTolerance: 5,
        delay: 0,
        chosenClass: 'sortable-chosen',
        ghostClass: 'sortable-ghost',
        dragClass: 'sortable-drag',
        onChoose: function(evt) {
          document.body.classList.add('is-dragging');
        },
        onUnchoose: function(evt) {
          document.body.classList.remove('is-dragging');
        },
        onEnd: function(evt) {
          document.body.classList.remove('is-dragging');
          const items = grid.querySelectorAll('.overview-card');
          const newOrder = Array.from(items).map(card => card.getAttribute('data-ip'));
          savePrinterOrder(newOrder);
          setTimeout(() => rebuildTabs(currentPrinters), 50);
        }
    });
  }
}

// ══════════════════════════════════════════════════
// PRINTER DETAIL & SENSOR DETAIL PANELS
// ══════════════════════════════════════════════════
function renderAllPrinterPanels(printers) {
  sortPrintersForDisplay(printers).forEach(p => {
    const id = 'panel-' + p.ip.replace(/\./g,'-');
    let panel = document.getElementById(id);
    if (!panel) {
      panel = document.createElement('div');
      panel.id = id;
      panel.className = 'tab-panel';
      document.querySelector('.main').appendChild(panel);
    }

    const wasActive = panel.classList.contains('active') || activeTab === p.ip;
    destroyPrinterDailyChart(p.ip);
    
    // تشخیص نوع دستگاه
    if (p.device_type === 'sensor') {
      panel.innerHTML = buildSensorDetail(p);
    } else {
      panel.innerHTML = buildPrinterDetail(p);
    }
    
    const logId = 'plog-' + p.ip.replace(/\./g,'-');
    const printerEvents = allEvents.filter(e => e.printer_ip === p.ip);
    renderLogTable(printerEvents, logId, logId+'-count');

    if (wasActive) {
      schedulePrinterDailyChartLoad(p.ip, 80, true);
    }
  });
}

function buildPrinterDetail(p) {
  if (!p.online && p.online !== null) return `
    <div class="offline-banner">
      <h2>🔌 آفلاین</h2>
      <div class="offline-message">${p.ip} — ${p.error || 'Device unreachable'}</div>
      <div style="margin-top:8px;font-size:11px;color:var(--red);font-family:var(--mono)">آخرین بررسی: ${p.last_poll ? new Date(p.last_poll).toLocaleTimeString('fa-IR') : '—'}</div>
      <button class="btn btn-cyan" style="margin-top:16px" onclick="removePrinter('${p.ip}','${p.name}')">× حذف پرینتر</button>
    </div>`;

  if (p.online === null) return `<div style="padding:60px;text-align:center;color:var(--text3);font-family:var(--mono)">در حال بررسی...</div>`;

  const c = p.counters || {}; const pz = p.paper_sizes || {};
  const toners = p.toners || {};
  const dev = p.device || {}; const alerts = p.alerts || [];
  const total = c.total || 0;
  const fc = c.full_color || 0;
  const bw = c.black_white || 0;
  const fcPct = total > 0 ? Math.round((fc / total) * 100) : 0;
  const bwPct = total > 0 ? Math.round((bw / total) * 100) : 0;
  const scan  = (c.scan_fc||0)+(c.scan_bw||0)+(c.scan_net_fc||0)+(c.scan_net_bw||0);
  const hasPagesSinceReset = typeof c.pages_since_last_reset === 'number' && Number.isFinite(c.pages_since_last_reset);

  const maxP = Math.max(...Object.values(pz).map(v=>v.total||0), 1);
  const PCOLS = {A4:{cls:'pb-a4',clr:'#00d4ff'},A3:{cls:'pb-a3',clr:'#ff7043'},A4R:{cls:'pb-a4r',clr:'#ffd740'},A5:{cls:'pb-a5',clr:'#00e676'},B4:{cls:'pb-b4',clr:'#ea80fc'}};
  const paperRows = Object.entries(pz).map(([size,d])=>{
    if (!d.total) return '';
    const pct = Math.round(d.total/maxP*100);
    const col = PCOLS[size]||PCOLS.A4;
    return `<div class="psize-row">
      <span class="pb ${col.cls}">${size}</span>
      <div class="pbar-wrap"><div class="pbar-fill" style="width:${pct}%;background:${col.clr}"></div></div>
      <div>
        <div class="pbar-count num">${fmtN(d.total)}</div>
        <div class="pbar-sub">FC:${fmtN(d.fc)} BW:${fmtN(d.bw)}</div>
      </div>
    </div>`;
  }).join('');

  const TCOLORS = {cyan:'#00d4ff',magenta:'#ea80fc',yellow:'#ffd740',black:'#9e9e9e'};
  const TGRADS  = {cyan:'#00d4ff,#0097a7',magenta:'#ea80fc,#ab47bc',yellow:'#ffd740,#f9a825',black:'#bdbdbd,#757575'};

  const preferredSupplyOrder = ['black','cyan','magenta','yellow','drum'];
  const orderedSupplyKeys = [
    ...preferredSupplyOrder.filter(key => Object.prototype.hasOwnProperty.call(toners, key)),
    ...Object.keys(toners).filter(key => !preferredSupplyOrder.includes(key))
  ];
  const supplyCards = orderedSupplyKeys
    .filter(col => {
      const td = toners[col] || {};
      return Boolean(td.name || td.level !== undefined && td.level !== null || td.remaining || td.max || td.usage || td.usage_m);
    })
    .map(col => {
      const td = toners[col] || {};
      const name = td.name || {black:'تونر سیاه',cyan:'تونر فیروزه‌ای',magenta:'تونر ارغوانی',yellow:'تونر زرد',drum:'درام'}[col];
      const hasLevel = typeof td.level === 'number' && Number.isFinite(td.level);
      const level = hasLevel ? Math.max(0, Math.min(100, td.level)) : null;
      const status = td.status || 'unknown';
      const statusMap = {ok:'سالم', low:'کم', critical:'بحرانی', empty:'خالی', unknown:'نامشخص', not_supported:'پشتیبانی‌نشده', no_sensor:'بدون سنسور'};
      const statusText = statusMap[status] || status;
      const statusColor = {ok:'var(--green)', low:'var(--yellow)', critical:'var(--red)', empty:'var(--red)', not_supported:'var(--text3)', no_sensor:'var(--text2)'}[status] || 'var(--text3)';
      const dotColor = col === 'drum' ? '#ffb74d' : (TCOLORS[col] || '#9e9e9e');
      const progressGradient = col === 'drum' ? '#ffb74d,#fb8c00' : (TGRADS[col] || '#9e9e9e,#757575');
      const usageValue = (typeof td.usage === 'number' && td.usage > 0) ? td.usage : null;
      const usageMega = (typeof td.usage_m === 'number' && td.usage_m > 0)
        ? td.usage_m
        : (usageValue ? Number((usageValue / 1000000).toFixed(2)) : null);
      const learnedCapacity = Number(td.capacity_pages || td.yield_per_page || c.yield_per_page || 0);
      const capacitySource = td.capacity_source || c.yield_source || '';
      const yieldConfidence = td.yield_confidence || c.yield_confidence || (capacitySource.includes('default') ? 'low' : 'medium');
      const yieldSamples = Number(td.yield_sample_count || 0);
      const yieldSourceMap = {
        default: 'پیش‌فرض',
        auto_learn: 'یادگیری خودکار',
        shared_profile: 'پروفایل مشترک',
        device_capacity: 'ظرفیت اعلامی دستگاه',
        catalog: 'کاتالوگ کارتریج',
        manual_reset: 'ریست دستی',
        forced_estimate: 'تخمین اجباری',
        cycle_learn: 'یادگیری چرخه کارتریج',
        learned: 'یادگرفته‌شده'
      };
      const yieldConfidenceMap = {low: 'کم', medium: 'متوسط', high: 'بالا'};
      const yieldSourceText = yieldSourceMap[capacitySource] || (capacitySource.includes('learned') ? 'یادگیری خودکار' : capacitySource || 'نامشخص');
      const yieldConfidenceText = yieldConfidenceMap[yieldConfidence] || yieldConfidence || 'نامشخص';
      const isEstimatedCapacity = learnedCapacity > 0 && (!td.max || td.max <= 100 || capacitySource.includes('learned') || capacitySource.includes('default') || capacitySource === 'auto_learn' || capacitySource === 'shared_profile');
      const hasLearnedCapacity = learnedCapacity > 0 && (capacitySource.includes('learned') || capacitySource.includes('forced') || capacitySource === 'auto_learn' || capacitySource === 'shared_profile');
      const capacityText = hasLearnedCapacity
        ? `${fmtN(learnedCapacity)} صفحه تخمینی`
        : ((typeof td.max === 'number' && td.max > 100)
          ? `${fmtN(td.max)} صفحه`
          : (learnedCapacity > 0 ? `${fmtN(learnedCapacity)} صفحه${isEstimatedCapacity ? ' تخمینی' : ''}` : '—'));
      const pagesAfterZero = Number(td.pages_after_zero || 0);
      // پیام zero plateau فقط وقتی معنی دارد که سطح فعلی واقعاً 0٪ باشد.
      // بعد از شارژ/ریست، ممکن است cycle_status تا تأیید poll بعدی در backend موقتاً باقی بماند؛
      // در سطح 100٪ نباید پیام «تونر صفر است» نمایش داده شود.
      const showZeroPlateau = hasLevel && level === 0 && (td.cycle_status === 'zero_plateau' || pagesAfterZero > 0);
      const zeroPlateauHtml = showZeroPlateau ? `
        <div class="supply-zero-hint">تونر از دید SNMP صفر است اما چاپ ادامه دارد؛ چاپ پس از صفر: <strong class="num">${fmtN(pagesAfterZero)}</strong> صفحه</div>
      ` : '';
      const yieldInfoHtml = learnedCapacity > 0 ? `
        <div class="supply-yield-info">
          <span class="yield-chip yc-${escapeHtml(yieldConfidence)}">اعتماد: ${escapeHtml(yieldConfidenceText)}</span>
          <span>منبع: ${escapeHtml(yieldSourceText)}</span>
          ${yieldSamples ? `<span>نمونه: <strong class="num">${fmtN(yieldSamples)}</strong></span>` : ''}
        </div>
        ${zeroPlateauHtml}
      ` : zeroPlateauHtml;
      const remainingText = hasLevel
        ? `${level}%`
        : (typeof td.remaining === 'number' && td.remaining >= 0 ? fmtN(td.remaining) : '—');
      const unsupportedHint = (status === 'not_supported' || status === 'no_sensor')
        ? `<div class="supply-card-hint">این دستگاه سطح این مصرفی را از طریق SNMP گزارش نمی‌کند.</div>`
        : '';

      return `
        <article class="supply-card">
          <div class="supply-card-head">
            <div class="supply-card-title">
              <span class="supply-card-dot" style="background:${dotColor};box-shadow:0 0 6px ${dotColor}40"></span>
              <div>
                <div class="supply-card-name">${escapeHtml(name)}</div>
                <div class="supply-card-capacity">ظرفیت: ${escapeHtml(capacityText)}</div>
              </div>
            </div>
            <span class="toner-status-badge ${ {ok:'ts-ok',low:'ts-low',critical:'ts-critical',empty:'ts-empty'}[status] || 'ts-ok' }">${escapeHtml(statusText)}</span>
          </div>

          <div class="supply-card-meta">
            <span>باقی‌مانده: <strong style="color:${statusColor}">${escapeHtml(remainingText)}</strong></span>
            ${hasLevel ? `<span class="supply-card-level num">${level}%</span>` : ''}
          </div>

          <div class="supply-card-progress">
            <div class="supply-card-progress-fill${hasLevel ? '' : ' is-empty'}" style="width:${hasLevel ? level : 0}%;background:${hasLevel ? `linear-gradient(90deg,${progressGradient})` : 'linear-gradient(90deg, rgba(123,134,160,.2), rgba(123,134,160,.08))'}"></div>
          </div>

          ${usageValue || usageMega ? `
            <div class="supply-card-stats">
              ${usageValue ? `<div class="supply-stat"><span class="supply-stat-lbl">Dot Count</span><span class="supply-stat-val num">${fmtN(usageValue)}</span></div>` : ''}
              ${usageMega ? `<div class="supply-stat"><span class="supply-stat-lbl">Mega Dots</span><span class="supply-stat-val num">${usageMega}M</span></div>` : ''}
            </div>
          ` : ''}
          ${yieldInfoHtml}
          ${unsupportedHint}
        </article>`;
    }).join('');

  const alertsHtml = alerts.length ? `<div class="section" style="margin-top:14px">
    <div class="section-title">🚨 هشدارهای فعال</div>
    ${alerts.map(a=>`<div class="alert-row"><span style="font-size:14px">⚠️</span><span class="alert-text">${a.message}</span><span class="alert-code">#${a.code}</span></div>`).join('')}
  </div>` : '';

  const ip = p.ip;
  const logId = 'plog-' + ip.replace(/\./g,'-');
  const displayName = p.nickname ? escapeHtml(p.nickname) : escapeHtml(p.name);
  const editButtonHtml = canEditPrinters()
    ? `<button class="btn btn-sm" onclick="openEditModal('${p.ip}')" 
                 style="font-size:10px; margin-right:8px; padding:2px 6px;" title="ویرایش اطلاعات و گروه">⚙️</button>`
    : '';
  const nameLine = p.nickname
    ? `<div style="font-size:16px;font-weight:700;margin-top:2px">
         ${displayName}
         ${editButtonHtml}
       </div>
       <div style="font-family:var(--mono);font-size:10px;color:var(--text3);margin-top:2px">نام اصلی: ${escapeHtml(p.name)}</div>`
    : `<div style="font-size:16px;font-weight:700;margin-top:2px">
         ${displayName}
          ${editButtonHtml}
       </div>`;

  // ✅ باگ #6: حذف دکمه حذف تکراری — فقط printerDeleteButtonHtml استفاده می‌شود
  const printerDeleteButtonHtml = canEditPrinters()
    ? `<button class="btn btn-sm btn-orange" onclick="removePrinter('${p.ip}','${escapeHtml(p.name)}')" style="font-size:9px">× حذف</button>`
    : '';

  const printerLogActionsHtml = canAdmin()
    ? `
        <button class="btn btn-cyan" onclick="openEvModal('${ip}','SERVICE')" style="font-size:11px">🔧 ثبت سرویس</button>
        <button class="btn btn-yellow" onclick="openEvModal('${ip}','REFILL')" style="font-size:11px">🖨 شارژ کارتریج</button>
        <input type="datetime-local" class="printer-log-start" data-ip="${ip}" style="width:auto; font-family:var(--mono); font-size:11px;" title="شروع بازه">
        <input type="datetime-local" class="printer-log-end" data-ip="${ip}" style="width:auto; font-family:var(--mono); font-size:11px;" title="پایان بازه">
        <button class="btn btn-cyan" onclick="applyDateFilter('${ip}')">🔍 اعمال فیلتر</button>
        
        <div class="dropdown" id="export-dropdown-${ip.replace(/\./g, '-')}">
          <button class="btn btn-yellow dropdown-toggle" onclick="toggleDropdown('export-dropdown-${ip.replace(/\./g, '-')}')">📥 خروجی گزارش‌ها ▼</button>
          <div class="dropdown-menu">
            <button class="dropdown-item" onclick="exportLogsWithRange('${ip}', 'excel')">📊 Excel (بازه انتخابی)</button>
            <button class="dropdown-item" onclick="exportLogsWithRange('${ip}', 'csv')">📄 CSV (بازه انتخابی)</button>
            <div class="topbar-dropdown-divider"></div>
            <button class="dropdown-item" onclick="exportPrinterLogExcel('${ip}')">📈 کل لاگ (Excel)</button>
            <button class="dropdown-item" onclick="exportPrinterLogJSON('${ip}')">🔗 کل لاگ (JSON)</button>
          </div>
        </div>

        <button class="btn" style="border-color:rgba(255,61,61,.3);color:var(--red);background:rgba(255,61,61,.06)" onclick="clearPrinterLog('${ip}')">× پاک</button>
      `
    : canManage()
      ? `
        <input type="datetime-local" class="printer-log-start" data-ip="${ip}" style="width:auto; font-family:var(--mono); font-size:11px;" title="شروع بازه">
        <input type="datetime-local" class="printer-log-end" data-ip="${ip}" style="width:auto; font-family:var(--mono); font-size:11px;" title="پایان بازه">
        <button class="btn btn-cyan" onclick="applyDateFilter('${ip}')">🔍 اعمال فیلتر</button>
        
        <div class="dropdown" id="export-dropdown-${ip.replace(/\./g, '-')}">
          <button class="btn btn-yellow dropdown-toggle" onclick="toggleDropdown('export-dropdown-${ip.replace(/\./g, '-')}')">📥 خروجی ▼</button>
          <div class="dropdown-menu">
            <button class="dropdown-item" onclick="exportLogsWithRange('${ip}', 'excel')">📊 Excel (بازه)</button>
            <button class="dropdown-item" onclick="exportPrinterLogExcel('${ip}')">📈 کل لاگ (Excel)</button>
          </div>
        </div>
      `
      : '';

  // ========== بخش جدید آمار با نوارهای افقی ==========
  const isColorDeviceType = (p.device_type === 'color');
  const statsCardHtml = `
    <div class="stats-card">
      <div class="stats-card-header">
        <span class="stats-icon">📊</span>
        <span class="stats-title">آمار کپی و چاپ</span>
        <span class="stats-badge">${new Date().toLocaleDateString('fa-IR')}</span>
      </div>
      <div class="stats-body">
        <div class="stat-summary-grid${hasPagesSinceReset ? ' has-reset' : ''}">
          <div class="stat-total compact">
            <div class="stat-total-label">کل چاپ</div>
            <div class="stat-total-value">${fmtN(total)}</div>
            <div class="stat-total-divider"></div>
          </div>
          ${hasPagesSinceReset ? `
            <div class="stat-total compact stat-reset-total">
              <div class="stat-total-label">چاپ از آخرین شارژ</div>
              <div class="stat-total-value">${fmtN(c.pages_since_last_reset)}</div>
              <div class="stat-total-divider"></div>
            </div>
          ` : ''}
        </div>
        ${isColorDeviceType ? `
        <div class="stat-row">
          <div class="stat-label">
            <span class="stat-dot">🎨</span>
            <span>رنگی</span>
          </div>
          <div class="stat-bar-wrapper">
            <div class="stat-bar" style="width: ${fcPct}%; background: linear-gradient(90deg, var(--cyan), #0097a7);"></div>
          </div>
          <div class="stat-value">${fmtN(fc)} <span class="stat-percent">(${fcPct}%)</span></div>
        </div>
        ` : ''}
        <div class="stat-row">
          <div class="stat-label">
            <span class="stat-dot">⚫</span>
            <span>سیاه‌سفید</span>
          </div>
          <div class="stat-bar-wrapper">
            <div class="stat-bar" style="width: ${bwPct}%; background: linear-gradient(90deg, var(--text2), #757575);"></div>
          </div>
          <div class="stat-value">${fmtN(bw)} <span class="stat-percent">(${bwPct}%)</span></div>
        </div>
      </div>
      <div class="stats-footer">
        <div class="stats-note">آخرین به‌روزرسانی: ${new Date(p.last_poll).toLocaleTimeString('fa-IR')}</div>
      </div>
    </div>
  `;

  // بخش شمارنده‌های اضافی (پرینتر، کپی، فکس، اسکن) به صورت دو ستونی
  // فقط ردیف‌هایی نمایش داده می‌شوند که مقدار عددی معتبر دارند.
  const extraCounterPrimaryRows = [
    ['Printer', (c.printer ?? total), 'var(--text2)'],
    ['Copy', c.copy, 'var(--magenta)'],
    ['Fax', c.fax, 'var(--blue)'],
    ['List', c.list, 'var(--text3)']
  ].filter(([, value]) => hasNumericCounterValue(value));

  const extraCounterSecondaryRows = [
    ['Scan FC', c.scan_fc, 'var(--cyan)'],
    ['Scan BW', c.scan_bw, 'var(--text2)'],
    ['Net Scan FC', c.scan_net_fc, 'var(--cyan)'],
    ['Net Scan BW', c.scan_net_bw, 'var(--text2)']
  ].filter(([, value]) => hasNumericCounterValue(value));

  const hasAnyExtraCounter = extraCounterPrimaryRows.length || extraCounterSecondaryRows.length;
  const accordionOpen = getExtraCountersAccordionState(ip);
  const safeIp = ip.replace(/\./g, '-');

  const renderExtraCounterRow = ([label, value, color]) => `
    <div class="counter-help-row" style="display:flex;justify-content:space-between;padding:5px 10px;background:var(--bg4);border-radius:5px;border:1px solid var(--border)">
      <span class="counter-help-title-wrap">${renderExtraCounterLabel(label)}</span>
      <span style="font-family:var(--mono);font-size:11px;font-weight:700;color:${color}">${fmtN(value)}</span>
    </div>`;

  const extraCounterPrimaryHtml = extraCounterPrimaryRows.length
    ? `<div style="display:flex;flex-direction:column;gap:6px">${extraCounterPrimaryRows.map(renderExtraCounterRow).join('')}</div>`
    : '';
  const extraCounterSecondaryHtml = extraCounterSecondaryRows.length
    ? `<div style="display:flex;flex-direction:column;gap:6px">${extraCounterSecondaryRows.map(renderExtraCounterRow).join('')}</div>`
    : '';

  const extraCountersHtml = hasAnyExtraCounter ? `
    <div class="section extra-counters-accordion" style="margin-top:10px">
      <button type="button" id="extra-counters-header-${safeIp}" class="extra-counters-accordion-header${accordionOpen ? ' open' : ''}" onclick="toggleExtraCountersAccordion('${ip}')">
        <span class="extra-counters-accordion-title">📊 سایر شمارنده‌ها</span>
        <span class="extra-counters-accordion-icon">⌄</span>
      </button>
      <div id="extra-counters-body-${safeIp}" class="extra-counters-accordion-body${accordionOpen ? ' open' : ''}">
        <div class="detail-grid${(extraCounterPrimaryRows.length === 0 || extraCounterSecondaryRows.length === 0) ? ' col1' : ''}" style="margin-top:10px">
          ${extraCounterPrimaryHtml}
          ${extraCounterSecondaryHtml}
        </div>
      </div>
    </div>
  ` : `<div class="section" style="margin-top:10px"><div class="section-title">📊 سایر شمارنده‌ها</div><div class="log-empty">اطلاعاتی موجود نیست</div></div>`;

  return `
    <div class="section" style="margin-bottom:14px">
      <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px">
        <div>
          <div style="font-family:var(--mono);font-size:12px;color:var(--cyan)">${p.ip}</div>
          ${nameLine}
          <div style="font-family:var(--mono);font-size:10px;color:var(--text3);margin-top:2px">${dev.model} · S/N: ${dev.serial} · FW: ${dev.firmware} · Uptime: ${dev.uptime_str}</div>
        </div>
        <div style="display:flex;gap:8px;align-items:center">
          <div style="text-align:center"><div style="font-family:var(--mono);font-size:9px;color:var(--text3)">Response</div><div style="font-family:var(--mono);font-size:13px;color:var(--cyan)">${p.poll_ms}ms</div></div>
          ${printerDeleteButtonHtml}
        </div>
      </div>
    </div>

    <div class="section" style="margin-bottom:14px">
      <div class="section-title">📊 کانترهای چاپ</div>
      ${statsCardHtml}
      ${extraCountersHtml}
    </div>

    <div class="detail-grid" style="margin-bottom:14px">
      <div class="section">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap">
          <div class="section-title">📦 تونر و کارتریج‌ها</div>
          ${canManage() ? `<button class="btn btn-cyan" onclick="openTonerResetModal('${ip}')" style="font-size:12px">تنظیم مجدد کارتریج</button>` : ''}
        </div>
        <div class="supply-grid">${supplyCards || '<span style="color:var(--text3);font-size:11px">اطلاعات موجود نیست</span>'}</div>
      </div>
      <div class="section"><div class="section-title">📈 نمودار مصرف روزانه (۳۰ روز اخیر)</div>
        <div style="position:relative;height:280px;">
          <canvas id="printer-daily-chart-${ip.replace(/\./g,'-')}" class="printer-daily-chart-canvas" style="width:100%;height:100%;"></canvas>
        </div>
      </div>
    </div>

    ${alertsHtml}

    <div class="section" style="margin-top:14px">
      <div class="section-title">📋 رویدادهای این دستگاه</div>
      <div class="log-toolbar">
        <div class="log-filter" id="filter-${ip.replace(/\./g,'-')}">
          <button class="filter-btn" onclick="filterLog('all',this,'${logId}')">همه</button>
          <button class="filter-btn" onclick="filterLog('ALERT',this,'${logId}')">هشدار</button>
          <button class="filter-btn active" onclick="filterLog('PRINT',this,'${logId}')">چاپ</button>
          <button class="filter-btn" onclick="filterLog('STATUS',this,'${logId}')">وضعیت</button>
          <button class="filter-btn" onclick="filterLog('SERVICE',this,'${logId}')">🔧 سرویس</button>
          <button class="filter-btn" onclick="filterLog('REFILL',this,'${logId}')">🖨 شارژ</button>
        </div>
        <span class="log-count" id="${logId}-count"></span>
        <button class="btn btn-cyan" onclick="openEvModal('${ip}','SERVICE')" style="font-size:11px">🔧 ثبت سرویس</button>
        <button class="btn btn-yellow" onclick="openEvModal('${ip}','REFILL')" style="font-size:11px">🖨 شارژ کارتریج</button>
        <input type="datetime-local" class="printer-log-start" data-ip="${ip}" style="width:auto; font-family:var(--mono); font-size:11px;" title="شروع بازه">
        <input type="datetime-local" class="printer-log-end" data-ip="${ip}" style="width:auto; font-family:var(--mono); font-size:11px;" title="پایان بازه">
        <button class="btn btn-cyan" onclick="applyDateFilter('${ip}')">🔍 اعمال فیلتر</button>
        <button class="btn btn-yellow" onclick="exportLogsWithRange('${ip}', 'excel')">↓ Excel (بازه)</button>
        <button class="btn btn-orange" onclick="exportLogsWithRange('${ip}', 'csv')">↓ CSV (بازه)</button>
        <button class="btn btn-yellow" onclick="exportPrinterLogExcel('${ip}')">↓ Excel</button>
        <button class="btn btn-orange" onclick="exportPrinterLogJSON('${ip}')">↓ JSON</button>
        <button class="btn" style="border-color:rgba(255,61,61,.3);color:var(--red);background:rgba(255,61,61,.06)" onclick="clearPrinterLog('${ip}')">× پاک</button>
      </div>
      <div id="${logId}"></div>
    </div>`;
}

function buildSensorDetail(p) {
  if (!p.online && p.online !== null) return `
    <div class="offline-banner">
      <h2>🔴 آفلاین</h2>
      <div class="offline-message">${p.ip} — ${p.error || 'دستگاه در دسترس نیست'}</div>
      ${canEditPrinters() ? `<button class="btn btn-cyan" style="margin-top:16px" onclick="removePrinter('${p.ip}','${p.name}')">× حذف دستگاه</button>` : ''}
    </div>`;

  if (p.online === null) return `<div style="padding:60px;text-align:center">⏳ در حال بررسی...</div>`;

  const dev = p.device || {};
  const c = p.counters || {};
  const tempPorts = Array.isArray(c.temp_ports) && c.temp_ports.length
    ? c.temp_ports
    : [{port: 1, value: c.temp1, status: c.temp1_status}, ...(c.temp2 !== null && c.temp2 !== undefined ? [{port: 2, value: c.temp2, status: c.temp2_status}] : [])];
  const humPorts = Array.isArray(c.hum_ports) && c.hum_ports.length
    ? c.hum_ports
    : [{port: 1, value: c.hum1, status: c.hum1_status}, ...(c.hum2 !== null && c.hum2 !== undefined ? [{port: 2, value: c.hum2, status: c.hum2_status}] : [])];
  const displayName = p.nickname ? `${p.nickname} (${p.name})` : p.name;
  const editButtonHtml = canEditPrinters()
    ? `<button class="btn btn-sm" onclick="openEditModal('${p.ip}')" 
                 style="font-size:10px; margin-right:8px; padding:2px 6px;" title="ویرایش اطلاعات و گروه">⚙️</button>`
    : '';
  const sensorDeleteButtonHtml = canEditPrinters()
    ? `<button class="btn btn-orange" onclick="removePrinter('${p.ip}','${p.name}')" style="font-size:10px">× حذف</button>`
    : '';
  const sensorLogActionsHtml = canAdmin()
    ? `
        <button class="btn btn-yellow" onclick="exportPrinterLogExcel('${p.ip}')">↓ Excel</button>
        <button class="btn btn-orange" onclick="exportPrinterLogJSON('${p.ip}')">↓ JSON</button>
        <button class="btn" style="border-color:red" onclick="clearPrinterLog('${p.ip}')">× پاک</button>
      `
    : canManage()
      ? `<button class="btn btn-yellow" onclick="exportPrinterLogExcel('${p.ip}')">↓ Excel</button>`
      : '';

  // Helper برای نمایش وضعیت سنسور
  const statusBadge = (status) => {
    if (status === 'active') return '<span style="color:var(--green); font-weight:700">⦿ فعال</span>';
    if (status === 'invalid') return '<span style="color:var(--red); font-weight:700">⚠ نامعتبر</span>';
    return '<span style="color:var(--text3)">⦾ غیرفعال</span>';
  };

  // ردیف یک سنسور (دما یا رطوبت)
  const sensorRow = (label, value, unit, status, port) => {
    const kind = sensorKindFromUnit(unit);
    return `
    <div class="sensor-row">
      <div class="sensor-row-left">
        <span class="sensor-icon">${unit === '°C' ? '🌡️' : '💧'}</span>
        <span class="sensor-label">${label}</span>
        <span class="sensor-status">${statusBadge(status)}</span>
      </div>
      <div class="sensor-value">
        ${sensorValueHtml({ip: p.ip, kind, port, value, unit, status, fontSize: 22})}
      </div>
    </div>
  `;
  };

  return `
    <div class="section" style="margin-bottom:14px">
      <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px">
        <div>
          <div style="font-family:var(--mono);font-size:12px;color:var(--orange)">${p.ip} 🌡️</div>
          <div style="font-size:18px;font-weight:700;margin-top:4px">${displayName}</div>
          <div style="font-family:var(--mono);font-size:10px;color:var(--text3)">
            ${dev.model || 'ECS100G'} · S/N: ${dev.serial || '—'} · FW: ${dev.firmware || '—'}
          </div>
        </div>
        <div style="display:flex;gap:8px;align-items:center">
          <div style="text-align:center"><div style="font-size:9px;color:var(--text3)">Response</div><div style="font-size:13px;color:var(--cyan)">${p.poll_ms}ms</div></div>
          ${sensorDeleteButtonHtml}
        </div>
      </div>
    </div>

    <!-- کارت‌های دما و رطوبت در یک detail-grid -->
    <div class="detail-grid" style="margin-bottom:14px">
      <div class="section">
        <div class="section-title">🌡️ دما</div>
        ${tempPorts.map(item => sensorRow(`پورت ${item.port}`, item.value, '°C', item.status, item.port)).join('') || '<span style="color:var(--text3)">داده‌ای موجود نیست</span>'}
      </div>
      <div class="section">
        <div class="section-title">💧 رطوبت</div>
        ${humPorts.map(item => sensorRow(`پورت ${item.port}`, item.value, '%', item.status, item.port)).join('') || '<span style="color:var(--text3)">داده‌ای موجود نیست</span>'}
      </div>
    </div>

    <div class="section" style="margin-bottom:14px">
      <div class="section-title">📈 میانگین روزانه دما و رطوبت (۳۰ روز اخیر)</div>
      <div style="position:relative;height:280px;">
        <canvas id="printer-daily-chart-${p.ip.replace(/\./g,'-')}" class="printer-daily-chart-canvas" data-chart-type="sensor" style="width:100%;height:100%;"></canvas>
      </div>
    </div>

    <!-- اطلاعات دستگاه -->
    <div class="section" style="margin-bottom:14px">
      <div class="section-title">📋 اطلاعات دستگاه</div>
      <div class="info-grid">
        ${[
          ['مدل', dev.model || 'ECS100G'],
          ['سریال', dev.serial || '—'],
          ['Firmware', dev.firmware || '—'],
          ['Uptime', dev.uptime_str || '—'],
          ['آخرین بروزرسانی', p.last_poll ? new Date(p.last_poll).toLocaleTimeString('fa-IR') : '—']
        ].map(([label, value]) => `
          <div class="info-item">
            <span class="info-label">${label}</span>
            <span class="info-value">${value}</span>
          </div>
        `).join('')}
      </div>
    </div>

    <!-- لاگ رویدادها -->
    <div class="section" style="margin-top:14px">
      <div class="section-title">📋 رویدادهای دستگاه</div>
      <div class="log-toolbar">
        <div class="log-filter" id="filter-${p.ip.replace(/\./g,'-')}">
          <button class="filter-btn active" onclick="filterLog('all',this,'plog-${p.ip.replace(/\./g,'-')}')">همه</button>
          <button class="filter-btn" onclick="filterLog('ALERT',this,'plog-${p.ip.replace(/\./g,'-')}')">هشدار</button>
        </div>
        <span class="log-count" id="plog-${p.ip.replace(/\./g,'-')}-count"></span>
        ${sensorLogActionsHtml}
      </div>
      <div id="plog-${p.ip.replace(/\./g,'-')}"></div>
    </div>`;
}

// ══════════════════════════════════════════════════
// LOG RENDER (بدون تغییر)
// ══════════════════════════════════════════════════
function _buildRows(events, hasPrinter, hasUser) {
  const SEV = {error:'sev-error',warning:'sev-warning',success:'sev-success',info:'sev-info'};
  return events.map(e => {
    const ts     = (e.timestamp || '').slice(0, 19).replace('T', ' ');
    const sev    = e.severity || 'info';
    const badge  = `<span class="sev-badge ${SEV[sev] || 'sev-info'}">${escapeHtml(sev.toUpperCase())}</span>`;
    const tbadge = `<span class="type-badge">${escapeHtml(e.type || '—')}</span>`;
    
    const pCell = hasPrinter
      ? `<td>${escapeHtml(e.printer_name || '—')}<br><span style="color:var(--text3);font-size:9px">${escapeHtml(e.printer_ip || '—')}</span></td>`
      : '';

    const uCell = hasUser
      ? `<td style="font-family:var(--mono);font-size:10px">${escapeHtml(e.username || '—')}</td>`
      : '';

    let message = e.message ? escapeHtml(e.message) : '—';
    let pages   = (e.pages !== undefined && e.pages !== null && e.pages !== '') ? escapeHtml(String(e.pages)) : '—';
    let color   = e.color ? escapeHtml(e.color) : '—';
    let code    = e.code ? escapeHtml(e.code) : '—';

    let pages_display = pages;
    if (e.type === 'PRINT' && pages !== '—' && !isNaN(parseInt(pages))) {
      if (e.paper_size && e.paper_size !== '') {
        pages_display = `${pages} (${e.paper_size})`;
      }
    }

    return `<tr data-type="${escapeHtml(e.type || '')}">
      <td style="direction:ltr">${escapeHtml(ts)}</td>
      ${pCell}
      <td>${tbadge}</td>
      <td style="color:var(--text)">${message}</td>
      <td class="num">${pages_display}</td>
      <td>${color}</td>
      ${uCell}
      <td>${code}</td>
      <td>${badge}</td>
    </tr>`;
  }).join('');
}
function _buildPagination(containerId, total, page, pageSize) {
  if (total <= pageSize) return '';
  const totalPages = Math.ceil(total / pageSize);
  if (totalPages <= 1) return '';

  const start = (page - 1) * pageSize + 1;
  const end   = Math.min(page * pageSize, total);

  const show = new Set([1, totalPages, page, page-1, page+1, page-2, page+2]
    .filter(p => p >= 1 && p <= totalPages));
  const pages = [...show].sort((a,b) => a-b);

  let btns = '';
  let prev = 0;
  for (const p of pages) {
    if (prev && p - prev > 1) btns += `<span class="pg-dots">…</span>`;
    btns += `<button class="pg-btn${p===page?' active':''}"
      onclick="_pgGoto('${containerId}',${p})">${p}</button>`;
    prev = p;
  }

  return `<div class="log-pagination">
    <button class="pg-btn pg-arrow" onclick="_pgGoto('${containerId}',${page-1})"
      ${page<=1?'disabled':''}>&#8249;</button>
    ${btns}
    <button class="pg-btn pg-arrow" onclick="_pgGoto('${containerId}',${page+1})"
      ${page>=totalPages?'disabled':''}>&#8250;</button>
    <span class="pg-info">${start}–${end} از ${total}</span>
  </div>`;
}

function _pgRender(containerId, counterId) {
  const st  = _pgState[containerId];
  const el  = document.getElementById(containerId);
  const cnt = document.getElementById(counterId);
  if (!st || !el) return;

  const filtered = st.filterType === 'all'
    ? st.events
    : st.events.filter(e => e.type === st.filterType);

  if (!filtered.length) {
    el.innerHTML = '<div class="log-empty">رویدادی یافت نشد</div>';
    if (cnt) cnt.textContent = '';
    return;
  }

  const totalPages = Math.ceil(filtered.length / PAGE_SIZE);
  if (st.page > totalPages) st.page = totalPages;

  const slice      = filtered.slice((st.page-1)*PAGE_SIZE, st.page*PAGE_SIZE);
  const hasPrinter = st.events.some(e => e.printer_name);
  const hasUser    = st.events.some(e => e.username);

  const tableHTML = `<table class="log-table"><thead><tr>
    <th>زمان</th>${hasPrinter?'<th>دستگاه</th>':''}
    <th>نوع</th><th>پیام</th><th>صفحات</th><th>رنگ</th>${hasUser?'<th>کاربر/سیستم</th>':''}<th>کد</th><th>اهمیت</th>
   </thead><tbody>${_buildRows(slice, hasPrinter, hasUser)}</tbody></table>`;

  const pgHTML = _buildPagination(containerId, filtered.length, st.page, PAGE_SIZE);

  el.innerHTML = tableHTML + pgHTML;
  if (cnt) cnt.textContent = `${filtered.length} رویداد — صفحه ${st.page} از ${Math.ceil(filtered.length/PAGE_SIZE)}`;
}

function _pgGoto(containerId, page) {
  const st = _pgState[containerId];
  if (!st) return;
  const filtered = st.filterType === 'all'
    ? st.events
    : st.events.filter(e => e.type === st.filterType);
  const totalPages = Math.ceil(filtered.length / PAGE_SIZE);
  st.page = Math.max(1, Math.min(page, totalPages));
  const el = document.getElementById(containerId);
  if (el) el.scrollIntoView({behavior:'smooth', block:'nearest'});
  _pgRender(containerId, st.counterId);
}

function renderLogTable(events, containerId, counterId) {
  _pgState[containerId] = {
    events:     events,
    page:       1,
    filterType: _pgState[containerId]?.filterType || 'all',
    counterId:  counterId,
  };
  _pgRender(containerId, counterId);
}

function renderGlobalLog(events) {
  const curPage = _pgState['global-log']?.page || 1;
  _pgState['global-log'] = {
    events:     events,
    page:       curPage,
    filterType: _pgState['global-log']?.filterType || 'all',
    counterId:  'global-log-count',
  };
  _pgRender('global-log', 'global-log-count');
}

function filterLog(type, btn, containerId) {
  const parent = btn.closest('.log-filter');
  if (parent) {
    parent.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
  }
  const st = _pgState[containerId];
  if (!st) return;
  st.filterType = type;
  st.page       = 1;
  _pgRender(containerId, st.counterId);
}

// ══════════════════════════════════════════════════
// DATE FILTER
// ══════════════════════════════════════════════════
async function applyDateFilter(ip) {
  const { start, end } = getLogRange(ip);
  let url = `${API}/api/logs/all?limit=10000`;
  if (start) url += `&start=${encodeURIComponent(start)}`;
  if (end) url += `&end=${encodeURIComponent(end)}`;
  if (ip) url += `&ip=${encodeURIComponent(ip)}`;

  try {
    const response = await fetch(url, { cache: 'no-store' });
    if (!response.ok) throw new Error('خطا در دریافت لاگ‌ها');
    const data = await response.json();
    const events = data.events || [];

    if (ip) {
      const logId = 'plog-' + ip.replace(/\./g,'-');
      renderLogTable(events, logId, logId+'-count');
    } else {
      renderLogTable(events, 'global-log', 'global-log-count');
    }
    if (_pgState['global-log']) _pgState['global-log'].page = 1;
    toast(`${events.length} رویداد یافت شد`, 's');
  } catch (e) {
    console.error(e);
    toast('خطا در اعمال فیلتر', 'e');
  }
}

// ══════════════════════════════════════════════════
// ACCESS PANEL
// ══════════════════════════════════════════════════
function renderAccessPanel(st) {
  const panel = document.getElementById('access-panel');
  const grid = document.getElementById('access-grid');
  if (!panel || !grid) return;

  // پاک کردن محتوای قبلی
  grid.innerHTML = '';

  // استفاده از آدرس فعلی صفحه (پروتکل + هاست) به جای IP ثابت
  const baseUrl = window.location.origin;   // مثال: https://khak-va-sazeh.ir یا http://172.16.25.82:5050

  const urls = [
    { title: 'داشبورد (همین صفحه)', url: baseUrl + '/' },
    { title: 'API — همه پرینترها', url: baseUrl + '/api/printers' },
    { title: 'API — رویدادها', url: baseUrl + '/api/logs/all' },
    { title: 'خروجی Excel', url: baseUrl + '/api/export/excel' },
  ];

  grid.innerHTML = urls.map(u => `
    <div class="access-card">
      <div class="access-card-title">${u.title}</div>
      <div class="access-url" onclick="copyURL(this,'${u.url}')" title="کلیک برای کپی">${u.url}</div>
      <div class="qr-hint">کلیک برای کپی آدرس</div>
    </div>
  `).join('');

  panel.style.display = 'block';
}

function copyURL(el, url) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(url).then(() => {
      el.classList.add('copied');
      el.textContent = '✓ کپی شد';
      setTimeout(() => { el.classList.remove('copied'); el.textContent = url; }, 2000);
      toast('آدرس کپی شد', 's');
    }).catch(() => {
      fallbackCopy(el, url);
    });
  } else {
    fallbackCopy(el, url);
  }
}

function fallbackCopy(el, url) {
  const textarea = document.createElement('textarea');
  textarea.value = url;
  textarea.style.position = 'fixed';
  textarea.style.opacity = '0';
  document.body.appendChild(textarea);
  textarea.focus();
  textarea.select();
  try {
    const successful = document.execCommand('copy');
    if (successful) {
      el.classList.add('copied');
      el.textContent = '✓ کپی شد';
      setTimeout(() => { el.classList.remove('copied'); el.textContent = url; }, 2000);
      toast('آدرس کپی شد', 's');
    } else {
      toast('کپی با شکست مواجه شد', 'e');
    }
  } catch (err) {
    toast('خطا در کپی', 'e');
  }
  document.body.removeChild(textarea);
}

// ══════════════════════════════════════════════════
// EXPORT FUNCTIONS
// ══════════════════════════════════════════════════
function exportExcel() {
  if (!canManage()) { toast('دسترسی ندارید', 'e'); return; }
  window.location.href = `${API}/api/export/excel`;
  toast('در حال آماده‌سازی فایل Excel...','s');
}
function exportLogExcel() {
  if (!canManage()) { toast('دسترسی ندارید', 'e'); return; }
  window.location.href = `${API}/api/export/excel`;
  toast('خروجی Excel رویدادها...','s');
}
function exportLogJSON() {
  if (!canAdmin()) { toast('دسترسی ندارید', 'e'); return; }
  const data = JSON.stringify({exported: new Date().toISOString(), events: allEvents}, null, 2);
  downloadFile(data, `toshiba_log_${dateStr()}.json`, 'application/json');
}
function exportPrinterLogExcel(ip) {
  if (!canManage()) { toast('دسترسی ندارید', 'e'); return; }
  window.location.href = `${API}/api/export/excel`;
}
function exportPrinterLogJSON(ip) {
  if (!canAdmin()) { toast('دسترسی ندارید', 'e'); return; }
  const events = allEvents.filter(e=>e.printer_ip===ip);
  const data = JSON.stringify({ip, exported:new Date().toISOString(), events}, null, 2);
  downloadFile(data, `log_${ip.replace(/\./g,'_')}_${dateStr()}.json`, 'application/json');
}
function exportJSON() {
  if (!canAdmin()) { toast('دسترسی ندارید', 'e'); return; }
  const data = JSON.stringify({exported: new Date().toISOString(), printers: allData, events: allEvents}, null, 2);
  downloadFile(data, `toshiba_report_${dateStr()}.json`, 'application/json');
}
function downloadFile(content, filename, type) {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([content],{type}));
  a.download = filename; a.click();
  toast('فایل در حال دانلود...','s');
}
function dateStr() { return new Date().toISOString().slice(0,19).replace(/[T:]/g,'-'); }

// ══════════════════════════════════════════════════
// LOG RANGE
// ══════════════════════════════════════════════════
function getLogRange(ip) {
  let start, end;
  if (ip) {
    start = document.querySelector(`.printer-log-start[data-ip="${ip}"]`)?.value;
    end = document.querySelector(`.printer-log-end[data-ip="${ip}"]`)?.value;
  } else {
    start = document.getElementById('log-start')?.value;
    end = document.getElementById('log-end')?.value;
  }
  return { start: start || null, end: end || null };
}

async function exportLogsWithRange(ip, format) {
  if (format === 'csv' && !canAdmin()) { toast('دسترسی ندارید', 'e'); return; }
  if (format !== 'csv' && !canManage()) { toast('دسترسی ندارید', 'e'); return; }
  const { start, end } = getLogRange(ip);
  let url = `${API}/api/export/logs?format=${format}`;
  if (start) url += `&start=${encodeURIComponent(start)}`;
  if (end) url += `&end=${encodeURIComponent(end)}`;
  if (ip) url += `&ip=${encodeURIComponent(ip)}`;

  window.location.href = url;
  toast(`در حال دریافت خروجی ${format}...`, 's');
}

// ══════════════════════════════════════════════════
// PRINTER MANAGEMENT
// ══════════════════════════════════════════════════
function showAddModal() {
  if (!canAdmin()) { toast('دسترسی ندارید', 'e'); return; }
  document.getElementById('modal-add').classList.add('show');
  switchAddTab('single', document.querySelector('.modal-tab'));
  document.getElementById('add-ip').focus();
}
function closeModal() {
  document.getElementById('modal-add').classList.remove('show');
  document.getElementById('discover-results').innerHTML = '';
  document.getElementById('bulk-results').innerHTML = '';
  document.getElementById('bulk-submit-btn').style.display = 'none';
  document.getElementById('bulk-input').value = '';
}

function switchAddTab(name, el) {
  document.querySelectorAll('.modal-tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.modal-pane').forEach(p => p.classList.remove('active'));
  el.classList.add('active');
  document.getElementById('pane-' + name).classList.add('active');
}

async function doAddPrinter() {
  if (!canAdmin()) { toast('دسترسی ندارید', 'e'); return; }
  const ip        = document.getElementById('add-ip').value.trim();
  const name      = document.getElementById('add-name').value.trim();
  const community = document.getElementById('add-community-single').value.trim() || 'public';
  const groupSel  = document.getElementById('add-group-select').value;
  const groupCust = document.getElementById('add-group-custom').value.trim();
  const group     = groupCust || groupSel;

  if (!ip) { toast('IP الزامی است','e'); return; }
  try {
    const r = await apiFetch(`${API}/api/printers/add`,{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({ip, name, community, group})
    });
    const j = await r.json();
    if (!r.ok) { toast(j.error||'خطا','e'); return; }
    toast(`پرینتر ${ip} اضافه شد`,'s');
    closeModal();
    setTimeout(fetchData, 2000);
  } catch(e) { toast('خطا در اتصال','e'); }
}

function parseBulkInput() {
  const lines = document.getElementById('bulk-input').value.split('\n');
  const defaultComm = document.getElementById('add-community-bulk').value.trim() || 'public';
  const existingIPs = new Set(allData.map(p => p.ip));
  const items = [];

  for (let raw of lines) {
    const line = raw.trim();
    if (!line || line.startsWith('#')) continue;
    const parts = line.split(/\s+/);
    const ip        = parts[0] || '';
    const name      = parts.slice(1, parts.length > 2 ? -1 : undefined)
                           .join(' ')
                           .replace(/['"]/g,'').trim() || '';
    const community = (parts.length > 2 && !parts[parts.length-1].includes('.'))
                      ? parts[parts.length-1]
                      : defaultComm;
    if (!ip) continue;
    items.push({
      ip, name, community,
      exists: existingIPs.has(ip),
      valid: /^\d{1,3}(\.\d{1,3}){3}$/.test(ip),
    });
  }
  return items;
}

function doBulkPreview() {
  const items = parseBulkInput();
  const container = document.getElementById('bulk-results');
  const btn = document.getElementById('bulk-submit-btn');

  if (!items.length) {
    container.innerHTML = '<div style="color:var(--text3);font-family:var(--mono);font-size:11px;padding:8px">هیچ آیتمی پیدا نشد</div>';
    btn.style.display = 'none';
    return;
  }

  const newCount = items.filter(x => x.valid && !x.exists).length;
  container.innerHTML = items.map(it => {
    if (!it.valid)
      return `<div class="bulk-result-row err">✗ ${it.ip} &nbsp;<span style="color:var(--text3)">فرمت IP نامعتبر</span></div>`;
    if (it.exists)
      return `<div class="bulk-result-row skip">⏭ ${it.ip} &nbsp;<span style="color:var(--text3)">قبلاً موجود است</span></div>`;
    return `<div class="bulk-result-row ok">✓ ${it.ip} &nbsp;<span style="color:var(--text2)">${it.name||'—'}</span> &nbsp;<span style="color:var(--text3)">[${it.community}]</span></div>`;
  }).join('');

  btn.style.display = newCount ? 'inline-flex' : 'none';
  if (newCount) btn.textContent = `＋ افزودن ${newCount} پرینتر`;
}

async function doBulkAdd() {
  if (!canAdmin()) { toast('دسترسی ندارید', 'e'); return; }
  const items = parseBulkInput().filter(x => x.valid && !x.exists);
  if (!items.length) { toast('هیچ پرینتر جدیدی وجود ندارد','w'); return; }

  const btn = document.getElementById('bulk-submit-btn');
  btn.disabled = true; btn.textContent = '⏳ در حال افزودن...';

  try {
    const r = await apiFetch(`${API}/api/printers/bulk-add`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        printers: items.map(({ip,name,community}) => ({ip,name,community})),
        scan: true,
        skip_existing: true,
      })
    });
    const j = await r.json();
    const added   = j.total_added || 0;
    const skipped = (j.skipped || []).length;
    const failed  = (j.failed  || []).length;

    let msg = '';
    if (added)   msg += `${added} پرینتر اضافه شد `;
    if (skipped) msg += `| ${skipped} تکراری `;
    if (failed)  msg += `| ${failed} خطا`;
    toast(msg.trim() || 'انجام شد', added ? 's' : 'w');

    if (added) { closeModal(); setTimeout(fetchData, 3000); }
    else { btn.disabled = false; btn.textContent = `＋ افزودن (${items.length})`; }
  } catch(e) {
    toast('خطا در اتصال','e');
    btn.disabled = false;
  }
}

async function removePrinter(ip, name) {
  if (!canAdmin()) { toast('دسترسی ندارید', 'e'); return; }
  if (!confirm(`پرینتر ${name} (${ip}) حذف شود؟`)) return;
  try {
    const r = await apiFetch(`${API}/api/printers/remove`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ip})});
    if (r.ok) {
      toast(`پرینتر ${ip} حذف شد`,'w');
      const panel = document.getElementById('panel-' + ip.replace(/\./g,'-'));
      if (panel) panel.remove();
      const tab = document.querySelector(`[data-tab="${ip}"]`);
      switchTab('overview');
      setTimeout(fetchData, 500);
    }
  } catch(e) { toast('خطا','e'); }
}

function addDiscoveryRange() {
  const container = document.getElementById('discovery-ranges');
  const newRow = document.createElement('div');
  newRow.className = 'range-row';
  newRow.innerHTML = `
    <input class="form-input" name="subnet[]" placeholder="172.16.0" value="" style="flex:2">
    <input class="form-input" name="start[]" placeholder="1" value="1" style="flex:1;direction:ltr">
    <input class="form-input" name="end[]" placeholder="254" value="254" style="flex:1;direction:ltr">
    <button type="button" class="btn" style="padding:5px 8px;" onclick="this.parentElement.remove()">✖</button>
  `;
  container.appendChild(newRow);
}

async function doDiscover() {
  if (!canAdmin()) { toast('دسترسی ندارید', 'e'); return; }
  const community = document.getElementById('add-community-discover').value.trim() || 'public';
  const rangeRows = document.querySelectorAll('#discovery-ranges .range-row');
  const ranges = [];
  
  for (let row of rangeRows) {
    const subnet = row.querySelector('input[name="subnet[]"]').value.trim();
    const start = row.querySelector('input[name="start[]"]').value.trim();
    const end = row.querySelector('input[name="end[]"]').value.trim();
    if (subnet && start && end) {
      ranges.push({
        subnet: subnet,
        start: parseInt(start),
        end: parseInt(end)
      });
    }
  }
  
  if (ranges.length === 0) {
    toast('حداقل یک رنج وارد کنید', 'e');
    return;
  }

  const dr = document.getElementById('discover-results');
  dr.innerHTML = `<div style="text-align:center;padding:20px;color:var(--text3);font-family:var(--mono)">🔍 در حال جستجو در ${ranges.length} رنج...</div>`;

  try {
    const r = await apiFetch(`${API}/api/printers/discover`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ranges, community })
    });
    const j = await r.json();
    if (!j.found.length) {
      dr.innerHTML = '<div style="text-align:center;padding:16px;color:var(--text3);font-family:var(--mono)">هیچ دستگاهی پیدا نشد</div>';
      return;
    }
    const existingIPs = new Set(allData.map(p => p.ip));
    const newDevices  = j.found.filter(f => !existingIPs.has(f.ip));
    const existDevices= j.found.filter(f =>  existingIPs.has(f.ip));
    const allItems    = [...newDevices, ...existDevices];

    dr.innerHTML = allItems.map(f => {
      const isExist = existingIPs.has(f.ip);
      return `
      <div class="discover-item${isExist ? ' discover-existing' : ''}">
        <div>
          <div class="discover-ip">${escapeHtml(f.ip)}
            ${isExist ? '<span class="discover-badge">موجود</span>' : ''}
          </div>
          <div class="discover-model">${escapeHtml(f.model || '')}</div>
        </div>
        ${isExist
          ? '<span style="font-size:10px;color:var(--text3);font-family:var(--mono)">قبلاً اضافه شده</span>'
          : `<button class="btn btn-green" onclick="quickAdd(event, '${escapeHtml(f.ip)}','${escapeHtml((f.model||'').slice(0,20))}')">＋ افزودن</button>`
        }
      </div>`;
    }).join('');

    const msg = newDevices.length
      ? `${newDevices.length} دستگاه جدید یافت شد${existDevices.length ? ` (+${existDevices.length} موجود)` : ''}`
      : `هیچ دستگاه جدیدی یافت نشد (${existDevices.length} دستگاه موجود)`;
    toast(msg, newDevices.length ? 's' : 'w');
  } catch (e) {
    dr.innerHTML = '<div style="color:var(--red);padding:12px;font-family:var(--mono)">خطا در جستجو</div>';
  }
}

async function quickAdd(event, ip, model) {
  // جلوگیری از بسته شدن مودال
  event.stopPropagation();
  
  const community = document.getElementById('add-community-discover').value.trim() || 'public';
  const btn = event.target;  // دکمه‌ای که کلیک شده است
  
  // غیرفعال کردن دکمه در حین درخواست
  btn.disabled = true;
  btn.textContent = '⏳';
  
  try {
    const r = await apiFetch(`${API}/api/printers/add`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ip, name: model, community })
    });
    const j = await r.json();
    
    if (r.ok) {
      toast(`${ip} اضافه شد`, 's');
      // تغییر ظاهر دکمه به "موجود"
      btn.textContent = '✓ اضافه شد';
      btn.className = 'btn btn-orange';
      btn.disabled = true;
      
      // به‌روزرسانی داده‌ها در پس‌زمینه
      fetchData();
    } else {
      toast(j.error || 'خطا', 'e');
      btn.textContent = '＋ افزودن';
      btn.disabled = false;
    }
  } catch (e) {
    toast('خطا در اتصال', 'e');
    btn.textContent = '＋ افزودن';
    btn.disabled = false;
  }
}

function openClearLogsModal(ip = null) {
  if (!canAdmin()) { toast('دسترسی ندارید', 'e'); return; }
  document.getElementById('clear-logs-ip').value = ip || '';
  document.getElementById('modal-clear-logs').classList.add('show');
}

function closeClearLogsModal() {
  document.getElementById('modal-clear-logs').classList.remove('show');
}

async function doClearLogs() {
  const ip = document.getElementById('clear-logs-ip').value;
  const checkboxes = document.querySelectorAll('#clear-logs-types input[type="checkbox"]:checked');
  const selectedTypes = Array.from(checkboxes).map(cb => cb.value);

  if (selectedTypes.length === 0) {
    toast('حداقل یک نوع رویداد را انتخاب کنید', 'w');
    return;
  }

  const confirmMsg = ip 
    ? `تمامی رویدادهای انتخاب شده برای پرینتر ${ip} حذف شوند؟`
    : `تمامی رویدادهای انتخاب شده برای "همه پرینترها" حذف شوند؟`;

  if (!confirm(confirmMsg)) return;

  const btn = document.querySelector('[onclick="doClearLogs()"]');
  btn.disabled = true; btn.textContent = 'در حال حذف...';

  try {
    const r = await apiFetch(`${API}/api/logs/clear`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ip: ip || null, types: selectedTypes })
    });
    
    if (r.ok) {
      const j = await r.json();
      toast(`${fmtN(j.deleted || 0)} رویداد با موفقیت پاک شد`, 's');
      closeClearLogsModal();
      fetchData();
    } else {
      toast('خطا در پاکسازی لاگ‌ها', 'e');
    }
  } catch(e) {
    toast('خطا در اتصال به سرور', 'e');
  } finally {
    btn.disabled = false; btn.textContent = '× حذف انتخاب شده‌ها';
  }
}

async function clearLogs() { openClearLogsModal(); }
async function clearPrinterLog(ip) { openClearLogsModal(ip); }

// ══════════════════════════════════════════════════
// NICKNAME EDITING
// ══════════════════════════════════════════════════
let _nicknameCallback = null;
let _nicknameIp = null;

function openNicknameModal(ip, currentNickname, callback) {
  _nicknameIp = ip;
  _nicknameCallback = callback;
  const input = document.getElementById('nickname-input');
  input.value = currentNickname || '';
  input.placeholder = 'نام مستعار جدید...';
  const modal = document.getElementById('modal-nickname');
  modal.classList.add('show');
  input.focus();
  input.select();
}

function closeNicknameModal() {
  document.getElementById('modal-nickname').classList.remove('show');
  _nicknameIp = null;
  _nicknameCallback = null;
}

function bindNicknameModalEvents() {
  const saveBtn = document.getElementById('nickname-save');
  const cancelBtn = document.getElementById('nickname-cancel');
  const modalOverlay = document.getElementById('modal-nickname');
  
  if (saveBtn) {
    saveBtn.onclick = () => {
      const newValue = document.getElementById('nickname-input').value.trim();
      if (_nicknameCallback) _nicknameCallback(_nicknameIp, newValue);
      closeNicknameModal();
    };
  }
  if (cancelBtn) {
    cancelBtn.onclick = () => closeNicknameModal();
  }
  if (modalOverlay) {
    modalOverlay.onclick = (e) => {
      if (e.target === modalOverlay) closeNicknameModal();
    };
  }
  const inputField = document.getElementById('nickname-input');
  if (inputField) {
    inputField.onkeypress = (e) => {
      if (e.key === 'Enter' && saveBtn) saveBtn.click();
    };
  }
}

async function editNickname(ip, currentNickname) {
  if (!canAdmin()) { toast('دسترسی ندارید', 'e'); return; }
  openNicknameModal(ip, currentNickname, async (ip, newNick) => {
    try {
      const r = await apiFetch(`${API}/api/printer/${ip}/rename`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ nickname: newNick })
      });
      const j = await r.json();
      if (r.ok) {
        toast(newNick ? 'نام مستعار تغییر کرد' : 'نام مستعار حذف شد', 's');
        fetchData();
      } else {
        toast(j.error || 'خطا', 'e');
      }
    } catch(e) {
      toast('خطا در اتصال', 'e');
    }
  });
}

// ══════════════════════════════════════════════════
// MANUAL EVENT MODAL
// ══════════════════════════════════════════════════
const EV_CONFIG = {
  SERVICE:   { icon:'🔧', iconCls:'service',   title:'ثبت سرویس دستگاه',  color:'var(--cyan)',   btnCls:'btn-cyan',   placeholder:'مثال: تنظیم فیدر کاغذ، تمیزکاری لیزر، تعویض بلت...' },
  REFILL: { icon:'🖨', iconCls:'cartridge', title:'شارژ / تعویض کارتریج', color:'var(--yellow)', btnCls:'btn-yellow', placeholder:'مثال: تعویض تونر مشکی، شارژ سیان، تعویض درام...' },
};

function openEvModal(ip, type) {
  if (!canAdmin()) { toast('دسترسی ندارید', 'e'); return; }
  const cfg = EV_CONFIG[type];
  if (!cfg) return;
  const printer = allData.find(p => p.ip === ip);
  const printerLabel = printer ? `${printer.name} (${ip})` : ip;

  document.getElementById('ev-ip').value     = ip;
  document.getElementById('ev-type').value   = type;
  document.getElementById('ev-modal-icon').textContent  = cfg.icon;
  document.getElementById('ev-modal-icon').className    = `ev-modal-icon ${cfg.iconCls}`;
  document.getElementById('ev-modal-title').textContent = cfg.title;
  document.getElementById('ev-modal-sub').textContent   = printerLabel;
  document.getElementById('ev-notes').placeholder       = cfg.placeholder;
  document.getElementById('ev-notes').value             = '';
  document.getElementById('ev-tech').value              = '';

  const btn = document.getElementById('ev-submit-btn');
  btn.className = `btn ${cfg.btnCls}`;
  btn.textContent = '✓ ثبت رویداد';
  btn.disabled = false;

  document.getElementById('ev-modal').classList.add('show');
  setTimeout(() => document.getElementById('ev-notes').focus(), 100);
}

function closeEvModal() {
  document.getElementById('ev-modal').classList.remove('show');
}

async function submitManualEvent() {
  if (!canAdmin()) { toast('دسترسی ندارید', 'e'); return; }
  const ip    = document.getElementById('ev-ip').value;
  const type  = document.getElementById('ev-type').value;
  const notes = document.getElementById('ev-notes').value.trim();
  const tech  = document.getElementById('ev-tech').value.trim();

  if (!notes) {
    document.getElementById('ev-notes').focus();
    toast('توضیحات را وارد کنید', 'e');
    return;
  }

  let fullMessage = notes;
  if (tech) fullMessage += ` (تکنسین: ${tech})`;

  const btn = document.getElementById('ev-submit-btn');
  btn.disabled = true;
  btn.textContent = '⏳ در حال ثبت...';

  try {
    const r = await apiFetch(`${API}/api/events/manual`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ip, type, notes, technician: tech }),
    });
    const j = await r.json();
    if (!r.ok) {
      toast(j.error || 'خطا در ثبت رویداد', 'e');
      btn.disabled = false;
      btn.textContent = '✓ ثبت رویداد';
      return;
    }
    const label = type === 'SERVICE' ? 'سرویس' : 'شارژ کارتریج';
    toast(`✓ ${label} ثبت شد`, 's');
    closeEvModal();
    await fetchData();
    const logEl = document.getElementById('plog-' + ip.replace(/\./g,'-'));
    if (logEl) logEl.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  } catch(e) {
    toast('خطا در اتصال به سرور', 'e');
    btn.disabled = false;
    btn.textContent = '✓ ثبت رویداد';
  }
}

// ══════════════════════════════════════════════════
// POLL & COUNTDOWN
// ══════════════════════════════════════════════════
async function triggerPoll() {
  try {
    const res = await apiFetch(`${API}/api/poll/now`, { method: 'POST' });
    let data = {};
    try {
      data = await res.json();
    } catch (_) {
      data = {};
    }

    if (!res.ok) {
      if (data.error === 'csrf_token_invalid') {
        throw new Error('SESSION_EXPIRED');
      }
      throw new Error(data.error || data.message || `HTTP ${res.status}`);
    }

    if (data.status !== 'started') {
      throw new Error(data.error || 'Unexpected response');
    }
    toast('Pull started', 's');
    setTimeout(fetchData, 2500);
  } catch (err) {
    console.error('Pull failed:', err);
    if (String(err?.message || err) === 'SESSION_EXPIRED') {
      toast('نشست شما منقضی شده است. صفحه را تازه کنید و دوباره تلاش کنید.', 'e');
    } else {
      toast('خطا در شروع Pull', 'e');
    }
  }
}

function resetCountdown() {
  countdown = 40;
  if (countTimer) clearInterval(countTimer);
  countTimer = setInterval(()=>{
    countdown = Math.max(0, countdown-1);
    document.getElementById('cfill').style.width = (countdown/40*100)+'%';
    if (countdown<=0) { clearInterval(countTimer); fetchData(); }
  },1000);
}

// ══════════════════════════════════════════════════
// UI HELPERS
// ══════════════════════════════════════════════════
function setDot(s) {
  const d = document.getElementById('dot');
  const t = document.getElementById('dot-txt');
  d.className = 'dot-live';
  if (s==='on')    { d.classList.add('on'); t.textContent='Live'; }
  else if(s==='fetch'){ d.classList.add('fetch'); t.textContent='Pulling...'; }
  else             { d.style.background='var(--red)'; t.textContent='Error'; }
}

let toastT;
function toast(msg, cls='') {
  const el=document.getElementById('toast'); el.textContent=msg; el.className=`show ${cls}`;
  clearTimeout(toastT); toastT=setTimeout(()=>el.className='',3000);
}

function fmtN(n) {
  if(n==null||n===undefined) return '—';
  return Number(n).toLocaleString('en');
}

function hasNumericCounterValue(value) {
  return typeof value === 'number' && Number.isFinite(value);
}

function _extraCountersStorageKey(ip) {
  return `${EXTRA_COUNTERS_ACCORDION_KEY}:${ip}`;
}

function getExtraCountersAccordionState(ip) {
  try {
    return localStorage.getItem(_extraCountersStorageKey(ip)) === '1';
  } catch (e) {
    return false;
  }
}

function setExtraCountersAccordionState(ip, isOpen) {
  try {
    localStorage.setItem(_extraCountersStorageKey(ip), isOpen ? '1' : '0');
  } catch (e) {
    console.warn('extra counters accordion state not persisted:', e);
  }
}

function toggleExtraCountersAccordion(ip) {
  const safeIp = String(ip || '').replace(/\./g, '-');
  const body = document.getElementById(`extra-counters-body-${safeIp}`);
  const header = document.getElementById(`extra-counters-header-${safeIp}`);
  if (!body || !header) return;
  const shouldOpen = !body.classList.contains('open');
  body.classList.toggle('open', shouldOpen);
  header.classList.toggle('open', shouldOpen);
  setExtraCountersAccordionState(ip, shouldOpen);
}

const EXTRA_COUNTER_HELP = {
  'Printer': {
    title: 'تعداد چاپ مستقیم',
    subtitle: 'چاپ‌های ارسال‌شده از رایانه یا شبکه'
  },
  'Copy': {
    title: 'تعداد کپی',
    subtitle: 'کپی‌های گرفته‌شده از روی دستگاه'
  },
  'Fax': {
    title: 'تعداد فکس',
    subtitle: 'فکس‌های ثبت‌شده توسط دستگاه'
  },
  'List': {
    title: 'تعداد لیست/گزارش',
    subtitle: 'چاپ گزارش‌ها یا صفحات سیستمی دستگاه'
  },
  'Scan FC': {
    title: 'اسکن رنگی محلی',
    subtitle: 'تعداد اسکن‌های رنگی انجام‌شده از روی خود دستگاه'
  },
  'Scan BW': {
    title: 'اسکن سیاه‌وسفید محلی',
    subtitle: 'تعداد اسکن‌های سیاه‌وسفید انجام‌شده از روی خود دستگاه'
  },
  'Net Scan FC': {
    title: 'اسکن رنگی شبکه‌ای',
    subtitle: 'تعداد اسکن‌های رنگی ارسال‌شده از طریق شبکه یا مقصدهای راه دور'
  },
  'Net Scan BW': {
    title: 'اسکن سیاه‌وسفید شبکه‌ای',
    subtitle: 'تعداد اسکن‌های سیاه‌وسفید ارسال‌شده از طریق شبکه یا مقصدهای راه دور'
  }
};

function renderExtraCounterLabel(label) {
  const meta = EXTRA_COUNTER_HELP[label];
  if (!meta) {
    return `<span class="counter-help-main">${escapeHtml(label)}</span>`;
  }
  const tooltipText = `${meta.title} — ${meta.subtitle}`;
  return `
    <span class="counter-help-label">
      <span class="counter-help-topline">
        <span class="counter-help-main">${escapeHtml(label)}</span>
        <span class="counter-help-icon" title="${escapeHtml(tooltipText)}" aria-label="راهنمای ${escapeHtml(label)}">❓</span>
      </span>
      <span class="counter-help-sub">${escapeHtml(meta.title)}</span>
    </span>`;
}

// ══════════════════════════════════════════════════
// KEYBOARD SHORTCUTS
// ══════════════════════════════════════════════════
document.addEventListener('keydown',e=>{
  if(e.key==='Escape') { closeModal(); closeEvModal(); closeEditModal(); closeImportModal(); closeClearLogsModal(); }
  if(e.key==='r'&&(e.ctrlKey||e.metaKey)&&!e.shiftKey){ e.preventDefault(); triggerPoll(); }
});
document.getElementById('modal-add').addEventListener('click',e=>{ if(e.target===document.getElementById('modal-add')) closeModal(); });
document.getElementById('ev-modal').addEventListener('click',e=>{ if(e.target===document.getElementById('ev-modal')) closeEvModal(); });

// ══════════════════════════════════════════════════
// DAILY CHART
// ══════════════════════════════════════════════════
function populatePrinterSelect() {
  const select = document.getElementById('chart-printer-select');
  if (!select) return;
  select.innerHTML = '<option value="">همه پرینترها</option>';
  allData.forEach(p => {
    select.innerHTML += `<option value="${p.ip}">${p.name} (${p.ip})</option>`;
  });
}

async function loadDailyChart() {
  const select = document.getElementById('chart-printer-select');
  const canvas = document.getElementById('dailyChart');
  
  if (!select || !canvas) {
    console.error('Chart elements not found');
    return;
  }
  
  const ip = select.value;
  let url = `${API}/api/stats/daily?days=30`;
  if (ip) url += `&ip=${encodeURIComponent(ip)}`;
  
  try {
    const res = await fetch(url, {cache: 'no-store'});
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    
    if (!data.dates || !data.totals || data.dates.length === 0) {
      toast('داده‌ای برای نمایش وجود ندارد', 'w');
      return;
    }
    
    if (chartInstance) {
      try {
        chartInstance.destroy();
      } catch (e) {
        console.warn('Chart destroy warning:', e);
      }
      chartInstance = null;
    }
    
    const persianDates = data.dates.map(d => {
      try {
        return new Date(d).toLocaleDateString('fa-IR', {
          month: 'short',
          day: 'numeric'
        });
      } catch {
        return d;
      }
    });
    
    const ctx = canvas.getContext('2d');
    chartInstance = new Chart(ctx, {
      type: 'bar',
      data: {
        labels: persianDates,
        datasets: [{
          label: 'تعداد صفحات چاپ شده',
          data: data.totals,
          backgroundColor: 'rgba(0, 212, 255, 0.6)',
          borderColor: '#00d4ff',
          borderWidth: 2,
          borderRadius: 4,
          hoverBackgroundColor: 'rgba(0, 212, 255, 0.8)'
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: {
            display: true,
            position: 'top',
            labels: {
              color: '#e4e8f0',
              font: {
                family: 'Noto Sans Arabic',
                size: 12
              }
            }
          },
          tooltip: {
            backgroundColor: 'rgba(20, 23, 32, 0.95)',
            titleColor: '#00d4ff',
            bodyColor: '#e4e8f0',
            borderColor: '#2a3248',
            borderWidth: 1,
            padding: 12,
            displayColors: false,
            callbacks: {
              label: function(context) {
                return `چاپ: ${context.parsed.y.toLocaleString('fa-IR')} صفحه`;
              }
            }
          }
        },
        scales: {
          y: {
            beginAtZero: true,
            grid: {
              color: 'rgba(42, 50, 72, 0.5)',
              drawBorder: false
            },
            ticks: {
              color: '#7b86a0',
              font: {
                family: 'JetBrains Mono',
                size: 10
              },
              callback: function(value) {
                return value.toLocaleString('fa-IR');
              }
            }
          },
          x: {
            grid: {
              display: false
            },
            ticks: {
              color: '#7b86a0',
              font: {
                family: 'Noto Sans Arabic',
                size: 10
              },
              maxRotation: 45,
              minRotation: 45
            }
          }
        },
        animation: {
          duration: 750,
          easing: 'easeInOutQuart'
        }
      }
    });
    
    toast(`نمودار ${data.dates.length} روز نمایش داده شد`, 's');
    
  } catch (e) {
    console.error('Chart error:', e);
    toast('خطا در بارگذاری نمودار: ' + e.message, 'e');
  }
}

async function loadPrinterDailyChart(ip) {
  if (!ip) {
    console.error('loadPrinterDailyChart called without ip');
    return;
  }

  let canvasId = `printer-daily-chart-${ip.replace(/\./g,'-')}`;
  let canvas = document.getElementById(canvasId);
  if (!canvas) {
    const panel = document.getElementById('panel-' + ip.replace(/\./g, '-'));
    if (!panel) {
      console.error('Printer detail panel not found for ip', ip);
      return;
    }
    const fallback = document.createElement('canvas');
    fallback.id = canvasId;
    fallback.className = 'printer-daily-chart-canvas';
    fallback.style.width = '100%';
    fallback.style.height = '300px';
    fallback.style.display = 'block';
    panel.appendChild(fallback);
    canvas = fallback;
  }

  if (!canvas || !canvas.parentElement) {
    console.error('Printer chart canvas missing or detached for ip', ip);
    return;
  }

  const container = canvas.parentElement;
  const chartType = canvas.dataset.chartType || 'print';
  canvas.style.width = '100%';
  canvas.style.height = '300px';
  canvas.style.display = 'block';
  const oldMessage = container.querySelector('.printer-chart-empty');
  if (oldMessage) oldMessage.remove();

  destroyPrinterDailyChart(ip);

  const url = chartType === 'sensor'
    ? `${API}/api/stats/sensor/daily?days=30&ip=${encodeURIComponent(ip)}`
    : `${API}/api/stats/daily?days=30&ip=${encodeURIComponent(ip)}`;
  console.debug('Loading printer daily chart from', url);

  try {
    const res = await fetch(url, {cache: 'no-store'});
    if (!res.ok) {
      const body = await res.text();
      console.error('Printer daily chart fetch error', res.status, body);
      throw new Error(`HTTP ${res.status}`);
    }

    const data = await res.json();
    console.debug('Printer daily chart response', data);

    const chartValuesOk = chartType === 'sensor'
      ? (Array.isArray(data.avg_temperature) || Array.isArray(data.avg_humidity))
      : Array.isArray(data.totals);
    if (!data || !Array.isArray(data.dates) || !chartValuesOk || data.dates.length === 0) {
      console.warn('Printer daily chart no data available for', ip, data);
      canvas.style.display = 'none';
      const message = document.createElement('div');
      message.className = 'printer-chart-empty';
      message.style.cssText = 'padding:18px 12px; color:var(--text3); font-size:13px; text-align:center; background:rgba(255,255,255,0.04); border:1px solid var(--border); border-radius:8px;';
      message.textContent = 'No data available';
      container.appendChild(message);
      return;
    }

    if (chartType !== 'sensor' && data.dates.length !== data.totals.length) {
      console.warn('Printer daily chart arrays length mismatch', {
        ip,
        dates: data.dates.length,
        totals: data.totals.length
      });
    }

    const persianDates = data.dates.map(d => {
      try {
        return new Date(d).toLocaleDateString('fa-IR', { month: 'short', day: 'numeric' });
      } catch {
        return d;
      }
    });

    const ctx = canvas.getContext('2d');
    if (!ctx) {
      console.error('Canvas context not available for ip', ip);
      return;
    }

    if (chartType === 'sensor') {
      const temps = Array.isArray(data.avg_temperature) ? data.avg_temperature : [];
      const hums = Array.isArray(data.avg_humidity) ? data.avg_humidity : [];
      const hasSensorData = temps.some(v => typeof v === 'number') || hums.some(v => typeof v === 'number');
      if (!hasSensorData) {
        canvas.style.display = 'none';
        const message = document.createElement('div');
        message.className = 'printer-chart-empty';
        message.style.cssText = 'padding:18px 12px; color:var(--text3); font-size:13px; text-align:center; background:rgba(255,255,255,0.04); border:1px solid var(--border); border-radius:8px;';
        message.textContent = 'هنوز داده روزانه سنسور موجود نیست';
        container.appendChild(message);
        return;
      }
      await new Promise(resolve => requestAnimationFrame(resolve));
      canvas.style.display = 'block';
      canvas.style.width = '100%';
      canvas.style.height = '300px';
      canvas.width = canvas.clientWidth;
      canvas.height = canvas.clientHeight;
      const chart = new Chart(ctx, {
        type: 'line',
        data: {
          labels: persianDates,
          datasets: [
            {
              label: 'میانگین دما (°C)',
              data: temps,
              borderColor: '#00d4ff',
              backgroundColor: 'rgba(0,212,255,0.12)',
              tension: 0.35,
              spanGaps: true,
              yAxisID: 'yTemp'
            },
            {
              label: 'میانگین رطوبت (%)',
              data: hums,
              borderColor: '#64b5f6',
              backgroundColor: 'rgba(100,181,246,0.10)',
              tension: 0.35,
              spanGaps: true,
              yAxisID: 'yHum'
            }
          ]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            legend: { labels: { color: '#c8d1e5', font: { family: 'Noto Sans Arabic', size: 11 } } },
            tooltip: {
              backgroundColor: 'rgba(20, 23, 32, 0.95)',
              titleColor: '#00d4ff',
              bodyColor: '#e4e8f0',
              borderColor: '#2a3248',
              borderWidth: 1,
              callbacks: {
                label: function(context) {
                  const unit = context.dataset.yAxisID === 'yTemp' ? '°C' : '%';
                  return `${context.dataset.label}: ${context.parsed.y}${unit}`;
                }
              }
            }
          },
          scales: {
            yTemp: { type: 'linear', position: 'left', grid: { color: 'rgba(42,50,72,.35)' }, ticks: { color: '#00d4ff' } },
            yHum: { type: 'linear', position: 'right', min: 0, max: 100, grid: { drawOnChartArea: false }, ticks: { color: '#64b5f6' } },
            x: { grid: { display: false }, ticks: { color: '#7b86a0', maxRotation: 45, minRotation: 45 } }
          }
        }
      });
      printerChartInstances[ip] = chart;
      return;
    }

    await new Promise(resolve => requestAnimationFrame(resolve));
    await new Promise(resolve => setTimeout(resolve, 20));
    canvas.style.display = 'block';
    canvas.style.width = '100%';
    canvas.style.height = '300px';
    canvas.style.backgroundColor = 'rgba(255,255,255,0.02)';
    canvas.style.border = '1px solid rgba(255,255,255,0.08)';
    canvas.style.borderRadius = '8px';
    canvas.style.boxSizing = 'border-box';
    canvas.width = canvas.clientWidth;
    canvas.height = canvas.clientHeight;
    console.debug('Creating Chart.js bar chart for ip', ip, 'canvas size', canvas.clientWidth, canvas.clientHeight, 'display', canvas.style.display);
    let chart;
    try {
      chart = new Chart(ctx, {
        type: 'bar',
        data: {
          labels: persianDates,
          datasets: [{
            label: 'تعداد صفحات چاپ شده',
            data: data.totals,
            backgroundColor: 'rgba(0,212,255,0.6)',
            borderColor: '#00d4ff',
            borderWidth: 2,
            borderRadius: 6
          }]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            legend: { display: false },
            tooltip: {
              backgroundColor: 'rgba(20, 23, 32, 0.95)',
              titleColor: '#00d4ff',
              bodyColor: '#e4e8f0',
              borderColor: '#2a3248',
              borderWidth: 1,
              padding: 12,
              displayColors: false,
              callbacks: {
                label: function(context) {
                  return `چاپ: ${context.parsed.y.toLocaleString('fa-IR')} صفحه`;
                }
              }
            }
          },
          scales: {
            y: {
              beginAtZero: true,
              grid: {
                color: 'rgba(42, 50, 72, 0.45)',
                drawBorder: false
              },
              ticks: {
                color: '#7b86a0',
                font: { family: 'JetBrains Mono', size: 10 },
                callback: function(value) { return value.toLocaleString('fa-IR'); }
              }
            },
            x: {
              grid: { display: false },
              ticks: {
                color: '#7b86a0',
                font: { family: 'Noto Sans Arabic', size: 10 },
                maxRotation: 45,
                minRotation: 45
              }
            }
          },
          interaction: {
            intersect: false,
            mode: 'index'
          },
          animation: {
            duration: 750,
            easing: 'easeInOutQuart'
          }
        }
      });
      console.debug('Chart created for ip', ip, chart);
    } catch (chartErr) {
      console.error('Printer chart creation failed:', chartErr);
      if (container) {
        const oldMessage = container.querySelector('.printer-chart-empty');
        if (!oldMessage) {
          const message = document.createElement('div');
          message.className = 'printer-chart-empty';
          message.style.cssText = 'padding:18px 12px; color:var(--text3); font-size:13px; text-align:center; background:rgba(255,255,255,0.04); border:1px solid var(--border); border-radius:8px;';
          message.textContent = 'Chart render failed';
          container.appendChild(message);
        }
      }
      return;
    }

    chart.update();
    console.debug('Printer chart updated for ip', ip);
    requestAnimationFrame(() => {
      try {
        chart.update();
      } catch (err) {
        console.error('Printer chart second update failed:', err);
      }
    });
    printerChartInstances[ip] = chart;
  } catch (e) {
    console.error('Printer daily chart error:', e);
    if (container) {
      const oldMessage = container.querySelector('.printer-chart-empty');
      if (!oldMessage) {
        const message = document.createElement('div');
        message.className = 'printer-chart-empty';
        message.style.cssText = 'padding:18px 12px; color:var(--text3); font-size:13px; text-align:center; background:rgba(255,255,255,0.04); border:1px solid var(--border); border-radius:8px;';
        message.textContent = 'No data available';
        container.appendChild(message);
      }
      canvas.style.display = 'none';
    }
  }
}

// ══════════════════════════════════════════════════
// INIT
// ══════════════════════════════════════════════════
fetchData();
resetCountdown();
// bindNicknameModalEvents();

setTimeout(() => {
  if (activeTab === 'overview') {
    loadDailyChart();
  }
}, 1000);
// ══════════════════════════════════════════════════
// PRINTER EDIT MODAL
// ══════════════════════════════════════════════════
function openEditModal(ip) {
  if (!canAdmin()) { toast('دسترسی ندارید', 'e'); return; }
  const p = allData.find(x => x.ip === ip);
  if (!p) return;

  const modal = document.getElementById('modal-edit-printer');
  if (!modal) return;

  document.getElementById('edit-printer-ip').value = ip;
  document.getElementById('edit-printer-name').value = p.name || '';
  document.getElementById('edit-printer-nickname').value = p.nickname || '';
  
  const groupSelect = document.getElementById('edit-printer-group-select');
  const customGroupInput = document.getElementById('edit-printer-group-custom');
  const currentGroup = p.group || getOfficeGroup(p.ip);
  
  const groupNames = new Set();
  OFFICE_GROUPS.forEach(g => groupNames.add(g.id));
  allData.forEach(item => { if(item.group) groupNames.add(item.group); });

  groupSelect.innerHTML = '<option value="">(بدون گروه)</option>';
  groupNames.forEach(g => {
    groupSelect.innerHTML += `<option value="${g}">${g}</option>`;
  });

  if (Array.from(groupSelect.options).some(opt => opt.value === currentGroup)) {
    groupSelect.value = currentGroup;
    customGroupInput.value = '';
  } else {
    groupSelect.value = '';
    customGroupInput.value = currentGroup;
  }

  modal.classList.add('show');
}

function closeEditModal() {
  const modal = document.getElementById('modal-edit-printer');
  if (modal) modal.classList.remove('show');
}

async function savePrinterEdit() {
  const ip = document.getElementById('edit-printer-ip').value;
  const name = document.getElementById('edit-printer-name').value.trim();
  const nickname = document.getElementById('edit-printer-nickname').value.trim();
  const groupSel = document.getElementById('edit-printer-group-select').value;
  const groupCust = document.getElementById('edit-printer-group-custom').value.trim();
  const group = groupCust || groupSel;

  try {
    const r = await apiFetch(`${API}/api/printer/${encodeURIComponent(ip)}/update`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, nickname, group })
    });
    if (r.ok) {
      toast('اطلاعات دستگاه بروزرسانی شد', 's');
      closeEditModal();
      fetchData();
    } else {
      const j = await r.json();
      toast(j.error || 'خطا در بروزرسانی', 'e');
    }
  } catch(e) {
    toast('خطا در اتصال', 'e');
  }
}

// ══════════════════════════════════════════════════
// IMPORT DATABASE (.db)
// ══════════════════════════════════════════════════
function showImportModal() {
  if (!canAdmin()) { toast('دسترسی ندارید', 'e'); return; }
  document.getElementById('import-step-1').style.display = 'block';
  document.getElementById('import-step-2').style.display = 'none';
  document.getElementById('import-db-file').value = '';
  document.getElementById('modal-import-db').classList.add('show');
}

function closeImportModal() {
  document.getElementById('modal-import-db').classList.remove('show');
}

async function analyzeImportFile() {
  const fileInput = document.getElementById('import-db-file');
  if (!fileInput.files.length) { toast('لطفاً فایلی انتخاب کنید', 'e'); return; }
  
  const formData = new FormData();
  formData.append('file', fileInput.files[0]);
  
  const btn = document.querySelector('[onclick="analyzeImportFile()"]');
  btn.disabled = true; btn.textContent = 'در حال تحلیل...';
  
  try {
    const res = await apiFetch(`${API}/api/import/analyze`, {
      method: 'POST',
      body: formData
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'خطا در تحلیل فایل');
    
    const s = data.summary;
    document.getElementById('import-count-logs').textContent = fmtN(s.logs_count || 0);
    const range = s.logs_range || {};
    document.getElementById('import-range-logs').textContent = range.start ? `${range.start.slice(0,10)} تا ${range.end.slice(0,10)}` : '—';

    // لیست بخش‌های قابل import
    const tableList = document.getElementById('import-table-list');
    const tables = s.tables || {};
    const tableOrder = ['logs','printer_counters','toner_history','sensor_readings','cartridge_state','yield_samples','toner_snapshots_v2','cartridge_yield_profiles'];
    tableList.innerHTML = tableOrder
      .filter(t => tables[t])
      .map(t => {
        const info = tables[t] || {};
        const checked = ['logs','printer_counters','toner_history','cartridge_state','yield_samples','toner_snapshots_v2','cartridge_yield_profiles'].includes(t) ? 'checked' : '';
        const dateRange = info.range && info.range.start ? ` • ${String(info.range.start).slice(0,10)} تا ${String(info.range.end || '').slice(0,10)}` : '';
        return `
          <label class="import-choice-item" style="display:flex;align-items:flex-start;gap:8px;font-size:11px;line-height:1.7;cursor:pointer;border:1px solid var(--border);border-radius:8px;padding:8px;background:var(--bg4)">
            <input type="checkbox" name="import-tables" value="${escapeHtml(t)}" ${checked} style="margin-top:4px">
            <span>
              <strong>${escapeHtml(info.label || t)}</strong>
              <small style="display:block;color:var(--text3)">${fmtN(info.count || 0)} رکورد${dateRange}</small>
            </span>
          </label>`;
      }).join('') || '<div style="font-size:11px;color:var(--red)">هیچ جدول قابل import یافت نشد.</div>';
    
    // لیست پرینترها؛ از همه جدول‌ها جمع‌آوری می‌شود نه فقط logs
    const pList = document.getElementById('import-printer-list');
    const printers = s.all_printers && s.all_printers.length ? s.all_printers : (s.printers_in_logs || []);
    pList.innerHTML = printers.map(p => `
      <label style="display:flex; align-items:center; gap:8px; margin-bottom:4px; font-size:11px; cursor:pointer">
        <input type="checkbox" name="import-ips" value="${escapeHtml(p.ip)}" checked>
        ${escapeHtml(p.name || p.ip)} (${escapeHtml(p.ip)})
      </label>
    `).join('') || '<div style="font-size:11px;color:var(--text3)">IP قابل فیلتر در فایل یافت نشد.</div>';
    
    document.getElementById('import-step-1').style.display = 'none';
    document.getElementById('import-step-2').style.display = 'block';
    
  } catch (err) {
    toast(err.message, 'e');
  } finally {
    btn.disabled = false; btn.textContent = 'تحلیل فایل...';
  }
}

function setImportPrintersChecked(checked) {
  document.querySelectorAll('input[name="import-ips"]').forEach(cb => { cb.checked = checked; });
}

async function confirmImport() {
  const selectedIps = Array.from(document.querySelectorAll('input[name="import-ips"]:checked')).map(cb => cb.value);
  const selectedTables = Array.from(document.querySelectorAll('input[name="import-tables"]:checked')).map(cb => cb.value);
  const startDate = document.getElementById('import-filter-start').value;
  const endDate = document.getElementById('import-filter-end').value;
  const duplicateMode = document.getElementById('import-duplicate-mode').value || 'skip';
  const createBackup = document.getElementById('import-create-backup').checked;
  
  if (document.querySelectorAll('input[name="import-ips"]').length && selectedIps.length === 0) { toast('حداقل یک پرینتر را انتخاب کنید', 'e'); return; }
  if (selectedTables.length === 0) { toast('حداقل یک بخش داده را انتخاب کنید', 'e'); return; }
  
  const btn = document.querySelector('[onclick="confirmImport()"]');
  btn.disabled = true; btn.textContent = 'در حال بارگذاری...';
  
  try {
    const res = await apiFetch(`${API}/api/import/confirm`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        tables: selectedTables,
        filters: {
          ips: selectedIps,
          start_date: startDate ? startDate + 'T00:00:00' : null,
          end_date: endDate ? endDate + 'T23:59:59' : null
        },
        options: {
          duplicate_mode: duplicateMode,
          backup: createBackup
        }
      })
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'خطا در بارگذاری');
    
    const imported = fmtN(data.imported || 0);
    const skipped = fmtN(data.skipped || 0);
    const backupMsg = data.backup ? ` | backup: ${data.backup}` : '';
    toast(`${imported} رکورد وارد شد، ${skipped} رکورد تکراری/رد شد${backupMsg}`, 's');
    closeImportModal();
    fetchData(); // بروزرسانی لیست لاگ‌ها
  } catch (err) {
    toast(err.message, 'e');
  } finally {
    btn.disabled = false; btn.textContent = 'تایید و بارگذاری دیتا';
  }
}

// ══════════════════════════════════════════════════
// GROUP MANAGER LOGIC
// ══════════════════════════════════════════════════
function addNewGroupToSystem() {
  const name = document.getElementById('new-group-name').value.trim();
  if (!name) { toast('نام گروه را وارد کنید', 'e'); return; }
  
  // برای ایجاد گروه، نیازی به API جدا نیست، کافیست در لیست دراپ‌داون‌ها ظاهر شود
  // این کار با اضافه کردن به یک پرینتر فرضی یا فقط نگه داشتن در حافظه انجام می‌شود
  // اما ساده‌ترین راه این است که کاربر نام را وارد کند و ما آن را در لیست انتخاب‌ها نشان دهیم.
  
  toast(`گروه "${name}" آماده تخصیص است`, 's');
  document.getElementById('new-group-name').value = '';
  renderGroupManagerList();
}

function renderGroupManagerList() {
  const container = document.getElementById('group-manager-list');
  const filter = document.getElementById('group-mgr-filter').value.toLowerCase();
  if (!container) return;

  // استخراج تمام گروه‌های موجود برای استفاده در دراپ‌داون هر ردیف
  const allGroupNames = new Set();
  OFFICE_GROUPS.forEach(g => allGroupNames.add(g.name));
  allData.forEach(p => { if(p.group) allGroupNames.add(p.group); });

  const filteredPrinters = allData.filter(p => 
    p.ip.toLowerCase().includes(filter) || 
    (p.name || '').toLowerCase().includes(filter) ||
    (p.nickname || '').toLowerCase().includes(filter)
  );

  if (filteredPrinters.length === 0) {
    container.innerHTML = '<div style="padding:20px; text-align:center; color:var(--text3)">پرینتری یافت نشد</div>';
    return;
  }

  container.innerHTML = filteredPrinters.map(p => {
    const currentGroup = p.group || '';
    const options = Array.from(allGroupNames).map(gName => 
      `<option value="${gName}" ${currentGroup === gName ? 'selected' : ''}>${gName}</option>`
    ).join('');

    return `
      <div style="display:flex; align-items:center; gap:10px; padding:8px; border-bottom:1px solid var(--border); background:var(--bg2); margin-bottom:4px; border-radius:6px">
        <div style="flex:1; min-width:0">
          <div style="font-size:12px; font-weight:700; color:var(--text); overflow:hidden; text-overflow:ellipsis; white-space:nowrap">${p.nickname || p.name}</div>
          <div style="font-family:var(--mono); font-size:10px; color:var(--cyan)">${p.ip}</div>
        </div>
        <select onchange="updatePrinterGroupDirectly('${p.ip}', this.value)" style="padding:4px 8px; font-size:11px; border-radius:4px; border:1px solid var(--border); background:var(--bg3); color:var(--text); width:140px">
          <option value="">(بدون گروه)</option>
          ${options}
        </select>
      </div>
    `;
  }).join('');
}

async function updatePrinterGroupDirectly(ip, newGroup) {
  try {
    const r = await apiFetch(`${API}/api/printer/${encodeURIComponent(ip)}/update`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ group: newGroup })
    });
    if (r.ok) {
      toast('گروه بروزرسانی شد', 's');
      // بروزرسانی داده‌های محلی بدون رفرش کامل اگر ممکن باشد
      const p = allData.find(x => x.ip === ip);
      if (p) p.group = newGroup;
      rebuildTabs(allData); // سایدبار بروز شود
    } else {
      toast('خطا در بروزرسانی', 'e');
    }
  } catch(e) {
    toast('خطا در اتصال', 'e');
  }
}

// اضافه کردن فراخوانی رندر لیست هنگام باز شدن تب گروه‌ها
const originalSwitchAddTab = switchAddTab;
switchAddTab = function(name, el) {
  originalSwitchAddTab(name, el);
  if (name === 'groups') {
    renderGroupManagerList();
  }
};

if (document.getElementById('modal-clear-logs')) {
  document.getElementById('modal-clear-logs').onclick = (e) => {
    if (e.target.id === 'modal-clear-logs') closeClearLogsModal();
  };
}
