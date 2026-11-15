"""Per-task, human-readable execution logs with structured raw artifacts.

Sensitive credential fields are redacted before persistence. DOM structure, visible
text, plans, decisions, action payloads and outcomes are otherwise retained.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SECRET_KEY = re.compile(r"(password|passwd|token|secret|authorization|cookie|api[_-]?key|credential)", re.I)
_SENSITIVE_INPUT_TYPES = {"password"}
_REDACTED = "[REDACTED]"


def _safe(value: Any, key: str = "") -> Any:
    """Convert arbitrary runtime objects to JSON-safe values and mask credentials."""
    if _SECRET_KEY.search(str(key or "")):
        return _REDACTED
    if isinstance(value, dict):
        result = {}
        input_type = str(value.get("type") or value.get("input_type") or value.get("field_type") or "").lower()
        for child_key, child_value in value.items():
            if input_type in _SENSITIVE_INPUT_TYPES and str(child_key).lower() in {"value", "current_value", "text"}:
                result[str(child_key)] = _REDACTED
            else:
                result[str(child_key)] = _safe(child_value, str(child_key))
        return result
    if isinstance(value, (list, tuple, set)):
        return [_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "model_dump"):
        return _safe(value.model_dump(mode="json"))
    if hasattr(value, "dict") and callable(value.dict):
        try:
            return _safe(value.dict())
        except Exception:
            pass
    if hasattr(value, "value") and isinstance(value.value, (str, int, float)):
        return value.value
    return repr(value)


class TaskFileLogger:
    """Writes append-only task.log/events.jsonl plus full per-step artifacts."""

    def __init__(self, task_id: str, root: str | os.PathLike[str] | None = None):
        safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", str(task_id))[:160] or "unknown"
        self.task_id = safe_id
        log_root = Path(root).resolve() if root is not None else Path(__file__).resolve().parents[2] / "logs"
        self.directory = log_root / safe_id
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._log_path = self.directory / "task.log"
        self._events_path = self.directory / "events.jsonl"
        self.event("logger_initialized", {"task_id": task_id, "directory": str(self.directory)})

    def _write_artifact(self, relative_path: str, payload: Any) -> str:
        path = self.directory / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        safe_payload = _safe(payload)
        with self._lock:
            path.write_text(json.dumps(safe_payload, ensure_ascii=False, indent=2, default=repr) + "\n", encoding="utf-8")
        return str(path.relative_to(self.directory))

    def event(self, event_type: str, data: Any = None, *, step: int | None = None) -> None:
        now = datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")
        record = {"timestamp": now, "task_id": self.task_id, "event": event_type, "step": step, "data": _safe(data or {})}
        encoded = json.dumps(record, ensure_ascii=False, default=repr)
        with self._lock:
            with self._events_path.open("a", encoding="utf-8") as stream:
                stream.write(encoded + "\n")
            summary = json.dumps(record["data"], ensure_ascii=False, indent=2, default=repr)
            with self._log_path.open("a", encoding="utf-8") as stream:
                stream.write(f"\n{'=' * 88}\n[{now}] {event_type.upper()} | step={step if step is not None else '-'}\n")
                stream.write(summary[:200000] + "\n")

    def artifact(self, relative_path: str, payload: Any) -> None:
        self._write_artifact(relative_path, payload)

    def dom_snapshot(self, step: int, page_info: Any) -> None:
        path = self._write_artifact(f"dom/step_{step:04d}.json", page_info)
        self.event("dom_snapshot", {"artifact": path, "url": (page_info or {}).get("url") if isinstance(page_info, dict) else None}, step=step)

    def plan(self, version: int, plan: Any, metadata: Any = None) -> None:
        path = self._write_artifact(f"plans/v{version:02d}.json", {"version": version, "plan": plan, "metadata": metadata})
        self.event("plan_version", {"version": version, "artifact": path, "plan": plan, "metadata": metadata})

    def action(self, step: int, action: Any, *, phase: str, result: Any = None) -> None:
        self.event(f"action_{phase}", {"action": action, "result": result}, step=step)

    def close(self, status: str, details: Any = None) -> None:
        self.event("task_finished", {"status": status, "details": details})
