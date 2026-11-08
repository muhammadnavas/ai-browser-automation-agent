import pytest
from unittest.mock import AsyncMock

from browser.extension_adapter import ExtensionBrowserAdapter


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
