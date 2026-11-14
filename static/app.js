"use strict";

const state = {
  activeTab: "explore",
  exploreMode: "mover", // 'mover' | 'ftp'
  moverOp: "copiar", // 'copiar' | 'mover'
  useAutoDest: false,
  queueTab: "mover",
  dataRoot: "/",

  srcPath: "",
  srcParent: null,
  srcData: null,
  srcLoading: false,
  srcError: "",
  selectedPaths: new Set(),
  entryInfo: new Map(), // path -> {name, is_dir, size}
  dirSizes: {}, // path -> {bytes, files}
  sort: { key: "name", order: "asc" },
  dstSort: { key: "name", order: "asc" },
  search: { active: false, q: "", loading: false, results: [], truncated: false, error: "", unsupported: false },

  dstLocalPath: "",
  dstLocalParent: null,
  dstLocalData: null,
  dstRemotePath: "/",
  dstRemoteData: null,

  workers: { mover: null, ftp: null },
  backup: null,
  ftpSpace: null,
  ftpSpaceUnsupported: false,
  ftpSpaceAt: 0,

  lastQueueItems: [],
  queueSig: "",
  queueDirty: false,
  drag: null,

  history: { items: [], total: 0, offset: 0 },
  trash: { items: [], selected: new Set() },
  trashAvailable: null,

  settings: null,
  favorites: [],
  picker: { targetInputId: null, path: "", parent: null, onPick: null },
};

const $ = (id) => document.getElementById(id);
const IS_MOBILE = () => window.matchMedia("(max-width: 768px)").matches;

// =========================================================================
// UTILIDADES
// =========================================================================
const storage = {
  get(key, fallback = null) {
    try {
      const v = localStorage.getItem(key);
      return v === null ? fallback : v;
    } catch {
      return fallback;
    }
  },
  set(key, value) {
    try {
      localStorage.setItem(key, value);
    } catch {
      /* almacenamiento no disponible */
    }
  },
};

