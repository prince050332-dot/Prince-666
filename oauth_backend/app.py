"""One-Click Facebook OAuth Backend for Social Manager.

Deploy on Vercel, Render, Railway, or run locally:
    uvicorn app:app --host 0.0.0.0 --port 8000

Environment variables:
    FB_APP_ID           - Your Meta App ID
    FB_APP_SECRET       - Your Meta App Secret (kept strictly on the server)
    PUBLIC_BASE_URL     - Public HTTPS URL of this server (e.g. https://your-app.vercel.app)
    SERVER_SIGNING_KEY  - Random secret string (>= 32 chars) used to sign/encrypt short-lived tokens
    FB_GRAPH_VERSION    - Optional Graph API version (default: v22.0)
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
import zlib

from cryptography.fernet import Fernet, InvalidToken
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
import requests

app = FastAPI(title="Social Manager One-Click OAuth Backend")

FB_APP_ID = os.getenv("FB_APP_ID", "").strip()
FB_APP_SECRET = os.getenv("FB_APP_SECRET", "").strip()
FB_GRAPH_VERSION = os.getenv("FB_GRAPH_VERSION", "v22.0").strip()
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
SCOPES = "pages_show_list,pages_manage_posts,pages_read_engagement,pages_manage_engagement,read_insights"

# Ephemeral fallback key for local dev only; always set SERVER_SIGNING_KEY in production!
_RAW_SIGNING_KEY = os.getenv("SERVER_SIGNING_KEY", "").strip()
_HAS_EXPLICIT_SIGNING_KEY = bool(_RAW_SIGNING_KEY)
_SIGNING_KEY = (_RAW_SIGNING_KEY or secrets.token_urlsafe(32)).encode("utf-8")
_FERNET = Fernet(base64.urlsafe_b64encode(hashlib.sha256(_SIGNING_KEY).digest()))

STATE_TTL_SECONDS = 600   # 10 minutes for user to finish Facebook login
SESSION_TTL_SECONDS = 90  # 90 seconds for desktop app to claim the session
RATE_LIMIT_WINDOW = 60    # seconds
RATE_LIMIT_MAX_REQ = 30   # max requests per IP per minute on /api/fb/*
_USED_JTIS: dict[str, float] = {}
_RATE_BUCKETS: dict[str, list[float]] = {}


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    return response


def _enforce_rate_limit(request: Request) -> None:
    ip = (request.client.host if request.client else "unknown") or "unknown"
    now = time.monotonic()
    cutoff = now - RATE_LIMIT_WINDOW
    hits = [t for t in _RATE_BUCKETS.get(ip, []) if t > cutoff]
    if len(hits) >= RATE_LIMIT_MAX_REQ:
        raise HTTPException(status_code=429, detail="Too many requests, please retry later")
    hits.append(now)
    _RATE_BUCKETS[ip] = hits
    if len(_RATE_BUCKETS) > 1000:
        stale = [k for k, v in _RATE_BUCKETS.items() if not v or v[-1] <= cutoff]
        for k in stale:
            _RATE_BUCKETS.pop(k, None)
        if len(_RATE_BUCKETS) > 5000:
            for k in list(_RATE_BUCKETS.keys())[: len(_RATE_BUCKETS) - 5000]:
                _RATE_BUCKETS.pop(k, None)


class ClaimRequest(BaseModel):
    session: str


def _normalized_graph_version() -> str:
    ver = FB_GRAPH_VERSION if FB_GRAPH_VERSION.startswith("v") else f"v{FB_GRAPH_VERSION}"
    if not re.fullmatch(r"v[0-9]{1,2}\.[0-9]{1,2}", ver):
        return "v22.0"
    return ver


def _graph_base() -> str:
    return f"https://graph.facebook.com/{_normalized_graph_version()}"


def _appsecret_proof(token: str) -> str:
    return hmac.new(
        FB_APP_SECRET.encode("utf-8"),
        token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _is_allowed_desktop_redirect(url: str) -> bool:
    """Only allow clean http://localhost:<port>/callback or http://127.0.0.1:<port>/callback."""
    if not url or any(c in url for c in (" ", "\t", "\r", "\n")):
        return False
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "http"
        and parsed.hostname in ("localhost", "127.0.0.1")
        and parsed.path == "/callback"
        and not parsed.query
        and not parsed.fragment
        and not parsed.username
        and not parsed.password
        and (port is None or 1024 <= port <= 65535)
    )


