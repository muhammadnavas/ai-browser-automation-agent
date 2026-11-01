# Browser Automation Core

This document covers the modular browser automation system implemented in the `browser/` package, detailing multi-engine support, low-level interaction primitives, tab management, dialog handling, session state, and file downloads.

---

## 1. Overview & Purpose

The **Browser Automation Core** provides a robust, asynchronous Playwright abstraction for controlling real web browsers. It exposes both high-level lifecycle control (`BrowserController`) and direct functional primitives (`browser.actions`, `browser.tabs`, `browser.dialogs`, `browser.downloader`, `browser.session`, `browser.waiter`, and `browser.stealth`).

---

## 2. Main Components & Files

| Component / Submodule | File Location | Responsibility |
|---|---|---|
| **`BrowserController`** | `browser/controller.py` | Lifecycle management, browser startup/shutdown, navigation, screenshot capture, DOM element indexing, and visual badge overlays. |
| **`browser.actions`** | `browser/actions.py` | Reusable interaction primitives: clicks, typing, dropdown selection, checkboxes, radio buttons, drag-and-drop, scrolling, and DOM queries. |
| **`BrowserPool`** | `browser/pool.py` | Singleton warm Playwright browser pool provisioning isolated contexts and pages with zero process spawn overhead. |
| **`TabManager`** | `browser/tabs.py` | Opening, listing, focusing, closing tabs, and intercepting popup windows (`wait_for_popup`). |
| **`browser.dialogs`** | `browser/dialogs.py` | Native JavaScript dialog handling (`alert`, `confirm`, `prompt`) and file upload chooser overrides. |
| **`DownloadManager`** | `browser/downloader.py` | File download event handling, saving files with unique-name collision control. |
| **`SessionManager`** | `browser/session.py` | Cookie CRUD, localStorage management, storage state JSON export/import, geolocation overriding, and custom HTTP headers. |
| **`browser.waiter`** | `browser/waiter.py` | Explicit wait utilities (selectors, text, load state, network idle, URL changes, and conditional polling). |
| **`browser.stealth`** | `browser/stealth.py` | Anti-bot evasion script injection and proxy configuration builders. |

---

## 3. Submodule Capabilities & Usage

### 3.1. Browser Lifecycle & Navigation (`BrowserController`)
`BrowserController` manages launching the browser engine (Chromium, Firefox, or WebKit), setting realistic user-agents, auto-prefixing URLs with `https://`, and taking screenshots.

```python
from browser.controller import BrowserController

# Launch a visible Chromium window maximized
async with BrowserController(headless=False, maximized=True) as ctrl:
    await ctrl.navigate("https://www.example.com")
    
    # Capture step screenshot
    screenshot_path, screenshot_url = await ctrl.capture_screenshot("task-123", step_num=1)
    print(f"Screenshot saved: {screenshot_path}")
```

### 3.2. Page Interactions (`browser.actions`)
All action functions operate directly on standard Playwright `Page` objects:

```python
from browser import actions as ba

# Fill inputs and select controls
await ba.fill_field(page, "#username", "alice@example.com")
await ba.select_option(page, "#country-dropdown", label="United States")
await ba.check_checkbox(page, "#terms-checkbox", checked=True)
await ba.select_radio(page, name="gender", value="female")

# Mouse gestures
await ba.click(page, "#submit-btn")
await ba.double_click(page, ".item-card")
await ba.right_click(page, ".context-target")
await ba.hover(page, ".nav-dropdown")
await ba.drag_and_drop(page, "#item-1", "#cart-target")

# Scrolling
await ba.scroll_page(page, direction="down", amount=500)
await ba.scroll_to_element(page, "#footer")
```

### 3.3. Multi-Tab and Popup Management (`browser.tabs`)
`TabManager` enables multi-tab workflows and handles popups that open via `target="_blank"` or JavaScript `window.open()`:

```python
# Open and switch tabs
new_tab = await ctrl.tabs.open_new_tab("https://www.example.com/docs")
await ctrl.tabs.switch_to_index(0)

# Catch popups triggered by clicks
popup = await ctrl.tabs.wait_for_popup(
    lambda: ctrl.current_page.click("a[target='_blank']")
)
print(f"Popup URL: {popup.url}")
await ctrl.tabs.close_tab(popup)
```

### 3.4. Dialog Handling & File Uploads (`browser.dialogs`)
Dialog listeners must be registered **before** the action that triggers the dialog occurs:

```python
from browser import dialogs as bd

# Auto-accept alerts and confirms
bd.auto_accept(page)
await page.click("#trigger-alert-btn")

# Accept prompt with specific text
bd.accept_with_text(page, "my-input-value")
await page.click("#trigger-prompt-btn")

# Handle file upload chooser dialogs
await bd.handle_file_upload(
    page,
    trigger=lambda: page.click("#upload-file-button"),
    file_paths=["/path/to/document.pdf"]
)
```

### 3.5. File Downloads (`browser.downloader`)
`DownloadManager` coordinates with Playwright download events to safely save files to disk without name collisions:

```python
# Trigger and await download
save_path = await ctrl.downloads.start_download(
    page,
    trigger=lambda: page.click("#download-csv-btn"),
    filename="export.csv"
)
print(f"File downloaded to: {save_path}")
```

### 3.6. Session & Storage Management (`browser.session`)
`SessionManager` allows dumping and restoring cookies and localStorage across browser sessions:

```python
# Export session to JSON
await ctrl.session.save_storage(ctrl.context, "session_state.json")

# Restore cookies in a running context
await ctrl.session.load_storage(ctrl.context, "session_state.json")

# Set custom headers and geolocation
await ctrl.session.set_extra_headers(ctrl.context, {"X-Custom-Header": "value"})
await ctrl.session.set_geolocation(ctrl.context, latitude=37.7749, longitude=-122.4194)
```

### 3.7. Smart Waiting Strategies (`browser.waiter`)
`browser.waiter` provides clean async wait utilities avoiding raw sleeps:

```python
from browser import waiter as bw

# Wait for selector state
await bw.wait_for_selector(page, "#data-table", state="visible", timeout=10000)

# Wait for network idle or page settlement
await bw.settle_page(page, timeout=5000)

# Wait for URL transition
await bw.wait_for_url_change(page, original_url="https://example.com/login")
```

---

## 4. Current Limitations

1. **Headed Windows on Headless Servers**: Headed browser mode (`headless=False`) requires a graphical desktop display server (or Xvfb virtual frame buffer on headless Linux).
2. **Cross-Origin Iframe Boundaries**: Form elements nested inside cross-origin iframes with strict sandbox restrictions may require explicit frame targeting before actions can be performed.

---

## 5. Technical API Reference

For detailed function signatures, argument types, and return values for every submodule method, refer to the technical reference in [`docs/BROWSER_ACTIONS.md`](../BROWSER_ACTIONS.md).
