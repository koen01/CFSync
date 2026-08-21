/* Minimal read-only UI for Creality K2 Plus CFS via Moonraker */

const $ = (id) => document.getElementById(id);
const PRINTER_SPOOL_SLOT = "SP";

function slotTitle(slotId) {
  if (slotId === PRINTER_SPOOL_SLOT) return "Printer Spool Input";
  return `Box ${slotId[0]} · Slot ${slotId[1]}`;
}

function fmtTs(ts) {
  if (!ts) return "—";
  try {
    const d = new Date(ts * 1000);
    return d.toLocaleString();
  } catch {
    return "—";
  }
}

function fmtDuration(startTs, endTs) {
  const start = Number(startTs || 0);
  const end = Number(endTs || 0);
  if (!(start > 0) || !(end >= start)) return "—";
  let secs = Math.round(end - start);
  const days = Math.floor(secs / 86400);
  secs -= days * 86400;
  const hours = Math.floor(secs / 3600);
  secs -= hours * 3600;
  const mins = Math.floor(secs / 60);
  secs -= mins * 60;

  if (days > 0) return `${days}d ${hours}h ${mins}m`;
  if (hours > 0) return `${hours}h ${mins}m`;
  if (mins > 0) return `${mins}m ${secs}s`;
  return `${secs}s`;
}

function badge(el, text, cls) {
  el.classList.remove("ok", "bad", "warn");
  if (cls) el.classList.add(cls);
  el.textContent = text;
}

function slotEl(slotId, label, meta, isActive, printerId) {
  const wrap = document.createElement("div");
  wrap.className = "slot" + (isActive ? " active" : "");
  wrap.dataset.slotid = slotId;

  const left = document.createElement("div");
  left.className = "slotLeft";

  const sw = document.createElement("div");
  sw.className = "swatch";
  sw.style.background = meta.color || "#2a3442";
  left.appendChild(sw);

  const txt = document.createElement("div");
  txt.className = "slotText";

  const nm = document.createElement("div");
  nm.className = "slotName";
  nm.textContent = label;
  txt.appendChild(nm);

  const sub = document.createElement("div");
  sub.className = "slotSub";
  if (meta.present === false) {
    sub.textContent = "empty";
    txt.appendChild(sub);
    left.appendChild(txt);
  } else {
  // Line 2: brand + filament name if available, else material + color
  const brandName = [meta.manufacturer, meta.name].filter(Boolean).join(' ');
  if (brandName) {
    sub.textContent = brandName;
  } else {
    const parts = [];
    if (meta.material) parts.push(meta.material);
    if (meta.color) parts.push(meta.color.toUpperCase());
    sub.textContent = parts.length ? parts.join(" · ") : "—";
  }
  txt.appendChild(sub);

  // Line 3: material type + Spoolman link indicator (only shown when line 2 has brand/name info)
  const detailParts = [];
  if (brandName && meta.material) detailParts.push(meta.material);
  if (meta.spoolman_id) detailParts.push('SP #' + meta.spoolman_id);
  if (detailParts.length) {
    const detail = document.createElement("div");
    detail.className = "slotDetail";
    detail.textContent = detailParts.join(' · ');
    txt.appendChild(detail);
  }

  left.appendChild(txt);
  }

  const right = document.createElement("div");
  right.className = "slotRight";
  const tag = document.createElement("div");
  tag.className = "tag" + (!meta.material ? " muted" : "");
  tag.textContent = meta.present === false ? 'empty' : (isActive ? 'active' : 'ready');
  right.appendChild(tag);

  if (meta.percent != null) {
    const pct = document.createElement("div");
    pct.className = "spoolPct";
    pct.textContent = meta.percent + "%";
    right.appendChild(pct);
  }

  wrap.appendChild(left);
  wrap.appendChild(right);

  wrap.addEventListener("click", (ev) => {
    ev.preventDefault();
    openSpoolModal(slotId, meta, printerId);
  });
  return wrap;
}

function fmtMm(mm) {
  const m = (mm || 0) / 1000.0;
  if (m >= 10) return m.toFixed(1) + " m";
  return m.toFixed(2) + " m";
}

function fmtG(g) {
  if (g == null) return "0 g";
  const gg = Number(g);
  if (Number.isNaN(gg)) return "0 g";
  if (gg >= 100) return gg.toFixed(0) + " g";
  if (gg >= 10) return gg.toFixed(1) + " g";
  return gg.toFixed(2) + " g";
}

function fmtUsedFromMm(mm) {
  const m = (mm || 0) / 1000.0;
  if (m >= 10) return m.toFixed(1) + " m";
  return m.toFixed(2) + " m";
}


async function postJson(url, payload) {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!r.ok) {
    const txt = await r.text().catch(() => "");
    throw new Error(txt || `HTTP ${r.status}`);
  }
  return r.json();
}

function normalizeHexColor(raw) {
  const v = String(raw || "").trim().toLowerCase();
  if (!v) return "";
  const col = v.startsWith("#") ? v : "#" + v;
  return /^#[0-9a-f]{6}$/.test(col) ? col : "";
}

function recentJobSlotLabel(slotId) {
  const sid = String(slotId || "").toUpperCase();
  if (sid === PRINTER_SPOOL_SLOT) return "Spool";
  if (/^[1-4][A-D]$/.test(sid)) return `CFS Box ${sid[0]} · ${sid}`;
  return sid || "—";
}

function renderSpoolmanList(listEl, spools, selectedId = null) {
  if (!listEl) return;
  listEl.innerHTML = '';
  if (!spools.length) {
    const o = document.createElement('div');
    o.className = 'spoolmanListItem muted';
    o.textContent = 'No spools found';
    listEl.appendChild(o);
    return;
  }
  for (const sp of spools) {
    const item = document.createElement('div');
    item.className = 'spoolmanListItem';
    item.dataset.id = String(sp.id);

    const swatch = document.createElement('span');
    swatch.className = 'spoolmanListSwatch';
    const col = sp.color_hex ? (sp.color_hex.startsWith('#') ? sp.color_hex : '#' + sp.color_hex) : null;
    if (col) swatch.style.background = col;

    const label = document.createElement('span');
    const remaining = sp.remaining_weight != null ? fmtG(sp.remaining_weight) : '?';
    label.textContent = `#${sp.id} ${sp.vendor || ''} ${sp.filament_name || ''} · ${sp.material || ''} · ${remaining}`;

    item.appendChild(swatch);
    item.appendChild(label);
    item.addEventListener('click', () => {
      for (const el of listEl.querySelectorAll('.spoolmanListItem')) el.classList.remove('selected');
      item.classList.add('selected');
    });
    listEl.appendChild(item);
  }
  const pickId = selectedId != null ? Number(selectedId) : Number(spools[0].id || 0);
  let selected = null;
  if (pickId > 0) {
    selected = listEl.querySelector(`.spoolmanListItem[data-id="${pickId}"]`);
    if (selected) selected.classList.add('selected');
  }
  if (!selected) {
    const first = listEl.querySelector('.spoolmanListItem');
    if (first) first.classList.add('selected');
  }
}

// --- Spoolman integration ---
let spoolmanConfigured = false;

// --- Spool editor modal (local only) ---
let spoolModalOpen = false;
let spoolPrevPaused = null;
let spoolSlotId = null;
let spoolPrinterId = null;
// Maps printerId → display name, populated by render() so the modal can show it
let printerDisplayNames = {};
let historyRelinkModalOpen = false;
let historyRelinkPrevPaused = null;
let historyRelinkCtx = null;
let envChartModalOpen = false;
let envChartPrevPaused = null;
let jobHistoryPage = 0;
// Tracks which printer camera streams are currently open (survives render cycles)
const cameraOpen = new Set();

// Per-printer camera enabled state — persisted in localStorage
function isCameraEnabled(printerId) {
  try {
    const s = JSON.parse(localStorage.getItem('cameraEnabled') || '{}');
    return s[printerId] !== false; // default: enabled
  } catch { return true; }
}
function setCameraEnabled(printerId, enabled) {
  try {
    const s = JSON.parse(localStorage.getItem('cameraEnabled') || '{}');
    s[printerId] = enabled;
    localStorage.setItem('cameraEnabled', JSON.stringify(s));
  } catch {}
}
// Incremental render state — avoids full DOM teardown on every tick
const _renderedPrinters = new Map(); // pid → {block, fingerprint}
let _renderedJobsCard = null; // {el, fingerprint} | null
let _drawerOpen = false;
let _currentPage = 'dashboard';

function navigateTo(page) {
  _currentPage = page;
  const pages = ['dashboard', 'jobs', 'settings'];
  for (const pg of pages) {
    const el = $('page' + pg.charAt(0).toUpperCase() + pg.slice(1));
    if (el) el.style.display = pg === page ? '' : 'none';
  }
  for (const item of document.querySelectorAll('.navItem[data-page]')) {
    item.classList.toggle('navItem--active', item.dataset.page === page);
  }
  const titles = { dashboard: 'CFSync', jobs: 'Completed Jobs', settings: 'Settings' };
  const titleEl = $('printerTitle');
  if (titleEl) titleEl.textContent = titles[page] || 'CFSync';
  const subEl = $('printerSubtitle');
  if (subEl && page !== 'dashboard') subEl.textContent = '';
  // Close drawer if open
  if (_drawerOpen) {
    _drawerOpen = false;
    const drawer = $('navDrawer');
    if (drawer) drawer.classList.remove('navDrawer--open');
  }
}

function closeSpoolModal() {
  const m = $('spoolModal');
  if (m) m.style.display = 'none';
  spoolModalOpen = false;
  spoolSlotId = null;
  spoolPrinterId = null;
  if (spoolPrevPaused !== null) {
    refreshPaused = spoolPrevPaused;
    spoolPrevPaused = null;
    applyRefreshTimer();
  }
}

function closeHistoryRelinkModal() {
  const m = $('historyRelinkModal');
  if (m) m.style.display = 'none';
  const applyBtn = $('historyRelinkApply');
  if (applyBtn) {
    applyBtn.disabled = false;
    applyBtn.textContent = 'Relink';
  }
  historyRelinkModalOpen = false;
  historyRelinkCtx = null;
  if (historyRelinkPrevPaused !== null) {
    refreshPaused = historyRelinkPrevPaused;
    historyRelinkPrevPaused = null;
    applyRefreshTimer();
  }
}

function closeEnvChartModal() {
  const m = $('envChartModal');
  if (m) m.style.display = 'none';
  const body = $('envChartBody');
  if (body) body.innerHTML = '';
  envChartModalOpen = false;
  if (envChartPrevPaused !== null) {
    refreshPaused = envChartPrevPaused;
    envChartPrevPaused = null;
    applyRefreshTimer();
  }
}

