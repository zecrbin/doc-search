import * as pdfjsLib from "/static/vendor/pdfjs/pdf.min.mjs";

pdfjsLib.GlobalWorkerOptions.workerSrc = "/static/vendor/pdfjs/pdf.worker.min.mjs";
const PDF_OPTS = {
  cMapUrl: "/static/vendor/pdfjs/cmaps/",
  cMapPacked: true,
  standardFontDataUrl: "/static/vendor/pdfjs/standard_fonts/",
};
const TOP_K = 200;
const ACCEPT = [".pdf", ".doc", ".docx", ".rtf"];

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch {}
    throw new Error(msg);
  }
  return r.json();
}

// ------------------------------------------------------------------ 页面切换

function showView(name) {
  $("#viewSearch").hidden = name !== "search";
  $("#viewLibrary").hidden = name !== "library";
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("on", b.dataset.view === name));
  if (location.hash !== `#${name}`) history.replaceState(null, "", `#${name}`);
  if (name === "library") refreshDocs();
  else if (viewer.pdf) requestAnimationFrame(relayout); // 隐藏期间窗口大小可能变了
}
$("#tabs").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-view]");
  if (b) showView(b.dataset.view);
});

// ------------------------------------------------------------------ 检索

const state = { results: [], query: "", current: null, docs: [], filter: "all" };

