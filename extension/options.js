// SafeEye options page

const serverUrlInput = document.getElementById("serverUrl");
const apiTokenInput = document.getElementById("apiToken");
const saveBtn = document.getElementById("saveBtn");
const testBtn = document.getElementById("testBtn");
const statusEl = document.getElementById("status");

const DEFAULTS = {
  safeeye_url: "http://localhost:1985",
  safeeye_token: ""
};

// Load saved settings
chrome.storage.sync.get(DEFAULTS, (data) => {
  serverUrlInput.value = data.safeeye_url;
  apiTokenInput.value = data.safeeye_token;
});

// Save
saveBtn.addEventListener("click", () => {
  const url = serverUrlInput.value.trim().replace(/\/+$/, "");
  const token = apiTokenInput.value.trim();

  if (!url) {
    showStatus("Server URL is required.", false);
    return;
  }
  if (!token) {
    showStatus("API token is required.", false);
    return;
  }

  chrome.storage.sync.set({
    safeeye_url: url,
    safeeye_token: token
  }, () => {
    showStatus("Settings saved.", true);
  });
});

// Test connection
testBtn.addEventListener("click", async () => {
  const url = serverUrlInput.value.trim().replace(/\/+$/, "");
  const token = apiTokenInput.value.trim();

  if (!url || !token) {
    showStatus("Fill in both fields first.", false);
    return;
  }

  statusEl.className = "status";
  statusEl.style.display = "block";
  statusEl.textContent = "Testing...";
  statusEl.style.color = "#6366f1";
  statusEl.style.background = "#eef2ff";
  statusEl.style.borderColor = "#c7d2fe";

  try {
    const resp = await fetch(`${url}/health`, {
      method: "GET",
      headers: { "Authorization": `Bearer ${token}` }
    });

    if (resp.ok) {
      const data = await resp.json();
      showStatus(`Connected. Server status: ${data.status || "ok"}`, true);
    } else {
      showStatus(`Server returned HTTP ${resp.status}`, false);
    }
  } catch (err) {
    showStatus(`Connection failed: ${err.message}`, false);
  }
});

function showStatus(msg, ok) {
  statusEl.textContent = msg;
  statusEl.className = "status " + (ok ? "ok" : "fail");
}
