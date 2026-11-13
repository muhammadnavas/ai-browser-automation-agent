"""
tests/test_generic_dom_automation.py
------------------------------------
Generic regression tests for the improved DOM automation engine:
1. Universal control type detection and is_editable identification.
2. Label, nested container, and question wrapper association.
3. Unambiguous FILL redirection away from non-editable targets (labels, containers).
4. Ambiguous container rejection (no arbitrary redirection).
5. Native and custom dropdowns, checkboxes, and radio buttons.
6. Planner action selection guardrails (no FILL on non-editable elements).
7. Context independence: explicit task data, populated My Context, and empty My Context (never invent personal info).
8. Adaptive re-inspection and recovery on stale or invalid element errors.
"""

import asyncio
import json
import subprocess
import pytest
from unittest.mock import AsyncMock, MagicMock

from models.schemas import BrowserAction, ActionType
from agents.planner import AIAgentPlanner, synthesize_generalized_field_value
from browser.recovery import classify_browser_error, execute_with_retry_and_fallback, RetryPolicy


# ==============================================================================
# PART A: Extension Content Script Tests (DOM Identification & Action Handlers)
# ==============================================================================

def test_extension_is_element_editable_classification():
    """Verify isElementEditable correctly classifies text/editable controls vs non-editable controls."""
    node_script = """
    const fs = require('fs');
    const contentSrc = fs.readFileSync('chrome_extension/content.js', 'utf8');
    const vm = require('vm');

    let mockWindow = {
      location: { href: 'https://example.com/form' },
      addEventListener: () => {},
    };
    mockWindow.top = mockWindow;
    mockWindow.self = mockWindow;

    const ctx = vm.createContext({
      window: mockWindow,
      location: mockWindow.location,
      document: { querySelectorAll: () => [], getElementById: () => null },
      chrome: { runtime: { onMessage: { addListener: () => {} } } },
      console,
      Node: { ELEMENT_NODE: 1 },
    });
    vm.runInContext(contentSrc, ctx);

    const isElementEditable = ctx.window.__AI_TEST__.isElementEditable;

    const checkEditable = (tag, type, attrs = {}) => {
      const mockNode = {
        isConnected: true,
        tagName: tag.toUpperCase(),
        getAttribute: (k) => attrs[k] !== undefined ? attrs[k] : (k === 'type' ? type : null),
        disabled: Boolean(attrs.disabled),
        readOnly: Boolean(attrs.readOnly),
        isContentEditable: Boolean(attrs.contentEditable),
      };
      return isElementEditable(mockNode);
    };

    // Text inputs must be editable
    if (!checkEditable('input', 'text')) process.exit(10);
    if (!checkEditable('input', 'email')) process.exit(11);
    if (!checkEditable('input', 'tel')) process.exit(12);
    if (!checkEditable('input', 'number')) process.exit(13);
    if (!checkEditable('input', 'date')) process.exit(14);
    if (!checkEditable('input', 'search')) process.exit(15);
    if (!checkEditable('textarea', null)) process.exit(16);
    if (!checkEditable('div', null, { contentEditable: true })) process.exit(17);
    if (!checkEditable('div', null, { role: 'textbox' })) process.exit(18);

    // Non-editable elements must be false
    if (checkEditable('input', 'checkbox')) process.exit(20);
    if (checkEditable('input', 'radio')) process.exit(21);
    if (checkEditable('input', 'button')) process.exit(22);
    if (checkEditable('input', 'submit')) process.exit(23);
    if (checkEditable('input', 'hidden')) process.exit(24);
    if (checkEditable('button', null)) process.exit(25);
    if (checkEditable('label', null)) process.exit(26);
    if (checkEditable('div', null)) process.exit(27);
    if (checkEditable('span', null)) process.exit(28);

    // Readonly and disabled must be false
    if (checkEditable('input', 'text', { readOnly: true })) process.exit(30);
    if (checkEditable('input', 'text', { disabled: true })) process.exit(31);
    if (checkEditable('input', 'text', { 'aria-disabled': 'true' })) process.exit(32);
    if (checkEditable('input', 'text', { 'aria-readonly': 'true' })) process.exit(33);

    process.exit(0);
    """
    res = subprocess.run(["node", "-e", node_script], capture_output=True, text=True)
    assert res.returncode == 0, f"Editable classification failed: code={res.returncode}, stderr={res.stderr}"


