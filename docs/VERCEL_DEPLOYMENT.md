# Vercel Deployment & Cross-Origin Architecture Guide

This document details the architectural requirements, security boundaries, and networking constraints when hosting the AI Browser Automation Agent frontend on **Vercel** (or similar HTTPS static hosting providers) while communicating with the local or cloud backend.

---

## 1. Core Browser Security Constraints

When the React/Vite dashboard is served from an HTTPS domain (e.g., `https://ai-browser-agent.vercel.app`), modern web browsers enforce two strict security boundaries:

### A. Mixed Content Restrictions (W3C Mixed Content Specification)
Browsers automatically block active network requests (such as `fetch()`, `XMLHttpRequest`, and WebSocket connections `ws://`) from an **HTTPS** origin to an unencrypted **HTTP** endpoint (`http://...`).
- An HTTPS-hosted frontend **cannot** directly connect to `http://127.0.0.1:8000` or `http://localhost:8000`.
- All backend endpoints and WebSocket connections must be served over **HTTPS** (`https://...`) and **WSS** (`wss://...`).

### B. Chrome Private Network Access (PNA)
Google Chrome and Chromium-based browsers restrict public websites (like `https://*.vercel.app`) from sending requests to private network addresses (such as `127.0.0.1`, `localhost`, or `192.168.x.x`):
- PNA blocks cross-origin requests from public origins to loopback unless explicitly permitted via PNA preflight headers (`Access-Control-Request-Private-Network`) and secure context (`https://`).

---

## 2. Production Deployment Configurations

To use a Vercel-hosted frontend with the backend, one of the following two production topologies must be used:

### Option 1: Secure Tunnel (Recommended for Local Agent Execution)
When running the agent backend on a local machine with the Chrome Extension, front the local backend with a secure TLS tunnel:

```
[ Vercel Frontend ] (HTTPS)
        │
        ▼ (HTTPS / WSS)
[ Cloudflare Tunnel / ngrok / Tailscale Funnel ] (Valid Public TLS Certificate)
        │
        ▼ (Loopback HTTP / WS)
[ Local Backend :8000 ]
        │
        ▼ (Loopback WebSocket with TOFU Token)
[ Chrome Extension ]
```

1. **Start the local tunnel**:
   ```bash
   cloudflared tunnel --url http://127.0.0.1:8000
   # Or using ngrok:
   ngrok http 8000
   ```
2. **Configure Backend Environment**:
   ```env
   CORS_ALLOW_ORIGINS=https://ai-browser-agent.vercel.app
   ```
3. **Configure Frontend Environment on Vercel**:
   ```env
   VITE_API_BASE_URL=https://<your-tunnel-subdomain>.trycloudflare.com
   VITE_WS_BASE_URL=wss://<your-tunnel-subdomain>.trycloudflare.com
   ```

### Option 2: Full Cloud Deployment
Host both backend and frontend on cloud infrastructure behind valid TLS certificates:
- Backend: Deployed on a container service (e.g., Railway, Cloud Run, AWS ECS) with HTTPS/WSS.
- Frontend: Deployed on Vercel with `VITE_API_BASE_URL` pointing to the cloud backend.

---

## 3. Cross-Origin Authentication: Bearer Token vs. Cookies

- In same-origin or localhost setups, session authentication is transparently handled via `HttpOnly`, `SameSite=Lax` cookies.
- In cross-origin setups (e.g. `https://ai-browser-agent.vercel.app` calling an external backend domain), third-party cookie restrictions in modern browsers may prevent cookie transmission.
- Therefore, the application supports **Bearer Token Authentication**:
  1. Frontend calls `POST /api/auth/session` with `Origin: https://ai-browser-agent.vercel.app`.
  2. Backend responds with `{ "session_id": "...", "csrf_token": "..." }`.
  3. Frontend transmits `Authorization: Bearer <session_id>` and `X-CSRF-Token: <csrf_token>` on subsequent API calls.
  4. For WebSocket streams, frontend requests a short-lived ticket via `POST /api/ws/ticket` and connects using `wss://...?ticket=<ticket>`.

---

## 4. Distinction Between Simulated Tests and Live Deployment

- **Automated Tests (`tests/test_issue2_auth.py`)**:
  - The automated test suite verifies CORS preflight (`OPTIONS`), Origin header validation, allowed origins matching, and Bearer token parsing at the HTTP framework layer using Starlette's in-process `TestClient`.
  - These tests verify that FastAPI, CORS middleware, and session managers correctly allow authorized cross-origin requests and block unauthorized ones.
- **Live Deployment**:
  - Live deployment requires actual TLS infrastructure (HTTPS/WSS) as described in Section 2 to satisfy browser-level Mixed Content and PNA security policies.

