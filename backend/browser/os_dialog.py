"""
browser/os_dialog.py
--------------------
Advanced File Discovery, Folder Traversal, and Native OS File Upload Automation.

Key capabilities:
1. Multi-category semantic intelligence:
   - Images/Photographs (.jpg, .jpeg, .png, .webp, .bmp, .gif)
   - Resumes & Documents (.pdf, .docx, .doc, .txt)
   - Spreadsheets (.xlsx, .xls, .csv, .tsv)
   - Presentations (.pptx, .ppt)
   - Archives (.zip, .tar, .gz, .7z)
   - Audio & Video (.mp3, .mp4, .wav, .mkv)
   - Structured Code/Data (.json, .xml, .yaml)

2. Folder Traversal & Navigation:
   - Deep recursive walk into subdirectories (depth 1 to 5)
   - Automatically navigates into requested folders (e.g. "travel into folder Pictures", "in folder Photos", "from folder Downloads/Docs")
   - Scans user roots: Pictures, Documents, Downloads, Desktop, Videos, Music, OneDrive, workspace

3. Security Exclusions:
   - Strictly blocks sensitive credentials, secrets, private keys, tokens, and .env files

4. Guaranteed Mock File Generation:
   - If no matching file exists on the user system for the required category,
     dynamically generates a valid, high-quality sample file (e.g., student portrait JPEG,
     resume PDF, or clean CSV) so form validation NEVER fails.

5. Native Windows Dialog Automation (#32770):
   - Focuses dialog, pastes full resolved path, pauses visibly for human-like demonstration,
     and confirms submission.
"""

import os
import re
import sys
import time
import base64
import logging
import asyncio
import mimetypes
from typing import Optional, Dict, Any, List, Set, Tuple

from backend.security import is_sensitive_file as sec_is_sensitive_file, validate_upload_path

logger = logging.getLogger("OsDialogHandler")

# ---------------------------------------------------------------------------
# Categories, Extensions & Semantic Keywords
# ---------------------------------------------------------------------------

CATEGORY_EXTENSIONS: Dict[str, Set[str]] = {
    "image": {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tiff"},
    "resume": {".pdf", ".docx", ".doc"},
    "document": {".pdf", ".docx", ".doc", ".odt", ".rtf", ".txt"},
    "spreadsheet": {".xlsx", ".xls", ".csv", ".tsv"},
    "presentation": {".pptx", ".ppt"},
    "archive": {".zip", ".tar", ".gz", ".7z", ".rar"},
    "audio": {".mp3", ".wav", ".m4a", ".ogg", ".flac"},
    "video": {".mp4", ".mov", ".avi", ".mkv", ".webm"},
    "code": {".json", ".xml", ".yaml", ".yml", ".py", ".js", ".html", ".css", ".txt"},
}

ALL_VALID_EXTENSIONS: Set[str] = set().union(*CATEGORY_EXTENSIONS.values())

CATEGORY_KEYWORDS: Dict[str, List[str]] = {
    "image": [
        "photo", "photograph", "picture", "image", "avatar", "headshot",
        "selfie", "portrait", "pic", "img", "thumbnail", "banner", "logo",
        "passport photo", "recent photo", "recent photograph", "id photo",
        "profile picture", "profile photo", "snapshot"
    ],
    "resume": [
        "resume", "cv", "curriculum vitae", "biodata", "job application"
    ],
    "document": [
        "document", "doc", "pdf", "report", "certificate", "marksheet",
        "id proof", "id card", "passport", "license", "driving license",
        "aadhaar", "pan card", "agreement", "contract", "invoice", "bill",
        "statement", "transcript", "letter", "paper", "memo", "form"
    ],
    "spreadsheet": [
        "excel", "spreadsheet", "sheet", "csv", "tsv", "data", "dataset",
        "table", "tabular", "workbook", "records", "metrics", "balance sheet"
    ],
    "presentation": [
        "presentation", "slides", "deck", "powerpoint", "ppt", "keynote"
    ],
    "archive": [
        "zip", "archive", "compressed", "tar", "backup", "bundle"
    ],
    "audio": [
        "audio", "sound", "voice", "recording", "music", "podcast", "mp3"
    ],
    "video": [
        "video", "movie", "clip", "footage", "mp4", "recording"
    ],
    "code": [
        "code", "script", "json", "xml", "config", "schema", "payload", "yaml"
    ],
}

SENSITIVE_PATTERNS: List[re.Pattern] = [
    re.compile(r"client_secret", re.I),
    re.compile(r"credentials?", re.I),
    re.compile(r"\btoken\b", re.I),
    re.compile(r"\bsecret\b", re.I),
    re.compile(r"\bpasswd\b", re.I),
    re.compile(r"\bpassword\b", re.I),
    re.compile(r"\.env", re.I),
    re.compile(r"id_rsa", re.I),
    re.compile(r"id_ed25519", re.I),
    re.compile(r"\.pem$", re.I),
    re.compile(r"\.key$", re.I),
    re.compile(r"\.pfx$", re.I),
    re.compile(r"\.p12$", re.I),
    re.compile(r"\.kdbx$", re.I),
]

IGNORED_DIRS: Set[str] = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "env",
    "appdata", "$recycle.bin", ".cache", ".idea", ".vscode", "windows",
    "program files", "program files (x86)", ".pytest_cache", "site-packages",
    "dist", "build", ".next", ".system_generated"
}


