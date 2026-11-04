/**
 * chrome_extension/popup/popup.js
 * -------------------------------
 * Popup UI logic for AI Browser Automation Agent extension.
 */

document.addEventListener("DOMContentLoaded", async () => {
  const statusIndicator = document.getElementById("status-indicator");
  const statusText = document.getElementById("status-text");
  const serverUrlInput = document.getElementById("server-url-input");
  const saveUrlBtn = document.getElementById("save-url-btn");
  const reconnectBtn = document.getElementById("reconnect-btn");
  const openDashboardBtn = document.getElementById("open-dashboard-btn");
  const taskCountBadge = document.getElementById("task-count-badge");
  const activeTasksList = document.getElementById("active-tasks-list");

  // Load saved settings
  const { serverUrl = "ws://127.0.0.1:8000/api/extension/ws" } = await chrome.storage.local.get("serverUrl");
  serverUrlInput.value = serverUrl;

  async function refreshStatus() {
    try {
      const response = await chrome.runtime.sendMessage({ type: "get_status" });
      if (response) {
        if (response.connected) {
          statusIndicator.className = "status-pill status-connected";
          statusText.textContent = "Connected";
        } else {
          statusIndicator.className = "status-pill status-disconnected";
          const stateLabels = {
            0: "Connecting...",
            1: "Connected",
            2: "Closing...",
            3: "Disconnected",
            "-1": "Starting..."
          };
          statusText.textContent = stateLabels[String(response.socket_state)] || "Disconnected";
          statusText.title = response.last_error || `Backend: ${response.server_url || "not configured"}`;
        }

        const tasks = response.active_tasks || [];
        taskCountBadge.textContent = tasks.length.toString();

        if (tasks.length === 0) {
          activeTasksList.innerHTML = `<div class="empty-state">No active automation tabs</div>`;
        } else {
          activeTasksList.innerHTML = tasks
            .map(
              (t) => `
              <div class="task-item">
                <span class="task-id">${t.task_id}</span>
                <span class="tab-badge">Tab #${t.tab_id}</span>
              </div>
            `
            )
            .join("");
        }
      }
    } catch (e) {
      statusIndicator.className = "status-pill status-disconnected";
      statusText.textContent = "Service Idle";
    }
  }

  // Initial load
  await refreshStatus();
  // Poll status while popup is open
  const interval = setInterval(refreshStatus, 1500);
  window.addEventListener("unload", () => clearInterval(interval));

  // Save server URL
  saveUrlBtn.addEventListener("click", async () => {
    const newUrl = serverUrlInput.value.trim();
    if (newUrl) {
      await chrome.storage.local.set({ serverUrl: newUrl });
      saveUrlBtn.textContent = "✓ Saved";
      setTimeout(() => (saveUrlBtn.textContent = "Save"), 1500);
      await chrome.runtime.sendMessage({ type: "reconnect" });
      await refreshStatus();
    }
  });

  // Reconnect button
  reconnectBtn.addEventListener("click", async () => {
    reconnectBtn.textContent = "Connecting...";
    await chrome.runtime.sendMessage({ type: "reconnect" });
    await new Promise((r) => setTimeout(r, 600));
    await refreshStatus();
    reconnectBtn.textContent = "🔄 Reconnect Backend";
  });

  // Open Dashboard button
  openDashboardBtn.addEventListener("click", async () => {
    const { serverUrl = "ws://localhost:8000/api/extension/ws" } = await chrome.storage.local.get("serverUrl");
    let httpUrl = "http://127.0.0.1:8000";
    try {
      if (serverUrl.startsWith("ws://")) {
        httpUrl = serverUrl.replace("ws://", "http://").replace("/api/extension/ws", "");
      } else if (serverUrl.startsWith("wss://")) {
        httpUrl = serverUrl.replace("wss://", "https://").replace("/api/extension/ws", "");
      }
    } catch (e) {}
    chrome.tabs.create({ url: httpUrl });
  });
});
