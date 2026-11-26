"""
tests/test_browser_automation_reliability.py
---------------------------------------------
Comprehensive regression test suite for browser automation reliability:
1. Extension communication & BFCache handling.
2. Navigation detection, lifecycle, and child frame port scoping.
3. Tab & session state tracking across reconnects and child tabs/popups.
4. Action execution postcondition verification for UI state changes (dropdowns, modals, tabs, accordions).
5. Recovery idempotency and query-before-retry duplicate prevention.
6. Per-task command serialization and MV3 service-worker reconnection grace period.
"""

import asyncio
import subprocess
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.browser.recovery import (
    classify_browser_error,
    execute_with_retry_and_fallback,
    _check_action_already_satisfied,
    RetryPolicy,
)
from backend.browser.extension_adapter import ExtensionBrowserAdapter, ExtensionManager
from backend.models.schemas import BrowserAction, ActionType, FormField, FieldType
from backend.api.task_manager import TaskManager


# ==============================================================================
# 1. Extension Communication & Navigation Lifecycle Tests (Node.js VM)
# ==============================================================================

def test_extension_acknowledges_navigation_actions_early():
    """Verify content.js detects pagehide/beforeunload and acknowledges navigation early."""
    node_script = """
    const fs = require('fs');
    const contentSrc = fs.readFileSync('chrome_extension/content.js', 'utf8');
    const vm = require('vm');

    let listeners = {};
    let mockElement = {
      tagName: 'BUTTON',
      innerText: 'Next Page',
      getAttribute: () => null,
      getBoundingClientRect: () => ({ left: 0, top: 0, width: 100, height: 40 }),
      dispatchEvent: (evt) => {},
      focus: () => {},
      scrollIntoView: () => {},
      click: () => {
        // Trigger pagehide on click
        if (listeners['pagehide']) {
          listeners['pagehide']({ persisted: false });
        }
      },
      isConnected: true,
    };

    let mockWindow = {
      location: { href: 'https://example.com/step1' },
      addEventListener: (type, fn) => { listeners[type] = fn; },
      removeEventListener: (type, fn) => { delete listeners[type]; },
      document: {
        querySelector: () => mockElement,
        querySelectorAll: () => [],
        getElementById: () => null,
        body: { appendChild: () => {} },
      },
      requestAnimationFrame: (cb) => cb(),
    };
    mockWindow.top = mockWindow;
    mockWindow.self = mockWindow;

    let runtimeListener = null;
    let mockChrome = {
      runtime: {
        onMessage: {
          addListener: (fn) => { runtimeListener = fn; },
        },
      },
    };

    const ctx = vm.createContext({
      window: mockWindow,
      location: mockWindow.location,
      document: mockWindow.document,
      chrome: mockChrome,
      console: console,
      Node: { ELEMENT_NODE: 1 },
      MouseEvent: function(type, opts) { this.type = type; },
      KeyboardEvent: function(type, opts) { this.type = type; },
      setTimeout: setTimeout,
      clearTimeout: clearTimeout,
    });

    vm.runInContext(contentSrc, ctx);

    // Call execute_action
    runtimeListener({
      type: 'execute_action',
      action: { action: 'click', selector: 'button#next' },
    }, {}, (response) => {
      if (!response || !response.success) {
        console.error('Expected success response, got:', response);
        process.exit(1);
      }
      if (!response.details || !response.details.will_navigate) {
        console.error('Expected will_navigate: true, got:', response);
        process.exit(2);
      }
      process.exit(0);
    });
    """
    res = subprocess.run(["node", "-e", node_script], capture_output=True, text=True)
    assert res.returncode == 0, f"Node test failed: {res.stderr}\n{res.stdout}"


def test_extension_tracks_child_tabs_and_promotes_on_parent_close():
    """Verify background.js tracks child tabs/popups and promotes child tab when primary closes."""
    node_script = """
    const fs = require('fs');
    const bgSrc = fs.readFileSync('chrome_extension/background.js', 'utf8');
    const vm = require('vm');

    let onCreatedListener = null;
    let onRemovedListener = null;
    let wsMessages = [];

    class MockWebSocket {
      constructor() { this.readyState = 1; }
      send(str) { wsMessages.push(JSON.parse(str)); }
    }
    MockWebSocket.OPEN = 1;

    let storageState = {
      taskMappings: { 'task-42': 101 },
      extensionToken: 'test-token',
    };

    const mockChrome = {
      tabs: {
        get: async (id) => ({ id, url: 'https://example.com', status: 'complete' }),
        onCreated: {
          addListener: (fn) => { onCreatedListener = fn; },
        },
        onRemoved: {
          addListener: (fn) => { onRemovedListener = fn; },
        },
        onUpdated: { addListener: () => {}, removeListener: () => {} },
      },
      runtime: {
        onMessage: { addListener: () => {} },
      },
      alarms: {
        onAlarm: { addListener: () => {} },
        create: () => {},
      },
      storage: {
        local: {
          get: async () => storageState,
          set: async (obj) => { Object.assign(storageState, obj); },
        },
      },
    };

    const ctx = vm.createContext({
      chrome: mockChrome,
      WebSocket: MockWebSocket,
      console: console,
      setTimeout: setTimeout,
      clearTimeout: clearTimeout,
    });

    vm.runInContext(bgSrc, ctx);

    (async () => {
      await new Promise(r => setTimeout(r, 50));
      // Child tab (id=202) spawned by parent tab (id=101)
      await onCreatedListener({ id: 202, openerTabId: 101 });

      // Close parent tab 101
      await onRemovedListener(101);

      // Verify task-42 was promoted to child tab 202 instead of sending tab_closed
      const closedMsg = wsMessages.find(m => m.type === 'tab_closed');
      if (closedMsg) {
        console.error('Expected no tab_closed message while child tab is open, got:', closedMsg);
        process.exit(1);
      }
      if (storageState.taskMappings['task-42'] !== 202) {
        console.error('Expected task-42 promoted to tab 202, got:', storageState.taskMappings);
        process.exit(2);
      }

      // Now close child tab 202
      await onRemovedListener(202);
      const finalClosedMsg = wsMessages.find(m => m.type === 'tab_closed' && m.task_id === 'task-42');
      if (!finalClosedMsg) {
        console.error('Expected tab_closed message after all tabs closed, wsMessages:', wsMessages);
        process.exit(3);
      }

      process.exit(0);
    })().catch(err => {
      console.error('Execution error:', err);
      process.exit(4);
    });
    """
    res = subprocess.run(["node", "-e", node_script], capture_output=True, text=True)
    assert res.returncode == 0, f"Node test failed: {res.stderr}\n{res.stdout}"