function fmtNum(val, decimals) {
  return Number(val).toLocaleString("es-ES", { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
}

function fmtBytes(bytes) {
  if (bytes == null || bytes === "" || isNaN(bytes)) return "-";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let val = Number(bytes);
  let i = 0;
  while (val >= 1024 && i < units.length - 1) {
    val /= 1024;
    i++;
  }
  return `${fmtNum(val, i === 0 ? 0 : 1)} ${units[i]}`;
}

function fmtSpeed(bps) {
  if (!bps || isNaN(bps)) return "--";
  return `${fmtBytes(bps)}/s`;
}

function fmtDuration(sec) {
  if (sec == null || isNaN(sec) || sec < 0) return "--";
  sec = Math.round(Number(sec));
  if (sec < 60) return `${sec} s`;
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  if (h > 0) return `${h} h ${String(m).padStart(2, "0")} min`;
  return s ? `${m} min ${s} s` : `${m} min`;
}

function toDate(ts) {
  if (ts == null || ts === "") return null;
  if (typeof ts === "number" || /^\d+(\.\d+)?$/.test(String(ts))) {
    const n = Number(ts);
    return new Date(n < 1e12 ? n * 1000 : n);
  }
  const d = new Date(ts);
  return isNaN(d.getTime()) ? null : d;
}

function fmtDate(ts, withYear = true) {
  const d = toDate(ts);
  if (!d) return "-";
  const now = new Date();
  const time = d.toLocaleTimeString("es-ES", { hour: "2-digit", minute: "2-digit" });
  if (d.toDateString() === now.toDateString()) return `hoy ${time}`;
  const y = new Date(now);
  y.setDate(now.getDate() - 1);
  if (d.toDateString() === y.toDateString()) return `ayer ${time}`;
  const date = d.toLocaleDateString("es-ES", withYear
    ? { day: "2-digit", month: "2-digit", year: "numeric" }
    : { day: "2-digit", month: "2-digit" });
  return `${date} ${time}`;
}

function fmtRelativeFuture(ts) {
  const d = toDate(ts);
  if (!d) return "-";
  const diff = d.getTime() - Date.now();
  if (diff <= 0) return "en breve";
  const days = Math.floor(diff / 86400000);
  if (days >= 1) return `en ${days} día${days === 1 ? "" : "s"}`;
  const hours = Math.max(1, Math.round(diff / 3600000));
  return `en ${hours} h`;
}

function escapeHtml(str) {
  return String(str ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function baseName(p) {
  const s = String(p || "").replace(/\/+$/, "");
  return s.slice(s.lastIndexOf("/") + 1) || s || "/";
}

function relPath(p) {
  const root = (state.dataRoot || "/").replace(/\/+$/, "");
  const s = String(p || "");
  if (root && root !== "/" && (s === root || s.startsWith(`${root}/`))) {
    return s.slice(root.length).replace(/^\/+/, "") || "/";
  }
  return s;
}

function debounce(fn, ms) {
  let t = null;
  const wrapped = (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
  wrapped.cancel = () => clearTimeout(t);
  return wrapped;
}

async function api(url, opts = {}) {
  const res = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    let msg = data.detail || data.error || `Error ${res.status}`;
    if (Array.isArray(msg)) msg = msg.map((d) => d.msg || JSON.stringify(d)).join("; ");
    const err = new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
    err.status = res.status;
    throw err;
  }
  return data;
}

const isMissing = (err) => err && (err.status === 404 || err.status === 405);

async function copyText(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
    } else {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      ta.remove();
    }
    showToast("Ruta copiada al portapapeles");
  } catch {
    showToast("No se pudo copiar la ruta", { type: "err" });
  }
}

// =========================================================================
// AVISOS (TOAST) CON ACCIÓN OPCIONAL
// =========================================================================
function showToast(msg, opts = {}) {
  const el = $("toast");
  const actBtn = $("toastAction");
  $("toastMsg").textContent = msg;
  el.classList.toggle("err", opts.type === "err");
  if (opts.action) {
    actBtn.textContent = opts.action.label;
    actBtn.classList.remove("hidden");
    actBtn.onclick = async () => {
      el.classList.add("hidden");
      try {
        await opts.action.fn();
      } catch (err) {
        showToast(err.message, { type: "err" });
      }
    };
  } else {
    actBtn.classList.add("hidden");
    actBtn.onclick = null;
  }
  el.classList.remove("hidden");
  clearTimeout(showToast._timer);
  showToast._timer = setTimeout(() => el.classList.add("hidden"), opts.timeout || (opts.action ? 8000 : 3500));
}

// =========================================================================
// MODALES ACCESIBLES (pila, foco atrapado, Esc, restaurar foco)
// =========================================================================
const modalStack = [];

function focusables(root) {
  return Array.from(root.querySelectorAll(
    'button:not([disabled]), [href], input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
  )).filter((el) => el.offsetParent !== null);
}

function openModal(el, { onClose = null, initialFocus = null } = {}) {
  closePopMenu();
  modalStack.push({ el, onClose, returnFocus: document.activeElement });
  el.classList.remove("hidden");
  requestAnimationFrame(() => {
    const target = initialFocus || focusables(el)[0];
    if (target) target.focus();
  });
}

function closeModal(el, reason = "close") {
  const idx = modalStack.findIndex((m) => m.el === el);
  if (idx === -1) return;
  const [entry] = modalStack.splice(idx, 1);
  el.classList.add("hidden");
  if (entry.onClose) entry.onClose(reason);
  if (entry.returnFocus && document.contains(entry.returnFocus)) {
    try { entry.returnFocus.focus(); } catch { /* elemento ya no enfocable */ }
  }
}

document.addEventListener("keydown", (e) => {
  const top = modalStack[modalStack.length - 1];
  if (!top) return;
  if (e.key === "Escape") {
    e.preventDefault();
    closeModal(top.el, "escape");
  } else if (e.key === "Tab") {
    const items = focusables(top.el);
    if (!items.length) return;
    const first = items[0];
    const last = items[items.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  }
});

document.querySelectorAll(".modal").forEach((m) => {
  m.addEventListener("mousedown", (e) => {
    if (e.target === m) closeModal(m, "backdrop");
  });
  m.querySelectorAll("[data-close-modal]").forEach((b) => {
    b.addEventListener("click", () => closeModal(m, "close"));
  });
});

// Diálogo genérico: sustituye a confirm() / prompt() nativos
function uiDialog({
  title = "Confirmar",
  html = "",
  input = null, // {label, value, placeholder, validate}
  okText = "Aceptar",
  cancelText = "Cancelar",
  danger = false,
  hideCancel = false,
  onOpen = null,
} = {}) {
  return new Promise((resolve) => {
    const modal = $("dialogModal");
    const okBtn = $("btnDialogOk");
    const cancelBtn = $("btnDialogCancel");
    const form = $("dialogForm");
    const inputEl = $("dialogInput");
    const errEl = $("dialogInputErr");
    let settled = false;

    $("dialogTitle").textContent = title;
    $("dialogBody").innerHTML = html;
    $("dialogBody").classList.toggle("hidden", !html);
    okBtn.textContent = okText;
    okBtn.className = `btn ${danger ? "btn-danger solid" : "btn-primary"}`;
    okBtn.disabled = false;
    cancelBtn.textContent = cancelText;
    cancelBtn.classList.toggle("hidden", hideCancel);
    errEl.classList.add("hidden");

    if (input) {
      form.classList.remove("hidden");
      $("dialogInputLabel").textContent = input.label || "Valor";
      inputEl.value = input.value || "";
      inputEl.placeholder = input.placeholder || "";
    } else {
      form.classList.add("hidden");
    }

    const finish = (ok) => {
      if (settled) return;
      if (ok && input && input.validate) {
        const msg = input.validate(inputEl.value.trim());
        if (msg) {
          errEl.textContent = msg;
          errEl.classList.remove("hidden");
          inputEl.focus();
          return;
        }
      }
      settled = true;
      const checks = {};
      $("dialogBody").querySelectorAll("input[type=checkbox][name]").forEach((c) => { checks[c.name] = c.checked; });
      const value = input ? inputEl.value.trim() : null;
      cleanup();
      closeModal(modal, ok ? "ok" : "cancel");
      resolve({ ok, value, checks });
    };

    const onOk = () => finish(true);
    const onCancel = () => finish(false);
    const onSubmit = (e) => { e.preventDefault(); finish(true); };
    function cleanup() {
      okBtn.removeEventListener("click", onOk);
      cancelBtn.removeEventListener("click", onCancel);
      $("btnDialogX").removeEventListener("click", onCancel);
      form.removeEventListener("submit", onSubmit);
    }
    okBtn.addEventListener("click", onOk);
    cancelBtn.addEventListener("click", onCancel);
    $("btnDialogX").addEventListener("click", onCancel);
    form.addEventListener("submit", onSubmit);

    openModal(modal, {
      initialFocus: input ? inputEl : (danger ? cancelBtn : okBtn),
      onClose: (reason) => {
        if (!settled) {
          settled = true;
          cleanup();
          resolve({ ok: false, value: null, checks: {} });
        }
        return reason;
      },
    });
    if (input) requestAnimationFrame(() => inputEl.select());
    if (onOpen) onOpen($("dialogBody"), okBtn);
  });
}

async function uiConfirm(opts) {
  const res = await uiDialog(opts);
  return res.ok;
}

async function uiPrompt(opts) {
  const res = await uiDialog({ ...opts, input: { label: opts.label, value: opts.value, placeholder: opts.placeholder, validate: opts.validate } });
  return res.ok ? res.value : null;
}

const validateName = (v) => {
  if (!v) return "Escribe un nombre";
  if (v.includes("/")) return "El nombre no puede contener «/»";
  if (v === "." || v === "..") return "Nombre no válido";
  return "";
};

// =========================================================================
// MENÚ EMERGENTE (⋯)
// =========================================================================
let popMenuAnchor = null;

function openPopMenu(anchor, items) {
  const menu = $("popMenu");
  if (popMenuAnchor === anchor && !menu.classList.contains("hidden")) {
    closePopMenu();
    return;
  }
  closePopMenu();
  menu.innerHTML = "";
  items.forEach((it) => {
    if (it.sep) {
      menu.appendChild(document.createElement("hr"));
      return;
    }
    const b = document.createElement("button");
    b.type = "button";
    b.setAttribute("role", "menuitem");
    b.innerHTML = `<span aria-hidden="true">${escapeHtml(it.icon || "")}</span><span>${escapeHtml(it.label)}</span>`;
    if (it.danger) b.classList.add("danger");
    if (it.disabled) b.disabled = true;
    b.onclick = () => {
      closePopMenu();
      it.onClick();
    };
    menu.appendChild(b);
  });
  menu.classList.remove("hidden");
  const r = anchor.getBoundingClientRect();
  const mw = menu.offsetWidth;
  const mh = menu.offsetHeight;
  const bottomLimit = window.innerHeight - (parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--bottom-nav-h")) || 0) - 8;
  let top = r.bottom + 4;
  if (top + mh > bottomLimit) top = Math.max(8, r.top - mh - 4);
  let left = Math.min(window.innerWidth - mw - 8, r.right - mw);
  left = Math.max(8, left);
  menu.style.top = `${top}px`;
  menu.style.left = `${left}px`;
  popMenuAnchor = anchor;
  anchor.setAttribute("aria-expanded", "true");
  const first = menu.querySelector("button:not([disabled])");
  if (first) first.focus();
}

function closePopMenu() {
  const menu = $("popMenu");
  if (menu.classList.contains("hidden")) return;
  menu.classList.add("hidden");
  if (popMenuAnchor) {
    popMenuAnchor.setAttribute("aria-expanded", "false");
    if (document.activeElement && menu.contains(document.activeElement)) popMenuAnchor.focus();
  }
  popMenuAnchor = null;
  if (state.queueDirty) loadQueue(true);
}

document.addEventListener("mousedown", (e) => {
  if (!$("popMenu").contains(e.target) && e.target !== popMenuAnchor && !(popMenuAnchor && popMenuAnchor.contains(e.target))) {
    closePopMenu();
  }
});
$("popMenu").addEventListener("keydown", (e) => {
  const items = Array.from($("popMenu").querySelectorAll("button:not([disabled])"));
  const idx = items.indexOf(document.activeElement);
  if (e.key === "Escape") {
    e.preventDefault();
    e.stopPropagation();
    closePopMenu();
  } else if (e.key === "ArrowDown") {
    e.preventDefault();
    items[(idx + 1) % items.length].focus();
  } else if (e.key === "ArrowUp") {
    e.preventDefault();
    items[(idx - 1 + items.length) % items.length].focus();
  } else if (e.key === "Tab") {
    closePopMenu();
  }
});
window.addEventListener("resize", closePopMenu);
window.addEventListener("scroll", () => { if (!IS_MOBILE()) closePopMenu(); }, { passive: true });

// =========================================================================
// TEMA (automático / claro / oscuro)
// =========================================================================
const THEME_META = {
  auto: { icon: "🖥", label: "automático" },
  light: { icon: "☀️", label: "claro" },
  dark: { icon: "🌙", label: "oscuro" },
};

function getTheme() {
  const t = storage.get("toolbox_theme", "auto");
  return THEME_META[t] ? t : "auto";
}

function applyTheme(theme) {
  if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;
  else delete document.documentElement.dataset.theme;
  storage.set("toolbox_theme", theme);
  const meta = THEME_META[theme];
  $("themeIcon").textContent = meta.icon;
  $("btnThemeToggle").setAttribute("aria-label", `Tema: ${meta.label} (cambiar)`);
  $("btnThemeToggle").title = `Tema: ${meta.label}`;
  $("cfgTheme").value = theme;
  document.querySelectorAll("[data-theme-set]").forEach((b) => {
    const on = b.dataset.themeSet === theme;
    b.classList.toggle("active", on);
    b.setAttribute("aria-pressed", String(on));
  });
  requestAnimationFrame(() => {
    const surface = getComputedStyle(document.documentElement).getPropertyValue("--surface").trim();
    if (surface) $("metaThemeColor").setAttribute("content", surface);
  });
}

$("btnThemeToggle").onclick = () => {
  const order = ["auto", "light", "dark"];
  applyTheme(order[(order.indexOf(getTheme()) + 1) % order.length]);
};
document.querySelectorAll("[data-theme-set]").forEach((b) => {
  b.onclick = () => applyTheme(b.dataset.themeSet);
});
$("cfgTheme").onchange = (e) => applyTheme(e.target.value);
window.matchMedia("(prefers-color-scheme: light)").addEventListener?.("change", () => applyTheme(getTheme()));

// =========================================================================
// NAVEGACIÓN ENTRE SECCIONES (pestañas superiores + barra inferior móvil)
// =========================================================================
const MORE_TABS = new Set(["history", "trash", "logs", "readme"]);

function switchTab(tab) {
  state.activeTab = tab;
  document.querySelectorAll("#mainTabs .tab").forEach((t) => {
    const on = t.dataset.tab === tab;
    t.classList.toggle("active", on);
    if (on) t.setAttribute("aria-current", "page");
    else t.removeAttribute("aria-current");
  });
  document.querySelectorAll("#bottomNav .bn-item").forEach((b) => {
    const on = b.dataset.tab ? b.dataset.tab === tab : (b.id === "bnMore" && MORE_TABS.has(tab));
    b.classList.toggle("active", on);
    if (on) b.setAttribute("aria-current", "page");
    else b.removeAttribute("aria-current");
  });
  document.querySelectorAll(".tab-panel").forEach((p) => p.classList.toggle("active", p.id === `tab-${tab}`));
  closeModal($("moreSheet"));
  closePopMenu();
  window.scrollTo({ top: 0 });
  if (tab === "queue") {
    loadQueue(true);
    if (state.queueTab === "ftp") fetchFtpSpace(true);
  }
  if (tab === "config") loadSettings();
  if (tab === "logs") loadLogs();
  if (tab === "readme") loadReadme();
  if (tab === "history") loadHistory(true);
  if (tab === "trash") loadTrash();
}

$("mainTabs").addEventListener("click", (e) => {
  const btn = e.target.closest(".tab");
  if (btn) switchTab(btn.dataset.tab);
});
$("bottomNav").addEventListener("click", (e) => {
  const btn = e.target.closest(".bn-item");
  if (!btn) return;
  if (btn.id === "bnMore") {
    $("bnMore").setAttribute("aria-expanded", "true");
    openModal($("moreSheet"), { onClose: () => $("bnMore").setAttribute("aria-expanded", "false") });
    return;
  }
  switchTab(btn.dataset.tab);
});
$("moreSheet").addEventListener("click", (e) => {
  const item = e.target.closest(".sheet-item");
  if (item) switchTab(item.dataset.tab);
});

// =========================================================================
// ESTADO DE LAS COLAS
// =========================================================================
function normalizeWorker(info) {
  if (!info) return null;
  const prog = info.progress || {};
  let cur = null;
  if (info.current && (info.current.name || info.current.id != null)) {
    cur = { ...info.current };
  } else if (prog.filename) {
    cur = {
      id: prog.item_id,
      name: prog.filename,
      bytes_done: prog.transferred_bytes,
      total_bytes: prog.total_bytes,
      percent: prog.percent,
      speed_txt: prog.speed,
      eta_txt: prog.eta,
    };
  }
  const running = !!info.running;
  let phase = info.phase || prog.phase || "";
  if (!phase) {
    if (!running) phase = "idle";
    else if (!cur) phase = "preparing";
    else {
      const noSpeed = !cur.speed_bps && (!cur.speed_txt || cur.speed_txt === "--");
      phase = Number(cur.percent || 0) <= 0 && noSpeed ? "preparing" : "transferring";
    }
  }
  const c = info.counts || {};
  const pendingCount = (c.PEND || 0) + (c.REAN || 0) + (c.PROC || 0);
  let agg = info.aggregate || null;
  if (!agg) {
    agg = {
      total_items: (c.OK || 0) + pendingCount,
      done_items: c.OK || 0,
      total_bytes: null,
      done_bytes: null,
      eta_s: null,
    };
  }
  return { ...info, running, cur, phase, counts: c, pendingCount, agg };
}

function curSpeed(cur) {
  if (!cur) return "--";
  if (cur.speed_bps) return fmtSpeed(cur.speed_bps);
  return cur.speed_txt && cur.speed_txt !== "--" ? cur.speed_txt : "--";
}

function curEta(cur) {
  if (!cur) return "--";
  if (cur.eta_s != null) return fmtDuration(cur.eta_s);
  return cur.eta_txt && cur.eta_txt !== "--" ? cur.eta_txt : "--";
}

function aggregateText(w) {
  const a = w.agg;
  if (!a || !a.total_items) return "";
  const parts = [`${a.done_items || 0}/${a.total_items}`];
  if (a.total_bytes) parts.push(`${fmtBytes(a.done_bytes || 0)}/${fmtBytes(a.total_bytes)}`);
  else if (w.pending_bytes) parts.push(`quedan ${fmtBytes(w.pending_bytes)}`);
  // Ignorar estimaciones absurdas (p. ej. calculadas con la velocidad de un fichero diminuto)
  if (a.eta_s != null && a.eta_s >= 0 && a.eta_s < 30 * 86400) parts.push(`ETA total ${fmtDuration(a.eta_s)}`);
  return parts.join(" · ");
}

function aggregateFraction(w) {
  const a = w.agg;
  if (!a || !a.total_items) return 0;
  if (a.total_bytes) return Math.min(1, (a.done_bytes || 0) / a.total_bytes);
  return Math.min(1, (a.done_items || 0) / a.total_items);
}

function setProgressBar(barEl, fillEl, pct, indeterminate) {
  barEl.classList.toggle("indeterminate", !!indeterminate);
  if (indeterminate) {
    barEl.removeAttribute("aria-valuenow");
    barEl.setAttribute("aria-valuetext", "Preparando");
  } else {
    const v = Math.min(100, Math.max(0, Number(pct) || 0));
    fillEl.style.width = `${v}%`;
    barEl.setAttribute("aria-valuenow", v.toFixed(0));
    barEl.removeAttribute("aria-valuetext");
  }
}

function ftpSpaceText() {
  const sp = state.ftpSpace;
  if (state.ftpSpaceUnsupported || !sp) return "";
  if (sp.free_bytes != null) return `SD libre: ${fmtBytes(sp.free_bytes)}`;
  if (sp.supported === false) return "SD: espacio no disponible";
  return ""; // aún sin comprobar
}

function renderWorkerCard(kind, w) {
  const p = kind === "mover" ? "mover" : "ftp";
  const stEl = $(`${p}State`);
  if (w.stopping) {
    stEl.textContent = "Deteniendo…";
    stEl.className = "status-pill stopping";
  } else if (w.running) {
    stEl.textContent = w.phase === "preparing" ? "Preparando…" : "En ejecución";
    stEl.className = "status-pill running";
  } else {
    stEl.textContent = "Detenido";
    stEl.className = "status-pill";
  }
  $(kind === "mover" ? "btnStartMover" : "btnStartFtp").classList.toggle("hidden", w.running);
  $(kind === "mover" ? "btnStopMover" : "btnStopFtp").classList.toggle("hidden", !w.running);

  const c = w.counts;
  const meta = [`Pend. ${w.pendingCount} (${fmtBytes(w.pending_bytes)})`];
  if (c.PAUS) meta.push(`Paus. ${c.PAUS}`);
  meta.push(`OK ${c.OK || 0}`);
  if (c.ERR) meta.push(`ERR ${c.ERR}`);
  const sc = w.space_check;
  if (kind === "mover" && sc && sc.free_bytes != null) meta.push(`Libre dest. ${fmtBytes(sc.free_bytes)}`);
  if (kind === "ftp") {
    const t = w.remote_free_bytes != null && !state.ftpSpace ? `SD libre: ${fmtBytes(w.remote_free_bytes)}` : ftpSpaceText();
    if (t) meta.push(t);
  }
  $(`${p}Meta`).textContent = meta.join(" · ");

  const alertEl = $(`${p}SpaceAlert`);
  if (kind === "mover" && sc && sc.space_warning) {
    alertEl.textContent = `⚠️ Espacio insuficiente en destino: se necesitan ${fmtBytes(sc.needed_bytes)} y quedan ${fmtBytes(sc.free_bytes)} (faltan ${fmtBytes(sc.shortfall_bytes)})`;
    alertEl.classList.remove("hidden");
  } else if (kind === "ftp" && state.ftpSpace && state.ftpSpace.supported !== false && state.ftpSpace.enough === false) {
    const need = state.ftpSpace.pending_bytes ?? w.pending_bytes;
    alertEl.textContent = `⚠️ No cabe en la SD: pendiente ${fmtBytes(need)} y quedan ${fmtBytes(state.ftpSpace.free_bytes)} libres`;
    alertEl.classList.remove("hidden");
  } else {
    alertEl.classList.add("hidden");
  }

  const progWrap = $(`${p}ProgWrap`);
  if (w.running) {
    progWrap.classList.remove("hidden");
    const bar = $(`${p}ProgBar`);
    const fill = $(`${p}ProgFill`);
    if (w.phase === "preparing" || !w.cur) {
      $(`${p}ProgFile`).textContent = w.cur && w.cur.name ? w.cur.name : "Preparando…";
      $(`${p}ProgStats`).textContent = "Preparando…";
      setProgressBar(bar, fill, 0, true);
    } else {
      const pct = Number(w.cur.percent || 0);
      $(`${p}ProgFile`).textContent = w.cur.name || "-";
      $(`${p}ProgFile`).title = w.cur.rel_src || w.cur.name || "";
      $(`${p}ProgStats`).textContent = `${fmtNum(pct, 1)} % · ${curSpeed(w.cur)} · ETA ${curEta(w.cur)}`;
      setProgressBar(bar, fill, pct, false);
    }
    $(`${p}ProgAgg`).textContent = aggregateText(w);
  } else {
    progWrap.classList.add("hidden");
  }
}

function renderCompactStatus(kind, w) {
  const el = $(kind === "mover" ? "scMover" : "scFtp");
  const label = kind === "mover" ? "Mover" : "FTP";
  let text;
  let dot = "";
  if (w.running) {
    dot = "on";
    if (w.phase === "preparing" || !w.cur) text = `${label} preparando…`;
    else text = `${label} ${fmtNum(Number(w.cur.percent || 0), 0)} %`;
  } else {
    text = w.pendingCount ? `${label} parado (${w.pendingCount} pend.)` : `${label} parado`;
  }
  const warn = (w.counts.ERR || 0) > 0 || (kind === "mover" && w.space_check && w.space_check.space_warning);
  if (warn && !w.running) dot = "warn";
  el.innerHTML = `<span class="dot ${dot}" aria-hidden="true"></span> ${escapeHtml(text)}${warn ? ' <span aria-label="con avisos">⚠️</span>' : ""}`;
}

$("statusCompact").onclick = () => {
  const strip = $("statusStrip");
  const expanded = !strip.classList.contains("expanded");
  strip.classList.toggle("expanded", expanded);
  $("statusCompact").setAttribute("aria-expanded", String(expanded));
};

async function refreshStatus() {
  try {
    const data = await api("/api/status");
    if (data.data_root) state.dataRoot = data.data_root;
    const w = data.workers || {};
    state.workers.mover = normalizeWorker(w.mover);
    state.workers.ftp = normalizeWorker(w.ftp);
    // El estado FTP trae la última comprobación de espacio de la SD (remote_space)
    const rs = state.workers.ftp && state.workers.ftp.remote_space;
    if (rs && rs.checked_at && (!state.ftpSpace || (state.ftpSpace.checked_at || 0) <= rs.checked_at)) {
      const pending = state.workers.ftp.pending_bytes;
      state.ftpSpace = {
        ...rs,
        pending_bytes: pending,
        enough: rs.free_bytes != null ? rs.free_bytes >= pending : null,
      };
    }
    for (const kind of ["mover", "ftp"]) {
      if (!state.workers[kind]) continue;
      renderWorkerCard(kind, state.workers[kind]);
      renderCompactStatus(kind, state.workers[kind]);
    }
    const totalActive = (state.workers.mover?.pendingCount || 0) + (state.workers.ftp?.pendingCount || 0);
    document.querySelectorAll("[data-queue-badge]").forEach((b) => {
      b.textContent = totalActive;
      if (b.classList.contains("bn-badge")) b.classList.toggle("hidden", totalActive === 0);
    });
    if (state.activeTab === "queue") {
      loadQueue();
      renderQueueSummary();
    }
    if (state.workers.ftp && state.workers.ftp.running) fetchFtpSpace();
    if (data.backup) renderBackup(data.backup);
  } catch {
    // Silenciar errores transitorios
  }
}

async function fetchFtpSpace(force = false) {
  if (state.ftpSpaceUnsupported) return;
  if (!force && Date.now() - state.ftpSpaceAt < 60000) return;
  state.ftpSpaceAt = Date.now();
  try {
    state.ftpSpace = await api("/api/ftp/space");
  } catch (err) {
    if (isMissing(err)) state.ftpSpaceUnsupported = true;
    else state.ftpSpace = { supported: false, error: err.message };
  }
  if (state.workers.ftp) renderWorkerCard("ftp", state.workers.ftp);
  if (state.activeTab === "queue") renderQueueSummary();
}

async function startQueue(kind) {
  try {
    await api(`/api/queue/${kind}/start`, { method: "POST" });
  } catch (err) {
    showToast(err.message, { type: "err" });
  }
  refreshStatus();
}

async function stopQueue(kind) {
  try {
    await api(`/api/queue/${kind}/stop`, { method: "POST" });
    showToast(kind === "mover" ? "Deteniendo la cola Mover…" : "Deteniendo la cola FTP…");
  } catch (err) {
    showToast(err.message, { type: "err" });
  }
  refreshStatus();
}

$("btnStartMover").onclick = () => startQueue("mover");
$("btnStopMover").onclick = () => stopQueue("mover");
$("btnStartFtp").onclick = () => startQueue("ftp");
$("btnStopFtp").onclick = () => stopQueue("ftp");

// =========================================================================
// BARRA DE RUTA: MIGAS DE PAN QUE SE CONVIERTEN EN CAMPO EDITABLE
// =========================================================================
function renderBreadcrumbs(containerEl, path, rootPath, onClickPath) {
  if (!path) {
    containerEl.innerHTML = "";
    return;
  }
  const normRoot = !rootPath || rootPath === "/" ? "/" : rootPath.replace(/\/+$/, "");
  const parts = [];
  if (normRoot === "/") {
    parts.push({ label: "/", path: "/" });
    let acc = "";
    for (const seg of path.replace(/^\/+/, "").split("/").filter(Boolean)) {
      acc += `/${seg}`;
      parts.push({ label: seg, path: acc });
    }
  } else {
    parts.push({ label: "🏠 Raíz", path: normRoot });
    const rel = path.startsWith(normRoot) ? path.slice(normRoot.length).replace(/^\/+/, "") : "";
    let acc = normRoot;
    for (const seg of rel.split("/").filter(Boolean)) {
      acc += `/${seg}`;
      parts.push({ label: seg, path: acc });
    }
  }

  containerEl.innerHTML = parts
    .map((p, idx) => {
      const isLast = idx === parts.length - 1;
      const sep = idx > 0 ? `<span class="crumb-sep" aria-hidden="true">›</span>` : "";
      if (isLast) return `${sep}<span class="crumb current" aria-current="location">${escapeHtml(p.label)}</span>`;
      return `${sep}<button type="button" class="crumb" data-cpath="${escapeHtml(p.path)}">${escapeHtml(p.label)}</button>`;
    })
    .join("");

  containerEl.querySelectorAll("button.crumb").forEach((b) => {
    b.onclick = (e) => {
      e.stopPropagation();
      onClickPath(b.dataset.cpath);
    };
  });
  requestAnimationFrame(() => { containerEl.scrollLeft = containerEl.scrollWidth; });
}

function setupPathbar({ crumbs, input, editBtn, getPath, onNavigate }) {
  let editing = false;
  const enter = () => {
    if (editing) return;
    editing = true;
    input.value = getPath() || "";
    crumbs.classList.add("hidden");
    input.classList.remove("hidden");
    editBtn.textContent = "✓";
    editBtn.setAttribute("aria-label", "Ir a la ruta escrita");
    input.focus();
    input.select();
  };
  const leave = () => {
    if (!editing) return;
    editing = false;
    input.classList.add("hidden");
    crumbs.classList.remove("hidden");
    editBtn.textContent = "✎";
    editBtn.setAttribute("aria-label", editBtn.dataset.label || "Editar ruta");
  };
  editBtn.dataset.label = editBtn.getAttribute("aria-label");
  crumbs.addEventListener("click", (e) => {
    if (!e.target.closest("button.crumb")) enter();
  });
  editBtn.addEventListener("pointerdown", (e) => { if (editing) e.preventDefault(); });
  editBtn.addEventListener("click", () => {
    if (editing) {
      const v = input.value.trim();
      leave();
      if (v) onNavigate(v);
    } else {
      enter();
    }
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      const v = input.value.trim();
      leave();
      if (v) onNavigate(v);
    } else if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      leave();
      editBtn.focus();
    }
  });
  input.addEventListener("blur", () => setTimeout(leave, 120));
  return { enter, leave };
}

function renderBookmarksBar(el, bookmarks, includeFavs, onClick) {
  const favs = includeFavs ? state.favorites : [];
  const all = [
    ...bookmarks,
    ...favs.map((p) => ({ label: `★ ${baseName(p)}`, path: p, title: p })),
  ];
  el.innerHTML = all
    .map((b) => `<button type="button" class="btn btn-sm btn-ghost" data-bpath="${escapeHtml(b.path)}" title="${escapeHtml(b.title || b.path)}">${escapeHtml(b.label)}</button>`)
    .join("");
  el.querySelectorAll("button").forEach((btn) => {
    btn.onclick = () => onClick(btn.dataset.bpath);
  });
}

// =========================================================================
// EXPLORAR: MODO, ACCIÓN Y PANEL VISIBLE EN MÓVIL
// =========================================================================
function updateExploreActionLabels() {
  const isFtp = state.exploreMode === "ftp";
  $("exploreMoverOpGroup").classList.toggle("hidden", isFtp);
  $("btnAddSwitchOnly").classList.toggle("hidden", !isFtp);
  $("btnAddGame").classList.toggle("hidden", !isFtp);
  $("dstPaneTitle").textContent = isFtp ? "📡 Destino FTP / Switch" : "🎯 Destino local";
  $("btnDstNewDir").classList.toggle("hidden", isFtp);
  $("btnDstFav").classList.toggle("hidden", isFtp);

  if (isFtp) {
    $("btnAddSelected").textContent = "📡 A la cola FTP →";
    $("btnSendAndStart").textContent = "⚡ Subir ahora";
  } else if (state.moverOp === "mover") {
    $("btnAddSelected").textContent = "✂️ Mover a la cola →";
    $("btnSendAndStart").textContent = "⚡ Mover ahora";
  } else {
    $("btnAddSelected").textContent = "📋 Copiar a la cola →";
    $("btnSendAndStart").textContent = "⚡ Copiar ahora";
  }
}

function setPressed(btn, on) {
  btn.classList.toggle("active", on);
  btn.setAttribute("aria-pressed", String(on));
}

function setExploreMode(mode) {
  state.exploreMode = mode;
  setPressed($("modeBtnMover"), mode === "mover");
  setPressed($("modeBtnFtp"), mode === "ftp");
  state.selectedPaths.clear();
  clearSearch(false);
  updateExploreActionLabels();
  loadSrcDir("");
  loadDstDir();
  if (mode === "ftp") fetchFtpSpace(true);
}

function setMoverOp(op) {
  state.moverOp = op;
  setPressed($("opBtnCopiar"), op === "copiar");
  setPressed($("opBtnMover"), op === "mover");
  updateExploreActionLabels();
  renderSrcTable();
}

$("modeBtnMover").onclick = () => setExploreMode("mover");
$("modeBtnFtp").onclick = () => setExploreMode("ftp");
$("opBtnCopiar").onclick = () => setMoverOp("copiar");
$("opBtnMover").onclick = () => setMoverOp("mover");

function setMobilePane(pane) {
  $("dualPanes").dataset.mobilePane = pane;
  setPressed($("mPaneBtnSrc"), pane === "src");
  setPressed($("mPaneBtnDst"), pane === "dst");
}

function updateMobileDstLabel() {
  if (state.useAutoDest) {
    $("mPaneDstLabel").textContent = "Destino automático";
    return;
  }
  const raw = state.exploreMode === "mover" ? (state.dstLocalPath || "-") : (state.dstRemotePath || "/");
  const shown = state.exploreMode === "mover" ? relPath(raw) : raw;
  const parts = shown.split("/").filter(Boolean);
  $("mPaneDstLabel").textContent = parts.length > 2 ? `…/${parts.slice(-2).join("/")}` : (shown || "/");
  $("mPaneBtnDst").title = raw;
}

$("mPaneBtnSrc").onclick = () => setMobilePane("src");
$("mPaneBtnDst").onclick = () => setMobilePane("dst");

$("chkAutoDest").onchange = (e) => {
  state.useAutoDest = e.target.checked;
  $("dstAutoNotice").classList.toggle("hidden", !state.useAutoDest);
  $("dstNavigator").classList.toggle("hidden", state.useAutoDest);
  updateMobileDstLabel();
  if (!state.useAutoDest) loadDstDir();
};

// =========================================================================
// PANEL ORIGEN: LISTADO, ORDEN, FILTRO Y BÚSQUEDA EN SUBCARPETAS
// =========================================================================
(function restoreSort() {
  const raw = storage.get("toolbox_sort", "");
  const [key, order] = raw.split(":");
  if (["name", "mtime", "size"].includes(key) && ["asc", "desc"].includes(order)) state.sort = { key, order };
})();

const srcPathbar = setupPathbar({
  crumbs: $("srcBreadcrumbs"),
  input: $("srcPathInput"),
  editBtn: $("btnSrcEditPath"),
  getPath: () => state.srcPath,
  onNavigate: (p) => loadSrcDir(p),
});

async function loadSrcDir(path) {
  state.srcLoading = true;
  state.srcError = "";
  if (!state.srcData) renderSrcTable();
  try {
    const hidden = $("chkShowHidden").checked ? "true" : "false";
    const qs = new URLSearchParams({ kind: state.exploreMode, show_hidden: hidden, sort: state.sort.key, order: state.sort.order });
    if (path) qs.set("path", path);
    const data = await api(`/api/fs/list?${qs}`);
    state.srcData = data;
    state.srcPath = data.path;
    state.srcParent = data.parent;
    if (data.data_root) state.dataRoot = data.data_root;
    state.selectedPaths.clear();
    state.dirSizes = {};
    $("btnSrcUp").disabled = !data.parent;
    $("srcDiskInfo").textContent = data.disk ? `Libre: ${fmtBytes(data.disk.free_bytes)}` : "";
    renderBookmarksBar($("srcBookmarks"), data.bookmarks || [], false, (p) => loadSrcDir(p));
    renderBreadcrumbs($("srcBreadcrumbs"), data.path, data.data_root, (p) => loadSrcDir(p));
    srcPathbar.leave();
  } catch (err) {
    state.srcError = err.message;
    showToast(err.message, { type: "err" });
  } finally {
    state.srcLoading = false;
  }
  if (state.search.active) runSearch();
  else renderSrcTable();
}

function sortEntries(list, isDir, sort = state.sort) {
  const { key, order } = sort;
  const dir = order === "desc" ? -1 : 1;
  const byName = (a, b) => a.name.localeCompare(b.name, "es", { numeric: true, sensitivity: "base" });
  const val = (e) => {
    if (key === "mtime") return Number(e.mtime || 0);
    if (key === "size") {
      if (isDir) return state.dirSizes[e.path] ? state.dirSizes[e.path].bytes : (e.size_bytes ?? e.size ?? -1);
      return Number(e.size_bytes ?? e.size ?? 0);
    }
    return 0;
  };
  return [...list].sort((a, b) => {
    if (key === "name") return byName(a, b) * dir;
    const d = val(a) - val(b);
    return d !== 0 ? d * dir : byName(a, b);
  });
}

function updateSortHeaders() {
  document.querySelectorAll(".fs-table .th-sort").forEach((b) => {
    const on = b.dataset.sort === state.sort.key;
    b.classList.toggle("active", on);
    b.querySelector(".sort-ind").textContent = on ? (state.sort.order === "asc" ? "▲" : "▼") : "";
    b.closest("th").setAttribute("aria-sort", on ? (state.sort.order === "asc" ? "ascending" : "descending") : "none");
  });
  $("mobileSortSelect").value = `${state.sort.key}:${state.sort.order}`;
}

function setSort(key, order) {
  state.sort = { key, order };
  storage.set("toolbox_sort", `${key}:${order}`);
  updateSortHeaders();
  renderSrcTable();
}

document.querySelectorAll(".fs-table .th-sort").forEach((b) => {
  b.onclick = () => {
    const key = b.dataset.sort;
    const order = state.sort.key === key
      ? (state.sort.order === "asc" ? "desc" : "asc")
      : (key === "name" ? "asc" : "desc");
    setSort(key, order);
  };
});
$("mobileSortSelect").onchange = (e) => {
  const [key, order] = e.target.value.split(":");
  setSort(key, order);
};

function stateRow(cols, { icon = "", title = "", text = "", actions = [], error = false, loading = false } = {}) {
  if (loading) {
    return `<tr class="state-row loading-row"><td colspan="${cols}" class="cell-state"><div class="empty-state"><span class="spinner" aria-hidden="true"></span>${escapeHtml(text || "Cargando…")}</div></td></tr>`;
  }
  const btns = actions.map((a) => `<button type="button" class="btn btn-sm ${a.primary ? "btn-primary" : ""}" data-state-action="${escapeHtml(a.id)}">${escapeHtml(a.label)}</button>`).join("");
  return `<tr class="state-row"><td colspan="${cols}" class="cell-state">
    <div class="empty-state ${error ? "is-error" : ""}" role="${error ? "alert" : "status"}">
      ${icon ? `<span class="es-icon" aria-hidden="true">${icon}</span>` : ""}
      ${title ? `<span class="es-title">${escapeHtml(title)}</span>` : ""}
      ${text ? `<span>${escapeHtml(text)}</span>` : ""}
      ${btns ? `<div class="es-actions">${btns}</div>` : ""}
    </div></td></tr>`;
}

function actionLabel() {
  if (state.exploreMode === "ftp") return "Subir →";
  return state.moverOp === "mover" ? "Mover →" : "Copiar →";
}

function dirSizeCell(d) {
  const s = state.dirSizes[d.path];
  if (s) return `${fmtBytes(s.bytes)} <span class="muted">(${s.files})</span>`;
  if (s === null) return `<span class="spinner" aria-label="Calculando"></span>`;
  return `<button type="button" class="btn btn-sm btn-ghost" data-calc-size="${escapeHtml(d.path)}" aria-label="Calcular tamaño de ${escapeHtml(d.name)}" title="Calcular tamaño">📏</button>`;
}

function renderEntryRow(e, { isDir, rel = "" }) {
  const checked = state.selectedPaths.has(e.path);
  state.entryInfo.set(e.path, { name: e.name, is_dir: isDir, size: isDir ? (state.dirSizes[e.path]?.bytes ?? null) : (e.size_bytes ?? e.size ?? 0) });
  const nameAttr = escapeHtml(e.name);
  const mtime = e.mtime ? fmtDate(e.mtime) : "";
  const relHtml = rel ? `<span class="name-sub">${escapeHtml(rel)}</span>` : "";
  let nameHtml;
  let sizeHtml;
  let metaSize;
  let actions;
  const renameBtn = `<button type="button" class="btn btn-sm btn-ghost btn-icon desk-only" data-rename="${escapeHtml(e.path)}" data-rname="${nameAttr}" aria-label="Renombrar ${nameAttr}" title="Renombrar">✏️</button>`;
  const moreBtn = `<button type="button" class="btn btn-sm btn-ghost btn-icon mob-only" data-row-more="${escapeHtml(e.path)}" aria-label="Más acciones para ${nameAttr}" aria-haspopup="menu" aria-expanded="false">⋯</button>`;

  if (isDir) {
    nameHtml = `<button type="button" class="dir-link" data-open="${escapeHtml(e.path)}">📁 ${nameAttr}</button>${relHtml}`;
    sizeHtml = dirSizeCell(e);
    const s = state.dirSizes[e.path];
    metaSize = s ? ` · ${fmtBytes(s.bytes)}` : "";
    const gameBtn = state.exploreMode === "ftp"
      ? `<button type="button" class="btn btn-sm btn-ghost desk-only" data-add-game="${escapeHtml(e.path)}" title="Encolar juego completo (base + UPD + DLC en orden)" aria-label="Subir juego completo de ${nameAttr}">🎮</button>`
      : "";
    actions = `<button type="button" class="btn btn-sm" data-add-dir="${escapeHtml(e.path)}" aria-label="${escapeHtml(actionLabel().replace(" →", ""))} carpeta ${nameAttr}">${actionLabel()}</button>${gameBtn}${renameBtn}${moreBtn}`;
  } else {
    const sw = e.switch_type ? `<span class="tag tag-${e.switch_type.toLowerCase()}">${escapeHtml(e.switch_type)}</span>` : "";
    const st = e.queue_status ? `<span class="tag tag-${e.queue_status.toLowerCase()}">${escapeHtml(e.queue_status)}</span>` : "";
    const tags = sw || st ? `<span class="name-tags">${sw}${st}</span>` : "";
    nameHtml = `<span class="file-name">${nameAttr}</span>${tags}${relHtml}`;
    sizeHtml = fmtBytes(e.size_bytes ?? e.size);
    metaSize = ` · ${fmtBytes(e.size_bytes ?? e.size)}`;
    actions = `<button type="button" class="btn btn-sm btn-primary" data-add-file="${escapeHtml(e.path)}" aria-label="${escapeHtml(actionLabel().replace(" →", ""))} ${nameAttr}">${actionLabel()}</button>${renameBtn}${moreBtn}`;
  }

  return `
    <tr class="${checked ? "is-selected" : ""}">
      <td class="col-chk"><label class="chk-hit"><input type="checkbox" class="fs-chk" data-path="${escapeHtml(e.path)}" ${checked ? "checked" : ""} aria-label="Seleccionar ${nameAttr}"></label></td>
      <td class="col-name">${nameHtml}</td>
      <td class="col-mtime">${escapeHtml(mtime || (isDir ? "" : "-"))}<span class="meta-size">${escapeHtml(metaSize.replace(/^ · /, mtime ? " · " : ""))}</span></td>
      <td class="col-size">${sizeHtml}</td>
      <td class="col-actions"><div class="row-actions">${actions}</div></td>
    </tr>`;
}

function renderSrcTable() {
  updateSortHeaders();
  state.entryInfo.clear();
  const body = $("fsTableBody");

  if (state.search.active) {
    renderSearchResults();
    return;
  }

  const data = state.srcData;
  if (!data) {
    body.innerHTML = state.srcError
      ? stateRow(5, { icon: "⚠️", title: "No se pudo abrir la carpeta", text: state.srcError, error: true, actions: [{ id: "retry", label: "↻ Reintentar", primary: true }] })
      : stateRow(5, { loading: true, text: "Cargando carpeta…" });
    updateSelectionSummary();
    return;
  }

  const filter = $("fsFilter").value.trim().toLowerCase();
  const dirs = sortEntries(data.dirs.filter((d) => !filter || d.name.toLowerCase().includes(filter)), true);
  const files = sortEntries(data.files.filter((f) => !filter || f.name.toLowerCase().includes(filter)), false);
  const rows = [
    ...dirs.map((d) => renderEntryRow(d, { isDir: true })),
    ...files.map((f) => renderEntryRow(f, { isDir: false })),
  ];

  if (!rows.length) {
    if (filter) {
      rows.push(stateRow(5, {
        icon: "🔍",
        title: `Sin coincidencias para «${$("fsFilter").value.trim()}»`,
        text: "El filtro solo mira esta carpeta.",
        actions: [
          { id: "search-recursive", label: "Buscar en subcarpetas", primary: true },
          { id: "clear-filter", label: "Limpiar filtro" },
        ],
      }));
    } else {
      const acts = [];
      if (state.srcParent) acts.push({ id: "up", label: "⬆ Subir" });
      acts.push({ id: "mkdir", label: "+ Carpeta" });
      rows.push(stateRow(5, { icon: "📭", title: "Esta carpeta está vacía", actions: acts }));
    }
  }
  body.innerHTML = rows.join("");
  updateSelectionSummary();
}

function renderSearchResults() {
  const body = $("fsTableBody");
  const s = state.search;
  if (s.unsupported) {
    body.innerHTML = stateRow(5, { icon: "🔍", title: "Búsqueda en subcarpetas no disponible", text: "Actualiza el servidor de Toolbox para usarla. Mientras tanto, el filtro busca en la carpeta actual.", actions: [{ id: "disable-recursive", label: "Filtrar solo esta carpeta" }] });
  } else if (s.loading && !s.results.length) {
    body.innerHTML = stateRow(5, { loading: true, text: `Buscando «${s.q}» en subcarpetas…` });
  } else if (s.error) {
    body.innerHTML = stateRow(5, { icon: "⚠️", title: "Error en la búsqueda", text: s.error, error: true, actions: [{ id: "retry-search", label: "↻ Reintentar", primary: true }] });
  } else if (!s.results.length) {
    body.innerHTML = stateRow(5, { icon: "🔍", title: `No se encontró «${s.q}»`, text: `Ni en ${relPath(state.srcPath)} ni en sus subcarpetas.`, actions: [{ id: "clear-filter", label: "Limpiar búsqueda" }] });
  } else {
    const rows = sortEntries(s.results.filter((r) => r.is_dir), true).map((r) => renderEntryRow(r, { isDir: true, rel: r.rel ? r.rel.replace(/\/?[^/]*$/, "") || "./" : "" }));
    rows.push(...sortEntries(s.results.filter((r) => !r.is_dir), false).map((r) => renderEntryRow(r, { isDir: false, rel: r.rel ? r.rel.replace(/\/?[^/]*$/, "") || "./" : "" })));
    if (s.truncated) {
      rows.push(`<tr class="state-row"><td colspan="5" class="muted" style="text-align:center;padding:10px">Se muestran los primeros ${s.results.length} resultados. Escribe un texto más concreto para afinar.</td></tr>`);
    }
    body.innerHTML = rows.join("");
  }
  updateSelectionSummary();
}

const runSearch = debounce(async () => {
  const q = $("fsFilter").value.trim();
  const s = state.search;
  if (!s.active) return;
  s.q = q;
  s.loading = true;
  s.error = "";
  renderSrcTable();
  try {
    const qs = new URLSearchParams({ path: state.srcPath, q, kind: state.exploreMode, limit: "300" });
    const res = await api(`/api/fs/search?${qs}`);
    if (!state.search.active || state.search.q !== q) return;
    s.results = (res.results || []).map((r) => ({ ...r, size_bytes: r.size_bytes ?? r.size }));
    s.truncated = !!res.truncated;
  } catch (err) {
    if (isMissing(err)) s.unsupported = true;
    else s.error = err.message;
    s.results = [];
  } finally {
    s.loading = false;
  }
  renderSrcTable();
}, 350);

function updateSearchMode() {
  const q = $("fsFilter").value.trim();
  const wantSearch = $("chkRecursive").checked && q.length >= 2;
  if (wantSearch) {
    state.search.active = true;
    state.selectedPaths.clear();
    runSearch();
  } else {
    if (state.search.active) state.selectedPaths.clear();
    state.search.active = false;
    state.search.results = [];
    runSearch.cancel();
  }
  renderSrcTable();
}

function clearSearch(render = true) {
  $("fsFilter").value = "";
  state.search.active = false;
  state.search.results = [];
  runSearch.cancel();
  if (render) renderSrcTable();
}

$("fsFilter").addEventListener("input", updateSearchMode);
$("chkRecursive").onchange = updateSearchMode;

function updateSelectionSummary() {
  const count = state.selectedPaths.size;
  let bytes = 0;
  let unknown = false;
  for (const p of state.selectedPaths) {
    const info = state.entryInfo.get(p);
    if (!info || info.size == null) unknown = true;
    else bytes += Number(info.size) || 0;
  }
  const sizeTxt = count ? ` · ${fmtBytes(bytes)}${unknown ? "+" : ""}` : "";
  $("selectionSummary").textContent = `${count} seleccionado${count === 1 ? "" : "s"}${sizeTxt}`;
  $("mPaneSrcCount").textContent = count;
  $("srcBatchBar").classList.toggle("has-selection", count > 0);
  $("btnDeleteSelected").disabled = count === 0;
  const visible = Array.from(document.querySelectorAll(".fs-chk"));
  $("chkHeaderAll").checked = visible.length > 0 && visible.every((c) => c.checked);
  $("chkHeaderAll").indeterminate = visible.some((c) => c.checked) && !$("chkHeaderAll").checked;
}

async function calcDirSize(dirPath) {
  state.dirSizes[dirPath] = null;
  renderSrcTable();
  try {
    const res = await api(`/api/fs/dir-size?path=${encodeURIComponent(dirPath)}`);
    state.dirSizes[dirPath] = { bytes: res.size_bytes, files: res.files_count };
  } catch (err) {
    delete state.dirSizes[dirPath];
    showToast(err.message, { type: "err" });
  }
  renderSrcTable();
}

async function renameEntry(path, oldName) {
  const newName = await uiPrompt({ title: "Renombrar", label: `Nuevo nombre para «${oldName}»`, value: oldName, okText: "Renombrar", validate: validateName });
  if (!newName || newName === oldName) return;
  try {
    await api("/api/fs/rename", { method: "POST", body: JSON.stringify({ path, new_name: newName }) });
    showToast("Renombrado correctamente");
    loadSrcDir(state.srcPath);
  } catch (err) {
    showToast(err.message, { type: "err" });
  }
}

async function makeDir(parent, onDone, where = "") {
  const name = await uiPrompt({ title: "Nueva carpeta", label: where ? `Nombre de la carpeta dentro de ${where}` : "Nombre de la nueva carpeta", okText: "Crear", validate: validateName });
  if (!name) return;
  try {
    const res = await api("/api/fs/mkdir", { method: "POST", body: JSON.stringify({ parent, name }) });
    showToast("Carpeta creada");
    onDone(res);
  } catch (err) {
    showToast(err.message, { type: "err" });
  }
}

$("fsTableBody").addEventListener("click", async (e) => {
  const stateBtn = e.target.closest("[data-state-action]");
  if (stateBtn) {
    const a = stateBtn.dataset.stateAction;
    if (a === "retry") loadSrcDir(state.srcPath);
    if (a === "up" && state.srcParent) loadSrcDir(state.srcParent);
    if (a === "mkdir") $("btnSrcNewDir").click();
    if (a === "clear-filter") clearSearch();
    if (a === "search-recursive") { $("chkRecursive").checked = true; updateSearchMode(); }
    if (a === "disable-recursive") { $("chkRecursive").checked = false; state.search.unsupported = false; updateSearchMode(); }
    if (a === "retry-search") runSearch();
    return;
  }
  const openEl = e.target.closest("[data-open]");
  if (openEl) {
    if (state.search.active) clearSearch(false);
    return loadSrcDir(openEl.dataset.open);
  }
  const addFileEl = e.target.closest("[data-add-file]");
  if (addFileEl) return enqueuePaths([addFileEl.dataset.addFile], false, false, false);
  const addDirEl = e.target.closest("[data-add-dir]");
  if (addDirEl) return enqueuePaths([addDirEl.dataset.addDir], true, false, false);
  const gameEl = e.target.closest("[data-add-game]");
  if (gameEl) return addGame([gameEl.dataset.addGame]);
  const sizeEl = e.target.closest("[data-calc-size]");
  if (sizeEl) return calcDirSize(sizeEl.dataset.calcSize);
  const renEl = e.target.closest("[data-rename]");
  if (renEl) return renameEntry(renEl.dataset.rename, renEl.dataset.rname || "");
  const moreEl = e.target.closest("[data-row-more]");
  if (moreEl) {
    const p = moreEl.dataset.rowMore;
    const info = state.entryInfo.get(p) || { name: baseName(p), is_dir: false };
    const items = [];
    if (info.is_dir) {
      items.push({ icon: "📏", label: "Calcular tamaño", onClick: () => calcDirSize(p) });
      if (state.exploreMode === "ftp") items.push({ icon: "🎮", label: "Subir juego completo", onClick: () => addGame([p]) });
    }
    items.push({ icon: "✏️", label: "Renombrar", onClick: () => renameEntry(p, info.name) });
    items.push({ icon: "⧉", label: "Copiar ruta", onClick: () => copyText(p) });
    items.push({ icon: "☑", label: state.selectedPaths.has(p) ? "Quitar de la selección" : "Seleccionar", onClick: () => toggleSelect(p) });
    openPopMenu(moreEl, items);
  }
});

function toggleSelect(path, force) {
  const on = force === undefined ? !state.selectedPaths.has(path) : force;
  if (on) state.selectedPaths.add(path);
  else state.selectedPaths.delete(path);
  const chk = document.querySelector(`.fs-chk[data-path="${CSS.escape(path)}"]`);
  if (chk) {
    chk.checked = on;
    chk.closest("tr").classList.toggle("is-selected", on);
  }
  updateSelectionSummary();
}

$("fsTableBody").addEventListener("change", (e) => {
  const chk = e.target.closest(".fs-chk");
  if (chk) toggleSelect(chk.dataset.path, chk.checked);
});

$("chkHeaderAll").onchange = (e) => {
  document.querySelectorAll(".fs-chk").forEach((c) => toggleSelect(c.dataset.path, e.target.checked));
};
$("btnSelectAll").onclick = () => document.querySelectorAll(".fs-chk").forEach((c) => toggleSelect(c.dataset.path, true));
$("btnSelectNone").onclick = () => {
  state.selectedPaths.clear();
  renderSrcTable();
};

$("btnSrcUp").onclick = () => { if (state.srcParent) loadSrcDir(state.srcParent); };
$("chkShowHidden").onchange = () => loadSrcDir(state.srcPath);
$("btnSrcNewDir").onclick = () => makeDir(state.srcPath, () => loadSrcDir(state.srcPath), relPath(state.srcPath));

// =========================================================================
// BORRAR (A LA PAPELERA, CON DESHACER)
// =========================================================================
function trashDays() {
  const d = state.settings ? Number(state.settings.trash_days) : NaN;
  return Number.isFinite(d) ? d : 7;
}

// Papelera activa = activada en ajustes y con días > 0 (si no, el servidor borra directamente)
function trashEnabled() {
  if (state.trashAvailable === false) return false;
  return !(state.settings && state.settings.trash_enabled === false) && trashDays() > 0;
}

function trashInfoText() {
  return trashEnabled()
    ? `Papelera activada: se eliminan automáticamente a los ${trashDays()} días.`
    : "Papelera desactivada: los borrados son definitivos. Lo que ya contiene se conserva hasta caducar.";
}

function paintTrashToggle() {
  const btn = $("btnTrashToggle");
  if (!btn) return;
  const known = state.settings && Object.prototype.hasOwnProperty.call(state.settings, "trash_enabled");
  btn.hidden = !known;
  btn.textContent = trashEnabled() ? "Desactivar papelera" : "Activar papelera";
  if ($("trashInfo")) $("trashInfo").textContent = trashInfoText();
  if ($("cfgTrashEnabled")) $("cfgTrashEnabled").checked = state.settings ? state.settings.trash_enabled !== false : true;
  if ($("cfgTrashDays")) $("cfgTrashDays").disabled = state.settings ? state.settings.trash_enabled === false : false;
}

async function setTrashEnabled(enabled) {
  if (!enabled) {
    const res = await uiDialog({
      title: "Desactivar la papelera",
      html: `<p>A partir de ahora, lo que borres se eliminará <strong>definitivamente</strong> y no se podrá deshacer.</p>
             <p class="muted">Lo que ya está en la papelera se conserva y puedes restaurarlo hasta que caduque.</p>`,
      okText: "Desactivar",
      danger: true,
    });
    if (!res.ok) { paintTrashToggle(); return; }
  }
  const ok = await saveSettings({ trash_enabled: !!enabled });
  paintTrashToggle();
  if (state.trash && Array.isArray(state.trash.items)) renderTrash();
  if (ok) showToast(enabled ? "Papelera activada" : "Papelera desactivada: los borrados serán definitivos");
}

async function deleteSelection() {
  const paths = Array.from(state.selectedPaths);
  if (!paths.length) return showToast("Selecciona al menos un archivo o carpeta");
  const infos = paths.map((p) => ({ path: p, ...(state.entryInfo.get(p) || { name: baseName(p), is_dir: false, size: null }) }));
  const listed = infos.slice(0, 8).map((i) => `<li>${i.is_dir ? "📁" : "📄"} ${escapeHtml(relPath(i.path))} <span class="muted" data-size-for="${escapeHtml(i.path)}">${i.size != null ? fmtBytes(i.size) : (i.is_dir ? "calculando…" : "")}</span></li>`).join("");
  const more = infos.length > 8 ? `<li class="muted">…y ${infos.length - 8} más</li>` : "";
  const useTrash = trashEnabled();
  const note = useTrash
    ? `<div class="dlg-note info">Se moverá a la papelera (se elimina definitivamente a los ${trashDays()} días). Podrás deshacerlo.</div>
       <label class="chk-block"><input type="checkbox" name="permanent"> Eliminar definitivamente, sin pasar por la papelera</label>`
    : `<div class="dlg-note">Se eliminará definitivamente del servidor. No se puede deshacer${state.trashAvailable === false ? "" : " (la papelera está desactivada en Ajustes)"}.</div>`;

  const res = await uiDialog({
    title: `Borrar ${paths.length} elemento${paths.length === 1 ? "" : "s"}`,
    html: `<ul>${listed}${more}</ul><p class="dlg-total" id="dlgDeleteTotal">Total: calculando…</p>${note}`,
    okText: useTrash ? "Mover a la papelera" : "Eliminar",
    danger: true,
    onOpen: async (body) => {
      let total = 0;
      let pending = infos.filter((i) => i.size == null && i.is_dir).slice(0, 12);
      for (const i of infos) if (i.size != null) total += Number(i.size) || 0;
      const totalEl = body.querySelector("#dlgDeleteTotal");
      const paint = (partial) => { if (totalEl) totalEl.textContent = `Total: ${fmtBytes(total)}${partial ? " (calculando el resto…)" : ""}`; };
      paint(pending.length > 0);
      await Promise.all(pending.map(async (i) => {
        try {
          const r = await api(`/api/fs/dir-size?path=${encodeURIComponent(i.path)}`);
          total += Number(r.size_bytes) || 0;
          const span = body.querySelector(`[data-size-for="${CSS.escape(i.path)}"]`);
          if (span) span.textContent = `${fmtBytes(r.size_bytes)} · ${r.files_count} archivos`;
        } catch {
          const span = body.querySelector(`[data-size-for="${CSS.escape(i.path)}"]`);
          if (span) span.textContent = "tamaño desconocido";
        }
        pending = pending.filter((x) => x !== i);
        paint(pending.length > 0);
      }));
      // Carpetas sin calcular (más de 12): el total es un mínimo
      const uncounted = infos.filter((i) => i.size == null && i.is_dir).length > 12;
      if (totalEl) totalEl.textContent = `Total: ${fmtBytes(total)}${uncounted ? " o más" : ""}`;
    },
  });
  if (!res.ok) return;

  try {
    const out = await api("/api/fs/delete", { method: "POST", body: JSON.stringify({ paths, permanent: !!res.checks.permanent }) });
    state.selectedPaths.clear();
    const trashed = out.trashed || [];
    if (trashed.length) {
      const ids = trashed.map((t) => t.id);
      showToast(`${trashed.length} elemento${trashed.length === 1 ? "" : "s"} movido${trashed.length === 1 ? "" : "s"} a la papelera`, {
        action: {
          label: "Deshacer",
          fn: async () => {
            await api("/api/trash/restore", { method: "POST", body: JSON.stringify({ ids }) });
            showToast("Restaurado");
            loadSrcDir(state.srcPath);
          },
        },
      });
    } else {
      showToast(`Eliminado${out.deleted === 1 ? "" : "s"}: ${out.deleted ?? paths.length}`);
    }
    loadSrcDir(state.srcPath);
  } catch (err) {
    showToast(err.message, { type: "err" });
  }
}

$("btnDeleteSelected").onclick = deleteSelection;

// =========================================================================
// SUBIR JUEGO COMPLETO (BASE + UPD + DLC)
// =========================================================================
async function addGame(paths) {
  if (!paths.length) paths = [state.srcPath];
  const destDir = state.useAutoDest ? "" : state.dstRemotePath;
  let res;
  try {
    res = await api("/api/ftp/add-game", { method: "POST", body: JSON.stringify({ paths, dest_dir: destDir }) });
  } catch (err) {
    if (isMissing(err)) showToast("«Juego completo» necesita una versión más reciente del servidor", { type: "err" });
    else showToast(err.message, { type: "err" });
    return;
  }
  const groups = (res.groups || []).map((g) => {
    const base = g.base ? (Array.isArray(g.base) ? g.base.length : 1) : 0;
    const upd = Array.isArray(g.upd) ? g.upd.length : (g.upd || 0);
    const dlc = Array.isArray(g.dlc) ? g.dlc.length : (g.dlc || 0);
    return `<div class="game-group">
      <strong>${escapeHtml(g.name || g.title_id || "Juego")}${g.title_id && g.name ? ` <span class="muted">${escapeHtml(g.title_id)}</span>` : ""}</strong>
      <div class="gg-parts">
        <span class="tag ${base ? "tag-base" : "tag-err"}">BASE ${base ? "✓" : "✗ falta"}</span>
        <span class="tag tag-upd">UPD ×${upd}</span>
        <span class="tag tag-dlc">DLC ×${dlc}</span>
      </div></div>`;
  }).join("");
  const warns = (res.warnings || []).length
    ? `<div class="dlg-note"><strong>Avisos</strong><ul>${res.warnings.map((w) => `<li>${escapeHtml(w)}</li>`).join("")}</ul></div>`
    : "";
  const added = res.added ?? (res.items || []).length;
  const ok = await uiConfirm({
    title: added ? `🎮 ${added} archivo${added === 1 ? "" : "s"} añadido${added === 1 ? "" : "s"} a la cola FTP` : "🎮 No se añadió nada",
    html: `${groups || '<p class="muted">No se encontraron paquetes de Switch en la selección.</p>'}${warns}<p class="muted">Orden de subida: base → actualizaciones → DLC.</p>`,
    okText: "▶ Iniciar subida",
    cancelText: "Cerrar",
  });
  loadSrcDir(state.srcPath);
  refreshStatus();
  if (ok && added) startQueue("ftp");
}

$("btnAddGame").onclick = () => addGame(Array.from(state.selectedPaths));

// =========================================================================
// PANEL DESTINO
// =========================================================================
const dstPathbar = setupPathbar({
  crumbs: $("dstBreadcrumbs"),
  input: $("dstPathInput"),
  editBtn: $("btnDstEditPath"),
  getPath: () => (state.exploreMode === "mover" ? state.dstLocalPath : state.dstRemotePath),
  onNavigate: (p) => loadDstDir(p),
});

(function restoreDstSort() {
  const raw = storage.get("toolbox_dst_sort", "");
  const [key, order] = raw.split(":");
  if (["name", "mtime", "size"].includes(key) && ["asc", "desc"].includes(order)) state.dstSort = { key, order };
})();

function updateDstSortHeaders() {
  // En FTP solo hay nombres de carpeta: fecha y tamaño no se pueden ordenar
  const ftp = state.exploreMode !== "mover";
  document.querySelectorAll(".dst-table .th-sort").forEach((b) => {
    const on = b.dataset.dsort === (ftp ? "name" : state.dstSort.key);
    b.disabled = ftp && b.dataset.dsort !== "name";
    b.classList.toggle("active", on);
    b.querySelector(".sort-ind").textContent = on ? (state.dstSort.order === "asc" ? "▲" : "▼") : "";
    b.closest("th").setAttribute("aria-sort", on ? (state.dstSort.order === "asc" ? "ascending" : "descending") : "none");
  });
}

document.querySelectorAll(".dst-table .th-sort").forEach((b) => {
  b.onclick = () => {
    const key = b.dataset.dsort;
    const cur = state.exploreMode === "mover" ? state.dstSort.key : "name";
    const order = cur === key
      ? (state.dstSort.order === "asc" ? "desc" : "asc")
      : (key === "name" ? "asc" : "desc");
    state.dstSort = { key, order };
    storage.set("toolbox_dst_sort", `${key}:${order}`);
    renderDstTable();
  };
});

function renderDstTable() {
  const q = ($("dstFilter").value || "").trim().toLowerCase();
  const body = $("dstTableBody");
  updateDstSortHeaders();
  if (state.exploreMode === "mover") {
    const data = state.dstLocalData;
    if (!data) return;
    const dirs = sortEntries((data.dirs || []).filter((d) => !q || d.name.toLowerCase().includes(q)), true, state.dstSort);
    const files = sortEntries((data.files || []).filter((f) => !q || f.name.toLowerCase().includes(q)), false, state.dstSort);
    const date = (e) => `<td class="col-mtime">${escapeHtml(e.mtime ? fmtDate(e.mtime) : "")}</td>`;
    const rows = [
      ...dirs.map((d) => `<tr><td><button type="button" class="dir-link" data-dopen="${escapeHtml(d.path)}">📁 ${escapeHtml(d.name)}</button></td>${date(d)}<td class="muted col-size">DIR</td></tr>`),
      ...files.map((f) => `<tr><td class="muted file-name">📄 ${escapeHtml(f.name)}</td>${date(f)}<td class="muted col-size">${fmtBytes(f.size_bytes)}</td></tr>`),
    ];
    body.innerHTML = rows.length
      ? rows.join("")
      : stateRow(3, q
        ? { icon: "🔍", title: `Sin coincidencias para «${$("dstFilter").value.trim()}»` }
        : { icon: "📭", title: "Carpeta vacía", text: "Los archivos se copiarán aquí.", actions: [{ id: "dst-mkdir", label: "+ Carpeta" }] });
  } else {
    const res = state.dstRemoteData;
    if (!res || !res.ok) return;
    const base = res.path === "/" ? "" : res.path;
    const dir = state.dstSort.order === "desc" ? -1 : 1;
    const dirs = (res.dirs || [])
      .filter((name) => !q || name.toLowerCase().includes(q))
      .sort((a, b) => a.localeCompare(b, "es", { numeric: true, sensitivity: "base" }) * dir);
    body.innerHTML = dirs.length
      ? dirs.map((name) => `<tr><td><button type="button" class="dir-link" data-dopen="${escapeHtml(`${base}/${name}`)}">📁 ${escapeHtml(name)}</button></td><td class="col-mtime"></td><td class="muted col-size">DIR</td></tr>`).join("")
      : stateRow(3, { icon: "📭", title: q ? "Sin coincidencias" : `Sin subcarpetas en ${state.dstRemotePath}`, text: q ? "" : "Los archivos se subirán aquí." });
  }
}

function updateFavButton() {
  const isFav = state.favorites.includes(state.dstLocalPath);
  const b = $("btnDstFav");
  b.textContent = isFav ? "★ En favoritos" : "☆ Favorito";
  b.setAttribute("aria-pressed", String(isFav));
  b.title = isFav ? "Quitar esta carpeta de favoritos" : "Añadir esta carpeta a favoritos";
}

async function loadDstDir(pathOverride) {
  if (state.useAutoDest) return;
  if (state.exploreMode === "mover") {
    const target = pathOverride !== undefined ? pathOverride : (state.dstLocalPath || (state.settings && state.settings.mover_dest) || "");
    try {
      const qs = new URLSearchParams({ kind: "mover", show_hidden: "false" });
      if (target) qs.set("path", target);
      const data = await api(`/api/fs/list?${qs}`);
      state.dstLocalData = data;
      state.dstLocalPath = data.path;
      state.dstLocalParent = data.parent;
      updateMobileDstLabel();
      updateFavButton();
      $("btnDstUp").disabled = !data.parent;
      $("dstDiskInfo").textContent = data.disk ? `Libre: ${fmtBytes(data.disk.free_bytes)}` : "";
      renderBookmarksBar($("dstBookmarks"), data.bookmarks || [], true, (p) => loadDstDir(p));
      renderBreadcrumbs($("dstBreadcrumbs"), data.path, data.data_root, (p) => loadDstDir(p));
      renderDstTable();
    } catch (err) {
      $("dstTableBody").innerHTML = stateRow(3, { icon: "⚠️", title: "No se pudo abrir el destino", text: err.message, error: true, actions: [{ id: "dst-retry", label: "↻ Reintentar", primary: true }] });
    }
  } else {
    const target = pathOverride !== undefined ? pathOverride : (state.dstRemotePath || (state.settings && state.settings.ftp_remote_dir) || "/");
    state.dstRemotePath = target || "/";
    updateMobileDstLabel();
    $("dstDiskInfo").textContent = ftpSpaceText();
    $("btnDstUp").disabled = state.dstRemotePath === "/";
    renderBookmarksBar($("dstBookmarks"), ["/", "/install", "/nsp", "/switch"].map((p) => ({ label: p, path: p })), false, (p) => loadDstDir(p));
    renderBreadcrumbs($("dstBreadcrumbs"), state.dstRemotePath, "/", (p) => loadDstDir(p));
    $("dstTableBody").innerHTML = stateRow(3, { loading: true, text: "Conectando con el servidor FTP…" });
    try {
      const res = await api(`/api/ftp/dirs?path=${encodeURIComponent(state.dstRemotePath)}`);
      state.dstRemoteData = res;
      if (!res.ok) {
        $("dstTableBody").innerHTML = stateRow(3, {
          icon: "📡",
          title: "Sin conexión con el servidor FTP",
          text: `${res.error || "Revisa Ajustes"}. Puedes escribir la ruta remota igualmente (✎).`,
          error: true,
          actions: [{ id: "dst-retry", label: "↻ Reintentar", primary: true }, { id: "go-config", label: "⚙️ Ajustes FTP" }],
        });
        return;
      }
      renderDstTable();
    } catch (err) {
      $("dstTableBody").innerHTML = stateRow(3, { icon: "⚠️", title: "Error", text: err.message, error: true, actions: [{ id: "dst-retry", label: "↻ Reintentar", primary: true }] });
    }
  }
}

$("dstTableBody").addEventListener("click", (e) => {
  const openEl = e.target.closest("[data-dopen]");
  if (openEl) return loadDstDir(openEl.dataset.dopen);
  const st = e.target.closest("[data-state-action]");
  if (!st) return;
  if (st.dataset.stateAction === "dst-retry") loadDstDir();
  if (st.dataset.stateAction === "dst-mkdir") $("btnDstNewDir").click();
  if (st.dataset.stateAction === "go-config") switchTab("config");
});

$("btnDstUp").onclick = () => {
  if (state.exploreMode === "mover") {
    if (state.dstLocalParent) loadDstDir(state.dstLocalParent);
  } else {
    const cur = state.dstRemotePath || "/";
    const idx = cur.lastIndexOf("/");
    loadDstDir(idx <= 0 ? "/" : cur.slice(0, idx));
  }
};
$("dstFilter").addEventListener("input", renderDstTable);
$("btnDstRefresh").onclick = () => {
  loadDstDir();
  if (state.exploreMode === "ftp") fetchFtpSpace(true);
};
$("btnDstNewDir").onclick = () => {
  if (state.exploreMode !== "mover") return;
  makeDir(state.dstLocalPath, (res) => loadDstDir(res.path), relPath(state.dstLocalPath));
};
$("btnDstFav").onclick = async () => {
  const p = state.dstLocalPath;
  if (!p) return;
  const isFav = state.favorites.includes(p);
  const next = isFav ? state.favorites.filter((x) => x !== p) : [...state.favorites, p];
  await saveFavorites(next);
  showToast(isFav ? "Quitada de favoritos" : "Añadida a favoritos");
};

// =========================================================================
// AÑADIR A LA COLA
// =========================================================================
async function enqueuePaths(paths, recursive, onlySwitch, startNow) {
  if (!paths.length) return showToast("Selecciona al menos un archivo o carpeta");
  let destDir = "";
  if (!state.useAutoDest) destDir = state.exploreMode === "mover" ? state.dstLocalPath : state.dstRemotePath;
  const payload = {
    kind: state.exploreMode,
    paths,
    recursive,
    only_switch: onlySwitch,
    base_browse_dir: state.srcPath,
    operation: state.exploreMode === "mover" ? state.moverOp : null,
    dest_dir: destDir,
  };
  try {
    const res = await api("/api/queue/add", { method: "POST", body: JSON.stringify(payload) });
    const kind = state.exploreMode;
    if (startNow && res.added > 0) {
      await api(`/api/queue/${kind}/start`, { method: "POST" });
      showToast(`Añadidos ${res.added} y cola iniciada`);
    } else {
      showToast(`Añadidos ${res.added} a la cola${res.skipped ? ` (omitidos por duplicado: ${res.skipped})` : ""}`, {
        action: res.added > 0 ? { label: "Ver cola", fn: async () => { switchQueueTab(kind, false); switchTab("queue"); } } : null,
      });
    }
    state.selectedPaths.clear();
    loadSrcDir(state.srcPath);
    refreshStatus();
  } catch (err) {
    showToast(err.message, { type: "err" });
  }
}

$("btnAddSelected").onclick = () => enqueuePaths(Array.from(state.selectedPaths), true, false, false);
$("btnSendAndStart").onclick = () => enqueuePaths(Array.from(state.selectedPaths), true, false, true);
$("btnAddSwitchOnly").onclick = () => {
  const paths = state.selectedPaths.size
    ? Array.from(state.selectedPaths)
    : (state.srcData?.files || []).filter((f) => f.switch_type).map((f) => f.path);
  enqueuePaths(paths, true, true, false);
};

// =========================================================================
// COLAS
// =========================================================================
function switchQueueTab(kind, reload = true) {
  state.queueTab = kind;
  setPressed($("qTabMover"), kind === "mover");
  setPressed($("qTabFtp"), kind === "ftp");
  $("btnQueueAutoOrder").textContent = kind === "ftp" ? "↕ Auto-ordenar (Base → UPD → DLC)" : "↕ Auto-ordenar";
  state.queueSig = "";
  if (reload) {
    loadQueue(true);
    if (kind === "ftp") fetchFtpSpace(true);
  }
}
$("qTabMover").onclick = () => switchQueueTab("mover");
$("qTabFtp").onclick = () => switchQueueTab("ftp");
$("chkOnlyPending").onchange = () => loadQueue(true);

function queueSignature(items) {
  return items.map((it) => [it.id, it.status, it.dest_path, it.operation, it.error || "", it.note || ""].join("|")).join("~");
}

let queueLoading = false;
async function loadQueue(force = false) {
  if (queueLoading) return;
  queueLoading = true;
  try {
    const only = $("chkOnlyPending").checked ? "true" : "false";
    const data = await api(`/api/queue?kind=${state.queueTab}&only_pending=${only}`);
    const items = data.items || [];
    const sig = `${state.queueTab}:${only}:${queueSignature(items)}`;
    state.lastQueueItems = items;
    if (!force && sig === state.queueSig) {
      updateQueueProgress();
    } else if (state.drag || !$("popMenu").classList.contains("hidden")) {
      state.queueDirty = true;
    } else {
      state.queueSig = sig;
      state.queueDirty = false;
      renderQueue();
    }
    renderQueueSummary();
  } catch (err) {
    $("queueList").innerHTML = "";
    $("queueEmpty").classList.remove("hidden");
    $("queueEmpty").innerHTML = `<div class="empty-state is-error" role="alert"><span class="es-icon">⚠️</span><span class="es-title">No se pudo cargar la cola</span><span>${escapeHtml(err.message)}</span></div>`;
  } finally {
    queueLoading = false;
  }
}

function dirOf(p) {
  const s = String(p || "");
  const i = s.lastIndexOf("/");
  if (i < 0) return "";
  return i === 0 ? "/" : s.slice(0, i);
}

// Ruta compacta: carpetas relativas a la raíz de datos (el nombre ya se muestra en negrita).
// Si el destino tiene otro nombre (conflicto «renombrar»), se muestra completo.
function queueItemView(it) {
  const name = baseName(it.source_path);
  const srcFull = relPath(it.source_path);
  const destFull = it.kind === "ftp" ? it.dest_path : relPath(it.dest_path);
  const relSrc = dirOf(srcFull) || it.rel_src || "./";
  let relDest = dirOf(destFull) || "/";
  if (baseName(it.dest_path) !== name) relDest = destFull;
  return { name, relSrc, relDest };
}

function currentFor(kind) {
  const w = state.workers[kind];
  return w && w.running ? w : null;
}

function renderQueue() {
  const list = $("queueList");
  const items = state.lastQueueItems;
  const empty = $("queueEmpty");
  if (!items.length) {
    list.innerHTML = "";
    empty.classList.remove("hidden");
    const only = $("chkOnlyPending").checked;
    empty.innerHTML = `<div class="empty-state">
      <span class="es-icon" aria-hidden="true">${state.queueTab === "ftp" ? "📡" : "💾"}</span>
      <span class="es-title">${only ? "No hay elementos activos" : "La cola está vacía"}</span>
      <span>${state.queueTab === "ftp" ? "Elige archivos en Explorar y pulsa «A la cola FTP»." : "Elige archivos en Explorar y pulsa «Copiar» o «Mover»."}</span>
      <div class="es-actions">
        <button type="button" class="btn btn-primary" data-qempty="explore">📁 Ir a Explorar</button>
        ${only ? '<button type="button" class="btn" data-qempty="all">Ver también completados</button>' : ""}
      </div></div>`;
    return;
  }
  empty.classList.add("hidden");
  const w = currentFor(state.queueTab);
  list.innerHTML = items.map((it, idx) => {
    const v = queueItemView(it);
    const isProc = it.status === "PROC";
    const canEdit = !isProc && it.status !== "OK";
    const nm = escapeHtml(v.name);
    const sw = it.switch_type ? `<span class="tag tag-${it.switch_type.toLowerCase()}">${escapeHtml(it.switch_type)}</span>` : "";
    const opTag = `<span class="tag">${escapeHtml((it.operation || "").toUpperCase())}</span>`;
    let actions;
    if (isProc) {
      actions = `<button type="button" class="btn btn-sm btn-danger" data-qstop="1" aria-label="Detener la cola">⏹ Detener</button>`;
    } else {
      actions = `
        <button type="button" class="btn btn-sm btn-icon" data-qmove="${it.id}:top" aria-label="Poner primero ${nm}" title="Poner primero">⤒</button>
        <button type="button" class="btn btn-sm btn-icon" data-qmove="${it.id}:up" aria-label="Subir ${nm}" title="Subir" ${idx === 0 ? "disabled" : ""}>↑</button>
        <button type="button" class="btn btn-sm btn-icon" data-qmove="${it.id}:down" aria-label="Bajar ${nm}" title="Bajar" ${idx === items.length - 1 ? "disabled" : ""}>↓</button>
        <button type="button" class="btn btn-sm btn-icon" data-qpause="${it.id}" aria-label="${it.status === "PAUS" ? "Reanudar" : "Pausar"} ${nm}" title="${it.status === "PAUS" ? "Reanudar" : "Pausar"}" ${it.status === "OK" ? "disabled" : ""}>${it.status === "PAUS" ? "▶" : "❚❚"}</button>
        ${it.kind === "mover" && canEdit ? `<button type="button" class="btn btn-sm btn-icon" data-qop="${it.id}" aria-label="Alternar copiar o mover ${nm}" title="Alternar copiar / mover">⇄</button>` : ""}
        ${canEdit ? `<button type="button" class="btn btn-sm btn-icon" data-qdest="${it.id}" aria-label="Cambiar destino de ${nm}" title="Cambiar destino">📂</button>` : ""}
        <button type="button" class="btn btn-sm btn-icon btn-danger" data-qdel="${it.id}" aria-label="Quitar ${nm} de la cola" title="Quitar de la cola">✕</button>`;
    }
    const prog = isProc ? `
      <div class="q-progress" data-qprog="${it.id}">
        <div class="progress-info"><span class="prog-stats" data-qprog-text>${w && w.phase !== "preparing" ? "" : "Preparando…"}</span></div>
        <div class="progress-bar thin ${!w || w.phase === "preparing" ? "indeterminate" : ""}"><div class="progress-fill ${it.kind === "ftp" ? "ftp" : ""}"></div></div>
      </div>` : "";
    return `
      <li class="qitem ${isProc ? "is-proc" : ""}" data-id="${it.id}" data-index="${idx}">
        <button type="button" class="q-handle" data-qhandle="${it.id}" aria-label="Reordenar ${nm}: arrastra o usa las flechas arriba y abajo" title="Arrastra para reordenar" ${isProc ? "disabled" : ""}>⠿</button>
        <span class="q-idx">#${idx + 1}</span>
        <span class="q-tags"><span class="tag tag-${it.status.toLowerCase()}">${escapeHtml(it.status)}</span>${sw}${opTag}</span>
        <div class="q-main">
          <div class="q-name" title="${escapeHtml(it.source_path)}">${nm}</div>
          <div class="q-route">
            <span class="q-route-text" title="${escapeHtml(`${it.source_path} → ${it.dest_path}`)}">${escapeHtml(v.relSrc)} → ${escapeHtml(v.relDest)}</span>
            <button type="button" class="q-copy" data-qcopy="${it.id}" aria-label="Copiar ruta completa de ${nm}" title="Copiar ruta completa">⧉</button>
          </div>
          ${it.note ? `<span class="sub-note">${escapeHtml(it.note)}</span>` : ""}
          ${it.error ? `<span class="sub-err">${escapeHtml(it.error)}</span>` : ""}
          ${prog}
        </div>
        <span class="q-size">${fmtBytes(it.size_bytes)}</span>
        <div class="q-actions">${actions}</div>
        <button type="button" class="btn btn-sm btn-icon q-more" data-qmore="${it.id}" aria-label="Acciones para ${nm}" aria-haspopup="menu" aria-expanded="false">⋯</button>
      </li>`;
  }).join("");
  updateQueueProgress();
}

function updateQueueProgress() {
  const w = currentFor(state.queueTab);
  document.querySelectorAll("[data-qprog]").forEach((el) => {
    const bar = el.querySelector(".progress-bar");
    const fill = el.querySelector(".progress-fill");
    const txt = el.querySelector("[data-qprog-text]");
    const cur = w && w.cur && (w.cur.id == null || String(w.cur.id) === el.dataset.qprog) ? w.cur : null;
    if (!w || w.phase === "preparing" || !cur) {
      bar.classList.add("indeterminate");
      txt.textContent = w ? "Preparando…" : "En pausa (la cola está detenida)";
      return;
    }
    bar.classList.remove("indeterminate");
    const pct = Number(cur.percent || 0);
    fill.style.width = `${Math.min(100, pct)}%`;
    const done = cur.bytes_done != null && cur.total_bytes ? ` · ${fmtBytes(cur.bytes_done)}/${fmtBytes(cur.total_bytes)}` : "";
    txt.textContent = `${fmtNum(pct, 1)} %${done} · ${curSpeed(cur)} · ETA ${curEta(cur)}`;
  });
}

function renderQueueSummary() {
  const w = state.workers[state.queueTab];
  const box = $("queueSummary");
  if (!w || !w.agg || !w.agg.total_items) {
    box.classList.add("hidden");
    return;
  }
  box.classList.remove("hidden");
  const txt = aggregateText(w) || `${w.agg.done_items}/${w.agg.total_items}`;
  $("qsText").textContent = txt;
  const extra = [];
  if (w.running) extra.push(w.phase === "preparing" ? "Preparando…" : `En curso: ${w.cur ? w.cur.name : "-"}`);
  else extra.push(w.pendingCount ? "Cola detenida" : "Sin pendientes");
  if (state.queueTab === "ftp") {
    const t = ftpSpaceText();
    if (t) extra.push(t);
  } else if (w.space_check && w.space_check.free_bytes != null) {
    extra.push(`Libre dest. ${fmtBytes(w.space_check.free_bytes)}`);
  }
  $("qsExtra").textContent = extra.join(" · ");
  $("qsFill").style.width = `${(aggregateFraction(w) * 100).toFixed(1)}%`;
}

async function queueAction(fn) {
  try {
    await fn();
  } catch (err) {
    showToast(err.message, { type: "err" });
  }
  loadQueue(true);
  refreshStatus();
}

const qMove = (id, direction) => queueAction(() => api(`/api/queue/item/${id}/move`, { method: "POST", body: JSON.stringify({ direction }) }));
const qPause = (id) => queueAction(() => api(`/api/queue/item/${id}/pause`, { method: "POST" }));
const qToggleOp = (id) => queueAction(() => api(`/api/queue/item/${id}/toggle-op`, { method: "POST" }));
const qRemove = (id) => queueAction(() => api(`/api/queue/item/${id}`, { method: "DELETE" }));

async function applyQueueDestChange(itemIds, newDestDir) {
  try {
    const res = await api("/api/queue/change-dest", { method: "POST", body: JSON.stringify({ item_ids: itemIds, dest_dir: newDestDir }) });
    showToast(`Destino cambiado en ${res.updated} elemento(s)`);
  } catch (err) {
    showToast(err.message, { type: "err" });
  }
  loadQueue(true);
  refreshStatus();
}

async function promptChangeQueueDest(itemIds) {
  if (!itemIds.length) return showToast("No hay elementos editables en la cola");
  if (state.queueTab === "mover") {
    const initPath = state.dstLocalPath || (state.settings && state.settings.mover_dest) || "";
    openPicker(null, initPath, (picked) => applyQueueDestChange(itemIds, picked), `Nuevo destino para ${itemIds.length} elemento(s)`);
  } else {
    const current = state.dstRemotePath || (state.settings && state.settings.ftp_remote_dir) || "/";
    const next = await uiPrompt({
      title: "Cambiar carpeta remota",
      label: `Nueva carpeta remota para ${itemIds.length} elemento(s)`,
      value: current,
      okText: "Cambiar",
      validate: (v) => (v.startsWith("/") ? "" : "La ruta remota debe empezar por «/»"),
    });
    if (next) applyQueueDestChange(itemIds, next);
  }
}

function queueMenuItems(it) {
  const v = queueItemView(it);
  if (it.status === "PROC") {
    return [
      { icon: "⏹", label: "Detener la cola", danger: true, onClick: () => stopQueue(state.queueTab) },
      { icon: "⧉", label: "Copiar ruta de origen", onClick: () => copyText(it.source_path) },
    ];
  }
  const canEdit = it.status !== "OK";
  const idx = state.lastQueueItems.findIndex((x) => x.id === it.id);
  return [
    { icon: "⤒", label: "Poner primero", disabled: idx === 0, onClick: () => qMove(it.id, "top") },
    { icon: "↑", label: "Subir", disabled: idx === 0, onClick: () => qMove(it.id, "up") },
    { icon: "↓", label: "Bajar", disabled: idx === state.lastQueueItems.length - 1, onClick: () => qMove(it.id, "down") },
    { icon: it.status === "PAUS" ? "▶" : "❚❚", label: it.status === "PAUS" ? "Reanudar" : "Pausar", disabled: it.status === "OK", onClick: () => qPause(it.id) },
    ...(it.kind === "mover" && canEdit ? [{ icon: "⇄", label: it.operation === "mover" ? "Cambiar a copiar" : "Cambiar a mover", onClick: () => qToggleOp(it.id) }] : []),
    ...(canEdit ? [{ icon: "📂", label: "Cambiar destino", onClick: () => promptChangeQueueDest([it.id]) }] : []),
    { sep: true },
    { icon: "⧉", label: "Copiar ruta de origen", onClick: () => copyText(it.source_path) },
    { icon: "⧉", label: "Copiar ruta de destino", onClick: () => copyText(it.dest_path) },
    { sep: true },
    { icon: "✕", label: `Quitar «${v.name.length > 28 ? `${v.name.slice(0, 27)}…` : v.name}» de la cola`, danger: true, onClick: () => qRemove(it.id) },
  ];
}

$("queueList").addEventListener("click", (e) => {
  const find = (attr) => e.target.closest(`[${attr}]`);
  let el;
  if ((el = find("data-qmove"))) {
    const [id, dir] = el.dataset.qmove.split(":");
    return qMove(id, dir);
  }
  if ((el = find("data-qpause"))) return qPause(el.dataset.qpause);
  if ((el = find("data-qop"))) return qToggleOp(el.dataset.qop);
  if ((el = find("data-qdest"))) return promptChangeQueueDest([Number(el.dataset.qdest)]);
  if ((el = find("data-qdel"))) return qRemove(el.dataset.qdel);
  if ((el = find("data-qstop"))) return stopQueue(state.queueTab);
  if ((el = find("data-qcopy"))) {
    const it = state.lastQueueItems.find((x) => String(x.id) === el.dataset.qcopy);
    if (it) copyText(`${it.source_path} → ${it.dest_path}`);
    return;
  }
  if ((el = find("data-qmore"))) {
    const it = state.lastQueueItems.find((x) => String(x.id) === el.dataset.qmore);
    if (it) openPopMenu(el, queueMenuItems(it));
  }
});

$("queueEmpty").addEventListener("click", (e) => {
  const b = e.target.closest("[data-qempty]");
  if (!b) return;
  if (b.dataset.qempty === "explore") switchTab("explore");
  if (b.dataset.qempty === "all") {
    $("chkOnlyPending").checked = false;
    loadQueue(true);
  }
});

// --- Reordenar arrastrando (ratón y táctil con Pointer Events) y con teclado ---
async function moveItemTo(id, from, to) {
  if (from === to) return;
  try {
    if (to === 0) {
      await api(`/api/queue/item/${id}/move`, { method: "POST", body: JSON.stringify({ direction: "top" }) });
    } else {
      const dir = to < from ? "up" : "down";
      for (let i = 0; i < Math.abs(to - from); i++) {
        await api(`/api/queue/item/${id}/move`, { method: "POST", body: JSON.stringify({ direction: dir }) });
      }
    }
  } catch (err) {
    showToast(err.message, { type: "err" });
  }
  loadQueue(true);
}

$("queueList").addEventListener("pointerdown", (e) => {
  const handle = e.target.closest("[data-qhandle]");
  if (!handle || handle.disabled || e.button > 0) return;
  e.preventDefault();
  const li = handle.closest(".qitem");
  handle.setPointerCapture(e.pointerId);
  state.drag = { id: Number(li.dataset.id), from: Number(li.dataset.index), to: Number(li.dataset.index), li, handle, pointerId: e.pointerId, moved: false, startY: e.clientY };
  li.classList.add("dragging");
  $("queueList").classList.add("is-dragging");
});

$("queueList").addEventListener("pointermove", (e) => {
  const d = state.drag;
  if (!d || e.pointerId !== d.pointerId) return;
  if (Math.abs(e.clientY - d.startY) > 4) d.moved = true;
  document.querySelectorAll(".qitem.drop-before, .qitem.drop-after").forEach((x) => x.classList.remove("drop-before", "drop-after"));
  const under = document.elementFromPoint(e.clientX, e.clientY);
  const target = under && under.closest(".qitem");
  if (target && target !== d.li) {
    const r = target.getBoundingClientRect();
    const after = e.clientY > r.top + r.height / 2;
    target.classList.add(after ? "drop-after" : "drop-before");
    const tIdx = Number(target.dataset.index);
    let to = after ? tIdx + 1 : tIdx;
    if (to > d.from) to -= 1;
    d.to = Math.max(0, Math.min(state.lastQueueItems.length - 1, to));
  }
  const edge = 60;
  const bottomLimit = window.innerHeight - (parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--bottom-nav-h")) || 0);
  if (e.clientY < edge) window.scrollBy(0, -12);
  else if (e.clientY > bottomLimit - edge) window.scrollBy(0, 12);
});

function endDrag(e, cancel) {
  const d = state.drag;
  if (!d || (e && e.pointerId !== d.pointerId)) return;
  d.li.classList.remove("dragging");
  $("queueList").classList.remove("is-dragging");
  document.querySelectorAll(".qitem.drop-before, .qitem.drop-after").forEach((x) => x.classList.remove("drop-before", "drop-after"));
  state.drag = null;
  if (!cancel && d.moved && d.to !== d.from) moveItemTo(d.id, d.from, d.to);
  else if (state.queueDirty) loadQueue(true);
}
$("queueList").addEventListener("pointerup", (e) => endDrag(e, false));
$("queueList").addEventListener("pointercancel", (e) => endDrag(e, true));

$("queueList").addEventListener("keydown", (e) => {
  const handle = e.target.closest("[data-qhandle]");
  if (!handle) return;
  const id = handle.dataset.qhandle;
  const keys = { ArrowUp: "up", ArrowDown: "down", Home: "top" };
  if (keys[e.key]) {
    e.preventDefault();
    qMove(id, keys[e.key]).then(() => {
      requestAnimationFrame(() => {
        const h = document.querySelector(`[data-qhandle="${id}"]`);
        if (h) h.focus();
      });
    });
  }
});

$("btnQueueMore").onclick = () => {
  const click = (id) => () => $(id).click();
  openPopMenu($("btnQueueMore"), [
    { icon: "📂", label: "Cambiar destino de los pendientes", onClick: click("btnQueueChangeDest") },
    { icon: "↕", label: state.queueTab === "ftp" ? "Auto-ordenar (Base → UPD → DLC)" : "Auto-ordenar", onClick: click("btnQueueAutoOrder") },
    { icon: "↻", label: "Reintentar errores", onClick: click("btnQueueRetry") },
    { sep: true },
    { icon: "🧹", label: "Limpiar completados (OK)", onClick: click("btnQueueClearOk") },
    { icon: "🧹", label: "Limpiar errores (ERR)", onClick: click("btnQueueClearErr") },
    { icon: "🗑", label: "Vaciar pendientes…", danger: true, onClick: click("btnQueueClearPend") },
  ]);
};

$("btnQueueChangeDest").onclick = () => {
  const ids = (state.lastQueueItems || []).filter((it) => it.status !== "PROC" && it.status !== "OK").map((it) => it.id);
  promptChangeQueueDest(ids);
};
$("btnQueueAutoOrder").onclick = () => queueAction(() => api(`/api/queue/${state.queueTab}/auto-order`, { method: "POST" }));
$("btnQueueRetry").onclick = () => queueAction(() => api(`/api/queue/${state.queueTab}/retry-errors`, { method: "POST" }));
$("btnQueueClearOk").onclick = () => queueAction(() => api(`/api/queue/${state.queueTab}/clear?scope=completed`, { method: "POST" }));
$("btnQueueClearErr").onclick = () => queueAction(() => api(`/api/queue/${state.queueTab}/clear?scope=errors`, { method: "POST" }));
$("btnQueueClearPend").onclick = async () => {
  const n = state.lastQueueItems.filter((it) => ["PEND", "REAN", "PAUS"].includes(it.status)).length;
  const ok = await uiConfirm({
    title: "Vaciar pendientes",
    html: `<p>Se quitarán de la cola ${state.queueTab === "ftp" ? "FTP" : "Mover"} <strong>${n}</strong> elemento(s) pendientes o pausados.</p><p class="muted">Los archivos no se borran del disco.</p>`,
    okText: "Vaciar pendientes",
    danger: true,
  });
  if (ok) queueAction(() => api(`/api/queue/${state.queueTab}/clear?scope=pending`, { method: "POST" }));
};

// =========================================================================
// HISTORIAL
// =========================================================================
async function loadHistory(reset = false) {
  const h = state.history;
  if (reset) {
    h.items = [];
    h.offset = 0;
    $("historyBody").innerHTML = stateRow(6, { loading: true, text: "Cargando historial…" });
  }
  try {
    const qs = new URLSearchParams({ limit: "50", offset: String(h.offset) });
    const kind = $("historyKind").value;
    if (kind) qs.set("kind", kind);
    const res = await api(`/api/history?${qs}`);
    h.items = h.items.concat(res.items || []);
    h.total = res.total ?? h.items.length;
    h.offset = h.items.length;
    renderHistory();
  } catch (err) {
    $("btnHistoryMore").classList.add("hidden");
    $("historyCount").textContent = "";
    $("historyBody").innerHTML = isMissing(err)
      ? stateRow(6, { icon: "🕘", title: "Historial no disponible", text: "Necesita una versión más reciente del servidor de Toolbox." })
      : stateRow(6, { icon: "⚠️", title: "No se pudo cargar el historial", text: err.message, error: true, actions: [{ id: "history-retry", label: "↻ Reintentar", primary: true }] });
  }
}

function renderHistory() {
  const h = state.history;
  if (!h.items.length) {
    $("historyBody").innerHTML = stateRow(6, { icon: "🕘", title: "Aún no hay transferencias terminadas", text: "Aquí verás cada copia, movimiento o subida al terminar, aunque limpies la cola.", actions: [{ id: "go-explore", label: "📁 Ir a Explorar", primary: true }] });
  } else {
    $("historyBody").innerHTML = h.items.map((it) => {
      const name = baseName(it.src || it.source_path || "");
      const st = (it.status || "").toUpperCase();
      const kind = it.op === "backup" ? "BACKUP SD" : (it.kind === "ftp" ? "FTP" : (it.op || it.operation || "mover").toUpperCase());
      return `<tr>
        <td class="h-date">${escapeHtml(fmtDate(it.finished_at || it.started_at))}</td>
        <td class="h-item">
          <span class="cell-name" title="${escapeHtml(it.src || "")}">${escapeHtml(name)}</span> <span class="tag">${escapeHtml(kind)}</span>
          <span class="name-sub" title="${escapeHtml(`${it.src || ""} → ${it.dest || ""}`)}">→ ${escapeHtml(it.kind === "ftp" ? (it.dest || "") : relPath(it.dest || ""))}</span>
          ${it.error ? `<span class="sub-err">${escapeHtml(it.error)}</span>` : ""}
        </td>
        <td class="h-num">${fmtBytes(it.size ?? it.size_bytes)}</td>
        <td class="h-num">${fmtDuration(it.duration_s)}</td>
        <td class="h-num">${fmtSpeed(it.avg_bps)}</td>
        <td><span class="tag tag-${escapeHtml(st.toLowerCase())}">${escapeHtml(st || "-")}</span></td>
      </tr>`;
    }).join("");
  }
  $("historyCount").textContent = h.items.length ? `Mostrando ${h.items.length} de ${h.total}` : "";
  $("btnHistoryMore").classList.toggle("hidden", h.items.length >= h.total);
}

$("historyBody").addEventListener("click", (e) => {
  const b = e.target.closest("[data-state-action]");
  if (!b) return;
  if (b.dataset.stateAction === "history-retry") loadHistory(true);
  if (b.dataset.stateAction === "go-explore") switchTab("explore");
});
$("historyKind").onchange = () => loadHistory(true);
$("btnHistoryRefresh").onclick = () => loadHistory(true);
$("btnHistoryMore").onclick = () => loadHistory(false);
$("btnHistoryClear").onclick = async () => {
  const kind = $("historyKind").value;
  const label = kind === "ftp" ? "de FTP" : (kind === "mover" ? "de Mover / Copiar" : "completo");
  const ok = await uiConfirm({ title: "Borrar historial", html: `<p>Se borrará el historial ${label}. Los archivos no se tocan.</p>`, okText: "Borrar historial", danger: true });
  if (!ok) return;
  try {
    await api(`/api/history${kind ? `?kind=${kind}` : ""}`, { method: "DELETE" });
    showToast("Historial borrado");
  } catch (err) {
    showToast(err.message, { type: "err" });
  }
  loadHistory(true);
};

// =========================================================================
// PAPELERA
// =========================================================================
async function loadTrash() {
  const t = state.trash;
  $("trashBody").innerHTML = stateRow(5, { loading: true, text: "Cargando papelera…" });
  $("trashInfo").textContent = trashInfoText();
  try {
    const res = await api("/api/trash");
    state.trashAvailable = true;
    const patch = {};
    if (res.trash_days != null) patch.trash_days = res.trash_days;
    if (res.trash_enabled != null && !(state.settings && "trash_enabled" in state.settings)) patch.trash_enabled = res.trash_enabled;
    state.settings = { ...(state.settings || {}), ...patch };
    paintTrashToggle();
    t.items = res.items || [];
    t.selected = new Set([...t.selected].filter((id) => t.items.some((x) => String(x.id) === String(id))));
    renderTrash();
  } catch (err) {
    if (isMissing(err)) state.trashAvailable = false;
    $("trashBody").innerHTML = isMissing(err)
      ? stateRow(5, { icon: "🗑", title: "Papelera no disponible", text: "Este servidor borra directamente. Actualiza Toolbox para poder deshacer borrados." })
      : stateRow(5, { icon: "⚠️", title: "No se pudo cargar la papelera", text: err.message, error: true, actions: [{ id: "trash-retry", label: "↻ Reintentar", primary: true }] });
    updateTrashButtons();
  }
}

function renderTrash() {
  const t = state.trash;
  if (!t.items.length) {
    $("trashBody").innerHTML = trashEnabled()
      ? stateRow(5, { icon: "✨", title: "La papelera está vacía", text: "Lo que borres desde Explorar aparecerá aquí y podrás restaurarlo." })
      : stateRow(5, { icon: "🗑", title: "Papelera desactivada", text: "Los borrados son definitivos. Actívala con el botón de arriba o en Ajustes para poder deshacerlos." });
  } else {
    $("trashBody").innerHTML = t.items.map((it) => {
      const sel = t.selected.has(String(it.id));
      const name = it.name || baseName(it.original_path);
      return `<tr class="${sel ? "is-selected" : ""}">
        <td class="col-chk"><label class="chk-hit"><input type="checkbox" class="trash-chk" data-tid="${escapeHtml(String(it.id))}" ${sel ? "checked" : ""} aria-label="Seleccionar ${escapeHtml(name)}"></label></td>
        <td class="t-item"><span class="cell-name">${it.is_dir ? "📁" : "📄"} ${escapeHtml(name)}</span><span class="name-sub" title="${escapeHtml(it.original_path)}">${escapeHtml(relPath(it.original_path))}</span></td>
        <td class="h-num">${fmtBytes(it.size)}</td>
        <td class="t-date">${escapeHtml(fmtDate(it.trashed_at))}</td>
        <td class="t-date">${escapeHtml(fmtRelativeFuture(it.expires_at))}</td>
      </tr>`;
    }).join("");
  }
  updateTrashButtons();
}

function updateTrashButtons() {
  const n = state.trash.selected.size;
  $("btnTrashRestore").disabled = n === 0;
  $("btnTrashDeleteSel").disabled = n === 0;
  $("btnTrashEmpty").disabled = !state.trash.items.length;
  $("btnTrashRestore").textContent = n ? `↩ Restaurar (${n})` : "↩ Restaurar seleccionados";
  const all = state.trash.items.length > 0 && n === state.trash.items.length;
  $("chkTrashAll").checked = all;
  $("chkTrashAll").indeterminate = n > 0 && !all;
}

$("trashBody").addEventListener("change", (e) => {
  const c = e.target.closest(".trash-chk");
  if (!c) return;
  if (c.checked) state.trash.selected.add(c.dataset.tid);
  else state.trash.selected.delete(c.dataset.tid);
  c.closest("tr").classList.toggle("is-selected", c.checked);
  updateTrashButtons();
});
$("trashBody").addEventListener("click", (e) => {
  const b = e.target.closest("[data-state-action]");
  if (b && b.dataset.stateAction === "trash-retry") loadTrash();
});
$("chkTrashAll").onchange = (e) => {
  state.trash.selected = e.target.checked ? new Set(state.trash.items.map((x) => String(x.id))) : new Set();
  renderTrash();
};
$("btnTrashRefresh").onclick = loadTrash;

function trashIds() {
  return state.trash.items.filter((x) => state.trash.selected.has(String(x.id))).map((x) => x.id);
}

$("btnTrashRestore").onclick = async () => {
  const ids = trashIds();
  if (!ids.length) return;
  try {
    const res = await api("/api/trash/restore", { method: "POST", body: JSON.stringify({ ids }) });
    showToast(`Restaurado${ids.length === 1 ? "" : "s"} ${res.restored ?? ids.length} elemento(s)`);
    state.trash.selected.clear();
  } catch (err) {
    showToast(err.message, { type: "err" });
  }
  loadTrash();
  if (state.srcData) loadSrcDir(state.srcPath);
};
$("btnTrashDeleteSel").onclick = async () => {
  const ids = trashIds();
  if (!ids.length) return;
  const ok = await uiConfirm({ title: "Eliminar definitivamente", html: `<p>Se eliminarán para siempre <strong>${ids.length}</strong> elemento(s) de la papelera. No se puede deshacer.</p>`, okText: "Eliminar definitivamente", danger: true });
  if (!ok) return;
  try {
    await api("/api/trash/empty", { method: "POST", body: JSON.stringify({ ids }) });
    showToast("Eliminados definitivamente");
    state.trash.selected.clear();
  } catch (err) {
    showToast(err.message, { type: "err" });
  }
  loadTrash();
};
$("btnTrashEmpty").onclick = async () => {
  const n = state.trash.items.length;
  const total = state.trash.items.reduce((s, x) => s + (Number(x.size) || 0), 0);
  const ok = await uiConfirm({ title: "Vaciar papelera", html: `<p>Se eliminarán para siempre <strong>${n}</strong> elemento(s) (${fmtBytes(total)}). No se puede deshacer.</p>`, okText: "Vaciar papelera", danger: true });
  if (!ok) return;
  try {
    await api("/api/trash/empty", { method: "POST", body: JSON.stringify({}) });
    showToast("Papelera vaciada");
    state.trash.selected.clear();
  } catch (err) {
    showToast(err.message, { type: "err" });
  }
  loadTrash();
};

// =========================================================================
// AJUSTES (GUARDADO AUTOMÁTICO)
// =========================================================================
const kbpsToMb = (kbps) => {
  const v = Number(kbps || 0) / 1024;
  return v ? String(Math.round(v * 100) / 100) : "0";
};
const mbToKbps = (mb) => Math.max(0, Math.round(Number(String(mb).replace(",", ".") || 0) * 1024));

function setSaveState(kind, msg) {
  const el = $("cfgSaveState");
  el.className = `save-indicator ${kind}`;
  el.textContent = msg;
}

function renderFavorites() {
  $("favList").innerHTML = state.favorites.map((p, i) => `
    <li><span aria-hidden="true">★</span><code title="${escapeHtml(p)}">${escapeHtml(p)}</code>
      <button type="button" class="btn btn-sm btn-ghost btn-icon" data-fav-del="${i}" aria-label="Quitar ${escapeHtml(p)} de favoritos" title="Quitar">✕</button></li>`).join("");
}

async function loadSettings() {
  try {
    const s = await api("/api/settings");
    state.settings = s;
    state.favorites = Array.isArray(s.mover_favorites) ? [...s.mover_favorites] : [];
    $("cfgMoverOrigin").value = s.mover_origin || "";
    $("cfgMoverDest").value = s.mover_dest || "";
    $("cfgMoverOp").value = s.mover_operation || "copiar";
    $("cfgMoverConflict").value = s.mover_conflict || "omitir";
    $("cfgMoverVerify").value = s.mover_verify || "tamano";
    $("cfgMoverLimitMb").value = kbpsToMb(s.mover_limit_kbps);
    $("cfgMoverRetries").value = s.mover_retries ?? 3;
    $("cfgMoverRetryWait").value = s.mover_retry_wait ?? 10;
    $("cfgMoverStruct").checked = !!s.mover_keep_structure;
    renderFavorites();

    $("cfgFtpHost").value = s.ftp_host || "";
    $("cfgFtpPort").value = s.ftp_port ?? 5000;
    $("cfgFtpUser").value = s.ftp_user || "";
    $("cfgFtpPassword").value = "";
    $("cfgFtpPwHint").textContent = s.ftp_has_password ? "(guardada)" : "(vacía)";
    $("cfgFtpLocalRoot").value = s.ftp_local_root || "";
    $("cfgFtpRemoteDir").value = s.ftp_remote_dir || "/";
    $("cfgFtpLimitMb").value = kbpsToMb(s.ftp_limit_kbps);
    $("cfgFtpRetries").value = s.ftp_retries ?? 3;
    $("cfgFtpRetryWait").value = s.ftp_retry_wait ?? 10;
    $("cfgFtpStruct").checked = !!s.ftp_keep_structure;
    $("cfgFtpPassive").checked = !!s.ftp_passive;
    $("cfgFtpTls").checked = !!s.ftp_tls;
    $("cfgFtpVerifySize").checked = !!s.ftp_verify_size;
    $("backupCard").classList.toggle("hidden", !Object.prototype.hasOwnProperty.call(s, "ftp_backup_dir"));
    $("cfgBackupDir").value = s.ftp_backup_dir || "";
    $("cfgBackupRemoteRoot").value = s.ftp_backup_remote_root || "/";
    $("cfgBackupExclude").value = (s.ftp_backup_exclude || []).join("\n");
    $("cfgBackupExcludeExt").value = (s.ftp_backup_exclude_ext || []).join(", ");
    $("cfgBackupIncremental").checked = !!s.ftp_backup_incremental;
    $("cfgBackupCompress").checked = !!s.ftp_backup_compress;
    $("cfgBackupKeep").value = s.ftp_backup_keep ?? 0;
    loadBackupList();
    const hasTrash = Object.prototype.hasOwnProperty.call(s, "trash_days");
    $("cfgTrashDaysWrap").classList.toggle("hidden", !hasTrash);
    if (hasTrash) $("cfgTrashDays").value = s.trash_days;
    $("cfgTrashEnabledWrap").classList.toggle("hidden", !Object.prototype.hasOwnProperty.call(s, "trash_enabled"));
    paintTrashToggle();
    $("cfgTheme").value = getTheme();
    if (!loadSettings.opApplied) {
      setMoverOp(s.mover_operation || "copiar");
      loadSettings.opApplied = true;
    }
    updateFavButton();
    if (state.dstLocalData) renderBookmarksBar($("dstBookmarks"), state.dstLocalData.bookmarks || [], true, (p) => loadDstDir(p));
  } catch (err) {
    setSaveState("error", `⚠️ No se pudo cargar la configuración: ${err.message}`);
    throw err;
  }
}

function collectSettings() {
  const payload = {
    mover_origin: $("cfgMoverOrigin").value.trim(),
    mover_dest: $("cfgMoverDest").value.trim(),
    mover_operation: $("cfgMoverOp").value,
    mover_conflict: $("cfgMoverConflict").value,
    mover_verify: $("cfgMoverVerify").value,
    mover_limit_kbps: mbToKbps($("cfgMoverLimitMb").value),
    mover_retries: Number($("cfgMoverRetries").value || 0),
    mover_retry_wait: Number($("cfgMoverRetryWait").value || 10),
    mover_keep_structure: $("cfgMoverStruct").checked,
    mover_favorites: state.favorites,
    ftp_host: $("cfgFtpHost").value.trim(),
    ftp_port: Number($("cfgFtpPort").value || 5000),
    ftp_user: $("cfgFtpUser").value.trim(),
    ftp_local_root: $("cfgFtpLocalRoot").value.trim(),
    ftp_remote_dir: $("cfgFtpRemoteDir").value.trim() || "/",
    ftp_limit_kbps: mbToKbps($("cfgFtpLimitMb").value),
    ftp_retries: Number($("cfgFtpRetries").value || 0),
    ftp_retry_wait: Number($("cfgFtpRetryWait").value || 10),
    ftp_keep_structure: $("cfgFtpStruct").checked,
    ftp_passive: $("cfgFtpPassive").checked,
    ftp_tls: $("cfgFtpTls").checked,
    ftp_verify_size: $("cfgFtpVerifySize").checked,
  };
  if (!$("backupCard").classList.contains("hidden")) {
    Object.assign(payload, {
      ftp_backup_dir: $("cfgBackupDir").value.trim(),
      ftp_backup_remote_root: $("cfgBackupRemoteRoot").value.trim() || "/",
      ftp_backup_exclude: $("cfgBackupExclude").value,
      ftp_backup_exclude_ext: $("cfgBackupExcludeExt").value,
      ftp_backup_incremental: $("cfgBackupIncremental").checked,
      ftp_backup_compress: $("cfgBackupCompress").checked,
      ftp_backup_keep: Number($("cfgBackupKeep").value || 0),
    });
  }
  if (!$("cfgTrashDaysWrap").classList.contains("hidden")) payload.trash_days = Number($("cfgTrashDays").value || 7);
  if ($("cfgFtpPassword").value !== "") payload.ftp_password = $("cfgFtpPassword").value;
  return payload;
}

let saveSeq = 0;
async function saveSettings(patch = null) {
  const seq = ++saveSeq;
  setSaveState("saving", "Guardando…");
  try {
    const payload = patch || collectSettings();
    const saved = await api("/api/settings", { method: "PUT", body: JSON.stringify(payload) });
    state.settings = saved;
    if (Array.isArray(saved.mover_favorites)) state.favorites = [...saved.mover_favorites];
    if (payload.ftp_password !== undefined) {
      $("cfgFtpPassword").value = "";
      $("cfgFtpPwHint").textContent = saved.ftp_has_password ? "(guardada)" : "(vacía)";
    }
    if (seq === saveSeq) setSaveState("saved", `✓ Guardado a las ${new Date().toLocaleTimeString("es-ES", { hour: "2-digit", minute: "2-digit" })}`);
    return true;
  } catch (err) {
    if (seq === saveSeq) setSaveState("error", `⚠️ No se guardó: ${err.message}`);
    return false;
  }
}

const autoSave = debounce(() => saveSettings(), 700);

$("settingsForm").addEventListener("submit", (e) => e.preventDefault());
$("settingsForm").addEventListener("input", (e) => {
  if (e.target.id === "cfgFtpPassword" || e.target.id === "cfgTheme") return;
  if (e.target.matches("input[type=text], input[type=number], textarea")) {
    setSaveState("saving", "Cambios sin guardar…");
    autoSave();
  }
});
$("settingsForm").addEventListener("change", (e) => {
  if (e.target.id === "cfgTheme") return;
  if (e.target.id === "cfgTrashEnabled") {
    autoSave.cancel();
    setTrashEnabled(e.target.checked);
    return;
  }
  if (e.target.id === "cfgFtpPassword" && !e.target.value) return;
  autoSave.cancel();
  saveSettings();
});

async function saveFavorites(next) {
  state.favorites = next;
  renderFavorites();
  updateFavButton();
  if (state.dstLocalData) renderBookmarksBar($("dstBookmarks"), state.dstLocalData.bookmarks || [], true, (p) => loadDstDir(p));
  const ok = await saveSettings(state.settings ? { ...collectSettingsFromState(), mover_favorites: next } : { mover_favorites: next });
  if (!ok) showToast("No se pudieron guardar los favoritos", { type: "err" });
}

// Al guardar favoritos desde el explorador no se ha cargado el formulario: usar los ajustes en memoria
function collectSettingsFromState() {
  if ($("cfgMoverOrigin").value || $("cfgFtpPort").value) return collectSettings();
  const s = { ...(state.settings || {}) };
  delete s.ftp_has_password;
  delete s.ftp_password;
  return s;
}

$("favList").addEventListener("click", (e) => {
  const b = e.target.closest("[data-fav-del]");
  if (!b) return;
  const i = Number(b.dataset.favDel);
  saveFavorites(state.favorites.filter((_, idx) => idx !== i));
});
$("btnFavAdd").onclick = () => {
  openPicker(null, state.dstLocalPath || (state.settings && state.settings.mover_dest) || "", (p) => {
    if (state.favorites.includes(p)) return showToast("Esa carpeta ya está en favoritos");
    saveFavorites([...state.favorites, p]);
  }, "Añadir carpeta favorita");
};

// =========================================================================
// BACKUP DE LA MICROSD
// =========================================================================
function renderBackup(b) {
  const prev = state.backup;
  state.backup = b;
  const running = !!b.running;
  $("btnBackupStart").classList.toggle("hidden", running);
  $("btnBackupSimulate").classList.toggle("hidden", running);
  $("btnBackupStop").classList.toggle("hidden", !running);
  $("btnBackupStop").disabled = !!b.stopping;
  const box = $("backupStatus");
  const p = running ? b.progress : b.last;
  if (!p) {
    box.classList.add("hidden");
    return;
  }
  box.classList.remove("hidden");
  const total = Number(p.bytes_total || 0);
  const done = Number(p.bytes_done || 0);
  let text;
  let extra = "";
  const sim = p.simulate || p.status === "SIM";
  $("backupSim").classList.toggle("hidden", !(!running && p.status === "SIM"));
  if (!running && p.status === "SIM") {
    text = "🔍 Simulación: esto es lo que se copiaría";
    extra = `${p.files_total || 0} fichero(s) · ${fmtBytes(total)}`;
    $("backupProgBar").classList.add("hidden");
    renderBackupSim(p);
  } else if (running && p.phase === "compressing") {
    text = b.stopping ? "Deteniendo…" : "Comprimiendo…";
    $("backupProgBar").classList.remove("hidden");
    setProgressBar($("backupProgBar"), $("backupProgFill"), 0, true);
  } else if (running && p.phase === "scanning") {
    $("backupProgBar").classList.remove("hidden");
    text = b.stopping ? "Deteniendo…" : (sim ? "Simulando: analizando la SD…" : "Analizando la SD…");
    extra = `${p.files_total || 0} fichero(s) · ${fmtBytes(total)}`;
    setProgressBar($("backupProgBar"), $("backupProgFill"), 0, true);
  } else {
    $("backupProgBar").classList.remove("hidden");
    const pct = total ? (done * 100) / total : (p.phase === "done" ? 100 : 0);
    if (running) text = b.stopping ? "Deteniendo…" : `Copiando ${pct.toFixed(1)} %`;
    else text = { OK: "✓ Último backup completado", ERR: "⚠ Último backup con errores", STOP: "⏹ Último backup detenido" }[p.status] || "Último backup";
    extra = `${p.files_done || 0}/${p.files_total || 0} fichero(s) · ${fmtBytes(done)} / ${fmtBytes(total)}`;
    if (!running && p.finished_at) extra += ` · ${fmtDate(p.finished_at)}`;
    setProgressBar($("backupProgBar"), $("backupProgFill"), pct, false);
  }
  $("backupStatusText").textContent = text;
  $("backupStatusExtra").textContent = extra;
  const lines = [];
  if (running && p.current) lines.push(p.current);
  if (p.target && !sim) lines.push(`→ ${p.target}`);
  if (!running && !sim) {
    const exc = p.excluded_dirs || [];
    if (exc.length) lines.push(`Excluidas: ${exc.slice(0, 6).join(", ")}${exc.length > 6 ? "…" : ""}`);
    if (p.files_skipped) lines.push(`Sin cambios: ${p.files_skipped} fichero(s)`);
    if (p.pruned && p.pruned.length) lines.push(`Backups antiguos borrados: ${p.pruned.join(", ")}`);
  }
  if (!running && p.error) lines.push(`Motivo: ${p.error}`);
  $("backupCurrent").textContent = [...new Set(lines)].join("\n");
  if (prev && prev.running && !running && b.last) {
    const st = b.last.status;
    if (st !== "SIM") loadBackupList();
    if (st === "SIM") return;
    showToast(st === "OK" ? "Backup de la microSD completado" : (st === "STOP" ? "Backup de la microSD detenido" : `Backup con errores: ${b.last.error || ""}`), { type: st === "ERR" ? "err" : undefined });
  }
}

function renderBackupSim(p) {
  const rows = (list) => list.map((g) => `<tr><td>${escapeHtml(g.path)}</td><td class="num">${g.files}</td><td class="num">${fmtBytes(g.bytes)}</td></tr>`).join("");
  const tops = p.top_dirs || [];
  const excl = [...(p.excluded_detail || [])].sort((a, b) => b.bytes - a.bytes);
  const exclTotal = excl.reduce((acc, e) => acc + Number(e.bytes || 0), 0) + Number(p.excluded_bytes || 0);
  let html = `<div><h4>Se copia · ${fmtBytes(p.bytes_total || 0)}</h4><table>${rows(tops) || '<tr><td colspan="3">Nada</td></tr>'}</table></div>`;
  html += `<div><h4>Se excluye · ${fmtBytes(exclTotal)}</h4><table>${rows(excl)}`;
  if (p.excluded_files) html += `<tr><td>Ficheros de juego sueltos</td><td class="num">${p.excluded_files}</td><td class="num">${fmtBytes(p.excluded_bytes || 0)}</td></tr>`;
  if (!excl.length && !p.excluded_files) html += `<tr><td colspan="3">Nada</td></tr>`;
  html += `</table></div>`;
  $("backupSim").innerHTML = html;
}

async function launchBackup(simulate) {
  autoSave.cancel();
  if (!(await saveSettings())) return;
  if (!$("cfgBackupDir").value.trim()) {
    showToast("Elige primero la carpeta de backups del servidor", { type: "err" });
    $("cfgBackupDir").focus();
    return;
  }
  try {
    const res = await api("/api/ftp/backup/start", { method: "POST", body: JSON.stringify({ simulate }) });
    renderBackup(res);
    showToast(simulate ? "Simulando el backup (no se descarga nada)…" : "Backup de la microSD iniciado");
  } catch (err) {
    showToast(err.message, { type: "err" });
  }
}

$("btnBackupStart").onclick = () => launchBackup(false);
$("btnBackupSimulate").onclick = () => launchBackup(true);

async function loadBackupList() {
  const ul = $("backupList");
  try {
    const data = await api("/api/ftp/backup/list");
    if (!data.path) {
      ul.innerHTML = `<li class="muted">Configura la carpeta de backups para verlos aquí.</li>`;
      return;
    }
    if (!data.items.length) {
      ul.innerHTML = `<li class="muted">Aún no hay backups en ${escapeHtml(data.path)}.</li>`;
      return;
    }
    ul.innerHTML = data.items.map((b) => {
      const kind = b.kind === "tar" ? "tar.gz" : (b.incremental ? "incremental" : "carpeta");
      const files = b.files != null ? ` · ${b.files} fichero(s)` : "";
      return `<li><div class="bk-info"><span class="bk-name">${escapeHtml(b.name)}</span>
        <span class="muted">${escapeHtml(fmtDate(b.mtime))} · ${fmtBytes(b.size)}${files} · ${kind}</span></div>
        <button type="button" class="btn btn-sm" data-restore="${escapeHtml(b.path)}" data-name="${escapeHtml(b.name)}">↩ Restaurar</button></li>`;
    }).join("");
  } catch (err) {
    if (isMissing(err)) $("backupList").closest(".card").querySelector(".backup-list-head").classList.add("hidden");
    else ul.innerHTML = `<li class="muted">${escapeHtml(err.message)}</li>`;
  }
}

$("btnBackupListRefresh").onclick = () => loadBackupList();
$("backupList").addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-restore]");
  if (!btn) return;
  const tar = btn.dataset.restore.endsWith(".tar.gz");
  const ok = await uiConfirm({
    title: "Restaurar en la microSD",
    html: `<p>Se subirá <strong>${escapeHtml(btn.dataset.name)}</strong> a la SD de la Switch por la <strong>cola FTP</strong>, cada fichero a su sitio original.</p>
      <p>Los ficheros que ya estén en la SD con el mismo tamaño se omiten; los demás se sobrescriben.${tar ? " Antes se descomprime el .tar.gz junto al original." : ""}</p>
      <p class="muted">Usa DBI en modo «explorar SD» (o ftpd) y no dejes la consola en un juego mientras se sube.</p>`,
    okText: "Restaurar y subir",
  });
  if (!ok) return;
  btn.disabled = true;
  try {
    const res = await api("/api/ftp/backup/restore", { method: "POST", body: JSON.stringify({ path: btn.dataset.restore, start: true }) });
    showToast(res.added ? `Subiendo ${res.added} archivo(s) a la SD (${fmtBytes(res.bytes)}). Míralo en Colas → FTP` : "No había nada nuevo que subir (ya estaba en la cola)");
    if (tar) loadBackupList();
    refreshStatus();
  } catch (err) {
    showToast(err.message, { type: "err" });
  } finally {
    btn.disabled = false;
  }
});