async function loadHistoryRelinkDropdown(ctx) {
  const list = $('historyRelinkSelect');
  if (!list) return;
  list.innerHTML = '';
  const ph = document.createElement('div');
  ph.className = 'spoolmanListItem muted';
  ph.textContent = 'Loading spools...';
  list.appendChild(ph);

  const applyBtn = $('historyRelinkApply');
  if (applyBtn) applyBtn.disabled = true;

  try {
    const r = await fetch(`/api/ui/spoolman/spools?slot=${encodeURIComponent(ctx.slot)}&printer_id=${encodeURIComponent(ctx.printerId || '')}`, { cache: 'no-store' });
    if (!r.ok) throw new Error(await r.text());
    const data = await r.json();
    const spools = Array.isArray(data.spools) ? data.spools : [];
    renderSpoolmanList(list, spools, ctx.currentSpoolId || null);
    if (applyBtn) applyBtn.disabled = !spools.length;
  } catch (e) {
    list.innerHTML = '';
    const o = document.createElement('div');
    o.className = 'spoolmanListItem muted';
    o.textContent = `Spoolman error: ${e.message || String(e)}`;
    list.appendChild(o);
    if (applyBtn) applyBtn.disabled = true;
  }
}

async function openHistoryRelinkModal(ctx) {
  const m = $('historyRelinkModal');
  if (!m) return;
  historyRelinkModalOpen = true;
  historyRelinkCtx = ctx;

  if (historyRelinkPrevPaused === null) historyRelinkPrevPaused = refreshPaused;
  refreshPaused = true;
  applyRefreshTimer();

  const title = $('historyRelinkTitle');
  const sub = $('historyRelinkSub');
  const hint = $('historyRelinkHint');
  const applyBtn = $('historyRelinkApply');
  if (title) title.textContent = `${ctx.currentSpoolId ? 'Relink' : 'Link'} Job Spool`;
  if (applyBtn) applyBtn.textContent = ctx.currentSpoolId ? 'Relink' : 'Link';
  if (sub) {
    const usage = `${Number(ctx.meters || 0).toFixed(2)} m · ${fmtG(Number(ctx.grams || 0))}`;
    const linked = ctx.currentSpoolId ? `#${ctx.currentSpoolId}` : 'not linked';
    sub.textContent = `${ctx.printerId} · ${recentJobSlotLabel(ctx.slot)} · ${linked} · ${usage}`;
  }
  if (hint) {
    hint.textContent = ctx.currentSpoolId
      ? 'Usage will be moved from the currently linked spool to the selected spool.'
      : 'Usage will be applied to the selected spool for this job entry.';
  }

  m.style.display = 'block';
  await loadHistoryRelinkDropdown(ctx);
}

function openSpoolModal(slotId, meta, printerId) {
  // Only open if modal exists (older builds)
  const m = $('spoolModal');
  if (!m) return;
  spoolModalOpen = true;
  spoolSlotId = slotId;
  spoolPrinterId = printerId || null;

  // Pause auto-refresh while editing so nothing collapses
  if (spoolPrevPaused === null) spoolPrevPaused = refreshPaused;
  refreshPaused = true;
  applyRefreshTimer();

  const title = $('spoolTitle');
  const sub = $('spoolSub');
  if (title) {
    const printerName = printerId ? (printerDisplayNames[printerId] || printerId) : null;
    const slotLabel = slotTitle(slotId);
    title.textContent = printerName ? `${printerName} · ${slotLabel}` : slotLabel;
  }
  if (sub) {
    if (meta.present === false) {
      sub.textContent = "empty";
    } else {
      sub.textContent = `${meta.material || '—'} · ${(meta.color || '').toUpperCase() || '—'}`;
    }
  }

  // --- Spoolman section ---
  const smSec = $('spoolmanSection');
  if (smSec) {
    if (spoolmanConfigured) {
      smSec.style.display = '';
      const bdg = $('spoolmanBadge');
      const notLinked = $('spoolmanNotLinked');
      const linked = $('spoolmanLinked');
      const info = $('spoolmanInfo');
      const smId = meta.spoolman_id;
      if (smId) {
        if (bdg) { bdg.textContent = 'linked'; bdg.classList.remove('muted'); bdg.classList.add('ok'); }
        if (notLinked) notLinked.style.display = 'none';
        if (linked) linked.style.display = 'flex';
        if (info) {
          info.textContent = 'Loading spool data…';
          // Fetch live remaining from Spoolman
          fetch(`/api/ui/spoolman/spool_detail?slot=${encodeURIComponent(slotId)}&printer_id=${encodeURIComponent(printerId || '')}`, { cache: 'no-store' })
            .then(r => r.json())
            .then(data => {
              if (data.spool) {
                const fil = data.spool.filament || {};
                const vendor = (fil.vendor || {}).name || meta.manufacturer || meta.vendor || '';
                const name = fil.name || meta.name || '';
                const material = (fil.material || '').toUpperCase();
                const remaining = data.spool.remaining_weight != null ? fmtG(data.spool.remaining_weight) : '—';
                info.textContent = [vendor, name, material, remaining].filter(Boolean).join(' · ');
              } else {
                info.textContent = data.error ? 'Spoolman unreachable' : `Spool #${smId}`;
              }
            })
            .catch(() => {
              info.textContent = 'Spoolman unreachable';
            });
        }
      } else {
        if (bdg) { bdg.textContent = 'not linked'; bdg.classList.add('muted'); bdg.classList.remove('ok'); }
        if (notLinked) notLinked.style.display = 'flex';
        if (linked) linked.style.display = 'none';
        loadSpoolmanDropdown(slotId, printerId);
      }
    } else {
      smSec.style.display = 'none';
    }
  }

  m.style.display = 'block';
}

async function loadSpoolmanDropdown(slotId, printerId) {
  const list = $('spoolmanSelect');
  if (!list) return;
  list.innerHTML = '';
  const ph = document.createElement('div');
  ph.className = 'spoolmanListItem muted';
  ph.textContent = 'Loading spools…';
  list.appendChild(ph);

  try {
    const r = await fetch(`/api/ui/spoolman/spools?slot=${encodeURIComponent(slotId)}&printer_id=${encodeURIComponent(printerId || '')}`, { cache: 'no-store' });
    if (!r.ok) throw new Error(await r.text());
    const data = await r.json();
    const spools = Array.isArray(data.spools) ? data.spools : [];
    renderSpoolmanList(list, spools, null);
  } catch (e) {
    list.innerHTML = '';
    const o = document.createElement('div');
    o.className = 'spoolmanListItem muted';
    o.textContent = `Spoolman error: ${e.message || String(e)}`;
    list.appendChild(o);
  }
}

function initSpoolModal() {
  const m = $('spoolModal');
  if (!m) return;
  const closeBtn = $('spoolClose');
  const back = $('spoolBackdrop');
  // IMPORTANT: stop event bubbling so a click does not "fall through" to the
  // underlying slot card and immediately re-open the modal.
  if (closeBtn) closeBtn.onclick = (ev) => {
    if (ev) { ev.preventDefault(); ev.stopPropagation(); }
    closeSpoolModal();
  };
  if (back) back.onclick = (ev) => {
    if (ev) { ev.preventDefault(); ev.stopPropagation(); }
    closeSpoolModal();
  };

  // Esc closes the modal
  document.addEventListener('keydown', (ev) => {
    if (!spoolModalOpen) return;
    if (ev.key === 'Escape') {
      ev.preventDefault();
      closeSpoolModal();
    }
  });

  // --- Spoolman button handlers ---
  const smLink = $('spoolmanLink');
  const smUnlink = $('spoolmanUnlink');
  const smRefresh = $('spoolmanRefresh');

  if (smLink) {
    smLink.onclick = async (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      if (!spoolSlotId) return;
      const list = $('spoolmanSelect');
      const selected = list && list.querySelector('.spoolmanListItem.selected');
      const id = selected ? Number(selected.dataset.id) : 0;
      if (!id) return;
      await postJson('/api/ui/spoolman/link', { printer_id: spoolPrinterId, slot: spoolSlotId, spoolman_id: id });
      closeSpoolModal();
      await tick();
    };
  }

  if (smUnlink) {
    smUnlink.onclick = async (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      if (!spoolSlotId) return;
      const unlinkSlot = spoolSlotId;
      const unlinkPrinter = spoolPrinterId;
      try {
        await postJson('/api/ui/spoolman/unlink', { printer_id: unlinkPrinter, slot: unlinkSlot });
      } catch (e) {
        alert(`Unlink failed: ${e.message || e}`);
        return;
      }
      // Switch modal to "not linked" state without closing
      const bdg = $('spoolmanBadge');
      const notLinked = $('spoolmanNotLinked');
      const linked = $('spoolmanLinked');
      if (bdg) { bdg.textContent = 'not linked'; bdg.classList.add('muted'); bdg.classList.remove('ok'); }
      if (linked) linked.style.display = 'none';
      if (notLinked) notLinked.style.display = 'flex';
      await loadSpoolmanDropdown(unlinkSlot, unlinkPrinter);
      await tick();
    };
  }

  if (smRefresh) {
    smRefresh.onclick = async (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      if (!spoolSlotId) return;
      // Re-fetch spool detail from Spoolman
      const info = $('spoolmanInfo');
      try {
        if (info) info.textContent = 'Loading spool data…';
        const r = await fetch(`/api/ui/spoolman/spool_detail?slot=${encodeURIComponent(spoolSlotId)}&printer_id=${encodeURIComponent(spoolPrinterId || '')}`, { cache: 'no-store' });
        const data = await r.json();
        if (data.spool) {
          const fil = data.spool.filament || {};
          const vendor = (fil.vendor || {}).name || '';
          const name = fil.name || '';
          const material = (fil.material || '').toUpperCase();
          const remaining = data.spool.remaining_weight != null ? fmtG(data.spool.remaining_weight) : '—';
          if (info) info.textContent = [vendor, name, material, remaining].filter(Boolean).join(' · ');
        } else {
          if (info) info.textContent = data.error ? 'Spoolman unreachable' : '—';
        }
      } catch (e) {
        if (info) info.textContent = `Spoolman error: ${e.message || String(e)}`;
      }
    };
  }
}