# ==============================================================================
# 2. Backend Tab Mapping Preservation & Concurrency Serialization Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_extension_manager_preserves_task_tabs_on_disconnect():
    """Verify ExtensionManager retains task_tabs mapping across temporary disconnections."""
    mgr = ExtensionManager()
    ws1 = MagicMock()
    ws1.send_json = AsyncMock()

    await mgr.register(ws1)
    mgr.task_connections["task-1"] = ws1
    mgr.task_tabs["task-1"] = 12345

    # Simulate extension service worker idle disconnect
    await mgr.unregister(ws1)

    # Task tab must NOT be removed from task_tabs
    assert mgr.task_tabs.get("task-1") == 12345
    assert "task-1" in mgr.disconnected_tasks
    assert "task-1" not in mgr.task_connections

    # Simulate re-register message from reconnected extension
    ws2 = MagicMock()
    ws2.send_json = AsyncMock()
    await mgr.register(ws2)
    with patch("backend.security.extension_auth.extension_auth_manager.register_or_authenticate", return_value=(True, "test-token", "OK")):
        handled = await mgr.handle_message(ws2, {
            "type": "register",
            "active_tasks": ["task-1"],
            "task_tabs": {"task-1": 12345},
        })
    assert handled is True
    assert mgr.task_connections.get("task-1") is ws2
    assert mgr.task_tabs.get("task-1") == 12345
    assert "task-1" not in mgr.disconnected_tasks


@pytest.mark.asyncio
async def test_extension_manager_serializes_per_task_commands():
    """Verify that multiple commands for the same task are serialized and executed in order."""
    mgr = ExtensionManager()
    ws = MagicMock()
    call_order = []

    async def fake_send_json(msg):
        cmd_type = msg.get("type")
        call_order.append(f"start_{cmd_type}")
        await asyncio.sleep(0.05)
        call_order.append(f"end_{cmd_type}")
        # Resolve pending future immediately
        msg_id = msg.get("id")
        if msg_id in mgr.pending_requests:
            _, fut = mgr.pending_requests[msg_id]
            fut.set_result({"success": True, "type": cmd_type})

    ws.send_json = fake_send_json
    await mgr.register(ws)
    mgr.task_connections["task-test"] = ws

    # Launch two concurrent commands for task-test
    task_a = asyncio.create_task(mgr.send_command("task-test", "inspect_dom"))
    task_b = asyncio.create_task(mgr.send_command("task-test", "execute_action"))

    res_a, res_b = await asyncio.gather(task_a, task_b)
    assert res_a["success"] is True
    assert res_b["success"] is True

    # Confirm commands were strictly serialized (first finished before second started)
    assert call_order == [
        "start_inspect_dom",
        "end_inspect_dom",
        "start_execute_action",
        "end_execute_action",
    ]


@pytest.mark.asyncio
async def test_extension_manager_reconnection_grace_period():
    """Verify send_command waits for reconnected client during MV3 wake-up grace period."""
    mgr = ExtensionManager()
    # Task exists but connection is temporarily absent
    mgr.task_tokens["task-grace"] = "tok-grace"
    mgr.token_connections["tok-grace"] = None

    async def delayed_reconnect():
        await asyncio.sleep(0.15)
        ws_new = MagicMock()
        ws_new.send_json = AsyncMock(side_effect=lambda msg: mgr.pending_requests[msg["id"]][1].set_result({"success": True}))
        await mgr.register(ws_new)
        with patch("backend.security.extension_auth.extension_auth_manager.register_or_authenticate", return_value=(True, "tok-grace", "OK")):
            await mgr.handle_message(ws_new, {
                "type": "register",
                "token": "tok-grace",
                "active_tasks": ["task-grace"],
            })

    reconnect_task = asyncio.create_task(delayed_reconnect())
    res = await mgr.send_command("task-grace", "inspect_dom", timeout=5.0)
    await reconnect_task

    assert res.get("success") is True