def test_extension_fill_redirection_from_label_to_input():
    """Verify that targeting FILL at a <label for='email'> seamlessly and unambiguously redirects to input#email."""
    node_script = """
    const fs = require('fs');
    const contentSrc = fs.readFileSync('chrome_extension/content.js', 'utf8');
    const vm = require('vm');

    let targetInput = {
      isConnected: true,
      tagName: 'INPUT',
      id: 'email',
      value: '',
      getAttribute: (k) => k === 'type' ? 'email' : (k === 'id' ? 'email' : null),
      disabled: false,
      readOnly: false,
      scrollIntoView: () => {},
      focus: () => {},
      dispatchEvent: () => true,
      parentElement: null,
    };

    let labelElement = {
      isConnected: true,
      tagName: 'LABEL',
      htmlFor: 'email',
      innerText: 'Email Address',
      getAttribute: (k) => k === 'for' ? 'email' : null,
      scrollIntoView: () => {},
      focus: () => {},
      closest: () => null,
      querySelectorAll: () => [],
      parentElement: null,
    };

    let mockDoc = {
      querySelectorAll: () => [],
      getElementById: (id) => id === 'email' ? targetInput : null,
      querySelector: (sel) => sel === '#email-label' ? labelElement : null,
      body: {},
      activeElement: null,
    };

    let mockWindow = {
      location: { href: 'https://example.com/register' },
      addEventListener: () => {},
      getComputedStyle: () => ({ pointerEvents: 'auto', display: 'block', visibility: 'visible' }),
    };
    mockWindow.top = mockWindow;
    mockWindow.self = mockWindow;

    const ctx = vm.createContext({
      window: mockWindow,
      location: mockWindow.location,
      document: mockDoc,
      chrome: { runtime: { onMessage: { addListener: () => {} } } },
      console,
      Node: { ELEMENT_NODE: 1 },
      Event: function(type) { this.type = type; },
      InputEvent: function(type, opts) { this.type = type; this.data = opts?.data; },
      KeyboardEvent: function(type) { this.type = type; },
      setTimeout: (cb) => { cb(); },
      requestAnimationFrame: (cb) => { cb(); },
      CSS: { escape: (s) => s },
      HTMLInputElement: function() {},
      HTMLTextAreaElement: function() {},
    });
    vm.runInContext(contentSrc, ctx);

    (async () => {
      const exports = ctx.window.__AI_TEST__;
      const res = await exports._executeActionInternal({
        action: 'fill',
        selector: '#email-label',
        text: 'jane@example.com'
      });

      if (!res.success) {
        console.error('Fill on label failed:', res.error);
        process.exit(1);
      }
      if (targetInput.value !== 'jane@example.com') {
        console.error('Value was not set on targetInput:', targetInput.value);
        process.exit(2);
      }
      process.exit(0);
    })();
    """
    res = subprocess.run(["node", "-e", node_script], capture_output=True, text=True)
    assert res.returncode == 0, f"Fill redirection on label failed: {res.stderr}"


