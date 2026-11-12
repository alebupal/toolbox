const state = {
  activeTab: "explore",
  exploreMode: "mover", // 'mover' | 'ftp'
  moverOp: "copiar", // 'copiar' | 'mover'
  useAutoDest: false,
  queueTab: "mover",

  srcPath: "",
  srcParent: null,
  srcData: null,
  selectedPaths: new Set(),
  dirSizes: {},
  lastQueueItems: [],

  dstLocalPath: "",
  dstLocalParent: null,
  dstLocalData: null,
  dstRemotePath: "/",
  dstRemoteData: null,

  settings: null,
  picker: { targetInputId: null, path: "", onPick: null },
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

function renderBreadcrumbs(containerEl, path, rootPath, onClickPath) {
  if (!path) {
    containerEl.innerHTML = "";
    return;
  }
  const normRoot = rootPath === "/" ? "/" : rootPath.replace(/\/+$/, "");
  const parts = [];
  if (normRoot === "/") {
    parts.push({ label: "/", path: "/" });
    const rest = path.replace(/^\/+/, "").split("/").filter(Boolean);
    let acc = "";
    for (const seg of rest) {
      acc += `/${seg}`;
      parts.push({ label: seg, path: acc });
    }
  } else {
    parts.push({ label: normRoot, path: normRoot });
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
      const sep = idx > 0 ? `<span class="crumb-sep">›</span>` : "";
      if (isLast) return `${sep}<span class="crumb current">${escapeHtml(p.label)}</span>`;
      return `${sep}<button type="button" class="crumb" data-cpath="${escapeHtml(p.path)}">${escapeHtml(p.label)}</button>`;
    })
    .join("");

  containerEl.querySelectorAll("button.crumb").forEach((b) => {
    b.onclick = () => onClickPath(b.dataset.cpath);
  });
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
  if (tab === "readme") loadReadme();
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
  const sc = info.space_check;
  const freeSuffix = (kind === "mover" && sc && sc.free_bytes != null)
    ? ` · Libre dest: ${fmtBytes(sc.free_bytes)}`
    : "";
  metaEl.textContent = `Pendientes: ${pend} (${fmtBytes(info.pending_bytes)}) · Pausados: ${c.PAUS || 0} · OK: ${c.OK || 0} · ERR: ${c.ERR || 0}${freeSuffix}`;

  if (kind === "mover") {
    const alertEl = $("moverSpaceAlert");
    if (sc && sc.space_warning) {
      alertEl.textContent = `⚠️ Espacio insuficiente en destino: se requieren ${fmtBytes(sc.needed_bytes)} y quedan ${fmtBytes(sc.free_bytes)} libres (faltan ${fmtBytes(sc.shortfall_bytes)})`;
      alertEl.classList.remove("hidden");
    } else {
      alertEl.classList.add("hidden");
    }
  }

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
    // Silenciar errores transitorios
  }
}

$("btnStartMover").onclick = async () => { await api("/api/queue/mover/start", { method: "POST" }); refreshStatus(); };
$("btnStopMover").onclick = async () => { await api("/api/queue/mover/stop", { method: "POST" }); refreshStatus(); };
$("btnStartFtp").onclick = async () => { await api("/api/queue/ftp/start", { method: "POST" }); refreshStatus(); };
$("btnStopFtp").onclick = async () => { await api("/api/queue/ftp/stop", { method: "POST" }); refreshStatus(); };

// --- MODO EXPLORAR (DOBLE PANEL) ---
function updateExploreActionLabels() {
  const isFtp = state.exploreMode === "ftp";
  $("exploreMoverOpGroup").classList.toggle("hidden", isFtp);
  $("btnAddSwitchOnly").classList.toggle("hidden", !isFtp);
  $("dstPaneTitle").textContent = isFtp ? "📡 Destino FTP / Switch" : "🎯 Destino local (Servidor)";
  $("btnDstNewDir").classList.toggle("hidden", isFtp);

  if (isFtp) {
    $("btnAddSelected").textContent = "📡 Añadir a cola FTP →";
    $("btnSendAndStart").textContent = "⚡ Subir ahora";
  } else if (state.moverOp === "mover") {
    $("btnAddSelected").textContent = "✂️ Mover seleccionados →";
    $("btnSendAndStart").textContent = "⚡ Mover ahora";
  } else {
    $("btnAddSelected").textContent = "📋 Copiar seleccionados →";
    $("btnSendAndStart").textContent = "⚡ Copiar ahora";
  }
}