# ==============================================================================
# 3. Action Execution Postcondition Verification Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_click_verifies_ui_state_changes_without_url_change():
    """Verify click postcondition succeeds on dropdowns, modals, tabs, and accordions."""
    tm = TaskManager()
    controller = MagicMock()

    # Case A: aria-expanded changed from false to true (Accordion / Dropdown)
    before_page = {
        "url": "https://example.com/app",
        "elements": [
            {"index": 1, "selector": "button#accordion", "text": "FAQ", "aria_expanded": "false"}
        ],
        "form_fields": [],
    }
    after_page = {
        "url": "https://example.com/app",
        "elements": [
            {"index": 1, "selector": "button#accordion", "text": "FAQ", "aria_expanded": "true"},
            {"index": 2, "selector": "div#faq-content", "text": "FAQ details"}
        ],
        "form_fields": [],
    }
    controller.parse_interactive_elements = AsyncMock(return_value=after_page)
    action = BrowserAction(action=ActionType.CLICK, selector="button#accordion", text="FAQ")

    verified, note = await tm._verify_extension_action_postcondition(action, controller, before_page=before_page)
    assert verified is True
    assert "Click produced an observable page or target-state change" in note

    # Case B: aria-selected changed (Tab navigation)
    before_tab = {
        "url": "https://example.com/dashboard",
        "elements": [
            {"index": 5, "selector": "div#tab-settings", "text": "Settings", "aria_selected": "false"}
        ],
    }
    after_tab = {
        "url": "https://example.com/dashboard",
        "elements": [
            {"index": 5, "selector": "div#tab-settings", "text": "Settings", "aria_selected": "true"}
        ],
    }
    controller.parse_interactive_elements = AsyncMock(return_value=after_tab)
    action_tab = BrowserAction(action=ActionType.CLICK, selector="div#tab-settings", text="Settings")
    verified, note = await tm._verify_extension_action_postcondition(action_tab, controller, before_page=before_tab)
    assert verified is True

    # Case C: Modal dialog opened (DOM elements count changed)
    before_modal = {
        "url": "https://example.com/items",
        "elements": [{"index": 1, "selector": "button#open-modal", "text": "Add Item"}],
    }
    after_modal = {
        "url": "https://example.com/items",
        "elements": [
            {"index": 1, "selector": "button#open-modal", "text": "Add Item"},
            {"index": 2, "selector": "div#modal", "text": "Item Modal Form"},
            {"index": 3, "selector": "input#item-name", "text": ""},
        ],
    }
    controller.parse_interactive_elements = AsyncMock(return_value=after_modal)
    action_modal = BrowserAction(action=ActionType.CLICK, selector="button#open-modal", text="Add Item")
    verified, note = await tm._verify_extension_action_postcondition(action_modal, controller, before_page=before_modal)
    assert verified is True

    # Case D: Unchanged Submit button with no DOM change fails verification
    before_submit = {
        "url": "https://example.com/form",
        "elements": [{"index": 10, "selector": "button#submit", "text": "Submit"}],
        "form_fields": [],
    }
    after_submit = {
        "url": "https://example.com/form",
        "elements": [{"index": 10, "selector": "button#submit", "text": "Submit"}],
        "form_fields": [],
    }
    controller.parse_interactive_elements = AsyncMock(return_value=after_submit)
    action_submit = BrowserAction(action=ActionType.CLICK, selector="button#submit", text="Submit")
    verified, note = await tm._verify_extension_action_postcondition(action_submit, controller, before_page=before_submit)
    assert verified is False
    assert "Click outcome is unverified" in note


@pytest.mark.asyncio
async def test_fill_and_select_postcondition_verification():
    """Verify text fill and select postconditions succeed and handle masked auth fields."""
    tm = TaskManager()
    controller = MagicMock()

    # Password / Auth field verification (redacted by security layer)
    after_password = {
        "url": "https://example.com/login",
        "form_fields": [
            FormField(
                field_type=FieldType.PASSWORD,
                selector="input#pwd",
                label="Password",
                current_value="",  # Redacted
            )
        ],
    }
    controller.parse_interactive_elements = AsyncMock(return_value=after_password)
    action_pwd = BrowserAction(action=ActionType.FILL, selector="input#pwd", value="Secret123!", field_label="Password")
    verified, note = await tm._verify_extension_action_postcondition(action_pwd, controller)
    assert verified is True
    assert "Password/auth field input accepted" in note

    # Regular text input verification
    after_text = {
        "url": "https://example.com/profile",
        "form_fields": [
            FormField(
                field_type=FieldType.TEXT,
                selector="input#name",
                label="Full Name",
                current_value="Alice Smith",
            )
        ],
    }
    controller.parse_interactive_elements = AsyncMock(return_value=after_text)
    action_text = BrowserAction(action=ActionType.FILL, selector="input#name", value="Alice Smith", field_label="Full Name")
    verified, note = await tm._verify_extension_action_postcondition(action_text, controller)
    assert verified is True
    assert "Text input value verified" in note

    # Select option verification
    after_select = {
        "url": "https://example.com/settings",
        "form_fields": [
            FormField(
                field_type=FieldType.DROPDOWN,
                selector="select#country",
                label="Country",
                current_value="United States",
            )
        ],
    }
    controller.parse_interactive_elements = AsyncMock(return_value=after_select)
    action_sel = BrowserAction(action=ActionType.SELECT, selector="select#country", select_label="United States")
    verified, note = await tm._verify_extension_action_postcondition(action_sel, controller)
    assert verified is True
    assert "Selected option verified" in note


# ==============================================================================
# 4. Recovery & Duplicate Prevention Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_query_before_retry_prevents_duplicate_actions():
    """Verify that query-before-retry detects already satisfied fields and avoids duplicate execution."""
    controller = MagicMock()
    # The page already has the field filled (e.g. from an attempt that hit a timeout after writing)
    controller.parse_interactive_elements = AsyncMock(return_value={
        "url": "https://example.com/form",
        "form_fields": [
            {"selector": "input#email", "current_value": "user@example.com"}
        ],
        "elements": [],
    })

    action = BrowserAction(action=ActionType.FILL, selector="input#email", value="user@example.com")
    satisfied, note = await _check_action_already_satisfied(controller, action)
    assert satisfied is True
    assert "Field already contains expected value" in note

    # Test in recovery runner: primary fails on attempt 1, query-before-retry recovers on attempt 2 without retyping
    call_count = 0
    async def primary_fn():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("Temporary socket timeout after typing")
        return "Typed again", None, True

    policy = RetryPolicy(max_retries=2, base_delay=0.01)
    desc, extracted, success, recovery_log = await execute_with_retry_and_fallback(
        execute_primary_fn=primary_fn,
        action=action,
        controller=controller,
        retry_policy=policy,
    )

    assert success is True
    # Primary function must NOT have been called a second time
    assert call_count == 1
    assert any("Query-before-retry confirmed effect already occurred" in entry for entry in recovery_log)


