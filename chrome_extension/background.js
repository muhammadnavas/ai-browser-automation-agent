/**
 * chrome_extension/background.js
 * -------------------------------
 * Background Service Worker for the AI Browser Automation Extension.
 *
 * Responsibilities:
 *  - Manages WebSocket bridge connection with FastAPI backend
 *  - Handles task-to-tab mappings (task_id -> chrome_tab_id)
 *  - Tab lifecycle (create, navigate, close, tab close detection)
 *  - Action routing and DOM inspection dispatching to content scripts
 *  - Visible tab screenshot capture
 */

const DEFAULT_SERVER_URL = "ws://localhost:8000/api/extension/ws";
let socket = null;
let reconnectTimer = null;
let pingInterval = null;
let isConnecting = false;

// In-memory task mapping: task_id -> tab_id
let taskTabMap = new Map();
// Reverse mapping: tab_id -> task_id
let tabTaskMap = new Map();

// ---------------------------------------------------------------------------
// Storage Sync Helpers
// ---------------------------------------------------------------------------

async function loadSettings() {
  const data = await chrome.storage.local.get(["serverUrl", "taskMappings"]);
  const serverUrl = data.serverUrl || DEFAULT_SERVER_URL;
  if (data.taskMappings) {
    taskTabMap = new Map(Object.entries(data.taskMappings));
    tabTaskMap.clear();
    for (const [taskId, tabId] of taskTabMap.entries()) {
      tabTaskMap.set(Number(tabId), taskId);
    }
  }
  return { serverUrl };
}

async function saveMappings() {
  const obj = {};
  for (const [taskId, tabId] of taskTabMap.entries()) {
    obj[taskId] = tabId;
  }
  await chrome.storage.local.set({ taskMappings: obj });
}

async function updateBadgeStatus(connected) {
  try {
    if (connected) {
      await chrome.action.setBadgeText({ text: "ON" });
      await chrome.action.setBadgeBackgroundColor({ color: "#22c55e" }); // Green
    } else {
      await chrome.action.setBadgeText({ text: "OFF" });
      await chrome.action.setBadgeBackgroundColor({ color: "#ef4444" }); // Red
    }
  } catch (e) {}
}

// ---------------------------------------------------------------------------
// WebSocket Connection Lifecycle
// ---------------------------------------------------------------------------

async function connectWebSocket() {
  if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {
    return;
  }

  if (isConnecting) return;
  isConnecting = true;

  const { serverUrl } = await loadSettings();

  try {
    console.log(`[AI Extension] Connecting to ${serverUrl}...`);
    socket = new WebSocket(serverUrl);

    socket.onopen = async () => {
      console.log("[AI Extension] Connected to FastAPI backend.");
      isConnecting = false;
      await updateBadgeStatus(true);
      await chrome.storage.local.set({ isConnected: true });

      // Send initial registration handshake
      sendWsMessage({
        type: "register",
        client: "chrome-extension",
        version: "1.0.0",
        active_tasks: Array.from(taskTabMap.keys()),
      });

      // Start ping heartbeat every 20s
      if (pingInterval) clearInterval(pingInterval);
      pingInterval = setInterval(() => {
        if (socket && socket.readyState === WebSocket.OPEN) {
          sendWsMessage({ type: "ping", timestamp: Date.now() });
        }
      }, 20000);
    };

    socket.onmessage = async (event) => {
      try {
        const message = JSON.parse(event.data);
        await handleBackendMessage(message);
      } catch (err) {
        console.error("[AI Extension] Error handling message:", err);
      }
    };

    socket.onclose = async (event) => {
      console.warn("[AI Extension] WebSocket disconnected. Code:", event.code);
      isConnecting = false;
      if (pingInterval) clearInterval(pingInterval);
      await updateBadgeStatus(false);
      await chrome.storage.local.set({ isConnected: false });

      // Schedule reconnection with backoff
      if (!reconnectTimer) {
        reconnectTimer = setTimeout(() => {
          reconnectTimer = null;
          connectWebSocket();
        }, 3000);
      }
    };

    socket.onerror = (error) => {
      console.error("[AI Extension] WebSocket error:", error);
    };
  } catch (err) {
    console.error("[AI Extension] Connection failed:", err);
    isConnecting = false;
    await updateBadgeStatus(false);
    if (!reconnectTimer) {
      reconnectTimer = setTimeout(() => {
        reconnectTimer = null;
        connectWebSocket();
      }, 5000);
    }
  }
}