function setExploreMode(mode) {
  state.exploreMode = mode;
  $("modeBtnMover").classList.toggle("active", mode === "mover");
  $("modeBtnFtp").classList.toggle("active", mode === "ftp");
  state.selectedPaths.clear();
  updateExploreActionLabels();
  loadSrcDir("");
  loadDstDir();
}

function setMoverOp(op) {
  state.moverOp = op;
  $("opBtnCopiar").classList.toggle("active", op === "copiar");
  $("opBtnMover").classList.toggle("active", op === "mover");
  updateExploreActionLabels();
  renderSrcTable();
}

$("modeBtnMover").onclick = () => setExploreMode("mover");
$("modeBtnFtp").onclick = () => setExploreMode("ftp");
$("opBtnCopiar").onclick = () => setMoverOp("copiar");
$("opBtnMover").onclick = () => setMoverOp("mover");

function setMobilePane(pane) {
  $("dualPanes").dataset.mobilePane = pane;
  $("mPaneBtnSrc").classList.toggle("active", pane === "src");
  $("mPaneBtnDst").classList.toggle("active", pane === "dst");
}

function updateMobileDstLabel() {
  if (state.useAutoDest) {
    $("mPaneDstLabel").textContent = "Auto (config)";
    return;
  }
  const raw = state.exploreMode === "mover" ? (state.dstLocalPath || "-") : (state.dstRemotePath || "/");
  if (!raw || raw === "/" || raw === "-") {
    $("mPaneDstLabel").textContent = raw || "-";
    return;
  }
  const parts = raw.split("/").filter(Boolean);
  $("mPaneDstLabel").textContent = parts.length > 2 ? `…/${parts.slice(-2).join("/")}` : raw;
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

// --- PANEL IZQUIERDO (ORIGEN) ---
async function loadSrcDir(path) {
  try {
    const hidden = $("chkShowHidden").checked ? "true" : "false";
    const qs = new URLSearchParams({ kind: state.exploreMode, show_hidden: hidden });
    if (path) qs.set("path", path);
    const data = await api(`/api/fs/list?${qs}`);
    state.srcData = data;
    state.srcPath = data.path;
    state.srcParent = data.parent;
    state.selectedPaths.clear();
    state.dirSizes = {};
    $("srcPathInput").value = data.path;
    $("btnSrcUp").disabled = !data.parent;
    $("srcDiskInfo").textContent = data.disk ? `Libre: ${fmtBytes(data.disk.free_bytes)}` : "";
    renderBookmarksBar($("srcBookmarks"), data.bookmarks || [], false, (p) => loadSrcDir(p));
    renderBreadcrumbs($("srcBreadcrumbs"), data.path, data.data_root, (p) => loadSrcDir(p));
    renderSrcTable();
  } catch (err) {
    showToast(err.message);
  }
}

function renderBookmarksBar(el, bookmarks, includeFavs, onClick) {
  const favs = includeFavs ? ((state.settings && state.settings.mover_favorites) || []) : [];
  const all = [
    ...bookmarks,
    ...favs.map((p) => ({ label: `★ ${p.split("/").pop() || p}`, path: p })),
  ];
  el.innerHTML = all
    .map((b) => `<button type="button" class="btn btn-sm btn-ghost" data-bpath="${escapeHtml(b.path)}">${escapeHtml(b.label)}</button>`)
    .join("");
  el.querySelectorAll("button").forEach((btn) => {
    btn.onclick = () => onClick(btn.dataset.bpath);
  });
}

function renderSrcTable() {
  const data = state.srcData;
  if (!data) return;
  const filter = $("fsFilter").value.trim().toLowerCase();
  const dirs = data.dirs.filter((d) => !filter || d.name.toLowerCase().includes(filter));
  const files = data.files.filter((f) => !filter || f.name.toLowerCase().includes(filter));

  const actionLabel = state.exploreMode === "ftp"
    ? "Subir →"
    : (state.moverOp === "mover" ? "Mover →" : "Copiar →");

  const rows = [];
  for (const d of dirs) {
    const checked = state.selectedPaths.has(d.path) ? "checked" : "";
    const sizeHtml = state.dirSizes[d.path]
      ? escapeHtml(state.dirSizes[d.path])
      : `<button type="button" class="btn btn-sm btn-ghost" data-calc-size="${escapeHtml(d.path)}" title="Calcular tamaño de esta carpeta">📏 Ver</button>`;
    rows.push(`
      <tr>
        <td class="col-chk"><input type="checkbox" class="fs-chk" data-path="${escapeHtml(d.path)}" ${checked}></td>
        <td class="col-name"><a class="dir-link" data-open="${escapeHtml(d.path)}">📁 ${escapeHtml(d.name)}</a></td>
        <td class="col-status"><span class="tag">DIR</span></td>
        <td class="col-size">${sizeHtml}</td>
        <td class="col-actions">
          <div class="row-actions">
            <button type="button" class="btn btn-sm" data-add-dir="${escapeHtml(d.path)}">+ Carpeta</button>
            <button type="button" class="btn btn-sm btn-ghost" data-rename="${escapeHtml(d.path)}" data-rname="${escapeHtml(d.name)}" title="Renombrar">✏️</button>
            <button type="button" class="btn btn-sm btn-danger" data-delete="${escapeHtml(d.path)}" title="Eliminar">🗑</button>
          </div>
        </td>
      </tr>
    `);
  }
  for (const f of files) {
    const checked = state.selectedPaths.has(f.path) ? "checked" : "";
    const swBadge = f.switch_type ? `<span class="tag tag-${f.switch_type.toLowerCase()}">${f.switch_type}</span> ` : "";
    const stBadge = f.queue_status ? `<span class="tag tag-${f.queue_status.toLowerCase()}">${f.queue_status}</span>` : "";
    rows.push(`
      <tr>
        <td class="col-chk"><input type="checkbox" class="fs-chk" data-path="${escapeHtml(f.path)}" ${checked}></td>
        <td class="col-name file-name">${escapeHtml(f.name)}</td>
        <td class="col-status">${swBadge}${stBadge}</td>
        <td class="col-size">${fmtBytes(f.size_bytes)}</td>
        <td class="col-actions">
          <div class="row-actions">
            <button type="button" class="btn btn-sm btn-primary" data-add-file="${escapeHtml(f.path)}">${actionLabel}</button>
            <button type="button" class="btn btn-sm btn-ghost" data-rename="${escapeHtml(f.path)}" data-rname="${escapeHtml(f.name)}" title="Renombrar">✏️</button>
            <button type="button" class="btn btn-sm btn-danger" data-delete="${escapeHtml(f.path)}" title="Eliminar">🗑</button>
          </div>
        </td>
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
  const count = state.selectedPaths.size;
  $("selectionSummary").textContent = `${count} seleccionado(s)`;
  $("mPaneSrcCount").textContent = count;
  $("srcBatchBar").classList.toggle("has-selection", count > 0);
}

$("fsTableBody").addEventListener("click", async (e) => {
  const openEl = e.target.closest("[data-open]");
  if (openEl) return loadSrcDir(openEl.dataset.open);
  const addFileEl = e.target.closest("[data-add-file]");
  if (addFileEl) return enqueuePaths([addFileEl.dataset.addFile], false, false, false);
  const addDirEl = e.target.closest("[data-add-dir]");
  if (addDirEl) return enqueuePaths([addDirEl.dataset.addDir], true, false, false);
  const sizeEl = e.target.closest("[data-calc-size]");
  if (sizeEl) {
    const dirPath = sizeEl.dataset.calcSize;
    sizeEl.textContent = "⏳...";
    try {
      const res = await api(`/api/fs/dir-size?path=${encodeURIComponent(dirPath)}`);
      state.dirSizes[dirPath] = `${fmtBytes(res.size_bytes)} (${res.files_count})`;
      renderSrcTable();
    } catch (err) {
      showToast(err.message);
      renderSrcTable();
    }
    return;
  }
  const renEl = e.target.closest("[data-rename]");
  if (renEl) {
    const oldName = renEl.dataset.rname || "";
    const newName = prompt("Nuevo nombre:", oldName);
    if (!newName || newName.trim() === oldName) return;
    try {
      await api("/api/fs/rename", {
        method: "POST",
        body: JSON.stringify({ path: renEl.dataset.rename, new_name: newName.trim() }),
      });
      showToast("Renombrado correctamente");
      loadSrcDir(state.srcPath);
    } catch (err) {
      showToast(err.message);
    }
    return;
  }
  const delEl = e.target.closest("[data-delete]");
  if (delEl) {
    const target = delEl.dataset.delete;
    if (!confirm(`¿Seguro que deseas eliminar permanentemente del servidor:\n${target}?`)) return;
    try {
      await api("/api/fs/delete", { method: "POST", body: JSON.stringify({ paths: [target] }) });
      showToast("Eliminado correctamente");
      loadSrcDir(state.srcPath);
    } catch (err) {
      showToast(err.message);
    }
  }
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

$("btnDeleteSelected").onclick = async () => {
  const paths = Array.from(state.selectedPaths);
  if (!paths.length) return showToast("Selecciona al menos un archivo o carpeta para borrar");
  if (!confirm(`¿Eliminar permanentemente ${paths.length} elemento(s) seleccionado(s) del servidor?`)) return;
  try {
    const res = await api("/api/fs/delete", { method: "POST", body: JSON.stringify({ paths }) });
    showToast(`Eliminados ${res.deleted} elemento(s)`);
    loadSrcDir(state.srcPath);
  } catch (err) {
    showToast(err.message);
  }
};

$("btnSrcUp").onclick = () => { if (state.srcParent) loadSrcDir(state.srcParent); };
$("btnSrcGo").onclick = () => loadSrcDir($("srcPathInput").value);
$("srcPathInput").addEventListener("keydown", (e) => { if (e.key === "Enter") loadSrcDir($("srcPathInput").value); });
$("fsFilter").addEventListener("input", renderSrcTable);
$("chkShowHidden").onchange = () => loadSrcDir(state.srcPath);

$("btnSrcNewDir").onclick = async () => {
  const name = prompt("Nombre de la nueva carpeta en origen:");
  if (!name) return;
  try {
    await api("/api/fs/mkdir", { method: "POST", body: JSON.stringify({ parent: state.srcPath, name }) });
    showToast("Carpeta creada en origen");
    loadSrcDir(state.srcPath);
  } catch (err) {
    showToast(err.message);
  }
};

// --- PANEL DERECHO (DESTINO) ---
function renderDstTable() {
  const q = (($("dstFilter") && $("dstFilter").value) || "").trim().toLowerCase();
  if (state.exploreMode === "mover") {
    const data = state.dstLocalData;
    if (!data) return;
    const dirs = (data.dirs || []).filter((d) => !q || d.name.toLowerCase().includes(q));
    const files = (data.files || []).filter((f) => !q || f.name.toLowerCase().includes(q));
    const rows = [
      ...dirs.map((d) => `
        <tr>
          <td><a class="dir-link" data-dopen="${escapeHtml(d.path)}">📁 ${escapeHtml(d.name)}</a></td>
          <td class="muted">DIR</td>
        </tr>
      `),
      ...files.map((f) => `
        <tr>
          <td class="muted file-name">📄 ${escapeHtml(f.name)}</td>
          <td class="muted">${fmtBytes(f.size_bytes)}</td>
        </tr>
      `),
    ];
    $("dstTableBody").innerHTML = rows.length
      ? rows.join("")
      : `<tr><td colspan="2" class="muted">Carpeta destino vacía o sin coincidencias.</td></tr>`;
  } else {
    const res = state.dstRemoteData;
    if (!res || !res.ok) return;
    const base = res.path === "/" ? "" : res.path;
    const dirs = (res.dirs || []).filter((name) => !q || name.toLowerCase().includes(q));
    const rows = dirs.map((name) => `
      <tr>
        <td><a class="dir-link" data-dopen="${escapeHtml(`${base}/${name}`)}">📁 ${escapeHtml(name)}</a></td>
        <td class="muted">DIR</td>
      </tr>
    `);
    $("dstTableBody").innerHTML = rows.length
      ? rows.join("")
      : `<tr><td colspan="2" class="muted">Sin subcarpetas o sin coincidencias en ${escapeHtml(state.dstRemotePath)}.</td></tr>`;
  }
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
      $("dstPathInput").value = data.path;
      $("dstActiveLabel").textContent = data.path;
      updateMobileDstLabel();
      $("btnDstUp").disabled = !data.parent;
      $("dstDiskInfo").textContent = data.disk ? `Libre: ${fmtBytes(data.disk.free_bytes)}` : "";
      renderBookmarksBar($("dstBookmarks"), data.bookmarks || [], true, (p) => loadDstDir(p));
      renderBreadcrumbs($("dstBreadcrumbs"), data.path, data.data_root, (p) => loadDstDir(p));
      renderDstTable();
    } catch (err) {
      showToast(err.message);
    }
  } else {
    const target = pathOverride !== undefined ? pathOverride : (state.dstRemotePath || (state.settings && state.settings.ftp_remote_dir) || "/");
    state.dstRemotePath = target || "/";
    $("dstPathInput").value = state.dstRemotePath;
    $("dstActiveLabel").textContent = state.dstRemotePath;
    updateMobileDstLabel();
    $("dstDiskInfo").textContent = "";
    $("btnDstUp").disabled = state.dstRemotePath === "/";

    const shortcuts = ["/", "/install", "/nsp", "/switch"];
    renderBookmarksBar(
      $("dstBookmarks"),
      shortcuts.map((p) => ({ label: p, path: p })),
      false,
      (p) => loadDstDir(p)
    );
    renderBreadcrumbs($("dstBreadcrumbs"), state.dstRemotePath, "/", (p) => loadDstDir(p));

    $("dstTableBody").innerHTML = `<tr><td colspan="2" class="muted">Cargando carpetas FTP...</td></tr>`;
    try {
      const res = await api(`/api/ftp/dirs?path=${encodeURIComponent(state.dstRemotePath)}`);
      state.dstRemoteData = res;
      if (!res.ok) {
        $("dstTableBody").innerHTML = `<tr><td colspan="2" class="muted">No conectado (${escapeHtml(res.error || "revisa Configuración")}). Puedes escribir la ruta arriba igualmente.</td></tr>`;
        return;
      }
      renderDstTable();
    } catch (err) {
      $("dstTableBody").innerHTML = `<tr><td colspan="2" class="muted">${escapeHtml(err.message)}</td></tr>`;
    }
  }
}

$("dstTableBody").addEventListener("click", (e) => {
  const openEl = e.target.closest("[data-dopen]");
  if (openEl) loadDstDir(openEl.dataset.dopen);
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
$("btnDstGo").onclick = () => loadDstDir($("dstPathInput").value.trim());
$("dstPathInput").addEventListener("keydown", (e) => { if (e.key === "Enter") loadDstDir($("dstPathInput").value.trim()); });
$("dstFilter").addEventListener("input", renderDstTable);
$("btnDstRefresh").onclick = () => loadDstDir();

$("btnDstNewDir").onclick = async () => {
  if (state.exploreMode !== "mover") return;
  const name = prompt(`Crear carpeta nueva dentro de:\n${state.dstLocalPath}`);
  if (!name) return;
  try {
    const res = await api("/api/fs/mkdir", { method: "POST", body: JSON.stringify({ parent: state.dstLocalPath, name }) });
    showToast("Carpeta creada en destino");
    loadDstDir(res.path);
  } catch (err) {
    showToast(err.message);
  }
};

// --- AÑADIR A LA COLA ---
async function enqueuePaths(paths, recursive, onlySwitch, startNow) {
  if (!paths.length) return showToast("Selecciona al menos un archivo o carpeta");
  let destDir = "";
  if (!state.useAutoDest) {
    destDir = state.exploreMode === "mover" ? state.dstLocalPath : state.dstRemotePath;
  }
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
    if (startNow && res.added > 0) {
      await api(`/api/queue/${state.exploreMode}/start`, { method: "POST" });
      showToast(`Añadidos ${res.added} y cola iniciada`);
    } else {
      showToast(`Añadidos: ${res.added}${res.skipped ? ` (omitidos por colisión: ${res.skipped})` : ""}`);
    }
    loadSrcDir(state.srcPath);
    refreshStatus();
  } catch (err) {
    showToast(err.message);
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
  state.lastQueueItems = data.items || [];
  const rows = state.lastQueueItems.map((it, idx) => {
    const isProc = it.status === "PROC";
    const canEdit = !isProc && it.status !== "OK";
    const sw = it.switch_type ? `<span class="tag tag-${it.switch_type.toLowerCase()}">${it.switch_type}</span> ` : "";
    const opBtn = it.kind === "mover" && canEdit
      ? `<button type="button" class="btn btn-sm" data-qop="${it.id}" title="Alternar copiar/mover">⇄</button>`
      : "";
    const destBtn = canEdit
      ? `<button type="button" class="btn btn-sm" data-qdest="${it.id}" title="Cambiar carpeta destino">📂</button>`
      : "";
    return `
      <tr class="queue-row">
        <td class="qcol-idx">#${idx + 1}</td>
        <td class="qcol-status"><span class="tag tag-${it.status.toLowerCase()}">${it.status}</span></td>
        <td class="qcol-type">${sw}<span class="tag">${escapeHtml(it.operation.toUpperCase())}</span></td>
        <td class="path-cell qcol-paths">
          <div>${escapeHtml(it.source_path)}</div>
          <div class="muted">→ ${escapeHtml(it.dest_path)}</div>
          ${it.note ? `<span class="sub-note">${escapeHtml(it.note)}</span>` : ""}
          ${it.error ? `<span class="sub-err">${escapeHtml(it.error)}</span>` : ""}
        </td>
        <td class="qcol-size">${fmtBytes(it.size_bytes)}</td>
        <td class="qcol-actions">
          <div class="inline-group">
            <button type="button" class="btn btn-sm" data-qmove="${it.id}:top" ${isProc ? "disabled" : ""} title="Al principio">⤒</button>
            <button type="button" class="btn btn-sm" data-qmove="${it.id}:up" ${isProc ? "disabled" : ""} title="Subir">↑</button>
            <button type="button" class="btn btn-sm" data-qmove="${it.id}:down" ${isProc ? "disabled" : ""} title="Bajar">↓</button>
            <button type="button" class="btn btn-sm" data-qpause="${it.id}" ${isProc ? "disabled" : ""} title="Pausar/Reanudar">⏯</button>
            ${opBtn}
            ${destBtn}
            <button type="button" class="btn btn-sm btn-danger" data-qdel="${it.id}" ${isProc ? "disabled" : ""} title="Quitar">✕</button>
          </div>
        </td>
      </tr>
    `;
  });
  $("queueTableBody").innerHTML = rows.length
    ? rows.join("")
    : `<tr><td colspan="6" class="muted">No hay elementos en la cola de ${state.queueTab}.</td></tr>`;
}

async function applyQueueDestChange(itemIds, newDestDir) {
  try {
    const res = await api("/api/queue/change-dest", {
      method: "POST",
      body: JSON.stringify({ item_ids: itemIds, dest_dir: newDestDir }),
    });
    showToast(`Destino cambiado en ${res.updated} elemento(s)`);
    loadQueue();
    refreshStatus();
  } catch (err) {
    showToast(err.message);
  }
}

function promptChangeQueueDest(itemIds) {
  if (!itemIds.length) return showToast("No hay elementos editables en la cola");
  if (state.queueTab === "mover") {
    const initPath = state.dstLocalPath || (state.settings && state.settings.mover_dest) || "";
    openPicker(null, initPath, (pickedPath) => applyQueueDestChange(itemIds, pickedPath));
  } else {
    const currentRemote = state.dstRemotePath || (state.settings && state.settings.ftp_remote_dir) || "/";
    const nextRemote = prompt("Nueva carpeta remota destino FTP:", currentRemote);
    if (nextRemote && nextRemote.trim()) {
      applyQueueDestChange(itemIds, nextRemote.trim());
    }
  }
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
  const destBtn = e.target.closest("[data-qdest]");
  if (destBtn) {
    return promptChangeQueueDest([Number(destBtn.dataset.qdest)]);
  }
  const delBtn = e.target.closest("[data-qdel]");
  if (delBtn) {
    await api(`/api/queue/item/${delBtn.dataset.qdel}`, { method: "DELETE" });
    loadQueue();
    refreshStatus();
  }
});

$("btnQueueChangeDest").onclick = () => {
  const editableIds = (state.lastQueueItems || [])
    .filter((it) => it.status !== "PROC" && it.status !== "OK")
    .map((it) => it.id);
  promptChangeQueueDest(editableIds);
};

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
  setMoverOp(s.mover_operation || "copiar");
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

// --- DOCUMENTACIÓN / README ---
let readmeLoaded = false;

function slugifyHeading(text) {
  return text
    .toLowerCase()
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
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

    // Bloque de código ```
    const fence = line.match(/^```(\S*)\s*$/);
    if (fence) {
      i++;
      const codeLines = [];
      while (i < lines.length && !/^```\s*$/.test(lines[i])) {
        codeLines.push(lines[i]);
        i++;
      }
      i++; // saltar cierre ```
      const codeText = codeLines.join("\n");
      out.push(
        `<div class="readme-code-wrap"><button type="button" class="copy-code-btn">Copiar</button><pre><code>${escapeHtml(codeText)}</code></pre></div>`
      );
      continue;
    }

    // Separador ---
    if (/^---+\s*$/.test(line)) {
      out.push("<hr>");
      i++;
      continue;
    }

    // Encabezados # .. ####
    const heading = line.match(/^(#{1,4})\s+(.+)$/);
    if (heading) {
      const level = heading[1].length;
      const rawTitle = heading[2].trim();
      const slug = slugifyHeading(rawTitle);
      out.push(`<h${level} id="readme-${escapeHtml(slug)}">${formatInlineMarkdown(rawTitle)}</h${level}>`);
      i++;
      continue;
    }

    // Tabla GFM
    if (line.trim().startsWith("|") && i + 1 < lines.length && /^\s*\|?[\s:-]+\|/.test(lines[i + 1])) {
      const parseRow = (r) => r.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
      const headers = parseRow(line);
      i += 2; // saltar cabecera + separador
      const bodyRows = [];
      while (i < lines.length && lines[i].trim().startsWith("|")) {
        bodyRows.push(parseRow(lines[i]));
        i++;
      }
      const ths = headers.map((h) => `<th>${formatInlineMarkdown(h)}</th>`).join("");
      const trs = bodyRows
        .map((row) => `<tr>${row.map((c) => `<td>${formatInlineMarkdown(c)}</td>`).join("")}</tr>`)
        .join("");
      out.push(`<div class="readme-table-wrap"><table><thead><tr>${ths}</tr></thead><tbody>${trs}</tbody></table></div>`);
      continue;
    }

    // Blockquote >
    if (line.trimStart().startsWith(">")) {
      const qLines = [];
      while (i < lines.length && lines[i].trimStart().startsWith(">")) {
        qLines.push(lines[i].replace(/^\s*>\s?/, ""));
        i++;
      }
      out.push(`<blockquote>${qLines.map((q) => formatInlineMarkdown(q)).join("<br>")}</blockquote>`);
      continue;
    }

    // Lista no ordenada - o *
    if (/^\s*[-*]\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) {
        items.push(`<li>${formatInlineMarkdown(lines[i].replace(/^\s*[-*]\s+/, ""))}</li>`);
        i++;
      }
      out.push(`<ul>${items.join("")}</ul>`);
      continue;
    }

    // Lista ordenada 1.
    if (/^\s*\d+\.\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) {
        items.push(`<li>${formatInlineMarkdown(lines[i].replace(/^\s*\d+\.\s+/, ""))}</li>`);
        i++;
      }
      out.push(`<ol>${items.join("")}</ol>`);
      continue;
    }

    // Línea vacía
    if (!line.trim()) {
      i++;
      continue;
    }

    // Párrafo continuo
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
  box.innerHTML = `<p class="muted">Cargando documentación...</p>`;
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
    if (codeEl && navigator.clipboard) {
      await navigator.clipboard.writeText(codeEl.textContent || "");
      copyBtn.textContent = "✓ Copiado";
      setTimeout(() => { copyBtn.textContent = "Copiar"; }, 1500);
    }
    return;
  }
  const anchor = e.target.closest("[data-readme-anchor]");
  if (anchor) {
    e.preventDefault();
    const rawId = anchor.dataset.readmeAnchor;
    const clean = rawId.toLowerCase().normalize("NFD").replace(/[\u0300-\u036f]/g, "");
    const target = document.getElementById(`readme-${clean}`) || document.getElementById(`readme-${rawId}`);
    if (target) target.scrollIntoView({ behavior: "smooth", block: "start" });
  }
});

