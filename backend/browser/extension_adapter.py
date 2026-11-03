"""
browser/extension_adapter.py
----------------------------
Chrome Extension Execution Layer & Bridge Manager.

Allows the AI Browser Automation Agent to execute planned actions inside the
user's active Google Chrome session via the Chrome Extension Bridge.
"""

import os
import uuid
import base64
import asyncio
import logging
from typing import Dict, Any, List, Optional, Tuple, Set
from fastapi import WebSocket

from models.schemas import BrowserAction, ActionType, FormField, FieldType

logger = logging.getLogger("ExtensionBridge")


class ExtensionManager:
    """
    Manages active Chrome Extension WebSocket connections and correlates
    bidirectional asynchronous task commands with execution responses.
    """

    def __init__(self):
        self.active_connections: Set[WebSocket] = set()
        # request id -> (originating websocket, response future). Keeping the
        # socket association prevents one disconnected client from failing
        # commands that are still in flight on another connected browser.
        self.pending_requests: Dict[str, Tuple[WebSocket, asyncio.Future]] = {}
        self.task_tabs: Dict[str, int] = {}
        # A task remains pinned to the extension socket that created its tab.
        self.task_connections: Dict[str, WebSocket] = {}
        self.closed_tasks: Set[str] = set()
        self.disconnected_tasks: Set[str] = set()
        self.lock = asyncio.Lock()

    @property
    def is_connected(self) -> bool:
        """Returns True if at least one Chrome Extension client is connected."""
        return len(self.active_connections) > 0

    async def register(self, websocket: WebSocket) -> None:
        """Registers a newly connected Chrome Extension client."""
        async with self.lock:
            self.active_connections.add(websocket)
        logger.info(f"Chrome Extension connected. Active clients: {len(self.active_connections)}")

    async def unregister(self, websocket: WebSocket) -> None:
        """Unregisters a disconnected Chrome Extension client and fails pending requests."""
        async with self.lock:
            self.active_connections.discard(websocket)
            # A task cannot silently migrate to another user's browser session.
            disconnected_tasks = [
                task_id for task_id, origin in self.task_connections.items()
                if origin is websocket
            ]
            for task_id in disconnected_tasks:
                self.task_connections.pop(task_id, None)
                self.task_tabs.pop(task_id, None)
                self.disconnected_tasks.add(task_id)

            for msg_id, (origin, fut) in list(self.pending_requests.items()):
                if origin is websocket:
                    self.pending_requests.pop(msg_id, None)
                    if not fut.done():
                        fut.set_exception(
                            RuntimeError("Chrome Extension disconnected during command execution")
                        )
        logger.warning(
            "Chrome Extension disconnected. Active clients: %s; invalidated tasks: %s",
            len(self.active_connections), len(disconnected_tasks)
        )

    async def handle_message(self, websocket: WebSocket, message: Dict[str, Any]) -> None:
        """Processes a message only from the socket that owns its task/request."""
        msg_type = message.get("type")
        msg_id = message.get("id")

        if msg_type == "pong":
            return

        if msg_type == "tab_closed":
            task_id = message.get("task_id")
            if task_id:
                async with self.lock:
                    if self.task_connections.get(task_id) is websocket:
                        self.closed_tasks.add(task_id)
                        self.task_tabs.pop(task_id, None)
                        logger.warning(f"Chrome tab for task {task_id} was closed by user.")
            return

        # Resolve correlated pending request
        if msg_id:
            async with self.lock:
                pending = self.pending_requests.get(msg_id)
                if pending:
                    origin, fut = pending
                    if origin is websocket and not fut.done():
                        fut.set_result(message)

    async def send_command(
        self,
        task_id: str,
        command_type: str,
        payload: Optional[Dict[str, Any]] = None,
        timeout: float = 12.0,
    ) -> Dict[str, Any]:
        """
        Dispatches a command to the connected Chrome Extension and awaits the result.
        """
        msg_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        # Pin a task to one browser client when its dedicated tab is created.
        # Never route an existing task to an arbitrary connected user's browser.
        async with self.lock:
            if task_id in self.closed_tasks and command_type not in ("create_tab", "close_tab"):
                raise RuntimeError(f"Chrome tab for task {task_id} is no longer available.")
            if command_type == "create_tab":
                if task_id in self.task_connections:
                    raise RuntimeError(f"Chrome tab for task {task_id} is already initialized.")
                if not self.active_connections:
                    raise RuntimeError("Chrome Extension is not connected. Please load and connect the extension.")
                target_ws = next(iter(self.active_connections))
                self.closed_tasks.discard(task_id)
                self.disconnected_tasks.discard(task_id)
                self.task_tabs.pop(task_id, None)
                self.task_connections[task_id] = target_ws
            else:
                target_ws = self.task_connections.get(task_id)
                if target_ws is None or target_ws not in self.active_connections:
                    raise RuntimeError(
                        f"No connected Chrome client is assigned to task {task_id}. "
                        "Reconnect the extension and start a new task."
                    )
            self.pending_requests[msg_id] = (target_ws, future)

        msg = {
            "id": msg_id,
            "type": command_type,
            "task_id": task_id,
        }
        if payload:
            msg.update(payload)

        # Send only to the connection associated with this request.
        try:
            await target_ws.send_json(msg)
            response = await asyncio.wait_for(future, timeout=timeout)
            return response
        except asyncio.TimeoutError:
            if command_type == "create_tab":
                async with self.lock:
                    if self.task_connections.get(task_id) is target_ws:
                        self.task_connections.pop(task_id, None)
            raise TimeoutError(f"Extension command '{command_type}' timed out after {timeout}s.")
        except Exception:
            if command_type == "create_tab":
                async with self.lock:
                    if self.task_connections.get(task_id) is target_ws:
                        self.task_connections.pop(task_id, None)
            raise
        finally:
            async with self.lock:
                self.pending_requests.pop(msg_id, None)

    def get_status(self) -> Dict[str, Any]:
        """Returns connection metrics for API and UI status indicators."""
        return {
            "connected": self.is_connected,
            "client_count": len(self.active_connections),
            "active_tasks": list(self.task_connections.keys()),
        }


