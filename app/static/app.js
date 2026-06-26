const app = document.querySelector("#app");

let activeView = "single";
let debugMode = false;
let localOcrMode = false;
let health = null;
let reviewQueue = [];

let singleFiles = [];
let singleManifest = null;
let singleResult = null;
let singleMessage = "";
let showSingleApplication = true;
let singleFormValues = {};

let batchFiles = [];
let batchManifest = null;
let currentJob = null;
let batchMessage = "";
let showBatchApplication = false;
let batchFormValues = {};

const fieldDefs = [
  ["brand_name", "Brand name", "e.g. Lenz Moser"],
  ["class_type", "Class / type", "e.g. Wine"],
  ["alcohol_content", "Alcohol content", "e.g. 12%"],
  ["net_contents", "Net contents", "e.g. 750 mL"],
  ["bottler", "Name and address of bottler / producer", "e.g. Produced and bottled by Example Co., Austin, TX"],
  ["country", "Country of origin", "e.g. United States"],
];

function render() {
  app.innerHTML = `
    <header class="appbar">
      <div class="left-tools">
        <select data-view class="view-select" aria-label="Current workspace">
          <option value="single" ${activeView === "single" ? "selected" : ""}>Single Verification</option>
          <option value="batch" ${activeView === "batch" ? "selected" : ""}>Batch Jobs</option>
          <option value="review" ${activeView === "review" ? "selected" : ""}>Review Queue (${reviewQueue.length})</option>
        </select>
      </div>
      <strong>TTB Label Verification</strong>
      <div class="right-tools">
        <label class="switch"><input type="checkbox" data-ocr-toggle ${localOcrMode ? "checked" : ""}/><span>Local OCR</span></label>
        <button data-debug class="icon-button ${debugMode ? "active" : ""}" title="Developer debug">?</button>
      </div>
    </header>
    <main>${activeView === "single" ? singleView() : activeView === "batch" ? batchView() : reviewView()}</main>
  `;
  bind();
}

function singleView() {
  return `<section class="workspace two-column">
    <div class="entry-card centered-card">
      <div class="section-head"><span class="step">1</span><div><h1>Single product</h1><p>Upload 1-4 label images for one product. Drop images and an optional manifest into the same area.</p></div></div>
      ${singleDropZone()}
      ${manifestStatus("single")}
      <div class="button-row">
        <button type="button" data-pick-files="single">Choose Images or Manifest</button>
        <button type="button" data-application="single">${showSingleApplication ? "Hide" : "Add"} application details</button>
        ${singleFiles.length || singleManifest ? `<button type="button" class="quiet" data-clear="single">Clear uploads</button>` : ""}
      </div>
      ${showSingleApplication ? applicationForm("single") : ""}
      ${singleMessage ? `<div class="form-message">${escapeHtml(singleMessage)}</div>` : ""}
      <button class="primary run-button" type="button" data-submit="single" ${singleFiles.length ? "" : "disabled"}>${localOcrMode ? "Run Local OCR" : "Verify with Free LLM Route"}</button>
      ${runtimeLine()}
    </div>
    <div class="results-panel fixed-result">
      <div class="results-head"><div><h2>Result</h2><p>Each run appears as a dropdown with extracted observations and timing.</p></div></div>
      ${singleResult ? resultDropdown(singleResult, false) : `<div class="empty">No product has been verified yet.</div>`}
    </div>
  </section>`;
}