@pytest.mark.asyncio
async def test_non_idempotent_actions_skip_blind_retry():
    """Verify that non-idempotent actions (e.g. submit clicks) are never blindly re-executed."""
    controller = MagicMock()
    controller.current_page = None
    controller.parse_interactive_elements = AsyncMock(return_value={
        "url": "https://example.com/checkout",
        "elements": [{"selector": "button#pay", "text": "Pay Now"}],
    })

    action = BrowserAction(action=ActionType.CLICK, selector="button#pay", text="Pay Now")

    call_count = 0
    async def primary_fn():
        nonlocal call_count
        call_count += 1
        return "Timeout waiting for button#pay", None, False

    policy = RetryPolicy(max_retries=3, base_delay=0.01)
    desc, extracted, success, recovery_log = await execute_with_retry_and_fallback(
        execute_primary_fn=primary_fn,
        action=action,
        controller=controller,
        retry_policy=policy,
    )

    assert success is False
    # Must only be attempted once, never blindly repeated
    assert call_count == 1
    assert any("automatic retry skipped" in entry or "Non-idempotent action" in entry for entry in recovery_log)


def test_extension_inspect_dom_waits_for_loading_tab():
    """Verify background.js waits briefly when inspect_dom is called while tab is still loading."""
    node_script = """
    const fs = require('fs');
    const bgSrc = fs.readFileSync('chrome_extension/background.js', 'utf8');
    const vm = require('vm');

    let wsMessages = [];
    class MockWebSocket {
      constructor() { this.readyState = 1; }
      send(str) { wsMessages.push(JSON.parse(str)); }
    }
    MockWebSocket.OPEN = 1;

    let tabStatus = 'loading';
    let onUpdatedListeners = [];
    let inspectCalledWhileLoading = false;

    const mockChrome = {
      tabs: {
        get: async (id) => ({ id, url: 'https://drive.google.com/drive/u/0/home', status: tabStatus }),
        sendMessage: async (tabId, msg, opts) => {
          if (msg.type === 'ping') return { pong: true, is_top_frame: true };
          if (msg.type === 'inspect_dom') {
            if (tabStatus !== 'complete') inspectCalledWhileLoading = true;
            return { success: true, data: { title: 'Home - Google Drive', url: 'https://drive.google.com/drive/u/0/home', elements: [] } };
          }
          return { success: true };
        },
        onUpdated: {
          addListener: (fn) => { onUpdatedListeners.push(fn); },
          removeListener: (fn) => { onUpdatedListeners = onUpdatedListeners.filter(x => x !== fn); },
        },
        onRemoved: { addListener: () => {}, removeListener: () => {} },
      },
      runtime: { onMessage: { addListener: () => {}, removeListener: () => {} } },
      alarms: { onAlarm: { addListener: () => {}, removeListener: () => {} }, create: () => {} },
      scripting: { executeScript: async () => {} },
      storage: {
        local: {
          get: async () => ({ taskMappings: { 'task-load': 55 }, extensionToken: 'tok' }),
          set: async () => {},
        },
      },
    };

    const ctx = vm.createContext({
      chrome: mockChrome,
      WebSocket: MockWebSocket,
      console,
      setTimeout,
      clearTimeout,
    });
    vm.runInContext(bgSrc, ctx);

    (async () => {
      await new Promise(r => setTimeout(r, 40));
      setTimeout(() => {
        tabStatus = 'complete';
        onUpdatedListeners.slice().forEach(fn => fn(55, { status: 'complete' }));
      }, 60);

      await ctx.handleBackendMessage({
        id: 'inspect-1',
        task_id: 'task-load',
        type: 'inspect_dom',
        show_badges: false,
      });

      if (inspectCalledWhileLoading) {
        console.error('inspect_dom dispatched before loading settled');
        process.exit(1);
      }
      if (wsMessages.length !== 1 || !wsMessages[0].success) {
        console.error('Expected successful inspect_dom response, got:', wsMessages);
        process.exit(2);
      }
      process.exit(0);
    })().catch((err) => {
      console.error(err);
      process.exit(3);
    });
    """
    res = subprocess.run(["node", "-e", node_script], capture_output=True, text=True)
    assert res.returncode == 0, f"Node test failed: {res.stderr}\n{res.stdout}"


