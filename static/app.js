const state = {
  activeTab: "explore",
  exploreMode: "mover", // 'mover' | 'ftp'
  queueTab: "mover",
  currentPath: "",
  parentPath: null,
  selectedPaths: new Set(),
  fsData: null,
  settings: null,
  picker: { mode: "local", path: "/" },
};

const $ = (id) => document.getElementById(id);

function fmtBytes(bytes) {
  if (bytes == null || isNaN(bytes)) return "-";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let val = Number(bytes);
  let i = 0;
  while (val >= 1024 && i < units.length - 1) {
    val /= 1024;
    i++;
  }
  return `${val.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

function showToast(msg) {
  const el = $("toast");
  el.textContent = msg;
  el.classList.remove("hidden");
  clearTimeout(showToast._timer);
  showToast._timer = setTimeout(() => el.classList.add("hidden"), 3200);
}

async function api(url, opts = {}) {
  const res = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(data.detail || data.error || `Error ${res.status}`);
  }
  return data;
}

function escapeHtml(str) {
  return String(str ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

// --- TABS ---
$("mainTabs").addEventListener("click", (e) => {
  const btn = e.target.closest(".tab");
  if (!btn) return;
  const tab = btn.dataset.tab;
  state.activeTab = tab;
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === tab));
  document.querySelectorAll(".tab-panel").forEach((p) => p.classList.toggle("active", p.id === `tab-${tab}`));
  if (tab === "queue") loadQueue();
  if (tab === "config") loadSettings();
  if (tab === "logs") loadLogs();
});

// --- STATUS POLLING ---
function renderWorkerCard(kind, info) {
  const prefix = kind === "mover" ? "mover" : "ftp";
  const stEl = $(`${prefix}State`);
  const btnStart = $(kind === "mover" ? "btnStartMover" : "btnStartFtp");
  const btnStop = $(kind === "mover" ? "btnStopMover" : "btnStopFtp");
  const metaEl = $(`${prefix}Meta`);
  const progWrap = $(`${prefix}ProgWrap`);

  if (info.stopping) {
    stEl.textContent = "Deteniendo...";
    stEl.className = "status-pill stopping";
  } else if (info.running) {
    stEl.textContent = "En ejecución";
    stEl.className = "status-pill running";
  } else {
    stEl.textContent = "Detenido";
    stEl.className = "status-pill";
  }

  btnStart.classList.toggle("hidden", info.running);
  btnStop.classList.toggle("hidden", !info.running);

  const c = info.counts || {};
  const pend = (c.PEND || 0) + (c.REAN || 0) + (c.PROC || 0);
  metaEl.textContent = `Pendientes: ${pend} (${fmtBytes(info.pending_bytes)}) · Pausados: ${c.PAUS || 0} · OK: ${c.OK || 0} · ERR: ${c.ERR || 0}`;

  if (info.running && info.progress && info.progress.filename) {
    progWrap.classList.remove("hidden");
    $(`${prefix}ProgFile`).textContent = info.progress.filename;
    const pct = Number(info.progress.percent || 0).toFixed(1);
    $(`${prefix}ProgStats`).textContent = `${pct}% · ${info.progress.speed || "--"} · ETA ${info.progress.eta || "--"}`;
    $(`${prefix}ProgFill`).style.width = `${Math.min(100, Math.max(0, pct))}%`;
  } else {
    progWrap.classList.add("hidden");
  }
}

async function refreshStatus() {
  try {
    const data = await api("/api/status");
    const w = data.workers;
    renderWorkerCard("mover", w.mover);
    renderWorkerCard("ftp", w.ftp);
    const totalActive =
      (w.mover.counts.PEND || 0) + (w.mover.counts.REAN || 0) + (w.mover.counts.PROC || 0) +
      (w.ftp.counts.PEND || 0) + (w.ftp.counts.REAN || 0) + (w.ftp.counts.PROC || 0);
    $("tabQueueBadge").textContent = totalActive;
    if (state.activeTab === "queue") loadQueue();
  } catch {
    // Silenciar errores transitorios de polling
  }
}

$("btnStartMover").onclick = async () => { await api("/api/queue/mover/start", { method: "POST" }); refreshStatus(); };
$("btnStopMover").onclick = async () => { await api("/api/queue/mover/stop", { method: "POST" }); refreshStatus(); };
$("btnStartFtp").onclick = async () => { await api("/api/queue/ftp/start", { method: "POST" }); refreshStatus(); };
$("btnStopFtp").onclick = async () => { await api("/api/queue/ftp/stop", { method: "POST" }); refreshStatus(); };

// --- EXPLORADOR ---
function setExploreMode(mode) {
  state.exploreMode = mode;
  $("modeBtnMover").classList.toggle("active", mode === "mover");
  $("modeBtnFtp").classList.toggle("active", mode === "ftp");
  $("exploreMoverOpts").classList.toggle("hidden", mode !== "mover");
  $("exploreFtpOpts").classList.toggle("hidden", mode !== "ftp");
  $("btnAddSwitchOnly").classList.toggle("hidden", mode !== "ftp");
  state.selectedPaths.clear();
  loadDir("");
}

$("modeBtnMover").onclick = () => setExploreMode("mover");
$("modeBtnFtp").onclick = () => setExploreMode("ftp");
$("btnResetLocalDest").onclick = () => { $("exploreMoverDest").value = ""; };
$("btnResetRemoteDest").onclick = () => { $("exploreFtpDest").value = ""; };

async function loadDir(path) {
  try {
    const hidden = $("chkShowHidden").checked ? "true" : "false";
    const qs = new URLSearchParams({ kind: state.exploreMode, show_hidden: hidden });
    if (path) qs.set("path", path);
    const data = await api(`/api/fs/list?${qs}`);
    state.fsData = data;
    state.currentPath = data.path;
    state.parentPath = data.parent;
    state.selectedPaths.clear();
    $("currentPathInput").value = data.path;
    $("btnDirUp").disabled = !data.parent;
    $("fsDiskInfo").textContent = data.disk ? `Libre: ${fmtBytes(data.disk.free_bytes)}` : "";
    renderBookmarks(data.bookmarks || []);
    renderFsTable();
  } catch (err) {
    showToast(err.message);
  }
}

function renderBookmarks(bookmarks) {
  const bar = $("bookmarksBar");
  const favs = (state.settings && state.settings.mover_favorites) || [];
  const all = [
    ...bookmarks,
    ...(state.exploreMode === "mover" ? favs.map((p) => ({ label: `★ ${p.split("/").pop() || p}`, path: p })) : []),
  ];
  bar.innerHTML = all
    .map((b) => `<button class="btn btn-sm btn-ghost" data-bpath="${escapeHtml(b.path)}">${escapeHtml(b.label)}</button>`)
    .join("");
  bar.querySelectorAll("button").forEach((btn) => {
    btn.onclick = () => loadDir(btn.dataset.bpath);
  });
}

function renderFsTable() {
  const data = state.fsData;
  if (!data) return;
  const filter = $("fsFilter").value.trim().toLowerCase();
  const dirs = data.dirs.filter((d) => !filter || d.name.toLowerCase().includes(filter));
  const files = data.files.filter((f) => !filter || f.name.toLowerCase().includes(filter));

  const rows = [];
  for (const d of dirs) {
    const checked = state.selectedPaths.has(d.path) ? "checked" : "";
    rows.push(`
      <tr>
        <td><input type="checkbox" class="fs-chk" data-path="${escapeHtml(d.path)}" ${checked}></td>
        <td><a class="dir-link" data-open="${escapeHtml(d.path)}">📁 ${escapeHtml(d.name)}</a></td>
        <td><span class="tag">DIR</span></td>
        <td>-</td>
        <td><button class="btn btn-sm" data-add-dir="${escapeHtml(d.path)}">+ Recursivo</button></td>
      </tr>
    `);
  }
  for (const f of files) {
    const checked = state.selectedPaths.has(f.path) ? "checked" : "";
    const swBadge = f.switch_type ? `<span class="tag tag-${f.switch_type.toLowerCase()}">${f.switch_type}</span> ` : "";
    const stBadge = f.queue_status ? `<span class="tag tag-${f.queue_status.toLowerCase()}">${f.queue_status}</span>` : "";
    rows.push(`
      <tr>
        <td><input type="checkbox" class="fs-chk" data-path="${escapeHtml(f.path)}" ${checked}></td>
        <td>${escapeHtml(f.name)}</td>
        <td>${swBadge}${stBadge}</td>
        <td>${fmtBytes(f.size_bytes)}</td>
        <td><button class="btn btn-sm btn-primary" data-add-file="${escapeHtml(f.path)}">+ Añadir</button></td>
      </tr>
    `);
  }
  if (!rows.length) {
    rows.push(`<tr><td colspan="5" class="muted">Carpeta vacía o sin coincidencias.</td></tr>`);
  }
  $("fsTableBody").innerHTML = rows.join("");
  updateSelectionSummary();
}

function updateSelectionSummary() {
  $("selectionSummary").textContent = `${state.selectedPaths.size} elemento(s) seleccionado(s)`;
}

$("fsTableBody").addEventListener("click", async (e) => {
  const openEl = e.target.closest("[data-open]");
  if (openEl) return loadDir(openEl.dataset.open);

  const addFileEl = e.target.closest("[data-add-file]");
  if (addFileEl) return enqueuePaths([addFileEl.dataset.addFile], false, false);

  const addDirEl = e.target.closest("[data-add-dir]");
  if (addDirEl) return enqueuePaths([addDirEl.dataset.addDir], true, false);
});

$("fsTableBody").addEventListener("change", (e) => {
  const chk = e.target.closest(".fs-chk");
  if (!chk) return;
  if (chk.checked) state.selectedPaths.add(chk.dataset.path);
  else state.selectedPaths.delete(chk.dataset.path);
  updateSelectionSummary();
});

$("chkHeaderAll").onchange = (e) => {
  document.querySelectorAll(".fs-chk").forEach((c) => {
    c.checked = e.target.checked;
    if (c.checked) state.selectedPaths.add(c.dataset.path);
    else state.selectedPaths.delete(c.dataset.path);
  });
  updateSelectionSummary();
};

$("btnSelectAll").onclick = () => {
  $("chkHeaderAll").checked = true;
  $("chkHeaderAll").dispatchEvent(new Event("change"));
};
$("btnSelectNone").onclick = () => {
  $("chkHeaderAll").checked = false;
  $("chkHeaderAll").dispatchEvent(new Event("change"));
};

$("btnDirUp").onclick = () => { if (state.parentPath) loadDir(state.parentPath); };
$("btnGoPath").onclick = () => loadDir($("currentPathInput").value);
$("currentPathInput").addEventListener("keydown", (e) => { if (e.key === "Enter") loadDir($("currentPathInput").value); });
$("fsFilter").addEventListener("input", renderFsTable);
$("chkShowHidden").onchange = () => loadDir(state.currentPath);

$("btnNewDir").onclick = async () => {
  const name = prompt("Nombre de la nueva carpeta:");
  if (!name) return;
  try {
    await api("/api/fs/mkdir", { method: "POST", body: JSON.stringify({ parent: state.currentPath, name }) });
    showToast("Carpeta creada");
    loadDir(state.currentPath);
  } catch (err) {
    showToast(err.message);
  }
};

async function enqueuePaths(paths, recursive, onlySwitch) {
  if (!paths.length) return showToast("Selecciona al menos un archivo o carpeta");
  const payload = {
    kind: state.exploreMode,
    paths,
    recursive,
    only_switch: onlySwitch,
    base_browse_dir: state.currentPath,
    operation: state.exploreMode === "mover" ? $("exploreMoverOp").value : null,
    dest_dir: state.exploreMode === "mover" ? $("exploreMoverDest").value.trim() : $("exploreFtpDest").value.trim(),
  };
  try {
    const res = await api("/api/queue/add", { method: "POST", body: JSON.stringify(payload) });
    showToast(`Añadidos: ${res.added}${res.skipped ? ` (omitidos por colisión: ${res.skipped})` : ""}`);
    loadDir(state.currentPath);
    refreshStatus();
  } catch (err) {
    showToast(err.message);
  }
}

$("btnAddSelected").onclick = () => enqueuePaths(Array.from(state.selectedPaths), true, false);
$("btnAddSwitchOnly").onclick = () => {
  const paths = state.selectedPaths.size
    ? Array.from(state.selectedPaths)
    : (state.fsData?.files || []).filter((f) => f.switch_type).map((f) => f.path);
  enqueuePaths(paths, true, true);
};

// --- COLAS ---
$("qTabMover").onclick = () => switchQueueTab("mover");
$("qTabFtp").onclick = () => switchQueueTab("ftp");

function switchQueueTab(kind) {
  state.queueTab = kind;
  $("qTabMover").classList.toggle("active", kind === "mover");
  $("qTabFtp").classList.toggle("active", kind === "ftp");
  loadQueue();
}

$("chkOnlyPending").onchange = () => loadQueue();

async function loadQueue() {
  const only = $("chkOnlyPending").checked ? "true" : "false";
  const data = await api(`/api/queue?kind=${state.queueTab}&only_pending=${only}`);
  const rows = (data.items || []).map((it, idx) => {
    const isProc = it.status === "PROC";
    const sw = it.switch_type ? `<span class="tag tag-${it.switch_type.toLowerCase()}">${it.switch_type}</span> ` : "";
    const opBtn = it.kind === "mover" && !isProc
      ? `<button class="btn btn-sm" data-qop="${it.id}" title="Alternar copiar/mover">⇄</button>`
      : "";
    return `
      <tr>
        <td>${idx + 1}</td>
        <td><span class="tag tag-${it.status.toLowerCase()}">${it.status}</span></td>
        <td>${sw}<span class="tag">${escapeHtml(it.operation.toUpperCase())}</span></td>
        <td class="path-cell">
          <div>${escapeHtml(it.source_path)}</div>
          <div class="muted">→ ${escapeHtml(it.dest_path)}</div>
          ${it.note ? `<span class="sub-note">${escapeHtml(it.note)}</span>` : ""}
          ${it.error ? `<span class="sub-err">${escapeHtml(it.error)}</span>` : ""}
        </td>
        <td>${fmtBytes(it.size_bytes)}</td>
        <td>
          <div class="inline-group">
            <button class="btn btn-sm" data-qmove="${it.id}:top" ${isProc ? "disabled" : ""} title="Al principio">⤒</button>
            <button class="btn btn-sm" data-qmove="${it.id}:up" ${isProc ? "disabled" : ""} title="Subir">↑</button>
            <button class="btn btn-sm" data-qmove="${it.id}:down" ${isProc ? "disabled" : ""} title="Bajar">↓</button>
            <button class="btn btn-sm" data-qpause="${it.id}" ${isProc ? "disabled" : ""} title="Pausar/Reanudar">⏯</button>
            ${opBtn}
            <button class="btn btn-sm btn-danger" data-qdel="${it.id}" ${isProc ? "disabled" : ""} title="Quitar">✕</button>
          </div>
        </td>
      </tr>
    `;
  });
  $("queueTableBody").innerHTML = rows.length
    ? rows.join("")
    : `<tr><td colspan="6" class="muted">No hay elementos en la cola de ${state.queueTab}.</td></tr>`;
}

$("queueTableBody").addEventListener("click", async (e) => {
  const moveBtn = e.target.closest("[data-qmove]");
  if (moveBtn) {
    const [id, dir] = moveBtn.dataset.qmove.split(":");
    await api(`/api/queue/item/${id}/move`, { method: "POST", body: JSON.stringify({ direction: dir }) });
    return loadQueue();
  }
  const pauseBtn = e.target.closest("[data-qpause]");
  if (pauseBtn) {
    await api(`/api/queue/item/${pauseBtn.dataset.qpause}/pause`, { method: "POST" });
    return loadQueue();
  }
  const opBtn = e.target.closest("[data-qop]");
  if (opBtn) {
    await api(`/api/queue/item/${opBtn.dataset.qop}/toggle-op`, { method: "POST" });
    return loadQueue();
  }
  const delBtn = e.target.closest("[data-qdel]");
  if (delBtn) {
    await api(`/api/queue/item/${delBtn.dataset.qdel}`, { method: "DELETE" });
    loadQueue();
    refreshStatus();
  }
});

$("btnQueueAutoOrder").onclick = async () => { await api(`/api/queue/${state.queueTab}/auto-order`, { method: "POST" }); loadQueue(); };
$("btnQueueRetry").onclick = async () => { await api(`/api/queue/${state.queueTab}/retry-errors`, { method: "POST" }); loadQueue(); };
$("btnQueueClearOk").onclick = async () => { await api(`/api/queue/${state.queueTab}/clear?scope=completed`, { method: "POST" }); loadQueue(); };
$("btnQueueClearErr").onclick = async () => { await api(`/api/queue/${state.queueTab}/clear?scope=errors`, { method: "POST" }); loadQueue(); };
$("btnQueueClearPend").onclick = async () => {
  if (!confirm("¿Vaciar todos los elementos pendientes de esta cola?")) return;
  await api(`/api/queue/${state.queueTab}/clear?scope=pending`, { method: "POST" });
  loadQueue();
  refreshStatus();
};

// --- CONFIGURACIÓN ---
async function loadSettings() {
  const s = await api("/api/settings");
  state.settings = s;
  $("cfgMoverOrigin").value = s.mover_origin || "";
  $("cfgMoverDest").value = s.mover_dest || "";
  $("cfgMoverOp").value = s.mover_operation || "copiar";
  $("cfgMoverConflict").value = s.mover_conflict || "omitir";
  $("cfgMoverVerify").value = s.mover_verify || "tamano";
  $("cfgMoverLimit").value = s.mover_limit_kbps ?? 0;
  $("cfgMoverRetries").value = s.mover_retries ?? 3;
  $("cfgMoverRetryWait").value = s.mover_retry_wait ?? 10;
  $("cfgMoverStruct").checked = !!s.mover_keep_structure;
  $("cfgMoverFavs").value = (s.mover_favorites || []).join("\n");

  $("cfgFtpHost").value = s.ftp_host || "";
  $("cfgFtpPort").value = s.ftp_port ?? 5000;
  $("cfgFtpUser").value = s.ftp_user || "";
  $("cfgFtpPassword").value = "";
  $("cfgFtpPwHint").textContent = s.ftp_has_password ? "(guardada)" : "(vacía)";
  $("cfgFtpLocalRoot").value = s.ftp_local_root || "";
  $("cfgFtpRemoteDir").value = s.ftp_remote_dir || "/";
  $("cfgFtpLimit").value = s.ftp_limit_kbps ?? 500;
  $("cfgFtpRetries").value = s.ftp_retries ?? 3;
  $("cfgFtpRetryWait").value = s.ftp_retry_wait ?? 10;
  $("cfgFtpStruct").checked = !!s.ftp_keep_structure;
  $("cfgFtpPassive").checked = !!s.ftp_passive;
  $("cfgFtpTls").checked = !!s.ftp_tls;
  $("cfgFtpVerifySize").checked = !!s.ftp_verify_size;
  $("exploreMoverOp").value = s.mover_operation || "copiar";
}

$("settingsForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const payload = {
    mover_origin: $("cfgMoverOrigin").value.trim(),
    mover_dest: $("cfgMoverDest").value.trim(),
    mover_operation: $("cfgMoverOp").value,
    mover_conflict: $("cfgMoverConflict").value,
    mover_verify: $("cfgMoverVerify").value,
    mover_limit_kbps: Number($("cfgMoverLimit").value || 0),
    mover_retries: Number($("cfgMoverRetries").value || 0),
    mover_retry_wait: Number($("cfgMoverRetryWait").value || 10),
    mover_keep_structure: $("cfgMoverStruct").checked,
    mover_favorites: $("cfgMoverFavs").value.split("\n").map((x) => x.trim()).filter(Boolean),
    ftp_host: $("cfgFtpHost").value.trim(),
    ftp_port: Number($("cfgFtpPort").value || 5000),
    ftp_user: $("cfgFtpUser").value.trim(),
    ftp_local_root: $("cfgFtpLocalRoot").value.trim(),
    ftp_remote_dir: $("cfgFtpRemoteDir").value.trim() || "/",
    ftp_limit_kbps: Number($("cfgFtpLimit").value || 0),
    ftp_retries: Number($("cfgFtpRetries").value || 0),
    ftp_retry_wait: Number($("cfgFtpRetryWait").value || 10),
    ftp_keep_structure: $("cfgFtpStruct").checked,
    ftp_passive: $("cfgFtpPassive").checked,
    ftp_tls: $("cfgFtpTls").checked,
    ftp_verify_size: $("cfgFtpVerifySize").checked,
  };
  if ($("cfgFtpPassword").value !== "") {
    payload.ftp_password = $("cfgFtpPassword").value;
  }
  try {
    state.settings = await api("/api/settings", { method: "PUT", body: JSON.stringify(payload) });
    $("cfgFtpPassword").value = "";
    $("cfgFtpPwHint").textContent = state.settings.ftp_has_password ? "(guardada)" : "(vacía)";
    $("cfgSaveMsg").textContent = "Configuración guardada";
    showToast("Configuración guardada");
  } catch (err) {
    showToast(err.message);
  }
});

$("btnTestFtp").onclick = async () => {
  const b = $("ftpTestBanner");
  b.className = "banner";
  b.textContent = "Probando conexión FTP...";
  b.classList.remove("hidden");
  try {
    const res = await api("/api/ftp/test", { method: "POST" });
    b.className = `banner ${res.ok ? "ok" : "err"}`;
    b.textContent = res.message;
  } catch (err) {
    b.className = "banner err";
    b.textContent = err.message;
  }
};

$("btnScanSwitch").onclick = async () => {
  const box = $("switchScanResults");
  box.classList.remove("hidden");
  box.innerHTML = "Escaneando subredes LAN (puertos 5000, 2121, 21)...";
  try {
    const res = await api("/api/ftp/scan-switch", { method: "POST", body: JSON.stringify({}) });
    if (!res.found || !res.found.length) {
      box.innerHTML = `No se encontró ningún servidor FTP en subredes [${(res.prefixes || []).join(", ")}] puertos [${(res.ports || []).join(", ")}].`;
      return;
    }
    box.innerHTML = res.found.map((hit) => `
      <div class="scan-hit">
        <div>
          <strong>${escapeHtml(hit.ip)}:${hit.port}</strong>
          <span class="muted">${hit.banner ? escapeHtml(hit.banner) : "puerto abierto"}</span>
        </div>
        <button type="button" class="btn btn-sm btn-primary" data-use-hit="${escapeHtml(hit.ip)}:${hit.port}">Usar esta IP</button>
      </div>
    `).join("");
    box.querySelectorAll("[data-use-hit]").forEach((btn) => {
      btn.onclick = () => {
        const [ip, port] = btn.dataset.useHit.split(":");
        $("cfgFtpHost").value = ip;
        $("cfgFtpPort").value = port;
        showToast(`Seleccionado ${ip}:${port} (pulsa Guardar configuración)`);
      };
    });
  } catch (err) {
    box.innerHTML = escapeHtml(err.message);
  }
};

// --- LOGS ---
async function loadLogs() {
  const kind = $("logFilterKind").value;
  const data = await api(`/api/logs?kind=${kind}&limit=300`);
  $("logBox").innerHTML = (data.logs || []).map((l) => {
    const dt = new Date(l.ts * 1000).toLocaleString();
    return `<div class="log-line log-${escapeHtml(l.level)}">[${dt}] [${escapeHtml(l.kind.toUpperCase())}] ${escapeHtml(l.message)}</div>`;
  }).join("") || `<div class="muted">Sin registros.</div>`;
}
$("logFilterKind").onchange = loadLogs;
$("btnRefreshLogs").onclick = loadLogs;
$("btnClearLogs").onclick = async () => { await api("/api/logs", { method: "DELETE" }); loadLogs(); };

// --- PICKER MODAL (Local / FTP) ---
async function openPicker(mode, initialPath) {
  state.picker.mode = mode;
  $("pickerModal").classList.remove("hidden");
  $("pickerTitle").textContent = mode === "local" ? "Elegir carpeta destino local" : "Elegir carpeta destino FTP";
  const shortcuts = $("pickerShortcuts");
  if (mode === "ftp") {
    shortcuts.innerHTML = ["/", "/install", "/nsp", "/switch"].map((p) =>
      `<button type="button" class="btn btn-sm" data-pk="${p}">${p}</button>`
    ).join("");
  } else {
    const favs = (state.settings && state.settings.mover_favorites) || [];
    shortcuts.innerHTML = favs.map((p) =>
      `<button type="button" class="btn btn-sm" data-pk="${escapeHtml(p)}">★ ${escapeHtml(p.split("/").pop() || p)}</button>`
    ).join("");
  }
  shortcuts.querySelectorAll("[data-pk]").forEach((b) => { b.onclick = () => loadPickerDir(b.dataset.pk); });
  await loadPickerDir(initialPath);
}

async function loadPickerDir(path) {
  state.picker.path = path;
  $("pickerPathInput").value = path;
  if (state.picker.mode === "local") {
    const qs = new URLSearchParams({ kind: "mover" });
    if (path) qs.set("path", path);
    const data = await api(`/api/fs/list?${qs}`);
    state.picker.path = data.path;
    $("pickerPathInput").value = data.path;
    $("pickerList").innerHTML = (data.dirs || []).map((d) =>
      `<div class="picker-row" data-pkopen="${escapeHtml(d.path)}">📁 ${escapeHtml(d.name)}</div>`
    ).join("") || `<div class="picker-row muted">(Sin subcarpetas)</div>`;
  } else {
    const res = await api(`/api/ftp/dirs?path=${encodeURIComponent(path || "/")}`);
    if (!res.ok) {
      $("pickerList").innerHTML = `<div class="picker-row muted">${escapeHtml(res.error || "Error")}</div>`;
      return;
    }
    const base = res.path === "/" ? "" : res.path;
    $("pickerList").innerHTML = (res.dirs || []).map((name) =>
      `<div class="picker-row" data-pkopen="${escapeHtml(`${base}/${name}`)}">📁 ${escapeHtml(name)}</div>`
    ).join("") || `<div class="picker-row muted">(Sin subcarpetas)</div>`;
  }
  $("pickerList").querySelectorAll("[data-pkopen]").forEach((r) => {
    r.onclick = () => loadPickerDir(r.dataset.pkopen);
  });
}

$("btnPickLocalDest").onclick = () => openPicker("local", $("exploreMoverDest").value || state.currentPath);
$("btnPickRemoteDest").onclick = () => openPicker("ftp", $("exploreFtpDest").value || "/");
$("btnPickerClose").onclick = () => $("pickerModal").classList.add("hidden");
$("btnPickerCancel").onclick = () => $("pickerModal").classList.add("hidden");
$("btnPickerGo").onclick = () => loadPickerDir($("pickerPathInput").value);
$("btnPickerUp").onclick = () => {
  const cur = state.picker.path || "/";
  const idx = cur.lastIndexOf("/");
  const parent = idx <= 0 ? "/" : cur.slice(0, idx);
  loadPickerDir(parent);
};
$("btnPickerUse").onclick = () => {
  if (state.picker.mode === "local") $("exploreMoverDest").value = state.picker.path;
  else $("exploreFtpDest").value = state.picker.path;
  $("pickerModal").classList.add("hidden");
};

// --- INIT ---
(async function init() {
  await loadSettings();
  await loadDir("");
  await refreshStatus();
  setInterval(refreshStatus, 1500);
})();

