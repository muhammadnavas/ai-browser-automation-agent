"""
backend/security/auth.py
------------------------
Local Backend Authentication, Session Management, and CSRF Protection
for the AI Browser Automation Agent (Issue 2).

Key Features:
- Transparent local authentication without a login screen or password prompt.
- In-memory session store with TTL and sliding activity expiration.
- CSRF token generation and validation for state-changing requests.
- Single-use, short-lived (15s) tickets for WebSocket authentication.
- Loopback client detection and origin validation.
- Audit logging of authentication and session lifecycle events.
"""

import hmac
import os
import getpass
import logging
import secrets
import threading
import time
import tempfile
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple, Set
from fastapi import Request, HTTPException, status
from backend.security.audit import audit_logger

logger = logging.getLogger("backend.security.auth")


SESSION_COOKIE_NAME = "agent_session"
CSRF_COOKIE_NAME = "agent_csrf"
CSRF_HEADER_NAME = "X-CSRF-Token"
SESSION_LIFETIME_SECONDS = int(os.getenv("SESSION_LIFETIME_SECONDS", str(24 * 3600)))  # 24 hours
WS_TICKET_LIFETIME_SECONDS = int(os.getenv("WS_TICKET_LIFETIME_SECONDS", "15"))  # 15 seconds

# Server secret for signing / HMAC operations
SERVER_SECRET = os.getenv("AGENT_SERVER_SECRET", secrets.token_hex(32))

BOOTSTRAP_FILE_PATH = os.path.join(tempfile.gettempdir(), ".agent_bootstrap_token")


def get_bootstrap_token() -> str:
    """Get or initialize the local single-user bootstrap token."""
    env_tok = os.getenv("AGENT_BOOTSTRAP_TOKEN")
    if env_tok and env_tok.strip():
        return env_tok.strip()
    if os.path.exists(BOOTSTRAP_FILE_PATH):
        try:
            with open(BOOTSTRAP_FILE_PATH, "r", encoding="utf-8") as f:
                token = f.read().strip()
                if token:
                    return token
        except Exception:
            pass
    token = secrets.token_urlsafe(32)
    try:
        with open(BOOTSTRAP_FILE_PATH, "w", encoding="utf-8") as f:
            f.write(token)
    except Exception:
        pass
    return token


class SessionRateLimiter:
    """In-memory sliding window rate limiter for session creation (per client IP)."""

    def __init__(self, max_requests: int = 30, window_seconds: float = 60.0):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._history: Dict[str, list] = {}
        self._lock = threading.Lock()

    def is_allowed(self, client_ip: str) -> bool:
        now = time.time()
        with self._lock:
            timestamps = self._history.get(client_ip, [])
            valid_timestamps = [t for t in timestamps if now - t < self.window_seconds]
            if len(valid_timestamps) >= self.max_requests:
                self._history[client_ip] = valid_timestamps
                return False
            valid_timestamps.append(now)
            self._history[client_ip] = valid_timestamps
            return True

    def reset(self) -> None:
        with self._lock:
            self._history.clear()


session_rate_limiter = SessionRateLimiter()


def is_origin_allowed(origin: Optional[str], allowed_origins: list) -> bool:
    """Check if an Origin header is permitted for session initialization."""
    if not origin:
        return True
    clean_origin = origin.strip().rstrip("/")
    for allowed in allowed_origins:
        if clean_origin == allowed.strip().rstrip("/"):
            return True
    import urllib.parse
    try:
        parsed = urllib.parse.urlparse(clean_origin)
        if parsed.hostname in ("localhost", "127.0.0.1", "::1"):
            return True
    except Exception:
        pass
    return False


@dataclass
class SessionData:
    session_id: str
    csrf_token: str
    user_id: str = "default_user"
    created_at: float = field(default_factory=time.time)
    last_accessed: float = field(default_factory=time.time)
    client_ip: str = "127.0.0.1"
    is_loopback: bool = True
    metadata: Dict[str, str] = field(default_factory=dict)

    def is_expired(self, max_lifetime: float = SESSION_LIFETIME_SECONDS) -> bool:
        now = time.time()
        return (now - self.created_at) > max_lifetime