def test_extension_fill_refuses_ambiguous_container_redirection():
    """Verify that a container with multiple inputs is NOT arbitrarily filled."""
    node_script = """
    const fs = require('fs');
    const contentSrc = fs.readFileSync('chrome_extension/content.js', 'utf8');
    const vm = require('vm');

    const input1 = { isConnected: true, tagName: 'INPUT', getAttribute: (k) => k==='type'?'text':null, disabled: false, readOnly: false };
    const input2 = { isConnected: true, tagName: 'INPUT', getAttribute: (k) => k==='type'?'text':null, disabled: false, readOnly: false };

    let containerDiv = {
      isConnected: true,
      tagName: 'DIV',
      className: 'form-group',
      getAttribute: (k) => k==='class'?'form-group':null,
      closest: (sel) => containerDiv,
      querySelectorAll: (sel) => [input1, input2],
      scrollIntoView: () => {},
      focus: () => {},
      parentElement: null,
    };

    let mockDoc = {
      querySelectorAll: () => [],
      getElementById: () => null,
      querySelector: (sel) => sel === '.form-group' ? containerDiv : null,
      body: {},
      activeElement: null,
    };

    let mockWindow = {
      location: { href: 'https://example.com' },
      addEventListener: () => {},
      getComputedStyle: () => ({ pointerEvents: 'auto', display: 'block', visibility: 'visible' }),
    };
    mockWindow.top = mockWindow;
    mockWindow.self = mockWindow;

    const ctx = vm.createContext({
      window: mockWindow,
      location: mockWindow.location,
      document: mockDoc,
      chrome: { runtime: { onMessage: { addListener: () => {} } } },
      console,
      Node: { ELEMENT_NODE: 1 },
      Event: function(t) {},
      setTimeout: (cb) => { cb(); },
      requestAnimationFrame: (cb) => { cb(); },
      CSS: { escape: (s) => s },
      HTMLInputElement: function() {},
      HTMLTextAreaElement: function() {},
    });
    vm.runInContext(contentSrc, ctx);

    (async () => {
      const exports = ctx.window.__AI_TEST__;
      const res = await exports._executeActionInternal({
        action: 'fill',
        selector: '.form-group',
        text: 'test'
      });

      // Must fail with ambiguous error, not fill input1 or input2 arbitrarily
      if (res.success) {
        process.exit(1);
      }
      if (!res.error || !res.error.includes('ambiguous')) {
        console.error('Expected ambiguous error but got:', res.error);
        process.exit(2);
      }
      process.exit(0);
    })();
    """
    res = subprocess.run(["node", "-e", node_script], capture_output=True, text=True)
    assert res.returncode == 0, f"Ambiguous container test failed: {res.stderr}"


def test_extension_check_and_radio_redirection():
    """Verify check/uncheck and radio_button resolve through label and custom roles."""
    node_script = """
    const fs = require('fs');
    const contentSrc = fs.readFileSync('chrome_extension/content.js', 'utf8');
    const vm = require('vm');

    let checkboxInput = {
      isConnected: true,
      tagName: 'INPUT',
      type: 'checkbox',
      checked: false,
      getAttribute: (k) => k==='type'?'checkbox':null,
      dispatchEvent: () => true,
      click: function() { this.checked = !this.checked; },
      scrollIntoView: () => {},
      focus: () => {},
    };

    let checkLabel = {
      isConnected: true,
      tagName: 'LABEL',
      htmlFor: 'agree',
      getAttribute: () => 'agree',
      scrollIntoView: () => {},
      focus: () => {},
      querySelector: () => null,
    };

    let radioInput = {
      isConnected: true,
      tagName: 'INPUT',
      type: 'radio',
      name: 'gender',
      checked: false,
      getAttribute: (k) => k==='type'?'radio':(k==='name'?'gender':null),
      dispatchEvent: () => true,
      click: function() { this.checked = true; },
      scrollIntoView: () => {},
      focus: () => {},
    };

    let mockDoc = {
      querySelectorAll: () => [],
      getElementById: (id) => id === 'agree' ? checkboxInput : null,
      querySelector: (sel) => {
        if (sel === '#agree-label') return checkLabel;
        if (sel === '#radio-female') return radioInput;
        return null;
      },
      body: {},
      activeElement: null,
    };

    let mockWindow = {
      location: { href: 'https://example.com' },
      addEventListener: () => {},
      getComputedStyle: () => ({ pointerEvents: 'auto', display: 'block', visibility: 'visible' }),
    };
    mockWindow.top = mockWindow;
    mockWindow.self = mockWindow;

    const ctx = vm.createContext({
      window: mockWindow,
      location: mockWindow.location,
      document: mockDoc,
      chrome: { runtime: { onMessage: { addListener: () => {} } } },
      console,
      Node: { ELEMENT_NODE: 1 },
      Event: function(t) { this.type = t; },
      MouseEvent: function(t) { this.type = t; },
      setTimeout: (cb) => { cb(); },
      requestAnimationFrame: (cb) => { cb(); },
      CSS: { escape: (s) => s },
    });
    vm.runInContext(contentSrc, ctx);

    (async () => {
      const exports = ctx.window.__AI_TEST__;

      // 1. Check action on label pointing to checkboxInput
      const resCheck = await exports._executeActionInternal({ action: 'check', selector: '#agree-label' });
      if (!resCheck.success || checkboxInput.checked !== true) process.exit(1);

      // 2. Uncheck action on label
      const resUncheck = await exports._executeActionInternal({ action: 'uncheck', selector: '#agree-label' });
      if (!resUncheck.success || checkboxInput.checked !== false) process.exit(2);

      // 3. Radio button action on radioInput
      const resRadio = await exports._executeActionInternal({ action: 'radio_button', selector: '#radio-female' });
      if (!resRadio.success || radioInput.checked !== true) process.exit(3);

      process.exit(0);
    })();
    """
    res = subprocess.run(["node", "-e", node_script], capture_output=True, text=True)
    assert res.returncode == 0, f"Check/radio redirection failed: {res.stderr}"