# Singleton Extension Manager
extension_manager = ExtensionManager()


class ExtensionBrowserAdapter:
    """
    Adapter implementing the browser execution interface for TaskManager
    by routing commands to the user's active Chrome browser session.
    """

    def __init__(
        self,
        task_id: str,
        manager: Optional[ExtensionManager] = None,
        show_badges: bool = False,
        screenshots_dir: str = "screenshots",
    ):
        self.task_id = task_id
        self.manager = manager or extension_manager
        self.show_badges = show_badges
        self.screenshots_dir = screenshots_dir
        self.is_running = False
        self._current_url = ""
        self.element_map: Dict[int, Dict[str, Any]] = {}
        self.current_page = None  # Playwright Page placeholder for compatibility

        os.makedirs(self.screenshots_dir, exist_ok=True)

    @property
    def current_url(self) -> str:
        return self._current_url

    async def lock_ui(self) -> None:
        """Locks the UI in the browser to prevent human interaction during automated execution."""
        try:
            await self.manager.send_command(
                task_id=self.task_id,
                command_type="lock_ui",
                timeout=5.0,
            )
        except Exception as e:
            logger.debug(f"Failed to lock UI: {e}")

    async def unlock_ui(self) -> None:
        """Unlocks the UI in the browser to allow human interaction."""
        try:
            await self.manager.send_command(
                task_id=self.task_id,
                command_type="unlock_ui",
                timeout=5.0,
            )
        except Exception as e:
            logger.debug(f"Failed to unlock UI: {e}")

    async def open_auth_tab(self, url: str) -> dict:
        """Open a dedicated, foreground Chrome tab for manual Google sign-in."""
        result = await self.manager.send_command(
            task_id=self.task_id,
            command_type="open_auth_tab",
            payload={"url": url},
            timeout=20.0,
        )
        if not result or not result.get("success"):
            raise RuntimeError((result or {}).get("error", "Chrome did not open the Google sign-in tab."))
        data = result.get("data") or {}
        self._current_url = data.get("url", url)
        return data

    async def open_recovery_tab(self, url: str) -> dict:
        """Open the detected Google recovery URL in a separate tab without remapping the task tab."""
        result = await self.manager.send_command(
            task_id=self.task_id,
            command_type="open_recovery_tab",
            payload={"url": url},
            timeout=20.0,
        )
        if not result or not result.get("success"):
            raise RuntimeError((result or {}).get("error", "Chrome did not open the Google recovery tab."))
        return result.get("data") or {}

    async def focus_tab(self) -> None:
        """Bring this task's Chrome tab to the foreground for manual recovery."""
        await self.manager.send_command(
            task_id=self.task_id,
            command_type="focus_tab",
            timeout=5.0,
        )

    async def start(self, initial_url: Optional[str] = None) -> None:
        """Initializes a new dedicated Chrome tab in the user's browser session."""
        if not self.manager.is_connected:
            raise RuntimeError(
                "Chrome Extension is not connected. "
                "Please load the extension in Chrome Developer Mode and ensure it is connected to http://localhost:8000."
            )

        resp = await self.manager.send_command(
            task_id=self.task_id,
            command_type="create_tab",
            payload={"url": initial_url or "about:blank"},
            timeout=15.0,
        )
        if not resp.get("success"):
            raise RuntimeError(resp.get("error", "Failed to create Chrome task tab"))

        data = resp.get("data", {})
        tab_id = data.get("tab_id")
        if tab_id:
            self.manager.task_tabs[self.task_id] = tab_id
        self._current_url = data.get("url", initial_url or "")
        self.is_running = True
        logger.info(f"Task {self.task_id} initialized with Chrome tab #{tab_id}")
        if initial_url and initial_url != "about:blank":
            await self.lock_ui()

    async def navigate(self, url: str) -> str:
        """Navigates the task's Chrome tab to the specified URL."""
        if not url.startswith("http://") and not url.startswith("https://") and not url.startswith("about:"):
            url = f"https://{url}"

        resp = await self.manager.send_command(
            task_id=self.task_id,
            command_type="navigate",
            payload={"url": url},
            timeout=20.0,
        )
        if not resp.get("success"):
            raise RuntimeError(resp.get("error", f"Failed to navigate to {url}"))

        data = resp.get("data", {})
        self._current_url = data.get("url", url)
        await self.lock_ui()
        return self._current_url

    async def parse_interactive_elements(self) -> Dict[str, Any]:
        """
        Requests structured DOM inspection and form discovery from the Chrome content script.
        """
        resp = await self.manager.send_command(
            task_id=self.task_id,
            command_type="inspect_dom",
            payload={"show_badges": self.show_badges},
            timeout=10.0,
        )
        if not resp.get("success"):
            logger.warning(f"DOM inspection returned error: {resp.get('error')}")
            return {
                "title": "Page",
                # Do not return a cached URL as if it were a live observation.
                # Callers must not infer an active Google challenge from stale state.
                "url": "",
                "inspection_error": True,
                "elements": [],
                "page_text": "",
                "headings": [],
                "form_questions": [],
                "form_fields": [],
                "compact_forms": {},
            }

        data = resp.get("data", {})
        self._current_url = data.get("url", self._current_url)

        # Build element_map for index lookups
        raw_elements = data.get("elements", [])
        self.element_map = {item["index"]: item for item in raw_elements if "index" in item}

        # Convert raw form fields into FormField pydantic models if needed
        raw_fields = data.get("form_fields", [])
        parsed_form_fields: List[FormField] = []
        for rf in raw_fields:
            try:
                parsed_form_fields.append(
                    FormField(
                        field_type=FieldType(rf.get("field_type", "text")),
                        selector=rf.get("selector", ""),
                        label=rf.get("label"),
                        name=rf.get("name"),
                        field_id=rf.get("field_id"),
                        placeholder=rf.get("placeholder"),
                        autocomplete=rf.get("autocomplete"),
                        required=bool(rf.get("required")),
                        disabled=bool(rf.get("disabled")),
                        readonly=bool(rf.get("readonly")),
                        current_value=rf.get("current_value"),
                        aria_label=rf.get("aria_label"),
                        visible=bool(rf.get("visible", True)),
                    )
                )
            except Exception:
                pass
        data["form_fields"] = parsed_form_fields

        return data

    async def execute_action(
        self,
        action: BrowserAction,
        task_form_fields: Optional[List[FormField]] = None,
    ) -> Tuple[str, Any]:
        """
        Executes a BrowserAction in the task's Chrome tab via the extension content script.
        """
        if not self.is_running:
            raise RuntimeError(f"Task {self.task_id} execution has been cancelled/stopped.")

        action_payload = action.model_dump(exclude_none=True)
        # Ensure enum string values
        if isinstance(action.action, ActionType):
            action_payload["action"] = action.action.value

        resp = await self.manager.send_command(
            task_id=self.task_id,
            command_type="execute_action",
            payload={"action": action_payload},
            timeout=12.0,
        )

        success = resp.get("success", False)
        error_msg = resp.get("error")
        data = resp.get("data", {})

        if not success:
            raise RuntimeError(error_msg or f"Action {action.action.value} failed in Chrome Extension")

        exec_desc = data.get("description", f"Action {action.action.value.upper()} executed in Chrome")
        extracted_data = data.get("extracted_data")
        if data.get("url"):
            self._current_url = data.get("url")

        return exec_desc, extracted_data

    async def capture_screenshot(self, task_id: str, step_num: int) -> Tuple[Optional[str], Optional[str]]:
        """
        Captures visible tab screenshot from Chrome Extension, writes PNG to disk,
        and returns (filepath, web_url).
        """
        filename = f"{task_id}_step_{step_num}.png"
        filepath = os.path.join(self.screenshots_dir, filename)
        web_url = f"/screenshots/{filename}"

        try:
            resp = await self.manager.send_command(
                task_id=self.task_id,
                command_type="capture_screenshot",
                payload={"step_number": step_num},
                timeout=10.0,
            )
            if resp.get("success"):
                data = resp.get("data", {})
                data_url = data.get("screenshot")
                if data_url and "," in data_url:
                    base64_data = data_url.split(",", 1)[1]
                    image_bytes = base64.b64decode(base64_data)
                    with open(filepath, "wb") as f:
                        f.write(image_bytes)
                    return filepath, web_url
        except Exception as exc:
            logger.warning(f"Screenshot capture failed via extension: {exc}")

        return None, None

    async def check_captcha_or_payment(self) -> Optional[str]:
        """Checks if current page has security challenges or payment walls."""
        url_lower = self._current_url.lower()
        if any(w in url_lower for w in ["captcha", "recaptcha", "cloudflare", "challenge"]):
            return "captcha"
        if any(w in url_lower for w in ["checkout", "payment", "billing", "pay"]):
            return "payment"
        return None

    async def is_google_signin_page(self) -> bool:
        """Checks if Chrome tab navigated to Google authentication."""
        url_lower = self._current_url.lower()
        return "accounts.google.com" in url_lower or "google.com/signin" in url_lower

    async def clear_badges(self) -> None:
        """Clears overlay badges from Chrome tab."""
        try:
            await self.manager.send_command(
                task_id=self.task_id,
                command_type="clear_badges",
                timeout=5.0,
            )
        except Exception:
            pass

    async def stop(self) -> None:
        """Releases the task tab and cleans up."""
        self.is_running = False
        try:
            await self.unlock_ui()
        except Exception:
            pass
        try:
            await self.clear_badges()
        except Exception:
            pass
        async with self.manager.lock:
            self.manager.task_tabs.pop(self.task_id, None)
            self.manager.task_connections.pop(self.task_id, None)
            self.manager.closed_tasks.discard(self.task_id)
            self.manager.disconnected_tasks.discard(self.task_id)
        logger.info(f"Task {self.task_id} adapter stopped.")
