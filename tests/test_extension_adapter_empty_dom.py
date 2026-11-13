import asyncio
import pytest
from unittest.mock import AsyncMock

from browser.extension_adapter import ExtensionBrowserAdapter, ExtensionManager


@pytest.mark.asyncio
async def test_empty_http_dom_snapshot_is_retried_until_live_dom_returns(tmp_path, monkeypatch):
    manager = type("Manager", (), {})()
    manager.send_command = AsyncMock(side_effect=[
        {"success": True, "data": {
            "url": "https://drive.google.com/drive/u/0/home",
            "browser_tab_url": "https://drive.google.com/drive/u/0/home",
            "title": "", "elements": [], "page_text": "", "headings": [],
        }},
        {"success": True, "data": {
            "url": "https://drive.google.com/drive/u/0/home",
            "browser_tab_url": "https://drive.google.com/drive/u/0/home",
            "title": "Home - Google Drive",
            "elements": [{"index": 1, "text": "New", "selector": "button"}],
            "page_text": "Google Drive",
            "headings": ["Home"],
            "form_fields": [],
        }},
    ])
    monkeypatch.setattr("browser.extension_adapter.asyncio.sleep", AsyncMock())

    adapter = ExtensionBrowserAdapter(
        task_id="task-test", manager=manager, screenshots_dir=str(tmp_path)
    )
    result = await adapter.parse_interactive_elements()

    assert result.get("inspection_error") is not True
    assert result["title"] == "Home - Google Drive"
    assert len(result["elements"]) == 1
    assert manager.send_command.await_count == 2


@pytest.mark.asyncio
async def test_persistent_empty_http_dom_fails_closed(tmp_path, monkeypatch):
    manager = type("Manager", (), {})()
    manager.send_command = AsyncMock(return_value={
        "success": True,
        "data": {
            "url": "https://drive.google.com/drive/u/0/home",
            "browser_tab_url": "https://drive.google.com/drive/u/0/home",
            "title": "", "elements": [], "page_text": "", "headings": [],
            "form_fields": [],
        },
    })
    monkeypatch.setattr("browser.extension_adapter.asyncio.sleep", AsyncMock())

    adapter = ExtensionBrowserAdapter(
        task_id="task-test", manager=manager, screenshots_dir=str(tmp_path)
    )
    result = await adapter.parse_interactive_elements()

    assert result["inspection_error"] is True
    assert result["inspection_attempts"] == 3
    assert "empty DOM snapshot" in result["inspection_error_reason"]
    assert manager.send_command.await_count == 3


def test_blank_or_restricted_page_is_not_misclassified_as_empty_http_document():
    assert ExtensionBrowserAdapter._is_empty_live_document({
        "url": "about:blank", "title": "", "elements": [], "page_text": "",
    }) is False


@pytest.mark.asyncio
async def test_restricted_or_blank_page_inspection_returns_clean_snapshot(tmp_path):
    """Verify that an about:blank initial task tab returns clean snapshot without inspection_error."""
    manager = type("Manager", (), {})()
    manager.send_command = AsyncMock(return_value={
        "success": True,
        "data": {
            "url": "about:blank",
            "browser_tab_url": "about:blank",
            "title": "",
            "elements": [],
            "page_text": "",
            "headings": [],
            "form_questions": [],
            "form_fields": [],
            "compact_forms": {"form_count": 0, "fields": []},
            "restricted": True,
        },
    })

    adapter = ExtensionBrowserAdapter(
        task_id="task-blank", manager=manager, screenshots_dir=str(tmp_path)
    )
    result = await adapter.parse_interactive_elements()

    assert result.get("inspection_error") is not True
    assert result["url"] == "about:blank"
    assert result["restricted"] is True
    assert result["elements"] == []
    assert manager.send_command.await_count == 1