# ==============================================================================
# PART B: Planner Generic Action Selection & Guardrails
# ==============================================================================

def test_planner_compact_interactive_elements_metadata():
    """Verify compact_interactive_elements includes is_editable, control_type, and required."""
    elements = [
        {"index": 1, "tag": "input", "type": "email", "is_editable": True, "control_category": "editable_text", "name": "email", "required": True},
        {"index": 2, "tag": "div", "role": "button", "text": "Submit", "is_editable": False, "control_category": "button"},
        {"index": 3, "tag": "input", "type": "checkbox", "is_editable": False, "control_category": "checkbox", "checked": True},
        {"index": 4, "tag": "textarea", "is_editable": True, "control_category": "textarea", "id": "notes"},
    ]
    compact = AIAgentPlanner.compact_interactive_elements(elements)
    assert len(compact) == 4
    assert compact[0]["is_editable"] is True
    assert compact[0]["control_type"] == "editable_text"
    assert compact[0]["required"] is True

    assert "is_editable" not in compact[1]
    assert compact[1]["control_type"] == "button"

    assert "is_editable" not in compact[2]
    assert compact[2]["control_type"] == "checkbox"
    assert compact[2]["checked"] is True

    assert compact[3]["is_editable"] is True
    assert compact[3]["control_type"] == "textarea"


def test_heuristic_planner_never_fills_non_editable_elements():
    """Verify heuristic planner never emits FILL on labels, divs, radios, or checkboxes."""
    planner = AIAgentPlanner()
    page_info = {
        "url": "https://example.com/register",
        "title": "Registration",
        "elements": [
            {"index": 1, "tag": "label", "text": "Full Name", "is_editable": False, "control_category": "container"},
            {"index": 2, "tag": "div", "id": "name-group", "is_editable": False, "control_category": "container"},
            {"index": 3, "tag": "input", "type": "radio", "name": "gender", "value": "female", "text": "Female", "is_editable": False, "control_category": "radio"},
            {"index": 4, "tag": "input", "type": "checkbox", "name": "terms", "text": "Agree to Terms", "is_editable": False, "control_category": "checkbox"},
            {"index": 5, "tag": "input", "type": "text", "name": "fullName", "placeholder": "Enter name", "is_editable": True, "control_category": "editable_text"},
        ],
    }

    # With explicit task data: Name: Alex Morgan
    action = planner._heuristic_decide_action(
        goal="Fill registration form with Name: Alex Morgan",
        step_number=1,
        page_info=page_info,
        extracted_data=[],
        trajectory_history=[{"action": "navigate", "url": "https://example.com/register"}],
    )

    # Must select index 5 (the actual editable input), NOT index 1 (label) or index 2 (div)
    assert action.action == ActionType.FILL
    assert action.element_id == 5
    assert action.text == "Alex Morgan"


