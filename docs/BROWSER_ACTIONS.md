# Reusable Browser Actions Module — Playwright Integration

This document covers the structure, APIs, and design of the fully modular, reusable browser automation engine implemented in the `browser/` package.

---

## 1. Directory Structure

The package is split into specialized sub-modules to separate concerns and provide cleaner code organization:

```
browser/
├── __init__.py          # Public package entry point
├── controller.py        # Lifecycle (Chromium, Firefox, WebKit), fullscreen, start/stop
├── actions.py           # Click, double-click, hover, fill, checkbox, radios, scroll, drag-drop
├── tabs.py              # TabManager (opening, closing, switching, popups)
├── dialogs.py           # Alert/confirm/prompt handling, file upload chooser
├── waiter.py            # Explicit wait strategies (selectors, text, load-state, network)
├── downloader.py        # DownloadManager (download trigger, save, unique-name collision control)
└── session.py           # SessionManager (cookies, storage state, headers, localStorage, GPS)
```

All functions in `actions.py`, `dialogs.py`, and `waiter.py` operate on standard Playwright `Page` objects, allowing them to be run directly on any active tab.

---

## 2. API Reference

### 2.1. BrowserController
The primary lifecycle wrapper defined in `browser/controller.py`.

```python
from browser import BrowserController

# Initialize in headed (visible) mode, starting maximized
async with BrowserController(headless=False, maximized=True) as ctrl:
    await ctrl.navigate("https://www.google.com")
```

#### Properties
- `.tabs`: Returns the `TabManager` instance.
- `.downloads`: Returns the `DownloadManager` instance.
- `.session`: Returns the `SessionManager` instance.
- `.current_page`: The currently active Playwright `Page` tab.
- `.current_url`: URL of the active tab.

#### Methods
- `await start()`: Launches browser engine and initializes sub-modules.
- `await stop()`: Gracefully halts browser processes.
- `await navigate(url, wait_until, timeout)`: Preprends `https://` if needed, navigates tab, and waits for load.
- `await reload()`, `await go_back()`, `await go_forward()`: History controls.
- `await get_title()`: Returns active page title.
- `await capture_screenshot(task_id, step_num, full_page)`: Captures tab PNG.
- `await capture_element_screenshot(selector, task_id, step_num)`: Captures element PNG.
- `await parse_interactive_elements()`: Identifies clickable/input elements and overlays numbered badges.
- `await clear_badges()`: Removes badges from the DOM.

---

### 2.2. Page Interactions (`browser/actions.py`)

All interaction functions accept a Playwright `Page` object as the first parameter.

- `await fill_field(page, selector, value, clear_first)`: Clears and writes to text/input.
- `await type_into(page, selector, text, delay_ms)`: Types realistic key-by-key strokes.
- `await click(page, selector, force)`: Clicks an element.
- `await double_click(page, selector)`: Double-clicks.
- `await right_click(page, selector)`: Context menu click.
- `await hover(page, selector)`: Moves mouse to element (triggers CSS `:hover`).
- `await press_key(page, key, selector)`: Presses keyboard key (e.g. `Enter`, `Control+A`).
- `await select_option(page, selector, value, label, index)`: Selects option inside dropdown.
- `await check_checkbox(page, selector, checked)`: Checks or unchecks a box.
- `await get_checkbox_state(page, selector)`: Returns boolean checked status.
- `await select_radio(page, name, value)`: Toggles radio button by group name and value.
- `await select_radio_by_label(page, label_text)`: Selects radio by visible label.
- `await drag_and_drop(page, source_selector, target_selector)`: Simulates drag-drop gesture.
- `await scroll_page(page, direction, amount)`: Viewport scrolls up or down.
- `await scroll_to_element(page, selector)`: Scrolls selector into view.
- `await get_text(page, selector)`: Extract element inner text.
- `await get_attribute(page, selector, attribute)`: Extract HTML element attribute.
- `await get_page_text(page)`: Grabs body inner text.
- `await is_visible(page, selector)` / `await is_enabled(page, selector)`: State queries.

---

### 2.3. Multi-Tab Management (`browser/tabs.py`)

Accessible via `ctrl.tabs` or directly through `TabManager(context)`.

- `await open_new_tab(url)`: Opens new tab and returns the `Page`.
- `await close_tab(page)`: Closes specific tab.
- `await close_all_except(page)`: Closes all tabs except the specified one.
- `list_tabs()`: List of all open `Page` objects.
- `get_tab_count()`: Integer tab count.
- `await switch_to(page)`: Focuses the specified tab.
- `await switch_to_index(index)`: Focuses the tab at the given index.
- `await wait_for_popup(trigger, timeout)`: Catch popup windows triggered by actions (e.g. clicking a `target="_blank"` link).

---

### 2.4. Dialog Handling (`browser/dialogs.py`)

