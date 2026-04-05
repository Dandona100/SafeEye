// SafeEyes Extension — Options with Pairing, Telegram, and Manual modes

const DEFAULTS = { safeeye_url: "http://localhost:1985", safeeye_token: "" };
const statusEl = document.getElementById("status");

// Tab switching
document.querySelectorAll(".tab").forEach(tab => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach(t => t.classList.remove("active"));
    document.querySelectorAll(".panel").forEach(p => p.classList.remove("active"));
    tab.classList.add("active");
    document.getElementById("panel-" + tab.dataset.tab).classList.add("active");
    statusEl.className = "status";
  });
});

// Show connected badge if already configured
chrome.storage.sync.get(DEFAULTS, (data) => {
  // Populate manual fields
  document.getElementById("serverUrl").value = data.safeeye_url;
  document.getElementById("apiToken").value = data.safeeye_token;
  document.getElementById("pairUrl").value = data.safeeye_url;
  document.getElementById("tgUrl").value = data.safeeye_url;
  if (data.safeeye_token) {
    document.getElementById("connected-info").style.display = "block";
  }
});

// === Pairing Code ===
document.getElementById("pairBtn").addEventListener("click", async () => {
  const url = document.getElementById("pairUrl").value.trim().replace(/\/+$/, "");
  const code = document.getElementById("pairCode").value.trim();
  if (!url) return showStatus("Server URL required", false);
  if (!code || code.length !== 6) return showStatus("Enter the 6-digit pairing code", false);

  showStatus("Connecting...", "info");
  try {
    const resp = await fetch(`${url}/api/v1/extension/pair/redeem`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code }),
    });
    const data = await resp.json();
    if (!resp.ok) return showStatus(data.detail || "Pairing failed", false);

    chrome.storage.sync.set({ safeeye_url: url, safeeye_token: data.token }, () => {
      showStatus("Paired successfully!", true);
      document.getElementById("connected-info").style.display = "block";
      document.getElementById("apiToken").value = data.token;
    });
  } catch (e) { showStatus("Connection failed: " + e.message, false); }
});

// === Telegram Auth ===
let tgStep = "request"; // request or verify
const tgBtn = document.getElementById("tgBtn");

tgBtn.addEventListener("click", async () => {
  const url = document.getElementById("tgUrl").value.trim().replace(/\/+$/, "");
  const username = document.getElementById("tgUsername").value.trim();

  if (!url) return showStatus("Server URL required", false);

  if (tgStep === "request") {
    if (!username) return showStatus("Telegram username required", false);
    showStatus("Sending code...", "info");
    try {
      const resp = await fetch(`${url}/api/v1/auth/telegram/request`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username }),
      });
      const data = await resp.json();
      if (!resp.ok) return showStatus(data.detail || "Failed", false);
      showStatus("Code sent! Check your Telegram.", true);
      document.getElementById("tgCodeField").style.display = "block";
      tgBtn.textContent = "Verify";
      tgStep = "verify";
    } catch (e) { showStatus("Connection failed: " + e.message, false); }
  } else {
    const code = document.getElementById("tgCode").value.trim();
    if (!code) return showStatus("Enter the code from Telegram", false);
    showStatus("Verifying...", "info");
    try {
      const resp = await fetch(`${url}/api/v1/auth/telegram/verify`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, code }),
      });
      const data = await resp.json();
      if (!resp.ok) return showStatus(data.detail || "Verification failed", false);

      chrome.storage.sync.set({ safeeye_url: url, safeeye_token: data.token }, () => {
        showStatus("Authenticated via Telegram!", true);
        document.getElementById("connected-info").style.display = "block";
        document.getElementById("apiToken").value = data.token;
        tgStep = "request";
        tgBtn.textContent = "Send Code";
        document.getElementById("tgCodeField").style.display = "none";
      });
    } catch (e) { showStatus("Verification failed: " + e.message, false); }
  }
});

// === Manual ===
document.getElementById("saveBtn").addEventListener("click", () => {
  const url = document.getElementById("serverUrl").value.trim().replace(/\/+$/, "");
  const token = document.getElementById("apiToken").value.trim();
  if (!url) return showStatus("Server URL is required.", false);
  if (!token) return showStatus("API token is required.", false);
  chrome.storage.sync.set({ safeeye_url: url, safeeye_token: token }, () => {
    showStatus("Settings saved.", true);
    document.getElementById("connected-info").style.display = "block";
  });
});

document.getElementById("testBtn").addEventListener("click", async () => {
  const url = document.getElementById("serverUrl").value.trim().replace(/\/+$/, "");
  const token = document.getElementById("apiToken").value.trim();
  if (!url || !token) return showStatus("Fill in both fields first.", false);
  showStatus("Testing...", "info");
  try {
    const resp = await fetch(`${url}/health`);
    if (resp.ok) {
      const data = await resp.json();
      showStatus(`Connected! Status: ${data.status || "ok"}`, true);
    } else {
      showStatus(`Server returned HTTP ${resp.status}`, false);
    }
  } catch (e) { showStatus("Connection failed: " + e.message, false); }
});

function showStatus(msg, state) {
  statusEl.textContent = msg;
  if (state === "info") statusEl.className = "status info";
  else statusEl.className = "status " + (state ? "ok" : "fail");
}
