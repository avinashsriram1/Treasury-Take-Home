const app = document.querySelector("#app");

let tab = "single";
let debugMode = false;
let singleImages = [];
let batchImages = [];
let manifest = null;
let singleResult = null;
let currentJob = null;
let reviewQueue = [];

function render() {
  app.innerHTML = `
    <header class="topbar">
      <div>
        <h1>TTB Label Verifier V3</h1>
        <p>LLM-first label extraction with deterministic compliance checks and local OCR test mode.</p>
      </div>
      <div class="actions">
        <button data-debug class="${debugMode ? "active" : ""}">Dev Debug ${debugMode ? "On" : "Off"}</button>
        ${tabButton("single", "Single")}
        ${tabButton("batch", "Batch")}
        ${tabButton("review", `Review (${reviewQueue.length})`)}
      </div>
    </header>
    <main>${tab === "single" ? singleView() : tab === "batch" ? batchView() : reviewView()}</main>
  `;
  bind();
}

function tabButton(name, label) {
  return `<button data-tab="${name}" class="${tab === name ? "active" : ""}">${label}</button>`;
}

function singleView() {
  return `
    <section class="grid">
      <form id="single-form" class="panel">
        <div class="panel-title"><h2>Single Product</h2><span class="chip">1-4 images</span></div>
        <div class="notice"><strong>Default mode uses OpenRouter.</strong><br/>Use Local OCR Test only when you need offline testing.</div>
        ${field("brand_name", "Brand name", "Lenz Moser")}
        ${field("class_type", "Class / type", "Wine")}
        ${field("alcohol_content", "Alcohol content", "12%")}
        ${field("net_contents", "Net contents", "750 mL")}
        ${field("bottler", "Bottler / producer", "optional", false)}
        ${field("country", "Country of origin", "Austria", false)}
        ${imagePicker("single", singleImages)}
        <div class="mode-actions">
          <button class="primary" type="submit" data-mode="llm">Verify with LLM</button>
          <button class="secondary" type="submit" data-mode="local_ocr">Run Local OCR Test</button>
        </div>
      </form>
      <section class="panel">
        <div class="panel-title"><h2>Result</h2>${singleResult ? badge(singleResult.verdict) : ""}</div>
        ${singleResult ? resultCard(singleResult, false) : `<div class="empty">No result yet.</div>`}
      </section>
    </section>`;
}

function batchView() {
  return `
    <section class="grid">
      <form id="batch-form" class="panel">
        <div class="panel-title"><h2>Batch</h2><span class="chip">JSON or CSV</span></div>
        ${imagePicker("batch", batchImages)}
        <div class="drop">
          <strong>Manifest</strong>
          <input id="manifest-input" type="file" accept=".json,.csv" />
          <p>${manifest ? escapeHtml(manifest.name) : "Optional. Without a manifest each image is a review item."}</p>
        </div>
        <div class="mode-actions">
          <button class="primary" type="submit" data-mode="llm">Start LLM Batch</button>
          <button class="secondary" type="submit" data-mode="local_ocr">Start Local OCR Test</button>
        </div>
      </form>
      <section class="panel">
        <div class="panel-title"><h2>Progress</h2>${currentJob ? `<span class="chip">${currentJob.completed}/${currentJob.total}</span>` : ""}</div>
        ${currentJob ? batchJob(currentJob) : `<div class="empty">No batch running.</div>`}
      </section>
    </section>`;
}

function reviewView() {
  const items = reviewQueue.filter((item) => item.verdict !== "pass");
  return `<section class="panel">
    <div class="panel-title"><h2>Review Queue</h2><span class="chip">${items.length}</span></div>
    ${items.length ? items.map((item) => resultCard(item, true)).join("") : `<div class="empty">No review or fail items.</div>`}
  </section>`;
}

function field(name, label, placeholder, required = true) {
  return `<label><span>${label}</span><input name="${name}" placeholder="${placeholder}" ${required ? "required" : ""}/></label>`;
}

function imagePicker(kind, files) {
  return `<div class="drop">
    <strong>Label images</strong>
    <input data-images="${kind}" type="file" accept="image/*" multiple />
    ${files.length ? files.map((file, i) => `<div class="selected-file"><span>${escapeHtml(file.name)}</span><button type="button" data-remove="${kind}:${i}">Remove</button></div>`).join("") : "<p>No images selected.</p>"}
  </div>`;
}

function batchJob(job) {
  const results = [...job.results].sort((a, b) => ({fail:0, review:1, pass:2}[a.verdict] - {fail:0, review:1, pass:2}[b.verdict]));
  return `<div>
    <p>Pass ${job.counts.pass} · Review ${job.counts.review} · Fail ${job.counts.fail}</p>
    <a class="export" href="/api/batch/jobs/${job.job_id}/export.csv">Export CSV</a>
    ${results.map((result) => `<details class="result batch-result ${result.verdict}"><summary><strong>${escapeHtml(result.label || result.product_id)}</strong>${badge(result.verdict)}</summary>${resultBody(result)}</details>`).join("")}
  </div>`;
}

