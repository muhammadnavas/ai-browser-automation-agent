"""
browser/extension_adapter.py
----------------------------
Chrome Extension Execution Layer & Bridge Manager.

Allows the AI Browser Automation Agent to execute planned actions inside the
user's active Google Chrome session via the Chrome Extension Bridge.
"""

import os
import sys
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
        self.task_tokens: Dict[str, str] = {}
        self.closed_tasks: Set[str] = set()
        self.disconnected_tasks: Set[str] = set()
        self.connection_tokens: Dict[WebSocket, str] = {}
        self.token_connections: Dict[str, WebSocket] = {}
        self._task_locks: Dict[str, asyncio.Lock] = {}
        self.lock = asyncio.Lock()

    def get_task_lock(self, task_id: str) -> asyncio.Lock:
        """Returns a per-task serialization lock to prevent concurrent command races."""
        if task_id not in self._task_locks:
            self._task_locks[task_id] = asyncio.Lock()
        return self._task_locks[task_id]

    @property
    def is_connected(self) -> bool:
        """Returns True if at least one authenticated Chrome Extension client is connected."""
        if not self.active_connections:
            return False
        if self.connection_tokens:
            return any(ws in self.connection_tokens for ws in self.active_connections)
        return True

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
            # Preserve task_tabs mapping across temporary disconnections.
            disconnected_tasks = [
                task_id for task_id, origin in self.task_connections.items()
                if origin is websocket
            ]
            for task_id in disconnected_tasks:
                self.task_connections.pop(task_id, None)
                self.disconnected_tasks.add(task_id)

            tok = self.connection_tokens.pop(websocket, None)
            if tok and self.token_connections.get(tok) is websocket:
                self.token_connections.pop(tok, None)

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

    async def handle_message(self, websocket: WebSocket, message: Dict[str, Any]) -> bool:
        """Processes a message only from the socket that owns its task/request.
        Returns False if the connection was terminated or rejected; True to continue."""
        msg_type = message.get("type")
        msg_id = message.get("id")

        if msg_type == "pong":
            return True

        if msg_type == "ping":
            try:
                await websocket.send_json({"type": "pong", "timestamp": message.get("timestamp")})
            except Exception:
                pass
            return True

        if msg_type == "register":
            from backend.security.extension_auth import extension_auth_manager
            from backend.security.auth import session_manager
            token = message.get("token")
            extension_id = message.get("extension_id")
            client_host = "127.0.0.1"
            if hasattr(websocket, "client") and websocket.client:
                raw_host = getattr(websocket.client, "host", None)
                if isinstance(raw_host, str):
                    client_host = raw_host
            is_loop = session_manager.is_loopback(client_host)

            auth_ok, active_token, reason = extension_auth_manager.register_or_authenticate(
                token=token,
                extension_id=extension_id,
                is_loopback=is_loop,
            )
            if not auth_ok:
                try:
                    await websocket.send_json({"type": "auth_failed", "reason": reason})
                    await websocket.close(code=4003, reason=reason)
                except Exception:
                    pass
                await self.unregister(websocket)
                return False

            async with self.lock:
                if active_token:
                    # Clean up any stale connection for this active token
                    old_ws = self.token_connections.get(active_token)
                    if old_ws and old_ws is not websocket and old_ws in self.active_connections:
                        self.active_connections.discard(old_ws)
                        self.connection_tokens.pop(old_ws, None)
                        for tid, origin in list(self.task_connections.items()):
                            if origin is old_ws:
                                self.task_connections[tid] = websocket
                                logger.info("Re-pinned task %s to reconnected extension client.", tid)
                        try:
                            await old_ws.close(code=1000, reason="Replaced by new connection")
                        except Exception:
                            pass

                    self.connection_tokens[websocket] = active_token
                    self.token_connections[active_token] = websocket

                self.active_connections.add(websocket)

                # Re-associate tasks reported by extension as still active
                active_tasks = message.get("active_tasks")
                if isinstance(active_tasks, list):
                    for tid in active_tasks:
                        if isinstance(tid, str) and tid not in self.closed_tasks:
                            self.task_connections[tid] = websocket
                            self.disconnected_tasks.discard(tid)

                task_tabs_payload = message.get("task_tabs")
                if isinstance(task_tabs_payload, dict):
                    for tid, tab_id in task_tabs_payload.items():
                        if tid not in self.closed_tasks:
                            self.task_tabs[tid] = tab_id

                if active_token:
                    for tid, tok in list(self.task_tokens.items()):
                        if tok == active_token and tid not in self.closed_tasks:
                            self.task_connections[tid] = websocket
                            self.disconnected_tasks.discard(tid)

            try:
                await websocket.send_json({
                    "type": "registered",
                    "token": active_token,
                    "client_count": len(self.active_connections),
                })
            except Exception:
                pass
            return True

        if msg_type == "tab_closed":
            task_id = message.get("task_id")
            if task_id:
                async with self.lock:
                    if self.task_connections.get(task_id) is websocket:
                        self.closed_tasks.add(task_id)
                        self.task_tabs.pop(task_id, None)
                        logger.warning(f"Chrome tab for task {task_id} was closed by user.")
            return True

        # Resolve correlated pending request
        if msg_id:
            async with self.lock:
                pending = self.pending_requests.get(msg_id)
                if pending:
                    origin, fut = pending
                    is_valid_origin = (
                        origin is websocket
                        or websocket in self.active_connections
                    )
                    if is_valid_origin and not fut.done():
                        fut.set_result(message)
        return True

    async def disconnect_token(self, token: str) -> None:
        """Disconnect and invalidate an extension socket matching a revoked token."""
        async with self.lock:
            ws = self.token_connections.pop(token, None)
            if ws:
                self.connection_tokens.pop(ws, None)
                self.active_connections.discard(ws)
                try:
                    await ws.send_json({"type": "auth_failed", "reason": "Credential revoked."})
                    await ws.close(code=4003, reason="Revoked")
                except Exception:
                    pass

    async def disconnect_all(self) -> None:
        """Disconnect all connected extension clients."""
        async with self.lock:
            for ws in list(self.active_connections):
                try:
                    await ws.send_json({"type": "auth_failed", "reason": "All extension credentials revoked."})
                    await ws.close(code=4003, reason="Revoked")
                except Exception:
                    pass
            self.active_connections.clear()
            self.connection_tokens.clear()
            self.token_connections.clear()

    async def send_command(
        self,
        task_id: str,
        command_type: str,
        payload: Optional[Dict[str, Any]] = None,
        timeout: float = 30.0,
    ) -> Dict[str, Any]:
        """
        Dispatches a command to the connected Chrome Extension and awaits the result.
        Serializes execution per task to prevent command race conditions.
        """
        task_lock = self.get_task_lock(task_id)
        async with task_lock:
            msg_id = str(uuid.uuid4())
            loop = asyncio.get_running_loop()
            future = loop.create_future()
            target_ws = None
            reconnect_deadline = loop.time() + (0.0 if command_type in ("create_tab", "close_tab") else 3.0)

            while True:
                async with self.lock:
                    if task_id in self.closed_tasks and command_type not in ("create_tab", "close_tab"):
                        raise RuntimeError(f"Chrome tab for task {task_id} is no longer available.")
                    if command_type == "create_tab":
                        if task_id in self.task_connections:
                            raise RuntimeError(f"Chrome tab for task {task_id} is already initialized.")
                        if not self.active_connections:
                            raise RuntimeError("Chrome Extension is not connected. Please load and connect the extension.")
                        auth_sockets = [ws for ws in self.active_connections if ws in self.connection_tokens]
                        target_ws = auth_sockets[-1] if auth_sockets else next(iter(self.active_connections))
                        self.closed_tasks.discard(task_id)
                        self.disconnected_tasks.discard(task_id)
                        self.task_tabs.pop(task_id, None)
                        self.task_connections[task_id] = target_ws
                        token = self.connection_tokens.get(target_ws)
                        if token:
                            self.task_tokens[task_id] = token
                        self.pending_requests[msg_id] = (target_ws, future)
                        break
                    else:
                        ws_candidate = self.task_connections.get(task_id)
                        if (ws_candidate is None or ws_candidate not in self.active_connections) and task_id not in self.closed_tasks:
                            # Attempt recovery if matching client reconnected
                            token = self.task_tokens.get(task_id)
                            if token and token in self.token_connections:
                                candidate_ws = self.token_connections[token]
                                if candidate_ws in self.active_connections:
                                    ws_candidate = candidate_ws
                                    self.task_connections[task_id] = ws_candidate
                                    self.disconnected_tasks.discard(task_id)
                                    logger.info("Auto-recovered task %s to reconnected client with matching token.", task_id)
                            elif not token and self.active_connections:
                                # Fallback for environments without token auth (e.g. testing)
                                auth_sockets = [ws for ws in self.active_connections if ws in self.connection_tokens]
                                candidate_ws = auth_sockets[-1] if auth_sockets else next(iter(self.active_connections))
                                ws_candidate = candidate_ws
                                self.task_connections[task_id] = ws_candidate
                                self.disconnected_tasks.discard(task_id)

                        if ws_candidate is not None and ws_candidate in self.active_connections:
                            target_ws = ws_candidate
                            self.pending_requests[msg_id] = (target_ws, future)
                            break

                if loop.time() >= reconnect_deadline:
                    raise RuntimeError(
                        f"No connected Chrome client is assigned to task {task_id}. "
                        "Reconnect the extension and start a new task."
                    )
                await asyncio.sleep(0.1)

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
                async with self.lock:
                    if self.task_connections.get(task_id) is target_ws:
                        self.disconnected_tasks.add(task_id)
                raise
            finally:
                async with self.lock:
                    self.pending_requests.pop(msg_id, None)

    def get_status(self) -> Dict[str, Any]:
        """Returns connection metrics for API and UI status indicators."""
        auth_count = (
            len([ws for ws in self.active_connections if ws in self.connection_tokens])
            if self.connection_tokens
            else len(self.active_connections)
        )
        return {
            "connected": self.is_connected,
            "client_count": auth_count,
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
        """Best-effort: apply a DOM interaction overlay on the task tab.

        A failed or timed-out lock is logged but never raises — the tab may
        still be loading (about:blank → real URL transition) or the page may
        be a restricted chrome:// URL that intentionally skips overlays.
        Failing here must never abort a task.
        """
        try:
            result = await self.manager.send_command(
                task_id=self.task_id,
                command_type="lock_ui",
                timeout=8.0,
            )
            if result and result.get("success"):
                logger.info("Task-tab interaction lock confirmed for task %s", self.task_id)
            else:
                error = (result or {}).get("error") or "Chrome did not confirm the task-tab lock."
                logger.warning("lock_ui non-fatal: %s (task=%s)", error, self.task_id)
        except Exception as exc:
            # Timeouts and connection errors during lock are non-fatal.
            logger.warning("lock_ui non-fatal: %s (task=%s)", exc, self.task_id)

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
        from backend.browser.security import validate_url_security
        safe, reason, normalized = validate_url_security(url, resolve_dns=True)
        if not safe:
            raise ValueError(f"Auth tab navigation blocked by security policy: {reason}")
        url = normalized or url

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
        from backend.browser.security import validate_url_security
        safe, reason, normalized = validate_url_security(url, resolve_dns=True)
        if not safe:
            raise ValueError(f"Recovery tab navigation blocked by security policy: {reason}")
        url = normalized or url

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

        if self.task_id in self.manager.task_tabs:
            tab_id = self.manager.task_tabs[self.task_id]
            logger.info("Task %s already holds tab #%s; reusing without creating new tab.", self.task_id, tab_id)
            self.is_running = True
            if initial_url and initial_url != "about:blank":
                await self.navigate(initial_url)
            await self.lock_ui()
            return

        if initial_url and initial_url != "about:blank":
            from backend.browser.security import validate_url_security
            safe, reason, normalized = validate_url_security(initial_url, resolve_dns=True)
            if not safe:
                raise ValueError(f"Initial tab creation blocked by security policy: {reason}")
            initial_url = normalized or initial_url

        resp = await self.manager.send_command(
            task_id=self.task_id,
            command_type="create_tab",
            payload={"url": initial_url or "about:blank"},
            timeout=30.0,
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
        # Apply the lock even for about:blank. The first navigation must not
        # create a window in which the task tab is accidentally interactive.
        await self.lock_ui()

    async def navigate(self, url: str) -> str:
        """Navigates the task's Chrome tab to the specified URL."""
        if not url.startswith("http://") and not url.startswith("https://") and not url.startswith("about:"):
            url = f"https://{url}"

        if url != "about:blank":
            from backend.browser.security import validate_url_security
            safe, reason, normalized = validate_url_security(url, resolve_dns=True)
            if not safe:
                raise ValueError(f"Navigation blocked by security policy: {reason}")
            url = normalized or url

        resp = await self.manager.send_command(
            task_id=self.task_id,
            command_type="navigate",
            payload={"url": url},
            timeout=20.0,
        )
        if not resp.get("success"):
            raise RuntimeError(resp.get("error", f"Failed to navigate to {url}"))

        data = resp.get("data", {})
        new_url = data.get("url", url)
        if new_url and new_url != "about:blank":
            from backend.browser.security import validate_url_security
            safe, reason, _ = validate_url_security(new_url, resolve_dns=False)
            if not safe:
                self.is_running = False
                raise RuntimeError(f"Unauthorized navigation detected in Chrome tab: {reason}")
        self._current_url = new_url
        await self.lock_ui()
        return self._current_url

    @staticmethod
    def _is_empty_live_document(data: Dict[str, Any]) -> bool:
        """Detect a failed snapshot handshake on a real web page.

        An empty DOM can be legitimate on about:blank/restricted pages, but an
        HTTP(S) page with no title, text, headings, controls, or form fields is
        not usable evidence. Treat it as an inspection failure, not as a page
        the planner can act on.
        """
        url = str(data.get("browser_tab_url") or data.get("url") or "").strip().lower()
        if not url.startswith(("http://", "https://")):
            return False
        return not any((
            str(data.get("title") or "").strip(),
            str(data.get("page_text") or "").strip(),
            bool(data.get("headings")),
            bool(data.get("elements")),
            bool(data.get("form_fields")),
        ))

    @staticmethod
    def _inspection_failure(reason: str, attempts: int = 1) -> Dict[str, Any]:
        return {
            "title": "Inspection unavailable",
            # Never present stale cached page content/URL as a live observation.
            "url": "",
            "inspection_error": True,
            "inspection_error_reason": reason,
            "inspection_attempts": attempts,
            "elements": [],
            "page_text": "",
            "headings": [],
            "form_questions": [],
            "form_fields": [],
            "compact_forms": {},
        }

    async def parse_interactive_elements(self) -> Dict[str, Any]:
        """Inspect the live task tab and fail closed when its DOM snapshot is empty."""
        data: Dict[str, Any] = {}
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            resp = await self.manager.send_command(
                task_id=self.task_id,
                command_type="inspect_dom",
                payload={"show_badges": self.show_badges},
                timeout=18.0,
            )
            if not resp.get("success"):
                reason = str(resp.get("error") or "Chrome content script did not return a DOM snapshot.")
                logger.warning("DOM inspection failed for task %s (attempt %s/%s): %s",
                               self.task_id, attempt, max_attempts, reason)
                if attempt < max_attempts:
                    await asyncio.sleep(0.8 * attempt)
                    continue
                return self._inspection_failure(reason, attempt)

            candidate = resp.get("data")
            if not isinstance(candidate, dict):
                candidate = {}
            data = candidate
            if not self._is_empty_live_document(data):
                break

            observed_url = data.get("browser_tab_url") or data.get("url") or "unknown URL"
            logger.warning(
                "Received an empty DOM snapshot for task %s at %s (attempt %s/%s); "
                "waiting for the page/content script to become inspectable.",
                self.task_id, observed_url, attempt, max_attempts,
            )
            if attempt < max_attempts:
                # A bounded retry handles SPA transitions without allowing
                # the planner to burn its full action budget on empty snapshots.
                await asyncio.sleep(0.8 * attempt)
        else:
            observed_url = data.get("browser_tab_url") or data.get("url") or "unknown URL"
            return self._inspection_failure(
                f"Chrome returned an empty DOM snapshot on {observed_url} after {max_attempts} attempts. "
                "The content script may be stale or the page may still be transitioning.",
                max_attempts,
            )

        self._current_url = data.get("url", self._current_url)

        # Sanitize extension-returned DOM and form fields at backend boundary
        try:
            from backend.security import redact_text, is_auth_field, redact_sensitive_data

            for item in data.get("elements", []):
                if is_auth_field(item):
                    item["value"] = ""
                elif item.get("value"):
                    item["value"] = redact_text(str(item["value"]), mask_tier3=True, preserve_public_contacts=True)
                if item.get("placeholder"):
                    item["placeholder"] = redact_text(str(item["placeholder"]), mask_tier3=True, preserve_public_contacts=True)
                if item.get("label"):
                    item["label"] = redact_text(str(item["label"]), mask_tier3=True, preserve_public_contacts=True)
                if item.get("text"):
                    item["text"] = redact_text(str(item["text"]), mask_tier3=True, preserve_public_contacts=True)
                if item.get("aria_label"):
                    item["aria_label"] = redact_text(str(item["aria_label"]), mask_tier3=True, preserve_public_contacts=True)
                if item.get("accessible_name"):
                    item["accessible_name"] = redact_text(str(item["accessible_name"]), mask_tier3=True, preserve_public_contacts=True)
                if item.get("title"):
                    item["title"] = redact_text(str(item["title"]), mask_tier3=True, preserve_public_contacts=True)

            if data.get("page_text"):
                data["page_text"] = redact_text(data["page_text"], mask_tier3=True, preserve_public_contacts=True)
            if data.get("headings"):
                data["headings"] = [redact_text(h, mask_tier3=True, preserve_public_contacts=True) for h in data["headings"]]
            if data.get("form_questions"):
                data["form_questions"] = [redact_text(q, mask_tier3=True, preserve_public_contacts=True) for q in data["form_questions"]]

            # Build element_map for index lookups
            raw_elements = data.get("elements", [])
            self.element_map = {item["index"]: item for item in raw_elements if "index" in item}

            # Convert raw form fields into FormField pydantic models with sanitization
            raw_fields = data.get("form_fields", [])
            parsed_form_fields: List[FormField] = []
            for rf in raw_fields:
                try:
                    cval = rf.get("current_value")
                    lbl = rf.get("label")
                    ph = rf.get("placeholder")
                    aria = rf.get("aria_label")

                    if is_auth_field(rf):
                        cval = ""
                    elif cval:
                        cval = redact_text(str(cval), mask_tier3=True, preserve_public_contacts=True)

                    if lbl:
                        lbl = redact_text(str(lbl), mask_tier3=True, preserve_public_contacts=True)
                    if ph:
                        ph = redact_text(str(ph), mask_tier3=True, preserve_public_contacts=True)
                    if aria:
                        aria = redact_text(str(aria), mask_tier3=True, preserve_public_contacts=True)

                    parsed_form_fields.append(
                        FormField(
                            field_type=FieldType(rf.get("field_type", "text")),
                            selector=rf.get("selector", ""),
                            label=lbl,
                            name=rf.get("name"),
                            field_id=rf.get("field_id"),
                            placeholder=ph,
                            autocomplete=rf.get("autocomplete"),
                            required=bool(rf.get("required")),
                            disabled=bool(rf.get("disabled")),
                            readonly=bool(rf.get("readonly")),
                            current_value=cval,
                            aria_label=aria,
                            visible=bool(rf.get("visible", True)),
                        )
                    )
                except Exception:
                    pass
            data["form_fields"] = parsed_form_fields
        except Exception as sec_exc:
            logger.error(f"Extension DOM sanitization failed, failing closed: {sec_exc}")
            return {
                "title": "Error",
                "url": self._current_url,
                "inspection_error": True,
                "elements": [],
                "page_text": "",
                "headings": [],
                "form_questions": [],
                "form_fields": [],
                "compact_forms": {},
            }

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

        # Validate target link or form submission destination before dispatch
        element_id = getattr(action, "element_id", None)
        if element_id is not None and element_id in self.element_map:
            elem = self.element_map[element_id]
            dest = elem.get("href") or elem.get("form_action") or elem.get("action")
            if dest and isinstance(dest, str) and dest.strip() and not dest.startswith(("#", "javascript:", "about:")):
                from backend.browser.security import validate_url_security
                safe, reason, _ = validate_url_security(dest, resolve_dns=True)
                if not safe:
                    raise ValueError(f"Action target URL blocked by security policy: {reason}")

        action_payload = action.model_dump(exclude_none=True)
        # Ensure enum string values
        if isinstance(action.action, ActionType):
            action_payload["action"] = action.action.value

        action_text_lower = str(action.text or "").strip().lower()
        action_reason_lower = str(action.reasoning or "").lower()
        is_menu_or_nav = (
            action_text_lower in ("new", "+ new", "+new", "create", "menu")
            or "to ensure menu opens" in action_reason_lower
            or "start the upload/create flow" in action_reason_lower
            or "open the 'new' menu" in action_reason_lower
            or "waiting for the 'new' menu" in action_reason_lower
            or ("folder" in action_reason_lower and "file upload" not in action_text_lower)
        )

        is_upload = not is_menu_or_nav and (
            action.action in (ActionType.UPLOAD, ActionType.UPLOAD_FILE)
            or bool(action_payload.get("file_name"))
            or bool(action_payload.get("file_base64"))
            or any(k in action_text_lower for k in ["add file", "choose file", "browse", "file upload", "upload file", "drop files"])
            or any(k in action_reason_lower for k in ["file upload", "upload file", "attach file", "choose file", "drop files"])
        )
        cmd_timeout = 60.0 if is_upload else 25.0

        os_dialog_task = None
        if is_upload and sys.platform == "win32":
            try:
                from backend.browser.os_dialog import handle_os_file_dialog
                resolved_file = None
                if action.upload_paths:
                    resolved_file = action.upload_paths[0]
                elif action_payload.get("upload_paths"):
                    resolved_file = action_payload["upload_paths"][0]
                os_dialog_task = asyncio.create_task(
                    handle_os_file_dialog(
                        goal=str(action.text or ""),
                        context_text=f"{action.reasoning or ''} {action.text or ''}",
                        wait_for_dialog_secs=25.0,
                        accept_types=getattr(action, "file_type", None),
                        user_approved=True,
                        file_path=resolved_file,
                    )
                )
            except Exception as e:
                logger.debug(f"Could not spawn OS dialog watcher: {e}")

        try:
            resp = await self.manager.send_command(
                task_id=self.task_id,
                command_type="execute_action",
                payload={"action": action_payload},
                timeout=cmd_timeout,
            )
            if os_dialog_task and not os_dialog_task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(os_dialog_task), timeout=8.0)
                except Exception:
                    pass
        finally:
            if os_dialog_task and not os_dialog_task.done():
                os_dialog_task.cancel()

        success = resp.get("success", False)
        error_msg = resp.get("error")
        data = resp.get("data", {})

        if not success:
            raise RuntimeError(error_msg or f"Action {action.action.value} failed in Chrome Extension")

        exec_desc = data.get("description", f"Action {action.action.value.upper()} executed in Chrome")
        extracted_data = data.get("extracted_data")
        if data.get("url"):
            new_url = data.get("url")
            if new_url and new_url != "about:blank":
                from backend.browser.security import validate_url_security
                safe, reason, _ = validate_url_security(new_url, resolve_dns=False)
                if not safe:
                    self.is_running = False
                    raise RuntimeError(f"Unauthorized navigation detected in Chrome tab after action: {reason}")
            self._current_url = new_url

        return exec_desc, extracted_data

    async def capture_screenshot(self, task_id: str, step_num: int) -> Tuple[Optional[str], Optional[str]]:
        """
        Captures visible tab screenshot from Chrome Extension, writes PNG to disk,
        and returns (filepath, web_url).
        """
        # Protect against capturing screenshots of local dashboard or backend administrative origins
        try:
            from urllib.parse import urlparse
            p_url = urlparse(self._current_url or "")
            h = (p_url.hostname or "").lower()
            p = p_url.port
            if h in ("localhost", "127.0.0.1", "0.0.0.0", "::1") and (p in (8000, 5173, 3000, 8080)):
                logger.warning("Skipping extension screenshot: protected backend/dashboard origin.")
                return None, None
        except Exception:
            pass

        # Check if auth/secret fields are currently active on the page and safe masking cannot be assured
        try:
            from backend.security import is_auth_field
            if any(is_auth_field(el) for el in getattr(self, "element_map", {}).values()):
                logger.warning("Skipping extension screenshot: password or OTP fields present on page.")
                return None, None
        except Exception:
            return None, None

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

    async def close_tab(self) -> None:
        """Closes the dedicated task tab in Chrome."""
        try:
            await self.manager.send_command(
                task_id=self.task_id,
                command_type="close_tab",
                timeout=5.0,
            )
        except Exception as exc:
            logger.warning("Failed to close tab for task %s: %s", self.task_id, exc)

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
