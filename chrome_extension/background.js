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

// Match the backend's usual Uvicorn bind address. On some Windows setups
// "localhost" resolves to ::1 while Uvicorn is listening only on 127.0.0.1.
const DEFAULT_SERVER_URL = "ws://127.0.0.1:8000/api/extension/ws";
const LEGACY_LOCAL_SERVER_URLS = new Set([
  "ws://localhost:8000/api/extension/ws",
  "ws://localhost:8000/ws/extension",
]);
let socket = null;
let reconnectTimer = null;
let pingInterval = null;
let isConnecting = false;
let isAuthenticated = false;
let isRevoked = false;
let lastConnectionError = null;
let lastConnectedAt = null;

// In-memory task mapping: task_id -> tab_id
let taskTabMap = new Map();
// Reverse mapping: tab_id -> task_id
let tabTaskMap = new Map();

// ---------------------------------------------------------------------------
// Storage Sync Helpers
// ---------------------------------------------------------------------------

async function loadSettings() {
  const data = await chrome.storage.local.get(["serverUrl", "taskMappings", "extensionToken"]);
  // Migrate the old localhost default. Keep any explicitly configured remote
  // server URL untouched.
  const storedUrl = String(data.serverUrl || "").trim();
  const serverUrl = !storedUrl || LEGACY_LOCAL_SERVER_URLS.has(storedUrl)
    ? DEFAULT_SERVER_URL
    : storedUrl;
  if (serverUrl !== storedUrl) {
    await chrome.storage.local.set({ serverUrl });
  }
  if (data.taskMappings) {
    taskTabMap = new Map(Object.entries(data.taskMappings));
    tabTaskMap.clear();
    for (const [taskId, tabId] of taskTabMap.entries()) {
      tabTaskMap.set(Number(tabId), taskId);
    }
  }
  return { serverUrl, extensionToken: data.extensionToken || null };
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

  try {
    if (socket) {
      try {
        socket.onopen = null;
        socket.onmessage = null;
        socket.onerror = null;
        socket.onclose = null;
        if (socket.readyState !== WebSocket.CLOSED) {
          socket.close();
        }
      } catch (e) {}
      socket = null;
    }

    const { serverUrl } = await loadSettings();
    console.log(`[AI Extension] Connecting to ${serverUrl}...`);
    const candidate = new WebSocket(serverUrl);
    socket = candidate;

    candidate.onopen = async () => {
      if (socket !== candidate) return;
      console.log("[AI Extension] Connected to FastAPI backend. Awaiting registration handshake...");
      isConnecting = false;
      isAuthenticated = false;
      lastConnectionError = null;

      // Send initial registration handshake
      const { extensionToken } = await loadSettings();
      const taskTabsObj = {};
      for (const [taskId, tabId] of taskTabMap.entries()) {
        taskTabsObj[taskId] = tabId;
      }
      sendWsMessage({
        type: "register",
        client: "chrome-extension",
        version: "1.0.0",
        token: extensionToken || null,
        extension_id: (typeof chrome !== "undefined" && chrome.runtime?.id) ? chrome.runtime.id : "chrome-extension",
        active_tasks: Array.from(taskTabMap.keys()),
        task_tabs: taskTabsObj,
      });
    };

    candidate.onmessage = async (event) => {
      if (socket !== candidate) return;
      let message;
      try {
        message = JSON.parse(event.data);
      } catch (err) {
        console.error("[AI Extension] JSON parse error:", err);
        return;
      }
      try {
        await handleBackendMessage(message);
      } catch (err) {
        console.error("[AI Extension] Error handling message:", err);
        if (message && message.id) {
          sendWsMessage({
            id: message.id,
            task_id: message.task_id,
            type: "response",
            success: false,
            error: `Extension internal error handling ${message.type}: ${err.message}`,
          });
        }
      }
    };

    candidate.onclose = async (event) => {
      if (socket !== candidate) return;
      console.warn("[AI Extension] WebSocket disconnected. Code:", event.code, event.reason || "");
      isConnecting = false;
      isAuthenticated = false;
      lastConnectionError = lastConnectionError || `WebSocket closed (code ${event.code})`;
      if (pingInterval) clearInterval(pingInterval);
      await updateBadgeStatus(false);
      await chrome.storage.local.set({ isConnected: false, isAuthenticated: false, lastConnectionError });

      // Schedule reconnection with backoff only if not explicitly revoked and not an auth failure (code 4003)
      if (!reconnectTimer && !isRevoked && event.code !== 4003) {
        reconnectTimer = setTimeout(() => {
          reconnectTimer = null;
          connectWebSocket();
        }, 3000);
      }
    };

    candidate.onerror = (error) => {
      if (socket !== candidate) return;
      console.error("[AI Extension] WebSocket error:", error);
      lastConnectionError = `Could not connect to ${candidate.url}. Confirm the backend is running and the WebSocket route is reachable.`;
      chrome.storage.local.set({ isConnected: false, lastConnectionError }).catch(() => {});
      updateBadgeStatus(false);
    };
  } catch (err) {
    console.error("[AI Extension] Connection failed:", err);
    isConnecting = false;
    lastConnectionError = err?.message || String(err);
    await chrome.storage.local.set({ isConnected: false, lastConnectionError }).catch(() => {});
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

function isRestrictedUrl(url) {
  if (!url) return false;
  const lower = url.toLowerCase().trim();
  return (
    lower === 'about:blank' ||
    lower.startsWith('about:') ||
    lower.startsWith('chrome://') ||
    lower.startsWith('chrome-extension://') ||
    lower.startsWith('devtools://') ||
    lower.startsWith('edge://') ||
    lower.startsWith('view-source:') ||
    lower.startsWith('data:') ||
    lower.startsWith('javascript:') ||
    lower.startsWith('file:') ||
    lower.startsWith('http://127.0.0.1:8000') ||
    lower.startsWith('http://localhost:8000') ||
    lower.startsWith('http://127.0.0.1:5173') ||
    lower.startsWith('http://localhost:5173') ||
    lower.startsWith('https://chrome.google.com/webstore') ||
    lower.startsWith('https://chromewebstore.google.com')
  );
}

async function ensureContentScriptInjected(tabId) {
  const getScriptableTab = async () => {
    const tab = await chrome.tabs.get(tabId);
    const pageUrl = tab?.url || "";
    if (isRestrictedUrl(pageUrl)) {
      throw new Error(`Content scripts cannot run on this browser page: ${pageUrl}`);
    }
    return tab;
  };

  const ping = async () => {
    try {
      const response = await chrome.tabs.sendMessage(tabId, { type: "ping" }, { frameId: 0 });
      return Boolean(response && response.pong && response.is_top_frame !== false);
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
      const loaded = await waitForTabLoaded(tabId, 2500);
      if (!loaded && attempt === 0) {
        // Document still loading; continue checking ping or injection
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
    if (await waitForPing([150, 250, 400])) return true;
    await new Promise((resolve) => setTimeout(resolve, 200 * (attempt + 1)));
  }

  const finalTab = await chrome.tabs.get(tabId).catch(() => null);
  const finalUrl = finalTab?.url || "unknown URL";
  const injectionDetail = lastInjectionError?.message
    ? ` Last injection error: ${lastInjectionError.message}`
    : "";
  throw new Error(
    `Browser content script did not become ready for tab ${tabId} (${finalUrl}) after injection.${injectionDetail} Reload the task tab and retry.`
  );
}

// Dispatch trusted browser input through Chrome DevTools Protocol. This does not
// synthesize DOM events; Chrome generates trusted pointer events for the page.
async function dispatchNativePointerClick(tabId, action) {
  const resolved = await sendContentMessage(tabId, {
    type: "resolve_native_click_target",
    action,
  }, { frameId: 0 });

  if (!resolved?.success || !resolved.data) {
    throw new Error(resolved?.error || "Could not resolve a unique, unobscured click target.");
  }

  const { x, y, target, button = "left" } = resolved.data;
  if (!Number.isFinite(x) || !Number.isFinite(y)) {
    throw new Error("Native click target returned invalid viewport coordinates.");
  }

  const debuggee = { tabId };
  let attachedHere = false;
  try {
    try {
      await chrome.debugger.attach(debuggee, "1.3");
      attachedHere = true;
    } catch (attachError) {
      // A debugger session may already be attached by this extension. Do not
      // take ownership of or detach a session that we did not create.
      if (!/already attached|Another debugger is already attached/i.test(attachError.message || "")) {
        throw new Error("Chrome native input could not attach to the task tab: " + attachError.message);
      }
    }

    const clickCount = String(action.action || "").toLowerCase() === "double_click" ? 2 : 1;
    await chrome.debugger.sendCommand(debuggee, "Input.dispatchMouseEvent", {
      type: "mouseMoved", x, y, button: "none",
    });
    await chrome.debugger.sendCommand(debuggee, "Input.dispatchMouseEvent", {
      type: "mousePressed", x, y, button, clickCount,
      ...(button === "right" ? { buttons: 2 } : { buttons: 1 }),
    });
    await chrome.debugger.sendCommand(debuggee, "Input.dispatchMouseEvent", {
      type: "mouseReleased", x, y, button, clickCount, buttons: 0,
    });

    return {
      success: true,
      data: {
        description: `Native ${clickCount === 2 ? "double-" : ""}${button} click dispatched to ${target || "resolved target"}`,
        native_input: true,
        input_method: "chrome-debugger-cdp",
        target,
        coordinates: { x, y },
      },
    };
  } finally {
    if (attachedHere) {
      await chrome.debugger.detach(debuggee).catch(() => {});
    }
  }
}

async function sendContentMessage(tabId, message, options = { frameId: 0 }) {
  await ensureContentScriptInjected(tabId);
  try {
    return await chrome.tabs.sendMessage(tabId, message, options);
  } catch (error) {
    // For read-only inspection or informational queries, a document transition / BFCache entry
    // can be recovered by waiting for the new document and re-querying once.
    // For action execution, we do NOT re-dispatch the action here to prevent duplicate actions.
    const isChannelClosed = /receiving end does not exist|could not establish connection|back\/forward cache|message channel.*closed/i.test(error.message || "");
    if (isChannelClosed && message && message.type !== "execute_action") {
      await waitForTabLoaded(tabId, 10000);
      await ensureContentScriptInjected(tabId);
      return await chrome.tabs.sendMessage(tabId, message, options);
    }
    throw error;
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

async function waitForTabLoaded(tabId, timeoutMs = 12000, options = {}) {
  const { expectNavigation = false, minWaitMs = 100 } = options;
  // If we do not expect navigation to start, a tab that is already complete is immediately ready
  if (!expectNavigation) {
    try {
      const tab = await chrome.tabs.get(tabId);
      if (tab && tab.status === "complete") {
        return true;
      }
    } catch (e) {}
  }

  // Give Chrome a short slice to transition tab.status to 'loading' if navigation just initiated
  if (minWaitMs > 0) {
    await new Promise((r) => setTimeout(r, minWaitMs));
  }

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

    // If navigation settled while adding the listener, check current status
    chrome.tabs.get(tabId).then((t) => {
      if (t && t.status === "complete" && !resolved) {
        if (!expectNavigation) {
          resolved = true;
          clearTimeout(timer);
          chrome.tabs.onUpdated.removeListener(listener);
          resolve(true);
        }
      }
    }).catch(() => {});
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

  if (type === "registered") {
    console.log("[AI Extension] Successfully authenticated and paired with backend.");
    isAuthenticated = true;
    isRevoked = false;
    lastConnectionError = null;
    lastConnectedAt = new Date().toISOString();
    await updateBadgeStatus(true);
    const storageUpdate = {
      isConnected: true,
      isAuthenticated: true,
      isRevoked: false,
      lastConnectionError: null,
      lastConnectedAt,
    };
    if (msg.token) {
      storageUpdate.extensionToken = msg.token;
    }
    await chrome.storage.local.set(storageUpdate);

    // Start ping heartbeat every 20s
    if (pingInterval) clearInterval(pingInterval);
    pingInterval = setInterval(() => {
      if (socket && socket.readyState === WebSocket.OPEN && isAuthenticated) {
        sendWsMessage({ type: "ping", timestamp: Date.now() });
      }
    }, 20000);
    return;
  }

  if (type === "auth_failed") {
    console.warn("[AI Extension] Authentication failed:", msg.reason);
    isAuthenticated = false;
    await updateBadgeStatus(false);
    const reason = msg.reason || "Extension authentication failed";
    lastConnectionError = reason;

    const isRevocation = Boolean(reason && reason.toLowerCase().includes("revoked"));
    if (isRevocation) {
      isRevoked = true;
      // When explicitly revoked, do NOT delete token or auto-reconnect to prevent silently hijacking TOFU
      await chrome.storage.local.set({
        isConnected: false,
        isAuthenticated: false,
        isRevoked: true,
        lastConnectionError: reason,
      });
    } else {
      // If token is invalid or rejected, remove stale token so user can re-pair explicitly
      await chrome.storage.local.remove(["extensionToken"]);
      await chrome.storage.local.set({
        isConnected: false,
        isAuthenticated: false,
        isRevoked: false,
        lastConnectionError: reason,
      });
    }

    if (socket) {
      try {
        socket.close(4003, reason);
      } catch (e) {}
    }
    return;
  }

  // ── 1. Create Task Tab ──
  if (type === "create_tab") {
    let createdTabId = null;
    try {
      const url = msg.url || "about:blank";

      // 1. Re-use existing mapped tab for this task if still open
      const existingTabId = taskTabMap.get(task_id);
      if (existingTabId !== undefined) {
        try {
          const existingTab = await chrome.tabs.get(existingTabId);
          if (existingTab && !isRestrictedUrl(existingTab.url)) {
            const needsNav = (url && url !== "about:blank" && !existingTab.url?.startsWith(url.split("#")[0])) ||
              existingTab.url?.includes("/file/d/");
            if (needsNav) {
              await chrome.tabs.update(existingTab.id, { url, active: true });
              await waitForTabLoaded(existingTab.id, 12000);
            } else {
              await chrome.tabs.update(existingTab.id, { active: true });
            }
            await ensureContentScriptInjected(existingTab.id);
            const currentTab = await chrome.tabs.get(existingTab.id).catch(() => existingTab);
            sendWsMessage({
              id,
              task_id,
              type: "response",
              success: true,
              data: { tab_id: existingTab.id, url: currentTab.url || url },
            });
            return;
          }
        } catch (e) {
          taskTabMap.delete(task_id);
          tabTaskMap.delete(existingTabId);
        }
      }

      // 2. Re-use an existing open tab in Chrome matching the target domain/URL
      // if it is not claimed by another active task. This prevents opening duplicate
      // tabs when Google Drive or a target site is already open or during retries.
      if (url && url !== "about:blank") {
        try {
          const targetUrlObj = new URL(url);
          const openTabs = await chrome.tabs.query({});
          for (const openTab of openTabs) {
            if (!openTab.id || !openTab.url || isRestrictedUrl(openTab.url)) continue;
            const assignedTask = tabTaskMap.get(openTab.id);
            if (assignedTask && assignedTask !== task_id) continue;

            try {
              const openUrlObj = new URL(openTab.url);
              if (openUrlObj.hostname === targetUrlObj.hostname) {
                taskTabMap.set(task_id, openTab.id);
                tabTaskMap.set(openTab.id, task_id);
                await saveMappings();

                const shouldNavigate = openTab.url.includes("/file/d/") ||
                  !openTab.url.startsWith(url.split("#")[0]) ||
                  (targetUrlObj.pathname && targetUrlObj.pathname !== "/" && !openUrlObj.pathname.startsWith(targetUrlObj.pathname));

                if (shouldNavigate) {
                  await chrome.tabs.update(openTab.id, { url, active: true });
                  await waitForTabLoaded(openTab.id, 12000);
                } else {
                  await chrome.tabs.update(openTab.id, { active: true });
                }
                await ensureContentScriptInjected(openTab.id);
                const currentTab = await chrome.tabs.get(openTab.id).catch(() => openTab);
                sendWsMessage({
                  id,
                  task_id,
                  type: "response",
                  success: true,
                  data: { tab_id: openTab.id, url: currentTab.url || url },
                });
                return;
              }
            } catch (uErr) {}
          }
        } catch (qErr) {}
      }

      // 3. Otherwise create a dedicated task tab
      const tab = await chrome.tabs.create({ url, active: false });
      createdTabId = tab.id;

      taskTabMap.set(task_id, tab.id);
      tabTaskMap.set(tab.id, task_id);
      await saveMappings();

      // Chrome does not allow content-script injection into about:blank,
      // even with <all_urls>. A blank task tab is only a temporary container:
      // the backend will navigate it before requesting DOM inspection/actions.
      // Inject only after a real, scriptable document has loaded.
      if (url && !isRestrictedUrl(url)) {
        await waitForTabLoaded(tab.id, 12000);
        const loadedTab = await chrome.tabs.get(tab.id);
        if (loadedTab.url && !isRestrictedUrl(loadedTab.url)) {
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
      if (!isRestrictedUrl(targetUrl)) {
        await ensureContentScriptInjected(tabId);
      }
      // Allow complex client-side SPAs (Google Drive, Docs, Gmail) a brief stabilization window
      // after document load for initial client-side framework bootstrapping
      if (/drive\.google\.com|mail\.google\.com|docs\.google\.com/i.test(targetUrl)) {
        await new Promise((r) => setTimeout(r, 1200));
      }
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
        try {
          await sendContentMessage(tabId, { type: "mask_sensitive_elements" });
        } catch (_) {}
        dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId || null, { format: "png" });
        try {
          await sendContentMessage(tabId, { type: "unmask_sensitive_elements" });
        } catch (_) {}
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
      const liveTab = await chrome.tabs.get(tabId);
      const currentUrl = liveTab?.url || "";
      if (isRestrictedUrl(currentUrl) || !currentUrl) {
        // Restricted or blank pages (about:blank, chrome://) cannot run content scripts.
        // Return a clean, non-interactive live document snapshot so the backend can
        // plan navigation or handle initial blank state without failing closed.
        sendWsMessage({
          id,
          task_id,
          type: "response",
          success: true,
          data: {
            url: currentUrl || "about:blank",
            browser_tab_url: currentUrl || "about:blank",
            title: liveTab?.title || "",
            elements: [],
            page_text: "",
            headings: [],
            form_questions: [],
            form_fields: [],
            compact_forms: { form_count: 0, fields: [] },
            restricted: true,
          },
        });
        return;
      }

      // DOM inspection is safe in an inactive tab. Never activate/focus the
      // task tab here: the planner inspects repeatedly between actions, and
      // focusing on every inspection steals the user's active Chrome tab.
      const response = await sendContentMessage(tabId, {
        type: "inspect_dom",
        show_badges: msg.show_badges,
      }, { frameId: 0 });

      const updatedTab = await chrome.tabs.get(tabId).catch(() => liveTab);
      const inspectionData = response && response.data ? {
        ...response.data,
        // The browser's tab URL is authoritative; content-script state can
        // lag during SPA redirects or after a navigation.
        url: updatedTab?.url || response.data.url || currentUrl,
        browser_tab_url: updatedTab?.url || currentUrl,
        frame_url: response.data.url || updatedTab?.url || currentUrl,
        is_top_frame: response.data.is_top_frame !== false,
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
          if (!isRestrictedUrl(navUrl)) {
            await ensureContentScriptInjected(tabId);
          }
          if (/drive\.google\.com|mail\.google\.com|docs\.google\.com/i.test(navUrl)) {
            await new Promise((r) => setTimeout(r, 1500));
          }
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

      // Pointer actions must use trusted browser input. DOM-dispatched MouseEvents and
      // HTMLElement.click() are synthetic (isTrusted=false) and are ignored by some
      // application controls. Keep DOM for target resolution, but let Chrome deliver input.
      // Exception: popup/menu triggers (e.g. Google Drive "+ New", dropdown menus) are
      // instantly dismissed if the Chrome debugger attaches and detaches (which blurs the window).
      // Dispatch those directly via content script DOM events so the menu stays open.
      const pointerAction = ["click", "double_click", "right_click"].includes(
        String(action.action || "").toLowerCase()
      );
      const isMenuTrigger = /^(new|\+ new|\+new)$/i.test(String(action.text || "").trim()) ||
        /new button|menu|dropdown|popup/i.test(String(action.reasoning || ""));
      if (pointerAction && !isMenuTrigger) {
        // The task-tab interaction guard normally blocks trusted user input. Temporarily
        // permit input for this tab while CDP dispatches the agent's pointer sequence.
        await sendContentMessage(tabId, {
          type: "native_input_state",
          enabled: true,
        }).catch(() => {});
        try {
          const nativeResult = await dispatchNativePointerClick(tabId, action);
          if (nativeResult?.success) {
            await new Promise((resolve) => setTimeout(resolve, 350));
            sendWsMessage({
              id,
              task_id,
              type: "response",
              success: true,
              data: {
                ...nativeResult.data,
                description: nativeResult.data.description + "; page state must be re-inspected before the next action",
                details: { postcondition: "input_dispatched_unverified" },
              },
            });
            return;
          }
        } catch (nativeErr) {
          console.warn("[AI Extension] Native pointer input failed, falling back to DOM action:", nativeErr.message);
        } finally {
          await sendContentMessage(tabId, {
            type: "native_input_state",
            enabled: false,
          }).catch(() => {});
        }
      }

      // Capture pre-action tab URL to verify whether genuine navigation occurred
      const preActionTab = await chrome.tabs.get(tabId).catch(() => null);
      const preActionUrl = preActionTab?.url || "";

      let result;
      try {
        result = await sendContentMessage(tabId, {
          type: "execute_action",
          action: action,
        });
      } catch (contentErr) {
        // Detect BFCache entry and message channel closure caused by action-induced navigation
        const isBfCacheOrChannelClosed = /back\/forward cache|message channel.*closed|port is closed/i.test(contentErr.message || "");
        if (isBfCacheOrChannelClosed) {
          // Wait for any in-flight navigation / document transition to settle
          await waitForTabLoaded(tabId, 12000);
          const postActionTab = await chrome.tabs.get(tabId).catch(() => null);
          const postActionUrl = postActionTab?.url || "";

          // Verify whether genuine navigation occurred by checking URL change
          const urlChanged = Boolean(
            postActionUrl &&
            preActionUrl &&
            postActionUrl !== preActionUrl &&
            postActionUrl !== "about:blank"
          );

          if (urlChanged) {
            // Re-establish content script readiness in the new top-level document using frameId: 0
            if (!isRestrictedUrl(postActionUrl)) {
              await ensureContentScriptInjected(tabId);
            }
            sendWsMessage({
              id,
              task_id,
              type: "response",
              success: true,
              data: {
                description: `Action [${action.action || "click"}] navigated to ${postActionUrl}`,
                url: postActionUrl,
                navigated: true,
                target: action.selector || action.element_id || action.text,
              },
            });
            return;
          }

          // If navigation could not be verified (e.g. tab URL did not change),
          // do NOT treat channel closure as successful navigation. Report an uncertain outcome
          // so recovery pauses without repeating a potentially side-effecting action.
          sendWsMessage({
            id,
            task_id,
            type: "response",
            success: false,
            error: `Action execution error: Message channel closed during [${action.action || "action"}], but navigation could not be verified (tab URL unchanged: ${postActionUrl || preActionUrl}). Inspect page state before retrying.`,
          });
          return;
        }

        // Re-throw any other execution error
        throw contentErr;
      }

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
      const currentTab = await chrome.tabs.get(tabId);
      const currentUrl = currentTab?.url || "";
      if (isRestrictedUrl(currentUrl) || !currentUrl) {
        // Restricted or blank pages (about:blank, chrome://) cannot run content scripts
        // and do not need a DOM interaction overlay. Acknowledge immediately.
        sendWsMessage({
          id,
          task_id,
          type: "response",
          success: true,
          data: {
            locked: command === "lock_ui",
            restricted: true,
            url: currentUrl,
            message: "Page is blank or restricted; content script lock overlay skipped.",
          },
        });
        return;
      }

      // If tab is still loading (not yet complete), skip scripting and fast-ack.
      // The overlay will be applied on the next lock_ui call after navigation completes.
      if (currentTab.status !== "complete") {
        sendWsMessage({
          id,
          task_id,
          type: "response",
          success: true,
          data: {
            locked: command === "lock_ui",
            loading: true,
            message: "Tab still loading; lock overlay deferred until page is ready.",
          },
        });
        return;
      }

      await ensureContentScriptInjected(tabId);
      // Send the command to every frame, not only the top document. Google Forms
      // and upload widgets can put interactive controls inside child frames.
      let results = [];
      try {
        results = await chrome.scripting.executeScript({
          target: { tabId, allFrames: true },
          world: "ISOLATED",
          func: (lockCommand) => {
            const detail = { command: lockCommand, result: false };
            window.dispatchEvent(new CustomEvent("ai-agent-ui-lock-command", { detail }));
            return detail.result;
          },
          args: [command],
        });
      } catch (scriptErr) {
        // Fallback: try targeting only the top frame if allFrames fails
        try {
          results = await chrome.scripting.executeScript({
            target: { tabId },
            world: "ISOLATED",
            func: (lockCommand) => {
              const detail = { command: lockCommand, result: false };
              window.dispatchEvent(new CustomEvent("ai-agent-ui-lock-command", { detail }));
              return detail.result;
            },
            args: [command],
          });
        } catch (topFrameErr) {
          // Page navigated or is unreachable — treat lock as best-effort success.
          console.warn(`[AI Agent] lock_ui scripting skipped (tab likely navigating): ${topFrameErr.message}`);
          sendWsMessage({
            id, task_id, type: "response", success: true,
            data: { locked: command === "lock_ui", scripting_skipped: true },
          });
          return;
        }
      }

      // For lock_ui, warn but don't fail if the overlay was not confirmed —
      // the backend now treats lock_ui as best-effort.
      if (command === "lock_ui") {
        const anyConfirmed = (results || []).some((frame) => frame.result === true);
        if (!anyConfirmed) {
          console.warn("[AI Agent] lock_ui overlay not confirmed by any frame; task continues anyway.");
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
      // lock_ui / unlock_ui errors must not crash the task. Report best-effort success.
      console.warn(`[AI Agent] ${command} non-fatal error: ${err.message}`);
      sendWsMessage({
        id,
        task_id,
        type: "response",
        success: true,
        data: { locked: command === "lock_ui", error_suppressed: err.message },
      });
    }
    return;
  }

  // Unknown / unhandled command fallback
  sendWsMessage({
    id,
    task_id,
    type: "response",
    success: false,
    error: `Unrecognized extension command: ${type}`,
  });
}

// ---------------------------------------------------------------------------
// Tab Lifecycle Event Listeners (including popups and child tabs)
// ---------------------------------------------------------------------------

const taskChildTabsMap = new Map(); // taskId -> Set of child tabIds

if (typeof chrome !== "undefined" && chrome.tabs && chrome.tabs.onCreated) {
  chrome.tabs.onCreated.addListener(async (tab) => {
    if (tab && tab.openerTabId && tabTaskMap.has(tab.openerTabId)) {
      const taskId = tabTaskMap.get(tab.openerTabId);
      let childSet = taskChildTabsMap.get(taskId);
      if (!childSet) {
        childSet = new Set();
        taskChildTabsMap.set(taskId, childSet);
      }
      childSet.add(tab.id);
      tabTaskMap.set(tab.id, taskId);
      console.log(`[AI Extension] Tracked child tab ${tab.id} for task ${taskId} (opener: ${tab.openerTabId})`);
    }
  });
}

chrome.tabs.onRemoved.addListener(async (tabId) => {
  const taskId = tabTaskMap.get(tabId);
  if (taskId) {
    tabTaskMap.delete(tabId);
    const childSet = taskChildTabsMap.get(taskId);
    if (childSet) {
      childSet.delete(tabId);
    }

    // Only if the closed tab was the active primary tab for the task:
    if (taskTabMap.get(taskId) === tabId) {
      // If a child tab exists (e.g. popup or target="_blank" tab), promote it
      if (childSet && childSet.size > 0) {
        const remaining = Array.from(childSet);
        const nextTabId = remaining[remaining.length - 1];
        taskTabMap.set(taskId, nextTabId);
        await saveMappings();
        console.log(`[AI Extension] Primary tab ${tabId} closed; switched task ${taskId} to child tab ${nextTabId}`);
        return;
      }

      taskTabMap.delete(taskId);
      taskChildTabsMap.delete(taskId);
      await saveMappings();

      // Notify backend
      sendWsMessage({
        type: "tab_closed",
        task_id: taskId,
        tab_id: tabId,
      });
    }
  }
});

// ---------------------------------------------------------------------------
// Runtime Messages (from Popup)
// ---------------------------------------------------------------------------

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  (async () => {
    if (message.type === "get_status") {
      const { serverUrl } = await loadSettings();
      const connected = Boolean(socket && socket.readyState === WebSocket.OPEN && isAuthenticated);
      const activeTasks = Array.from(taskTabMap.entries()).map(([taskId, tabId]) => ({
        task_id: taskId,
        tab_id: tabId,
      }));
      sendResponse({
        connected,
        authenticated: isAuthenticated,
        is_revoked: isRevoked,
        socket_state: socket ? socket.readyState : -1,
        server_url: serverUrl,
        last_error: lastConnectionError,
        last_connected_at: lastConnectedAt,
        active_tasks: activeTasks,
      });
    } else if (message.type === "reconnect") {
      isRevoked = false;
      if (reconnectTimer) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
      }
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

// Keep-alive alarm to prevent MV3 service worker dormancy during idle connections
try {
  chrome.alarms.create("ws-keepalive", { periodInMinutes: 0.5 });
  chrome.alarms.onAlarm.addListener((alarm) => {
    if (alarm.name === "ws-keepalive") {
      if (!socket || socket.readyState !== WebSocket.OPEN) {
        connectWebSocket();
      }
    }
  });
} catch (_) {}
