"""
browser/downloader.py
---------------------
File download management for Playwright.

Playwright must have `accept_downloads=True` set on the BrowserContext
(the updated BrowserController handles this automatically).
"""

import logging
import os
from typing import Callable, Coroutine, List, Optional, Any

from playwright.async_api import Download, Page

logger = logging.getLogger("browser.downloader")

class DownloadManager:
    """
    Manages file downloads triggered by browser interactions.

    Usage::

        dm = DownloadManager(save_dir="downloads")
        path = await dm.start_download(
            page,
            lambda: page.click("#download-btn")
        )
        print(f"Downloaded to: {path}")
    """

    def __init__(self, save_dir: str = "downloads"):
        """
        Args:
            save_dir: Directory where downloaded files will be saved.
        """
        self.save_dir = save_dir
        os.makedirs(save_dir, exist_ok=True)

    # ------------------------------------------------------------------
    # Core download helpers
    # ------------------------------------------------------------------

    async def start_download(
        self,
        page: Page,
        trigger: Callable[[], Coroutine[Any, Any, None]],
        filename: Optional[str] = None,
        timeout: int = 60000,
    ) -> Optional[str]:
        """
        Trigger a file download and wait for it to complete.

        Args:
            page: Playwright Page (context must have accept_downloads=True).
            trigger: Async callable that triggers the download (e.g. button click).
            filename: Override the saved filename. Uses the suggested name if None.
            timeout: Max wait in ms for download to start.

        Returns:
            Absolute path to the saved file, or None on failure.

        Usage::

            path = await dm.start_download(
                page,
                lambda: page.click("#csv-export"),
                filename="export.csv"
            )
        """
        try:
            async with page.expect_download(timeout=timeout) as download_info:
                await trigger()

            download: Download = await download_info.value

            # Determine save path
            suggested = download.suggested_filename
            save_name = filename or suggested or "download"
            save_path = os.path.join(self.save_dir, save_name)

            # Avoid overwriting by appending a counter
            save_path = self._unique_path(save_path)

            await download.save_as(save_path)
            logger.info(f"Download complete: '{save_path}'")
            return save_path

        except Exception as exc:
            logger.error(f"start_download failed: {exc}")
            return None

    async def wait_for_download(
        self,
        page: Page,
        trigger: Callable[[], Coroutine[Any, Any, None]],
        timeout: int = 60000,
    ) -> Optional[Download]:
        """
        Wait for a download event without saving immediately.
        Returns the raw Playwright `Download` object for custom handling.

        Args:
            page: Playwright Page.
            trigger: Async callable that triggers the download.
            timeout: Max wait in ms.

        Returns:
            Playwright Download object, or None on failure.
        """
        try:
            async with page.expect_download(timeout=timeout) as download_info:
                await trigger()
            download = await download_info.value
            logger.debug(f"Download started: '{download.suggested_filename}'")
            return download
        except Exception as exc:
            logger.error(f"wait_for_download failed: {exc}")
            return None

    async def save_download(
        self,
        download: Download,
        filename: Optional[str] = None,
    ) -> Optional[str]:
        """
        Save a previously captured Download object to *save_dir*.

        Args:
            download: Playwright Download object.
            filename: Override filename. Uses suggested name if None.

        Returns:
            Absolute path to saved file, or None on failure.
        """
        try:
            name = filename or download.suggested_filename or "download"
            path = self._unique_path(os.path.join(self.save_dir, name))
            await download.save_as(path)
            logger.info(f"Saved download to: '{path}'")
            return path
        except Exception as exc:
            logger.error(f"save_download failed: {exc}")
            return None

    # ------------------------------------------------------------------
    # File listing
    # ------------------------------------------------------------------

    def list_downloads(self) -> List[str]:
        """
        Return a list of filenames in the download directory.

        Returns:
            List of filename strings (not full paths).
        """
        try:
            return [
                f for f in os.listdir(self.save_dir)
                if os.path.isfile(os.path.join(self.save_dir, f))
            ]
        except Exception as exc:
            logger.error(f"list_downloads failed: {exc}")
            return []

    def get_download_path(self, filename: str) -> str:
        """
        Return the full absolute path for a filename in the download directory.

        Args:
            filename: The filename (not full path).

        Returns:
            Absolute file path string.
        """
        return os.path.abspath(os.path.join(self.save_dir, filename))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _unique_path(self, path: str) -> str:
        """Return a unique file path by appending a counter if path exists."""
        if not os.path.exists(path):
            return path
        base, ext = os.path.splitext(path)
        counter = 1
        while True:
            candidate = f"{base}_{counter}{ext}"
            if not os.path.exists(candidate):
                return candidate
            counter += 1