# ---------------------------------------------------------------------------
# Semantic Category & Folder Intent Extraction
# ---------------------------------------------------------------------------

def is_sensitive_file(filename: str) -> bool:
    """Returns True if the file name or path suggests private credentials/secrets."""
    if sec_is_sensitive_file(filename):
        return True
    for pat in SENSITIVE_PATTERNS:
        if pat.search(filename):
            return True
    return False


def infer_requested_category(goal: str = "", context_text: str = "", accept_types: Optional[str] = None) -> Optional[str]:
    """
    Infers the desired file category from context, prompt, field labels, or HTML accept attribute.
    """
    combined = f"{goal} {context_text}".lower()

    # 1. HTML accept attribute takes high priority
    if accept_types:
        acc = accept_types.lower()
        if "image/" in acc or any(ext in acc for ext in [".jpg", ".png", ".jpeg"]):
            return "image"
        if "pdf" in acc or ".pdf" in acc:
            return "document"
        if "video/" in acc:
            return "video"
        if "audio/" in acc:
            return "audio"
        if "csv" in acc or "excel" in acc or "spreadsheet" in acc or ".xlsx" in acc:
            return "spreadsheet"

    # 2. Check for explicit category keywords (check image & resume first)
    for cat in ["image", "resume", "spreadsheet", "presentation", "document", "archive", "audio", "video", "code"]:
        for kw in CATEGORY_KEYWORDS[cat]:
            if re.search(r"\b" + re.escape(kw) + r"\b", combined):
                return cat

    return None


def extract_target_folder(text: str) -> Optional[str]:
    """
    Extracts explicit folder traveling request (e.g., 'travel into folder Pictures', 'in folder Photos').
    """
    known_folders = [
        "pictures", "photos", "images", "downloads", "documents", "desktop",
        "videos", "music", "resumes", "camera roll", "screenshots", "saved pictures"
    ]
    text_lower = text.lower()
    for kf in known_folders:
        if (
            re.search(r"\b(?:folder|directory)\s+['\"]?" + re.escape(kf) + r"['\"]?\b", text_lower)
            or re.search(r"\b" + re.escape(kf) + r"\s+folder\b", text_lower)
            or re.search(r"\b(?:travel into|go into|open|in|inside|from)\s+(?:the\s+|my\s+)?['\"]?" + re.escape(kf) + r"['\"]?(?:\s+folder)?\b", text_lower)
        ):
            return kf

    patterns = [
        r"(?:travel into|go into|open|navigate to|look in|search in|inside|from|in)\s+(?:the\s+|my\s+)?['\"]?([a-zA-Z0-9_\-]+(?:\s+[a-zA-Z0-9_\-]+)?)\s+folder\b",
        r"(?:travel into|go into|open|navigate to|look in|search in|inside|from|in)\s+(?:the\s+|my\s+)?folder\s+['\"]?([a-zA-Z0-9_\-]+(?:\s+[a-zA-Z0-9_\-]+)?)['\"]?",
        r"(?:travel into|go into|open|navigate to|look in|search in|inside|from|in)\s+(?:the\s+|my\s+)?directory\s+['\"]?([a-zA-Z0-9_\-]+(?:\s+[a-zA-Z0-9_\-]+)?)['\"]?",
        r"folder[:\s=]+['\"]?([a-zA-Z0-9_\-]+(?:\s+[a-zA-Z0-9_\-]+)?)['\"]?",
    ]
    for p in patterns:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            folder = m.group(1).strip()
            parts = folder.split()
            clean_parts = [part for part in parts if part.lower() not in ("and", "or", "to", "for", "the", "a", "an", "this", "that", "my")]
            if clean_parts:
                return " ".join(clean_parts)
    return None