function batchView() {
  return `<section class="workspace two-column">
    <div class="entry-card centered-card">
      <div class="section-head"><span class="step">1</span><div><h1>Batch job</h1><p>Choose a folder of label images. Add a manifest when you need to group front, back, and side images by application.</p></div></div>
      ${batchImageDropZone()}
      <div class="button-row primary-actions">
        <button type="button" data-pick-folder="batch">Choose Folder</button>
        <details class="advanced-actions"><summary>Advanced</summary><button type="button" data-pick-files="batch">Choose Image Files</button></details>
      </div>
      ${batchManifestZone()}
      ${manifestStatus("batch")}
      <div class="button-row">
        <button type="button" data-application="batch">${showBatchApplication ? "Hide" : "Add"} shared application details</button>
        ${batchFiles.length || batchManifest ? `<button type="button" class="quiet" data-clear="batch">Clear uploads</button>` : ""}
      </div>
      ${showBatchApplication ? applicationForm("batch") : ""}
      ${batchMessage ? `<div class="form-message">${escapeHtml(batchMessage)}</div>` : ""}
      <button class="primary run-button" type="button" data-submit="batch" ${batchFiles.length ? "" : "disabled"}>${localOcrMode ? "Run OCR Batch" : "Start Free LLM Batch"}</button>
      ${runtimeLine()}
    </div>
    <div class="results-panel fixed-result">
      <div class="results-head">
        <div><h2>Batch results</h2><p>${currentJob ? `${escapeHtml(currentJob.status)} - ${currentJob.completed}/${currentJob.total} processed` : "Issue-first dropdown results appear here."}</p></div>
        ${currentJob ? batchSummaryPanel(currentJob) : ""}
      </div>
      ${currentJob ? batchResults(currentJob) : `<div class="empty">No batch has been run yet.</div>`}
    </div>
  </section>`;
}

function reviewView() {
  const items = reviewQueue.filter((item) => item.verdict !== "pass");
  return `<section class="workspace">
    <div class="results-panel">
      <div class="results-head"><div><h1>Review queue</h1><p>Resolve labels that need agent attention.</p></div><span class="chip">${items.length}</span></div>
      ${items.length ? `<div class="result-list">${items.map((item) => resultDropdown(item, true)).join("")}</div>` : `<div class="empty">No review or fail items.</div>`}
    </div>
  </section>`;
}

function singleDropZone() {
  return `<div class="drop-zone" data-dropzone="single">
    <input id="single-file-input" type="file" accept="image/*,.json,.csv" multiple hidden />
    <strong>Drop product images or manifest here</strong>
    <p>Images are treated as one product. A CSV or JSON manifest may supply application values.</p>
    <p class="selection-summary">${escapeHtml(singleSummary())}</p>
  </div>`;
}

function batchImageDropZone() {
  return `<div class="drop-zone" data-dropzone="batch-images">
    <input id="batch-file-input" type="file" accept="image/*" multiple hidden />
    <input id="batch-folder-input" type="file" accept="image/*" webkitdirectory directory multiple hidden />
    <strong>Drop a folder or batch images here</strong>
    <p>Without a manifest, each image becomes its own product. Manifests dropped here are routed to the manifest slot.</p>
    <p class="selection-summary">${escapeHtml(batchSummary())}</p>
  </div>`;
}

function batchManifestZone() {
  return `<div class="manifest-zone" data-dropzone="batch-manifest">
    <input id="batch-manifest-input" type="file" accept=".json,.csv" hidden />
    <div><strong>Manifest</strong><p>Drop manifest.json or manifest.csv here to group images with applications.</p></div>
    <button type="button" data-pick-manifest="batch">Choose Manifest</button>
  </div>`;
}

function singleSummary() {
  const parts = [];
  if (singleFiles.length) parts.push(`${singleFiles.length} image${singleFiles.length === 1 ? "" : "s"} selected`);
  if (singleManifest) parts.push(`${singleManifest.name} attached`);
  return parts.length ? parts.join(" + ") : "No single-product files selected.";
}

function batchSummary() {
  const parts = [];
  if (batchFiles.length) parts.push(`${batchFiles.length} image${batchFiles.length === 1 ? "" : "s"} selected`);
  if (batchManifest) parts.push(`${batchManifest.name} attached`);
  return parts.length ? parts.join(" + ") : "No batch images selected.";
}