def test_heuristic_planner_emits_click_for_radio_and_check_for_checkbox():
    """Verify radio buttons receive CLICK/RADIO_BUTTON and checkboxes receive CHECK_BOX, not FILL."""
    planner = AIAgentPlanner()
    page_info = {
        "url": "https://example.com/survey",
        "title": "Survey",
        "elements": [
            {"index": 10, "tag": "input", "type": "radio", "name": "preference", "value": "option_a", "label": "Option A", "is_editable": False, "control_category": "radio"},
            {"index": 11, "tag": "input", "type": "checkbox", "name": "subscribe", "label": "Subscribe to newsletter", "is_editable": False, "control_category": "checkbox"},
        ],
    }

    # 1. Radio interaction
    action_radio = planner._heuristic_decide_action(
        goal="Fill survey and select preference Option A",
        step_number=1,
        page_info=page_info,
        extracted_data=[],
        trajectory_history=[{"action": "navigate", "url": "https://example.com/survey"}],
    )
    assert action_radio.action == ActionType.CLICK
    assert action_radio.element_id == 10

    # 2. Checkbox interaction
    action_check = planner._heuristic_decide_action(
        goal="Fill survey and set subscribe to newsletter true",
        step_number=2,
        page_info=page_info,
        extracted_data=[],
        trajectory_history=[
            {"action": "navigate", "url": "https://example.com/survey"},
            {"action": "click", "element_id": 10},
        ],
    )
    assert action_check.action == ActionType.CHECK_BOX
    assert action_check.element_id == 11
    assert action_check.checked is True


def test_heuristic_planner_skips_readonly_and_disabled_inputs():
    """Verify heuristic planner ignores readonly and disabled inputs."""
    planner = AIAgentPlanner()
    page_info = {
        "url": "https://example.com/profile",
        "title": "User Profile",
        "elements": [
            {"index": 1, "tag": "input", "type": "text", "name": "accountId", "label": "Account ID", "readonly": True, "is_editable": False},
            {"index": 2, "tag": "input", "type": "text", "name": "email", "label": "Email", "disabled": True, "is_editable": False},
            {"index": 3, "tag": "input", "type": "text", "name": "nickname", "label": "Nickname", "is_editable": True},
        ],
    }

    action = planner._heuristic_decide_action(
        goal="Fill profile form with Nickname: SuperCoder",
        step_number=1,
        page_info=page_info,
        extracted_data=[],
        trajectory_history=[{"action": "navigate", "url": "https://example.com/profile"}],
    )

    # Must skip 1 (readonly) and 2 (disabled), and fill 3 (nickname)
    assert action.action == ActionType.FILL
    assert action.element_id == 3
    assert action.text == "SuperCoder"


# ==============================================================================
# PART C: Context Independence & Personal Information Protection
# ==============================================================================

def test_empty_my_context_never_invents_personal_information():
    """Verify that when My Context is empty and no explicit task data is provided, personal information is NOT synthesized."""
    name_el = {"id": "fullname", "label": "Full Name", "type": "text"}
    email_el = {"id": "email", "label": "Email Address", "type": "email"}
    phone_el = {"id": "phone", "label": "Phone Number", "type": "tel"}

    # With empty retrieved_context or exclusive My Context instructed
    res_name = synthesize_generalized_field_value(name_el, goal="Fill registration form exclusively from My Context", retrieved_context=None)
    assert res_name.value == ""

    res_email = synthesize_generalized_field_value(email_el, goal="Fill registration form", retrieved_context={})
    assert res_email.value == ""

    res_phone = synthesize_generalized_field_value(phone_el, goal="Fill registration form", retrieved_context={"profile": {}})
    assert res_phone.value == ""


