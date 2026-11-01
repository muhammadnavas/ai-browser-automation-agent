"""
browser/__init__.py
-------------------
Public API surface for the browser automation module.

Import the main controller and sub-modules from here::

    from browser import BrowserController
    from browser import actions, tabs, dialogs, waiter, downloader, session

Or import sub-module functions directly::

    from browser.actions import fill_field, select_option
    from browser.waiter import wait_for_selector, wait_for_text
    from browser.dialogs import auto_accept
    from browser.tabs import TabManager
    from browser.downloader import DownloadManager
    from browser.session import SessionManager
    from browser.extractor import InfoExtractor
    from browser.form_discovery import FormDiscovery
"""

from browser.controller import BrowserController
from browser.extractor import InfoExtractor
from browser.form_discovery import FormDiscovery
from browser import actions
from browser import tabs
from browser import dialogs
from browser import waiter
from browser import downloader
from browser import session

__all__ = [
    "BrowserController",
    "InfoExtractor",
    "FormDiscovery",
    "actions",
    "tabs",
    "dialogs",
    "waiter",
    "downloader",
    "session",
]