function initHistoryRelinkModal() {
  const m = $('historyRelinkModal');
  if (!m) return;
  const closeBtn = $('historyRelinkClose');
  const back = $('historyRelinkBackdrop');
  if (closeBtn) closeBtn.onclick = (ev) => {
    if (ev) { ev.preventDefault(); ev.stopPropagation(); }
    closeHistoryRelinkModal();
  };
  if (back) back.onclick = (ev) => {
    if (ev) { ev.preventDefault(); ev.stopPropagation(); }
    closeHistoryRelinkModal();
  };

  document.addEventListener('keydown', (ev) => {
    if (!historyRelinkModalOpen) return;
    if (ev.key === 'Escape') {
      ev.preventDefault();
      closeHistoryRelinkModal();
    }
  });

  const applyBtn = $('historyRelinkApply');
  if (applyBtn) {
    applyBtn.onclick = async (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      if (!historyRelinkCtx) return;
      const list = $('historyRelinkSelect');
      const selected = list && list.querySelector('.spoolmanListItem.selected');
      const id = selected ? Number(selected.dataset.id) : 0;
      if (!id) return;
      applyBtn.disabled = true;
      const prevText = applyBtn.textContent;
      applyBtn.textContent = 'Saving...';
      try {
        await postJson('/api/ui/jobs/reallocate_spool', {
          printer_id: historyRelinkCtx.printerId,
          ended_at: historyRelinkCtx.endedAt,
          slot: historyRelinkCtx.slot,
          spoolman_id: id,
        });
        closeHistoryRelinkModal();
        await tick();
      } catch (e) {
        window.alert(`Failed to reallocate spool: ${e.message || String(e)}`);
        applyBtn.disabled = false;
        applyBtn.textContent = prevText || 'Relink';
      }
    };
  }
}

function envMetricMeta(metricKey) {
  if (metricKey === 'humidity_pct') {
    return { title: 'Humidity', unit: '%', lineClass: 'envLineHum', areaClass: 'envAreaHum' };
  }
  return { title: 'Temperature', unit: '°C', lineClass: 'envLineTemp', areaClass: 'envAreaTemp' };
}

function fmtEnvValue(v, metricKey) {
  const n = Number(v);
  if (!Number.isFinite(n)) return '—';
  if (metricKey === 'humidity_pct') return `${Math.round(n)}%`;
  return `${n.toFixed(1)}°C`;
}

function envHistorySeries(history, metricKey) {
  const out = [];
  for (const item of (Array.isArray(history) ? history : [])) {
    if (!item || typeof item !== 'object') continue;
    const ts = Number(item.ts || 0);
    const v = Number(item[metricKey]);
    if (!(ts > 0) || Number.isNaN(v)) continue;
    out.push({ ts, value: v });
  }
  out.sort((a, b) => a.ts - b.ts);
  const maxPoints = 360;
  if (out.length <= maxPoints) return out;
  const step = Math.ceil(out.length / maxPoints);
  const reduced = [];
  for (let i = 0; i < out.length; i += step) reduced.push(out[i]);
  if (reduced[reduced.length - 1] !== out[out.length - 1]) reduced.push(out[out.length - 1]);
  return reduced;
}

function renderEnvChart(metricKey, history) {
  const body = $('envChartBody');
  const meta = $('envChartMeta');
  if (!body) return;
  body.innerHTML = '';
  const mm = envMetricMeta(metricKey);
  const points = envHistorySeries(history, metricKey);

  if (!points.length) {
    if (meta) meta.textContent = 'No samples available yet.';
    const empty = document.createElement('div');
    empty.className = 'envChartEmpty';
    empty.textContent = 'No history yet. Wait for live CFS updates to collect samples.';
    body.appendChild(empty);
    return;
  }

  const first = points[0];
  const last = points[points.length - 1];
  if (meta) {
    meta.textContent = `Samples: ${points.length} · ${fmtTs(first.ts)} → ${fmtTs(last.ts)} · Latest: ${fmtEnvValue(last.value, metricKey)}`;
  }

  const NS = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('class', 'envChartSvg');
  svg.setAttribute('viewBox', '0 0 820 320');
  svg.setAttribute('preserveAspectRatio', 'none');

  const pad = { left: 56, right: 16, top: 16, bottom: 32 };
  const w = 820;
  const h = 320;
  const plotW = w - pad.left - pad.right;
  const plotH = h - pad.top - pad.bottom;
  const xMin = first.ts;
  const xMax = last.ts > xMin ? last.ts : xMin + 1;

  let yMin = Math.min(...points.map(p => p.value));
  let yMax = Math.max(...points.map(p => p.value));
  if (Math.abs(yMax - yMin) < 0.001) {
    const bump = metricKey === 'humidity_pct' ? 2 : 1;
    yMin -= bump;
    yMax += bump;
  } else {
    const padY = (yMax - yMin) * 0.12;
    yMin -= padY;
    yMax += padY;
  }
  const yRange = yMax - yMin;

  const toX = (ts) => pad.left + ((ts - xMin) / (xMax - xMin)) * plotW;
  const toY = (v) => pad.top + (1 - ((v - yMin) / yRange)) * plotH;

  for (let i = 0; i <= 4; i++) {
    const y = pad.top + (plotH / 4) * i;
    const val = yMax - (yRange / 4) * i;
    const line = document.createElementNS(NS, 'line');
    line.setAttribute('class', 'envGridLine');
    line.setAttribute('x1', String(pad.left));
    line.setAttribute('x2', String(w - pad.right));
    line.setAttribute('y1', String(y));
    line.setAttribute('y2', String(y));
    svg.appendChild(line);

    const label = document.createElementNS(NS, 'text');
    label.setAttribute('class', 'envAxisText');
    label.setAttribute('x', String(pad.left - 8));
    label.setAttribute('y', String(y + 4));
    label.setAttribute('text-anchor', 'end');
    label.textContent = metricKey === 'humidity_pct' ? `${Math.round(val)}%` : `${val.toFixed(1)}°C`;
    svg.appendChild(label);
  }

  const pathPairs = points.map((p) => [toX(p.ts), toY(p.value)]);
  const pathPoints = pathPairs.map(([x, y]) => `${x},${y}`).join(' ');
  const areaPoints = pathPairs.map(([x, y]) => `${x} ${y}`).join(' ');
  const firstX = toX(points[0].ts);
  const lastX = toX(last.ts);
  const baselineY = pad.top + plotH;

  const area = document.createElementNS(NS, 'path');
  area.setAttribute('class', mm.areaClass);
  area.setAttribute('d', `M ${firstX} ${baselineY} L ${areaPoints} L ${lastX} ${baselineY} Z`);
  svg.appendChild(area);

  const line = document.createElementNS(NS, 'polyline');
  line.setAttribute('class', mm.lineClass);
  line.setAttribute('points', pathPoints);
  svg.appendChild(line);

  const dot = document.createElementNS(NS, 'circle');
  dot.setAttribute('class', 'envPoint');
  dot.setAttribute('cx', String(lastX));
  dot.setAttribute('cy', String(toY(last.value)));
  dot.setAttribute('r', '4');
  dot.setAttribute('stroke', metricKey === 'humidity_pct' ? '#3fb6ff' : '#ff8a3d');
  svg.appendChild(dot);

  const xStart = document.createElementNS(NS, 'text');
  xStart.setAttribute('class', 'envAxisText');
  xStart.setAttribute('x', String(pad.left));
  xStart.setAttribute('y', String(h - 10));
  xStart.textContent = new Date(first.ts * 1000).toLocaleString();
  svg.appendChild(xStart);

  const xEnd = document.createElementNS(NS, 'text');
  xEnd.setAttribute('class', 'envAxisText');
  xEnd.setAttribute('x', String(w - pad.right));
  xEnd.setAttribute('y', String(h - 10));
  xEnd.setAttribute('text-anchor', 'end');
  xEnd.textContent = new Date(last.ts * 1000).toLocaleString();
  svg.appendChild(xEnd);

  body.appendChild(svg);
}

function openEnvChartModal(ctx) {
  const m = $('envChartModal');
  if (!m) return;
  envChartModalOpen = true;

  if (envChartPrevPaused === null) envChartPrevPaused = refreshPaused;
  refreshPaused = true;
  applyRefreshTimer();

  const mm = envMetricMeta(ctx.metricKey);
  const title = $('envChartTitle');
  const sub = $('envChartSub');
  if (title) title.textContent = `CFS Box ${ctx.boxId} · ${mm.title}`;
  if (sub) {
    const printer = ctx.printerName || ctx.printerId || 'Printer';
    sub.textContent = `${printer} · ${ctx.printerId || ''}`.replace(/\s·\s$/, '');
  }

  renderEnvChart(ctx.metricKey, ctx.history);
  m.style.display = 'block';
}

function initEnvChartModal() {
  const m = $('envChartModal');
  if (!m) return;
  const closeBtn = $('envChartClose');
  const back = $('envChartBackdrop');
  if (closeBtn) closeBtn.onclick = (ev) => {
    if (ev) { ev.preventDefault(); ev.stopPropagation(); }
    closeEnvChartModal();
  };
  if (back) back.onclick = (ev) => {
    if (ev) { ev.preventDefault(); ev.stopPropagation(); }
    closeEnvChartModal();
  };
  document.addEventListener('keydown', (ev) => {
    if (!envChartModalOpen) return;
    if (ev.key === 'Escape') {
      ev.preventDefault();
      closeEnvChartModal();
    }
  });
}


function fmtRelative(ts) {
  if (!ts) return '—';
  const secs = Math.floor(Date.now() / 1000 - ts);
  if (secs < 60) return 'just now';
  if (secs < 3600) return Math.floor(secs / 60) + 'm ago';
  if (secs < 86400) return Math.floor(secs / 3600) + 'h ago';
  if (secs < 86400 * 30) return Math.floor(secs / 86400) + 'd ago';
  return Math.floor(secs / (86400 * 30)) + 'mo ago';
}

function inferLiveCfsBoxes(cfsSlots) {
  if (!cfsSlots || typeof cfsSlots !== "object") return [];
  const out = new Set();
  for (const sid of Object.keys(cfsSlots)) {
    if (!/^[1-4][A-D]$/.test(sid)) continue;
    const m = cfsSlots[sid] || {};
    const rfid = String(m.rfid ?? "").trim();
    const hasLiveSignal = (
      m.present === true ||
      Number(m.state ?? 0) > 0 ||
      Number(m.selected ?? 0) === 1 ||
      m.percent != null ||
      (rfid && !["0", "00", "000", "0000", "00000", "000000"].includes(rfid))
    );
    if (hasLiveSignal) out.add(sid[0]);
  }
  return Array.from(out).sort();
}