function manifestStatus(kind) {
  const manifest = kind === "single" ? singleManifest : batchManifest;
  return `<div class="manifest-status ${manifest ? "attached" : ""}">
    <span>${manifest ? `${escapeHtml(manifest.name)} attached` : "No manifest attached"}</span>
    ${manifest ? `<button type="button" class="link-button" data-remove-manifest="${kind}">Remove</button>` : ""}
  </div>`;
}

function applicationForm(kind) {
  const values = kind === "single" ? singleFormValues : batchFormValues;
  return `<form id="${kind}-form" class="application-form"><p class="form-hint">Application details are optional. Example placeholders are not submitted unless you type values.</p><div class="form-grid">${fieldDefs.map(([name, label, placeholder]) => field(kind, name, label, placeholder, values[name] || "")).join("")}</div></form>`;
}

function field(kind, name, labelText, placeholder, value) {
  return `<label><span>${labelText}</span><input data-form-kind="${kind}" name="${name}" placeholder="${placeholder}" value="${escapeAttr(value)}" /></label>`;
}

function batchSummaryPanel(job) {
  const metrics = job.metrics || {};
  return `<div class="summary-stack">
    <div class="summary-row"><span>Pass ${job.counts.pass}</span><span>Review ${job.counts.review}</span><span>Fail ${job.counts.fail}</span><a class="export" href="/api/batch/jobs/${job.job_id}/export.csv">Export CSV</a></div>
    ${metrics.average_latency_ms ? `<div class="metric-row"><span>Avg ${formatMs(metrics.average_latency_ms)}</span><span>P95 ${formatMs(metrics.p95_latency_ms)}</span><span>${escapeHtml(metrics.throughput_images_per_minute)} images/min</span></div>` : ""}
  </div>`;
}

function batchResults(job) {
  const ordered = [...job.results].sort((a, b) => ({ fail: 0, review: 1, pass: 2 }[a.verdict] - { fail: 0, review: 1, pass: 2 }[b.verdict]));
  return `${job.errors.length ? `<div class="form-message">${job.errors.map(escapeHtml).join("<br/>")}</div>` : ""}<div class="result-list">${ordered.map((result) => resultDropdown(result, false)).join("")}</div>`;
}

function resultDropdown(result, reviewActions) {
  const issueSummary = resultIssues(result).join(", ") || "No issues";
  return `<details class="result ${result.verdict}" ${result.verdict !== "pass" ? "open" : ""}>
    <summary>
      <div><strong>${escapeHtml(result.label || result.product_id)}</strong><p>${escapeHtml(issueSummary)}</p></div>
      <div class="result-meta"><span>${result.image_count} image(s)</span><span>${formatMs(result.latency_ms)}</span>${badge(result.verdict)}</div>
    </summary>
    ${resultBody(result)}
    ${reviewActions ? `<div class="review-actions"><button data-dismiss="${result.product_id}">Dismiss</button><button class="danger" data-fail="${result.product_id}">Mark Application Failed</button></div>` : ""}
  </details>`;
}

function resultIssues(result) {
  const issues = Object.values(result.fields).filter((f) => !["pass", "not_checked"].includes(f.status)).map((f) => label(f.field));
  if (result.government_warning.status !== "pass") issues.unshift("Government Warning");
  return issues;
}

function resultBody(result) {
  return `<table><thead><tr><th>Field</th><th>Expected</th><th>Observed</th><th>Status</th><th>Detail</th></tr></thead><tbody>
    ${Object.values(result.fields).map((f) => `<tr><td>${label(f.field)}</td><td>${escapeHtml(f.expected || "")}</td><td>${escapeHtml(f.observed || "")}</td><td>${status(f.status)}</td><td>${escapeHtml(f.detail)}</td></tr>`).join("")}
    </tbody></table>
    <div class="warning-block"><strong>Government Warning</strong> ${status(result.government_warning.status)}<p>${escapeHtml(result.government_warning.detail)}</p></div>
    ${result.notes.length ? `<p class="notes">${result.notes.map(escapeHtml).join(" ")}</p>` : ""}
    ${debugMode ? debugBlock(result) : ""}`;
}

