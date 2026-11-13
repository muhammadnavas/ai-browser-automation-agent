"""
backend/security/extension_auth.py
----------------------------------
Chrome Extension Authentication, TOFU Pairing, and Key Revocation (Issue 2).

Key Features:
- TOFU (Trust On First Use) pairing over loopback interface.
- Single trusted initial pairing enforcement: rejects subsequent unauthenticated pairing attempts while active.
- Extension ID validation against ALLOWED_EXTENSION_IDS when configured.
- Cryptographic pairing tokens stored locally in the extension and persisted securely on backend.
- Revocation support (revoking one or all extension pairings).
- Disconnection of revoked extensions.
- Thread-safe pairing synchronization.
- Comprehensive audit logging without leaking tokens.
"""

import os
import json
import secrets
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Set, Tuple
from backend.security.audit import audit_logger

DEFAULT_PERSISTENCE_FILE = Path(__file__).resolve().parent.parent / "data" / ".extension_pairing.json"


@dataclass
class ExtensionRegistration:
    token: str
    extension_id: str
    created_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    revoked: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "token": self.token,
            "extension_id": self.extension_id,
            "created_at": self.created_at,
            "last_seen": self.last_seen,
            "revoked": self.revoked,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ExtensionRegistration":
        return cls(
            token=data["token"],
            extension_id=data.get("extension_id", "unknown_extension"),
            created_at=data.get("created_at", time.time()),
            last_seen=data.get("last_seen", time.time()),
            revoked=data.get("revoked", False),
        )


class ExtensionAuthManager:
    """Manages Chrome Extension pairing credentials and access control."""

    def __init__(self, persistence_file: Optional[str] = None):
        self._registrations: Dict[str, ExtensionRegistration] = {}
        # token -> registration
        self._allowed_extension_ids: Optional[Set[str]] = self._load_allowed_ids()
        self._lock = threading.Lock()
        self._persistence_file: Optional[Path] = Path(persistence_file) if persistence_file else None

        if self._persistence_file:
            self._load_from_disk()

    def _load_allowed_ids(self) -> Optional[Set[str]]:
        raw = os.getenv("ALLOWED_EXTENSION_IDS", "").strip()
        if not raw:
            return None
        return {item.strip() for item in raw.split(",") if item.strip()}

    def reload_allowed_ids(self) -> None:
        with self._lock:
            self._allowed_extension_ids = self._load_allowed_ids()

    def _load_from_disk(self) -> None:
        """Load paired extension registrations from disk storage."""
        if not self._persistence_file or not self._persistence_file.exists():
            return
        try:
            with open(self._persistence_file, "r", encoding="utf-8") as f:
                payload = json.load(f)
            regs = payload.get("registrations", {})
            for tok, rdata in regs.items():
                if isinstance(rdata, dict) and "token" in rdata:
                    self._registrations[tok] = ExtensionRegistration.from_dict(rdata)
        except Exception as exc:
            audit_logger.record_event(
                event_type="extension_auth_store_read_failed",
                task_id="system",
                details={"error": str(exc)},
            )

    def _save_to_disk(self) -> None:
        """Atomically persist registrations to disk with restricted permissions."""
        if not self._persistence_file:
            return
        try:
            self._persistence_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": "1.0.0",
                "updated_at": time.time(),
                "registrations": {
                    tok: reg.to_dict() for tok, reg in self._registrations.items()
                },
            }
            tmp_fd, tmp_path = tempfile.mkstemp(
                dir=str(self._persistence_file.parent),
                prefix=".ext_pair_",
                suffix=".tmp",
            )
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)

            # Restrict permissions where supported
            try:
                os.chmod(tmp_path, 0o600)
            except Exception:
                pass

            os.replace(tmp_path, self._persistence_file)
        except Exception as exc:
            audit_logger.record_event(
                event_type="extension_auth_store_write_failed",
                task_id="system",
                details={"error": str(exc)},
            )

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
                    if reg.revoked:
                        audit_logger.record_event(
                            event_type="extension_auth_rejected",
                            task_id="system",
                            details={"reason": "revoked_token", "extension_id": clean_ext_id},
                        )
                        return False, None, "Extension pairing credential has been revoked."

                    # Verify extension ID binding if both are specific IDs
                    if (
                        reg.extension_id not in ("unknown_extension", "chrome-extension")
                        and clean_ext_id not in ("unknown_extension", "chrome-extension")
                        and reg.extension_id != clean_ext_id
                    ):
                        audit_logger.record_event(
                            event_type="extension_auth_rejected",
                            task_id="system",
                            details={
                                "reason": "extension_id_mismatch",
                                "registered_id": reg.extension_id,
                                "provided_id": clean_ext_id,
                            },
                        )
                        return False, None, "Extension pairing credential belongs to a different extension ID."

                    reg.last_seen = time.time()
                    if extension_id and reg.extension_id in ("unknown_extension", "chrome-extension"):
                        reg.extension_id = extension_id
                    self._save_to_disk()
                    return True, token, "Authenticated successfully."
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
            self._save_to_disk()
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
            if token in self._registrations:
                reg = self._registrations[token]
                reg.revoked = True
                self._save_to_disk()
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
            for reg in self._registrations.values():
                if not reg.revoked:
                    reg.revoked = True
                    count += 1
            if count > 0:
                self._save_to_disk()
            audit_logger.record_event(
                event_type="all_extensions_revoked",
                task_id="system",
                details={"revoked_count": count},
            )
            return count

    def get_registered_count(self) -> int:
        with self._lock:
            return sum(1 for reg in self._registrations.values() if not reg.revoked)


# Global singleton instance with persistent storage
extension_auth_manager = ExtensionAuthManager(
    persistence_file=os.getenv("EXTENSION_PAIRING_FILE", str(DEFAULT_PERSISTENCE_FILE))
)
