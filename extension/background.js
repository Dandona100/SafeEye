// SafeEye Chrome Extension — Service Worker

const DEFAULTS = {
  safeeye_url: "http://localhost:1985",
  safeeye_token: ""
};

// Create context menu on install
chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: "safeeye-scan",
    title: "Scan with SafeEye",
    contexts: ["image"]
  });
});

// Handle context menu click
chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  if (info.menuItemId !== "safeeye-scan") return;

  const imageUrl = info.srcUrl;
  if (!imageUrl) return;

  // Store that we're scanning so popup can pick it up
  await chrome.storage.local.set({
    safeeye_last_scan: {
      status: "scanning",
      url: imageUrl,
      timestamp: Date.now()
    }
  });

  // Open the popup programmatically isn't possible in MV3,
  // so we send the result to storage and badge the icon.
  chrome.action.setBadgeText({ text: "..." });
  chrome.action.setBadgeBackgroundColor({ color: "#6366f1" });

  try {
    const settings = await chrome.storage.sync.get(DEFAULTS);

    if (!settings.safeeye_token) {
      await chrome.storage.local.set({
        safeeye_last_scan: {
          status: "error",
          url: imageUrl,
          error: "No API token configured. Open extension settings first.",
          timestamp: Date.now()
        }
      });
      chrome.action.setBadgeText({ text: "!" });
      chrome.action.setBadgeBackgroundColor({ color: "#ef4444" });
      return;
    }

    const baseUrl = settings.safeeye_url.replace(/\/+$/, "");
    const endpoint = `${baseUrl}/api/v1/scan/url?url=${encodeURIComponent(imageUrl)}`;

    const resp = await fetch(endpoint, {
      method: "POST",
      headers: {
        "Authorization": `Bearer ${settings.safeeye_token}`
      }
    });

    if (!resp.ok) {
      const text = await resp.text();
      throw new Error(`HTTP ${resp.status}: ${text}`);
    }

    const data = await resp.json();

    // Store result
    await chrome.storage.local.set({
      safeeye_last_scan: {
        status: "done",
        url: imageUrl,
        result: data,
        timestamp: Date.now()
      }
    });

    // Update badge
    const isNsfw = data.result?.is_nsfw;
    chrome.action.setBadgeText({ text: isNsfw ? "X" : "OK" });
    chrome.action.setBadgeBackgroundColor({
      color: isNsfw ? "#ef4444" : "#22c55e"
    });

    // Show notification-like injection in the page
    if (tab?.id) {
      chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: showToast,
        args: [isNsfw, data.result?.labels || [], data.result?.confidence || 0]
      }).catch(() => {});
    }

  } catch (err) {
    await chrome.storage.local.set({
      safeeye_last_scan: {
        status: "error",
        url: imageUrl,
        error: err.message,
        timestamp: Date.now()
      }
    });
    chrome.action.setBadgeText({ text: "!" });
    chrome.action.setBadgeBackgroundColor({ color: "#ef4444" });
  }
});

// Injected into the page to show a brief toast
function showToast(isNsfw, labels, confidence) {
  const existing = document.getElementById("safeeye-toast");
  if (existing) existing.remove();

  const el = document.createElement("div");
  el.id = "safeeye-toast";
  const pct = (confidence * 100).toFixed(1);
  const labelText = labels.length ? labels.join(", ") : "none";
  el.innerHTML = `
    <strong>${isNsfw ? "NSFW DETECTED" : "SAFE"}</strong>
    <br>Confidence: ${pct}%
    <br>Labels: ${labelText}
  `;
  Object.assign(el.style, {
    position: "fixed",
    top: "16px",
    right: "16px",
    zIndex: "2147483647",
    padding: "14px 20px",
    borderRadius: "10px",
    fontFamily: "system-ui, sans-serif",
    fontSize: "14px",
    lineHeight: "1.5",
    color: "#fff",
    background: isNsfw
      ? "linear-gradient(135deg, #dc2626, #991b1b)"
      : "linear-gradient(135deg, #16a34a, #15803d)",
    boxShadow: "0 4px 24px rgba(0,0,0,0.3)",
    transition: "opacity 0.3s",
    opacity: "0"
  });
  document.body.appendChild(el);

  requestAnimationFrame(() => { el.style.opacity = "1"; });
  setTimeout(() => {
    el.style.opacity = "0";
    setTimeout(() => el.remove(), 400);
  }, 4000);
}
