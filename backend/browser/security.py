"""
browser/security.py
-------------------
URL validation and navigation protections for the AI Browser Automation Agent.

Protects against:
- Localhost and loopback destinations (127.0.0.0/8, ::1, localhost)
- Private network addresses (10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, fc00::/7)
- Link-local and cloud metadata destinations (169.254.0.0/16, fe80::/10, metadata.google.internal)
- Prohibited administrative/internal ports including agent backend (8000) and frontend (5173)
- Non-HTTP/HTTPS schemes (file://, javascript:, data:, blob:, gopher:, ftp:, etc.)
- Unsafe redirects and DNS rebinding paths
- Agent's own backend origin/port and local agent endpoints (/api/*, /screenshots/*, /reports/*)
"""

import asyncio
import ipaddress
import logging
import os
import socket
from typing import Optional, Set, Tuple
from urllib.parse import urlparse

logger = logging.getLogger("browser.security")

AGENT_PORT = int(os.getenv("PORT", "8000"))
FRONTEND_PORT = int(os.getenv("VITE_PORT", "5173"))

# Prohibited dangerous/administrative network ports
PROHIBITED_PORTS: Set[int] = {
    21,    # FTP
    22,    # SSH
    23,    # Telnet
    25,    # SMTP
    135,   # RPC
    139,   # NetBIOS
    445,   # SMB
    1433,  # MSSQL
    1521,  # Oracle DB
    2375,  # Docker
    2376,  # Docker SSL
    3306,  # MySQL
    3389,  # RDP
    5173,  # Vite dev server default
    5432,  # PostgreSQL
    5900,  # VNC
    6379,  # Redis
    8000,  # FastAPI backend default
    9200,  # Elasticsearch
    11211, # Memcached
    27017, # MongoDB
    AGENT_PORT,
    FRONTEND_PORT,
}

# Blocked hostnames and suffixes
PROHIBITED_HOST_PATTERNS: Set[str] = {
    "localhost",
    "127.0.0.1",
    "::1",
    "0.0.0.0",
    "metadata.google.internal",
    "instance-data",
}

CLOUD_METADATA_IPS = {
    ipaddress.ip_address("169.254.169.254"),
    ipaddress.ip_address("100.100.100.200"),
}