$("btnBackupExcludeDefaults").onclick = async () => {
  const s = state.settings || {};
  if (!s.ftp_backup_exclude_default) return;
  $("cfgBackupExclude").value = s.ftp_backup_exclude_default.join("\n");
  $("cfgBackupExcludeExt").value = (s.ftp_backup_exclude_ext_default || []).join(", ");
  autoSave.cancel();
  if (await saveSettings()) showToast("Exclusiones restauradas a los valores por defecto");
};

$("btnBackupStop").onclick = async () => {
  try {
    renderBackup(await api("/api/ftp/backup/stop", { method: "POST" }));
  } catch (err) {
    showToast(err.message, { type: "err" });
  }
};

$("btnTestFtp").onclick = async () => {
  const b = $("ftpTestBanner");
  b.className = "banner";
  b.innerHTML = `<span class="spinner" aria-hidden="true"></span>Probando conexión FTP…`;
  b.classList.remove("hidden");
  try {
    const res = await api("/api/ftp/test", { method: "POST" });
    b.className = `banner ${res.ok ? "ok" : "err"}`;
    b.textContent = res.message;
    if (res.ok) fetchFtpSpace(true);
  } catch (err) {
    b.className = "banner err";
    b.textContent = err.message;
  }
};

$("btnScanSwitch").onclick = async () => {
  const box = $("switchScanResults");
  box.classList.remove("hidden");
  box.innerHTML = `<span class="spinner" aria-hidden="true"></span>Buscando la Switch en la red local (puertos 5000, 2121 y 21)…`;
  try {
    const res = await api("/api/ftp/scan-switch", { method: "POST", body: JSON.stringify({}) });
    if (!res.found || !res.found.length) {
      box.innerHTML = `No se encontró ningún servidor FTP en ${escapeHtml((res.prefixes || []).join(", "))} (puertos ${escapeHtml((res.ports || []).join(", "))}). Comprueba que la Switch tiene el FTP activo (DBI / ftpd).`;
      return;
    }
    box.innerHTML = res.found.map((hit) => `
      <div class="scan-hit">
        <div><strong>${escapeHtml(hit.ip)}:${escapeHtml(String(hit.port))}</strong> <span class="muted">${hit.banner ? escapeHtml(hit.banner) : "puerto abierto"}</span></div>
        <button type="button" class="btn btn-sm btn-primary" data-use-hit="${escapeHtml(hit.ip)}:${escapeHtml(String(hit.port))}">Usar esta IP</button>
      </div>`).join("");
    box.querySelectorAll("[data-use-hit]").forEach((btn) => {
      btn.onclick = () => {
        const [ip, port] = btn.dataset.useHit.split(":");
        $("cfgFtpHost").value = ip;
        $("cfgFtpPort").value = port;
        saveSettings();
        showToast(`Usando ${ip}:${port}`);
      };
    });
  } catch (err) {
    box.textContent = err.message;
  }
};