function renderCfsStats(state, wrap) {
  if (!wrap) return;
  wrap.innerHTML = '';

  const stats = state.cfs_stats || {};
  const cfsSlots = state.cfs_slots || {};

  // Always show direct spool input status in the status panel, using the
  // same distance/grams metrics as CFS slots.
  const spoolStats = stats[PRINTER_SPOOL_SLOT] || {};
  const spoolMetersVal = Number(spoolStats.total_meters || 0);
  const spoolKgVal = Number(spoolStats.total_kg || 0);

  const spoolDiv = document.createElement('div');
  spoolDiv.className = 'cfsBox';
  const spoolHead = document.createElement('div');
  spoolHead.className = 'cfsBoxHead';
  const spoolHeadLabel = document.createElement('span');
  spoolHeadLabel.textContent = 'Spool';
  const spoolHeadTotals = document.createElement('span');
  spoolHeadTotals.className = 'cfsBoxTotals';
  spoolHeadTotals.textContent = `${spoolMetersVal.toFixed(1)} m  ·  ${fmtG(spoolKgVal * 1000)}`;
  spoolHead.appendChild(spoolHeadLabel);
  spoolHead.appendChild(spoolHeadTotals);
  spoolDiv.appendChild(spoolHead);

  const spoolRow = document.createElement('div');
  spoolRow.className = 'cfsSlotRow';
  const spoolLabel = document.createElement('span');
  spoolLabel.className = 'cfsSlotLabel';
  spoolLabel.textContent = PRINTER_SPOOL_SLOT;
  const spoolMeters = document.createElement('span');
  spoolMeters.className = 'cfsSlotMeters';
  spoolMeters.textContent = spoolMetersVal.toFixed(1) + ' m';
  const spoolKg = document.createElement('span');
  spoolKg.className = 'cfsSlotKg';
  spoolKg.textContent = fmtG(spoolKgVal * 1000);
  const spoolLast = document.createElement('span');
  spoolLast.className = 'cfsSlotLast';
  spoolLast.textContent = fmtRelative(spoolStats.last_used_at || null);
  spoolRow.appendChild(spoolLabel);
  spoolRow.appendChild(spoolMeters);
  spoolRow.appendChild(spoolKg);
  spoolRow.appendChild(spoolLast);
  spoolDiv.appendChild(spoolRow);
  wrap.appendChild(spoolDiv);

  const boxesMeta = cfsSlots['_boxes'] || {};
  const activeBoxIds = Object.keys(boxesMeta).map(Number).filter(n => n >= 1 && n <= 4).sort();
  const inferredFromStats = Array.from(
    new Set(
      Object.entries(stats)
        .filter(([sid, s]) => /^[1-4][A-D]$/.test(sid) && !!s && (((s.total_meters || 0) > 0) || ((s.total_kg || 0) > 0) || !!s.last_used_at))
        .map(([sid]) => Number(sid[0]))
        .filter(n => n >= 1 && n <= 4)
    )
  ).sort();
  const boxIds = activeBoxIds.length ? activeBoxIds : inferredFromStats;

  if (!boxIds.length) {
    const empty = document.createElement('div');
    empty.className = 'emptyState';
    empty.textContent = 'No CFS detected';
    wrap.appendChild(empty);
    return;
  }

  for (const b of boxIds) {
    const slotIds = ['A', 'B', 'C', 'D'].map(l => `${b}${l}`);

    let boxMeters = 0, boxKg = 0;
    for (const sid of slotIds) {
      const s = stats[sid];
      if (s) { boxMeters += s.total_meters || 0; boxKg += s.total_kg || 0; }
    }

    const boxDiv = document.createElement('div');
    boxDiv.className = 'cfsBox';

    const head = document.createElement('div');
    head.className = 'cfsBoxHead';
    const headLabel = document.createElement('span');
    headLabel.textContent = `CFS Box ${b}`;
    const headTotals = document.createElement('span');
    headTotals.className = 'cfsBoxTotals';
    headTotals.textContent = `${boxMeters.toFixed(1)} m  ·  ${fmtG(boxKg * 1000)}`;
    head.appendChild(headLabel);
    head.appendChild(headTotals);
    boxDiv.appendChild(head);

    for (const sid of slotIds) {
      const s = stats[sid] || {};
      const row = document.createElement('div');
      row.className = 'cfsSlotRow';

      const label = document.createElement('span');
      label.className = 'cfsSlotLabel';
      label.textContent = sid;

      const meters = document.createElement('span');
      meters.className = 'cfsSlotMeters';
      meters.textContent = ((s.total_meters || 0)).toFixed(1) + ' m';

      const kg = document.createElement('span');
      kg.className = 'cfsSlotKg';
      kg.textContent = fmtG((s.total_kg || 0) * 1000);

      const last = document.createElement('span');
      last.className = 'cfsSlotLast';
      last.textContent = fmtRelative(s.last_used_at || null);

      row.appendChild(label);
      row.appendChild(meters);
      row.appendChild(kg);
      row.appendChild(last);
      boxDiv.appendChild(row);
    }

    wrap.appendChild(boxDiv);
  }
}

function hexBrightness(hex) {
  const h = (hex || '').replace('#', '');
  if (h.length !== 6) return 128;
  const r = parseInt(h.substring(0, 2), 16);
  const g = parseInt(h.substring(2, 4), 16);
  const b = parseInt(h.substring(4, 6), 16);
  return (r * 299 + g * 587 + b * 114) / 1000;
}

function makeSpoolSvg(meta) {
  const present = meta.present !== false;
  const rawColor = meta.color || '';
  const hasColor = present && rawColor && rawColor !== '#2a3442' && rawColor.length >= 4;

  if (!hasColor) {
    // Empty slot — light disk with diagonal slash
    return `<svg viewBox="0 0 80 80" fill="none" xmlns="http://www.w3.org/2000/svg">
      <circle cx="40" cy="40" r="36" fill="#DDE3DE" stroke="#BEC9C0" stroke-width="3"/>
      <line x1="22" y1="58" x2="58" y2="22" stroke="#BEC9C0" stroke-width="4" stroke-linecap="round"/>
    </svg>`;
  }

  const c = rawColor.startsWith('#') ? rawColor : '#' + rawColor;
  const bright = hexBrightness(c);
  const tick  = bright > 145 ? 'rgba(0,0,0,0.28)' : 'rgba(255,255,255,0.18)';

  // Filament fill radius: area-proportional so it matches how a real spool empties.
  // At 100% the colored disk reaches the outer rim (r=36); at 0% it shrinks to the hub (r=10).
  const pct = (meta.percent != null) ? Math.max(0, Math.min(100, meta.percent)) / 100 : 1.0;
  const R_OUTER = 36, R_CORE = 10;
  const filR = Math.round(Math.sqrt(R_CORE * R_CORE + (R_OUTER * R_OUTER - R_CORE * R_CORE) * pct) * 10) / 10;
  const filamentDisk = filR > R_CORE + 0.5 ? `<circle cx="40" cy="40" r="${filR}" fill="${c}"/>` : '';

  return `<svg viewBox="0 0 80 80" fill="none" xmlns="http://www.w3.org/2000/svg">
    <circle cx="40" cy="40" r="36" fill="#DDE3DE" stroke="#BEC9C0" stroke-width="3"/>
    ${filamentDisk}
    <circle cx="40" cy="40" r="20" fill="none" stroke="${tick}" stroke-width="1.5"/>
    <line x1="40" y1="22" x2="40" y2="29" stroke="${tick}" stroke-width="2.5" stroke-linecap="round"/>
    <line x1="40" y1="51" x2="40" y2="58" stroke="${tick}" stroke-width="2.5" stroke-linecap="round"/>
    <line x1="22" y1="40" x2="29" y2="40" stroke="${tick}" stroke-width="2.5" stroke-linecap="round"/>
    <line x1="51" y1="40" x2="58" y2="40" stroke="${tick}" stroke-width="2.5" stroke-linecap="round"/>
    <circle cx="40" cy="40" r="10" fill="#DDE3DE" stroke="#BEC9C0" stroke-width="1.5"/>
    <circle cx="40" cy="40" r="3.5" fill="#6F7972"/>
  </svg>`;
}