def is_ip_safe(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> Tuple[bool, str]:
    """
    Check if an IPv4 or IPv6 address is safe for external research navigation.
    Rejects loopback, private, link-local, multicast, reserved, and cloud metadata IPs.
    Properly unwraps IPv4-mapped IPv6 addresses (e.g. ::ffff:100.100.100.200) before checking.
    """
    # Unwrap IPv4-mapped IPv6 addresses
    check_ip = getattr(ip, "ipv4_mapped", None) or ip

    if check_ip in CLOUD_METADATA_IPS:
        return False, f"Cloud metadata IP prohibited: {ip}"
    if check_ip.is_loopback:
        return False, f"Loopback address prohibited: {ip}"
    if check_ip.is_private:
        return False, f"Private/internal network address prohibited: {ip}"
    if check_ip.is_link_local:
        return False, f"Link-local address prohibited: {ip}"
    if check_ip.is_multicast:
        return False, f"Multicast address prohibited: {ip}"
    if check_ip.is_reserved:
        return False, f"Reserved address prohibited: {ip}"
    if check_ip.is_unspecified:
        return False, f"Unspecified address prohibited: {ip}"

    # Also verify the original address if it differs from check_ip
    if ip != check_ip:
        if ip.is_loopback:
            return False, f"Loopback address prohibited: {ip}"
        if ip.is_private:
            return False, f"Private/internal network address prohibited: {ip}"
        if ip.is_link_local:
            return False, f"Link-local address prohibited: {ip}"

    return True, "Safe"


def validate_url_security(url: str, resolve_dns: bool = True) -> Tuple[bool, str, Optional[str]]:
    """
    Validate a destination URL against security policies.

    Args:
        url: The URL string to validate.
        resolve_dns: Whether to perform DNS resolution to verify resolved IP addresses.

    Returns:
        Tuple of (is_safe: bool, reason: str, normalized_url: Optional[str])
    """
    if not url or not isinstance(url, str):
        return False, "URL is empty or invalid type", None

    clean_url = url.strip()
    if not clean_url:
        return False, "URL is empty", None

    # Only prepend https:// if no scheme is specified at all
    if "://" not in clean_url and not clean_url.startswith(("javascript:", "data:", "about:", "blob:", "file:", "ftp:", "gopher:", "mailto:")):
        clean_url = "https://" + clean_url

    try:
        parsed = urlparse(clean_url)
    except Exception as exc:
        return False, f"Malformed URL: {exc}", None

    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        return False, f"Prohibited scheme '{scheme}'. Only HTTP and HTTPS are permitted.", None

    hostname = (parsed.hostname or "").strip().lower()
    if not hostname:
        return False, "Missing hostname in URL", None

    # Check prohibited hostnames
    if hostname in PROHIBITED_HOST_PATTERNS or hostname.endswith((".localhost", ".local", ".internal", ".lan")):
        return False, f"Prohibited host target '{hostname}'", None

    # Safely evaluate and validate port if specified
    try:
        port = parsed.port
    except (ValueError, TypeError):
        return False, "Malformed or out-of-range port in URL", None

    if port and port in PROHIBITED_PORTS:
        return False, f"Prohibited port {port}", None

    # Check for targeting local agent endpoints
    path_lower = (parsed.path or "").lower()
    if path_lower.startswith(("/api", "/screenshots", "/reports", "/ws")):
        if hostname in ("localhost", "127.0.0.1", "::1", "0.0.0.0") or (port and port in (AGENT_PORT, FRONTEND_PORT, 8000, 5173)):
            return False, f"Direct access to agent internal endpoint '{parsed.path}' is prohibited", None

    # Check IP literal or resolve domain
    try:
        ip_obj = ipaddress.ip_address(hostname)
        is_safe, reason = is_ip_safe(ip_obj)
        if not is_safe:
            return False, reason, None
    except ValueError:
        # Hostname is a domain name
        if resolve_dns:
            trusted_suffixes = (
                ".google.com", ".googleapis.com", ".gstatic.com",
                ".github.com", ".microsoft.com", ".apple.com",
                ".mozilla.org", ".wikipedia.org", ".cloudflare.com",
            )
            is_trusted_domain = (
                hostname in ("google.com", "github.com", "microsoft.com", "apple.com", "wikipedia.org")
                or hostname.endswith(trusted_suffixes)
            )

            addr_info = None
            last_dns_error = None
            for dns_attempt in range(2):
                try:
                    addr_info = socket.getaddrinfo(hostname, parsed.port or 80, socket.AF_UNSPEC, socket.SOCK_STREAM)
                    break
                except socket.gaierror as exc:
                    last_dns_error = exc
                    if dns_attempt == 0:
                        import time
                        time.sleep(0.3)
                except Exception as exc:
                    logger.warning(f"Unexpected DNS resolution error for {hostname}: {exc}")
                    break

            if addr_info is not None:
                resolved_ips = set()
                for item in addr_info:
                    sockaddr = item[4]
                    resolved_ip_str = sockaddr[0]
                    resolved_ips.add(resolved_ip_str)

                for ip_str in resolved_ips:
                    try:
                        resolved_ip = ipaddress.ip_address(ip_str)
                        safe, reason = is_ip_safe(resolved_ip)
                        if not safe:
                            return False, f"Domain '{hostname}' resolves to prohibited IP: {reason}", None
                    except ValueError:
                        continue
            elif last_dns_error is not None:
                if is_trusted_domain:
                    logger.warning(
                        "DNS resolution failed for trusted public domain %s: %s; allowing browser to handle resolution",
                        hostname, last_dns_error,
                    )
                else:
                    return False, f"DNS resolution failed for '{hostname}': {last_dns_error}. Please check your internet connection.", None

    normalized = parsed.geturl()
    return True, "Safe", normalized


async def install_navigation_guard(page) -> None:
    """
    Install a Playwright request guard on a page.

    Every HTTP(S) request is validated, not only the first page.goto URL, so
    redirects and page-initiated requests cannot reach prohibited internal
    destinations. DNS resolution runs in a worker thread to avoid blocking the
    asyncio event loop.
    """
    if page is None or not hasattr(page, "route"):
        raise TypeError("install_navigation_guard requires a Playwright Page")

    async def guard_request(route) -> None:
        request = route.request
        target_url = getattr(request, "url", "") or ""
        try:
            is_safe, reason, _ = await asyncio.to_thread(
                validate_url_security, target_url, True
            )
        except Exception as exc:
            logger.warning("URL security validation failed closed for %s: %s", target_url, exc)
            await route.abort("blockedbyclient")
            return

        if not is_safe:
            logger.warning("Blocked browser request to %s: %s", target_url, reason)
            await route.abort("blockedbyclient")
            return

        await route.continue_()

    await page.route("**/*", guard_request)