// =========================================================================
// REGISTRO
// =========================================================================
async function loadLogs() {
  const box = $("logBox");
  try {
    const data = await api(`/api/logs?kind=${$("logFilterKind").value}&limit=300`);
    box.innerHTML = (data.logs || []).map((l) => {
      const dt = toDate(l.ts);
      return `<div class="log-line log-${escapeHtml(l.level)}">[${escapeHtml(dt ? dt.toLocaleString("es-ES") : "-")}] [${escapeHtml(String(l.kind).toUpperCase())}] ${escapeHtml(l.message)}</div>`;
    }).join("") || `<div class="empty-state"><span class="es-icon">📜</span><span class="es-title">Sin registros</span></div>`;
  } catch (err) {
    box.innerHTML = `<div class="empty-state is-error" role="alert"><span class="es-title">No se pudo cargar el registro</span><span>${escapeHtml(err.message)}</span></div>`;
  }
}
$("logFilterKind").onchange = loadLogs;
$("btnRefreshLogs").onclick = loadLogs;
$("btnClearLogs").onclick = async () => {
  const ok = await uiConfirm({ title: "Vaciar registro", html: "<p>Se borrarán todos los eventos del registro de actividad.</p>", okText: "Vaciar registro", danger: true });
  if (!ok) return;
  try {
    await api("/api/logs", { method: "DELETE" });
  } catch (err) {
    showToast(err.message, { type: "err" });
  }
  loadLogs();
};