$("btnRefreshReadme").onclick = () => loadReadme(true);
$("btnReadmeTop").onclick = () => window.scrollTo({ top: 0, behavior: "smooth" });

// --- MODAL SELECTOR DE CARPETA (PARA CONFIGURACIÓN) ---
document.querySelectorAll("[data-browse-input]").forEach((btn) => {
  btn.onclick = () => {
    const inputId = btn.dataset.browseInput;
    openPicker(inputId, $(inputId).value || "");
  };
});

async function openPicker(targetInputId, initialPath, onPick = null) {
  state.picker.targetInputId = targetInputId;
  state.picker.onPick = onPick;
  $("pickerModal").classList.remove("hidden");
  $("pickerTitle").textContent = "Elegir carpeta del servidor";
  await loadPickerDir(initialPath);
}

async function loadPickerDir(path) {
  const qs = new URLSearchParams({ kind: "mover" });
  if (path) qs.set("path", path);
  const data = await api(`/api/fs/list?${qs}`);
  state.picker.path = data.path;
  state.picker.parent = data.parent;
  $("pickerPathInput").value = data.path;
  $("btnPickerUp").disabled = !data.parent;
  renderBookmarksBar($("pickerShortcuts"), data.bookmarks || [], true, (p) => loadPickerDir(p));
  renderBreadcrumbs($("pickerBreadcrumbs"), data.path, data.data_root, (p) => loadPickerDir(p));
  $("pickerList").innerHTML = (data.dirs || []).map((d) =>
    `<div class="picker-row" data-pkopen="${escapeHtml(d.path)}">📁 ${escapeHtml(d.name)}</div>`
  ).join("") || `<div class="picker-row muted">(Sin subcarpetas)</div>`;
  $("pickerList").querySelectorAll("[data-pkopen]").forEach((r) => {
    r.onclick = () => loadPickerDir(r.dataset.pkopen);
  });
}

