import * as pdfjsLib from "/static/vendor/pdfjs/pdf.min.mjs";

pdfjsLib.GlobalWorkerOptions.workerSrc = "/static/vendor/pdfjs/pdf.worker.min.mjs";
const PDF_OPTS = {
  cMapUrl: "/static/vendor/pdfjs/cmaps/",
  cMapPacked: true,
  standardFontDataUrl: "/static/vendor/pdfjs/standard_fonts/",
};

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

// ------------------------------------------------------------------ 检索

const state = { mode: "hybrid", results: [], activeIdx: -1, docs: [] };

$("#mode").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  state.mode = b.dataset.mode;
  document.querySelectorAll("#mode button").forEach((x) => x.classList.toggle("on", x === b));
  if ($("#q").value.trim()) runSearch();
});
$("#docFilter").addEventListener("change", () => $("#q").value.trim() && runSearch());
$("#searchForm").addEventListener("submit", (e) => { e.preventDefault(); runSearch(); });
document.addEventListener("keydown", (e) => {
  if (e.key === "/" && document.activeElement.tagName !== "INPUT") { e.preventDefault(); $("#q").focus(); }
});

async function runSearch() {
  const q = $("#q").value.trim();
  if (!q) return;
  const params = new URLSearchParams({ q, mode: state.mode, top_k: 30 });
  if ($("#docFilter").value) params.set("doc_id", $("#docFilter").value);
  $("#meta").textContent = "检索中…";
  try {
    const data = await api(`/api/search?${params}`);
    state.results = data.results;
    state.activeIdx = -1;
    const extra = data.reranked ? " · 已重排" : "";
    $("#meta").textContent = `${data.results.length} 条结果 · ${data.took_ms} ms${extra}`;
    renderResults(data);
  } catch (err) {
    $("#meta").textContent = "";
    $("#results").innerHTML = `<li class="hint">检索失败：${esc(err.message)}</li>`;
  }
}

function highlighter(data) {
  // 查询原词 + 分词结果；单字分词太碎，只在查询本身是单字时保留
  const words = new Set(data.query.toLowerCase().split(/\s+/).filter(Boolean));
  data.tokens.forEach((t) => (t.length > 1 || data.query.length === 1) && words.add(t));
  const list = [...words].sort((a, b) => b.length - a.length).map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  if (!list.length) return esc;
  const re = new RegExp(`(${list.join("|")})`, "gi");
  return (text) => text.split(re).map((part, i) => (i % 2 ? `<mark>${esc(part)}</mark>` : esc(part))).join("");
}

