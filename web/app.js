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
const state = { data: null, active: null };

$("#searchForm").addEventListener("submit", (e) => {
  e.preventDefault();
  const pick = history_.items[history_.sel];
  if (history_.open && pick !== undefined) $("#q").value = pick; // 方向键选中了历史记录
  hideHistory();
  runSearch();
});

const HINT = `<li class="hint">输入要查找的原文开始检索<br><small>在全部文件的正文和文件名里逐字查找；多个词用空格分隔。<br>按 / 聚焦搜索框，Esc 清除搜索，F3 / Shift+F3 在原文里跳到下一处 / 上一处</small></li>`;

function clearSearch() {
  $("#q").value = "";
  state.data = null;
  state.active = null;
  $("#meta").textContent = "";
  $("#results").innerHTML = HINT;
  $("#qClear").hidden = true;
  closeViewer("点击左侧检索结果，在这里查看原文并定位关键字");
  $("#q").focus();
}
$("#qClear").addEventListener("click", clearSearch);

// ---- 搜索历史：存在各自浏览器里，最多 30 条，最近的在前
const HISTORY_KEY = "searchHistory";
const HISTORY_MAX = 30;
const history_ = { items: [], sel: -1, open: false };

function loadHistory() {
  try { return JSON.parse(localStorage.getItem(HISTORY_KEY)) || []; } catch { return []; }
}
function saveHistory(list) {
  try { localStorage.setItem(HISTORY_KEY, JSON.stringify(list)); } catch {}
}
function addHistory(q) {
  saveHistory([q, ...loadHistory().filter((x) => x !== q)].slice(0, HISTORY_MAX));
}

function showHistory() {
  const typed = $("#q").value.trim().toLowerCase();
  history_.items = loadHistory().filter((h) => !typed || (h.toLowerCase().includes(typed) && h.toLowerCase() !== typed));
  history_.sel = -1;
  history_.open = history_.items.length > 0;
  const box = $("#history");
  box.hidden = !history_.open;
  if (!history_.open) return;
  const hl = highlighter(typed ? [typed] : []);
  box.innerHTML = `<div class="history-head"><span>搜索历史</span><button type="button" class="link" data-clear-all>清空</button></div>` +
    history_.items.map((h, i) => `<div class="history-item" data-i="${i}"><span class="history-text">${hl(h)}</span>` +
      `<button type="button" class="history-del" data-del="${i}" title="删除这条">×</button></div>`).join("");
}
function hideHistory() {
  history_.open = false;
  history_.sel = -1;
  $("#history").hidden = true;
}
function moveHistory(delta) {
  if (!history_.open) return showHistory();
  // 在 -1（不选，用输入框里的内容）和 0..n-1 之间循环
  const n = history_.items.length;
  history_.sel += delta;
  if (history_.sel >= n) history_.sel = -1;
  else if (history_.sel < -1) history_.sel = n - 1;
  document.querySelectorAll("#history .history-item").forEach((el) => el.classList.toggle("sel", +el.dataset.i === history_.sel));
}