function renderPrinter(printerId, state) {
  const block = document.createElement("section");
  block.className = "printerBlock";
  if (printerId) block.dataset.printerId = printerId;

  const head = document.createElement("div");
  head.className = "printerHead";

  const titleWrap = document.createElement("div");
  titleWrap.className = "printerTitleWrap";
  const nameEl = document.createElement("div");
  nameEl.className = "printerName";
  nameEl.textContent = state.printer_name || printerId || "Printer";
  const metaEl = document.createElement("div");
  metaEl.className = "printerMeta";
  metaEl.textContent = [printerId, state.printer_firmware].filter(Boolean).join(" · ");
  titleWrap.appendChild(nameEl);
  titleWrap.appendChild(metaEl);
  head.appendChild(titleWrap);

  const badges = document.createElement("div");
  badges.className = "printerBadges";
  const pBadge = document.createElement("div");
  pBadge.className = "badge";
  pBadge.dataset.live = "printer-badge";
  const cfsBadge = document.createElement("div");
  cfsBadge.className = "badge";
  cfsBadge.dataset.live = "cfs-badge";
  const printerOk = !!state.printer_connected;
  const _narrow = window.innerWidth < 480;
  badge(pBadge, printerOk ? (_narrow ? "Connected" : "Printer: connected") : (_narrow ? "Offline" : "Printer: disconnected"), printerOk ? "ok" : "bad");
  if (!printerOk && state.printer_last_error && !_narrow) {
    pBadge.textContent += " (" + state.printer_last_error + ")";
  }
  const cfsOk = !!state.cfs_connected;
  badge(
    cfsBadge,
    cfsOk ? (_narrow ? "CFS ✓" : `CFS: detected · ${fmtTs(state.cfs_last_update)}`) : "CFS: —",
    cfsOk ? "ok" : "warn"
  );
  badges.appendChild(pBadge);
  badges.appendChild(cfsBadge);
  head.appendChild(badges);
  block.appendChild(head);

  const layout = document.createElement("div");
  layout.className = "layout";

  const leftCol = document.createElement("div");
  leftCol.className = "leftCol";
  const boxesGrid = document.createElement("section");
  boxesGrid.className = "grid";
  leftCol.appendChild(boxesGrid);

  const activeCard = document.createElement("section");
  activeCard.className = "card";
  activeCard.style.marginTop = "16px";
  const activeHead = document.createElement("div");
  activeHead.className = "cardHead";
  const activeTitle = document.createElement("div");
  activeTitle.className = "cardTitle";
  activeTitle.textContent = "Active";
  const activeMeta = document.createElement("div");
  activeMeta.className = "cardMeta";
  activeMeta.textContent = "—";
  activeHead.appendChild(activeTitle);
  activeHead.appendChild(activeMeta);
  activeCard.appendChild(activeHead);
  const activeRow = document.createElement("div");
  activeRow.className = "activeRow";
  activeCard.appendChild(activeRow);
  const activeLive = document.createElement("div");
  activeLive.className = "activeLive";
  activeLive.style.display = "none";
  activeCard.appendChild(activeLive);
  // CFS usage stats — belongs with the boxes, not the printer hardware
  const statsCard = document.createElement("section");
  statsCard.className = "card";
  statsCard.style.marginTop = "16px";
  const statsHead = document.createElement("div");
  statsHead.className = "cardHead";
  const statsTitle = document.createElement("div");
  statsTitle.className = "cardTitle";
  statsTitle.textContent = "Filament usage";
  const statsMeta = document.createElement("div");
  statsMeta.className = "cardMeta";
  statsHead.appendChild(statsTitle);
  statsHead.appendChild(statsMeta);
  statsCard.appendChild(statsHead);
  const history = document.createElement("div");
  history.className = "history";
  statsCard.appendChild(history);
  leftCol.appendChild(activeCard);
  leftCol.appendChild(statsCard);

  const rightCol = document.createElement("aside");
  rightCol.className = "rightCol";
  rightCol.appendChild(renderPrinterStatusCard(state));
  rightCol.appendChild(renderCameraCard(state, printerId));

  layout.appendChild(leftCol);
  layout.appendChild(rightCol);
  block.appendChild(layout);

  // We prefer Creality CFS slots (state.cfs_slots). Fallback to local slots if not present.
  const localSlots = state.slots || {};
  const slots = (state.cfs_slots && Object.keys(state.cfs_slots).length) ? state.cfs_slots : localSlots;
  const moonPrinting = ['printing', 'paused'].includes(state.moon_print_state || '');
  const spPresentNow = !!(slots[PRINTER_SPOOL_SLOT] || localSlots[PRINTER_SPOOL_SLOT] || {}).present;
  // Only treat SP as active once actual extrusion has started (filament_used > 0).
  // This prevents the spool holder showing as "active" during homing, bed meshing,
  // and startup sequences where no filament is extruded yet.
  const spExtruding = (state.moon_filament_used_mm || 0) > 0;
  const active = state.cfs_active_slot || (moonPrinting && spPresentNow && spExtruding ? PRINTER_SPOOL_SLOT : null);

  // Determine which CFS boxes are actually connected.
  const boxesInfo = (slots && slots._boxes) ? slots._boxes : {};
  const envHistoryByBox = (state.cfs_env_history && typeof state.cfs_env_history === 'object') ? state.cfs_env_history : {};
  const connectedBoxes = [];
  for (const n of ["1", "2", "3", "4"]) {
    const bi = boxesInfo[n];
    if (bi && bi.connected === true) connectedBoxes.push(n);
  }
  // Fallback: infer from live slot signals if firmware omits box metadata.
  if (!connectedBoxes.length) connectedBoxes.push(...inferLiveCfsBoxes(state.cfs_slots || {}));

  const metaFor = (sid) => {
    // We render slots primarily from Creality CFS data (state.cfs_slots),
    // BUT spool tracking (remaining/consumed + reference points) lives in state.slots.
    // Therefore we must merge both.
    const m = (slots && slots[sid]) ? slots[sid] : {};
    const local = (localSlots && localSlots[sid]) ? localSlots[sid] : {};
    const hasLiveCfs = !!(state.cfs_slots && Object.keys(state.cfs_slots).length);
    const localHasSpool = !!(
      local.spoolman_id ||
      local.name ||
      local.manufacturer ||
      (String(local.material || "").toUpperCase() && String(local.material || "").toUpperCase() !== "OTHER")
    );
    const defaultPresent = hasLiveCfs ? false : localHasSpool;
    let present = (m.present ?? local.present ?? defaultPresent);
    const wsState = Number(m.state ?? -1);
    const wsRfid = String(m.rfid ?? "").trim();
    const wsRfidMissing = ["", "0", "00", "000", "0000", "00000", "000000"].includes(wsRfid);
    const mergedMaterial = String((m.material ?? local.material) || "").toUpperCase();
    const mergedName = String((m.name ?? local.name) || "").trim();
    const mergedVendor = String((m.manufacturer ?? m.vendor ?? local.manufacturer ?? local.vendor) || "").trim();
    const looksLikeEmptyManual = wsState === 1 && wsRfidMissing && !mergedName && !mergedVendor && (!mergedMaterial || mergedMaterial === "OTHER");
    if (looksLikeEmptyManual) present = false;

    // normalize fields from either cfs_slots or local slots
    const out = {
      present,
      material: present === false ? "" : ((m.material ?? local.material) || "").toString().toUpperCase(),
      color: present === false ? "" : ((m.color ?? m.color_hex ?? local.color ?? local.color_hex) || "").toString().toLowerCase(),

      // spool epoch (for roll-change tracking)
      spool_epoch: (local.spool_epoch ?? null),

      // Spoolman
      spoolman_id: (local.spoolman_id ?? null),
      name: present === false ? "" : (local.name ?? ''),
      manufacturer: present === false ? "" : (local.manufacturer ?? local.vendor ?? ''),

      // CFS percent remaining from WS data
      percent: (m.percent != null ? m.percent : null),
    };
    return out;
  };

  function makeSlotPod(sid, m, isAct) {
    const pod = document.createElement("div");
    pod.className = "slotPod" + (isAct ? " active" : "");
    pod.dataset.slotid = sid;

    // Slot ID badge
    const idBadge = document.createElement("div");
    idBadge.className = "slotPodId";
    idBadge.textContent = sid;
    pod.appendChild(idBadge);

    // Spool graphic
    const spoolWrap = document.createElement("div");
    spoolWrap.className = "slotPodSpool";
    spoolWrap.innerHTML = makeSpoolSvg(m);
    pod.appendChild(spoolWrap);

    // Material — only shown when slot is occupied
    const matEl = document.createElement("div");
    matEl.className = "slotPodMaterial";
    matEl.textContent = m.present === false ? "" : (m.material || "—");
    pod.appendChild(matEl);

    // Percent remaining (if available from CFS/WS)
    if (m.present !== false && m.percent != null) {
      const pctEl = document.createElement("div");
      pctEl.className = "slotPodPct";
      pctEl.textContent = m.percent + "%";
      pod.appendChild(pctEl);
    }

    // Spoolman link indicator dot
    const linkDot = document.createElement("div");
    linkDot.className = "slotPodLink" + (m.spoolman_id ? " linked" : "");
    linkDot.title = m.spoolman_id ? "Linked to Spoolman #" + m.spoolman_id : "Not linked to Spoolman";
    pod.appendChild(linkDot);

    pod.addEventListener("click", (ev) => {
      ev.preventDefault();
      openSpoolModal(sid, m, printerId);
    });

    return pod;
  }

  function makeBoxCard(boxNum) {
    const card = document.createElement("section");
    card.className = "card";

    // Card head: title + active slot badge + env chips
    const head = document.createElement("div");
    head.className = "cardHead";

    const titleEl = document.createElement("div");
    titleEl.className = "cardTitle";
    titleEl.textContent = `Box ${boxNum}`;
    head.appendChild(titleEl);

    const meta = document.createElement("div");
    meta.className = "cardMeta";

    // Active slot badge — show which slot letter is active in this box
    const activeSlotLetter = (active && active[0] === String(boxNum)) ? active[1] : null;
    if (activeSlotLetter) {
      const activeBadge = document.createElement("span");
      activeBadge.className = "tag ok";
      activeBadge.textContent = `Slot ${activeSlotLetter} active`;
      meta.appendChild(activeBadge);
    }

    // Env sensor chips
    const bi = boxesInfo[boxNum] || {};
    const boxHistory = Array.isArray(envHistoryByBox[String(boxNum)]) ? envHistoryByBox[String(boxNum)] : [];
    const tC = bi.temperature_c;
    const rh = bi.humidity_pct;
    if (typeof tC === "number" && !Number.isNaN(tC)) {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "boxEnvChip boxEnvChipBtn";
      chip.textContent = `🌡 ${Math.round(tC)}°C`;
      chip.title = "Show temperature history";
      chip.addEventListener("click", (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        openEnvChartModal({
          printerId,
          printerName: state.printer_name || printerId || "Printer",
          boxId: String(boxNum),
          metricKey: "temperature_c",
          history: boxHistory,
        });
      });
      meta.appendChild(chip);
    }
    if (typeof rh === "number" && !Number.isNaN(rh)) {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "boxEnvChip boxEnvChipBtn";
      chip.textContent = `💧 ${Math.round(rh)}%`;
      chip.title = "Show humidity history";
      chip.addEventListener("click", (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        openEnvChartModal({
          printerId,
          printerName: state.printer_name || printerId || "Printer",
          boxId: String(boxNum),
          metricKey: "humidity_pct",
          history: boxHistory,
        });
      });
      meta.appendChild(chip);
    }

    head.appendChild(meta);
    card.appendChild(head);

    // Horizontal row of 4 slot pods
    const slotsWrap = document.createElement("div");
    slotsWrap.className = "boxSlots";
    for (const letter of ["A", "B", "C", "D"]) {
      const sid = `${boxNum}${letter}`;
      const m = metaFor(sid);
      const isAct = sid === active;
      slotsWrap.appendChild(makeSlotPod(sid, m, isAct));
    }
    card.appendChild(slotsWrap);

    return card;
  }

  function makeSpoolInputCard() {
    const card = document.createElement("section");
    card.className = "card";

    const head = document.createElement("div");
    head.className = "cardHead";
    const titleEl = document.createElement("div");
    titleEl.className = "cardTitle";
    titleEl.textContent = "Spool";
    head.appendChild(titleEl);

    if (active === PRINTER_SPOOL_SLOT) {
      const meta = document.createElement("div");
      meta.className = "cardMeta";
      const activeBadge = document.createElement("span");
      activeBadge.className = "tag ok";
      activeBadge.textContent = "Active";
      meta.appendChild(activeBadge);
      head.appendChild(meta);
    }
    card.appendChild(head);

    const slotsWrap = document.createElement("div");
    slotsWrap.className = "boxSlots boxSlotsSingle";
    const m = metaFor(PRINTER_SPOOL_SLOT);
    const isAct = PRINTER_SPOOL_SLOT === active;
    slotsWrap.appendChild(makeSlotPod(PRINTER_SPOOL_SLOT, m, isAct));
    card.appendChild(slotsWrap);

    return card;
  }

  for (const b of connectedBoxes) {
    boxesGrid.appendChild(makeBoxCard(b));
  }
  boxesGrid.appendChild(makeSpoolInputCard());

  // Right-side CFS stats panel
  renderCfsStats(state, history);

  // Active card
  if (active && (slots[active] || localSlots[active])) {
    const m = metaFor(active);
    activeRow.appendChild(slotEl(active, slotTitle(active), m, true, printerId));
    activeMeta.textContent = m.material ? (m.material + " · " + (m.color ? m.color.toUpperCase() : "")) : "—";
  } else {
    activeMeta.textContent = "—";
  }

  return block;
}

