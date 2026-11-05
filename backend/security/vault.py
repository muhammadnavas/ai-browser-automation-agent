"""
backend/security/vault.py
-------------------------
In-memory, per-task sensitive data vault for authorized contact values.
Contents are kept strictly in backend memory and never persisted to Redis,
disk, logs, task archives, reports, or WebSocket messages.
Tier 1 secrets are strictly prohibited from being stored in the vault.
"""

import re
import threading
from typing import Dict, Optional

from backend.security.detector import (
    contains_tier1_secrets,
    is_public_business_email,
    is_tier1_key,
)
from backend.security.tiers import EMAIL_REGEX, PHONE_REGEX


class TaskVault:
    """In-memory sensitive data vault for a single task."""

    def __init__(self, task_id: str):
        self.task_id = task_id
        self._authorized_contacts: Dict[str, str] = {}  # placeholder -> raw_value
        self._reverse_map: Dict[str, str] = {}  # raw_value -> placeholder
        self._lock = threading.Lock()

    def store_contact(self, contact_type: str, raw_value: str) -> str:
        """Store an authorized contact value (email, phone, address, name) and return its placeholder.

        Raises ValueError if raw_value or contact_type contains Tier 1 secrets.
        """
        if not raw_value or not isinstance(raw_value, str):
            return ""

        clean_val = raw_value.strip()

        # Strict check: NEVER store Tier 1 secrets
        if is_tier1_key(contact_type) or contains_tier1_secrets(clean_val):
            raise ValueError(f"Tier 1 secrets cannot be stored in the TaskVault: key='{contact_type}'")

        with self._lock:
            # If already vaulted, return existing placeholder
            if clean_val in self._reverse_map:
                return self._reverse_map[clean_val]

            norm_type = contact_type.strip().upper().replace(" ", "_").replace("-", "_")
            if not norm_type.startswith("USER_"):
                placeholder_base = f"USER_{norm_type}"
            else:
                placeholder_base = norm_type

            placeholder = f"{{{{{placeholder_base}}}}}"
            idx = 1
            while placeholder in self._authorized_contacts:
                idx += 1
                placeholder = f"{{{{{placeholder_base}_{idx}}}}}"

            self._authorized_contacts[placeholder] = clean_val
            self._reverse_map[clean_val] = placeholder
            return placeholder

    def get_raw_value(self, placeholder: str, calling_task_id: Optional[str] = None) -> Optional[str]:
        """Retrieve raw contact value by placeholder strictly within task boundary. Missing or mismatched caller identity is blocked."""
        if not calling_task_id or calling_task_id != self.task_id:
            import logging
            logging.getLogger("security.vault").warning(
                "Task vault access rejected: calling_task_id '%s' does not match vault task_id '%s'",
                calling_task_id,
                self.task_id,
            )
            return None
        with self._lock:
            return self._authorized_contacts.get(placeholder)

    def substitute_placeholders(self, text: str, calling_task_id: Optional[str] = None) -> str:
        """Substitute authorized contact values for placeholders at the form-filling stage.
        Restricted to the intended task execution boundary. Missing or mismatched caller identity is blocked.
        """
        if not calling_task_id or calling_task_id != self.task_id:
            import logging
            logging.getLogger("security.vault").warning(
                "Task vault placeholder substitution rejected: calling_task_id '%s' does not match vault task_id '%s'",
                calling_task_id,
                self.task_id,
            )
            return text
        if not text or not isinstance(text, str):
            return text
        with self._lock:
            result = text
            for placeholder, raw_val in self._authorized_contacts.items():
                result = result.replace(placeholder, raw_val)
            return result

    def sanitize_and_vault_text(self, text: str) -> str:
        """Scan text (e.g. user goal/prompt), extract authorized personal contact values,

        store them in memory, and replace them with placeholders.
        """
        if not text or not isinstance(text, str):
            return text

        result = text

        # 1. Detect and vault explicit contact key-values: e.g. email: user@domain.com
        kv_pairs = re.findall(
            r'(?i)\b(email|mail|phone|tel|mobile|name|address)\s*[:=]\s*["\']?([^,"\';\n]+)["\']?',
            result,
        )
        for k, v in kv_pairs:
            k_clean = k.strip().lower()
            v_clean = v.strip()
            if v_clean and not contains_tier1_secrets(v_clean):
                try:
                    ph = self.store_contact(k_clean, v_clean)
                    result = result.replace(v_clean, ph)
                except ValueError:
                    pass

        # 2. Detect and vault standalone personal emails
        emails = EMAIL_REGEX.findall(result)
        for email in emails:
            # Do not vault already vaulted placeholders
            if email.startswith("{{") and email.endswith("}}"):
                continue
            # Keep public business emails unchanged
            if not is_public_business_email(email):
                try:
                    ph = self.store_contact("EMAIL", email)
                    result = result.replace(email, ph)
                except ValueError:
                    pass

        # 3. Detect and vault standalone phone numbers
        phones = PHONE_REGEX.findall(result)
        for phone in phones:
            phone_clean = phone.strip()
            if len(re.sub(r"\D", "", phone_clean)) >= 10:
                try:
                    ph = self.store_contact("PHONE", phone_clean)
                    result = result.replace(phone_clean, ph)
                except ValueError:
                    pass

        return result

    def get_placeholders(self) -> list[str]:
        """Return list of active placeholder names without exposing raw values."""
        with self._lock:
            return list(self._authorized_contacts.keys())

    def get_all_placeholders(self) -> Dict[str, str]:
        """Return placeholders mapped to masked indicators rather than exposing raw contact values."""
        with self._lock:
            return {k: "[VAULTED_CONTACT]" for k in self._authorized_contacts.keys()}

    vault_contact = store_contact

    def clear(self) -> None:
        """Wipe all vault contents from memory."""
        with self._lock:
            self._authorized_contacts.clear()
            self._reverse_map.clear()


class TaskVaultManager:
    """Registry of in-memory task vaults."""

    def __init__(self):
        self._vaults: Dict[str, TaskVault] = {}
        self._lock = threading.Lock()

    def get_vault(self, task_id: str, calling_task_id: Optional[str] = None) -> TaskVault:
        if not calling_task_id or calling_task_id != task_id:
            raise PermissionError(
                f"Task vault access requires valid calling_task_id matching '{task_id}'. Received: '{calling_task_id}'."
            )
        with self._lock:
            if task_id not in self._vaults:
                self._vaults[task_id] = TaskVault(task_id)
            return self._vaults[task_id]

    def clear_vault(self, task_id: str) -> None:
        with self._lock:
            vault = self._vaults.pop(task_id, None)
            if vault:
                vault.clear()

    def has_vault(self, task_id: str) -> bool:
        with self._lock:
            return task_id in self._vaults


# Global singleton instance
default_vault_manager = TaskVaultManager()


def get_task_vault(task_id: str, calling_task_id: Optional[str] = None) -> TaskVault:
    return default_vault_manager.get_vault(task_id, calling_task_id=calling_task_id)


def clear_task_vault(task_id: str) -> None:
    default_vault_manager.clear_vault(task_id)