def test_content_script_lock_screen_shield_and_iframe_unlock():
    """Verify content.js engages an interactive shield, bypasses it for hit-testing/CDP,

    and unlocks top + child frames cleanly.
    """
    node_script = """
    const fs = require('fs');
    const contentSrc = fs.readFileSync('chrome_extension/content.js', 'utf8');
    const vm = require('vm');

    const nodes = new Map();
    const makeStyle = () => {
      const store = {};
      return {
        getPropertyValue: (k) => store[k] || '',
        getPropertyPriority: () => '',
        setProperty: (k, v) => { store[k] = v; },
        removeProperty: (k) => { delete store[k]; },
      };
    };
    const makeNode = (tag, id = '') => ({
      nodeType: 1,
      tagName: tag.toUpperCase(),
      id,
      isConnected: true,
      style: makeStyle(),
      children: [],
      attrs: {},
      setAttribute(k, v) { this.attrs[k] = String(v); },
      getAttribute(k) { return this.attrs[k] ?? null; },
      append(...items) { items.forEach((x) => this.appendChild(x)); },
      appendChild(child) {
        child.parentNode = this;
        this.children.push(child);
        if (child.id) nodes.set(child.id, child);
        return child;
      },
      remove() {
        if (this.id) nodes.delete(this.id);
        this.parentNode = null;
        this.isConnected = false;
      },
      matches: () => false,
      querySelectorAll: () => [],
    });

    const childFrameMessages = [];
    const mockIframe = {
      matches: (s) => s.includes('iframe'),
      querySelectorAll: () => [],
      addEventListener: () => {},
      contentWindow: { postMessage: (msg) => childFrameMessages.push(msg) },
    };

    const htmlEl = makeNode('HTML');
    const bodyEl = makeNode('BODY');
    const listeners = {};
    let scrollByCount = 0;

    const doc = {
      title: 'Test Page',
      forms: [],
      documentElement: htmlEl,
      body: bodyEl,
      createElement: (tag) => makeNode(tag),
      getElementById: (id) => nodes.get(id) || null,
      querySelectorAll: (sel) => sel === 'iframe,frame' ? [mockIframe] : [],
      querySelector: () => null,
      addEventListener: (t, fn) => { listeners[t] = fn; },
      removeEventListener: (t) => { delete listeners[t]; },
      elementFromPoint: () => bodyEl,
    };

    const win = {
      location: { href: 'https://example.com/' },
      innerWidth: 1280, innerHeight: 800, scrollX: 0, scrollY: 0, devicePixelRatio: 1,
      addEventListener: (t, fn) => { listeners['win:' + t] = fn; },
      removeEventListener: (t) => { delete listeners['win:' + t]; },
      getComputedStyle: () => ({ display: 'block', visibility: 'visible', opacity: '1', position: 'static' }),
      scrollBy: () => {
        if (htmlEl.style.getPropertyValue('overflow') !== 'hidden') scrollByCount++;
      },
      performance: { getEntriesByType: () => [] },
    };
    win.top = win;
    win.self = win;

    let runtimeListener = null;
    const ctx = vm.createContext({
      window: win, document: doc, location: win.location, performance: win.performance,
      chrome: { runtime: { onMessage: { addListener: (fn) => { runtimeListener = fn; } } } },
      Node: { ELEMENT_NODE: 1 }, console, setTimeout, clearTimeout,
      CustomEvent: function(name, init) { this.type = name; this.detail = init?.detail; },
    });
    vm.runInContext(contentSrc, ctx);

    (async () => {
      // 1. Lock screen
      const lockResp = await new Promise((r) => runtimeListener({ type: 'lock_ui' }, {}, r));
      if (!lockResp.success || !lockResp.data.locked) process.exit(1);

      const border = nodes.get('ai-agent-running-border');
      if (!border || border.style.pointerEvents !== 'auto' || border.style.cursor !== 'not-allowed') {
        console.error('Lock shield style incorrect:', border?.style);
        process.exit(2);
      }
      if (htmlEl.style.getPropertyValue('overflow') !== 'hidden') process.exit(3);
      if (!childFrameMessages.some((m) => m.__aiAgentLockCommand && m.command === 'lock_ui')) process.exit(4);

      // 2. Trusted user click is blocked
      let defaultPrevented = false;
      let stopped = false;
      listeners['click']({
        type: 'click',
        isTrusted: true,
        cancelable: true,
        preventDefault: () => { defaultPrevented = true; },
        stopPropagation: () => { stopped = true; },
        stopImmediatePropagation: () => {},
      });
      if (!defaultPrevented || !stopped) process.exit(5);

      // 3. native_input_state temporarily lowers shield for CDP, then re-engages
      await new Promise((r) => runtimeListener({ type: 'native_input_state', enabled: true }, {}, r));
      if (border.style.pointerEvents !== 'none') process.exit(6);
      await new Promise((r) => runtimeListener({ type: 'native_input_state', enabled: false }, {}, r));
      if (border.style.pointerEvents !== 'auto') process.exit(7);

      // 4. Agent scroll succeeds even when locked, then restores overflow hidden
      await new Promise((r) => runtimeListener({
        type: 'execute_action',
        action: { action: 'scroll', scroll_direction: 'down', scroll_amount: 300 },
      }, {}, r));
      if (scrollByCount !== 1 || htmlEl.style.getPropertyValue('overflow') !== 'hidden') process.exit(8);

      // 5. Unlock restores page and broadcasts unlock to child frames
      await new Promise((r) => runtimeListener({ type: 'unlock_ui' }, {}, r));
      if (nodes.get('ai-agent-running-border') || nodes.get('ai-agent-ui-lock')) process.exit(9);
      if (htmlEl.style.getPropertyValue('overflow') === 'hidden') process.exit(10);
      if (!childFrameMessages.some((m) => m.__aiAgentLockCommand && m.command === 'unlock_ui')) process.exit(11);

      process.exit(0);
    })().catch((e) => {
      console.error(e);
      process.exit(12);
    });
    """
    res = subprocess.run(["node", "-e", node_script], capture_output=True, text=True)
    assert res.returncode == 0, f"Lock screen test failed: {res.stderr}\n{res.stdout}"


