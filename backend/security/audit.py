"""
backend/security/audit.py
-------------------------
Shared security audit module that records security events without
recording raw sensitive values, vault contents, or private contact values.
"""

from dataclasses import dataclass, field
import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("security.audit")


@dataclass
class SecurityAuditEvent:
    event_type: str
    task_id: str
    timestamp: float = field(default_factory=time.time)
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        from dataclasses import asdict
        return asdict(self)

    def model_dump(self) -> Dict[str, Any]:
        from dataclasses import asdict
        return asdict(self)


class SecurityAuditLogger:
    """In-memory and structured logging for security events."""

    def __init__(self, max_events: int = 1000):
        self.max_events = max_events
        self.events: List[SecurityAuditEvent] = []

    def record_event(
        self,
        event_type: str,
        task_id: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> SecurityAuditEvent:
        """Record a sanitized security audit event."""
        from backend.security.redactor import redact_text, redact_sensitive_data
        from backend.security.detector import is_tier1_key, is_tier2_key

        clean_details = dict(details or {})
        sanitized_details = {}
        for k, v in clean_details.items():
            k_str = redact_text(str(k), mask_tier3=True)
            # If the key itself is a sensitive credential / secret key
            if is_tier1_key(k_str) or is_tier2_key(k_str) or any(
                t in k_str.lower() for t in ("password", "secret", "token", "key", "credential", "vault")
            ):
                sanitized_details[k_str] = "[REDACTED]"
            elif isinstance(v, (dict, list, tuple, set)):
                sanitized_details[k_str] = redact_sensitive_data(v, mask_tier3=True)
            elif isinstance(v, (int, float, bool)):
                sanitized_details[k_str] = v
            else:
                # Stringify arbitrary objects, exceptions, and strings
                v_str = str(v)
                # If key suggests filename or path, strip path to basename first to avoid leaking directory structures
                if any(p in k_str.lower() for p in ("path", "file", "dir")) and ("/" in v_str or "\\" in v_str):
                    v_str = v_str.replace("\\", "/").split("/")[-1]
                # Redact any passwords, secrets, tokens, government IDs, emails, phone numbers
                sanitized_str = redact_text(v_str, mask_tier3=True)
                sanitized_details[k_str] = sanitized_str[:500]

        import re
        safe_task_id = redact_text(str(task_id or ""), mask_tier3=True)
        safe_event_type = redact_text(str(event_type or ""), mask_tier3=True)
        safe_event_type = re.sub(r'(?i)[a-z0-9_-]*(?:pass(?:word)?|secret|token|api_?key)[a-z0-9_-]*[0-9]{4,}', '[REDACTED]', safe_event_type)

        event = SecurityAuditEvent(
            event_type=safe_event_type,
            task_id=safe_task_id,
            timestamp=time.time(),
            details=sanitized_details,
        )
        self.events.append(event)
        if len(self.events) > self.max_events:
            self.events = self.events[-self.max_events:]

        logger.info(
            "SECURITY_AUDIT: event=%s task=%s details=%s",
            safe_event_type,
            safe_task_id,
            sanitized_details,
        )
        return event

    def log_sensitive_detected(
        self, task_id: str, tier: str, description: str
    ) -> SecurityAuditEvent:
        return self.record_event(
            event_type="sensitive_information_detected",
            task_id=task_id,
            details={"tier": tier, "description": description},
        )

    def log_data_redacted(
        self, task_id: str, target: str, count: int = 1
    ) -> SecurityAuditEvent:
        return self.record_event(
            event_type="data_redacted",
            task_id=task_id,
            details={"target": target, "count": count},
        )

    def log_secret_blocked(
        self, task_id: str, reason: str
    ) -> SecurityAuditEvent:
        return self.record_event(
            event_type="secret_containing_goal_blocked",
            task_id=task_id,
            details={"reason": reason},
        )

    def log_extraction_refused(
        self, task_id: str, reason: str, ratio: float
    ) -> SecurityAuditEvent:
        return self.record_event(
            event_type="extraction_refused",
            task_id=task_id,
            details={"reason": reason, "sensitive_ratio": f"{ratio:.2%}"},
        )

    def log_file_rejected(
        self, task_id: str, filename: str, reason: str
    ) -> SecurityAuditEvent:
        # Strip path directory to avoid leaking user home directory paths
        safe_filename = filename.replace("\\", "/").split("/")[-1] if filename else "unknown"
        return self.record_event(
            event_type="sensitive_file_rejected",
            task_id=task_id,
            details={"filename": safe_filename, "reason": reason},
        )

    def log_upload_event(
        self,
        task_id: str,
        file_path: str,
        approved: bool,
        reason: str,
    ) -> SecurityAuditEvent:
        safe_name = file_path.replace("\\", "/").split("/")[-1] if file_path else "unknown"
        return self.record_event(
            event_type="upload_approved" if approved else "upload_blocked",
            task_id=task_id,
            details={"filename": safe_name, "approved": approved, "reason": reason},
        )

    def log_auth_event(
        self, task_id: str, action: str, reason: str = ""
    ) -> SecurityAuditEvent:
        return self.record_event(
            event_type=f"user_assisted_auth_{action}",
            task_id=task_id,
            details={"action": action, "reason": reason},
        )

    def get_events_for_task(self, task_id: str) -> List[SecurityAuditEvent]:
        return [e for e in self.events if e.task_id == task_id]

    def clear(self) -> None:
        self.events.clear()


# Global singleton audit logger
audit_logger = SecurityAuditLogger()