$("btnPickerClose").onclick = () => $("pickerModal").classList.add("hidden");
$("btnPickerCancel").onclick = () => $("pickerModal").classList.add("hidden");
$("btnPickerGo").onclick = () => loadPickerDir($("pickerPathInput").value);
$("pickerPathInput").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); loadPickerDir($("pickerPathInput").value); } });
$("btnPickerUp").onclick = () => { if (state.picker.parent) loadPickerDir(state.picker.parent); };
$("btnPickerNewDir").onclick = async () => {
  const name = prompt(`Nueva carpeta dentro de:\n${state.picker.path}`);
  if (!name) return;
  try {
    const res = await api("/api/fs/mkdir", { method: "POST", body: JSON.stringify({ parent: state.picker.path, name }) });
    loadPickerDir(res.path);
  } catch (err) {
    showToast(err.message);
  }
};
$("btnPickerUse").onclick = () => {
  if (state.picker.onPick) {
    state.picker.onPick(state.picker.path);
    state.picker.onPick = null;
  } else if (state.picker.targetInputId) {
    $(state.picker.targetInputId).value = state.picker.path;
  }
  $("pickerModal").classList.add("hidden");
};

// --- INIT ---
(async function init() {
  try {
    await loadSettings();
  } catch (err) {
    showToast(err.message);
  }
  await loadSrcDir("");
  await loadDstDir();
  await refreshStatus();
  setInterval(refreshStatus, 1500);
})();