def test_background_relocks_after_navigation_and_unlocks_on_auth_or_disconnect():
    """Verify background.js persists lock state across navigations and unlocks on open_auth_tab and auth_failed."""
    node_script = """
    const fs = require('fs');
    const bgSrc = fs.readFileSync('chrome_extension/background.js', 'utf8');
    const vm = require('vm');

    let wsMessages = [];
    class MockWebSocket {
      constructor() { this.readyState = 1; }
      send(str) { wsMessages.push(JSON.parse(str)); }
      close() {}
    }
    MockWebSocket.OPEN = 1;

    let scriptCalls = [];
    let onUpdatedListeners = [];

    const mockChrome = {
      tabs: {
        get: async (id) => ({ id, url: 'https://example.com/next', status: 'complete' }),
        create: async ({ url }) => ({ id: 99, url, active: true, windowId: 1 }),
        sendMessage: async (tabId, msg) => msg.type === 'ping' ? { pong: true, is_top_frame: true } : { success: true },
        onCreated: { addListener: () => {} },
        onUpdated: { addListener: (fn) => onUpdatedListeners.push(fn), removeListener: () => {} },
        onRemoved: { addListener: () => {} },
      },
      windows: {
        get: async () => ({ state: 'normal' }),
        update: async () => {},
      },
      scripting: {
        executeScript: async ({ target, args }) => {
          scriptCalls.push({ tabId: target.tabId, command: args[0] });
          return [{ result: true }];
        },
      },
      action: {
        setBadgeText: async () => {},
        setBadgeBackgroundColor: async () => {},
      },
      runtime: { onMessage: { addListener: () => {} } },
      alarms: { onAlarm: { addListener: () => {} }, create: () => {} },
      storage: {
        local: {
          get: async () => ({ taskMappings: { 'task-nav': 50 }, extensionToken: 'tok' }),
          set: async () => {},
          remove: async () => {},
        },
      },
    };

    const ctx = vm.createContext({
      chrome: mockChrome,
      WebSocket: MockWebSocket,
      console,
      setTimeout,
      clearTimeout,
    });
    vm.runInContext(bgSrc, ctx);

    (async () => {
      await new Promise(r => setTimeout(r, 40));

      // 1. Engage lock_ui for task-nav
      await ctx.handleBackendMessage({ id: '1', task_id: 'task-nav', type: 'lock_ui' });
      if (!scriptCalls.some(c => c.tabId === 50 && c.command === 'lock_ui')) process.exit(1);

      // 2. Navigation completes -> onUpdated automatically re-applies lock_ui
      scriptCalls = [];
      for (const fn of onUpdatedListeners) {
        await fn(50, { status: 'complete' }, { id: 50, url: 'https://example.com/step2' });
      }
      if (!scriptCalls.some(c => c.tabId === 50 && c.command === 'lock_ui')) process.exit(2);

      // 3. auth_failed unlocks active locked tabs
      scriptCalls = [];
      await ctx.handleBackendMessage({ type: 'auth_failed', reason: 'Credential revoked.' });
      if (!scriptCalls.some(c => c.tabId === 50 && c.command === 'unlock_ui')) process.exit(3);

      // 4. Re-lock then open_auth_tab unlocks the old tab cleanly
      await ctx.handleBackendMessage({ id: '2', task_id: 'task-nav', type: 'lock_ui' });
      scriptCalls = [];
      await ctx.handleBackendMessage({ id: '3', task_id: 'task-nav', type: 'open_auth_tab', url: 'https://accounts.google.com/' });
      if (!scriptCalls.some(c => c.tabId === 50 && c.command === 'unlock_ui')) process.exit(4);

      process.exit(0);
    })().catch((e) => {
      console.error(e);
      process.exit(5);
    });
    """
    res = subprocess.run(["node", "-e", node_script], capture_output=True, text=True)
    assert res.returncode == 0, f"Background lock test failed: {res.stderr}\n{res.stdout}"


