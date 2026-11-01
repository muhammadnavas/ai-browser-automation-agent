"""
browser/dialogs.py
------------------
Handlers for native browser dialogs (alert, confirm, prompt) and
file chooser dialogs (upload).

All handler registration is done at the Page level; call the appropriate
function BEFORE triggering the action that opens the dialog.
"""

import asyncio
import logging
from typing import Callable, Optional

from playwright.async_api import Dialog, FileChooser, Page

logger = logging.getLogger("browser.dialogs")


# ---------------------------------------------------------------------------
# Alert / Confirm / Prompt Handlers
# ---------------------------------------------------------------------------

def auto_accept(page: Page, prompt_text: str = "") -> None:
    """
    Register a one-time listener that automatically **accepts** the next dialog.

    For *alert*   — simply accepts.
    For *confirm* — clicks OK.
    For *prompt*  — types *prompt_text* and accepts.

    Args:
        page: Playwright Page to attach the handler to.
        prompt_text: Text to enter if the dialog is a prompt. Defaults to "".

    Usage::

        dialogs.auto_accept(page)
        await page.click("#trigger-alert-button")
    """
    async def _handler(dialog: Dialog) -> None:
        logger.debug(
            f"Auto-accepting dialog [{dialog.type}]: '{dialog.message[:80]}'"
        )
        await dialog.accept(prompt_text)

    page.once("dialog", _handler)
    logger.debug(f"Registered auto-accept dialog handler (prompt_text='{prompt_text}')")


def auto_dismiss(page: Page) -> None:
    """
    Register a one-time listener that automatically **dismisses** the next dialog.

    For *alert*   — accepts (alerts can only be accepted).
    For *confirm* — clicks Cancel.
    For *prompt*  — clicks Cancel.

    Args:
        page: Playwright Page to attach the handler to.

    Usage::

        dialogs.auto_dismiss(page)
        await page.click("#trigger-confirm-button")
    """
    async def _handler(dialog: Dialog) -> None:
        logger.debug(
            f"Auto-dismissing dialog [{dialog.type}]: '{dialog.message[:80]}'"
        )
        if dialog.type == "alert":
            await dialog.accept()   # alerts cannot be dismissed
        else:
            await dialog.dismiss()

    page.once("dialog", _handler)
    logger.debug("Registered auto-dismiss dialog handler")


async def capture_next_dialog(
    page: Page,
    trigger: Callable,
    timeout: float = 5.0,
) -> Optional[dict]:
    """
    Capture the message of the next dialog WITHOUT dismissing it.

    Returns dialog metadata (type, message, default_value) and then accepts it.

    Args:
        page: Playwright Page.
        trigger: An async callable that triggers the dialog.
        timeout: Seconds to wait for dialog.

    Returns:
        dict with keys 'type', 'message', 'default_value', or None on timeout.

    Usage::

        info = await dialogs.capture_next_dialog(
            page,
            lambda: page.click("#alert-btn")
        )
        print(info["message"])
    """
    captured: list = []

    async def _handler(dialog: Dialog) -> None:
        captured.append({
            "type": dialog.type,
            "message": dialog.message,
            "default_value": dialog.default_value,
        })
        await dialog.accept()

    page.once("dialog", _handler)

    try:
        await trigger()
    except Exception as exc:
        logger.warning(f"capture_next_dialog trigger raised: {exc}")

    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if captured:
            logger.debug(f"Captured dialog: {captured[0]}")
            return captured[0]
        await asyncio.sleep(0.2)

    logger.warning("capture_next_dialog timed out — no dialog appeared")
    return None


def accept_with_text(page: Page, text: str) -> None:
    """
    Register a one-time listener that accepts the next prompt dialog with *text*.

    Args:
        page: Playwright Page.
        text: Text to enter into the prompt.

    Usage::

        dialogs.accept_with_text(page, "my answer")
        await page.click("#prompt-btn")
    """
    auto_accept(page, prompt_text=text)


# ---------------------------------------------------------------------------
# File Chooser (Upload) Handler
# ---------------------------------------------------------------------------

async def handle_file_upload(
    page: Page,
    trigger: Callable,
    file_paths: list,
    timeout: float = 5.0,
) -> bool:
    """
    Handle a file chooser dialog that opens after *trigger* is called.

    Args:
        page: Playwright Page.
        trigger: An async callable that opens the file chooser (e.g. clicking "Upload").
        file_paths: List of absolute file paths to upload.
        timeout: Seconds to wait for the chooser.

    Returns:
        True on success, False on timeout or error.

    Usage::

        await dialogs.handle_file_upload(
            page,
            lambda: page.click("#upload-btn"),
            ["/tmp/myfile.pdf"]
        )
    """
    try:
        async with page.expect_file_chooser(timeout=int(timeout * 1000)) as fc_info:
            await trigger()
        file_chooser: FileChooser = await fc_info.value
        await file_chooser.set_files(file_paths)
        logger.debug(f"Uploaded files: {file_paths}")
        return True
    except Exception as exc:
        logger.error(f"handle_file_upload failed: {exc}")
        return False