$("#docFilter").addEventListener("change", () => $("#q").value.trim() && runSearch());
$("#searchForm").addEventListener("submit", (e) => { e.preventDefault(); runSearch(); });
document.addEventListener("keydown", (e) => {
  if (e.key === "/" && !["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement.tagName)) {
    e.preventDefault();
    showView("search");
    $("#q").focus();
  }
});

async function runSearch() {
  const q = $("#q").value.trim();
  if (!q) return;
  const params = new URLSearchParams({ q, mode: "keyword", top_k: TOP_K });
  if ($("#docFilter").value) params.set("doc_id", $("#docFilter").value);
  $("#meta").textContent = "检索中…";
  try {
    const data = await api(`/api/search?${params}`);
    state.results = data.results;
    state.query = data.query;
    state.current = null;
    const more = data.results.length >= TOP_K ? `（只显示前 ${TOP_K} 条，可加词或选定文档缩小范围）` : "";
    $("#meta").textContent = `${data.results.length} 条结果${more} · ${data.took_ms} ms`;
    renderResults(data);
  } catch (err) {
    $("#meta").textContent = "";
    $("#results").innerHTML = `<li class="hint">检索失败：${esc(err.message)}</li>`;
  }
}

function highlighter(query) {
  // 精确匹配：只标出查询里的原词（空格分隔的每个词）
  const list = [...new Set(query.toLowerCase().split(/\s+/).filter(Boolean))]
    .sort((a, b) => b.length - a.length)
    .map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  if (!list.length) return esc;
  const re = new RegExp(`(${list.join("|")})`, "gi");
  return (text) => text.split(re).map((part, i) => (i % 2 ? `<mark>${esc(part)}</mark>` : esc(part))).join("");
}

function renderResults(data) {
  const ol = $("#results");
  if (!data.results.length) {
    ol.innerHTML = `<li class="hint">没有找到逐字包含「${esc(data.query)}」的内容。<br>多个词用空格分隔时，要求每个词都出现在同一段里。</li>`;
    return;
  }
  const hl = highlighter(data.query);
  ol.innerHTML = data.results.map((r, i) => {
    const pages = r.pages.length > 1 ? `第 ${r.pages[0]}–${r.pages.at(-1)} 页` : `第 ${r.page} 页`;
    const dups = r.duplicates.length
      ? `<details class="dups"><summary>相同段落还出现在 ${r.duplicates.length} 处</summary><ul>${
          r.duplicates.slice(0, 50).map((d, j) => `<li data-dup="${j}"><span class="dup-file">${esc(d.filename)}</span> · 第 ${d.page} 页<span class="here">正在查看</span></li>`).join("")
        }</ul></details>`
      : "";
    return `<li class="result" data-idx="${i}">
      <div class="result-head">
        <span class="result-file" title="${esc(r.filename)}">${esc(r.filename)}</span>
        <span class="here">正在查看</span>
        ${r.kind === "table" ? '<span class="tag table">表格</span>' : ""}
        <span class="result-page">${pages}</span>
      </div>
      ${r.heading ? `<div class="result-heading" title="${esc(r.heading)}">${esc(r.heading)}</div>` : ""}
      <div class="result-text">${hl(r.text)}</div>
      ${dups}
    </li>`;
  }).join("");
}

$("#results").addEventListener("click", (e) => {
  const li = e.target.closest(".result");
  if (!li) return;
  const idx = +li.dataset.idx;
  const dupEl = e.target.closest("[data-dup]");
  if (dupEl) {
    select(idx, +dupEl.dataset.dup);
    return;
  }
  if (e.target.closest("summary")) return;
  if (li.classList.contains("active")) li.classList.toggle("expanded");
  select(idx, null);
});

// 选中一条结果（dup 为相同段落里的第几处，null 表示结果本身），并标出右侧正在看的是哪个文件
function select(idx, dup) {
  const r = state.results[idx];
  state.current = { idx, dup };
  document.querySelectorAll("#results .result").forEach((el) => {
    const on = +el.dataset.idx === idx;
    el.classList.toggle("active", on);
    if (!on) el.classList.remove("expanded");
    el.querySelector(".result-head").classList.toggle("current", on && dup === null);
    el.querySelectorAll("[data-dup]").forEach((d) => d.classList.toggle("current", on && +d.dataset.dup === dup));
  });
  const t = dup === null ? r : r.duplicates[dup];
  openSource({ docId: t.doc_id, filename: t.filename, chunkId: t.chunk_id, regions: t.regions });
}

// ------------------------------------------------------------------ 原文预览

const viewer = { docId: null, pdf: null, pages: [], token: 0, hlToken: 0, boxes: [], exact: true, observer: null };
const pagesEl = $("#pages");

async function openSource({ docId, filename, chunkId, regions }) {
  $("#viewerBar").hidden = false;
  $("#viewerTitle").textContent = filename;
  $("#viewerTitle").title = filename;
  $("#downloadOrig").href = `/api/documents/${docId}/file`;
  $("#viewerNote").hidden = true;
  $("#viewerHits").textContent = "";
  const token = ++viewer.hlToken;
  const hlReq = api(`/api/chunks/${chunkId}/highlights?${new URLSearchParams({ q: state.query })}`)
    .catch(() => ({ boxes: regions, exact: false }));
  if (viewer.docId !== docId) {
    const ok = await loadPdf(docId);
    if (!ok) return;
  }
  const hl = await hlReq;
  if (token !== viewer.hlToken) return; // 期间又点了别的结果
  $("#viewerNote").hidden = hl.exact && !hl.ocr;
  $("#viewerNote").className = `viewer-note${hl.exact ? " soft" : ""}`;
  $("#viewerNote").textContent = hl.exact
    ? "扫描件：按 OCR 识别位置定位"
    : "扫描件未能定位到关键字（旧文档需重新解析），框出的是整段";
  $("#viewerHits").textContent = hl.exact ? `${hl.boxes.length} 处命中` : "";
  showHighlights(hl.boxes, hl.exact);
}

async function loadPdf(docId) {
  const token = ++viewer.token;
  viewer.observer?.disconnect();
  viewer.pdf?.destroy();
  viewer.pdf = null;
  viewer.docId = null;
  pagesEl.innerHTML = `<div class="empty">加载原文…</div>`;
  let pdf;
  try {
    pdf = await pdfjsLib.getDocument({ url: `/api/documents/${docId}/pdf`, ...PDF_OPTS }).promise;
  } catch (err) {
    pagesEl.innerHTML = `<div class="empty">原文加载失败：${esc(err.message)}</div>`;
    return false;
  }
  if (token !== viewer.token) { pdf.destroy(); return false; }
  const sizes = await Promise.all(
    Array.from({ length: pdf.numPages }, (_, i) => pdf.getPage(i + 1).then((p) => {
      const vp = p.getViewport({ scale: 1 });
      return [vp.width, vp.height];
    })),
  );
  if (token !== viewer.token) { pdf.destroy(); return false; }
  viewer.pdf = pdf;
  viewer.docId = docId;
  viewer.pages = sizes.map(([w, h]) => ({ w, h, scale: 1, el: null, rendered: false, gen: 0, task: null, canvas: null }));
  layoutPages();
  return true;
}

function layoutPages() {
  viewer.observer?.disconnect();
  pagesEl.innerHTML = "";
  const avail = Math.max(300, pagesEl.clientWidth - 64);
  viewer.observer = new IntersectionObserver((entries) => {
    entries.forEach((en) => {
      const i = +en.target.dataset.i;
      if (en.isIntersecting) renderPage(i);
      else if (viewer.pages[i]) releasePage(viewer.pages[i]);
    });
  }, { root: pagesEl, rootMargin: "800px 0px" });
  viewer.pages.forEach((p, i) => {
    releasePage(p);
    p.scale = Math.min(avail / p.w, 2);
    const el = document.createElement("div");
    el.className = "page";
    el.dataset.i = i;
    el.style.width = `${p.w * p.scale}px`;
    el.style.height = `${p.h * p.scale}px`;
    el.innerHTML = `<span class="page-no">${i + 1}</span>`;
    p.el = el;
    pagesEl.appendChild(el);
    viewer.observer.observe(el);
  });
  updatePageIndicator();
}

async function renderPage(i) {
  const p = viewer.pages[i];
  if (!p || p.rendered || !viewer.pdf) return;
  p.rendered = true;
  const gen = ++p.gen;
  let page;
  try {
    page = await viewer.pdf.getPage(i + 1);
  } catch {
    return; // 文档已切换/销毁
  }
  // 等待期间页面被重新布局、移出视野或再次渲染，这次渲染作废，避免同一页叠两张 canvas
  if (gen !== p.gen) return;
  const dpr = window.devicePixelRatio || 1;
  const vp = page.getViewport({ scale: p.scale * dpr });
  const canvas = document.createElement("canvas");
  canvas.width = Math.floor(vp.width);
  canvas.height = Math.floor(vp.height);
  p.el.prepend(canvas);
  p.canvas = canvas;
  p.task = page.render({ canvasContext: canvas.getContext("2d"), viewport: vp });
  try {
    await p.task.promise;
  } catch (err) {
    if (err?.name !== "RenderingCancelledException") console.error(err);
  }
}

// 远离视野的页释放 canvas，几百页的标书滚一遍也不会把内存吃满
function releasePage(p) {
  p.gen++;
  p.task?.cancel();
  p.canvas?.remove();
  Object.assign(p, { rendered: false, task: null, canvas: null });
}

function showHighlights(boxes, exact, flash = true) {
  viewer.boxes = boxes;
  viewer.exact = exact;
  pagesEl.querySelectorAll(".hl").forEach((x) => x.remove());
  let first = null;
  const pad = exact ? 1.5 : 3;
  for (const [pg, x0, y0, x1, y1] of boxes) {
    const p = viewer.pages[pg];
    if (!p) continue;
    const div = document.createElement("div");
    div.className = `hl ${exact ? "kw" : "para"}${flash ? " flash" : ""}`;
    Object.assign(div.style, {
      left: `${x0 * p.scale - pad}px`, top: `${y0 * p.scale - pad}px`,
      width: `${(x1 - x0) * p.scale + pad * 2}px`, height: `${(y1 - y0) * p.scale + pad * 2}px`,
    });
    p.el.appendChild(div);
    first ??= { p, y0 };
  }
  if (first && flash) {
    pagesEl.scrollTo({ top: first.p.el.offsetTop + first.y0 * first.p.scale - 120, behavior: "smooth" });
  }
}

function updatePageIndicator() {
  if (!viewer.pages.length) return;
  const mid = pagesEl.scrollTop + pagesEl.clientHeight / 3;
  let cur = 0;
  viewer.pages.forEach((p, i) => { if (p.el.offsetTop <= mid) cur = i; });
  $("#viewerPage").textContent = `${cur + 1} / ${viewer.pages.length} 页`;
}
pagesEl.addEventListener("scroll", () => requestAnimationFrame(updatePageIndicator), { passive: true });

function relayout() {
  if (!viewer.pdf || !pagesEl.clientWidth) return;
  const ratio = pagesEl.scrollTop / Math.max(1, pagesEl.scrollHeight);
  layoutPages();
  showHighlights(viewer.boxes, viewer.exact, false);
  pagesEl.scrollTop = ratio * pagesEl.scrollHeight;
}
let resizeTimer;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(relayout, 200);
});