$("#q").addEventListener("focus", showHistory);
$("#q").addEventListener("input", () => { $("#qClear").hidden = !$("#q").value; showHistory(); });
$("#q").addEventListener("blur", () => setTimeout(hideHistory, 150));
$("#q").addEventListener("keydown", (e) => {
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    moveHistory(e.key === "ArrowDown" ? 1 : -1);
  } else if (e.key === "Escape") {
    e.preventDefault();
    if (history_.open) hideHistory();
    else clearSearch();
  }
});
// 点历史记录时不让输入框失焦，否则 blur 先把列表关掉
$("#history").addEventListener("mousedown", (e) => e.preventDefault());
$("#history").addEventListener("click", (e) => {
  const del = e.target.closest("[data-del]");
  if (del) {
    const h = history_.items[+del.dataset.del];
    saveHistory(loadHistory().filter((x) => x !== h));
    showHistory();
    return;
  }
  if (e.target.closest("[data-clear-all]")) {
    saveHistory([]);
    hideHistory();
    return;
  }
  const item = e.target.closest(".history-item");
  if (!item) return;
  $("#q").value = history_.items[+item.dataset.i];
  $("#qClear").hidden = false;
  hideHistory();
  runSearch();
});
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
    addHistory(q);
    state.data = data;
    state.active = null;
    $("#meta").textContent = data.total_docs
      ? `${data.total_docs} 个文件 · ${data.total_chunks} 段 · 共 ${data.total_hits} 处 · ${data.took_ms} ms`
      : "";
    renderResults(data);
    // 右侧还开着上一次检索的文件：在新结果里就按新查询词重新高亮，不在就关掉，免得看到旧的高亮
    if (viewer.docId !== null) {
      const idx = data.docs.findIndex((d) => d.doc_id === viewer.docId);
      if (idx >= 0) openDoc(idx, null);
      else closeViewer("点击左侧检索结果，在这里查看原文并定位关键字");
    }
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
// 摘要：第一处命中前 20 字、后 60 字左右，换行和表格分隔合成一个空格；全文在"再点一次"时展开
const SNIP_BEFORE = 20;
const SNIP_LEN = 80;
function snippet(text, terms) {
  const flat = text.replace(/\s*\|\s*/g, " | ").replace(/\s+/g, " ").trim();
  const low = flat.toLowerCase();
  const at = Math.min(...terms.map((t) => low.indexOf(t)).filter((i) => i >= 0));
  const start = Number.isFinite(at) ? Math.max(0, at - SNIP_BEFORE) : 0;
  const end = Math.min(flat.length, start + SNIP_LEN);
  return `${start > 0 ? "…" : ""}${flat.slice(start, end)}${end < flat.length ? "…" : ""}`;
}

function renderResults(data) {
  const ol = $("#results");
  if (!data.docs.length) {
    ol.innerHTML = `<li class="hint">没有找到包含「${esc(data.query)}」的文件。<br>多个词用空格分隔时，每个词都要出现在同一段里（或文件名里）。</li>`;
    return;
  }
  const hl = highlighter(data.terms);
  ol.innerHTML = data.docs.map((d, i) => {
    Object.assign(d, { preview: d.chunks.length, collapsed: false, loading: false, error: null });
    return `<li class="doc" data-idx="${i}">
      <div class="doc-head">
        <span class="doc-name" title="${esc(d.filename)}">${hl(d.filename)}</span>
        ${d.filename_match ? '<span class="tag name">文件名命中</span>' : ""}
        <span class="doc-count">${d.hit_count ? `${d.chunk_count} 段 · ${d.hit_count} 处` : ""}</span>
      </div>
      ${d.chunks.length ? `<ol class="hits">${d.chunks.map((c, j) => hitHtml(c, d.chunks[j - 1], hl, data.terms)).join("")}</ol>
        <div class="doc-foot">${footHtml(d)}</div>`
        : '<div class="hit-none">正文里没有查询词，只有文件名命中</div>'}
    </li>`;
  }).join("");
}

// 卡片底部：继续展开 / 收起 / 展开全部。收起时只显示前 preview 段（样式见 .doc.collapsed），数据不丢，再展开不用重新加载
function footHtml(d) {
  if (d.loading) return '<span class="muted">加载中…</span>';
  const rest = d.chunk_count - d.chunks.length;
  if (d.collapsed) return `<button class="more" type="button" data-act="expand">展开全部 ${d.chunk_count} 段</button>`;
  const btns = [];
  if (rest > 0) {
    const label = d.error ? `加载失败（${esc(d.error)}），点击重试`
      : d.chunks.length > d.preview ? `还有 ${rest} 段，继续展开` : `还有 ${rest} 段命中，展开`;
    btns.push(`<button class="more" type="button" data-act="more">${label}</button>`);
  }
  if (d.chunks.length > d.preview) btns.push('<button class="more" type="button" data-act="collapse">收起</button>');
  return btns.join("");
}

function renderFoot(idx) {
  const d = state.data.docs[idx];
  const card = document.querySelector(`#results .doc[data-idx="${idx}"]`);
  if (!card) return;
  card.classList.toggle("collapsed", d.collapsed);
  card.querySelector(".doc-foot").innerHTML = footHtml(d);
}