// Returns a fingerprint string covering elements that require full DOM rebuild.
// Things that change every tick (temps, progress) are intentionally excluded.
function _printerStructFingerprint(st) {
  const cfsSlots = st.cfs_slots || {};
  const spMeta = cfsSlots['SP'] || {};
  const moonPrinting = ['printing', 'paused'].includes(st.moon_print_state || '');
  const spExtruding = (st.moon_filament_used_mm || 0) > 0;
  const effectiveActive = st.cfs_active_slot || (moonPrinting && spMeta.present && spExtruding ? 'SP' : '');
  const slotSig = Object.keys(cfsSlots)
    .filter(k => /^[1-4][A-D]$/.test(k) || k === 'SP')
    .sort()
    .map(k => { const v = cfsSlots[k] || {}; return `${k}:${v.state ?? ''}:${!!v.present}:${v.selected ?? 0}`; })
    .join('|');
  const localSlots = st.slots || {};
  const localSig = Object.keys(localSlots).sort()
    .map(k => { const s = localSlots[k] || {}; return `${k}:${s.spoolman_id ?? ''}:${s.material ?? ''}:${s.color ?? s.color_hex ?? ''}:${s.name ?? ''}`; })
    .join('|');
  return `${effectiveActive}:${slotSig}:${JSON.stringify(cfsSlots._boxes || {})}:${localSig}`;
}

function _jobsFingerprint(printers) {
  return printers.map(p => {
    const hist = (p.state || p).job_history || [];
    return hist.map(j => j.ended_at || j.started_at || 0).join(',');
  }).join('|');
}

// Patches only live-changing data (temps, progress, badges) into an existing printer block.
function _patchPrinterBlock(block, st) {
  const live = key => block.querySelector(`[data-live="${key}"]`);
  const setText = (key, text) => { const el = live(key); if (el && el.textContent !== text) el.textContent = text; };

  const pBadge = live('printer-badge');
  if (pBadge) {
    const ok = !!st.printer_connected;
    const _narrow = window.innerWidth < 480;
    let text = ok ? (_narrow ? "Connected" : "Printer: connected") : (_narrow ? "Offline" : "Printer: disconnected");
    if (!ok && st.printer_last_error && !_narrow) text += ` (${st.printer_last_error})`;
    if (pBadge.textContent !== text) pBadge.textContent = text;
    pBadge.className = 'badge ' + (ok ? 'ok' : 'bad');
  }
  const cfsBadge = live('cfs-badge');
  if (cfsBadge) {
    const ok = !!st.cfs_connected;
    const _narrow = window.innerWidth < 480;
    const text = ok ? (_narrow ? "CFS ✓" : `CFS: detected · ${fmtTs(st.cfs_last_update)}`) : "CFS: —";
    if (cfsBadge.textContent !== text) cfsBadge.textContent = text;
    cfsBadge.className = 'badge ' + (ok ? 'ok' : 'warn');
  }

  const ps = st.moon_print_state || '';
  setText('print-state', ps ? ps.charAt(0).toUpperCase() + ps.slice(1) : 'Idle');

  function fmtTemp(actual, target) {
    const a = actual > 0 ? actual.toFixed(1) + '°' : '—';
    const t = target > 0 ? target.toFixed(0) + '°' : '';
    return t ? `${a} / ${t}` : a;
  }
  function patchTemp(key, actual, target) {
    const el = live(key);
    if (!el) return;
    const text = fmtTemp(actual, target);
    if (el.textContent !== text) el.textContent = text;
    const at = target > 0 && Math.abs(actual - target) < 3;
    el.className = 'tempVal' + (at ? ' atTemp' : '');
  }
  patchTemp('nozzle-val', st.moon_nozzle_temp || 0, st.moon_nozzle_target || 0);
  patchTemp('bed-val', st.moon_bed_temp || 0, st.moon_bed_target || 0);

  const section = live('progress-section');
  if (section) {
    const printing = ['printing', 'paused'].includes(ps);
    const progress = Number(st.moon_progress || 0);
    const show = printing || progress > 0;
    section.style.display = show ? '' : 'none';
    if (show) {
      const bar = live('progress-bar');
      if (bar) { const w = (progress * 100).toFixed(1) + '%'; if (bar.style.width !== w) bar.style.width = w; }
      const filename = st.moon_print_filename || '';
      const fnEl = live('filename');
      if (fnEl) {
        const t = filename.replace(/\.gcode$/i, '');
        if (fnEl.textContent !== t) { fnEl.textContent = t; fnEl.title = filename; }
        fnEl.style.display = filename ? '' : 'none';
      }
      const durationS = Number(st.moon_print_duration_s || 0);
      const pct = (progress * 100).toFixed(0) + '%';
      let timeStr = '';
      if (durationS > 0) {
        const h = Math.floor(durationS / 3600), m = Math.floor((durationS % 3600) / 60);
        timeStr = h > 0 ? `${h}h ${m}m elapsed` : `${m}m elapsed`;
      }
      setText('progress-meta', timeStr ? `${pct} · ${timeStr}` : pct);
    }
  }
}

function renderPrinterStatusCard(state) {
  const card = document.createElement("section");
  card.className = "card printerStatusCard";

  const head = document.createElement("div");
  head.className = "cardHead";
  const title = document.createElement("div");
  title.className = "cardTitle";
  title.textContent = "Printer";
  const stateTag = document.createElement("div");
  stateTag.className = "cardMeta";
  stateTag.dataset.live = "print-state";
  const ps = state.moon_print_state || "";
  stateTag.textContent = ps ? ps.charAt(0).toUpperCase() + ps.slice(1) : "Idle";
  head.appendChild(title);
  head.appendChild(stateTag);
  card.appendChild(head);

  const body = document.createElement("div");
  body.className = "printerStatusBody";

  // Temperatures row
  const tempsRow = document.createElement("div");
  tempsRow.className = "printerTemps";

  function tempWidget(label, liveKey, actual, target) {
    const w = document.createElement("div");
    w.className = "tempWidget";
    const lbl = document.createElement("div");
    lbl.className = "tempLabel";
    lbl.textContent = label;
    const val = document.createElement("div");
    val.className = "tempVal";
    val.dataset.live = liveKey;
    const actualStr = actual > 0 ? actual.toFixed(1) + "°" : "—";
    const targetStr = target > 0 ? target.toFixed(0) + "°" : "";
    val.textContent = targetStr ? `${actualStr} / ${targetStr}` : actualStr;
    if (target > 0 && Math.abs(actual - target) < 3) val.classList.add("atTemp");
    w.appendChild(lbl);
    w.appendChild(val);
    return w;
  }
  tempsRow.appendChild(tempWidget("Nozzle", "nozzle-val", state.moon_nozzle_temp || 0, state.moon_nozzle_target || 0));
  tempsRow.appendChild(tempWidget("Bed", "bed-val", state.moon_bed_temp || 0, state.moon_bed_target || 0));
  body.appendChild(tempsRow);

  // Progress section — always in DOM, hidden when not printing so it can be patched in-place
  const printing = ['printing', 'paused'].includes(ps);
  const progress = Number(state.moon_progress || 0);
  const filename = state.moon_print_filename || "";
  const durationS = Number(state.moon_print_duration_s || 0);

  const progressSection = document.createElement("div");
  progressSection.dataset.live = "progress-section";
  progressSection.style.display = (printing || progress > 0) ? '' : 'none';

  const fnRow = document.createElement("div");
  fnRow.className = "printerFilename";
  fnRow.dataset.live = "filename";
  fnRow.textContent = filename.replace(/\.gcode$/i, "");
  fnRow.title = filename;
  fnRow.style.display = filename ? '' : 'none';
  progressSection.appendChild(fnRow);

  const barWrap = document.createElement("div");
  barWrap.className = "progressBarWrap";
  const bar = document.createElement("div");
  bar.className = "progressBar";
  bar.dataset.live = "progress-bar";
  bar.style.width = (progress * 100).toFixed(1) + "%";
  barWrap.appendChild(bar);
  progressSection.appendChild(barWrap);

  const progressMeta = document.createElement("div");
  progressMeta.className = "progressMeta";
  progressMeta.dataset.live = "progress-meta";
  const pct = (progress * 100).toFixed(0) + "%";
  let timeStr = "";
  if (durationS > 0) {
    const h = Math.floor(durationS / 3600);
    const m = Math.floor((durationS % 3600) / 60);
    timeStr = h > 0 ? `${h}h ${m}m elapsed` : `${m}m elapsed`;
  }
  progressMeta.textContent = timeStr ? `${pct} · ${timeStr}` : pct;
  progressSection.appendChild(progressMeta);

  body.appendChild(progressSection);

  card.appendChild(body);
  return card;
}

function renderCameraCard(state, printerId) {
  const webcamUrl = state.moon_webcam_url || "";
  const card = document.createElement("section");
  card.className = "card cameraCard";

  const head = document.createElement("div");
  head.className = "cardHead";
  const title = document.createElement("div");
  title.className = "cardTitle";
  title.textContent = "Camera";
  const toggleBtn = document.createElement("button");
  toggleBtn.className = "btn mini";
  head.appendChild(title);
  head.appendChild(toggleBtn);
  card.appendChild(head);

  const streamWrap = document.createElement("div");
  streamWrap.className = "cameraWrap";

  if (!webcamUrl) {
    toggleBtn.style.display = "none";
    const hint = document.createElement("div");
    hint.className = "cameraPlaceholder";
    hint.textContent = "No webcam configured in Moonraker.";
    streamWrap.appendChild(hint);
  } else if (!isCameraEnabled(printerId)) {
    // Camera disabled via settings — compact state, no 16:9 space reserved
    toggleBtn.style.display = "none";
    const disabledHint = document.createElement("div");
    disabledHint.className = "cameraDisabled";
    disabledHint.textContent = "Camera disabled in Settings.";
    streamWrap.appendChild(disabledHint);
  } else {
    const isOpen = cameraOpen.has(printerId);

    // Placeholder — always in DOM so layout height never changes on toggle
    const placeholder = document.createElement("div");
    placeholder.className = "cameraPlaceholder";
    placeholder.textContent = "Camera hidden";
    placeholder.style.display = isOpen ? "none" : "";

    const img = document.createElement("img");
    img.className = "cameraFeed";
    img.alt = "Camera feed";
    img.style.display = isOpen ? "" : "none";
    if (isOpen) img.src = webcamUrl;

    toggleBtn.textContent = isOpen ? "Hide" : "Show";

    toggleBtn.addEventListener("click", () => {
      const showing = img.style.display !== "none";
      if (showing) {
        img.src = "";
        img.style.display = "none";
        placeholder.style.display = "";
        toggleBtn.textContent = "Show";
        cameraOpen.delete(printerId);
      } else {
        img.src = webcamUrl;
        img.style.display = "";
        placeholder.style.display = "none";
        toggleBtn.textContent = "Hide";
        cameraOpen.add(printerId);
      }
    });

    streamWrap.appendChild(placeholder);
    streamWrap.appendChild(img);
  }

  card.appendChild(streamWrap);
  return card;
}

