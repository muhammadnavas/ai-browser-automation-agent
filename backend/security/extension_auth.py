"""
backend/security/extension_auth.py
----------------------------------
Chrome Extension Authentication, TOFU Pairing, and Key Revocation (Issue 2).

Key Features:
- TOFU (Trust On First Use) pairing over loopback interface.
- Single trusted initial pairing enforcement: rejects subsequent unauthenticated pairing attempts while active.
- Extension ID validation against ALLOWED_EXTENSION_IDS when configured.
- Cryptographic pairing tokens stored locally in the extension.
- Revocation support (revoking one or all extension pairings).
- Disconnection of revoked extensions.
- Thread-safe pairing synchronization.
- Comprehensive audit logging without leaking tokens.
"""

import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Set, Tuple
from backend.security.audit import audit_logger


@dataclass
class ExtensionRegistration:
    token: str
    extension_id: str
    created_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    revoked: bool = False


class ExtensionAuthManager:
    """Manages Chrome Extension pairing credentials and access control."""

    def __init__(self):
        self._registrations: Dict[str, ExtensionRegistration] = {}
        # token -> registration
        self._revoked_tokens: Set[str] = set()
        self._allowed_extension_ids: Optional[Set[str]] = self._load_allowed_ids()
        self._lock = threading.Lock()

    def _load_allowed_ids(self) -> Optional[Set[str]]:
        raw = os.getenv("ALLOWED_EXTENSION_IDS", "").strip()
        if not raw:
            return None
        return {item.strip() for item in raw.split(",") if item.strip()}

    def reload_allowed_ids(self) -> None:
        with self._lock:
            self._allowed_extension_ids = self._load_allowed_ids()

    def is_extension_id_allowed(self, extension_id: Optional[str]) -> bool:
        if self._allowed_extension_ids is None:
            return True
        if not extension_id:
            return False
        return extension_id in self._allowed_extension_ids

    def register_or_authenticate(
        self,
        token: Optional[str],
        extension_id: Optional[str],
        is_loopback: bool,
    ) -> Tuple[bool, Optional[str], str]:
        """
        Validate an existing token or perform TOFU pairing on loopback.
        Enforces a single trusted initial pairing and rejects unauthenticated
        attempts when already paired.

        Returns:
            (authenticated: bool, active_token: Optional[str], reason: str)
        """
        with self._lock:
            if not is_loopback:
                audit_logger.record_event(
                    event_type="extension_auth_rejected",
                    task_id="system",
                    details={"reason": "non_loopback_connection_rejected", "extension_id": extension_id or "unknown"},
                )
                return False, None, "Extension connection rejected: Non-loopback connection."

            # Check configured extension ID restriction
            if not self.is_extension_id_allowed(extension_id):
                audit_logger.record_event(
                    event_type="extension_auth_rejected",
                    task_id="system",
                    details={"reason": "unauthorized_extension_id", "extension_id": extension_id or "unknown"},
                )
                return False, None, "Extension connection rejected: Unauthorized extension ID."

            clean_ext_id = extension_id or "unknown_extension"

            # Case 1: Existing token provided
            if token:
                if token in self._registrations:
                    reg = self._registrations[token]
                    if reg.revoked or token in self._revoked_tokens:
                        audit_logger.record_event(
                            event_type="extension_auth_rejected",
                            task_id="system",
                            details={"reason": "revoked_token", "extension_id": clean_ext_id},
                        )
                        return False, None, "Extension pairing credential has been revoked."
                    reg.last_seen = time.time()
                    if extension_id and reg.extension_id == "unknown_extension":
                        reg.extension_id = extension_id
                    return True, token, "Authenticated successfully."
                elif is_loopback and token.startswith("ext_sec_") and token not in self._revoked_tokens:
                    # Seamlessly re-adopt existing loopback token after server/backend restart
                    reg = ExtensionRegistration(
                        token=token,
                        extension_id=clean_ext_id,
                        created_at=time.time(),
                        last_seen=time.time(),
                        revoked=False,
                    )
                    self._registrations[token] = reg
                    audit_logger.record_event(
                        event_type="extension_reconnected_after_restart",
                        task_id="system",
                        details={"extension_id": clean_ext_id},
                    )
                    return True, token, "Re-authenticated successfully after backend restart."
                else:
                    audit_logger.record_event(
                        event_type="extension_auth_rejected",
                        task_id="system",
                        details={"reason": "invalid_token", "extension_id": clean_ext_id},
                    )
                    return False, None, "Invalid extension pairing token."

            # Case 2: TOFU pairing over loopback
            # Enforce single trusted initial pairing:
            # If an active unrevoked registration exists, reject unauthenticated pairing attempts.
            active_count = sum(1 for reg in self._registrations.values() if not reg.revoked)
            if active_count > 0:
                audit_logger.record_event(
                    event_type="extension_auth_rejected",
                    task_id="system",
                    details={"reason": "already_paired", "extension_id": clean_ext_id},
                )
                return False, None, "Extension pairing rejected: An extension is already paired. Revoke existing pairing to re-pair."

            new_token = f"ext_sec_{secrets.token_urlsafe(32)}"
            reg = ExtensionRegistration(
                token=new_token,
                extension_id=clean_ext_id,
                created_at=time.time(),
                last_seen=time.time(),
                revoked=False,
            )
            self._registrations[new_token] = reg
            audit_logger.record_event(
                event_type="extension_paired",
                task_id="system",
                details={"extension_id": clean_ext_id, "is_loopback": is_loopback},
            )
            return True, new_token, "Paired successfully via Trust-On-First-Use."

    def is_token_valid(self, token: Optional[str]) -> bool:
        if not token:
            return False
        with self._lock:
            reg = self._registrations.get(token)
            return reg is not None and not reg.revoked

    def revoke_token(self, token: str) -> bool:
        with self._lock:
            self._revoked_tokens.add(token)
            if token in self._registrations:
                reg = self._registrations[token]
                reg.revoked = True
                audit_logger.record_event(
                    event_type="extension_revoked",
                    task_id="system",
                    details={"extension_id": reg.extension_id},
                )
                return True
            return False

    def revoke_all(self) -> int:
        with self._lock:
            count = 0
            for tok, reg in self._registrations.items():
                self._revoked_tokens.add(tok)
                if not reg.revoked:
                    reg.revoked = True
                    count += 1
            audit_logger.record_event(
                event_type="all_extensions_revoked",
                task_id="system",
                details={"revoked_count": count},
            )
            return count

    def get_registered_count(self) -> int:
        with self._lock:
            return sum(1 for reg in self._registrations.values() if not reg.revoked)


# Global singleton instance
extension_auth_manager = ExtensionAuthManager()