function sendWsMessage(msg) {
  if (socket && socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify(msg));
  }
}

// ---------------------------------------------------------------------------
// Tab Management & Content Script Injection Helpers
// ---------------------------------------------------------------------------

async function ensureContentScriptInjected(tabId) {
  const getScriptableTab = async () => {
    const tab = await chrome.tabs.get(tabId);
    const pageUrl = tab?.url || "";
    if (/^(chrome|edge|about|devtools):\\/\\//i.test(pageUrl) ||
        pageUrl.startsWith("chrome-extension://")) {
      throw new Error(`Content scripts cannot run on this browser page: ${pageUrl}`);
    }
    return tab;
  };

  const ping = async () => {
    try {
      const response = await chrome.tabs.sendMessage(tabId, { type: "ping" });
      return Boolean(response && response.pong);
    } catch (error) {
      return false;
    }
  };

  const waitForPing = async (delays = [100, 200, 350, 600, 900]) => {
    for (const delay of delays) {
      if (await ping()) return true;
      await new Promise((resolve) => setTimeout(resolve, delay));
    }
    return ping();
  };

  await getScriptableTab();
  // document_idle injection may still be registering its message listener when
  // tabs.onUpdated reports complete. Give the manifest-injected script time first.
  if (await waitForPing([100, 200, 350])) return true;

  let lastInjectionError = null;
  for (let attempt = 0; attempt < 3; attempt += 1) {
    const currentTab = await getScriptableTab();
    if (currentTab.status !== "complete") {
      const loaded = await waitForTabLoaded(tabId, 15000);
      if (!loaded) {
        await new Promise((resolve) => setTimeout(resolve, 500));
        continue;
      }
    }

    try {
      await chrome.scripting.executeScript({
        target: { tabId, frameIds: [0] },
        files: ["content.js"],
      });
    } catch (error) {
      lastInjectionError = error;
    }

    // executeScript confirms evaluation was requested; ping confirms that the
    // runtime message listener is actually available in the top frame.
    if (await waitForPing([150, 300, 500, 800, 1200])) return true;
    await new Promise((resolve) => setTimeout(resolve, 300 * (attempt + 1)));
  }

  const finalTab = await chrome.tabs.get(tabId).catch(() => null);
  const finalUrl = finalTab?.url || "unknown URL";
  const injectionDetail = lastInjectionError?.message
    ? ` Last injection error: ${lastInjectionError.message}`
    : "";
  throw new Error(
    `Browser content script did not become ready for tab ${tabId} (${finalUrl}) after readiness polling and reinjection.${injectionDetail} Check that the extension has access to this site, then reload the task tab.`
  );
}

async function sendContentMessage(tabId, message) {
  await ensureContentScriptInjected(tabId);
  try {
    return await chrome.tabs.sendMessage(tabId, message);
  } catch (error) {
    // A page can navigate between the readiness check and message delivery.
    // Re-inject into the new document and retry once.
    if (!/receiving end does not exist|could not establish connection/i.test(error.message || "")) {
      throw error;
    }
    await waitForTabLoaded(tabId, 10000);
    await ensureContentScriptInjected(tabId);
    return await chrome.tabs.sendMessage(tabId, message);
  }
}

async function focusTaskTab(tabId) {
  const tab = await chrome.tabs.get(tabId);
  if (!tab || tab.windowId === undefined) {
    throw new Error("The agent task tab is not attached to a Chrome window.");
  }
  const win = await chrome.windows.get(tab.windowId);
  if (win.state === "minimized") {
    await chrome.windows.update(tab.windowId, { state: "maximized" });
  }
  await chrome.windows.update(tab.windowId, { focused: true });
  const activeTab = await chrome.tabs.update(tabId, { active: true });
  if (!activeTab.active) {
    throw new Error("Chrome did not activate the agent task tab.");
  }
  return activeTab;
}