function debugBlock(result) {
  const timings = result.stage_timings || [];
  return `<details class="debug" open><summary>Extraction debug</summary>
    <div class="debug-grid"><span>Mode</span><strong>${escapeHtml(result.processing_mode)}</strong><span>Model</span><strong>${escapeHtml(result.model_used || "")}</strong><span>Fallback used</span><strong>${result.fallback_used ? "yes" : "no"}</strong><span>Confidence</span><strong>${escapeHtml(result.extraction_confidence)}</strong></div>
    ${timings.length ? `<table class="timing-table"><tbody>${timings.map((timing) => `<tr><td>${escapeHtml(timing.stage)}</td><td>${formatMs(timing.elapsed_ms)}</td></tr>`).join("")}</tbody></table>` : ""}
    <pre>${escapeHtml(result.raw_extraction || "Raw extraction hidden by server configuration.")}</pre>
  </details>`;
}

function runtimeLine() {
  if (!health) return "";
  const llm = health.llm || {};
  const routeLabel = localOcrMode ? "Local OCR mode is on." : llm.provider === "azure_foundry" ? "Azure Foundry route is active." : "Free OpenRouter route is active.";
  return `<section class="runtime-line"><span>${routeLabel}</span><span>Provider: ${escapeHtml(llm.provider || "unknown")}</span><span>Model: ${escapeHtml(llm.configured_model || "not configured")}</span><span>Timeout: ${escapeHtml(llm.timeout_seconds || "")}s</span><span>Fallbacks: ${llm.fallbacks_enabled ? "enabled" : "disabled"}</span>${llm.free_route_config_error ? `<span class="danger-text">${escapeHtml(llm.free_route_config_error)}</span>` : ""}</section>`;
}

function bind() {
  document.querySelector("[data-view]")?.addEventListener("change", (e) => { activeView = e.target.value; render(); });
  document.querySelector("[data-debug]")?.addEventListener("click", () => { debugMode = !debugMode; render(); });
  document.querySelector("[data-ocr-toggle]")?.addEventListener("change", (e) => { localOcrMode = e.target.checked; render(); });
  document.querySelectorAll("[data-pick-files]").forEach((btn) => btn.onclick = (event) => { event.stopPropagation(); document.querySelector(`#${btn.dataset.pickFiles}-file-input`)?.click(); });
  document.querySelectorAll("[data-pick-folder]").forEach((btn) => btn.onclick = (event) => { event.stopPropagation(); document.querySelector(`#${btn.dataset.pickFolder}-folder-input`)?.click(); });
  document.querySelectorAll("[data-pick-manifest]").forEach((btn) => btn.onclick = (event) => { event.stopPropagation(); document.querySelector(`#${btn.dataset.pickManifest}-manifest-input`)?.click(); });
  document.querySelectorAll("input[type=file]").forEach((input) => input.addEventListener("change", (event) => {
    const files = [...event.target.files];
    if (input.id === "batch-manifest-input") addSelectedFiles(files, "batch-manifest");
    else addSelectedFiles(files, input.id.startsWith("single") ? "single" : "batch-images");
    input.value = "";
  }));
  document.querySelectorAll("[data-dropzone]").forEach((zone) => {
    zone.addEventListener("click", () => {
      const target = zone.dataset.dropzone === "batch-images" ? "#batch-folder-input" : zone.dataset.dropzone === "batch-manifest" ? "#batch-manifest-input" : "#single-file-input";
      document.querySelector(target)?.click();
    });
    zone.addEventListener("dragover", (event) => { event.preventDefault(); zone.classList.add("dragging"); });
    zone.addEventListener("dragleave", () => zone.classList.remove("dragging"));
    zone.addEventListener("drop", (event) => handleDrop(event, zone.dataset.dropzone));
  });
  document.querySelectorAll("[data-form-kind]").forEach((input) => input.addEventListener("input", updateFormValue));
  document.querySelectorAll("[data-application]").forEach((btn) => btn.onclick = () => toggleApplication(btn.dataset.application));
  document.querySelectorAll("[data-clear]").forEach((btn) => btn.onclick = () => clearFiles(btn.dataset.clear));
  document.querySelectorAll("[data-remove-manifest]").forEach((btn) => btn.onclick = () => removeManifest(btn.dataset.removeManifest));
  document.querySelectorAll("[data-submit]").forEach((btn) => btn.onclick = () => (btn.dataset.submit === "single" ? submitSingle() : submitBatch()));
  document.querySelectorAll("[data-dismiss]").forEach((btn) => btn.onclick = () => { reviewQueue = reviewQueue.filter((item) => item.product_id !== btn.dataset.dismiss); render(); });
  document.querySelectorAll("[data-fail]").forEach((btn) => btn.onclick = async () => { await markFailed(btn.dataset.fail); });
}