def test_explicit_task_data_works_without_my_context():
    """Verify that explicit task prompt data fills fields even when My Context is completely empty."""
    name_el = {"id": "fullname", "label": "Full Name", "type": "text"}
    email_el = {"id": "email", "label": "Email Address", "type": "email"}

    goal = "Register user with Full Name: Sarah Connor, Email: sarah@resistance.org"
    res_name = synthesize_generalized_field_value(name_el, goal=goal, retrieved_context=None)
    assert res_name.value == "Sarah Connor"

    res_email = synthesize_generalized_field_value(email_el, goal=goal, retrieved_context={})
    assert res_email.value == "sarah@resistance.org"


def test_populated_my_context_used_when_available():
    """Verify that when My Context has data, it is used accurately."""
    ctx = {
        "profile": {
            "personal_details": {
                "full_name": "Dev User",
                "email": "dev@example.com",
                "phone": "+1-800-555-0100",
            }
        }
    }
    name_el = {"id": "name", "label": "Full Name", "type": "text"}
    res = synthesize_generalized_field_value(name_el, goal="Fill the form", retrieved_context=ctx)
    assert res.value == "Dev User"
    assert "My Context" in res.source


# ==============================================================================
# PART D: Recovery Layer & Adaptive Re-targeting
# ==============================================================================

def test_recovery_error_classification_for_non_editable_target():
    """Verify that non-editable element errors are classified as locator_or_stale_state for retry eligibility."""
    err1 = "Action execution error: Selected element is not editable and its question container has no editable text field."
    assert classify_browser_error(err1) == "locator_or_stale_state"

    err2 = "Selected element is not editable and no unambiguous associated editable control was found."
    assert classify_browser_error(err2) == "locator_or_stale_state"

    err3 = "Target field is disabled, read-only, or not an editable text control."
    assert classify_browser_error(err3) == "locator_or_stale_state"


@pytest.mark.asyncio
async def test_recovery_adaptive_retargeting_on_stale_element():
    """Verify execute_with_retry_and_fallback adaptively discovers and updates the action target when initial execution fails."""
    # Controller that initially fails with non-editable error, but parse_interactive_elements reveals live input
    class MockController:
        def __init__(self):
            self.call_count = 0
            self.is_running = True

        async def parse_interactive_elements(self):
            return {
                "elements": [
                    {"index": 99, "tag": "div", "text": "Email", "is_editable": False},
                    {"index": 105, "tag": "input", "type": "email", "name": "userEmail", "label": "Email Address", "is_editable": True, "selector": "#userEmail"},
                ]
            }

    controller = MockController()
    action = BrowserAction(
        action=ActionType.FILL,
        element_id=99,  # Stale div target initially
        field_label="Email Address",
        selector="#email-wrapper",
        text="test@example.com",
    )

    attempts = []
    async def primary_fn():
        attempts.append(action.element_id)
        if len(attempts) == 1:
            return "Selected element is not editable and its question container has no editable text field.", None, False
        return "Filled #userEmail successfully", None, True

    policy = RetryPolicy(max_retries=2, base_delay=0.01, jitter=False)
    from unittest.mock import patch
    with patch("browser.recovery.asyncio.sleep", new=AsyncMock()):
        desc, _, success, recovery_log = await execute_with_retry_and_fallback(
            execute_primary_fn=primary_fn,
            action=action,
            controller=controller,
            retry_policy=policy,
            task_id="adaptive-test-task",
        )

    assert success is True
    # Attempt 1 used element_id=99, Attempt 2 adaptively discovered element_id=105
    assert attempts == [99, 105]
    assert action.element_id == 105
    assert any("Adaptively re-targeted" in line for line in recovery_log)