// =========================================================================
// DOCUMENTACIÓN / README
// =========================================================================
let readmeLoaded = false;

function slugifyHeading(text) {
  return text
    .toLowerCase()
    .normalize("NFD")
    .replace(/[̀-ͯ]/g, "")
    .replace(/[^a-z0-9\s-]/g, "")
    .replace(/\s/g, "-");
}

function formatInlineMarkdown(raw) {
  let s = escapeHtml(raw);
  const codeSpans = [];
  s = s.replace(/`([^`]+)`/g, (_m, code) => {
    const idx = codeSpans.length;
    codeSpans.push(`<code>${code}</code>`);
    return `\u0000CODE${idx}\u0000`;
  });
  s = s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (_m, label, href) => {
    const cleanHref = href.replace(/&amp;/g, "&");
    if (cleanHref.startsWith("#")) {
      return `<a href="${escapeHtml(cleanHref)}" data-readme-anchor="${escapeHtml(cleanHref.slice(1))}">${label}</a>`;
    }
    if (/^https?:\/\//i.test(cleanHref)) {
      return `<a href="${escapeHtml(cleanHref)}" target="_blank" rel="noopener noreferrer">${label}</a>`;
    }
    return label;
  });
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/\*([^*]+)\*/g, "<em>$1</em>");
  s = s.replace(/\u0000CODE(\d+)\u0000/g, (_m, idx) => codeSpans[Number(idx)]);
  return s;
}

function renderMarkdown(md) {
  const lines = String(md || "").replace(/\r\n/g, "\n").split("\n");
  const out = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    const fence = line.match(/^```(\S*)\s*$/);
    if (fence) {
      i++;
      const codeLines = [];
      while (i < lines.length && !/^```\s*$/.test(lines[i])) {
        codeLines.push(lines[i]);
        i++;
      }
      i++;
      out.push(`<div class="readme-code-wrap"><button type="button" class="copy-code-btn">Copiar</button><pre><code>${escapeHtml(codeLines.join("\n"))}</code></pre></div>`);
      continue;
    }

    if (/^---+\s*$/.test(line)) {
      out.push("<hr>");
      i++;
      continue;
    }

    const heading = line.match(/^(#{1,4})\s+(.+)$/);
    if (heading) {
      const level = heading[1].length;
      const rawTitle = heading[2].trim();
      out.push(`<h${level} id="readme-${escapeHtml(slugifyHeading(rawTitle))}">${formatInlineMarkdown(rawTitle)}</h${level}>`);
      i++;
      continue;
    }

    if (line.trim().startsWith("|") && i + 1 < lines.length && /^\s*\|?[\s:-]+\|/.test(lines[i + 1])) {
      const parseRow = (r) => r.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
      const headers = parseRow(line);
      i += 2;
      const bodyRows = [];
      while (i < lines.length && lines[i].trim().startsWith("|")) {
        bodyRows.push(parseRow(lines[i]));
        i++;
      }
      const ths = headers.map((h) => `<th>${formatInlineMarkdown(h)}</th>`).join("");
      const trs = bodyRows.map((row) => `<tr>${row.map((c) => `<td>${formatInlineMarkdown(c)}</td>`).join("")}</tr>`).join("");
      out.push(`<div class="readme-table-wrap"><table><thead><tr>${ths}</tr></thead><tbody>${trs}</tbody></table></div>`);
      continue;
    }

    if (line.trimStart().startsWith(">")) {
      const qLines = [];
      while (i < lines.length && lines[i].trimStart().startsWith(">")) {
        qLines.push(lines[i].replace(/^\s*>\s?/, ""));
        i++;
      }
      out.push(`<blockquote>${qLines.map((q) => formatInlineMarkdown(q)).join("<br>")}</blockquote>`);
      continue;
    }

    if (/^\s*[-*]\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) {
        items.push(`<li>${formatInlineMarkdown(lines[i].replace(/^\s*[-*]\s+/, ""))}</li>`);
        i++;
      }
      out.push(`<ul>${items.join("")}</ul>`);
      continue;
    }

    if (/^\s*\d+\.\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) {
        items.push(`<li>${formatInlineMarkdown(lines[i].replace(/^\s*\d+\.\s+/, ""))}</li>`);
        i++;
      }
      out.push(`<ol>${items.join("")}</ol>`);
      continue;
    }

    if (!line.trim()) {
      i++;
      continue;
    }

    const para = [];
    while (
      i < lines.length &&
      lines[i].trim() &&
      !/^(#{1,4}\s+|```|---+\s*$|>\s?|\s*[-*]\s+|\s*\d+\.\s+)/.test(lines[i]) &&
      !(lines[i].trim().startsWith("|") && i + 1 < lines.length && /^\s*\|?[\s:-]+\|/.test(lines[i + 1]))
    ) {
      para.push(formatInlineMarkdown(lines[i].trim()));
      i++;
    }
    out.push(`<p>${para.join(" ")}</p>`);
  }

  return out.join("\n");
}