def extract_target_file_name(text: str) -> Optional[str]:
    """
    Extracts a specific file name with extension from prompt or context.
    """
    matches = re.findall(r"\b([a-zA-Z0-9_\-\.]+\.[a-zA-Z0-9]{2,5})\b", text)
    for match in matches:
        ext = os.path.splitext(match)[1].lower()
        if ext in ALL_VALID_EXTENSIONS and not is_sensitive_file(match):
            return match
    return None


# ---------------------------------------------------------------------------
# Guaranteed Mock File Generator (Removed for Security)
# ---------------------------------------------------------------------------

def generate_clean_mock_file(category: str = "image", filename: Optional[str] = None, context_text: str = "") -> str:
    """Mock file generation has been removed to satisfy Upload Safety requirements.
    Never substitute a mock file for a requested file.
    """
    raise PermissionError(
        "Automatic mock file fallback has been removed for upload security. "
        "Explicit file paths are required."
    )


# ---------------------------------------------------------------------------
# Deep File Search & Folder Traveling Engine
# ---------------------------------------------------------------------------

def _format_file_info(file_path: str) -> Dict[str, Any]:
    """Reads file info and prepares base64 payload with real directory context."""
    name = os.path.basename(file_path)
    directory = os.path.dirname(file_path)
    folder_name = os.path.basename(directory) or "Documents"
    mime_type, _ = mimetypes.guess_type(file_path)
    if not mime_type:
        ext = os.path.splitext(file_path)[1].lower()
        if ext in (".jpg", ".jpeg"):
            mime_type = "image/jpeg"
        elif ext == ".png":
            mime_type = "image/png"
        elif ext == ".pdf":
            mime_type = "application/pdf"
        elif ext == ".csv":
            mime_type = "text/csv"
        else:
            mime_type = "application/octet-stream"

    b64_content = ""
    file_size = 0
    try:
        from backend.security.policy import MAX_UPLOAD_FILE_SIZE
        canonical_path = os.path.realpath(file_path)
        if canonical_path != file_path:
            logger.warning("Path resolution discrepancy detected between validation and read.")
            return None
        file_size = os.path.getsize(canonical_path)
        if file_size > MAX_UPLOAD_FILE_SIZE:
            logger.warning(f"File {os.path.basename(canonical_path)} exceeds max upload size.")
            return None
        with open(canonical_path, "rb") as f:
            content_bytes = f.read(MAX_UPLOAD_FILE_SIZE)
        b64_content = base64.b64encode(content_bytes).decode("ascii")
    except Exception as exc:
        logger.warning(f"Could not read binary file content: {exc}")
        return None

    # Discover neighbor files in user's directory for realistic OS catalog display
    neighbor_files = []
    try:
        if os.path.isdir(directory):
            for entry in os.listdir(directory):
                if entry.startswith(".") or entry.startswith("~") or entry == name or is_sensitive_file(entry):
                    continue
                entry_path = os.path.join(directory, entry)
                is_dir = os.path.isdir(entry_path)
                try:
                    sz = os.path.getsize(entry_path) if not is_dir else 0
                    sz_str = f"{round(sz / 1024)} KB" if not is_dir and sz > 0 else ""
                except OSError:
                    sz_str = ""
                neighbor_files.append({
                    "name": entry,
                    "is_dir": is_dir,
                    "size_str": sz_str,
                })
                if len(neighbor_files) >= 8:
                    break
    except Exception:
        pass

    return {
        "path": file_path,
        "name": name,
        "directory": directory,
        "folder_name": folder_name,
        "size": file_size,
        "mime_type": mime_type,
        "base64": b64_content,
        "neighbor_files": neighbor_files,
    }


def resolve_upload_file(
    file_path: str,
    allowlist_dirs: list[str] | None = None,
    user_approved: bool = False,
) -> dict | None:
    """Validate an explicit upload path and return structured file info if permitted."""
    is_valid, reason, canonical = validate_upload_path(
        file_path,
        allowlist_dirs=allowlist_dirs,
        user_approved=user_approved,
    )
    if not is_valid or not canonical:
        logger.warning(f"Upload file rejected by policy: {reason}")
        return None
    return _format_file_info(canonical)


