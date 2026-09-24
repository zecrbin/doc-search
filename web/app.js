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

const MORE_PAGE = 500; // 展开时每次向后端取多少段
const state = { data: null, active: null, docs: [], filter: "all" };

$("#searchForm").addEventListener("submit", (e) => { e.preventDefault(); runSearch(); });
document.addEventListener("keydown", (e) => {
  const typing = ["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement.tagName);
  if (e.key === "/" && !typing) {
    e.preventDefault();
    showView("search");
    $("#q").focus();
  } else if (e.key === "F3" && viewer.hits.length) { // 和浏览器查找一样：F3 下一处，Shift+F3 上一处
    e.preventDefault();
    step(e.shiftKey ? -1 : 1);
  }
});

async function runSearch() {
  const q = $("#q").value.trim();
  if (!q) return;
  $("#meta").textContent = "检索中…";
  try {
    const data = await api(`/api/search?${new URLSearchParams({ q, mode: "keyword" })}`);
    state.data = data;
    state.active = null;
    $("#meta").textContent = data.total_docs
      ? `${data.total_docs} 个文件 · ${data.total_chunks} 段 · 共 ${data.total_hits} 处 · ${data.took_ms} ms`
      : "";
    renderResults(data);
  } catch (err) {
    $("#meta").textContent = "";
    $("#results").innerHTML = `<li class="hint">检索失败：${esc(err.message)}</li>`;
  }
}

function highlighter(terms) {
  const list = [...terms].sort((a, b) => b.length - a.length).map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  if (!list.length) return esc;
  const re = new RegExp(`(${list.join("|")})`, "gi");
  return (text) => text.split(re).map((part, i) => (i % 2 ? `<mark>${esc(part)}</mark>` : esc(part))).join("");
}

// 命中位置前后各截一段作为摘要
function snippet(text, terms) {
  const low = text.toLowerCase();
  const at = Math.min(...terms.map((t) => low.indexOf(t)).filter((i) => i >= 0));
  if (!Number.isFinite(at) || text.length <= 160) return text;
  const start = Math.max(0, at - 50);
  const end = Math.min(text.length, start + 160);
  return `${start > 0 ? "…" : ""}${text.slice(start, end)}${end < text.length ? "…" : ""}`;
}

function renderResults(data) {
  const ol = $("#results");
  if (!data.docs.length) {
    ol.innerHTML = `<li class="hint">没有找到包含「${esc(data.query)}」的文件。<br>多个词用空格分隔时，每个词都要出现在同一段里（或文件名里）。</li>`;
    return;
  }
  const hl = highlighter(data.terms);
  ol.innerHTML = data.docs.map((d, i) => {
    const rest = d.chunk_count - d.chunks.length;
    const more = rest > 0 ? `<button class="more" type="button">还有 ${rest} 段命中，展开</button>` : "";
    return `<li class="doc" data-idx="${i}">
      <div class="doc-head">
        <span class="doc-name" title="${esc(d.filename)}">${hl(d.filename)}</span>
        ${d.filename_match ? '<span class="tag name">文件名命中</span>' : ""}
        <span class="doc-count">${d.hit_count ? `${d.chunk_count} 段 · ${d.hit_count} 处` : ""}</span>
      </div>
      ${d.chunks.length ? `<ol class="hits">${d.chunks.map((c) => hitHtml(c, hl, data.terms)).join("")}</ol>${more}`
        : '<div class="hit-none">正文里没有查询词，只有文件名命中</div>'}
    </li>`;
  }).join("");
}

function hitHtml(c, hl, terms) {
  const where = c.pages.length > 1 ? `第 ${c.pages[0]}–${c.pages.at(-1)} 页` : `第 ${c.page} 页`;
  return `<li class="hit" data-chunk="${c.chunk_id}">
    <div class="hit-meta"><span class="hit-page">${where}</span>${c.kind === "table" ? '<span class="tag table">表格</span>' : ""}
      ${c.heading ? `<span class="hit-heading" title="${esc(c.heading)}">${esc(c.heading)}</span>` : ""}
      ${c.count > 1 ? `<span class="hit-n">本段 ${c.count} 处</span>` : ""}</div>
    <div class="hit-text">${hl(snippet(c.text, terms))}</div>
    <div class="hit-full">${hl(c.text)}</div>
  </li>`;
}

// 展开：从后端接着取这个文件剩下的段落，一次 MORE_PAGE 段，直到取完
async function loadMore(idx, btn) {
  const data = state.data;
  const d = data.docs[idx];
  btn.disabled = true;
  btn.textContent = "加载中…";
  try {
    const res = await api(`/api/documents/${d.doc_id}/hits?${new URLSearchParams({ q: data.query, offset: d.chunks.length, limit: MORE_PAGE })}`);
    if (state.data !== data) return; // 期间又检索了别的
    d.chunks.push(...res.chunks);
    d.chunk_count = res.total;
    const hl = highlighter(data.terms);
    btn.previousElementSibling.insertAdjacentHTML("beforeend", res.chunks.map((c) => hitHtml(c, hl, data.terms)).join(""));
    const rest = res.total - d.chunks.length;
    if (rest > 0 && res.chunks.length) {
      btn.disabled = false;
      btn.textContent = `还有 ${rest} 段命中，继续展开`;
    } else {
      btn.remove();
    }
    if (state.active === idx && viewer.cur >= 0) markCurrent(); // 新加载的段落里可能有正在看的那段
  } catch (err) {
    btn.disabled = false;
    btn.textContent = `加载失败（${err.message}），点击重试`;
  }
}

$("#results").addEventListener("click", (e) => {
  const li = e.target.closest(".doc");
  if (!li) return;
  const idx = +li.dataset.idx;
  const moreBtn = e.target.closest(".more");
  if (moreBtn) {
    loadMore(idx, moreBtn);
    return;
  }
  const hit = e.target.closest(".hit");
  if (hit) {
    // 再点一次正在看的段落：展开/收起全文
    if (hit.classList.contains("current")) hit.classList.toggle("expanded");
    openDoc(idx, +hit.dataset.chunk);
  } else {
    openDoc(idx, null);
  }
});

// ------------------------------------------------------------------ 原文预览

const viewer = {
  docId: null, pdf: null, pages: [], token: 0, observer: null,
  hlToken: 0, hits: [], cur: -1, // hits：当前文件的全部命中，按阅读顺序
};
const pagesEl = $("#pages");

$("#prevHit").addEventListener("click", () => step(-1));
$("#nextHit").addEventListener("click", () => step(1));

async function openDoc(idx, chunkId) {
  const d = state.data.docs[idx];
  const sameDoc = state.active === idx && viewer.docId === d.doc_id && viewer.hitsQuery === state.data.query;
  state.active = idx;
  document.querySelectorAll("#results .doc").forEach((el) => el.classList.toggle("active", +el.dataset.idx === idx));
  if (sameDoc) { // 同一个文件里跳到另一段
    const i = viewer.hits.findIndex((h) => h.chunk_id === chunkId);
    goTo(i >= 0 ? i : 0);
    return;
  }
  $("#viewerBar").hidden = false;
  $("#viewerTitle").textContent = d.filename;
  $("#viewerTitle").title = d.filename;
  $("#downloadOrig").href = `/api/documents/${d.doc_id}/file`;
  $("#viewerNote").hidden = true;
  setHits([]);
  const token = ++viewer.hlToken;
  const query = state.data.query;
  const hlReq = api(`/api/documents/${d.doc_id}/highlights?${new URLSearchParams({ q: query })}`)
    .catch(() => ({ hits: [], ocr: false }));
  if (viewer.docId !== d.doc_id) {
    const ok = await loadPdf(d.doc_id);
    if (!ok) return;
  }
  const res = await hlReq;
  if (token !== viewer.hlToken) return; // 期间又点了别的
  viewer.hitsQuery = query;
  const inexact = res.hits.filter((h) => !h.exact).length;
  const note = $("#viewerNote");
  note.hidden = !inexact && !res.ocr;
  note.className = `viewer-note${inexact ? "" : " soft"}`;
  note.textContent = inexact
    ? `${inexact} 处在扫描页上未能定位到关键字（旧文档需重新解析），框出的是整段`
    : "扫描件：按 OCR 识别位置定位";
  if (!res.hits.length) {
    note.hidden = false;
    note.className = "viewer-note soft";
    note.textContent = d.filename_match && !d.chunks.length ? "文件名命中，正文里没有查询词" : "原文里没有找到查询词";
  }
  setHits(res.hits);
  const i = chunkId === null ? 0 : viewer.hits.findIndex((h) => h.chunk_id === chunkId);
  if (viewer.hits.length) goTo(Math.max(0, i));
}

function setHits(hits) {
  viewer.hits = hits;
  viewer.cur = -1;
  drawHits();
  updateHitNav();
}

function drawHits() {
  pagesEl.querySelectorAll(".hl").forEach((x) => x.remove());
  viewer.hits.forEach((h, i) => {
    const pad = h.exact ? 1.5 : 3;
    for (const [pg, x0, y0, x1, y1] of h.boxes) {
      const p = viewer.pages[pg];
      if (!p) continue;
      const div = document.createElement("div");
      div.className = `hl ${h.exact ? "kw" : "para"}${i === viewer.cur ? " cur" : ""}`;
      div.dataset.hit = i;
      Object.assign(div.style, {
        left: `${x0 * p.scale - pad}px`, top: `${y0 * p.scale - pad}px`,
        width: `${(x1 - x0) * p.scale + pad * 2}px`, height: `${(y1 - y0) * p.scale + pad * 2}px`,
      });
      p.el.appendChild(div);
    }
  });
}

function goTo(i) {
  if (!viewer.hits.length) return;
  viewer.cur = (i + viewer.hits.length) % viewer.hits.length;
  pagesEl.querySelectorAll(".hl.cur").forEach((x) => x.classList.remove("cur", "flash"));
  pagesEl.querySelectorAll(`.hl[data-hit="${viewer.cur}"]`).forEach((x) => {
    x.classList.add("cur", "flash");
  });
  const h = viewer.hits[viewer.cur];
  const [pg, , y0] = h.boxes[0];
  const p = viewer.pages[pg];
  if (p) pagesEl.scrollTo({ top: p.el.offsetTop + y0 * p.scale - pagesEl.clientHeight / 3, behavior: "smooth" });
  updateHitNav();
  markCurrent(true);
}

// 左侧同步标出正在看的段落（还没展开加载的段落标不到，右侧照常跳转）
function markCurrent(scroll = false) {
  const h = viewer.hits[viewer.cur];
  const card = document.querySelector(`#results .doc[data-idx="${state.active}"]`);
  card?.querySelectorAll(".hit").forEach((el) => {
    const on = +el.dataset.chunk === h?.chunk_id;
    el.classList.toggle("current", on);
    if (!on) el.classList.remove("expanded");
    if (on && scroll) el.scrollIntoView({ block: "nearest", behavior: "smooth" });
  });
}

function step(delta) {
  goTo(viewer.cur + delta);
}

function updateHitNav() {
  const n = viewer.hits.length;
  $("#hitNav").hidden = !n;
  $("#hitPos").textContent = n ? `第 ${viewer.cur + 1} / ${n} 处` : "";
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
  drawHits();
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
  const nBusy = docs.filter(busy).length;
  $("#docCount").textContent = docs.length;
  $("#busyCount").hidden = !nBusy;
  $("#busyCount").textContent = `${nBusy} 处理中`;

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
        setHits([]);
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
$("#results").innerHTML = `<li class="hint">输入要查找的原文开始检索<br><small>在全部文件的正文和文件名里逐字查找；多个词用空格分隔。<br>按 / 聚焦搜索框，F3 / Shift+F3 在原文里跳到下一处 / 上一处</small></li>`;