async function waitForTabLoaded(tabId, timeoutMs = 12000) {
  try {
    const tab = await chrome.tabs.get(tabId);
    if (tab && tab.status === "complete") {
      return true;
    }
  } catch (e) {}

  return new Promise((resolve) => {
    let resolved = false;
    const timer = setTimeout(() => {
      if (!resolved) {
        resolved = true;
        chrome.tabs.onUpdated.removeListener(listener);
        resolve(null); // Resolve anyway to avoid blocking execution
      }
    }, timeoutMs);

    function listener(updatedTabId, info) {
      if (updatedTabId === tabId && info.status === "complete") {
        if (!resolved) {
          resolved = true;
          clearTimeout(timer);
          chrome.tabs.onUpdated.removeListener(listener);
          resolve(true);
        }
      }
    }

    chrome.tabs.onUpdated.addListener(listener);
  });
}

// ---------------------------------------------------------------------------
// Backend Message Router
// ---------------------------------------------------------------------------

async function handleBackendMessage(msg) {
  const { id, type, task_id } = msg;

  if (type === "pong") {
    return; // Heartbeat response
  }

  // ── 1. Create Task Tab ──
  if (type === "create_tab") {
    let createdTabId = null;
    try {
      const url = msg.url || "about:blank";
      // Keep the task in a dedicated background tab. Chrome can execute
      // content-script DOM actions in inactive tabs; never steal focus from
      // the user's current tab when a task starts.
      const tab = await chrome.tabs.create({ url, active: false });
      createdTabId = tab.id;

      taskTabMap.set(task_id, tab.id);
      tabTaskMap.set(tab.id, task_id);
      await saveMappings();

      // Chrome does not allow content-script injection into about:blank,
      // even with <all_urls>. A blank task tab is only a temporary container:
      // the backend will navigate it before requesting DOM inspection/actions.
      // Inject only after a real, scriptable document has loaded.
      if (url && url !== "about:blank") {
        await waitForTabLoaded(tab.id, 12000);
        const loadedTab = await chrome.tabs.get(tab.id);
        if (loadedTab.url && !loadedTab.url.startsWith("about:blank")) {
          await ensureContentScriptInjected(tab.id);
        }
      }

      sendWsMessage({
        id,
        task_id,
        type: "response",
        success: true,
        data: { tab_id: tab.id, url: tab.url || url },
      });
    } catch (err) {
      // A failed readiness handshake must not leave a stale task->tab mapping.
      // Remove the partially-created tab so the next task attempt starts cleanly.
      if (createdTabId !== null) {
        taskTabMap.delete(task_id);
        tabTaskMap.delete(createdTabId);
        try {
          await chrome.tabs.remove(createdTabId);
        } catch (cleanupError) {
          console.warn("[AI Extension] Could not remove failed task tab:", cleanupError);
        }
        try {
          await saveMappings();
        } catch (storageError) {
          console.warn("[AI Extension] Could not persist task-tab cleanup:", storageError);
        }
      }
      sendWsMessage({
        id,
        task_id,
        type: "response",
        success: false,
        error: `Failed to create task tab: ${err.message}`,
      });
    }
    return;
  }

  // ── Open a dedicated visible Google sign-in tab ──
  // This is handled before task-tab lookup because it must recover even if
  // the previous task tab is stale or no longer visible.
  if (type === "open_auth_tab") {
    try {
      const authUrl = msg.url || "https://accounts.google.com/ServiceLogin?service=mail&continue=https%3A%2F%2Fmail.google.com%2Fmail%2F";
      // Create a separate, visible tab so the user can clearly see and complete
      // Google's security challenge. Keep the new tab mapped to this task so
      // subsequent focus/inspection commands target the recovered session.
      const oldTabId = taskTabMap.get(task_id);
      const tab = await chrome.tabs.create({ url: authUrl, active: true });

      if (oldTabId !== undefined && oldTabId !== tab.id) {
        tabTaskMap.delete(oldTabId);
      }
      taskTabMap.set(task_id, tab.id);
      tabTaskMap.set(tab.id, task_id);
      await saveMappings();

      if (tab.windowId !== undefined) {
        const win = await chrome.windows.get(tab.windowId);
        if (win.state === "minimized") {
          await chrome.windows.update(tab.windowId, { state: "maximized" });
        }
        await chrome.windows.update(tab.windowId, { focused: true });
      }

      await waitForTabLoaded(tab.id, 15000);
      const openedTab = await chrome.tabs.get(tab.id);
      sendWsMessage({
        id, task_id, type: "response", success: true,
        data: {
          tab_id: tab.id,
          url: openedTab.url || authUrl,
          active: openedTab.active,
          window_id: openedTab.windowId,
        },
      });
    } catch (err) {
      console.error("AI Agent: failed to create visible Google sign-in tab:", err);
      sendWsMessage({
        id, task_id, type: "response", success: false,
        error: `Could not create a visible Google sign-in tab: ${err.message}`,
      });
    }
    return;
  }

  // Open a separate recovery tab without changing the task's tab mapping.
  // The original task tab remains available for live-session verification.
  if (type === "open_recovery_tab") {
    try {
      const recoveryUrl = msg.url;
      if (!recoveryUrl || !/^https:\/\/accounts\.google\.com\/RotateCookiesPage(?:[?#]|$)/i.test(recoveryUrl)) {
        throw new Error("Recovery tab URL must be the detected Google RotateCookiesPage URL.");
      }
      const tab = await chrome.tabs.create({ url: recoveryUrl, active: true });
      if (tab.windowId !== undefined) {
        const win = await chrome.windows.get(tab.windowId);
        if (win.state === "minimized") {
          await chrome.windows.update(tab.windowId, { state: "maximized" });
        }
        await chrome.windows.update(tab.windowId, { focused: true });
      }
      await waitForTabLoaded(tab.id, 15000);
      const openedTab = await chrome.tabs.get(tab.id);
      sendWsMessage({
        id, task_id, type: "response", success: true,
        data: {
          tab_id: tab.id,
          url: openedTab.url || recoveryUrl,
          active: openedTab.active,
          window_id: openedTab.windowId,
          mapped_as_task_tab: false,
        },
      });
    } catch (err) {
      console.error("AI Agent: failed to open Google recovery tab:", err);
      sendWsMessage({
        id, task_id, type: "response", success: false,
        error: `Could not open a separate Google recovery tab: ${err.message}`,
      });
    }
    return;
  }

  // ── 2. Close Task Tab ──
  if (type === "close_tab") {
    try {
      const tabId = taskTabMap.get(task_id);
      if (tabId) {
        try {
          await chrome.tabs.remove(tabId);
        } catch (e) {}
        taskTabMap.delete(task_id);
        tabTaskMap.delete(tabId);
        await saveMappings();
      }
      sendWsMessage({ id, task_id, type: "response", success: true });
    } catch (err) {
      sendWsMessage({ id, task_id, type: "response", success: false, error: err.message });
    }
    return;
  }

  // Validate task tab exists for page operations
  const tabId = taskTabMap.get(task_id);
  if (!tabId) {
    sendWsMessage({
      id,
      task_id,
      type: "response",
      success: false,
      error: `No Chrome tab registered for task ${task_id}`,
    });
    return;
  }

  // Verify tab is still open
  try {
    await chrome.tabs.get(tabId);
  } catch (e) {
    taskTabMap.delete(task_id);
    tabTaskMap.delete(tabId);
    await saveMappings();
    sendWsMessage({
      id,
      task_id,
      type: "response",
      success: false,
      error: `Chrome tab for task ${task_id} has been closed`,
    });
    return;
  }

  // Bring the task tab to the foreground so the user can complete
  // account sign-in or security challenges in the exact session used by the agent.
  if (type === "focus_tab") {
    try {
      // Focus the existing Chrome window without changing its dimensions.
      // Only restore a minimized window; preserve maximized/normal state.
      const currentTab = await chrome.tabs.get(tabId);
      if (currentTab.windowId === undefined) {
        throw new Error("The task tab is not attached to a Chrome window.");
      }
      const currentWindow = await chrome.windows.get(currentTab.windowId);
      if (currentWindow.state === "minimized") {
        await chrome.windows.update(currentTab.windowId, { state: "maximized" });
      }
      await chrome.windows.update(currentTab.windowId, { focused: true });
      const focusedTab = await chrome.tabs.update(tabId, { active: true });
      const verifiedTab = await chrome.tabs.get(tabId);
      if (!verifiedTab.active) {
        throw new Error("Chrome did not activate the task tab. Check Chrome window focus and extension permissions.");
      }
      sendWsMessage({
        id,
        task_id,
        type: "response",
        success: true,
        data: { tab_id: tabId, url: focusedTab.url || verifiedTab.url, active: true },
      });
    } catch (err) {
      sendWsMessage({
        id,
        task_id,
        type: "response",
        success: false,
        error: `Could not focus task tab: ${err.message}`,
      });
    }
    return;
  }

  // ── 3. Navigate ──
  if (type === "navigate") {
    try {
      const targetUrl = msg.url;
      await chrome.tabs.update(tabId, { url: targetUrl });
      await waitForTabLoaded(tabId);
      await ensureContentScriptInjected(tabId);
      const updatedTab = await chrome.tabs.get(tabId);

      sendWsMessage({
        id,
        task_id,
        type: "response",
        success: true,
        data: { url: updatedTab.url, title: updatedTab.title },
      });
    } catch (err) {
      sendWsMessage({
        id,
        task_id,
        type: "response",
        success: false,
        error: `Navigation failed: ${err.message}`,
      });
    }
    return;
  }

  // ── 4. Capture Screenshot ──
  if (type === "capture_screenshot") {
    try {
      // Do not force active: true; only capture if the user is already viewing the tab
      const tab = await chrome.tabs.get(tabId);
      let dataUrl = null;
      if (tab && tab.active) {
        dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId || null, { format: "png" });
      }

      sendWsMessage({
        id,
        task_id,
        type: "response",
        success: true,
        data: { screenshot: dataUrl },
      });
    } catch (err) {
      // Return empty screenshot if capture fails (e.g. backgrounded window)
      sendWsMessage({
        id,
        task_id,
        type: "response",
        success: true,
        data: { screenshot: null, warning: err.message },
      });
    }
    return;
  }

  // ── 5. DOM Inspection ──
  if (type === "inspect_dom") {
    try {
      // DOM inspection is safe in an inactive tab. Never activate/focus the
      // task tab here: the planner inspects repeatedly between actions, and
      // focusing on every inspection steals the user's active Chrome tab.
      const response = await sendContentMessage(tabId, {
        type: "inspect_dom",
        show_badges: msg.show_badges,
      });

      const liveTab = await chrome.tabs.get(tabId);
      const inspectionData = response && response.data ? {
        ...response.data,
        // The browser's tab URL is authoritative; content-script state can
        // lag during SPA redirects or after a navigation.
        url: liveTab.url || response.data.url || "",
        browser_tab_url: liveTab.url || "",
      } : null;
      sendWsMessage({
        id,
        task_id,
        type: "response",
        success: response ? response.success : false,
        data: inspectionData,
        error: response ? response.error : "No response from page content script",
      });
    } catch (err) {
      sendWsMessage({
        id,
        task_id,
        type: "response",
        success: false,
        error: `DOM inspection failed: ${err.message}`,
      });
    }
    return;
  }

  // ── 6. Execute Browser Action ──
  if (type === "execute_action") {
    try {
      // Execute against the mapped task tab without activating it. This lets
      // the user keep working in other Chrome tabs while automation continues.
      // Explicit focus is reserved for user-requested recovery flows only.
      const action = msg.action || {};

      // FINISH is a task-control action, not a page interaction. It must not
      // depend on a content-script listener being alive after navigation.
      // Best-effort badge cleanup is cosmetic; it must never turn a verified
      // task completion into "Receiving end does not exist".
      if (String(action.action || "").toLowerCase() === "finish") {
        try {
          await sendContentMessage(tabId, { type: "clear_badges" });
        } catch (cleanupError) {
          console.debug("[AI Extension] Badge cleanup skipped during FINISH:", cleanupError.message);
        }
        sendWsMessage({
          id,
          task_id,
          type: "response",
          success: true,
          data: {
            description: "Finish action acknowledged by extension background worker",
            extracted_data: action.extracted_data,
          },
        });
        return;
      }

      // If action is navigate, handle at background level
      if (action.action === "navigate") {
        const navUrl = action.url || action.text || "";
        if (navUrl) {
          await chrome.tabs.update(tabId, { url: navUrl });
          await waitForTabLoaded(tabId);
          await ensureContentScriptInjected(tabId);
          const tab = await chrome.tabs.get(tabId);
          sendWsMessage({
            id,
            task_id,
            type: "response",
            success: true,
            data: { description: `Navigated to ${tab.url}`, url: tab.url },
          });
          return;
        }
      }

      const result = await sendContentMessage(tabId, {
        type: "execute_action",
        action: action,
      });

      sendWsMessage({
        id,
        task_id,
        type: "response",
        success: result ? result.success : false,
        data: result,
        error: result ? result.error : "No response from page content script",
      });
    } catch (err) {
      sendWsMessage({
        id,
        task_id,
        type: "response",
        success: false,
        error: `Action execution error: ${err.message}`,
      });
    }
    return;
  }

  // ── 7. Clear Badges ──
  if (type === "clear_badges") {
    try {
      await sendContentMessage(tabId, { type: "clear_badges" });
      sendWsMessage({ id, task_id, type: "response", success: true });
    } catch (err) {
      sendWsMessage({ id, task_id, type: "response", success: true });
    }
    return;
  }

  // ── 8. Lock UI ──
  if (type === "lock_ui" || type === "unlock_ui") {
    const command = type;
    try {
      await ensureContentScriptInjected(tabId);
      // Send the command to every frame, not only the top document. Google Forms
      // and upload widgets can put interactive controls inside child frames.
      const results = await chrome.scripting.executeScript({
        target: { tabId, allFrames: true },
        world: "ISOLATED",
        func: (lockCommand) => {
          const detail = { command: lockCommand, result: false };
          window.dispatchEvent(new CustomEvent("ai-agent-ui-lock-command", { detail }));
          return detail.result;
        },
        args: [command],
      });
      if (command === "lock_ui") {
        const failedFrames = (results || []).filter((frame) => frame.result !== true);
        if (!results?.length || failedFrames.length) {
          throw new Error(`Lock was not confirmed in ${failedFrames.length || "any"} frame(s).`);
        }
      }
      sendWsMessage({
        id,
        task_id,
        type: "response",
        success: true,
        data: {
          locked: command === "lock_ui",
          frames: results?.length || 0,
          confirmed_frames: (results || []).filter((frame) => frame.result === true).length,
        },
      });
    } catch (err) {
      sendWsMessage({
        id,
        task_id,
        type: "response",
        success: false,
        error: `Failed to ${command === "lock_ui" ? "lock" : "unlock"} UI: ${err.message}`,
      });
    }
    return;
  }
}

// ---------------------------------------------------------------------------
// Tab Lifecycle Event Listeners
// ---------------------------------------------------------------------------

chrome.tabs.onRemoved.addListener(async (tabId) => {
  const taskId = tabTaskMap.get(tabId);
  if (taskId) {
    taskTabMap.delete(taskId);
    tabTaskMap.delete(tabId);
    await saveMappings();

    // Notify backend
    sendWsMessage({
      type: "tab_closed",
      task_id: taskId,
      tab_id: tabId,
    });
  }
});

// ---------------------------------------------------------------------------
// Runtime Messages (from Popup)
// ---------------------------------------------------------------------------

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  (async () => {
    if (message.type === "get_status") {
      const { isConnected } = await chrome.storage.local.get("isConnected");
      const activeTasks = Array.from(taskTabMap.entries()).map(([taskId, tabId]) => ({
        task_id: taskId,
        tab_id: tabId,
      }));
      sendResponse({
        connected: Boolean(isConnected),
        socket_state: socket ? socket.readyState : -1,
        active_tasks: activeTasks,
      });
    } else if (message.type === "reconnect") {
      if (socket) {
        try {
          socket.close();
        } catch (e) {}
      }
      await connectWebSocket();
      sendResponse({ success: true });
    }
  })();
  return true;
});

// ---------------------------------------------------------------------------
// Initial Launch
// ---------------------------------------------------------------------------

loadSettings().then(() => {
  connectWebSocket();
});