function renderRecentJobsCard(printers) {
  const rows = [];
  for (const p of printers) {
    const pid = p.id || p.printer_id || p.host || "";
    const st = p.state || p;
    const hist = Array.isArray(st.job_history) ? st.job_history : [];
    for (const j of hist) {
      if (!j || typeof j !== "object") continue;
      const startedAt = Number(j.started_at || 0);
      const endedAt = Number(j.ended_at || 0);
      const spools = Array.isArray(j.spools) ? j.spools : [];
      const totalMeters = Number(j.total_meters || 0);
      const totalGrams = Number(j.total_grams || 0);
      rows.push({
        startedAt,
        endedAt,
        printer: String(j.printer_id || pid || "—"),
        jobName: String(j.job_name || ""),
        spools,
        totalMeters,
        totalGrams,
      });
    }
  }

  rows.sort((a, b) => (b.endedAt || 0) - (a.endedAt || 0));

  const PAGE_SIZE = 10;
  const totalPages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
  if (jobHistoryPage >= totalPages) jobHistoryPage = totalPages - 1;
  const pageStart = jobHistoryPage * PAGE_SIZE;
  const top = rows.slice(pageStart, pageStart + PAGE_SIZE);

  const block = document.createElement("section");
  block.className = "printerBlock";
  const head = document.createElement("div");
  head.className = "printerHead";
  const titleWrap = document.createElement("div");
  titleWrap.className = "printerTitleWrap";
  const title = document.createElement("div");
  title.className = "printerName";
  title.textContent = "Recent Jobs";
  const meta = document.createElement("div");
  meta.className = "printerMeta";
  meta.textContent = rows.length > PAGE_SIZE
    ? `Jobs ${pageStart + 1}–${Math.min(pageStart + PAGE_SIZE, rows.length)} of ${rows.length}`
    : `${rows.length} completed job${rows.length === 1 ? "" : "s"}`;
  titleWrap.appendChild(title);
  titleWrap.appendChild(meta);
  head.appendChild(titleWrap);
  block.appendChild(head);

  const body = document.createElement("section");
  body.className = "card";
  const list = document.createElement("div");
  list.className = "moonHist";
  body.appendChild(list);
  block.appendChild(body);

  if (!top.length) {
    const empty = document.createElement("div");
    empty.className = "emptyState";
    empty.textContent = "No completed jobs yet.";
    list.appendChild(empty);
    return block;
  }

  for (const j of top) {
    const entry = document.createElement("div");
    entry.className = "moonEntry";

    const row = document.createElement("div");
    row.className = "moonRow";
    const left = document.createElement("div");
    left.className = "moonJob";
    left.textContent = `Printer: ${j.printer}${j.jobName ? " · " + j.jobName : ""}`;
    const right = document.createElement("div");
    right.className = "moonNums";
    right.textContent = `${j.totalMeters.toFixed(1)} m · ${fmtG(j.totalGrams)}`;
    row.appendChild(left);
    row.appendChild(right);
    entry.appendChild(row);

    const sub = document.createElement("div");
    sub.className = "moonSub";
    sub.textContent = `Start: ${fmtTs(j.startedAt)} · End: ${fmtTs(j.endedAt)} · Print Time: ${fmtDuration(j.startedAt, j.endedAt)}`;
    entry.appendChild(sub);

    const spoolList = document.createElement("div");
    spoolList.className = "moonSpoolList";
    if (!j.spools.length) {
      const empty = document.createElement("div");
      empty.className = "moonSpoolEmpty";
      empty.textContent = "No spool usage recorded";
      spoolList.appendChild(empty);
    } else {
      for (const s of j.spools) {
        const spoolRow = document.createElement("div");
        spoolRow.className = "moonSpoolRow";

        const info = document.createElement("div");
        info.className = "moonSpoolInfo";
        const swatch = document.createElement("span");
        swatch.className = "moonSpoolSwatch";
        const col = normalizeHexColor(s.color_hex || s.color);
        if (col) swatch.style.background = col;
        info.appendChild(swatch);

        const textWrap = document.createElement("div");
        textWrap.className = "moonSpoolTextWrap";
        const label = document.createElement("div");
        label.className = "moonSpoolLabel";
        const spoolId = Number(s.spoolman_id || 0);
        label.textContent = `${recentJobSlotLabel(s.slot)} · ${spoolId > 0 ? "#" + spoolId : "not linked"}`;
        const spoolMeta = document.createElement("div");
        spoolMeta.className = "moonSpoolMeta";
        spoolMeta.textContent = `${(Number(s.meters || 0)).toFixed(2)} m · ${fmtG(Number(s.grams || 0))}`;
        textWrap.appendChild(label);
        textWrap.appendChild(spoolMeta);
        info.appendChild(textWrap);
        spoolRow.appendChild(info);

        const canRelink = spoolmanConfigured && !!j.printer && !!s.slot;
        const btn = document.createElement("button");
        btn.className = "btn mini";
        btn.textContent = spoolId > 0 ? "Relink" : "Link";
        if (!canRelink) btn.disabled = true;
        btn.onclick = async (ev) => {
          ev.preventDefault();
          ev.stopPropagation();
          await openHistoryRelinkModal({
            printerId: j.printer,
            endedAt: j.endedAt,
            slot: String(s.slot || ""),
            currentSpoolId: spoolId || null,
            grams: Number(s.grams || 0),
            meters: Number(s.meters || 0),
          });
        };
        spoolRow.appendChild(btn);
        spoolList.appendChild(spoolRow);
      }
    }
    entry.appendChild(spoolList);

    list.appendChild(entry);
  }

  if (totalPages > 1) {
    const pager = document.createElement("div");
    pager.className = "jobHistoryPager";
    const prev = document.createElement("button");
    prev.className = "btn mini";
    prev.textContent = "← Prev";
    prev.disabled = jobHistoryPage === 0;
    prev.onclick = () => { jobHistoryPage--; tick(); };
    const pageLabel = document.createElement("span");
    pageLabel.className = "jobHistoryPageLabel";
    pageLabel.textContent = `Page ${jobHistoryPage + 1} / ${totalPages}`;
    const next = document.createElement("button");
    next.className = "btn mini";
    next.textContent = "Next →";
    next.disabled = jobHistoryPage >= totalPages - 1;
    next.onclick = () => { jobHistoryPage++; tick(); };
    pager.appendChild(prev);
    pager.appendChild(pageLabel);
    pager.appendChild(next);
    list.appendChild(pager);
  }

  return block;
}

function renderCameraSettings(printers) {
  const wrap = $('settingsCameraRows');
  if (!wrap) return;
  wrap.innerHTML = '';

  const withCam = printers.filter(p => (p.state || p).moon_webcam_url);
  if (!withCam.length) {
    const none = document.createElement('div');
    none.className = 'settingsHint';
    none.style.padding = '10px 0';
    none.textContent = 'No webcam URLs detected from Moonraker.';
    wrap.appendChild(none);
    return;
  }

  for (const p of withCam) {
    const pid  = p.id || p.printer_id || p.host || '';
    const st   = p.state || p;
    const name = st.printer_name || pid || 'Printer';
    const enabled = isCameraEnabled(pid);

    const row = document.createElement('div');
    row.className = 'settingsItem settingsCameraRow';

    const lbl = document.createElement('div');
    lbl.className = 'settingsItemLabel';
    lbl.textContent = name;

    const switchLabel = document.createElement('label');
    switchLabel.className = 'mdSwitch';
    switchLabel.title = enabled ? 'Disable camera' : 'Enable camera';

    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = enabled;
    cb.addEventListener('change', () => {
      setCameraEnabled(pid, cb.checked);
      // Force a full rebuild of the affected printer block
      const existing = _renderedPrinters.get(pid);
      if (existing) {
        existing.fingerprint = null; // invalidate so next tick rebuilds
      }
      tick();
    });

    const track = document.createElement('span');
    track.className = 'mdSwitchTrack';

    switchLabel.appendChild(cb);
    switchLabel.appendChild(track);

    row.appendChild(lbl);
    row.appendChild(switchLabel);
    wrap.appendChild(row);
  }
}