async function loadReadme(force = false) {
  if (readmeLoaded && !force) return;
  const box = $("readmeBody");
  box.innerHTML = `<p class="muted"><span class="spinner" aria-hidden="true"></span>Cargando documentación…</p>`;
  try {
    const data = await api("/api/readme");
    box.innerHTML = renderMarkdown(data.content || "");
    readmeLoaded = true;
  } catch (err) {
    box.innerHTML = `<p class="sub-err">${escapeHtml(err.message)}</p>`;
  }
}

$("readmeBody").addEventListener("click", async (e) => {
  const copyBtn = e.target.closest(".copy-code-btn");
  if (copyBtn) {
    const codeEl = copyBtn.parentElement.querySelector("code");
    if (codeEl) {
      await copyText(codeEl.textContent || "");
      copyBtn.textContent = "✓ Copiado";
      setTimeout(() => { copyBtn.textContent = "Copiar"; }, 1500);
    }
    return;
  }
  const anchor = e.target.closest("[data-readme-anchor]");
  if (anchor) {
    e.preventDefault();
    const rawId = anchor.dataset.readmeAnchor;
    const clean = rawId.toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, "");
    const target = document.getElementById(`readme-${clean}`) || document.getElementById(`readme-${rawId}`);
    if (target) target.scrollIntoView({ behavior: "smooth", block: "start" });
  }
});