def test_background_uses_native_cdp_for_right_click_and_menuitem_clicks():
    """Verify right_click and open-menu item clicks use CDP native input (attaching debugger first), while menu openers keep DOM dispatch."""
    node_script = """
    const fs = require('fs');
    const bgSrc = fs.readFileSync('chrome_extension/background.js', 'utf8');
    const vm = require('vm');

    class MockWebSocket {
      constructor() { this.readyState = 1; }
      send() {}
      close() {}
    }
    MockWebSocket.OPEN = 1;

    const events = [];
    const mockChrome = {
      tabs: {
        get: async (id) => ({ id, url: 'https://drive.google.com/drive/u/0/home', status: 'complete' }),
        sendMessage: async (tabId, msg) => {
          events.push({ kind: 'tabMsg', type: msg.type, action: msg.action?.action });
          if (msg.type === 'ping') return { pong: true, is_top_frame: true };
          if (msg.type === 'resolve_native_click_target') {
            return {
              success: true,
              data: {
                x: 120, y: 240, target: msg.action?.text || 'target',
                button: msg.action?.action === 'right_click' ? 'right' : 'left',
              },
            };
          }
          if (msg.type === 'execute_action') return { success: true, data: { description: 'DOM click' } };
          return { success: true };
        },
        onCreated: { addListener: () => {} },
        onUpdated: { addListener: () => {}, removeListener: () => {} },
        onRemoved: { addListener: () => {} },
      },
      windows: { get: async () => ({ state: 'normal' }), update: async () => {} },
      scripting: { executeScript: async () => [{ result: true }] },
      debugger: {
        attach: async () => { events.push({ kind: 'dbgAttach' }); },
        sendCommand: async (_dbg, method, params) => {
          events.push({ kind: 'dbgCmd', method, type: params.type, button: params.button });
        },
        detach: async () => { events.push({ kind: 'dbgDetach' }); },
      },
      action: { setBadgeText: async () => {}, setBadgeBackgroundColor: async () => {} },
      runtime: { onMessage: { addListener: () => {} } },
      alarms: { onAlarm: { addListener: () => {} }, create: () => {} },
      storage: {
        local: {
          get: async () => ({ taskMappings: { 'task-drive': 77 }, extensionToken: 'tok' }),
          set: async () => {},
          remove: async () => {},
        },
      },
    };

    const ctx = vm.createContext({
      chrome: mockChrome, WebSocket: MockWebSocket, console, setTimeout, clearTimeout,
    });
    vm.runInContext(bgSrc, ctx);

    (async () => {
      await new Promise(r => setTimeout(r, 40));

      // 1. right_click mentioning "context menu" MUST use CDP (never skipped by isMenuTrigger), and attach before resolving coords.
      events.length = 0;
      await ctx.handleBackendMessage({
        id: 'r1', task_id: 'task-drive', type: 'execute_action',
        action: { action: 'right_click', element_id: 31, reasoning: 'Right-click Assist folder to open the context menu' },
      });
      const attachIdx1 = events.findIndex(e => e.kind === 'dbgAttach');
      const resolveIdx1 = events.findIndex(e => e.kind === 'tabMsg' && e.type === 'resolve_native_click_target');
      const rightPress = events.find(e => e.kind === 'dbgCmd' && e.type === 'mousePressed' && e.button === 'right');
      if (attachIdx1 < 0 || resolveIdx1 < 0 || attachIdx1 > resolveIdx1 || !rightPress) {
        console.error('right_click failed CDP routing:', events);
        process.exit(1);
      }

      // 2. click on Rename menuitem MUST use CDP despite "menu" appearing in reasoning.
      events.length = 0;
      await ctx.handleBackendMessage({
        id: 'r2', task_id: 'task-drive', type: 'execute_action',
        action: {
          action: 'click',
          selector: '[aria-label="Rename, Ctrl+Alt+E"]',
          text: 'Rename Ctrl+Alt+E',
          reasoning: 'Click the Rename menu item in the context menu',
        },
      });
      const leftPress = events.find(e => e.kind === 'dbgCmd' && e.type === 'mousePressed' && e.button === 'left');
      if (!leftPress || events.some(e => e.kind === 'tabMsg' && e.type === 'execute_action')) {
        console.error('Rename menuitem failed CDP routing:', events);
        process.exit(2);
      }

      // 3. click on "+ New" menu opener MUST still bypass CDP and use execute_action DOM dispatch.
      events.length = 0;
      await ctx.handleBackendMessage({
        id: 'r3', task_id: 'task-drive', type: 'execute_action',
        action: {
          action: 'click',
          text: '+ New',
          reasoning: 'Click + New button to open the menu',
        },
      });
      if (events.some(e => e.kind === 'dbgAttach') || !events.some(e => e.kind === 'tabMsg' && e.type === 'execute_action')) {
        console.error('+ New menu opener did not stay on DOM dispatch:', events);
        process.exit(3);
      }

      process.exit(0);
    })().catch((e) => {
      console.error(e);
      process.exit(4);
    });
    """
    res = subprocess.run(["node", "-e", node_script], capture_output=True, text=True)
    assert res.returncode == 0, f"Background CDP routing test failed: {res.stderr}\n{res.stdout}"