// prev：同一文件里的上一段。标题和上一段相同就不重复显示（右侧正在看的那段照常显示）
function hitHtml(c, prev, hl, terms) {
  const where = c.pages.length > 1 ? `第 ${c.pages[0]}–${c.pages.at(-1)} 页` : `第 ${c.page} 页`;
  const same = prev && prev.heading === c.heading;
  return `<li class="hit${same ? " same-heading" : ""}" data-chunk="${c.chunk_id}">
    <div class="hit-meta"><span class="hit-page">${where}</span>${c.kind === "table" ? '<span class="tag table">表格</span>' : ""}
      ${c.heading ? `<span class="hit-heading" title="${esc(c.heading)}">${esc(c.heading)}</span>` : ""}
      ${c.count > 1 ? `<span class="hit-n">本段 ${c.count} 处</span>` : ""}</div>
    <div class="hit-text">${hl(snippet(c.text, terms))}</div>
    <div class="hit-full">${hl(c.text)}</div>
  </li>`;
}

// 展开：从后端接着取这个文件剩下的段落，一次 MORE_PAGE 段，直到取完
// 同一个文件同时只加载一批（手动点"展开"和右侧跳转触发的自动展开可能撞在一起）
const moreLoading = new Map();
function loadMore(idx) {
  if (!moreLoading.has(idx)) {
    moreLoading.set(idx, loadMoreOnce(idx).finally(() => moreLoading.delete(idx)));
  }
  return moreLoading.get(idx);
}

// 返回是否加载到了新段落
async function loadMoreOnce(idx) {
  const data = state.data;
  const d = data.docs[idx];
  if (d.chunks.length >= d.chunk_count) return false;
  d.loading = true;
  d.error = null;
  renderFoot(idx);
  try {
    const res = await api(`/api/documents/${d.doc_id}/hits?${new URLSearchParams({ q: data.query, offset: d.chunks.length, limit: MORE_PAGE })}`);
    if (state.data !== data) return false; // 期间又检索了别的
    const before = d.chunks.length;
    d.chunks.push(...res.chunks);
    d.chunk_count = res.chunks.length ? res.total : d.chunks.length; // 期间文件被重新解析、段落变少了
    const hl = highlighter(data.terms);
    document.querySelector(`#results .doc[data-idx="${idx}"] .hits`)
      .insertAdjacentHTML("beforeend", res.chunks.map((c, j) => hitHtml(c, d.chunks[before + j - 1], hl, data.terms)).join(""));
    return res.chunks.length > 0;
  } catch (err) {
    d.error = err.message;
    return false;
  } finally {
    if (state.data === data) {
      d.loading = false;
      renderFoot(idx);
    }
  }
}