$("btnRefreshReadme").onclick = () => loadReadme(true);
$("btnReadmeTop").onclick = () => window.scrollTo({ top: 0, behavior: "smooth" });

// =========================================================================
// SELECTOR DE CARPETA (MODAL)
// =========================================================================
document.querySelectorAll("[data-browse-input]").forEach((btn) => {
  btn.onclick = () => {
    const inputId = btn.dataset.browseInput;
    openPicker(inputId, $(inputId).value || "");
  };
});

const pickerPathbar = setupPathbar({
  crumbs: $("pickerBreadcrumbs"),
  input: $("pickerPathInput"),
  editBtn: $("btnPickerEditPath"),
  getPath: () => state.picker.path,
  onNavigate: (p) => loadPickerDir(p),
});

async function openPicker(targetInputId, initialPath, onPick = null, title = "Elegir carpeta del servidor") {
  state.picker.targetInputId = targetInputId;
  state.picker.onPick = onPick;
  $("pickerTitle").textContent = title;
  openModal($("pickerModal"), { initialFocus: $("btnPickerUse") });
  await loadPickerDir(initialPath);
}

async function loadPickerDir(path) {
  $("pickerList").innerHTML = `<div class="empty-state"><span class="spinner" aria-hidden="true"></span>Cargando…</div>`;
  try {
    const qs = new URLSearchParams({ kind: "mover" });
    if (path) qs.set("path", path);
    const data = await api(`/api/fs/list?${qs}`);
    state.picker.path = data.path;
    state.picker.parent = data.parent;
    $("btnPickerUp").disabled = !data.parent;
    renderBookmarksBar($("pickerShortcuts"), data.bookmarks || [], true, (p) => loadPickerDir(p));
    renderBreadcrumbs($("pickerBreadcrumbs"), data.path, data.data_root, (p) => loadPickerDir(p));
    pickerPathbar.leave();
    $("pickerList").innerHTML = (data.dirs || []).map((d) =>
      `<button type="button" class="picker-row" data-pkopen="${escapeHtml(d.path)}">📁 ${escapeHtml(d.name)}</button>`
    ).join("") || `<div class="empty-state"><span class="es-title">Sin subcarpetas</span><span>Puedes usar esta carpeta o crear una nueva.</span></div>`;
  } catch (err) {
    $("pickerList").innerHTML = `<div class="empty-state is-error" role="alert"><span class="es-title">No se pudo abrir</span><span>${escapeHtml(err.message)}</span></div>`;
  }
}