// ------------------------------------------------------------------ 文件库

const STATUS = {
  importing: "导入中", queued: "排队中", converting: "转换 PDF", parsing: "解析中", embedding: "向量化", done: "成功", failed: "失败",
};
const busy = (d) => !["done", "failed"].includes(d.status);
const FILTERS = [
  ["all", "全部", () => true],
  ["busy", "处理中", busy],
  ["done", "成功", (d) => d.status === "done"],
  ["failed", "失败", (d) => d.status === "failed"],
];

const parseTime = (s) => (s ? new Date(s.replace(" ", "T")) : null);
const fmtTime = (s) => (s ? s.slice(0, 16) : "–");
function fmtDur(ms) {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return `${s} 秒`;
  if (s < 3600) return `${Math.floor(s / 60)} 分 ${s % 60} 秒`;
  return `${Math.floor(s / 3600)} 小时 ${Math.floor((s % 3600) / 60)} 分`;
}
function fmtSize(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / 1024 ** 2).toFixed(1)} MB`;
}

$("#statusFilter").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-filter]");
  if (!b) return;
  state.filter = b.dataset.filter;
  renderDocs();
});

$("#fileInput").addEventListener("change", (e) => { upload(e.target.files); e.target.value = ""; });

const dz = $("#dropzone");
["dragover", "dragenter"].forEach((t) => dz.addEventListener(t, (e) => { e.preventDefault(); dz.classList.add("over"); }));
["dragleave", "drop"].forEach((t) => dz.addEventListener(t, () => dz.classList.remove("over")));

// 整个窗口都可以拖文件进来
let dragDepth = 0;
window.addEventListener("dragenter", (e) => { if (e.dataTransfer?.types.includes("Files")) { dragDepth++; $("#dropOverlay").classList.add("show"); } });
window.addEventListener("dragleave", () => { if (--dragDepth <= 0) { dragDepth = 0; $("#dropOverlay").classList.remove("show"); } });
window.addEventListener("dragover", (e) => e.preventDefault());
window.addEventListener("drop", (e) => {
  e.preventDefault();
  dragDepth = 0;
  $("#dropOverlay").classList.remove("show");
  if (e.dataTransfer?.files.length) upload(e.dataTransfer.files);
});

function postFiles(fd, onProgress) {
  return new Promise((resolve, reject) => {
    const x = new XMLHttpRequest();
    x.open("POST", "/api/documents");
    x.upload.onprogress = (e) => e.lengthComputable && onProgress(e.loaded / e.total);
    x.onload = () => {
      let body = null;
      try { body = JSON.parse(x.responseText); } catch {}
      if (x.status >= 200 && x.status < 300 && body) resolve(body);
      else reject(new Error(body?.detail || x.statusText || `HTTP ${x.status}`));
    };
    x.onerror = () => reject(new Error("网络错误，服务是否在运行？"));
    x.send(fd);
  });
}

function notice(kind, title, names = []) {
  const div = document.createElement("div");
  div.className = `notice ${kind}`;
  div.innerHTML = `<button class="close" title="关闭">×</button><strong>${esc(title)}</strong>${
    names.length ? `<ul>${names.map((n) => `<li>${esc(n)}</li>`).join("")}</ul>` : ""}`;
  div.querySelector(".close").addEventListener("click", () => div.remove());
  $("#notices").prepend(div);
}

let uploadChain = Promise.resolve();
function upload(fileList) {
  const all = [...fileList];
  if (!all.length) return;
  showView("library");
  const bad = all.filter((f) => !ACCEPT.some((ext) => f.name.toLowerCase().endsWith(ext)));
  const files = all.filter((f) => !bad.includes(f));
  if (bad.length) notice("bad", `不支持的文件类型，已跳过 ${bad.length} 个`, bad.map((f) => f.name));
  if (files.length) uploadChain = uploadChain.then(() => doUpload(files));
}

async function doUpload(files) {
  const total = files.reduce((s, f) => s + f.size, 0);
  const fd = new FormData();
  files.forEach((f) => fd.append("files", f));
  $("#uploadPanel").hidden = false;
  $("#uploadText").textContent = `正在上传 ${files.length} 个文件（${fmtSize(total)}）`;
  const setPct = (frac) => {
    $("#uploadBar").style.width = `${Math.round(frac * 100)}%`;
    $("#uploadPct").textContent = frac < 1 ? `${Math.round(frac * 100)}%` : "服务器校验中…";
  };
  setPct(0);
  try {
    const res = await postFiles(fd, setPct);
    const ok = res.filter((r) => !r.error && !r.duplicate).map((r) => r.filename);
    const dups = res.filter((r) => r.duplicate).map((r) => r.filename);
    const errs = res.filter((r) => r.error).map((r) => `${r.filename}：${r.error}`);
    if (ok.length) notice("ok", `已上传 ${ok.length} 个，正在排队解析`, ok);
    if (dups.length) notice("dup", `${dups.length} 个文件库里已有（内容相同），已跳过`, dups);
    if (errs.length) notice("bad", `${errs.length} 个上传失败`, errs);
  } catch (err) {
    notice("bad", `上传失败：${err.message}`, files.map((f) => f.name));
  } finally {
    $("#uploadPanel").hidden = true;
    refreshDocs();
  }
}

let pollTimer;
async function refreshDocs() {
  clearTimeout(pollTimer);
  try {
    state.docs = await api("/api/documents");
  } catch {
    pollTimer = setTimeout(refreshDocs, 5000);
    return;
  }
  renderDocs();
  pollTimer = setTimeout(refreshDocs, state.docs.some(busy) ? 2000 : 15000);
}

function renderDocs() {
  const docs = state.docs;
  const done = docs.filter((d) => d.status === "done");
  const nBusy = docs.filter(busy).length;
  $("#docCount").textContent = docs.length;
  $("#busyCount").hidden = !nBusy;
  $("#busyCount").textContent = `${nBusy} 处理中`;

  const sel = $("#docFilter");
  const cur = sel.value;
  sel.innerHTML = `<option value="">全部文档（${done.length}）</option>` +
    done.map((d) => `<option value="${d.id}">${esc(d.filename)}</option>`).join("");
  sel.value = done.some((d) => String(d.id) === cur) ? cur : "";

  $("#statusFilter").innerHTML = FILTERS.map(([key, label, fn]) =>
    `<button type="button" data-filter="${key}" class="${state.filter === key ? "on" : ""} ${key}">${label} <b>${docs.filter(fn).length}</b></button>`,
  ).join("");

  // 排队顺序：后台按 id 从小到大处理
  const queue = docs.filter((d) => d.status === "queued").map((d) => d.id).sort((a, b) => a - b);
  const now = Date.now();
  const shown = docs.filter(FILTERS.find(([k]) => k === state.filter)[2]);
  $("#docRows").innerHTML = shown.length ? shown.map((d) => {
    const cls = d.status === "done" ? "done" : d.status === "failed" ? "failed" : "busy";
    let detail;
    if (d.status === "failed") {
      detail = `<div class="reason">${esc(d.message || "未知错误")}</div>`;
    } else if (d.status === "done") {
      detail = `<span class="muted">${d.chunk_count} 个片段${d.ocr_pages ? ` · OCR ${d.ocr_pages} 页` : ""}</span>`;
    } else if (d.status === "queued") {
      detail = `<span class="muted">排队第 ${queue.indexOf(d.id) + 1} 位</span>`;
    } else {
      const pct = Math.round((d.progress || 0) * 100);
      detail = `<div class="bar"><i style="width:${pct}%"></i></div><span class="muted">${pct}%${d.message ? ` · ${esc(d.message)}` : ""}</span>`;
    }
    const start = parseTime(d.started_at);
    const end = parseTime(d.finished_at);
    const dur = start && end ? fmtDur(end - start) : start && busy(d) ? `已用 ${fmtDur(now - start)}` : "–";
    const pages = d.pages ? d.pages : "–";
    return `<tr class="${cls}">
      <td class="name" title="${esc(d.filename)}">${esc(d.filename)}</td>
      <td class="num">${fmtSize(d.size)}</td>
      <td class="num">${pages}</td>
      <td><span class="label ${cls}">${STATUS[d.status] || esc(d.status)}</span></td>
      <td class="detail">${detail}</td>
      <td class="time">${fmtTime(d.created_at)}</td>
      <td class="time">${fmtTime(d.finished_at)}</td>
      <td class="num">${dur}</td>
      <td class="actions">
        <a class="btn small" href="/api/documents/${d.id}/file" download>下载</a>
        <button class="btn small" data-reindex="${d.id}" ${busy(d) ? "disabled" : ""}>重新解析</button>
        <button class="btn small danger" data-del="${d.id}">删除</button>
      </td></tr>`;
  }).join("") : `<tr><td colspan="9" class="hint">${docs.length ? "没有符合条件的文件" : "还没有文件，点击上方上传"}</td></tr>`;
}

$("#docRows").addEventListener("click", async (e) => {
  const re = e.target.closest("[data-reindex]");
  const del = e.target.closest("[data-del]");
  try {
    if (re) {
      await api(`/api/documents/${re.dataset.reindex}/reindex`, { method: "POST" });
    } else if (del) {
      const d = state.docs.find((x) => String(x.id) === del.dataset.del);
      if (!confirm(`确定删除「${d?.filename}」？索引和文件都会一起删除。`)) return;
      await api(`/api/documents/${del.dataset.del}`, { method: "DELETE" });
      if (viewer.docId === +del.dataset.del) {
        viewer.token++;
        viewer.pdf?.destroy();
        Object.assign(viewer, { pdf: null, docId: null, pages: [] });
        pagesEl.innerHTML = `<div class="empty">文档已删除</div>`;
        $("#viewerBar").hidden = true;
      }
    }
  } catch (err) {
    alert(`操作失败：${err.message}`);
  }
  refreshDocs();
});

$("#reindexAll").addEventListener("click", async () => {
  if (!confirm("把所有已完成/失败的文件重新解析一遍？文件多时需要较长时间，期间原有结果仍可检索。")) return;
  try {
    const r = await api("/api/documents/reindex", { method: "POST" });
    notice("ok", `已加入解析队列：${r.queued} 个文件`);
  } catch (err) {
    alert(`操作失败：${err.message}`);
  }
  refreshDocs();
});

// ------------------------------------------------------------------ 服务状态

async function refreshHealth() {
  try {
    const h = await api("/api/health");
    const items = [["OCR（MinerU）", h.mineru ? "up" : ""]];
    if (h.semantic) items.push(["Embedding", h.embedding ? "up" : ""]);
    $("#health").innerHTML = items.map(([n, c]) => `<span class="${c}">${n}</span>`).join("");
    $("#health").title = `已入库 ${h.documents} 份文档，${h.chunks} 个片段；OCR 服务只影响扫描件解析`;
  } catch {
    $("#health").innerHTML = `<span>服务未连接</span>`;
  }
}

showView(location.hash === "#library" ? "library" : "search");
refreshDocs();
refreshHealth();
setInterval(refreshHealth, 30000);
$("#results").innerHTML = `<li class="hint">输入要查找的原文开始检索<br><small>原文必须逐字包含查询词；多个词用空格分隔。按 / 快速聚焦搜索框</small></li>`;