function updateFormValue(event) {
  const target = event.target;
  const values = target.dataset.formKind === "single" ? singleFormValues : batchFormValues;
  values[target.name] = target.value;
}

function captureFormValues(kind) {
  const values = kind === "single" ? singleFormValues : batchFormValues;
  document.querySelectorAll(`[data-form-kind="${kind}"]`).forEach((input) => {
    values[input.name] = input.value;
  });
}

function toggleApplication(kind) {
  if (kind === "single") showSingleApplication = !showSingleApplication;
  else showBatchApplication = !showBatchApplication;
  render();
}

async function handleDrop(event, kind) {
  event.preventDefault();
  event.currentTarget.classList.remove("dragging");
  const files = await filesFromDataTransfer(event.dataTransfer);
  addSelectedFiles(files, kind);
}

function addSelectedFiles(files, kind) {
  if (kind === "single") singleMessage = "";
  else batchMessage = "";
  const target = kind === "single" ? singleFiles : batchFiles;
  for (const file of files) {
    if (isManifest(file)) {
      if (kind === "single") singleManifest = file;
      else batchManifest = file;
      continue;
    }
    if (kind === "batch-manifest") {
      batchMessage = `${file.name} is not a CSV or JSON manifest.`;
      continue;
    }
    if (!isImage(file)) {
      if (kind === "single") singleMessage = `${file.name} is not a supported image or manifest.`;
      else batchMessage = `${file.name} is not a supported image.`;
      continue;
    }
    const path = file.webkitRelativePath || file.relativePath || file.name;
    if (kind === "single" && target.length >= 4) {
      singleMessage = "Single verification supports up to 4 images for one product.";
      continue;
    }
    const exists = target.some((item) => item.path === path && item.file.size === file.size);
    if (!exists) target.push({ file, path });
  }
  render();
}

function isManifest(file) { return /(^|[\\/])manifest\.(json|csv)$/i.test(file.webkitRelativePath || file.relativePath || file.name) || /\.(json|csv)$/i.test(file.name); }
function isImage(file) { return file.type.startsWith("image/") || /\.(png|jpe?g|webp|gif|bmp|tiff?)$/i.test(file.name); }

async function filesFromDataTransfer(dataTransfer) {
  const entries = [...(dataTransfer.items || [])].map((item) => item.webkitGetAsEntry?.()).filter(Boolean);
  if (!entries.length) return [...dataTransfer.files];
  const files = [];
  for (const entry of entries) files.push(...await readEntry(entry));
  return files;
}