function render(ui) {
  const printers = (ui && ui.printers) ? ui.printers : [];

  // Spoolman sync mode radio buttons (inside settings modal)
  // Use first printer's state to get the mode, or fall back to top-level
  const firstState = printers.length ? (printers[0].state || printers[0]) : {};
  const currentMode = (ui && ui.spoolman_mode) || firstState.spoolman_mode || "direct";
  document.querySelectorAll('input[name="spoolmanMode"]').forEach(r => {
    r.checked = (r.value === currentMode);
    r.onchange = async () => {
      await postJson("/api/ui/set_spoolman_mode", { mode: r.value });
    };
  });

  // Show/hide Spoolman sync mode section
  const smSection = $("settingsSpoolmanSection");
  if (smSection) smSection.style.display = spoolmanConfigured ? '' : 'none';

  // Populate Spoolman URL input — skip if the field is actively focused
  const smUrlInput = $("settingsSpoolmanUrl");
  if (smUrlInput && document.activeElement !== smUrlInput) {
    smUrlInput.value = (ui && ui.spoolman_url) || '';
  }

  // Spoolman external links (topbar and settings modal)
  for (const linkId of ["spoolmanExtLink", "spoolmanExtLinkSettings"]) {
    const smExtLink = $(linkId);
    if (smExtLink) {
      if (ui && ui.spoolman_url) {
        smExtLink.href = ui.spoolman_url;
        smExtLink.style.display = '';
      } else {
        smExtLink.style.display = 'none';
      }
    }
  }

  // Update heading / title (only on dashboard page; other pages set their own title)
  document.title = printers.length ? `CFSync · ${printers.length} printers` : "CFSync";
  if (_currentPage === 'dashboard') {
    const printerTitle = $("printerTitle");
    if (printerTitle) printerTitle.textContent = "CFSync";
    const sub = $("printerSubtitle");
    if (sub) {
      sub.textContent = printers.length ? `${printers.length} printer${printers.length === 1 ? "" : "s"} configured` : "No printers configured";
    }
  }

  const printerBadge = $("printerBadge");
  const cfsBadge = $("cfsBadge");
  const total = printers.length;
  const connected = printers.filter(p => (p.state || p).printer_connected).length;
  const cfsOk = printers.filter(p => (p.state || p).cfs_connected).length;

  const _topNarrow = window.innerWidth < 480;
  if (printerBadge) {
    if (!total) {
      badge(printerBadge, "Printers: —", "warn");
    } else {
      const cls = connected === total ? "ok" : (connected > 0 ? "warn" : "bad");
      badge(printerBadge, _topNarrow ? `${connected}/${total}` : `Printers: ${connected}/${total} online`, cls);
    }
  }
  if (cfsBadge) {
    if (!total) {
      badge(cfsBadge, "CFS: —", "warn");
    } else {
      badge(cfsBadge, _topNarrow ? `CFS: ${cfsOk}` : `CFS: ${cfsOk} detected`, cfsOk > 0 ? "ok" : "warn");
    }
  }

  const wrap = $("printersWrap");
  if (!wrap) return;

  if (!printers.length) {
    wrap.innerHTML = "";
    _renderedPrinters.clear();
    _renderedJobsCard = null;
    const jobsWrap = $('jobsPageWrap');
    if (jobsWrap) jobsWrap.innerHTML = '';
    const empty = document.createElement("div");
    empty.className = "emptyState";
    empty.textContent = "No printers configured. Set printer_urls (or printers) in data/config.json and reload.";
    wrap.appendChild(empty);
    return;
  }

  // Remove stale empty state if present
  const emptyEl = wrap.querySelector('.emptyState');
  if (emptyEl) emptyEl.remove();

  printerDisplayNames = {};
  const currentPids = new Set(printers.map(p => p.id || p.printer_id || p.host || ''));

  // Remove blocks for printers no longer in the list
  for (const [pid] of _renderedPrinters) {
    if (!currentPids.has(pid)) {
      _renderedPrinters.get(pid).block.remove();
      _renderedPrinters.delete(pid);
    }
  }

  for (const p of printers) {
    const pid = p.id || p.printer_id || p.host || "";
    const st = p.state || p;
    printerDisplayNames[pid] = st.printer_name || pid;

    const fingerprint = _printerStructFingerprint(st);
    const existing = _renderedPrinters.get(pid);

    if (!existing || !existing.block.isConnected) {
      // New printer — append to main content
      const block = renderPrinter(pid, st);
      wrap.appendChild(block);
      _renderedPrinters.set(pid, { block, fingerprint });
    } else if (existing.fingerprint !== fingerprint) {
      // Structure changed — full rebuild for this printer only
      const block = renderPrinter(pid, st);
      wrap.replaceChild(block, existing.block);
      _renderedPrinters.set(pid, { block, fingerprint });
    } else {
      // No structural change — patch live data in-place
      _patchPrinterBlock(existing.block, st);
    }
  }

  // Camera settings — per-printer toggles on Settings page
  renderCameraSettings(printers);

  // Recent jobs — rendered into the Jobs page
  const jobsWrap = $('jobsPageWrap');
  
  if (jobsWrap) {
      // Include actual page to force rendering when
      // pressing Prev / Next.
      const jobsFp = `${_jobsFingerprint(printers)}|page:${jobHistoryPage}`;
  
      if (_renderedJobsCard?.fingerprint !== jobsFp) {
          const newCard = renderRecentJobsCard(printers);
  
          jobsWrap.innerHTML = '';
          jobsWrap.appendChild(newCard);
  
          _renderedJobsCard = {
              el: newCard,
              fingerprint: jobsFp
          };
      }
  }
}

async function tick() {
  try {
    const r = await fetch("/api/ui/state", { cache: "no-store" });
    const j = await r.json();
    const st = j.result || j;
    spoolmanConfigured = !!st.spoolman_configured;
    render(st);
  } catch (e) {
    const pb = $("printerBadge");
    const cb = $("cfsBadge");
    if (pb) badge(pb, 'Printers: —', "warn");
    if (cb) badge(cb, 'CFS: —', "warn");
  }
}

// --- Refresh control (client-side only) ---
let refreshTimer = null;
let refreshMs = Number(localStorage.getItem('refreshMs') || 10000);
if (!Number.isFinite(refreshMs) || refreshMs < 2000) refreshMs = 10000;
let refreshPaused = localStorage.getItem('refreshPaused') === '1';

function applyRefreshTimer() {
  if (refreshTimer) clearInterval(refreshTimer);
  refreshTimer = null;
  if (!refreshPaused) refreshTimer = setInterval(tick, refreshMs);

  const sel = $('refreshSelect');
  const btn = $('refreshToggle');
  if (sel) sel.value = String(refreshMs);
  if (btn) {
    btn.textContent = refreshPaused ? '▶' : '⏸';
    btn.classList.toggle('paused', refreshPaused);
  }
}

function initRefreshControls() {
  const sel = $('refreshSelect');
  const btn = $('refreshToggle');
  if (sel) {
    sel.value = String(refreshMs);
    sel.onchange = () => {
      refreshMs = Number(sel.value || 10000);
      if (!Number.isFinite(refreshMs) || refreshMs < 2000) refreshMs = 10000;
      localStorage.setItem('refreshMs', String(refreshMs));
      applyRefreshTimer();
    };
  }
  if (btn) {
    btn.onclick = () => {
      refreshPaused = !refreshPaused;
      localStorage.setItem('refreshPaused', refreshPaused ? '1' : '0');
      if (!refreshPaused) tick();
      applyRefreshTimer();
    };
  }
  applyRefreshTimer();
}

function initFluiddBookmarklet() {
  const origin = window.location.origin;
  const code = "javascript:(function(){window.CFSYNC_URL='" + origin + "';" +
    "var s=document.createElement('script');" +
    "s.src='" + origin + "/static/fluidd-panel.js?v=1&t='+Date.now();" +
    "document.head.appendChild(s);})();";

  // Wire up both footer link and settings modal link
  for (const id of ['fluiddBookmarklet', 'fluiddBookmarkletSettings']) {
    const link = document.getElementById(id);
    if (link) link.href = code;
  }

  // Wire up both copy buttons (footer + settings modal)
  function makeCopyHandler(btn) {
    if (!btn) return;
    btn.onclick = async () => {
      try {
        await navigator.clipboard.writeText(code);
        const prev = btn.textContent;
        btn.textContent = '✓';
        setTimeout(() => { btn.textContent = prev; }, 2000);
      } catch (_) {
        prompt('Copy this bookmarklet URL and save it as a browser bookmark:', code);
      }
    };
  }
  makeCopyHandler(document.getElementById('fluiddCopyBtn'));
  makeCopyHandler(document.getElementById('fluiddCopyBtnSettings'));
}

function initFluiddUserscript() {
  function makeUserscriptHandler(btn) {
    if (!btn) return;
    btn.onclick = async () => {
      const origin = window.location.origin;
      const fluiddUrl = prompt(
        'Enter your Fluidd URL (e.g. http://192.168.1.100)\nThis will be used for the @match rule so the script only runs on Fluidd:',
        'http://192.168.1.100'
      );
      if (!fluiddUrl) return;

      const matchUrl = fluiddUrl.replace(/\/$/, '') + '/*';
      const script = [
        '// ==UserScript==',
        '// @name         CFSync — Fluidd Panel',
        '// @namespace    http://tampermonkey.net/',
        '// @version      1.0',
        '// @description  Shows live CFS slot status in Fluidd\'s Runout Sensors card',
        '// @match        ' + matchUrl,
        '// @grant        none',
        '// ==/UserScript==',
        '',
        '(function () {',
        "  'use strict';",
        "  window.CFSYNC_URL = '" + origin + "';",
        "  var s = document.createElement('script');",
        "  s.src = window.CFSYNC_URL + '/static/fluidd-panel.js?v=1&t=' + Date.now();",
        "  document.head.appendChild(s);",
        '})();',
      ].join('\n');

      try {
        await navigator.clipboard.writeText(script);
        const prev = btn.textContent;
        btn.textContent = '✓ Copied!';
        setTimeout(() => { btn.textContent = prev; }, 2500);
      } catch (_) {
        prompt('Copy this userscript and paste it into Tampermonkey → New Script:', script);
      }
    };
  }

  // Wire up both footer button and settings modal button
  makeUserscriptHandler(document.getElementById('fluiddUserscriptBtn'));
  makeUserscriptHandler(document.getElementById('fluiddUserscriptBtnSettings'));
}

function _initSettingsHandlers() {
  // Theme toggle
  const themeToggle = document.getElementById('themeToggle');
  if (themeToggle) {
    themeToggle.checked = document.documentElement.getAttribute('data-theme') === 'dark';
    themeToggle.onchange = () => {
      const t = themeToggle.checked ? 'dark' : 'light';
      document.documentElement.setAttribute('data-theme', t);
      localStorage.setItem('theme', t);
    };
  }

  // Spoolman URL save — wired regardless of which container holds the form
  const urlInput  = $('settingsSpoolmanUrl');
  const urlSave   = $('settingsSpoolmanUrlSave');
  const urlStatus = $('settingsSpoolmanUrlStatus');
  if (urlSave && urlInput) {
    urlSave.onclick = async () => {
      urlSave.disabled = true;
      if (urlStatus) urlStatus.textContent = 'Saving…';
      try {
        const res = await postJson('/api/ui/set_spoolman_url', { url: urlInput.value.trim() });
        const st = (res && res.result) ? res.result : res;
        spoolmanConfigured = !!st.spoolman_configured;
        render(st);
        if (urlStatus) urlStatus.textContent = st.spoolman_url ? '✓ Saved' : '✓ Cleared';
      } catch (e) {
        if (urlStatus) urlStatus.textContent = 'Error: ' + (e.message || String(e));
      } finally {
        urlSave.disabled = false;
        setTimeout(() => { if (urlStatus) urlStatus.textContent = ''; }, 3000);
      }
    };
  }
}

function initNavDrawer() {
  const drawer   = $('navDrawer');
  if (!drawer) return;
  const backdrop = $('navDrawerBackdrop');
  const closeBtn = $('navDrawerClose');

  function openDrawer() {
    _drawerOpen = true;
    drawer.classList.add('navDrawer--open');
  }
  function closeDrawer() {
    _drawerOpen = false;
    drawer.classList.remove('navDrawer--open');
  }

  const menuBtn = $('menuBtn');
  if (menuBtn) menuBtn.onclick = openDrawer;
  if (closeBtn)    closeBtn.onclick    = (ev) => { ev.stopPropagation(); closeDrawer(); };
  if (backdrop)    backdrop.onclick    = closeDrawer;

  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && _drawerOpen) closeDrawer();
  });

  // Nav item clicks — navigateTo closes the drawer automatically
  for (const item of document.querySelectorAll('.navItem[data-page]')) {
    item.addEventListener('click', () => navigateTo(item.dataset.page));
  }

  _initSettingsHandlers();
}

function boot() {
  initSpoolModal();
  initHistoryRelinkModal();
  initNavDrawer();
  initEnvChartModal();
  initRefreshControls();
  initFluiddBookmarklet();
  initFluiddUserscript();
  tick();
}

// app.js may be loaded before some HTML (e.g. the spool modal) in certain
// templates. Ensure we wire up DOM-dependent handlers only after DOM is ready.
if (document.readyState === 'loading') {
  window.addEventListener('DOMContentLoaded', boot, { once: true });
} else {
  boot();
}