function renderResults(data) {
  const ol = $("#results");
  if (!data.results.length) {
    ol.innerHTML = `<li class="hint">没有找到相关内容。<br>试试换个说法，或切换到“综合 / 语义”模式。</li>`;
    return;
  }
  const hl = highlighter(data);
  ol.innerHTML = data.results.map((r, i) => {
    const pages = r.pages.length > 1 ? `第 ${r.pages[0]}–${r.pages.at(-1)} 页` : `第 ${r.page} 页`;
    const dups = r.duplicates.length
      ? `<details class="dups"><summary>相同段落还出现在 ${r.duplicates.length} 处</summary><ul>${
          r.duplicates.slice(0, 50).map((d, j) => `<li><a data-dup="${j}">${esc(d.filename)}</a> · 第 ${d.page} 页</li>`).join("")
        }</ul></details>`
      : "";
    return `<li class="result" data-idx="${i}">
      <div class="result-head">
        <span class="result-file" title="${esc(r.filename)}">${esc(r.filename)}</span>
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
  const r = state.results[+li.dataset.idx];
  const dupLink = e.target.closest("[data-dup]");
  if (dupLink) {
    const d = r.duplicates[+dupLink.dataset.dup];
    openSource(d.doc_id, d.filename, d.regions);
    return;
  }
  if (e.target.closest("summary")) return;
  document.querySelectorAll(".result.active").forEach((x) => x !== li && x.classList.remove("active", "expanded"));
  if (li.classList.contains("active")) li.classList.toggle("expanded");
  li.classList.add("active");
  openSource(r.doc_id, r.filename, r.regions);
});

// ------------------------------------------------------------------ 原文预览

const viewer = { docId: null, pdf: null, pages: [], token: 0, regions: [], observer: null };
const pagesEl = $("#pages");

async function openSource(docId, filename, regions) {
  $("#viewerBar").hidden = false;
  $("#viewerTitle").textContent = filename;
  $("#downloadOrig").href = `/api/documents/${docId}/file`;
  if (viewer.docId !== docId) {
    const ok = await loadPdf(docId);
    if (!ok) return;
  }
  showHighlights(regions);
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
  viewer.pages = sizes.map(([w, h]) => ({ w, h, scale: 1, el: null, rendered: false }));
  layoutPages();
  return true;
}

function layoutPages() {
  viewer.observer?.disconnect();
  pagesEl.innerHTML = "";
  const avail = Math.max(300, pagesEl.clientWidth - 64);
  viewer.observer = new IntersectionObserver((entries) => {
    entries.forEach((en) => en.isIntersecting && renderPage(+en.target.dataset.i));
  }, { root: pagesEl, rootMargin: "800px 0px" });
  viewer.pages.forEach((p, i) => {
    p.scale = Math.min(avail / p.w, 2);
    p.rendered = false;
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
  const page = await viewer.pdf.getPage(i + 1);
  const dpr = window.devicePixelRatio || 1;
  const vp = page.getViewport({ scale: p.scale * dpr });
  const canvas = document.createElement("canvas");
  canvas.width = Math.floor(vp.width);
  canvas.height = Math.floor(vp.height);
  p.el.prepend(canvas);
  try {
    await page.render({ canvasContext: canvas.getContext("2d"), viewport: vp }).promise;
  } catch (err) {
    if (err?.name !== "RenderingCancelledException") console.error(err);
  }
}

function showHighlights(regions, flash = true) {
  viewer.regions = regions;
  pagesEl.querySelectorAll(".hl").forEach((x) => x.remove());
  let first = null;
  for (const [pg, x0, y0, x1, y1] of regions) {
    const p = viewer.pages[pg];
    if (!p) continue;
    const pad = 3;
    const div = document.createElement("div");
    div.className = flash ? "hl flash" : "hl";
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

let resizeTimer;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {
    if (!viewer.pdf) return;
    const ratio = pagesEl.scrollTop / Math.max(1, pagesEl.scrollHeight);
    layoutPages();
    showHighlights(viewer.regions, false);
    pagesEl.scrollTop = ratio * pagesEl.scrollHeight;
  }, 200);
});

// ------------------------------------------------------------------ 文档库

const STATUS = {
  queued: "排队中", converting: "转换 PDF", parsing: "解析中", embedding: "向量化", done: "完成", failed: "失败",
};
const busy = (d) => !["done", "failed"].includes(d.status);

$("#openLibrary").addEventListener("click", () => { $("#library").showModal(); refreshDocs(); });
$("#closeLibrary").addEventListener("click", () => $("#library").close());
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

async function upload(fileList) {
  const files = [...fileList];
  if (!files.length) return;
  if (!$("#library").open) $("#library").showModal();
  const fd = new FormData();
  files.forEach((f) => fd.append("files", f));
  dz.querySelector("strong").textContent = `正在上传 ${files.length} 个文件…`;
  try {
    const res = await api("/api/documents", { method: "POST", body: fd });
    const errs = res.filter((r) => r.error).map((r) => `${r.filename}：${r.error}`);
    const dups = res.filter((r) => r.duplicate).map((r) => r.filename);
    if (errs.length || dups.length) {
      alert([...errs, ...(dups.length ? [`已存在，跳过：${dups.join("、")}`] : [])].join("\n"));
    }
  } catch (err) {
    alert(`上传失败：${err.message}`);
  } finally {
    dz.querySelector("strong").textContent = "点击选择文件或拖拽到这里";
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
  $("#docCount").textContent = docs.length;
  const sel = $("#docFilter");
  const cur = sel.value;
  sel.innerHTML = `<option value="">全部文档（${done.length}）</option>` +
    done.map((d) => `<option value="${d.id}">${esc(d.filename)}</option>`).join("");
  sel.value = done.some((d) => String(d.id) === cur) ? cur : "";

  $("#docRows").innerHTML = docs.length ? docs.map((d) => {
    const cls = d.status === "done" ? "done" : d.status === "failed" ? "failed" : "busy";
    const bar = busy(d) ? `<div class="bar"><i style="width:${Math.round((d.progress || 0) * 100)}%"></i></div>` : "";
    const msg = d.message ? `<span class="msg" title="${esc(d.message)}">${esc(d.message)}</span>` : "";
    const pages = d.pages ? `${d.pages}${d.ocr_pages ? `<br><small>OCR ${d.ocr_pages}</small>` : ""}` : "–";
    return `<tr>
      <td title="${esc(d.filename)}">${esc(d.filename)}</td>
      <td class="num">${pages}</td>
      <td class="num">${d.chunk_count || "–"}</td>
      <td><div class="status"><span class="label ${cls}">${STATUS[d.status] || d.status}</span>${bar}${msg}</div></td>
      <td class="num">${esc((d.created_at || "").slice(0, 16))}</td>
      <td class="actions">
        <button class="btn small" data-reindex="${d.id}" ${busy(d) ? "disabled" : ""}>重新解析</button>
        <button class="btn small danger" data-del="${d.id}">删除</button>
      </td></tr>`;
  }).join("") : `<tr><td colspan="6" class="hint">还没有文档，先上传几份吧</td></tr>`;
}

$("#docRows").addEventListener("click", async (e) => {
  const re = e.target.closest("[data-reindex]");
  const del = e.target.closest("[data-del]");
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
  refreshDocs();
});

// ------------------------------------------------------------------ 服务状态

async function refreshHealth() {
  try {
    const h = await api("/api/health");
    $("#health").innerHTML = [
      [`MinerU`, h.mineru ? "up" : ""],
      [`Embedding`, h.embedding ? "up" : ""],
      [`重排`, h.reranker ? "up" : "off"],
    ].map(([n, c]) => `<span class="${c}">${n}</span>`).join("");
    $("#health").title = `已入库 ${h.documents} 份文档，${h.chunks} 个片段`;
  } catch {
    $("#health").innerHTML = `<span>服务未连接</span>`;
  }
}

refreshDocs();
refreshHealth();
setInterval(refreshHealth, 30000);
if (!$("#results").children.length) {
  $("#results").innerHTML = `<li class="hint">输入关键字开始检索<br><small>按 / 快速聚焦搜索框；“精确”模式要求原文逐字包含</small></li>`;
}