$("#results").addEventListener("click", (e) => {
  const li = e.target.closest(".doc");
  if (!li) return;
  const idx = +li.dataset.idx;
  const act = e.target.closest("[data-act]")?.dataset.act;
  if (act) {
    const d = state.data.docs[idx];
    if (act === "collapse") {
      d.collapsed = true;
      renderFoot(idx);
      // 收起后卡片可能整个跑到上面去了，滚回来
      li.scrollIntoView({ block: "nearest" });
    } else if (act === "expand") {
      d.collapsed = false;
      renderFoot(idx);
    } else {
      // 新加载的段落里可能有右侧正在看的那段
      loadMore(idx).then(() => state.active === idx && viewer.cur >= 0 && markCurrent());
    }
    return;
  }
  if (e.target.closest(".doc-foot")) return;
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
  markCurrent(); // 先清掉上一个文件里标着的段落，新文件的命中加载完再标
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
  viewer.hitsQuery = null;
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
  let found = false;
  document.querySelectorAll("#results .hit").forEach((el) => {
    const on = +el.closest(".doc").dataset.idx === state.active && +el.dataset.chunk === h?.chunk_id;
    found ||= on;
    el.classList.toggle("current", on);
    if (!on) el.classList.remove("expanded");
    if (on && scroll) el.scrollIntoView({ block: "nearest", behavior: "smooth" });
  });
  // 右侧跳到的段落左侧还没加载：自动加载（一批一批，直到这段出现），再标出来。
  // 卡片收起着也没关系：正在看的那段不受收起影响，照常显示
  const d = state.active === null ? null : state.data?.docs[state.active];
  if (h && !found && d && d.chunks.length < d.chunk_count) {
    const idx = state.active;
    loadMore(idx).then((loaded) => {
      if (loaded && state.active === idx && viewer.hits[viewer.cur] === h) markCurrent(scroll);
    });
  }
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

function closeViewer(message) {
  viewer.token++;
  viewer.hlToken++;
  viewer.observer?.disconnect();
  viewer.pdf?.destroy();
  Object.assign(viewer, { pdf: null, docId: null, pages: [] });
  setHits([]);
  pagesEl.innerHTML = `<div class="empty"><p>${esc(message)}</p></div>`;
  $("#viewerBar").hidden = true;
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
const FILTERS = [["all", "全部"], ["busy", "处理中"], ["done", "成功"], ["failed", "失败"]];

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

// 文件库列表：服务端分页；选中的文件跨页保留，换筛选条件时清空
const lib = { page: 1, pageSize: 50, status: "all", q: "", data: null, selected: new Set() };
try { lib.pageSize = +localStorage.getItem("libPageSize") || 50; } catch {}

const serverNow = () => Date.now() + (lib.clockOffset || 0);

// 处理中文件的"已用时间"每秒走一次（列表本身 2 秒刷新一次）
setInterval(() => {
  document.querySelectorAll("#docRows [data-started]").forEach((td) => {
    td.textContent = `已用 ${fmtDur(serverNow() - parseTime(td.dataset.started))}`;
  });
}, 1000);

let pollTimer;
async function refreshDocs() {
  clearTimeout(pollTimer);
  const params = new URLSearchParams({ page: lib.page, page_size: lib.pageSize, status: lib.status, q: lib.q });
  let data;
  try {
    data = await api(`/api/documents?${params}`);
  } catch {
    pollTimer = setTimeout(refreshDocs, 5000);
    return;
  }
  const pages = Math.max(1, Math.ceil(data.total / lib.pageSize));
  if (lib.page > pages) { // 删除后当前页可能没了
    lib.page = pages;
    return refreshDocs();
  }
  lib.data = data;
  lib.clockOffset = parseTime(data.now) - Date.now(); // 服务器时钟 - 本机时钟
  renderDocs();
  const summarizing = data.items.some((d) => d.summary_status === "queued" || d.summary_status === "running");
  pollTimer = setTimeout(refreshDocs, data.overall.busy ? 2000 : summarizing ? 4000 : 15000);
}

function resetList() {
  lib.page = 1;
  lib.selected.clear();
  refreshDocs();
}

$("#statusFilter").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-filter]");
  if (!b || b.dataset.filter === lib.status) return;
  lib.status = b.dataset.filter;
  resetList();
});

let libQTimer;
$("#libQ").addEventListener("input", (e) => {
  clearTimeout(libQTimer);
  libQTimer = setTimeout(() => { lib.q = e.target.value.trim(); resetList(); }, 300);
});