def _callback_url(request: Request) -> str:
    if PUBLIC_BASE_URL:
        return f"{PUBLIC_BASE_URL}/api/fb/callback"
    return str(request.base_url).rstrip("/") + "/api/fb/callback"


def _seal(payload: dict) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    compressed = zlib.compress(raw, level=9)
    return _FERNET.encrypt(compressed).decode("ascii")


def _unseal(token: str, max_age: int) -> dict:
    if not token or len(token) > 65536:
        raise HTTPException(status_code=400, detail="Invalid or expired token")
    try:
        decrypted = _FERNET.decrypt(token.encode("ascii"), ttl=max_age)
        try:
            decomp = zlib.decompressobj()
            raw = decomp.decompress(decrypted, 262144)
            if decomp.unconsumed_tail:
                raise ValueError("Decompressed payload exceeds size limit")
        except zlib.error:
            raw = decrypted
        return json.loads(raw.decode("utf-8"))
    except (InvalidToken, ValueError, UnicodeEncodeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid or expired token") from exc


def _prune_used_jtis() -> None:
    now = time.time()
    expired = [k for k, exp in _USED_JTIS.items() if exp < now]
    for k in expired:
        _USED_JTIS.pop(k, None)
    if len(_USED_JTIS) > 5000:
        for k in list(_USED_JTIS.keys())[: len(_USED_JTIS) - 5000]:
            _USED_JTIS.pop(k, None)


def _parse_graph(r: requests.Response) -> dict:
    try:
        data = r.json()
    except ValueError as exc:
        raise RuntimeError(f"Non-JSON response from Facebook (HTTP {r.status_code})") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"Unexpected response format from Facebook (HTTP {r.status_code})")
    if r.status_code != 200:
        err = data.get("error", {})
        msg = err.get("message") if isinstance(err, dict) else r.text
        raise RuntimeError(msg or f"Facebook API error HTTP {r.status_code}")
    return data


def _fetch_pages(long_token: str) -> list[dict]:
    pages: list[dict] = []
    next_url: str | None = f"{_graph_base()}/me/accounts"
    params: dict | None = {
        "fields": "id,name,access_token",
        "limit": "100",
        "appsecret_proof": _appsecret_proof(long_token),
    }
    headers = {"Authorization": f"Bearer {long_token}"}
    seen_urls: set[str] = set()

    while next_url and len(seen_urls) < 50:
        if next_url in seen_urls:
            break
        seen_urls.add(next_url)
        parsed = urlparse(next_url)
        if parsed.scheme != "https" or parsed.netloc.lower() != "graph.facebook.com":
            break
        if params is None:
            q = [(k, v) for k, v in parse_qsl(parsed.query) if k.lower() != "access_token"]
            if not any(k == "appsecret_proof" for k, _ in q):
                q.append(("appsecret_proof", _appsecret_proof(long_token)))
            url = urlunparse(parsed._replace(query=urlencode(q)))
            r = requests.get(url, headers=headers, timeout=30)
        else:
            r = requests.get(next_url, params=params, headers=headers, timeout=30)
            params = None

        data = _parse_graph(r)
        for p in data.get("data", []):
            if p.get("id") and p.get("name") and p.get("access_token"):
                pages.append(
                    {
                        "id": str(p["id"]),
                        "name": str(p["name"]),
                        "access_token": str(p["access_token"]),
                    }
                )
        paging = data.get("paging")
        next_url = paging.get("next") if isinstance(paging, dict) else None
    return pages


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "configured": bool(FB_APP_ID and FB_APP_SECRET),
        "signing_key_configured": _HAS_EXPLICIT_SIGNING_KEY,
        "public_base_url_configured": bool(PUBLIC_BASE_URL),
        "graph_version": _normalized_graph_version(),
    }