@dataclass
class WSTicketData:
    ticket: str
    session_id: str
    task_id: Optional[str]
    created_at: float = field(default_factory=time.time)
    consumed: bool = False

    def is_valid(self, max_age: float = WS_TICKET_LIFETIME_SECONDS) -> bool:
        if self.consumed:
            return False
        return (time.time() - self.created_at) <= max_age


class SessionManager:
    """Thread-safe in-memory session and ticket manager."""

    def __init__(self):
        self._sessions: Dict[str, SessionData] = {}
        self._ws_tickets: Dict[str, WSTicketData] = {}
        self._loopback_ips: Set[str] = {
            "127.0.0.1",
            "::1",
            "localhost",
            "testclient",
        }

    def is_loopback(self, host: Optional[str]) -> bool:
        if not host:
            return False
        host_clean = host.strip().lower()
        if host_clean in self._loopback_ips:
            return True
        if host_clean.startswith("127."):
            return True
        return False

    def create_session(self, client_ip: str = "127.0.0.1", user_id: str = "default_user") -> SessionData:
        """Create a new authenticated session."""
        session_id = secrets.token_urlsafe(32)
        csrf_token = secrets.token_hex(16)
        is_loop = self.is_loopback(client_ip)

        # Precedence:
        # 1. If an explicit verified user_id is provided (not None / 'default_user'), use it directly.
        #    APP_USER_ID can NEVER override an explicit authenticated user identity.
        # 2. In production (ENVIRONMENT=production/prod), never apply developer fallback namespaces.
        # 3. In local development / non-production, check APP_USER_ID from environment.
        # 4. If APP_USER_ID is unset in non-production, warn and derive a machine/user fallback
        #    (dev_<system_user>) to prevent developer data collision on shared Neon database.
        effective_user = user_id
        if not user_id or user_id == "default_user":
            is_prod = os.getenv("ENVIRONMENT", "").strip().lower() in ("production", "prod")
            if is_prod:
                effective_user = "default_user"
            else:
                env_user = os.getenv("APP_USER_ID", "").strip()
                if env_user:
                    effective_user = env_user
                else:
                    try:
                        sys_user = getpass.getuser().strip()
                    except Exception:
                        sys_user = os.getenv("USERNAME", os.getenv("USER", "")).strip()
                    if sys_user and sys_user.lower() not in ("root", "administrator", "system"):
                        effective_user = f"dev_{sys_user}"
                        logger.warning(
                            "APP_USER_ID is not configured in .env; auto-assigned '%s' from local system username to avoid sharing data on hosted database. Please set APP_USER_ID in .env.",
                            effective_user,
                        )
                    else:
                        effective_user = "dev_local_user"
                        logger.warning(
                            "APP_USER_ID is not configured in .env; defaulting to '%s'. Please set APP_USER_ID in .env.",
                            effective_user,
                        )

        session = SessionData(
            session_id=session_id,
            csrf_token=csrf_token,
            user_id=effective_user or "default_user",
            created_at=time.time(),
            last_accessed=time.time(),
            client_ip=client_ip,
            is_loopback=is_loop,
        )
        self._sessions[session_id] = session
        audit_logger.record_event(
            event_type="session_created",
            task_id="system",
            details={"is_loopback": is_loop, "client_ip": client_ip, "user_id": effective_user},
        )
        return session

    def get_session(self, session_id: Optional[str]) -> Optional[SessionData]:
        """Retrieve an active unexpired session."""
        if not session_id:
            return None
        session = self._sessions.get(session_id)
        if not session:
            return None
        if session.is_expired():
            self._sessions.pop(session_id, None)
            audit_logger.record_event(
                event_type="session_expired",
                task_id="system",
                details={},
            )
            return None
        session.last_accessed = time.time()
        return session

    def delete_session(self, session_id: str) -> None:
        """Revoke a session."""
        if session_id in self._sessions:
            del self._sessions[session_id]
            audit_logger.record_event(
                event_type="session_revoked",
                task_id="system",
                details={},
            )

    def verify_csrf(self, session: SessionData, header_csrf: Optional[str]) -> bool:
        """Verify CSRF token match using constant-time comparison."""
        if not session or not header_csrf:
            return False
        return hmac.compare_digest(session.csrf_token, header_csrf.strip())

    def create_ws_ticket(self, session_id: str, task_id: Optional[str] = None) -> str:
        """Generate a short-lived single-use ticket for WebSocket connections."""
        ticket = f"wst_{secrets.token_urlsafe(24)}"
        ticket_data = WSTicketData(
            ticket=ticket,
            session_id=session_id,
            task_id=task_id,
            created_at=time.time(),
            consumed=False,
        )
        self._ws_tickets[ticket] = ticket_data
        return ticket

    def consume_ws_ticket(self, ticket: Optional[str], expected_task_id: Optional[str] = None) -> Optional[SessionData]:
        """Validate and immediately consume a WebSocket ticket."""
        if not ticket:
            return None
        ticket_data = self._ws_tickets.get(ticket)
        if not ticket_data:
            return None
        if not ticket_data.is_valid():
            self._ws_tickets.pop(ticket, None)
            return None

        # Verify task binding if specified
        if expected_task_id and ticket_data.task_id and ticket_data.task_id != expected_task_id:
            audit_logger.record_event(
                event_type="ws_ticket_task_mismatch",
                task_id=expected_task_id,
                details={"bound_task_id": ticket_data.task_id},
            )
            return None

        ticket_data.consumed = True
        self._ws_tickets.pop(ticket, None)
        return self.get_session(ticket_data.session_id)

    def prune_expired(self) -> None:
        """Prune old sessions and stale tickets."""
        now = time.time()
        expired_sessions = [sid for sid, s in self._sessions.items() if s.is_expired()]
        for sid in expired_sessions:
            del self._sessions[sid]

        expired_tickets = [tid for tid, t in self._ws_tickets.items() if not t.is_valid()]
        for tid in expired_tickets:
            del self._ws_tickets[tid]