function renderDocs() {
  const { items, total, counts, overall } = lib.data;
  $("#docCount").textContent = overall.all;
  $("#busyCount").hidden = !overall.busy;
  $("#busyCount").textContent = `${overall.busy} 处理中`;

  $("#statusFilter").innerHTML = FILTERS.map(([key, label]) =>
    `<button type="button" data-filter="${key}" class="${lib.status === key ? "on" : ""} ${key}">${label} <b>${counts[key]}</b></button>`,
  ).join("");

  const now = serverNow();
  $("#docRows").innerHTML = items.length ? items.map((d) => {
    const cls = d.status === "done" ? "done" : d.status === "failed" ? "failed" : "busy";
    let detail;
    if (d.status === "failed") {
      detail = `<div class="reason">${esc(d.message || "未知错误")}</div>`;
    } else if (d.status === "done") {
      detail = `<span class="muted">${d.chunk_count} 个片段${d.ocr_pages ? ` · OCR ${d.ocr_pages} 页` : ""}</span>`;
    } else if (d.status === "queued") {
      detail = `<span class="muted">排队第 ${d.queue_pos} 位</span>`;
    } else {
      const pct = Math.round((d.progress || 0) * 100);
      detail = `<div class="bar"><i style="width:${pct}%"></i></div><span class="muted">${pct}%${d.message ? ` · ${esc(d.message)}` : ""}</span>`;
    }
    const start = parseTime(d.started_at);
    const end = parseTime(d.finished_at);
    const running = start && !end && busy(d);
    const dur = start && end ? fmtDur(end - start) : running ? `已用 ${fmtDur(now - start)}` : "–";
    const sel = lib.selected.has(d.id);
    return `<tr class="${cls}${sel ? " selected" : ""}">
      <td class="check"><input type="checkbox" data-sel="${d.id}" ${sel ? "checked" : ""}></td>
      <td class="name" title="${esc(d.filename)}">${esc(d.filename)}</td>
      <td class="num">${fmtSize(d.size)}</td>
      <td class="num">${d.pages || "–"}</td>
      <td><span class="label ${cls}">${STATUS[d.status] || esc(d.status)}</span></td>
      <td class="detail">${detail}</td>
      <td class="time">${fmtTime(d.created_at)}</td>
      <td class="time">${fmtTime(d.finished_at)}</td>
      <td class="num"${running ? ` data-started="${esc(d.started_at)}"` : ""}>${dur}</td>
      <td class="actions">
        ${summaryButton(d)}
        <a class="btn small" href="/api/documents/${d.id}/file" download>下载</a>
        <button class="btn small" data-reindex="${d.id}" ${busy(d) ? "disabled" : ""}>重新解析</button>
        <button class="btn small danger" data-del="${d.id}">删除</button>
      </td></tr>`;
  }).join("") : `<tr><td colspan="10" class="hint">${overall.all ? "没有符合条件的文件" : "还没有文件，点击上方上传"}</td></tr>`;

  renderSelection();
  renderPager(total);
}

function renderSelection() {
  const { items, total } = lib.data;
  const n = lib.selected.size;
  const onPage = items.filter((d) => lib.selected.has(d.id)).length;
  const head = $("#selPage");
  head.checked = items.length > 0 && onPage === items.length;
  head.indeterminate = onPage > 0 && onPage < items.length;
  document.querySelectorAll("#docRows [data-sel]").forEach((cb) => {
    cb.checked = lib.selected.has(+cb.dataset.sel);
    cb.closest("tr").classList.toggle("selected", cb.checked);
  });
  $("#selBar").hidden = !n;
  $("#selText").textContent = `已选 ${n} 个文件`;
  // 本页全选了、但筛选结果不止这一页：提供"选中全部"
  const canAll = head.checked && n < total;
  $("#selAll").hidden = !canAll;
  $("#selAll").textContent = `选中全部 ${total} 个${lib.status !== "all" || lib.q ? "符合条件的" : ""}文件`;
}

function renderPager(total) {
  const pages = Math.max(1, Math.ceil(total / lib.pageSize));
  const cur = lib.page;
  const nums = [...new Set([1, cur - 2, cur - 1, cur, cur + 1, cur + 2, pages])].filter((x) => x >= 1 && x <= pages).sort((a, b) => a - b);
  let prev = 0;
  const btns = nums.map((x) => {
    const gap = x - prev > 1 ? '<span class="gap">…</span>' : "";
    prev = x;
    return `${gap}<button type="button" data-page="${x}" class="${x === cur ? "on" : ""}">${x}</button>`;
  }).join("");
  // 正在输入跳转页码时不重画（处理中的文件每 2 秒刷新一次列表，会把输入冲掉）
  if (document.activeElement?.id === "jumpPage") return;
  const all = lib.data.overall.all;
  const filtered = lib.status !== "all" || lib.q;
  const summary = filtered
    ? `筛选出 <b>${total}</b> 个文件<span class="muted">（全部 ${all} 个）</span>`
    : `共 <b>${total}</b> 个文件`;
  $("#pager").innerHTML = `
    <span class="pager-info">${summary} · 第 <b>${cur}</b> / ${pages} 页</span>
    <span class="pages-btns">
      <button type="button" data-page="${cur - 1}" ${cur <= 1 ? "disabled" : ""}>‹ 上一页</button>${btns}
      <button type="button" data-page="${cur + 1}" ${cur >= pages ? "disabled" : ""}>下一页 ›</button>
    </span>
    <span class="pager-tools">
      <label>每页 <select id="pageSize">${[20, 50, 100, 200].map((n) =>
        `<option ${n === lib.pageSize ? "selected" : ""}>${n}</option>`).join("")}</select> 个</label>
      <label>跳至 <input id="jumpPage" type="number" min="1" max="${pages}" inputmode="numeric"> 页</label>
      <button type="button" class="btn small" id="jumpGo">跳转</button>
    </span>`;
}