@app.get("/api/fb/login")
def fb_login(request: Request, redirect_uri: str, state: str):
    _enforce_rate_limit(request)
    if not (FB_APP_ID and FB_APP_SECRET):
        raise HTTPException(status_code=503, detail="Backend FB_APP_ID / FB_APP_SECRET not configured")
    if not _is_allowed_desktop_redirect(redirect_uri):
        raise HTTPException(status_code=400, detail="redirect_uri must be http://localhost:<port>/callback")
    if not state or len(state) > 256:
        raise HTTPException(status_code=400, detail="Invalid state")

    sealed_state = _seal({"desktop_redirect": redirect_uri, "client_state": state})
    ver = _normalized_graph_version()
    fb_oauth_url = f"https://www.facebook.com/{ver}/dialog/oauth?" + urlencode(
        {
            "client_id": FB_APP_ID,
            "redirect_uri": _callback_url(request),
            "state": sealed_state,
            "scope": SCOPES,
        }
    )
    return RedirectResponse(url=fb_oauth_url, status_code=302)


@app.get("/api/fb/callback")
def fb_callback(
    request: Request,
    state: str = "",
    code: str = "",
    error_description: str = "",
):
    _enforce_rate_limit(request)
    if not (FB_APP_ID and FB_APP_SECRET):
        raise HTTPException(status_code=503, detail="Backend FB_APP_ID / FB_APP_SECRET not configured")
    if not state:
        return HTMLResponse("<h2>Missing OAuth state</h2>", status_code=400)

    state_data = _unseal(state, max_age=STATE_TTL_SECONDS)
    desktop_redirect = state_data.get("desktop_redirect", "")
    client_state = state_data.get("client_state", "")
    if not _is_allowed_desktop_redirect(desktop_redirect):
        raise HTTPException(status_code=400, detail="Invalid desktop redirect")

    if error_description or not code:
        err_url = desktop_redirect + "?" + urlencode(
            {
                "state": client_state,
                "error_description": error_description or "ไม่ได้รับสิทธิ์จาก Facebook",
            }
        )
        return RedirectResponse(url=err_url, status_code=302)

    try:
        short_r = requests.post(
            f"{_graph_base()}/oauth/access_token",
            data={
                "client_id": FB_APP_ID,
                "client_secret": FB_APP_SECRET,
                "redirect_uri": _callback_url(request),
                "code": code,
            },
            timeout=30,
        )
        short_token = _parse_graph(short_r).get("access_token")
        if not short_token:
            raise RuntimeError("Missing short-lived access_token from Facebook")

        long_r = requests.post(
            f"{_graph_base()}/oauth/access_token",
            data={
                "grant_type": "fb_exchange_token",
                "client_id": FB_APP_ID,
                "client_secret": FB_APP_SECRET,
                "fb_exchange_token": short_token,
            },
            timeout=30,
        )
        long_token = _parse_graph(long_r).get("access_token")
        if not long_token:
            raise RuntimeError("Missing long-lived access_token from Facebook")
        pages = _fetch_pages(long_token)
    except Exception as exc:  # noqa: BLE001
        err_url = desktop_redirect + "?" + urlencode(
            {"state": client_state, "error_description": str(exc)}
        )
        return RedirectResponse(url=err_url, status_code=302)

    session_token = _seal({"jti": secrets.token_urlsafe(16), "pages": pages})
    ok_url = desktop_redirect + "?" + urlencode(
        {"state": client_state, "session": session_token}
    )
    return RedirectResponse(url=ok_url, status_code=302)


@app.post("/api/fb/claim")
def fb_claim(request: Request, body: ClaimRequest):
    _enforce_rate_limit(request)
    _prune_used_jtis()
    payload = _unseal(body.session, max_age=SESSION_TTL_SECONDS)
    jti = payload.get("jti", "")
    if not jti or jti in _USED_JTIS:
        raise HTTPException(status_code=400, detail="Session already claimed or invalid")
    _USED_JTIS[jti] = time.time() + SESSION_TTL_SECONDS + 10
    return {"pages": payload.get("pages", [])}