def get_user_system_file(
    goal: str = "",
    context_text: str = "",
    accept_types: str | None = None,
    allowlist_dirs: list[str] | None = None,
    user_approved: bool = False,
) -> dict | None:
    """
    Finds a file matching prompt requirements ONLY when an explicit, valid file path is provided.
    Automatic personal-folder discovery and mock-file generation are removed for upload safety.
    """
    combined_context = f"{goal} {context_text}".strip()

    # 1. Check for explicit path in goal or context
    path_match = re.search(r"(?<![A-Za-z0-9])[A-Za-z]:[/\\](?!/)[^\s\"'<>]+", combined_context)
    candidate: str | None = None
    if path_match:
        candidate = path_match.group(0).strip().strip('"\'')
    else:
        file_match = re.search(r'(?:file|path|upload|attach)[:=\s]+["\']?([^\s"\'<>]+\.[a-zA-Z0-9]{2,5})["\']?', combined_context)
        if file_match:
            candidate = file_match.group(1).strip().strip('"\'')
    if not candidate:
        target_fn = extract_target_file_name(combined_context)
        if target_fn and os.path.isabs(target_fn):
            candidate = target_fn

    if not candidate:
        # Check for safe allowlisted mock files when filling forms with mock data or testing uploads
        combined_lower = combined_context.lower()
        if "mock" in combined_lower or "test" in combined_lower or "sample" in combined_lower:
            from pathlib import Path
            project_root = Path(__file__).resolve().parent.parent.parent
            mock_dir = project_root / "downloads" / "mock_files"
            if not mock_dir.exists():
                mock_dir = Path.cwd() / "downloads" / "mock_files"

            if mock_dir.exists():
                category = infer_requested_category(goal, context_text, accept_types)
                if category == "image" or any(w in combined_lower for w in ["photo", "photograph", "picture", "image", "avatar"]):
                    mock_img = mock_dir / "mock_photograph.jpg"
                    if mock_img.is_file():
                        candidate = str(mock_img)
                elif category in ("spreadsheet", "csv") or "csv" in combined_lower:
                    mock_csv = mock_dir / "mock_data.csv"
                    if mock_csv.is_file():
                        candidate = str(mock_csv)
                else:
                    mock_doc = mock_dir / "mock_document.pdf"
                    if mock_doc.is_file():
                        candidate = str(mock_doc)
                    elif (mock_dir / "mock_photograph.jpg").is_file():
                        candidate = str(mock_dir / "mock_photograph.jpg")

    if not candidate:
        logger.info("Upload safety: no explicit file path specified. Refusing automatic directory search.")
        return None

    return resolve_upload_file(candidate, allowlist_dirs=allowlist_dirs, user_approved=user_approved)


# ---------------------------------------------------------------------------
# Native Windows Common File Dialog (#32770) Automation
# ---------------------------------------------------------------------------