Alert, prompt, confirm, and file upload overrides. Handler registers must be defined **before** triggering the opening action.

- `auto_accept(page, prompt_text)`: One-time listener to accept next alert/confirm/prompt.
- `auto_dismiss(page)`: One-time listener to dismiss/cancel next dialog.
- `await capture_next_dialog(page, trigger)`: Captures dialog text, accepts it, and returns details.
- `accept_with_text(page, text)`: Accepts a prompt typing `text`.
- `await handle_file_upload(page, trigger, file_paths)`: Overrides next file chooser dialog to upload target absolute file paths.

---

### 2.5. Wait Strategies (`browser/waiter.py`)

Explicit wait routines.

- `await wait_for_selector(page, selector, state, timeout)`: Wait for element state (`visible`, `hidden`, `attached`, `detached`).
- `await wait_for_text(page, text, timeout)`: Wait until text appears anywhere in the body.
- `await wait_for_navigation(page, url_pattern)`: Wait for navigation URL to match.
- `await wait_for_load_state(page, state)`: Wait for `'domcontentloaded'`, `'load'`, or `'networkidle'`.
- `await wait_for_network_idle(page)`: Wait until no new request starts for 500ms.
- `await wait_for_url_change(page, original_url)`: Wait until current page URL changes.
- `await wait_for_condition(condition_async)`: Polls an async conditional function until True.
- `await sleep(seconds)`: Pauses execution thread.

---

### 2.6. File Downloads (`browser/downloader.py`)

Accessible via `ctrl.downloads` or `DownloadManager(save_dir)`.

- `await start_download(page, trigger, filename)`: Triggers download, saves to disk, and returns the absolute local path.
- `await wait_for_download(page, trigger)`: Awaits download start and returns raw Playwright `Download` object.
- `await save_download(download, filename)`: Writes raw download object to disk.
- `list_downloads()`: Lists files present inside the download directory.

---

### 2.7. Session & Cookie Storage (`browser/session.py`)

Accessible via `ctrl.session` or `SessionManager()`.

- `await save_storage(context, path)`: Dumps cookies and localStorage states into a JSON file.
- `await load_storage(context, path)`: Restores cookies from storage file.
- `await get_cookies(context)` / `await add_cookies(context, cookies)` / `await clear_cookies(context)`: Cookies helper.
- `await set_viewport(page, width, height)`: Resizes target tab viewport.
- `await set_geolocation(context, lat, lng)`: Overrides GPS coordinates.
- `await set_extra_headers(context, headers)`: Header override.
- `await set_local_storage(page, key, value)` / `await get_local_storage(page, key)` / `await clear_local_storage(page)`: localStorage helper.

---

## 3. Data Schema Integration

The browser actions are fully integrated into `models/schemas.py` and `api/task_manager.py`.

### 3.1. BrowserAction Schema
The agent planner returns `BrowserAction` payloads containing target arguments:

```python
class BrowserAction(BaseModel):
    action: ActionType               # ActionType.CHECK_BOX, ActionType.SELECT_OPTION, etc.
    selector: Optional[str]
    element_id: Optional[int]
    text: Optional[str]              # Used for type_text and url
    value: Optional[str]             # Select value / fill value
    checked: Optional[bool]          # True to check, False to uncheck
    radio_name: Optional[str]        # Radio button input name
    select_label: Optional[str]      # Dropdown option text
    select_index: Optional[int]      # Dropdown index
    tab_index: Optional[int]         # Switch to tab index
    dialog_action: Optional[str]     # accept | dismiss
    dialog_response: Optional[str]   # Prompt fill text
    download_filename: Optional[str] # Download file override name
    upload_paths: Optional[List[str]]# Absolute files to upload
    source_selector: Optional[str]   # Drag source
    target_selector: Optional[str]   # Drag drop target
    key: Optional[str]               # Keyboard key code
```

### 3.2. Execution Handling
When task execution starts in `api/task_manager.py`, the task manager maps each `ActionType` directly to its respective sub-module wrapper:

```python
# Task execution snippet mapping (api/task_manager.py)
elif action.action == ActionType.CHECK_BOX and action.selector:
    ok = await browser_actions.check_checkbox(
        page, action.selector,
        checked=action.checked if action.checked is not None else True
    )

elif action.action == ActionType.DOWNLOAD_FILE and action.selector:
    save_path = await controller.downloads.start_download(
        page,
        lambda: browser_actions.click(page, action.selector),
        filename=action.download_filename
    )
```

---

## 4. Verification Manuals

To verify code changes, the following scripts and test runs are available:

### Automated Tests
Run the async pytest suite:
```bash
python -m pytest tests/test_browser_module.py -v
```

### Headed Feature Demo Task
Run the fullscreen local verification task showing all features in a visible window:
```bash
python run_demo_task.py
```