function jumpToPage() {
  const input = $("#jumpPage");
  const pages = Math.max(1, Math.ceil(lib.data.total / lib.pageSize));
  const n = Math.round(+input.value);
  if (!input.value || !Number.isFinite(n)) return input.focus();
  input.value = "";
  input.blur();
  lib.page = Math.min(Math.max(1, n), pages); // 超出范围的跳到首页/末页
  refreshDocs();
  $(".lib-table-wrap").scrollTop = 0;
}

$("#pager").addEventListener("keydown", (e) => {
  if (e.target.id === "jumpPage" && e.key === "Enter") jumpToPage();
});
$("#pager").addEventListener("click", (e) => {
  if (e.target.id === "jumpGo") return jumpToPage();
  const b = e.target.closest("button[data-page]");
  if (!b || b.disabled || +b.dataset.page === lib.page) return;
  lib.page = +b.dataset.page;
  refreshDocs();
  $(".lib-table-wrap").scrollTop = 0;
});
$("#pager").addEventListener("change", (e) => {
  if (e.target.id !== "pageSize") return;
  lib.pageSize = +e.target.value;
  try { localStorage.setItem("libPageSize", lib.pageSize); } catch {}
  lib.page = 1;
  refreshDocs();
});

$("#selPage").addEventListener("change", (e) => {
  lib.data.items.forEach((d) => (e.target.checked ? lib.selected.add(d.id) : lib.selected.delete(d.id)));
  renderSelection();
});
$("#selAll").addEventListener("click", async () => {
  try {
    const ids = await api(`/api/documents/ids?${new URLSearchParams({ status: lib.status, q: lib.q })}`);
    ids.forEach((id) => lib.selected.add(id));
    renderSelection();
  } catch (err) {
    alert(`操作失败：${err.message}`);
  }
});
$("#selClear").addEventListener("click", () => { lib.selected.clear(); renderSelection(); });

$("#selDelete").addEventListener("click", async () => {
  const ids = [...lib.selected];
  if (!confirm(`确定删除选中的 ${ids.length} 个文件？索引和文件都会一起删除，无法恢复。`)) return;
  await withBusy($("#selDelete"), "删除中…", async () => {
    const r = await api("/api/documents/delete", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ids }),
    });
    notice("ok", `已删除 ${r.deleted} 个文件`);
    afterDelete(ids);
  });
});
$("#selReindex").addEventListener("click", async () => {
  const ids = [...lib.selected];
  await withBusy($("#selReindex"), "提交中…", async () => {
    const r = await api("/api/documents/reindex", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ids }),
    });
    const skipped = ids.length - r.queued;
    notice("ok", `已加入解析队列：${r.queued} 个文件${skipped ? `（${skipped} 个正在处理中，已跳过）` : ""}`);
    lib.selected.clear();
  });
});

async function withBusy(btn, text, fn) {
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = text;
  try {
    await fn();
  } catch (err) {
    alert(`操作失败：${err.message}`);
  } finally {
    btn.disabled = false;
    btn.textContent = old;
    refreshDocs();
  }
}

// 删掉的文件如果正在预览、或在检索结果里，一并清掉
function afterDelete(ids) {
  const gone = new Set(ids.map(Number));
  gone.forEach((id) => lib.selected.delete(id));
  if (gone.has(viewer.docId)) closeViewer("文档已删除");
  if (state.data?.docs.some((d) => gone.has(d.doc_id))) runSearch();
}

$("#docRows").addEventListener("change", (e) => {
  const cb = e.target.closest("[data-sel]");
  if (!cb) return;
  (cb.checked ? lib.selected.add(+cb.dataset.sel) : lib.selected.delete(+cb.dataset.sel));
  renderSelection();
});