def test_content_filters_hidden_form_fields_and_dispatches_single_menuitem_click():
    """Verify inspectPageDOM marks hidden/aria-hidden/disabled inputs invisible in compact_forms, and DOM menuitem clicks dispatch single clean activation."""
    node_script = """
    const fs = require('fs');
    const contentSrc = fs.readFileSync('chrome_extension/content.js', 'utf8');
    const vm = require('vm');

    const makeStyle = () => {
      const store = {};
      return {
        display: 'block', visibility: 'visible', opacity: '1', pointerEvents: 'auto',
        setProperty: (k, v) => { store[k] = v; },
        getPropertyValue: (k) => store[k] || '',
        removeProperty: (k) => { delete store[k]; },
      };
    };

    const makeEl = (tag, { id = '', attrs = {}, rect = { left: 100, top: 100, width: 200, height: 32, right: 300, bottom: 132 }, disabled = false, hidden = false } = {}) => {
      const dispatched = [];
      let nativeClicks = 0;
      const el = {
        nodeType: 1, tagName: tag.toUpperCase(), id, attrs: { ...attrs }, isConnected: true,
        style: makeStyle(), children: [], disabled, hidden, readOnly: false, value: attrs.value || '',
        innerText: attrs.innerText || '', textContent: attrs.innerText || '',
        labels: [], parentElement: null,
        dispatched,
        getNativeClicks: () => nativeClicks,
        setAttribute(k, v) { this.attrs[k] = String(v); },
        getAttribute(k) { return this.attrs[k] ?? (k === 'id' ? this.id : null); },
        hasAttribute(k) { return k in this.attrs; },
        getBoundingClientRect: () => rect,
        closest: (sel) => (sel.includes('aria-hidden') && el.attrs['aria-hidden'] === 'true') ? el : null,
        matches: (sel) => sel.includes('li') || sel.includes('menuitem') || sel.includes('input'),
        querySelector: () => null,
        querySelectorAll: () => [],
        appendChild(c) { c.parentElement = this; this.children.push(c); return c; },
        append(...items) { items.forEach((c) => this.appendChild(c)); },
        focus: () => {},
        scrollIntoView: () => {},
        dispatchEvent: (ev) => { dispatched.push(ev.type); return true; },
        click: () => { nativeClicks++; },
      };
      return el;
    };

    const hiddenSearch = makeEl('input', {
      id: 'gs_taif50',
      attrs: { type: 'text', 'aria-hidden': 'true' },
      disabled: true,
      rect: { left: 100, top: 20, width: 400, height: 36, right: 500, bottom: 56 },
    });
    const renameInput = makeEl('input', {
      id: 'rename-box',
      attrs: { type: 'text', 'aria-label': 'Rename', value: 'Assist' },
      disabled: false,
      rect: { left: 400, top: 300, width: 320, height: 36, right: 720, bottom: 336 },
    });
    const menuLabelSpan = makeEl('span', { attrs: { innerText: 'Rename' } });
    const renameMenuItem = makeEl('li', {
      id: 'rename-menuitem',
      attrs: { role: 'menuitem', 'aria-label': 'Rename, Ctrl+Alt+E', innerText: 'Rename Ctrl+Alt+E' },
    });
    renameMenuItem.querySelector = () => menuLabelSpan;
    renameMenuItem.closest = (sel) => sel.includes('menu') ? renameMenuItem : null;

    const bodyEl = makeEl('body');
    const htmlEl = makeEl('html');
    const doc = {
      title: 'My Drive - Google Drive',
      forms: [],
      documentElement: htmlEl,
      body: bodyEl,
      createElement: (tag) => makeEl(tag),
      createDocumentFragment: () => ({ appendChild: () => {}, children: [] }),
      getElementById: (id) => ({ gs_taif50: hiddenSearch, 'rename-box': renameInput, 'rename-menuitem': renameMenuItem }[id] || null),
      querySelector: (sel) => sel.includes('Rename') ? renameMenuItem : null,
      querySelectorAll: (sel) => {
        if (sel === 'input, select, textarea') return [hiddenSearch, renameInput];
        if (sel.includes('Rename')) return [renameMenuItem];
        return [];
      },
      addEventListener: () => {},
      removeEventListener: () => {},
      elementFromPoint: () => renameMenuItem,
    };

    const win = {
      location: { href: 'https://drive.google.com/drive/u/0/home', hostname: 'drive.google.com' },
      innerWidth: 1280, innerHeight: 800, scrollX: 0, scrollY: 0, devicePixelRatio: 1,
      addEventListener: () => {}, removeEventListener: () => {},
      getComputedStyle: (el) => el.style,
      scrollBy: () => {},
      performance: { getEntriesByType: () => [] },
    };
    win.top = win; win.self = win;

    let runtimeListener = null;
    const Ev = function(type, init) { this.type = type; Object.assign(this, init || {}); };
    const ctx = vm.createContext({
      window: win, document: doc, location: win.location, performance: win.performance,
      chrome: { runtime: { onMessage: { addListener: (fn) => { runtimeListener = fn; } } } },
      Node: { ELEMENT_NODE: 1 },
      Element: function() {},
      HTMLElement: function() {},
      SVGElement: function() {},
      MouseEvent: Ev,
      PointerEvent: Ev,
      KeyboardEvent: Ev,
      CustomEvent: Ev,
      CSS: { escape: (s) => s },
      console, setTimeout, clearTimeout,
    });
    vm.runInContext(contentSrc, ctx);

    (async () => {
      // 1. inspect_dom excludes hidden/disabled #gs_taif50 from compact_forms and marks it visible:false
      const domResp = await new Promise((r) => runtimeListener({ type: 'inspect_dom', show_badges: false }, {}, r));
      if (!domResp?.success) { console.error('inspect_dom failed', domResp); process.exit(1); }
      const hiddenField = domResp.data.form_fields.find(f => f.field_id === 'gs_taif50');
      const dialogField = domResp.data.form_fields.find(f => f.field_id === 'rename-box');
      if (!hiddenField || hiddenField.visible !== false) { console.error('gs_taif50 still visible:', hiddenField); process.exit(2); }
      if (!dialogField || dialogField.visible !== true) { console.error('rename-box not visible:', dialogField); process.exit(3); }
      if (domResp.data.compact_forms.fields.some(f => f.id === 'gs_taif50')) {
        console.error('gs_taif50 leaked into compact_forms:', domResp.data.compact_forms.fields);
        process.exit(4);
      }

      // 2. click on role=menuitem fires pointerdown + click cleanly once on menuitem (not 4 clicks)
      await new Promise((r) => runtimeListener({
        type: 'execute_action',
        action: { action: 'click', selector: '[aria-label="Rename, Ctrl+Alt+E"]', text: 'Rename Ctrl+Alt+E' },
      }, {}, r));
      if (renameMenuItem.getNativeClicks() !== 1 || menuLabelSpan.getNativeClicks() !== 0 || !renameMenuItem.dispatched.includes('pointerdown')) {
        console.error('Menuitem click count or pointerdown incorrect:', {
          liClicks: renameMenuItem.getNativeClicks(),
          spanClicks: menuLabelSpan.getNativeClicks(),
          dispatched: renameMenuItem.dispatched,
        });
        process.exit(5);
      }

      // 3. right_click DOM fallback dispatches pointerdown, mousedown, pointerup, mouseup, contextmenu
      renameMenuItem.dispatched.length = 0;
      await new Promise((r) => runtimeListener({
        type: 'execute_action',
        action: { action: 'right_click', selector: '[aria-label="Rename, Ctrl+Alt+E"]' },
      }, {}, r));
      if (!renameMenuItem.dispatched.includes('contextmenu') || !renameMenuItem.dispatched.includes('pointerdown')) {
        console.error('right_click events missing:', renameMenuItem.dispatched);
        process.exit(6);
      }

      process.exit(0);
    })().catch((e) => {
      console.error(e);
      process.exit(7);
    });
    """
    res = subprocess.run(["node", "-e", node_script], capture_output=True, text=True)
    assert res.returncode == 0, f"Content DOM filtering & click test failed: {res.stderr}\n{res.stdout}"

