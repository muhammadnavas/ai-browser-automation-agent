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
