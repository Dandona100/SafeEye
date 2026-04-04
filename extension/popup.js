// SafeEye popup — displays the last scan result

const content = document.getElementById("content");
const openSettings = document.getElementById("openSettings");

openSettings.addEventListener("click", () => {
  chrome.runtime.openOptionsPage();
});

function render(scan) {
  if (!scan) {
    content.innerHTML = `<div class="empty">Right-click an image and choose<br><strong>"Scan with SafeEye"</strong></div>`;
    return;
  }

  if (scan.status === "scanning") {
    content.innerHTML = `
      <div class="scanning">
        <div class="spinner"></div>
        <div>Scanning image...</div>
      </div>`;
    // Re-check shortly
    setTimeout(loadResult, 500);
    return;
  }

  if (scan.status === "error") {
    content.innerHTML = `
      <div class="result-card error">
        <div class="verdict">Error</div>
        <div class="meta">${escapeHtml(scan.error || "Unknown error")}</div>
        ${scan.url ? `<div class="url-preview">${escapeHtml(truncate(scan.url, 100))}</div>` : ""}
      </div>`;
    return;
  }

  // status === "done"
  const r = scan.result?.result;
  if (!r) {
    content.innerHTML = `<div class="result-card error"><div class="verdict">Unexpected response</div></div>`;
    return;
  }

  const isNsfw = r.is_nsfw;
  const cls = isNsfw ? "nsfw" : "safe";
  const verdictText = isNsfw ? "NSFW Detected" : "Safe";
  const pct = (r.confidence * 100).toFixed(1);
  const labels = r.labels || [];
  const duration = r.scan_duration_ms ? r.scan_duration_ms.toFixed(0) : "?";
  const providerResults = r.provider_results || [];

  let labelsHtml = "";
  if (labels.length) {
    labelsHtml = `<div class="labels">${labels.map(l => `<span class="label-tag">${escapeHtml(translateLabel(l))}</span>`).join("")}</div>`;
  }

  let providersHtml = "";
  if (providerResults.length) {
    const rows = providerResults.map(p => {
      const color = p.error ? "#f59e0b" : (p.is_nsfw ? "#ef4444" : "#22c55e");
      const status = p.error ? "error" : (p.skipped ? "skipped" : (p.is_nsfw ? "NSFW" : "safe"));
      return `<div class="provider-row">
        <span class="provider-dot" style="background:${color}"></span>
        <strong>${escapeHtml(p.provider)}</strong>: ${status}
        ${p.confidence ? ` (${(p.confidence * 100).toFixed(0)}%)` : ""}
        ${p.latency_ms ? ` - ${p.latency_ms.toFixed(0)}ms` : ""}
      </div>`;
    }).join("");
    providersHtml = `<details class="providers"><summary>${r.providers_agree}/${r.providers_total} providers agree</summary>${rows}</details>`;
  }

  content.innerHTML = `
    <div class="result-card ${cls}">
      <div class="verdict">${isNsfw ? "&#9888;" : "&#10003;"} ${verdictText}</div>
      <div class="meta">
        <strong>Confidence:</strong> ${pct}%
        &nbsp;&middot;&nbsp;
        <strong>Time:</strong> ${duration}ms
        ${r.borderline ? '&nbsp;&middot;&nbsp;<strong style="color:#d97706">Borderline</strong>' : ""}
      </div>
      ${labelsHtml}
      ${providersHtml}
      <div class="url-preview">${escapeHtml(truncate(scan.url || "", 100))}</div>
    </div>`;
}

// Label translations (common NudeNet / classifier labels)
const LABEL_MAP = {
  "FEMALE_BREAST_EXPOSED": "Exposed breast",
  "FEMALE_GENITALIA_EXPOSED": "Exposed genitalia",
  "MALE_GENITALIA_EXPOSED": "Exposed genitalia",
  "BUTTOCKS_EXPOSED": "Exposed buttocks",
  "ANUS_EXPOSED": "Exposed anus",
  "FEMALE_BREAST_COVERED": "Covered breast",
  "FEMALE_GENITALIA_COVERED": "Covered genitalia",
  "MALE_GENITALIA_COVERED": "Covered genitalia",
  "BUTTOCKS_COVERED": "Covered buttocks",
  "BELLY_EXPOSED": "Exposed belly",
  "BELLY_COVERED": "Covered belly",
  "ARMPITS_EXPOSED": "Exposed armpits",
  "FEET_EXPOSED": "Exposed feet",
  "FACE_FEMALE": "Female face",
  "FACE_MALE": "Male face"
};

function translateLabel(label) {
  return LABEL_MAP[label] || label.replace(/_/g, " ").toLowerCase();
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

function truncate(str, max) {
  return str.length > max ? str.slice(0, max) + "..." : str;
}

function loadResult() {
  chrome.storage.local.get("safeeye_last_scan", (data) => {
    render(data.safeeye_last_scan || null);
  });
}

// Listen for storage changes to update in real-time
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes.safeeye_last_scan) {
    render(changes.safeeye_last_scan.newValue);
  }
});

loadResult();