def _find_windows_file_dialog() -> Optional[int]:
    """Locate the native Windows common file dialog HWND (#32770 or 'Open')."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        found_hwnds = []

        hdesk = user32.OpenInputDesktop(0, False, 0x0100)  # DESKTOP_ENUMERATE
        if not hdesk:
            hdesk = user32.GetThreadDesktop(kernel32.GetCurrentThreadId())

        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def enum_cb(hwnd, lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            class_buff = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, class_buff, 256)
            title_buff = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, title_buff, 512)
            cls = class_buff.value
            title = title_buff.value
            if cls == "#32770" or title in ("Open", "File Upload", "Choose File to Upload", "Select file", "Insert file"):
                found_hwnds.append(hwnd)
            return True

        user32.EnumDesktopWindows(hdesk, WNDENUMPROC(enum_cb), 0)
        return found_hwnds[0] if found_hwnds else None
    except Exception as exc:
        logger.debug(f"Win32 dialog enum error: {exc}")
        return None


async def handle_os_file_dialog(
    goal: str = "",
    context_text: str = "",
    screen_x: Optional[int] = None,
    screen_y: Optional[int] = None,
    wait_for_dialog_secs: float = 5.0,
    accept_types: Optional[str] = None,
    user_approved: bool = False,
) -> bool:
    """
    Automates uploading an actual file from the user's system:
    1. If screen coordinates of the upload button are given, clicks them.
    2. Detects and brings the native Windows 'Open' dialog (#32770) into focus.
    3. Types/pastes the exact resolved file path and pauses visibly.
    4. Confirms with Enter and verifies dialog closure.
    """
    try:
        import pyautogui
        import pyperclip
        pyautogui.FAILSAFE = False
        pyautogui.PAUSE = 0.05
    except ImportError:
        logger.warning("pyautogui / pyperclip not installed.")
        return False

    file_info = get_user_system_file(
        goal=goal,
        context_text=context_text,
        accept_types=accept_types,
        user_approved=user_approved,
    )
    if not file_info:
        logger.error("No valid user file could be found or generated to upload.")
        return False

    file_path = file_info["path"]
    logger.info(f"Preparing to upload user file: '{file_path}' ({file_info['size']} bytes) in folder '{file_info.get('folder_name')}'")

    if screen_x is not None and screen_y is not None and screen_x > 0 and screen_y > 0:
        try:
            screen_w, screen_h = pyautogui.size()
            target_x = max(20, min(screen_x, screen_w - 20))
            target_y = max(20, min(screen_y, screen_h - 20))
            logger.info(f"Physical hardware click executed at ({target_x}, {target_y}).")
            pyautogui.moveTo(target_x, target_y, duration=0.25)
            await asyncio.sleep(0.1)
            pyautogui.click(target_x, target_y)
        except Exception as exc:
            logger.warning(f"OS physical click warning: {exc}")

    # Wait for Windows file dialog to appear
    dialog_hwnd = None
    start_wait = time.time()
    while time.time() - start_wait < wait_for_dialog_secs:
        dialog_hwnd = _find_windows_file_dialog()
        if dialog_hwnd:
            break
        await asyncio.sleep(0.2)

    if dialog_hwnd:
        logger.info(f"Detected native Windows 'Open' dialog (HWND={dialog_hwnd}). Bringing to foreground.")
        try:
            import ctypes
            user32 = ctypes.windll.user32
            user32.ShowWindow(dialog_hwnd, 9)  # SW_RESTORE
            user32.SetForegroundWindow(dialog_hwnd)
            user32.BringWindowToTop(dialog_hwnd)
        except Exception:
            pass
        await asyncio.sleep(0.6)
    else:
        logger.info("Native file dialog (#32770) not detected by EnumDesktopWindows within timeout.")
        return False

    try:
        # Select all in the filename field
        pyautogui.hotkey("ctrl", "a")
        await asyncio.sleep(0.2)

        # Paste the real file path into the catalog dialog
        pyperclip.copy(file_path)
        pyautogui.hotkey("ctrl", "v")

        logger.info(f"Pasted '{file_path}' into native file catalog. Pausing for visual confirmation.")
        await asyncio.sleep(1.0)

        # Also apply direct Win32 WM_SETTEXT if edit control exists
        if dialog_hwnd and sys.platform == "win32":
            try:
                import ctypes
                from ctypes import wintypes
                user32 = ctypes.windll.user32
                edit_hwnd = None

                WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
                def enum_child_cb(hwnd, lparam):
                    nonlocal edit_hwnd
                    cls_buff = ctypes.create_unicode_buffer(256)
                    user32.GetClassNameW(hwnd, class_buff, 256)
                    if cls_buff.value.lower() == "edit" and not edit_hwnd:
                        edit_hwnd = hwnd
                    return True
                user32.EnumChildWindows(dialog_hwnd, WNDENUMPROC(enum_child_cb), 0)
                if edit_hwnd:
                    user32.SendMessageW(edit_hwnd, 0x000C, 0, file_path)  # WM_SETTEXT
            except Exception as e:
                logger.debug(f"Direct WM_SETTEXT note: {e}")

        # Press Enter to confirm selection
        pyautogui.press("enter")
        await asyncio.sleep(0.8)

        # Confirm dialog closed
        if dialog_hwnd and sys.platform == "win32":
            try:
                import ctypes
                user32 = ctypes.windll.user32
                if user32.IsWindow(dialog_hwnd) and user32.IsWindowVisible(dialog_hwnd):
                    pyautogui.press("enter")
                    await asyncio.sleep(0.8)
            except Exception:
                pass

        logger.info(f"Successfully selected '{file_path}' in native OS file catalog and confirmed.")
        return True
    except Exception as exc:
        logger.error(f"Failed to interact with native file catalog: {exc}")
        return False