$("pickerList").addEventListener("click", (e) => {
  const r = e.target.closest("[data-pkopen]");
  if (r) loadPickerDir(r.dataset.pkopen);
});
$("btnPickerClose").onclick = () => closeModal($("pickerModal"));
$("btnPickerCancel").onclick = () => closeModal($("pickerModal"));
$("btnPickerUp").onclick = () => { if (state.picker.parent) loadPickerDir(state.picker.parent); };
$("btnPickerNewDir").onclick = () => makeDir(state.picker.path, (res) => loadPickerDir(res.path), relPath(state.picker.path));
$("btnPickerUse").onclick = () => {
  const picked = state.picker.path;
  const onPick = state.picker.onPick;
  const inputId = state.picker.targetInputId;
  state.picker.onPick = null;
  closeModal($("pickerModal"));
  if (onPick) onPick(picked);
  else if (inputId) {
    $(inputId).value = picked;
    $(inputId).dispatchEvent(new Event("change", { bubbles: true }));
  }
};

// =========================================================================
// INICIO
// =========================================================================
(async function init() {
  applyTheme(getTheme());
  updateExploreActionLabels();
  updateSortHeaders();
  switchQueueTab("mover", false);
  try {
    await loadSettings();
  } catch (err) {
    showToast(err.message, { type: "err" });
  }
  await loadSrcDir("");
  await loadDstDir();
  await refreshStatus();
  api("/api/trash").then(() => { state.trashAvailable = true; }).catch((err) => { if (isMissing(err)) state.trashAvailable = false; });
  setInterval(refreshStatus, 1500);
})();

$("btnTrashToggle").addEventListener("click", () => setTrashEnabled(!trashEnabled()));