$("#docRows").addEventListener("click", async (e) => {
  const re = e.target.closest("[data-reindex]");
  const del = e.target.closest("[data-del]");
  if (!re && !del) return;
  try {
    if (re) {
      await api(`/api/documents/${re.dataset.reindex}/reindex`, { method: "POST" });
    } else {
      const d = lib.data.items.find((x) => String(x.id) === del.dataset.del);
      if (!confirm(`确定删除「${d?.filename}」？索引和文件都会一起删除。`)) return;
      await api(`/api/documents/${del.dataset.del}`, { method: "DELETE" });
      afterDelete([+del.dataset.del]);
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

// ------------------------------------------------------------------ 文档概述（大模型）

let llmEnabled = false;
const SUMMARY_LABEL = { queued: "概述排队中", running: "概述生成中", failed: "概述失败", done: "概述" };

function summaryButton(d) {
  if (d.status !== "done" || (!llmEnabled && d.summary_status !== "done")) return "";
  const st = d.summary_status;
  const cls = st === "failed" ? " danger" : st === "queued" || st === "running" ? " pending" : "";
  const title = st === "failed" ? d.summary_message || "" : st === "running" ? d.summary_message || "" : "";
  const label = st === "running" ? `概述 ${Math.round((d.summary_progress || 0) * 100)}%` : SUMMARY_LABEL[st] || "生成概述";
  return `<button class="btn small${cls}" data-summary="${d.id}" title="${esc(title)}">${label}</button>`;
}

// 极简 Markdown：标题、列表、加粗；先转义，不会注入 HTML
function renderMarkdown(md) {
  const inline = (s) => esc(s).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  const out = [];
  let list = null;
  const close = () => { if (list) { out.push(`</${list}>`); list = null; } };
  for (const raw of md.split("\n")) {
    const line = raw.trim();
    let m;
    if (!line) { close(); continue; }
    if ((m = line.match(/^#{1,6}\s+(.*)$/))) { close(); out.push(`<h3>${inline(m[1])}</h3>`); continue; }
    if ((m = line.match(/^(?:[-*•·]|\d+[.、)])\s*(.*)$/))) {
      const tag = /^\d/.test(line) ? "ol" : "ul";
      if (list !== tag) { close(); out.push(`<${tag}>`); list = tag; }
      out.push(`<li>${inline(m[1])}</li>`);
      continue;
    }
    close();
    out.push(`<p>${inline(line)}</p>`);
  }
  close();
  return out.join("");
}

const sumDlg = { docId: null, timer: null, text: "", offset: 0 };

// 概述生成中的"已用时间"每秒走一次（按服务器时钟）
setInterval(() => {
  const el = document.querySelector("#sumStatus [data-started]");
  if (el) el.textContent = fmtDur(Date.now() + sumDlg.offset - parseTime(el.dataset.started));
}, 1000);

async function openSummary(docId) {
  sumDlg.docId = docId;
  sumDlg.text = "";
  $("#sumBody").innerHTML = "";
  $("#sumStatus").textContent = "加载中…";
  if (!$("#summaryDlg").open) $("#summaryDlg").showModal();
  await loadSummary();
}

async function loadSummary() {
  clearTimeout(sumDlg.timer);
  const docId = sumDlg.docId;
  let r;
  try {
    r = await api(`/api/documents/${docId}/summary`);
  } catch (err) {
    $("#sumStatus").textContent = `加载失败：${err.message}`;
    return;
  }
  if (docId !== sumDlg.docId || !$("#summaryDlg").open) return;
  sumDlg.offset = parseTime(r.now) - Date.now();
  $("#sumFile").textContent = r.filename;
  $("#sumFile").title = r.filename;
  const status = $("#sumStatus");
  status.className = "sum-status";
  const busyNow = r.status === "queued" || r.status === "running";
  if (!r.llm && r.status !== "done") {
    status.textContent = "没有配置大模型（DOCSEARCH_LLM_URL），无法生成概述";
  } else if (r.doc_status !== "done") {
    status.textContent = "文件还在解析，解析完成后自动生成概述";
  } else if (r.status === "queued") {
    status.textContent = r.summary ? "文件已重新解析，正在排队重新生成（下面是上一次的概述）" : "排队中，前面的文件生成完就开始";
  } else if (r.status === "running") {
    const pct = Math.round((r.progress || 0) * 100);
    const elapsed = r.started_at ? fmtDur(Date.now() + sumDlg.offset - parseTime(r.started_at)) : "";
    status.innerHTML = `<div class="sum-prog"><div class="bar"><i style="width:${pct}%"></i></div><b>${pct}%</b></div>
      <div>${esc(r.message || "准备中")}${r.started_at ? ` · 已用 <span data-started="${esc(r.started_at)}">${elapsed}</span>` : ""}</div>`;
  } else if (r.status === "failed") {
    status.className = "sum-status bad";
    status.textContent = `生成失败：${r.message || "未知错误"}`;
  } else if (r.status === "done") {
    status.textContent = `生成于 ${r.summary_at}`;
  } else {
    status.textContent = "还没有生成概述，点击下方“重新生成”开始";
  }
  sumDlg.text = r.summary || "";
  // 生成最后一步时，边生成边显示草稿
  const drafting = r.status === "running" && r.draft;
  $("#sumBody").classList.toggle("drafting", !!drafting);
  $("#sumBody").innerHTML = drafting ? renderMarkdown(r.draft) : r.summary ? renderMarkdown(r.summary) : "";
  if (drafting) $("#sumBody").scrollTop = $("#sumBody").scrollHeight;
  $("#sumRegen").hidden = !r.llm || r.doc_status !== "done";
  $("#sumRegen").disabled = busyNow;
  $("#sumCopy").disabled = !r.summary;
  if (busyNow || r.doc_status !== "done") sumDlg.timer = setTimeout(loadSummary, r.status === "running" ? 1500 : 3000);
}

$("#sumClose").addEventListener("click", () => $("#summaryDlg").close());
$("#summaryDlg").addEventListener("close", () => { clearTimeout(sumDlg.timer); sumDlg.docId = null; });
$("#sumRegen").addEventListener("click", async () => {
  try {
    await api("/api/documents/summarize", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ids: [sumDlg.docId] }),
    });
  } catch (err) {
    alert(`操作失败：${err.message}`);
  }
  loadSummary();
  if (!$("#viewLibrary").hidden) refreshDocs();
});
$("#sumCopy").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(sumDlg.text);
    $("#sumCopy").textContent = "已复制";
  } catch {
    // 非 https 页面没有剪贴板权限：选中文字让用户手动复制
    getSelection().selectAllChildren($("#sumBody"));
    $("#sumCopy").textContent = "已选中，按 Ctrl+C 复制";
  }
  setTimeout(() => { $("#sumCopy").textContent = "复制"; }, 2000);
});
$("#viewerSummary").addEventListener("click", () => viewer.docId !== null && openSummary(viewer.docId));
$("#docRows").addEventListener("click", (e) => {
  const b = e.target.closest("[data-summary]");
  if (b) openSummary(+b.dataset.summary);
});
$("#selSummarize").addEventListener("click", async () => {
  const ids = [...lib.selected];
  await withBusy($("#selSummarize"), "提交中…", async () => {
    const r = await api("/api/documents/summarize", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ids }),
    });
    const skipped = ids.length - r.queued;
    notice("ok", `已加入概述队列：${r.queued} 个文件${skipped ? `（${skipped} 个未解析完成或正在生成，已跳过）` : ""}`);
    lib.selected.clear();
  });
});

// ------------------------------------------------------------------ 服务状态

async function refreshHealth() {
  try {
    const h = await api("/api/health");
    const items = [["OCR（MinerU）", h.mineru ? "up" : ""]];
    if (h.semantic) items.push(["Embedding", h.embedding ? "up" : ""]);
    if (h.llm.enabled) items.push([`大模型${h.llm.model ? `（${h.llm.model}）` : ""}`, h.llm.up ? "up" : ""]);
    if (llmEnabled !== h.llm.enabled) {
      llmEnabled = h.llm.enabled;
      document.querySelectorAll("[data-llm]").forEach((el) => { el.hidden = !llmEnabled; });
      if (lib.data) renderDocs(); // 文件库可能先于服务状态加载，补上"概述"按钮
    }
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
$("#results").innerHTML = HINT;