@pytest.mark.asyncio
async def test_transient_extension_inspection_failure_retries_and_succeeds(tmp_path, monkeypatch):
    """Verify that a transient failure on attempt 1 is retried and succeeds on attempt 2."""
    manager = type("Manager", (), {})()
    manager.send_command = AsyncMock(side_effect=[
        {"success": False, "error": "Chrome content script did not return a DOM snapshot."},
        {"success": True, "data": {
            "url": "https://example.com",
            "browser_tab_url": "https://example.com",
            "title": "Example Domain",
            "elements": [{"index": 1, "text": "More information...", "selector": "a"}],
            "page_text": "Example Domain",
            "headings": ["Example Domain"],
            "form_fields": [],
        }},
    ])
    monkeypatch.setattr("browser.extension_adapter.asyncio.sleep", AsyncMock())

    adapter = ExtensionBrowserAdapter(
        task_id="task-retry-success", manager=manager, screenshots_dir=str(tmp_path)
    )
    result = await adapter.parse_interactive_elements()

    assert result.get("inspection_error") is not True
    assert result["title"] == "Example Domain"
    assert len(result["elements"]) == 1
    assert manager.send_command.await_count == 2


@pytest.mark.asyncio
async def test_persistent_extension_inspection_failure_fails_after_retries(tmp_path, monkeypatch):
    """Verify that persistent non-success extension responses retry up to max_attempts before failing."""
    manager = type("Manager", (), {})()
    manager.send_command = AsyncMock(return_value={
        "success": False,
        "error": "DOM inspection failed: Content script disconnected",
    })
    monkeypatch.setattr("browser.extension_adapter.asyncio.sleep", AsyncMock())

    adapter = ExtensionBrowserAdapter(
        task_id="task-fail-closed", manager=manager, screenshots_dir=str(tmp_path)
    )
    result = await adapter.parse_interactive_elements()

    assert result["inspection_error"] is True
    assert result["inspection_attempts"] == 3
    assert "Content script disconnected" in result["inspection_error_reason"]
    assert manager.send_command.await_count == 3


@pytest.mark.asyncio
async def test_extension_manager_prioritizes_authenticated_socket():
    mgr = ExtensionManager()
    ws_unauth = AsyncMock()
    ws_auth = AsyncMock()
    ws_auth.send_json = AsyncMock()

    await mgr.register(ws_unauth)
    await mgr.register(ws_auth)

    mgr.connection_tokens[ws_auth] = "token-123"
    mgr.token_connections["token-123"] = ws_auth

    async def fake_respond():
        await asyncio.sleep(0.01)
        for msg_id, (target, fut) in list(mgr.pending_requests.items()):
            if target is ws_auth and not fut.done():
                fut.set_result({"id": msg_id, "success": True, "data": {"tab_id": 42}})

    asyncio.create_task(fake_respond())
    resp = await mgr.send_command("task-auth-test", "create_tab", timeout=2.0)
    assert resp["success"] is True
    assert mgr.task_connections["task-auth-test"] is ws_auth
    assert mgr.task_tokens["task-auth-test"] == "token-123"


@pytest.mark.asyncio
async def test_extension_manager_recovers_task_on_reconnect(monkeypatch):
    mgr = ExtensionManager()
    ws_old = AsyncMock()
    ws_new = AsyncMock()
    ws_new.send_json = AsyncMock()

    mgr.active_connections.add(ws_old)
    mgr.connection_tokens[ws_old] = "tok-rec"
    mgr.token_connections["tok-rec"] = ws_old
    mgr.task_connections["task-rec"] = ws_old
    mgr.task_tokens["task-rec"] = "tok-rec"

    await mgr.unregister(ws_old)
    assert "task-rec" in mgr.disconnected_tasks

    monkeypatch.setattr(
        "backend.security.extension_auth.extension_auth_manager.register_or_authenticate",
        lambda token, extension_id, is_loopback: (True, "tok-rec", None),
    )

    await mgr.register(ws_new)
    handled = await mgr.handle_message(
        ws_new,
        {
            "type": "register",
            "token": "tok-rec",
            "extension_id": "test-ext",
            "active_tasks": ["task-rec"],
        },
    )
    assert handled is True
    assert mgr.task_connections.get("task-rec") is ws_new
    assert "task-rec" not in mgr.disconnected_tasks


@pytest.mark.asyncio
async def test_extension_manager_handles_ping_pong_heartbeat():
    mgr = ExtensionManager()
    ws = AsyncMock()
    ws.send_json = AsyncMock()

    handled = await mgr.handle_message(ws, {"type": "ping", "timestamp": 123456789})
    assert handled is True
    ws.send_json.assert_awaited_once_with({"type": "pong", "timestamp": 123456789})