function readEntry(entry, prefix = "") {
  return new Promise((resolve) => {
    if (entry.isFile) {
      entry.file((file) => {
        Object.defineProperty(file, "relativePath", { value: `${prefix}${file.name}` });
        resolve([file]);
      });
      return;
    }
    if (!entry.isDirectory) return resolve([]);
    const reader = entry.createReader();
    const all = [];
    const readBatch = () => reader.readEntries(async (entries) => {
      if (!entries.length) return resolve(all.flat());
      for (const child of entries) all.push(await readEntry(child, `${prefix}${entry.name}/`));
      readBatch();
    });
    readBatch();
  });
}

function clearFiles(kind) {
  if (kind === "single") {
    singleFiles = [];
    singleManifest = null;
    singleMessage = "";
  } else {
    batchFiles = [];
    batchManifest = null;
    batchMessage = "";
  }
  render();
}

function removeManifest(kind) {
  if (kind === "single") singleManifest = null;
  else batchManifest = null;
  render();
}

async function submitSingle() {
  if (!singleFiles.length) return;
  singleMessage = "";
  captureFormValues("single");
  const data = buildFormData(singleFormValues);
  data.set("processing_mode", localOcrMode ? "local_ocr" : "llm");
  if (debugMode) data.set("debug_mode", "true");
  singleFiles.forEach((item) => data.append("images[]", item.file, item.path));
  if (singleManifest) data.append("manifest", singleManifest, singleManifest.name);
  try {
    singleResult = await post("/api/verify", data);
    if (singleResult.verdict !== "pass") mergeReview(singleResult);
    render();
  } catch (error) {
    singleMessage = friendlyError(error.message);
    render();
  }
}

async function submitBatch() {
  if (!batchFiles.length) return;
  batchMessage = "";
  captureFormValues("batch");
  const data = buildFormData(batchFormValues);
  data.set("processing_mode", localOcrMode ? "local_ocr" : "llm");
  if (debugMode) data.set("debug_mode", "true");
  batchFiles.forEach((item) => data.append("images[]", item.file, item.path));
  if (batchManifest) data.append("manifest", batchManifest, batchManifest.name);
  try {
    const started = await post("/api/batch/jobs", data);
    currentJob = { job_id: started.job_id, status: "queued", total: batchFiles.length, completed: 0, counts: { pass: 0, review: 0, fail: 0 }, results: [], errors: [], metrics: null };
    render();
    await poll(started.job_id);
  } catch (error) {
    batchMessage = friendlyError(error.message);
    render();
  }
}

function buildFormData(values) {
  const data = new FormData();
  fieldDefs.forEach(([name]) => {
    const value = (values[name] || "").trim();
    if (value) data.set(name, value);
  });
  return data;
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

async function markFailed(productId) {
  const item = reviewQueue.find((result) => result.product_id === productId);
  await postJson("/api/corrections", { product_id: productId, label: item?.label || null, field: "final_verdict", corrected_value: "fail", verdict: "fail", verifier_note: "Agent marked application failed from review queue." });
  reviewQueue = reviewQueue.filter((result) => result.product_id !== productId);
  render();
}

async function post(url, body) {
  const response = await fetch(url, { method: "POST", body });
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}
async function postJson(url, body) {
  const response = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}
async function get(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}
async function loadHealth() {
  try { health = await get("/api/health"); } catch { health = null; }
  render();
}
function friendlyError(message) { try { return JSON.parse(message).detail || message; } catch { return message; } }
function badge(v) { return `<span class="verdict ${v}">${v}</span>`; }
function status(v) { return `<span class="status ${v}">${v.replace("_", " ")}</span>`; }
function label(v) { return v.split("_").map((p) => p[0].toUpperCase() + p.slice(1)).join(" "); }
function formatMs(ms) { return ms >= 1000 ? `${(ms / 1000).toFixed(2)} sec` : `${ms || 0} ms`; }
function escapeHtml(value) { return String(value).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" }[c])); }
function escapeAttr(value) { return escapeHtml(value).replace(/`/g, "&#096;"); }

render();
loadHealth();