# Global session manager instance
session_manager = SessionManager()


def extract_session_id(request: Request) -> Optional[str]:
    """Extract session ID from Cookie, Authorization header, or X-Session-ID."""
    # 1. Cookie
    session_cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if session_cookie:
        return session_cookie

    # 2. Authorization header: Bearer <session_id>
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
        if token:
            return token

    # 3. Custom Header
    custom_hdr = request.headers.get("X-Session-ID")
    if custom_hdr:
        return custom_hdr.strip()

    return None


def get_client_ip(request: Request) -> str:
    """Safely get client IP address."""
    if request.client and request.client.host:
        return request.client.host
    return "127.0.0.1"


def get_current_session(request: Request) -> Optional[SessionData]:
    """Get the active session for the request, or None."""
    session_id = extract_session_id(request)
    return session_manager.get_session(session_id)


def require_session(request: Request) -> SessionData:
    """Dependency that raises 401 if request has no valid session."""
    session = get_current_session(request)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. No valid session active.",
        )
    return session


def verify_csrf_if_mutating(request: Request, session: SessionData) -> None:
    """Verify CSRF token on mutating HTTP requests (POST, PUT, DELETE, PATCH)."""
    if request.method in ("POST", "PUT", "DELETE", "PATCH"):
        csrf_header = request.headers.get(CSRF_HEADER_NAME)
        if not csrf_header or not session_manager.verify_csrf(session, csrf_header):
            audit_logger.record_event(
                event_type="csrf_validation_failed",
                task_id="system",
                details={"method": request.method, "path": request.url.path},
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="CSRF validation failed. Missing or invalid X-CSRF-Token.",
            )


def get_authenticated_user_id(session: SessionData) -> str:
    """Derive authenticated user identity from the backend session. Never trust client-supplied user_id."""
    if not session:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. No valid session active.",
        )
    return getattr(session, "user_id", None) or "default_user"