function resultCard(result, reviewActions) {
  return `<article class="result ${result.verdict}">
    <div class="result-head"><h3>${escapeHtml(result.label || result.product_id)}</h3>${badge(result.verdict)}</div>
    ${resultBody(result)}
    ${reviewActions ? `<div class="review-actions"><button data-dismiss="${result.product_id}">Dismiss</button><button class="danger" data-fail="${result.product_id}">Mark Application Failed</button></div>` : ""}
  </article>`;
}

function resultBody(result) {
  return `<p>${result.image_count} image(s), ${formatMs(result.latency_ms)}, mode ${result.processing_mode}</p>
    <table><thead><tr><th>Field</th><th>Expected</th><th>Observed</th><th>Status</th><th>Detail</th></tr></thead><tbody>
    ${Object.values(result.fields).map((f) => `<tr><td>${label(f.field)}</td><td>${escapeHtml(f.expected || "")}</td><td>${escapeHtml(f.observed || "")}</td><td>${status(f.status)}</td><td>${escapeHtml(f.detail)}</td></tr>`).join("")}
    </tbody></table>
    <div class="debug"><strong>Government Warning</strong> ${status(result.government_warning.status)}<p>${escapeHtml(result.government_warning.detail)}</p></div>
    ${debugMode ? `<details class="debug" open><summary>Extraction debug</summary><p>Model: ${escapeHtml(result.model_used || "")}</p><p>Confidence: ${result.extraction_confidence}</p><pre>${escapeHtml(result.raw_extraction || "Raw extraction hidden by server configuration.")}</pre></details>` : ""}`;
}

function bind() {
  document.querySelectorAll("[data-tab]").forEach((btn) => btn.onclick = () => { tab = btn.dataset.tab; render(); });
  document.querySelector("[data-debug]")?.addEventListener("click", () => { debugMode = !debugMode; render(); });
  bindImages("single");
  bindImages("batch");
  document.querySelector("#single-form")?.addEventListener("submit", submitSingle);
  document.querySelector("#batch-form")?.addEventListener("submit", submitBatch);
  document.querySelector("#manifest-input")?.addEventListener("change", (e) => { manifest = e.target.files[0] || null; render(); });
  document.querySelectorAll("[data-dismiss]").forEach((btn) => btn.onclick = () => { reviewQueue = reviewQueue.filter((item) => item.product_id !== btn.dataset.dismiss); render(); });
  document.querySelectorAll("[data-fail]").forEach((btn) => btn.onclick = async () => { reviewQueue = reviewQueue.filter((item) => item.product_id !== btn.dataset.fail); render(); });
}

function bindImages(kind) {
  document.querySelector(`[data-images="${kind}"]`)?.addEventListener("change", (e) => {
    const target = kind === "single" ? singleImages : batchImages;
    for (const file of e.target.files) if (!target.find((f) => f.name === file.name && f.size === file.size)) target.push(file);
    e.target.value = "";
    render();
  });
  document.querySelectorAll(`[data-remove^="${kind}:"]`).forEach((btn) => btn.onclick = () => {
    const [, index] = btn.dataset.remove.split(":");
    if (kind === "single") singleImages.splice(Number(index), 1); else batchImages.splice(Number(index), 1);
    render();
  });
}

async function submitSingle(event) {
  event.preventDefault();
  const mode = event.submitter.dataset.mode;
  const form = event.currentTarget;
  const data = new FormData(form);
  data.set("processing_mode", mode);
  if (debugMode) data.set("debug_mode", "true");
  singleImages.forEach((file) => data.append("images[]", file));
  singleResult = await post("/api/verify", data);
  if (singleResult.verdict !== "pass") mergeReview(singleResult);
  singleImages = [];
  form.reset();
  render();
}

async function submitBatch(event) {
  event.preventDefault();
  const data = new FormData();
  data.set("processing_mode", event.submitter.dataset.mode);
  if (debugMode) data.set("debug_mode", "true");
  batchImages.forEach((file) => data.append("images[]", file));
  if (manifest) data.append("manifest", manifest);
  const started = await post("/api/batch/jobs", data);
  batchImages = [];
  manifest = null;
  await poll(started.job_id);
}

async function poll(jobId) {
  currentJob = await get(`/api/batch/jobs/${jobId}`);
  currentJob.results.forEach((result) => { if (result.verdict !== "pass") mergeReview(result); });
  render();
  if (!["complete", "failed"].includes(currentJob.status)) setTimeout(() => poll(jobId), 1000);
}

function mergeReview(result) {
  reviewQueue = [result, ...reviewQueue.filter((item) => item.product_id !== result.product_id)];
}

async function post(url, body) {
  const response = await fetch(url, { method: "POST", body });
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}
async function get(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}
function badge(v) { return `<span class="verdict ${v}">${v}</span>`; }
function status(v) { return `<span class="status ${v}">${v}</span>`; }
function label(v) { return v.split("_").map((p) => p[0].toUpperCase() + p.slice(1)).join(" "); }
function formatMs(ms) { return ms >= 1000 ? `${(ms / 1000).toFixed(2)} sec` : `${ms} ms`; }
function escapeHtml(value) { return String(value).replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c])); }

render();

