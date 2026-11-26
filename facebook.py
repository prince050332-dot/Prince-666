"""Facebook Page connector (official Graph API). Tokens live in the OS keychain."""
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import mimetypes
from pathlib import Path
import random
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlparse, urlunparse
import webbrowser

import keyring
import keyring.errors
import requests

import config

PORT = 8765
REDIRECT_URI = f"http://localhost:{PORT}/callback"
SCOPES = "pages_show_list,pages_manage_posts,pages_read_engagement,pages_manage_engagement,read_insights"
SERVICE = "social_manager"
PLATFORM = "facebook"

MIN_FB_SCHEDULE_SECONDS = 10 * 60       # Facebook requires >= 10 minutes ahead
MAX_FB_SCHEDULE_SECONDS = 75 * 24 * 3600  # Facebook allows up to 75 days ahead
FB_REEL_MAX_SCHEDULE_DAYS = 29
MAX_FB_REEL_SCHEDULE_SECONDS = FB_REEL_MAX_SCHEDULE_DAYS * 24 * 3600  # Reels allow up to 29 days ahead

FEELING_ACTION_ID = "383634835006146"

# Official Facebook Graph API Open Graph action_type_id presets (Activity / Feeling)
ACTIVITY_ACTIONS: tuple[tuple[str, str, str], ...] = (
    ("", "ไม่ระบุกิจกรรม / ความรู้สึก", ""),
    (FEELING_ACTION_ID, "😊 รู้สึก (Feeling)", "รู้สึก"),
    ("520095228026772", "🎮 กำลังเล่น (Playing)", "กำลังเล่น"),
    ("383634671672829", "🎬 กำลังดู (Watching)", "กำลังดู"),
    ("383634868339476", "🎵 กำลังฟัง (Listening to)", "กำลังฟัง"),
    ("742120442490915", "🎉 กำลังเฉลิมฉลอง (Celebrating)", "กำลังเฉลิมฉลอง"),
    ("383634705006159", "🍽️ กำลังรับประทาน (Eating)", "กำลังรับประทาน"),
    ("383634741672822", "☕ กำลังดื่ม (Drinking)", "กำลังดื่ม"),
    ("383635058339457", "📖 กำลังอ่าน (Reading)", "กำลังอ่าน"),
    ("580961725273455", "✈️ กำลังเดินทางไป (Traveling to)", "กำลังเดินทางไป"),
    ("668012816568345", "🎫 กำลังเข้าร่วม (Attending)", "กำลังเข้าร่วม"),
)

# Official Facebook Graph API og_object_id presets for FEELS (383634835006146)
FEELING_PRESETS: tuple[tuple[str, str], ...] = (
    ("528297480516636", "😄 มีความสุข (Happy)"),
    ("308167675961412", "🤩 ตื่นเต้น (Excited)"),
    ("525497104142297", "🙏 โชคดี / อิ่มบุญ (Blessed)"),
    ("443609379021593", "💛 ขอบคุณ (Thankful)"),
    ("123103951186111", "🥰 รู้สึกดี / ถูกรัก (Loved)"),
    ("136050896551329", "🏆 ประสบความสำเร็จ (Accomplished)"),
    ("224113357712800", "🧭 พร้อมลุย (Adventurous)"),
)


def activity_action_short_label(action_id: str) -> str:
    clean = (action_id or "").strip()
    for aid, _display, short in ACTIVITY_ACTIONS:
        if aid == clean:
            return short
    return "กิจกรรม" if clean else ""


def extract_place_or_page_id(raw_input: str) -> str:
    """Extract a numeric Facebook Place/Page ID from a raw ID or Facebook Page/Place URL.

    Returns an empty string if empty; raises ValueError if the input cannot be parsed
    into a valid ASCII numeric Facebook Page/Place ID.
    """
    raw = (raw_input or "").strip()
    if not raw:
        return ""
    if raw.isascii() and raw.isdigit():
        return raw
    candidate = raw
    lower_cand = candidate.lower()
    if not lower_cand.startswith(("http://", "https://")) and (
        "facebook.com" in lower_cand or "fb.com" in lower_cand
    ):
        candidate = "https://" + candidate
    try:
        parsed = urlparse(candidate)
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("รูปแบบลิงก์หรือ Place/Page ID ไม่ถูกต้อง") from exc
    host = (parsed.hostname or "").lower()
    if not (host == "facebook.com" or host.endswith(".facebook.com") or host == "fb.com"):
        raise ValueError("กรุณากรอกเลข Place/Page ID (ตัวเลขอารบิก) หรือลิงก์ของ facebook.com เท่านั้น")
    qs = parse_qs(parsed.query)
    for key in ("id", "place_id", "page_id"):
        vals = qs.get(key)
        if vals:
            v = vals[0].strip()
            if v.isascii() and v.isdigit():
                return v
    segs = [s for s in parsed.path.split("/") if s]
    # e.g. /pages/Name/123456789012345 or /123456789012345
    for seg in reversed(segs):
        if seg.isascii() and seg.isdigit() and len(seg) >= 5:
            return seg
        # e.g. /p/Xbox-100064123456789 or /Name-123456789012345
        m_tail = re.search(r"-([0-9]{6,})$", seg)
        if m_tail:
            return m_tail.group(1)
    raise ValueError(
        "ไม่พบเลข Place/Page ID ในข้อความที่กรอก (กรุณาใส่เลข ID เช่น 1155726107773264 หรือใช้ปุ่มค้นหา)"
    )


def search_places(page_id: str, query: str, limit: int = 15) -> list[dict]:
    """Search Facebook Places / Pages via Graph API /pages/search using a connected Page token."""
    clean_pid = _validate_graph_id(page_id, "Page ID")
    q = (query or "").strip()
    if not q:
        raise ValueError("กรุณาพิมพ์ชื่อสถานที่ ชื่อเพจ หรือเลข ID ที่ต้องการค้นหา")
    token = _get_page_token(clean_pid)

    # If user pasted a direct numeric ID or extractable URL, inspect that node first
    try:
        direct_id = extract_place_or_page_id(q)
    except ValueError:
        direct_id = ""
    if direct_id:
        try:
            node = _graph_get(
                direct_id,
                token=token,
                params={"fields": "id,name,category,location,link"},
            )
            if node.get("id"):
                loc = node.get("location") if isinstance(node.get("location"), dict) else {}
                city_parts = [str(loc.get(k) or "").strip() for k in ("city", "country") if loc.get(k)]
                return [
                    {
                        "id": str(node["id"]),
                        "name": str(node.get("name") or direct_id),
                        "category": str(node.get("category") or ""),
                        "location": ", ".join(city_parts),
                        "link": str(node.get("link") or ""),
                    }
                ]
        except RuntimeError:
            pass

    resp = _graph_get(
        "pages/search",
        token=token,
        params={
            "q": q,
            "fields": "id,name,category,location,link",
            "limit": str(max(1, min(limit, 50))),
        },
    )
    out: list[dict] = []
    for item in resp.get("data", []):
        iid = item.get("id")
        if not iid:
            continue
        loc = item.get("location") if isinstance(item.get("location"), dict) else {}
        city_parts = [str(loc.get(k) or "").strip() for k in ("city", "country") if loc.get(k)]
        out.append(
            {
                "id": str(iid),
                "name": str(item.get("name") or iid),
                "category": str(item.get("category") or ""),
                "location": ", ".join(city_parts),
                "link": str(item.get("link") or ""),
            }
        )
    return out


class OAuthCancelled(RuntimeError):
    """Raised when the user cancels an in-progress OAuth login."""


def _graph_base() -> str:
    return f"https://graph.facebook.com/{config.get_graph_version()}"


def _graph_video_base() -> str:
    return f"https://graph-video.facebook.com/{config.get_graph_version()}"


def _appsecret_proof(token: str, *, use_proof: bool = True) -> str | None:
    if not use_proof or config.get_auth_mode() != "self_app":
        return None
    secret = config.get_fb_app_secret()
    if not secret or not token:
        return None
    return hmac.new(
        secret.encode("utf-8"),
        token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _parse_response(r: requests.Response) -> dict:
    try:
        data = r.json()
    except ValueError as exc:
        snippet = (r.text or "").strip()[:200]
        raise RuntimeError(
            f"Facebook API ตอบกลับไม่ใช่ JSON (HTTP {r.status_code}): {snippet}"
        ) from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"รูปแบบข้อมูลจาก Facebook API ไม่ถูกต้อง (HTTP {r.status_code})")
    if r.status_code != 200:
        err = data.get("error")
        if isinstance(err, dict):
            msg = err.get("error_user_msg") or err.get("message") or str(err)
        else:
            msg = str(err) if err else (r.text or f"HTTP {r.status_code}")
        raise RuntimeError(msg)
    return data


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _graph_get(
    path_or_url: str,
    token: str,
    params: dict | None = None,
    timeout: int = 30,
    *,
    use_proof: bool = True,
) -> dict:
    base_params = dict(params or {})
    proof = _appsecret_proof(token, use_proof=use_proof)

    if path_or_url.lower().startswith("http://"):
        raise RuntimeError("ปฏิเสธ URL แบบไม่เข้ารหัส (http://) สำหรับ Facebook Graph API")
    try:
        if path_or_url.startswith("https://"):
            parsed = urlparse(path_or_url)
            if parsed.netloc.lower() != "graph.facebook.com":
                raise RuntimeError(f"ปฏิเสธ URL ภายนอกที่ไม่ใช่ graph.facebook.com: {parsed.netloc}")
            q = [(k, v) for k, v in parse_qsl(parsed.query) if k.lower() != "access_token" and k != "appsecret_proof"]
            if proof:
                q.append(("appsecret_proof", proof))
            url = urlunparse(parsed._replace(query=urlencode(q)))
            r = requests.get(url, headers=_auth_headers(token), timeout=timeout)
        else:
            req_params = dict(base_params)
            if proof:
                req_params["appsecret_proof"] = proof
            url = f"{_graph_base()}/{path_or_url.lstrip('/')}"
            r = requests.get(url, params=req_params, headers=_auth_headers(token), timeout=timeout)
    except requests.RequestException as exc:
        raise RuntimeError(f"เชื่อมต่อ Facebook ไม่สำเร็จ (กรุณาตรวจสอบอินเทอร์เน็ต): {exc}") from exc

    try:
        return _parse_response(r)
    except RuntimeError as exc:
        if proof and "appsecret_proof" in str(exc).lower():
            return _graph_get(
                path_or_url,
                token,
                params=base_params,
                timeout=timeout,
                use_proof=False,
            )
        raise


def _graph_post(
    path: str,
    token: str | None = None,
    data: dict | None = None,
    files: dict | None = None,
    timeout: int = 60,
    base_url: str | None = None,
    *,
    use_proof: bool = True,
) -> dict:
    base_data = dict(data or {})
    payload = dict(base_data)
    headers: dict[str, str] = {}
    proof = None
    if token:
        headers.update(_auth_headers(token))
        proof = _appsecret_proof(token, use_proof=use_proof)
        if proof:
            payload["appsecret_proof"] = proof
    root = (base_url or _graph_base()).rstrip("/")
    url = f"{root}/{path.lstrip('/')}"
    try:
        r = requests.post(url, data=payload, files=files, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        raise RuntimeError(f"เชื่อมต่อ Facebook ไม่สำเร็จ (กรุณาตรวจสอบอินเทอร์เน็ต): {exc}") from exc
    try:
        return _parse_response(r)
    except RuntimeError as exc:
        if proof and "appsecret_proof" in str(exc).lower():
            if files:
                for val in files.values():
                    fobj = val[1] if isinstance(val, tuple) and len(val) >= 2 else val
                    if hasattr(fobj, "seek"):
                        fobj.seek(0)
            return _graph_post(
                path,
                token=token,
                data=base_data,
                files=files,
                timeout=timeout,
                base_url=base_url,
                use_proof=False,
            )
        raise


def _graph_delete(
    path: str,
    token: str,
    timeout: int = 30,
    *,
    use_proof: bool = True,
) -> dict:
    params: dict[str, str] = {}
    proof = _appsecret_proof(token, use_proof=use_proof)
    if proof:
        params["appsecret_proof"] = proof
    url = f"{_graph_base()}/{path.lstrip('/')}"
    try:
        r = requests.delete(url, params=params, headers=_auth_headers(token), timeout=timeout)
    except requests.RequestException as exc:
        raise RuntimeError(f"เชื่อมต่อ Facebook ไม่สำเร็จ (กรุณาตรวจสอบอินเทอร์เน็ต): {exc}") from exc
    try:
        return _parse_response(r)
    except RuntimeError as exc:
        if proof and "appsecret_proof" in str(exc).lower():
            return _graph_delete(path, token=token, timeout=timeout, use_proof=False)
        raise


def _wait_for_callback(
    state: str,
    *,
    expected_param: str = "code",
    auth_url: str | None = None,
    timeout: int = 180,
    cancel_event: threading.Event | None = None,
    open_browser: bool = True,
) -> str:
    result: dict[str, str] = {}
    done_event = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        timeout = 2.0

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return

            q = parse_qs(parsed.query)
            returned_state = q.get("state", [""])[0]
            state_ok = secrets.compare_digest(
                returned_state.encode("utf-8", "surrogatepass"),
                state.encode("utf-8", "surrogatepass"),
            )
            if state_ok and expected_param in q:
                result["value"] = q[expected_param][0]
                msg = "เชื่อมต่อสำเร็จ ปิดหน้านี้แล้วกลับไปที่โปรแกรมได้เลย"
                status_code = 200
            else:
                result["error"] = q.get(
                    "error_description",
                    q.get("error", ["ไม่ได้รับสิทธิ์หรือค่า state ไม่ตรงกัน"]),
                )[0]
                msg = "เชื่อมต่อไม่สำเร็จ กรุณากลับไปลองใหม่ในโปรแกรม"
                status_code = 400

            self.send_response(status_code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Pragma", "no-cache")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            body = (
                "<!doctype html><html><head><meta charset='utf-8'>"
                "<title>Prince Studio</title></head>"
                "<body style='font-family:sans-serif;text-align:center;padding:48px'>"
                f"<h2>{msg}</h2></body></html>"
            )
            self.wfile.write(body.encode("utf-8"))
            done_event.set()

        def log_message(self, *args):
            pass

    try:
        server = HTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as exc:
        raise RuntimeError(
            f"ไม่สามารถเปิดพอร์ต {PORT} เพื่อรับค่า OAuth ได้ (อาจมีโปรแกรมอื่นใช้งานอยู่): {exc}"
        ) from exc

    server.timeout = 0.5
    try:
        if open_browser and auth_url:
            webbrowser.open(auth_url)

        deadline = time.monotonic() + timeout
        while not done_event.is_set():
            if cancel_event and cancel_event.is_set():
                raise OAuthCancelled("ยกเลิกการเชื่อมต่อ Facebook แล้ว")
            if time.monotonic() >= deadline:
                raise RuntimeError("หมดเวลารอการล็อกอินในเบราว์เซอร์")
            server.handle_request()
    finally:
        server.server_close()

    if "value" not in result:
        raise RuntimeError(result.get("error", "ไม่ได้รับรหัสยืนยันจาก Facebook"))
    return result["value"]


def _wait_for_code(
    state: str,
    timeout: int = 180,
    cancel_event: threading.Event | None = None,
    open_browser: bool = True,
) -> str:
    ver = config.get_graph_version()
    url = f"https://www.facebook.com/{ver}/dialog/oauth?" + urlencode(
        {
            "client_id": config.get_fb_app_id(),
            "redirect_uri": REDIRECT_URI,
            "state": state,
            "scope": SCOPES,
        }
    )
    return _wait_for_callback(
        state,
        expected_param="code",
        auth_url=url,
        timeout=timeout,
        cancel_event=cancel_event,
        open_browser=open_browser,
    )


def _save_page_token(page_id: str, token: str) -> None:
    try:
        keyring.set_password(SERVICE, f"{PLATFORM}:{page_id}", token)
    except keyring.errors.KeyringError as exc:
        raise RuntimeError(f"ไม่สามารถบันทึก token ลง Keychain ของระบบได้: {exc}") from exc


def _get_page_token(page_id: str) -> str:
    try:
        token = keyring.get_password(SERVICE, f"{PLATFORM}:{page_id}")
    except keyring.errors.KeyringError as exc:
        raise RuntimeError(f"ไม่สามารถอ่าน token จาก Keychain ของระบบได้: {exc}") from exc
    if not token:
        raise RuntimeError("ไม่พบ token ของเพจนี้ ลองกดเชื่อมต่อ Facebook ใหม่")
    return token


def verify_app_credentials(app_id: str, app_secret: str) -> bool:
    """Verify FB_APP_ID and FB_APP_SECRET via Graph API client_credentials grant."""
    app_id = (app_id or "").strip()
    app_secret = (app_secret or "").strip()
    if not app_id or not app_secret:
        raise RuntimeError("กรุณากรอกทั้ง FB App ID และ FB App Secret ก่อนทดสอบ")
    resp = _graph_post(
        "oauth/access_token",
        data={
            "client_id": app_id,
            "client_secret": app_secret,
            "grant_type": "client_credentials",
        },
        timeout=20,
    )
    if not resp.get("access_token"):
        raise RuntimeError("ไม่พบ access_token ตอบกลับจาก Facebook")
    return True


def _fetch_all_pages(user_token: str, *, use_proof: bool = True) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    next_url: str | None = "me/accounts"
    params: dict | None = {"fields": "id,name,access_token", "limit": "100"}
    seen_urls: set[str] = set()

    while next_url and len(seen_urls) < 50:
        if next_url in seen_urls:
            break
        seen_urls.add(next_url)
        page_resp = _graph_get(next_url, token=user_token, params=params, use_proof=use_proof)
        params = None
        for p in page_resp.get("data", []):
            pid = p.get("id")
            pname = p.get("name")
            ptoken = p.get("access_token")
            if pid and pname and ptoken:
                _save_page_token(str(pid), str(ptoken))
                out.append((str(pid), str(pname)))
        paging = page_resp.get("paging")
        next_url = paging.get("next") if isinstance(paging, dict) else None
    return out


def connect_with_token(raw_token: str, *, exchange_long_lived: bool = False) -> list[tuple[str, str]]:
    """Connect directly using a Page Access Token or User Access Token (no App Secret needed)."""
    token = (raw_token or "").strip()
    if not token:
        raise RuntimeError("กรุณาวาง Access Token ก่อนเชื่อมต่อ")

    if exchange_long_lived:
        app_id = config.get_fb_app_id()
        app_secret = config.get_fb_app_secret()
        if app_id and app_secret:
            try:
                long_resp = _graph_post(
                    "oauth/access_token",
                    data={
                        "grant_type": "fb_exchange_token",
                        "client_id": app_id,
                        "client_secret": app_secret,
                        "fb_exchange_token": token,
                    },
                    timeout=20,
                )
                if long_resp.get("access_token"):
                    token = str(long_resp["access_token"])
            except RuntimeError:
                pass

    # Try fetching managed pages first (in case user pasted a User Access Token)
    accounts_succeeded_empty = False
    try:
        pages = _fetch_all_pages(token, use_proof=False)
        if pages:
            return pages
        accounts_succeeded_empty = True
    except RuntimeError:
        pass

    if accounts_succeeded_empty:
        raise RuntimeError(
            "Token นี้เป็น User Token แต่ไม่พบเพจที่จัดการ (หรือยังไม่ได้ติ๊กสิทธิ์ pages_show_list ใน Graph API Explorer)"
        )

    # Otherwise treat as a direct Page Access Token and inspect /me
    me = _graph_get(
        "me",
        token=token,
        params={"fields": "id,name,category", "metadata": "1"},
        use_proof=False,
    )
    meta = me.get("metadata")
    meta_type = str(meta.get("type", "")).lower() if isinstance(meta, dict) else ""
    if meta_type == "user":
        raise RuntimeError(
            "Token นี้เป็น User Token ส่วนตัว ไม่ใช่ Page Token (กรุณาเลือกเพจในช่อง User or Page ของ Graph API Explorer)"
        )
    pid = me.get("id")
    pname = me.get("name")
    if not (pid and pname):
        raise RuntimeError("Token นี้ไม่สามารถดึงข้อมูลเพจได้ กรุณาตรวจสอบสิทธิ์ของ Token")
    _save_page_token(str(pid), token)
    return [(str(pid), str(pname))]


def _validate_backend_url(backend_url: str) -> str:
    """Ensure remote backend URLs use HTTPS (HTTP allowed only for localhost/127.0.0.1)."""
    base = (backend_url or "").strip().rstrip("/")
    if not base or any(c in base for c in (" ", "\t", "\r", "\n")):
        raise RuntimeError("Backend URL ไม่ถูกต้อง ต้องขึ้นต้นด้วย https://")
    try:
        parsed = urlparse(base)
        _ = parsed.port
    except ValueError as exc:
        raise RuntimeError("Backend URL ไม่ถูกต้องหรือมีหมายเลขพอร์ตไม่ถูกต้อง") from exc
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or not host or parsed.username or parsed.password:
        raise RuntimeError("Backend URL ไม่ถูกต้อง ต้องขึ้นต้นด้วย https://")
    if parsed.scheme == "http" and host not in ("localhost", "127.0.0.1"):
        raise RuntimeError(
            "เพื่อความปลอดภัย Backend Server ภายนอกต้องใช้ https:// เท่านั้น (อนุญาต http:// เฉพาะ localhost)"
        )
    return base


def _validate_graph_id(raw_id: str, label: str = "ID") -> str:
    clean = (raw_id or "").strip()
    if not clean or not re.fullmatch(r"[A-Za-z0-9_]+", clean):
        raise ValueError(f"{label} ไม่ถูกต้อง (ต้องเป็นตัวอักษร ตัวเลข หรือ _ เท่านั้น)")
    return clean


def check_backend_health(backend_url: str) -> dict:
    """Check if the One-Click OAuth Backend Server is reachable and configured."""
    base = _validate_backend_url(backend_url)
    try:
        r = requests.get(f"{base}/api/health", timeout=15)
    except requests.RequestException as exc:
        raise RuntimeError(f"ไม่สามารถเชื่อมต่อ Backend Server ได้: {exc}") from exc
    data = _parse_response(r)
    if not data.get("ok"):
        raise RuntimeError("Backend Server ตอบกลับสถานะไม่พร้อมใช้งาน")
    return data


def connect_via_backend(
    backend_url: str,
    cancel_event: threading.Event | None = None,
) -> list[tuple[str, str]]:
    """One-Click OAuth via external Backend Server (no App ID/Secret needed on client)."""
    base = _validate_backend_url(backend_url)

    state = secrets.token_urlsafe(24)
    login_url = f"{base}/api/fb/login?" + urlencode(
        {"redirect_uri": REDIRECT_URI, "state": state}
    )
    session_code = _wait_for_callback(
        state,
        expected_param="session",
        auth_url=login_url,
        cancel_event=cancel_event,
    )

    try:
        r = requests.post(
            f"{base}/api/fb/claim",
            json={"session": session_code},
            timeout=30,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"ไม่สามารถดึงข้อมูลเพจจาก Backend Server ได้: {exc}") from exc

    data = _parse_response(r)
    out: list[tuple[str, str]] = []
    for p in data.get("pages", []):
        pid = p.get("id")
        pname = p.get("name")
        ptoken = p.get("access_token")
        if pid and pname and ptoken:
            _save_page_token(str(pid), str(ptoken))
            out.append((str(pid), str(pname)))
    return out


def connect(cancel_event: threading.Event | None = None) -> list[tuple[str, str]]:
    """OAuth login -> returns list of (page_id, page_name) and stores page tokens."""
    if config.get_auth_mode() == "backend_oauth":
        backend_url = config.get_backend_url()
        if not backend_url:
            raise RuntimeError(
                "เปิดโหมด One-Click Backend ไว้แต่ยังไม่ได้ใส่ Backend Server URL ในหน้าตั้งค่า"
            )
        return connect_via_backend(backend_url, cancel_event=cancel_event)

    app_id = config.get_fb_app_id()
    app_secret = config.get_fb_app_secret()
    if not (app_id and app_secret):
        raise RuntimeError(
            "ยังไม่ได้ตั้งค่า FB_APP_ID / FB_APP_SECRET กรุณาตั้งค่าในปุ่ม 'ตั้งค่า' หรือไฟล์ .env"
        )

    code = _wait_for_code(secrets.token_urlsafe(24), cancel_event=cancel_event)

    short_resp = _graph_post(
        "oauth/access_token",
        data={
            "client_id": app_id,
            "client_secret": app_secret,
            "redirect_uri": REDIRECT_URI,
            "code": code,
        },
    )
    short_token = short_resp.get("access_token")
    if not short_token:
        raise RuntimeError("ไม่พบ short-lived access_token จาก Facebook")

    long_resp = _graph_post(
        "oauth/access_token",
        data={
            "grant_type": "fb_exchange_token",
            "client_id": app_id,
            "client_secret": app_secret,
            "fb_exchange_token": short_token,
        },
    )
    long_token = long_resp.get("access_token")
    if not long_token:
        raise RuntimeError("ไม่พบ long-lived access_token จาก Facebook")

    return _fetch_all_pages(long_token)


def validate_fb_schedule_time(scheduled_dt: datetime) -> int:
    """Validate and convert datetime to Unix timestamp for Facebook native scheduling."""
    dt = scheduled_dt
    if dt.tzinfo is None:
        dt = dt.astimezone()
    now_ts = int(datetime.now(timezone.utc).timestamp())
    target_ts = int(dt.astimezone(timezone.utc).timestamp())
    delta = target_ts - now_ts
    if delta < MIN_FB_SCHEDULE_SECONDS:
        raise ValueError(
            "Facebook Native Schedule ต้องตั้งเวลาล่วงหน้าอย่างน้อย 10 นาที "
            "(หากต้องการเร็วกว่านี้ ให้เลือกโหมด 'คิวในเครื่อง')"
        )
    if delta > MAX_FB_SCHEDULE_SECONDS:
        raise ValueError("Facebook Native Schedule ตั้งเวลาล่วงหน้าได้สูงสุดไม่เกิน 75 วัน")
    return target_ts


def validate_fb_reel_schedule_time(scheduled_dt: datetime) -> int:
    """Validate and convert datetime to Unix timestamp for Facebook Reels scheduling (10 min .. 29 days)."""
    dt = scheduled_dt
    if dt.tzinfo is None:
        dt = dt.astimezone()
    now_ts = int(datetime.now(timezone.utc).timestamp())
    target_ts = int(dt.astimezone(timezone.utc).timestamp())
    delta = target_ts - now_ts
    if delta < MIN_FB_SCHEDULE_SECONDS:
        raise ValueError(
            "Facebook Reels ต้องตั้งเวลาล่วงหน้าอย่างน้อย 10 นาที "
            "(หากต้องการเร็วกว่านี้ ให้เลือกโหมด 'คิวในเครื่อง')"
        )
    if delta > MAX_FB_REEL_SCHEDULE_SECONDS:
        raise ValueError(
            "Facebook Reels ตั้งเวลาบนเซิร์ฟเวอร์ Facebook ได้สูงสุดไม่เกิน 29 วัน "
            "(หากต้องการตั้งเกิน 29 วัน ให้เลือกโหมด 'คิวในเครื่อง')"
        )
    return target_ts


def _upload_reel_binary(
    upload_url: str,
    token: str,
    video_file: Path,
    *,
    timeout: int = 300,
) -> dict:
    """Step 2 of Meta Reels API: stream raw video bytes to `https://rupload.facebook.com/...`."""
    raw_url = (upload_url or "").strip()
    if not raw_url.startswith("https://"):
        raise RuntimeError("ปฏิเสธ upload_url ของ Reels ที่ไม่ใช่ https://")
    parsed = urlparse(raw_url)
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or not (host == "rupload.facebook.com" or host.endswith(".rupload.facebook.com"))
    ):
        raise RuntimeError(f"ปฏิเสธ upload_url ภายนอกที่ไม่ใช่ rupload.facebook.com: {host or raw_url}")
    if not video_file.is_file():
        raise RuntimeError(f"ไม่พบไฟล์วิดีโอสำหรับ Reels: {video_file}")
    file_size = video_file.stat().st_size
    if file_size <= 0:
        raise ValueError("ไฟล์วิดีโอว่างเปล่า (0 bytes) ไม่สามารถอัปโหลดเป็น Reels ได้")

    headers = {
        "Authorization": f"OAuth {token}",
        "offset": "0",
        "file_size": str(file_size),
        "Content-Type": "application/octet-stream",
    }
    try:
        with video_file.open("rb") as fh:
            r = requests.post(raw_url, data=fh, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        raise RuntimeError(f"อัปโหลดไฟล์วิดีโอขึ้น rupload.facebook.com ไม่สำเร็จ: {exc}") from exc

    resp = _parse_response(r)
    if resp.get("success") is False:
        raise RuntimeError(f"rupload.facebook.com ปฏิเสธไฟล์วิดีโอ Reels: {resp}")
    return resp


def post_reel_to_page(
    page_id: str,
    video_path: str,
    description: str = "",
    *,
    scheduled_time: datetime | None = None,
) -> str:
    """Publish or schedule a Facebook Reel via Meta's official 3-step `/{page_id}/video_reels` API."""
    clean_pid = _validate_graph_id(page_id, "Page ID")
    raw_video = (video_path or "").strip()
    if not raw_video:
        raise ValueError("กรุณาแนบไฟล์วิดีโอสำหรับโพสต์ Reels")
    vp = Path(raw_video)
    if not vp.is_file():
        raise RuntimeError(f"ไม่พบไฟล์วิดีโอสำหรับ Reels: {raw_video}")
    if vp.stat().st_size <= 0:
        raise ValueError("ไฟล์วิดีโอว่างเปล่า (0 bytes) ไม่สามารถอัปโหลดเป็น Reels ได้")

    schedule_ts: int | None = None
    if scheduled_time is not None:
        schedule_ts = validate_fb_reel_schedule_time(scheduled_time)

    token = _get_page_token(clean_pid)

    # Step 1: Initialize upload session
    init_resp = _graph_post(
        f"{clean_pid}/video_reels",
        token=token,
        data={"upload_phase": "start"},
        timeout=60,
    )
    video_id = str(init_resp.get("video_id") or "").strip()
    upload_url = str(init_resp.get("upload_url") or "").strip()
    if not video_id or not upload_url:
        raise RuntimeError("Facebook ไม่ส่งคืน video_id หรือ upload_url ในขั้นตอนเริ่มอัปโหลด Reels")

    # Step 2: Binary upload to rupload.facebook.com
    _upload_reel_binary(upload_url, token, vp, timeout=300)

    # Step 3: Finish & publish (or schedule)
    finish_data: dict[str, str] = {
        "upload_phase": "finish",
        "video_id": video_id,
        "video_state": "SCHEDULED" if schedule_ts is not None else "PUBLISHED",
    }
    clean_desc = (description or "").strip()
    if clean_desc:
        finish_data["description"] = clean_desc
    if schedule_ts is not None:
        finish_data["scheduled_publish_time"] = str(schedule_ts)

    finish_resp = _graph_post(
        f"{clean_pid}/video_reels",
        token=token,
        data=finish_data,
        timeout=90,
    )
    if finish_resp.get("success") is False:
        raise RuntimeError("Facebook ปฏิเสธการเผยแพร่ Reels ในขั้นตอนสุดท้าย (upload_phase=finish)")
    post_id = str(finish_resp.get("post_id") or finish_resp.get("id") or video_id).strip()
    return post_id


def _upload_unpublished_photo(page_id: str, token: str, image_path: str) -> str:
    p = Path(image_path)
    if not p.is_file():
        raise RuntimeError(f"ไม่พบไฟล์รูปภาพ: {image_path}")
    mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    with p.open("rb") as fh:
        resp = _graph_post(
            f"{page_id}/photos",
            token=token,
            data={"published": "false"},
            files={"source": (p.name, fh, mime)},
            timeout=90,
        )
    media_id = resp.get("id")
    if not media_id:
        raise RuntimeError(f"อัปโหลดรูปภาพไม่สำเร็จ ({p.name})")
    return str(media_id)


def _is_token_auth_error(exc: Exception) -> bool:
    """Return True only for fatal token/credential errors where retries/fallbacks cannot help."""
    err_low = str(exc).lower()
    return any(
        k in err_low
        for k in (
            "access token",
            "session has expired",
            "error validating",
            "ไม่พบ token",
            "(#190)",
        )
    )


def post_to_page(
    page_id: str,
    message: str,
    *,
    link: str = "",
    image_paths: list[str] | None = None,
    video_path: str = "",
    is_reel: bool = False,
    scheduled_time: datetime | None = None,
    place_id: str = "",
    og_action_type_id: str = "",
    og_object_id: str = "",
    og_phrase: str = "",
) -> str:
    """Post text, link, single photo, multi-photo, video, or Reel to a Facebook Page.

    Supports optional Facebook native scheduling (`scheduled_time`) and Check-in
    (`place_id`). Legacy Open Graph Feeling/Activity params (`og_*`) are accepted
    for backward compatibility with old queued jobs but ignored because Meta Graph
    API deprecated them on Page posts.
    Returns the created remote post/photo/video/reel ID.
    """
    _ = (og_action_type_id, og_object_id, og_phrase)
    page_id = _validate_graph_id(page_id, "Page ID")
    images = [p for p in (image_paths or []) if p]
    video = (video_path or "").strip()
    link = (link or "").strip()
    message = (message or "").strip()
    clean_place = extract_place_or_page_id(place_id) if (place_id or "").strip() else ""

    if images and video:
        raise ValueError(
            "Facebook API ไม่รองรับการแนบทั้งรูปภาพและวิดีโอในโพสต์เดียวกัน กรุณาเลือกอย่างใดอย่างหนึ่ง"
        )

    if is_reel:
        if images:
            raise ValueError("โหมด Facebook Reels รองรับเฉพาะไฟล์วิดีโอเท่านั้น (ไม่สามารถแนบรูปภาพได้)")
        if not video:
            raise ValueError("กรุณาแนบไฟล์วิดีโอก่อนโพสต์ในโหมด Facebook Reels")
        reel_desc = message
        if link:
            reel_desc = f"{reel_desc}\n\n{link}" if reel_desc else link
        return post_reel_to_page(
            page_id,
            video,
            description=reel_desc,
            scheduled_time=scheduled_time,
        )

    token = _get_page_token(page_id)

    schedule_ts: int | None = None
    if scheduled_time is not None:
        schedule_ts = validate_fb_schedule_time(scheduled_time)

    def _attach_context_fields(payload: dict[str, str]) -> None:
        if clean_place:
            payload["place"] = clean_place

    # Video post (uploaded directly to /{page_id}/videos; optional place check-in supported where Meta allows)
    if video:
        vp = Path(video)
        if not vp.is_file():
            raise RuntimeError(f"ไม่พบไฟล์วิดีโอ: {video}")
        desc = message
        if link:
            desc = f"{desc}\n\n{link}" if desc else link
        data_v: dict[str, str] = {}
        if desc:
            data_v["description"] = desc
        _attach_context_fields(data_v)
        if schedule_ts is not None:
            data_v["published"] = "false"
            data_v["scheduled_publish_time"] = str(schedule_ts)
        mime = mimetypes.guess_type(vp.name)[0] or "video/mp4"
        with vp.open("rb") as fh:
            resp = _graph_post(
                f"{page_id}/videos",
                token=token,
                data=data_v,
                files={"source": (vp.name, fh, mime)},
                timeout=300,
                base_url=_graph_video_base(),
            )
        vid = resp.get("id")
        if not vid:
            raise RuntimeError("Facebook ไม่ส่งคืน video_id หลังอัปโหลดวิดีโอ")
        return str(vid)

    # Single photo post
    if len(images) == 1:
        p = Path(images[0])
        if not p.is_file():
            raise RuntimeError(f"ไม่พบไฟล์รูปภาพ: {images[0]}")
        caption = message
        if link:
            caption = f"{caption}\n\n{link}" if caption else link
        data = {}
        if caption:
            data["caption"] = caption
        if clean_place:
            data["place"] = clean_place
        if schedule_ts is not None:
            data["published"] = "false"
            data["scheduled_publish_time"] = str(schedule_ts)
        mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        with p.open("rb") as fh:
            resp = _graph_post(
                f"{page_id}/photos",
                token=token,
                data=data,
                files={"source": (p.name, fh, mime)},
                timeout=90,
            )
        post_id = resp.get("post_id") or resp.get("id")
        if not post_id:
            raise RuntimeError("Facebook ไม่ส่งคืน post_id หลังอัปโหลดรูปภาพ")
        return str(post_id)

    # Multi-photo post (upload unpublished -> attach to /feed)
    if len(images) > 1:
        media_ids = [_upload_unpublished_photo(page_id, token, img) for img in images]
        final_msg = message
        if link:
            final_msg = f"{final_msg}\n\n{link}" if final_msg else link
        data = {}
        if final_msg:
            data["message"] = final_msg
        for idx, mid in enumerate(media_ids):
            data[f"attached_media[{idx}]"] = json.dumps({"media_fbid": mid})
        _attach_context_fields(data)
        if schedule_ts is not None:
            data["published"] = "false"
            data["scheduled_publish_time"] = str(schedule_ts)
        resp = _graph_post(f"{page_id}/feed", token=token, data=data, timeout=60)
        post_id = resp.get("id")
        if not post_id:
            raise RuntimeError("Facebook ไม่ส่งคืน post_id หลังโพสต์รูปภาพ")
        return str(post_id)

    # Text / Link / Check-in feed post
    if not message and not link and not clean_place:
        raise ValueError("ต้องระบุข้อความ ลิงก์ หรือเช็คอินอย่างน้อย 1 อย่าง")
    data = {}
    if message:
        data["message"] = message
    if link:
        data["link"] = link
    _attach_context_fields(data)
    if schedule_ts is not None:
        data["published"] = "false"
        data["scheduled_publish_time"] = str(schedule_ts)
    resp = _graph_post(f"{page_id}/feed", token=token, data=data, timeout=30)
    post_id = resp.get("id")
    if not post_id:
        raise RuntimeError("Facebook ไม่ส่งคืน post_id")
    return str(post_id)


def post_text(page_id: str, message: str) -> str:
    """Backward-compatible helper for posting plain text."""
    return post_to_page(page_id, message)


def edit_post(
    page_id: str,
    post_id: str,
    new_message: str,
    *,
    is_video: bool | None = None,
) -> bool:
    """Edit the text/caption of an already-published Facebook Page post in-place.

    Preserves Post ID, likes, comments, media, and check-in. Uses `description` for
    Video nodes (`is_video=True` or numeric-only ID) and `message` for Feed/Photo
    posts (`pageId_postId`), falling back to the alternate text field on non-token
    errors. Meta Graph API does not allow changing attached photos/videos on already-
    published posts via API.
    """
    page_id = _validate_graph_id(page_id, "Page ID")
    clean_post_id = _validate_graph_id(post_id, "Post ID")
    clean_msg = (new_message or "").strip()
    if not clean_msg:
        raise ValueError("ข้อความใหม่ต้องไม่ว่าง")
    token = _get_page_token(page_id)

    if is_video is True:
        primary_field = "description"
    elif is_video is False:
        primary_field = "message"
    else:
        primary_field = (
            "description"
            if ("_" not in clean_post_id and clean_post_id.isascii() and clean_post_id.isdigit())
            else "message"
        )
    alt_field = "message" if primary_field == "description" else "description"
    try:
        _graph_post(
            clean_post_id,
            token=token,
            data={primary_field: clean_msg},
            timeout=30,
        )
    except RuntimeError as primary_exc:
        if _is_token_auth_error(primary_exc):
            raise
        try:
            _graph_post(
                clean_post_id,
                token=token,
                data={alt_field: clean_msg},
                timeout=30,
            )
        except RuntimeError:
            raise primary_exc from None
    return True


def list_page_posts(page_id: str, limit: int = 20) -> list[dict]:
    """Fetch recent published posts for a Facebook Page."""
    page_id = _validate_graph_id(page_id, "Page ID")
    token = _get_page_token(page_id)
    resp = _graph_get(
        f"{page_id}/posts",
        token=token,
        params={
            "fields": "id,message,created_time,permalink_url",
            "limit": str(max(1, min(limit, 50))),
        },
    )
    out: list[dict] = []
    for p in resp.get("data", []):
        pid = p.get("id")
        if not pid:
            continue
        raw_msg = str(p.get("message") or "")
        out.append(
            {
                "id": str(pid),
                "message": raw_msg or "(ไม่มีข้อความ / โพสต์รูปภาพ)",
                "raw_message": raw_msg,
                "created_time": str(p.get("created_time") or ""),
                "permalink_url": str(p.get("permalink_url") or ""),
            }
        )
    return out


def extract_post_id_from_input(page_id: str, raw_input: str) -> tuple[str, bool]:
    """Parse a raw Post ID or Facebook post URL into (post_id_or_pfbid, needs_api_lookup).

    Supports:
      - Direct IDs: `123456_789012`, `789012`
      - `permalink.php?story_fbid=<fbid>&id=<pid>`
      - `/posts/<id_or_pfbid>`
      - `/videos/<id>`, `/reel/<id>`, `/reels/<id>`
      - `/photo/?fbid=<id>` or `/photos/.../<id>`
    """
    raw = (raw_input or "").strip()
    if not raw:
        raise ValueError("กรุณาวางลิงก์โพสต์หรือ Post ID")

    low = raw.lower()
    is_url = low.startswith(("http://", "https://")) or any(
        dom in low for dom in ("facebook.com/", "fb.com/", "fb.watch/")
    )

    # Direct post/video/photo ID or bare pfbid (not a URL)
    if not is_url:
        clean_id = _validate_graph_id(raw, "Post ID")
        if clean_id.lower().startswith("pfbid"):
            return clean_id, True
        return clean_id, False

    if low.startswith("http://"):
        url = "https://" + raw[7:]
    elif low.startswith("https://"):
        url = raw
    else:
        url = f"https://{raw}"
    if not _is_facebook_host(url):
        raise ValueError("ลิงก์โพสต์ต้องเป็นโดเมนของ Facebook เท่านั้น")
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    path = parsed.path.rstrip("/")

    # 1) permalink.php?story_fbid=...&id=...
    if "story_fbid" in qs:
        fbid = qs["story_fbid"][0].strip()
        pid = qs.get("id", [page_id])[0].strip() or page_id
        if fbid.lower().startswith("pfbid"):
            return fbid, True
        return (f"{pid}_{fbid}" if pid and "_" not in fbid else fbid), False

    # 2) photo/?fbid=... or watch/?v=...
    if "fbid" in qs:
        fbid = qs["fbid"][0].strip()
        if fbid.lower().startswith("pfbid"):
            return fbid, True
        return fbid, False
    if "v" in qs and qs["v"][0].strip().isascii() and qs["v"][0].strip().isdigit():
        return qs["v"][0].strip(), False

    # 3) /posts/<id_or_pfbid> or /posts/<seo-slug>/<id_or_pfbid>
    m_post = re.search(r"/posts/(.+)$", path)
    if m_post:
        segments = [s for s in m_post.group(1).split("/") if s]
        for seg in segments:
            if seg.lower().startswith("pfbid"):
                return seg, True
        for seg in reversed(segments):
            if re.fullmatch(r"[0-9]+(?:_[0-9]+)?", seg):
                if "_" in seg or not page_id:
                    return seg, False
                return f"{page_id}_{seg}", False

    # 4) /videos/<id> or /reel/<id> or /reels/<id>
    m_vid = re.search(r"/(?:videos|reel|reels)/(?:[^/]+/)?([0-9]+)", path)
    if m_vid:
        return m_vid.group(1), False

    # 5) /photos/.../<numeric_id>
    m_photo = re.search(r"/photos/(?:[^/]+/)?([0-9]+)", path)
    if m_photo:
        return m_photo.group(1), False

    # 6) Any pfbid anywhere in the URL
    m_pfbid = re.search(r"(pfbid[0-9A-Za-z]+)", raw)
    if m_pfbid:
        return m_pfbid.group(1), True

    # 7) Short / share links (/share/p/..., /share/v/..., /share/r/..., fb.watch/...)
    host = (parsed.hostname or "").lower()
    if host.endswith("fb.watch") or re.search(r"/share/(?:[pvr]/)?[^/?#]+", path):
        return url, True

    raise ValueError(
        "ไม่สามารถแกะ Post ID จากลิงก์นี้ได้ ลองกด 'โหลดโพสต์ล่าสุด' แล้วคลิกเลือกโพสต์แทน"
    )


def _is_facebook_host(url: str) -> bool:
    if not url or any(c in url for c in (" ", "\t", "\r", "\n")):
        return False
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError:
        return False
    if parsed.scheme != "https" or parsed.username or parsed.password or port not in (None, 443):
        return False
    host = (parsed.hostname or "").lower()
    return host in ("facebook.com", "fb.com", "fb.watch") or host.endswith(
        (".facebook.com", ".fb.com", ".fb.watch")
    )


def _resolve_facebook_redirect(start_url: str, max_hops: int = 5) -> str:
    """Follow redirects hop-by-hop using GET (stream=True), allowing only HTTPS Facebook hosts."""
    cur = start_url
    if cur.lower().startswith("http://"):
        cur = "https://" + cur[7:]
    for _ in range(max_hops):
        if not _is_facebook_host(cur):
            break
        r = requests.get(
            cur,
            allow_redirects=False,
            stream=True,
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        try:
            loc = r.headers.get("Location") or r.headers.get("location")
            r_final = getattr(r, "url", "") or ""
        finally:
            r.close()
        if loc:
            nxt = urljoin(cur, loc)
            if not _is_facebook_host(nxt) or nxt == cur:
                break
            cur = nxt
        elif r_final and r_final != cur and _is_facebook_host(r_final):
            cur = r_final
            break
        else:
            break
    return cur


def resolve_post_id_for_page(page_id: str, raw_input: str) -> str:
    """Resolve a Facebook post URL or ID to a Graph API Post ID for `page_id`."""
    extracted, needs_lookup = extract_post_id_from_input(page_id, raw_input)
    if not needs_lookup:
        return extracted

    # Upgrade http:// share links to https:// and resolve redirects strictly on HTTPS Facebook hosts
    if extracted.lower().startswith("http://"):
        extracted = "https://" + extracted[7:]
    if extracted.startswith("https://") and _is_facebook_host(extracted):
        try:
            final_url = _resolve_facebook_redirect(extracted)
            if final_url and final_url != extracted and _is_facebook_host(final_url):
                ext2, lookup2 = extract_post_id_from_input(page_id, final_url)
                if not lookup2:
                    return ext2
                if not ext2.startswith(("http://", "https://")):
                    extracted = ext2
        except (requests.RequestException, ValueError):
            pass

    if extracted.startswith(("http://", "https://")):
        raise ValueError(
            "ลิงก์สั้นนี้ไม่สามารถแปลงเป็น Post ID ได้โดยตรง กรุณากด 'โหลดโพสต์ล่าสุด' แล้วคลิกเลือกโพสต์จากรายการ"
        )

    # For pfbid tokens, match against the page's recent posts permalink_url
    posts = list_page_posts(page_id, limit=50)
    needle = extracted.lower()
    for p in posts:
        if needle in (p.get("permalink_url") or "").lower():
            return p["id"]
    # Fallback: Graph API also accepts `{page_id}_{pfbid}` on modern versions
    return f"{page_id}_{extracted}" if page_id else extracted


COMMENT_IMG_SEP = " ||| "


def split_comment_spec(raw_spec: str) -> tuple[str, str]:
    """Split an encoded comment spec 'text ||| image_path' into (text, image_path)."""
    raw = raw_spec or ""
    if COMMENT_IMG_SEP in raw:
        text_part, img_part = raw.split(COMMENT_IMG_SEP, 1)
        return text_part.strip(), img_part.strip()
    stripped = raw.strip()
    if stripped.startswith("|||"):
        return "", stripped[3:].strip()
    return stripped, ""


def format_comment_spec(text: str, image_path: str = "") -> str:
    clean_t = (text or "").strip()
    clean_img = (image_path or "").strip()
    if clean_img:
        return f"{clean_t}{COMMENT_IMG_SEP}{clean_img}"
    return clean_t


def comment_on_post(
    page_id: str,
    post_id: str,
    message: str = "",
    *,
    image_path: str = "",
) -> str:
    """Post a single comment (text, image, or text+image) on a Facebook post as the Page."""
    page_id = _validate_graph_id(page_id, "Page ID")
    clean_post_id = _validate_graph_id(post_id, "Post ID")
    clean_msg, spec_img = split_comment_spec(message)
    clean_img = (image_path or spec_img or "").strip()
    if not clean_msg and not clean_img:
        raise ValueError("ต้องระบุข้อความคอมเมนต์หรือรูปภาพแนบอย่างน้อย 1 อย่าง")
    token = _get_page_token(page_id)
    data = {"message": clean_msg} if clean_msg else {}
    if clean_img:
        p = Path(clean_img)
        if not p.is_file():
            raise RuntimeError(f"ไม่พบไฟล์รูปสำหรับคอมเมนต์: {clean_img}")
        mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        with p.open("rb") as fh:
            resp = _graph_post(
                f"{clean_post_id}/comments",
                token=token,
                data=data,
                files={"source": (p.name, fh, mime)},
                timeout=90,
            )
    else:
        resp = _graph_post(
            f"{clean_post_id}/comments",
            token=token,
            data=data,
            timeout=30,
        )
    cid = resp.get("id")
    if not cid:
        raise RuntimeError("Facebook ไม่ส่งคืน comment_id")
    return str(cid)


def post_multiple_comments(
    page_id: str,
    post_id: str,
    comments: list[str],
    *,
    delay_seconds: float = 1.5,
    cancel_event: threading.Event | None = None,
) -> list[tuple[int, str, str, str | None]]:
    """Post multiple comments sequentially on a post.

    Each item in `comments` can be a text string or `'text ||| image_path'`.
    Returns list of (1-based index, comment_spec, comment_id, error_or_None).
    """
    clean_comments = [
        c for c in comments if c and any(split_comment_spec(c))
    ]
    results: list[tuple[int, str, str, str | None]] = []
    delay = max(0.0, float(delay_seconds))

    for idx, spec in enumerate(clean_comments, start=1):
        if cancel_event and cancel_event.is_set():
            results.append((idx, spec, "", "ถูกยกเลิก"))
            break
        try:
            text_part, img_part = split_comment_spec(spec)
            cid = comment_on_post(page_id, post_id, text_part, image_path=img_part)
            results.append((idx, spec, cid, None))
        except Exception as exc:  # noqa: BLE001
            results.append((idx, spec, "", str(exc)))

        if idx < len(clean_comments) and delay > 0:
            if cancel_event:
                cancel_event.wait(delay)
            else:
                time.sleep(delay)

    return results


def forget(page_id: str) -> None:
    try:
        keyring.delete_password(SERVICE, f"{PLATFORM}:{page_id}")
    except (keyring.errors.PasswordDeleteError, keyring.errors.KeyringError):
        pass


def build_post_permalink(
    page_id: str,
    remote_post_id: str,
    *,
    is_reel: bool = False,
) -> str:
    """Build a standard Facebook URL from a Graph API remote_post_id."""
    pid = (page_id or "").strip()
    rid = (remote_post_id or "").strip()
    if not rid:
        return ""
    if "_" in rid:
        left, right = rid.split("_", 1)
        if left and right:
            return f"https://www.facebook.com/{left}/posts/{right}"
    if rid.isascii() and rid.isdigit():
        if is_reel:
            return f"https://www.facebook.com/reel/{rid}"
        if pid:
            return f"https://www.facebook.com/{pid}/videos/{rid}"
    return f"https://www.facebook.com/{rid}"


def validate_facebook_url(raw_url: str, label: str = "ลิงก์ Facebook") -> str:
    """Validate and normalize a Facebook URL to https:// (Facebook hosts only)."""
    raw = (raw_url or "").strip()
    if not raw:
        raise ValueError(f"กรุณาวาง{label}")
    low = raw.lower()
    if low.startswith("http://"):
        raw = "https://" + raw[7:]
    elif not low.startswith("https://"):
        if any(
            low.startswith(p)
            for p in (
                "facebook.com/",
                "www.facebook.com/",
                "m.facebook.com/",
                "web.facebook.com/",
                "fb.com/",
                "fb.watch/",
            )
        ):
            raw = "https://" + raw
        else:
            raise ValueError(f"{label}ต้องเป็น URL ของ Facebook (เช่น https://www.facebook.com/...)")
    if not _is_facebook_host(raw):
        raise ValueError(f"{label}ต้องเป็นโดเมนของ Facebook เท่านั้น")
    return raw


def validate_facebook_group_url(raw_url: str) -> str:
    """Validate and normalize a Facebook Group URL to https://."""
    return validate_facebook_url(raw_url, label="ลิงก์กลุ่ม Facebook")


def build_facebook_share_dialog_url(post_url: str) -> str:
    """Build Facebook's official web Share Dialog URL for manual sharing to Groups/Feed."""
    url = (post_url or "").strip()
    if not url or any(c in url for c in (" ", "\t", "\r", "\n")):
        raise ValueError("กรุณาระบุลิงก์โพสต์ที่ต้องการแชร์ (ต้องไม่มีช่องว่าง)")
    try:
        parsed = urlparse(url)
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("ลิงก์โพสต์ไม่ถูกต้อง ต้องขึ้นต้นด้วย https://") from exc
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("ลิงก์โพสต์ไม่ถูกต้อง ต้องขึ้นต้นด้วย https://")
    return "https://www.facebook.com/sharer/sharer.php?" + urlencode({"u": url})


def check_page_token_health(page_id: str) -> tuple[bool, str]:
    """Check whether a saved Page Access Token is still valid via Graph API."""
    try:
        clean_pid = _validate_graph_id(page_id, "Page ID")
        token = _get_page_token(clean_pid)
        resp = _graph_get(
            clean_pid,
            token=token,
            params={"fields": "id,name"},
        )
        if resp.get("id"):
            return True, "Token ใช้งานได้ปกติ"
        return False, "ไม่พบข้อมูลเพจตอบกลับ"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def get_post_engagement(page_id: str, post_id: str) -> dict:
    """Fetch engagement metrics (likes, comments, shares) for a published Page post."""
    clean_pid = _validate_graph_id(page_id, "Page ID")
    clean_post_id = _validate_graph_id(post_id, "Post ID")
    token = _get_page_token(clean_pid)
    try:
        resp = _graph_get(
            clean_post_id,
            token=token,
            params={
                "fields": "id,shares,likes.summary(true).limit(0),comments.summary(true).limit(0),permalink_url",
            },
        )
    except RuntimeError:
        resp = _graph_get(
            clean_post_id,
            token=token,
            params={
                "fields": "id,likes.summary(true).limit(0),comments.summary(true).limit(0)",
            },
        )
    likes_obj = resp.get("likes")
    likes = (
        int(likes_obj.get("summary", {}).get("total_count", 0) or 0)
        if isinstance(likes_obj, dict)
        else 0
    )
    comments_obj = resp.get("comments")
    comments = (
        int(comments_obj.get("summary", {}).get("total_count", 0) or 0)
        if isinstance(comments_obj, dict)
        else 0
    )
    shares_obj = resp.get("shares")
    shares = int(shares_obj.get("count", 0) or 0) if isinstance(shares_obj, dict) else 0
    permalink = str(resp.get("permalink_url") or build_post_permalink(clean_pid, clean_post_id))
    return {
        "id": clean_post_id,
        "likes": likes,
        "comments": comments,
        "shares": shares,
        "permalink_url": permalink,
    }


def list_post_comments(page_id: str, post_id: str, limit: int = 30) -> list[dict]:
    """Fetch comments on a Page post for the Comment Inbox / Reply dialog."""
    clean_pid = _validate_graph_id(page_id, "Page ID")
    clean_post_id = _validate_graph_id(post_id, "Post ID")
    token = _get_page_token(clean_pid)
    try:
        resp = _graph_get(
            f"{clean_post_id}/comments",
            token=token,
            params={
                "fields": "id,from,message,created_time,comment_count,is_hidden",
                "limit": str(max(1, min(limit, 100))),
            },
        )
    except RuntimeError as exc:
        err_low = str(exc).lower()
        if any(k in err_low for k in ("access token", "oauthexception", "ไม่พบ token")):
            raise
        resp = _graph_get(
            f"{clean_post_id}/comments",
            token=token,
            params={
                "fields": "id,from,message,created_time,comment_count",
                "limit": str(max(1, min(limit, 100))),
            },
        )
    out: list[dict] = []
    for c in resp.get("data", []):
        cid = c.get("id")
        if not cid:
            continue
        from_obj = c.get("from")
        author = (
            str(from_obj.get("name") or "ผู้ใช้ Facebook")
            if isinstance(from_obj, dict)
            else "ผู้ใช้ Facebook"
        )
        author_id = (
            str(from_obj.get("id") or "").strip()
            if isinstance(from_obj, dict)
            else ""
        )
        out.append(
            {
                "id": str(cid),
                "author": author,
                "author_id": author_id,
                "message": str(c.get("message") or ""),
                "created_time": str(c.get("created_time") or ""),
                "reply_count": int(c.get("comment_count") or 0),
                "is_hidden": bool(c.get("is_hidden", False)),
            }
        )
    return out


def reply_to_comment(page_id: str, comment_id: str, message: str) -> str:
    """Reply to a specific comment on a Page post as the Page."""
    clean_pid = _validate_graph_id(page_id, "Page ID")
    clean_cid = _validate_graph_id(comment_id, "Comment ID")
    clean_msg = (message or "").strip()
    if not clean_msg:
        raise ValueError("ข้อความตอบกลับต้องไม่ว่าง")
    token = _get_page_token(clean_pid)
    resp = _graph_post(
        f"{clean_cid}/comments",
        token=token,
        data={"message": clean_msg},
        timeout=30,
    )
    rid = resp.get("id")
    if not rid:
        raise RuntimeError("Facebook ไม่ส่งคืน reply comment_id")
    return str(rid)


def delete_post(page_id: str, post_id: str) -> bool:
    """Delete a published post from a Facebook Page via DELETE /{post_id}."""
    clean_pid = _validate_graph_id(page_id, "Page ID")
    clean_post_id = _validate_graph_id(post_id, "Post ID")
    token = _get_page_token(clean_pid)
    _graph_delete(clean_post_id, token=token, timeout=30)
    return True


def set_comment_hidden(page_id: str, comment_id: str, is_hidden: bool = True) -> bool:
    """Hide or unhide a comment on a Page post via POST /{comment_id} is_hidden."""
    clean_pid = _validate_graph_id(page_id, "Page ID")
    clean_cid = _validate_graph_id(comment_id, "Comment ID")
    token = _get_page_token(clean_pid)
    _graph_post(
        clean_cid,
        token=token,
        data={"is_hidden": "true" if is_hidden else "false"},
        timeout=30,
    )
    return True


def delete_comment(page_id: str, comment_id: str) -> bool:
    """Delete a comment on a Page post via DELETE /{comment_id}."""
    clean_pid = _validate_graph_id(page_id, "Page ID")
    clean_cid = _validate_graph_id(comment_id, "Comment ID")
    token = _get_page_token(clean_pid)
    _graph_delete(clean_cid, token=token, timeout=30)
    return True


_SPINTAX_PATTERN = re.compile(r"\{([^{}]*\|[^{}]*)\}")
_SENTINEL_PATTERN = re.compile(
    r"\x00SM_(?:PAGE_NAME|COMMENTER_NAME|DATE|TIME)\x00"
)


def render_spintax_and_variables(
    template_text: str,
    *,
    page_name: str = "",
    commenter_name: str = "",
    now_dt: datetime | None = None,
) -> str:
    """Expand `{page_name}`, `{commenter_name}`, `{date}`, `{time}` and Spintax `{A|B|C}`.

    Dynamic variables are shielded with temporary sentinel tokens during Spintax
    expansion so `{page_name}` / `{commenter_name}` can appear inside Spintax options
    AND external names containing `{A|B}` or `{...}` are never mangled.
    """
    if not template_text:
        return ""
    now = now_dt or datetime.now(timezone.utc).astimezone()
    sent_page = "\x00SM_PAGE_NAME\x00"
    sent_commenter = "\x00SM_COMMENTER_NAME\x00"
    sent_date = "\x00SM_DATE\x00"
    sent_time = "\x00SM_TIME\x00"
    out = (
        template_text.replace("{page_name}", sent_page)
        .replace("{commenter_name}", sent_commenter)
        .replace("{date}", sent_date)
        .replace("{time}", sent_time)
    )
    for _ in range(20):
        if not _SPINTAX_PATTERN.search(out):
            break
        out = _SPINTAX_PATTERN.sub(
            lambda m: random.choice([opt for opt in m.group(1).split("|")]),
            out,
        )
    replacements = {
        sent_page: page_name or "",
        sent_commenter: commenter_name or "",
        sent_date: now.strftime("%Y-%m-%d"),
        sent_time: now.strftime("%H:%M"),
    }
    return _SENTINEL_PATTERN.sub(lambda m: replacements.get(m.group(0), ""), out)


def match_auto_reply_rule(
    comment_text: str,
    rules: list[dict],
) -> tuple[dict, str] | None:
    """Return `(matched_rule, matched_keyword)` for the first enabled rule matching `comment_text`."""
    clean_text = (comment_text or "").strip()
    if not clean_text:
        return None
    folded_text = clean_text.casefold()
    for rule in rules or []:
        if not rule.get("enabled", True):
            continue
        mode = rule.get("match_mode", "contains")
        for kw in rule.get("keywords") or []:
            clean_kw = str(kw).strip()
            if not clean_kw:
                continue
            folded_kw = clean_kw.casefold()
            if mode == "exact":
                if folded_text == folded_kw:
                    return rule, clean_kw
            else:
                if folded_kw in folded_text:
                    return rule, clean_kw
    return None


def run_keyword_auto_reply_pass(
    pages: list[tuple[str, str]],
    rules: list[dict],
    *,
    has_replied_fn=None,
    record_reply_fn=None,
    posts_per_page: int = 5,
    comments_per_post: int = 25,
    delay_seconds: float = 0.8,
    cancel_event: threading.Event | None = None,
) -> dict:
    """Scan recent posts across `pages` (`[(page_id, page_name), ...]`) and auto-reply once per comment."""
    if has_replied_fn is None or record_reply_fn is None:
        import db as _db

        if has_replied_fn is None:
            has_replied_fn = _db.has_auto_replied
        if record_reply_fn is None:
            record_reply_fn = _db.record_auto_reply

    active_rules = [
        r
        for r in (rules or [])
        if r.get("enabled", True)
        and (r.get("keywords") or [])
        and str(r.get("reply_template") or "").strip()
    ]
    summary: dict = {
        "pages_checked": 0,
        "posts_checked": 0,
        "comments_scanned": 0,
        "replied_count": 0,
        "hidden_count": 0,
        "skipped_replied": 0,
        "skipped_self": 0,
        "errors": [],
        "details": [],
        "cancelled": False,
    }
    if not active_rules or not pages:
        return summary

    p_limit = max(1, min(int(posts_per_page or 5), 20))
    c_limit = max(1, min(int(comments_per_post or 25), 100))
    delay = max(0.0, float(delay_seconds))

    for raw_pid, page_name in pages:
        if cancel_event and cancel_event.is_set():
            summary["cancelled"] = True
            break
        clean_pid = (raw_pid or "").strip()
        if not clean_pid:
            continue
        try:
            posts = list_page_posts(clean_pid, limit=p_limit)
            summary["pages_checked"] += 1
        except Exception as exc:  # noqa: BLE001
            summary["errors"].append(f"{page_name}: {exc}")
            continue

        for post in posts:
            if cancel_event and cancel_event.is_set():
                summary["cancelled"] = True
                break
            post_id = str(post.get("id") or "").strip()
            if not post_id:
                continue
            try:
                comments = list_post_comments(clean_pid, post_id, limit=c_limit)
                summary["posts_checked"] += 1
            except Exception as exc:  # noqa: BLE001
                summary["errors"].append(f"{page_name} ({post_id}): {exc}")
                continue

            for c in comments:
                if cancel_event and cancel_event.is_set():
                    summary["cancelled"] = True
                    break
                cid = str(c.get("id") or "").strip()
                msg = str(c.get("message") or "").strip()
                if not cid or not msg:
                    continue
                summary["comments_scanned"] += 1

                author_id = str(c.get("author_id") or "").strip()
                author_name = str(c.get("author") or "").strip()
                if (author_id and author_id == clean_pid) or (
                    not author_id
                    and page_name
                    and author_name != "ผู้ใช้ Facebook"
                    and author_name == page_name.strip()
                ):
                    summary["skipped_self"] += 1
                    continue

                if has_replied_fn(cid):
                    summary["skipped_replied"] += 1
                    continue

                matched = match_auto_reply_rule(msg, active_rules)
                if matched is None:
                    continue
                rule, matched_kw = matched

                commenter_display = (
                    author_name
                    if author_name and author_name != "ผู้ใช้ Facebook"
                    else "ลูกค้า"
                )
                reply_txt = render_spintax_and_variables(
                    str(rule["reply_template"]),
                    page_name=page_name,
                    commenter_name=commenter_display,
                ).strip()
                if not reply_txt:
                    continue

                try:
                    reply_cid = reply_to_comment(clean_pid, cid, reply_txt)
                except Exception as exc:  # noqa: BLE001
                    summary["errors"].append(f"{page_name} ตอบคอมเมนต์ {cid} ไม่สำเร็จ: {exc}")
                    continue

                record_reply_fn(
                    comment_id=cid,
                    platform=PLATFORM,
                    page_id=clean_pid,
                    post_id=post_id,
                    rule_id=int(rule.get("id") or 0),
                    reply_comment_id=reply_cid,
                    commenter_name=author_name,
                    comment_text=msg,
                    reply_text=reply_txt,
                )
                summary["replied_count"] += 1

                hidden_ok = False
                if rule.get("hide_after_reply"):
                    try:
                        set_comment_hidden(clean_pid, cid, True)
                        hidden_ok = True
                        summary["hidden_count"] += 1
                    except Exception as exc:  # noqa: BLE001
                        summary["errors"].append(f"{page_name} ซ่อนคอมเมนต์ {cid} ไม่สำเร็จ: {exc}")

                summary["details"].append(
                    {
                        "page_id": clean_pid,
                        "page_name": page_name,
                        "post_id": post_id,
                        "comment_id": cid,
                        "reply_comment_id": reply_cid,
                        "commenter_name": author_name,
                        "matched_keyword": matched_kw,
                        "rule_name": str(rule.get("name") or ""),
                        "reply_text": reply_txt,
                        "hidden": hidden_ok,
                    }
                )

                if delay > 0:
                    if cancel_event:
                        cancel_event.wait(delay)
                    else:
                        time.sleep(delay)
    return summary


# --- Page Analytics, Comparison & RT Dashboard ---

def _extract_insight_day_values(
    resp: dict, *, dict_key: str | None = None
) -> list[int]:
    """Extract ordered daily integer values from a Graph API /{page_id}/insights response."""
    data = resp.get("data")
    if not isinstance(data, list) or not data:
        return []
    first = data[0] if isinstance(data[0], dict) else {}
    raw_vals = first.get("values")
    if not isinstance(raw_vals, list):
        return []
    out: list[int] = []
    for item in raw_vals:
        if not isinstance(item, dict):
            continue
        val = item.get("value")
        if dict_key is not None:
            if isinstance(val, dict):
                v_sub = val.get(dict_key, 0)
                try:
                    out.append(int(v_sub or 0))
                except (TypeError, ValueError):
                    out.append(0)
            else:
                out.append(0)
        elif isinstance(val, dict):
            total_v = 0
            for v_sub in val.values():
                try:
                    total_v += int(v_sub or 0)
                except (TypeError, ValueError):
                    pass
            out.append(total_v)
        else:
            try:
                out.append(int(val or 0))
            except (TypeError, ValueError):
                out.append(0)
    return out


def _fetch_insight_metric_series(
    page_id: str,
    token: str,
    candidates: tuple[str, ...],
    *,
    since_ts: int,
    until_ts: int,
    dict_key: str | None = None,
) -> tuple[list[int] | None, str, str]:
    """Query Page Insights metric candidates one-by-one so one deprecated metric never breaks others.

    Returns (daily_values_or_None, matched_metric_name, last_non_auth_error).
    If any candidate succeeds with HTTP 200 (even with empty `data: []`), any earlier
    deprecated-metric error is cleared so callers never report a stale `(#100)` error.
    Re-raises fatal token/session errors immediately.
    """
    last_err = ""
    had_valid_empty = False
    for metric in candidates:
        try:
            resp = _graph_get(
                f"{page_id}/insights",
                token=token,
                params={
                    "metric": metric,
                    "period": "day",
                    "since": str(since_ts),
                    "until": str(until_ts),
                },
                timeout=20,
            )
            vals = _extract_insight_day_values(resp, dict_key=dict_key)
            if vals:
                return vals, metric, ""
            had_valid_empty = True
            last_err = ""
        except RuntimeError as exc:
            if _is_token_auth_error(exc):
                raise
            if not had_valid_empty:
                last_err = str(exc)
            continue
    return None, "", last_err


def _fetch_insight_rolling_pair(
    page_id: str,
    token: str,
    candidates: tuple[str, ...],
    *,
    period: str,
    since_ts: int,
    until_ts: int,
) -> tuple[int, int, str] | None:
    """Query a rolling-window Page Insights period (`week` or `days_28`) when `period=day` is empty.

    Returns `(current_total, previous_total, matched_metric)` or `None`.
    """
    if period not in ("week", "days_28"):
        return None
    step = 7 if period == "week" else 28
    for metric in candidates:
        try:
            resp = _graph_get(
                f"{page_id}/insights",
                token=token,
                params={
                    "metric": metric,
                    "period": period,
                    "since": str(since_ts),
                    "until": str(until_ts),
                },
                timeout=20,
            )
            vals = _extract_insight_day_values(resp)
            if vals:
                cur = int(vals[-1])
                prev = int(vals[-1 - step]) if len(vals) > step else 0
                return cur, prev, metric
        except RuntimeError as exc:
            if _is_token_auth_error(exc):
                raise
            continue
    return None


def _fetch_post_insights_rollup(
    post_ids: list[str],
    token: str,
    *,
    need_reach: bool,
    need_impressions: bool,
    max_posts: int = 10,
) -> tuple[int, int]:
    """Best-effort post-level Insights fallback (`post_total_media_view_unique` / `post_media_view`).

    Used when Page-level Insights returns 0/empty (e.g. small pages or Graph API metric changes).
    """
    if not post_ids or (not need_reach and not need_impressions):
        return 0, 0
    total_reach = 0
    total_imp = 0
    reach_candidates = ("post_total_media_view_unique", "post_impressions_unique")
    imp_candidates = ("post_media_view", "post_impressions")
    working_reach_metric: str | None = None
    working_imp_metric: str | None = None
    reach_dead = not need_reach
    imp_dead = not need_impressions

    for post_id in post_ids[:max_posts]:
        if reach_dead and imp_dead:
            break
        if not reach_dead:
            cands = (working_reach_metric,) if working_reach_metric else reach_candidates
            got_val = False
            for m in cands:
                try:
                    resp = _graph_get(
                        f"{post_id}/insights",
                        token=token,
                        params={"metric": m},
                        timeout=15,
                    )
                    vals = _extract_insight_day_values(resp)
                    if vals:
                        total_reach += int(vals[-1])
                        working_reach_metric = m
                        got_val = True
                        break
                except RuntimeError as exc:
                    if _is_token_auth_error(exc):
                        raise
                    continue
            if not got_val and not working_reach_metric:
                reach_dead = True

        if not imp_dead:
            cands = (working_imp_metric,) if working_imp_metric else imp_candidates
            got_val = False
            for m in cands:
                try:
                    resp = _graph_get(
                        f"{post_id}/insights",
                        token=token,
                        params={"metric": m},
                        timeout=15,
                    )
                    vals = _extract_insight_day_values(resp)
                    if vals:
                        total_imp += int(vals[-1])
                        working_imp_metric = m
                        got_val = True
                        break
                except RuntimeError as exc:
                    if _is_token_auth_error(exc):
                        raise
                    continue
            if not got_val and not working_imp_metric:
                imp_dead = True

    return total_reach, total_imp


def _split_period_totals(series: list[int] | None, period_days: int) -> tuple[int, int]:
    """Split a 2*period_days daily series into (current_total, previous_total)."""
    if not series:
        return 0, 0
    cur = sum(series[-period_days:])
    if len(series) < period_days * 2:
        return cur, 0
    prev = sum(series[-2 * period_days : -period_days])
    return cur, prev


def _calc_delta_pct(current: int, previous: int) -> float | None:
    if previous <= 0:
        return None
    return round(((current - previous) / previous) * 100.0, 1)


def get_page_analytics(
    page_id: str,
    page_name: str = "",
    *,
    period_days: int = 7,
    prev_snapshot: dict | None = None,
) -> dict:
    """Fetch live Page Analytics (Reach, Impressions, Engagement, Reactions, Comments,
    Shares, Link Clicks, Followers) + growth/decline trends for a Facebook Page.

    Queries Page Insights metrics individually with modern-API fallbacks so a single
    deprecated Meta metric never causes the entire dashboard call to fail.
    """
    clean_pid = _validate_graph_id(page_id, "Page ID")
    p_days = max(1, min(int(period_days or 7), 90))
    token = _get_page_token(clean_pid)

    now_utc = datetime.now(timezone.utc)
    fetched_iso = now_utc.astimezone().isoformat(timespec="seconds")
    until_ts = int(now_utc.timestamp())
    lookback_days = min(p_days * 2, 90)
    since_ts = int((now_utc - timedelta(days=lookback_days)).timestamp())
    cutoff_dt = now_utc - timedelta(days=p_days)

    warnings: list[str] = []

    # 1) Page node: Followers & canonical name
    page_info = _graph_get(
        clean_pid,
        token=token,
        params={"fields": "name,followers_count,fan_count"},
        timeout=20,
    )
    resolved_name = str(page_info.get("name") or page_name or clean_pid).strip()
    followers = int(
        page_info.get("followers_count")
        if page_info.get("followers_count") is not None
        else (page_info.get("fan_count") or 0)
    )

    # 2) Recent posts rollup: live Reactions, Comments, Shares, and latest post timestamp
    reactions = 0
    comments = 0
    shares = 0
    posts_in_period = 0
    latest_post_at = ""
    in_window_post_ids: list[str] = []
    try:
        posts_resp = _graph_get(
            f"{clean_pid}/posts",
            token=token,
            params={
                "fields": (
                    "id,created_time,shares,"
                    "reactions.summary(total_count).limit(0),"
                    "comments.summary(total_count).limit(0)"
                ),
                "limit": "50",
            },
            timeout=25,
        )
        for post in posts_resp.get("data", []) or []:
            if not isinstance(post, dict):
                continue
            ctime_str = str(post.get("created_time") or "").strip()
            if ctime_str and not latest_post_at:
                latest_post_at = ctime_str
            in_window = True
            if ctime_str:
                try:
                    p_dt = datetime.fromisoformat(ctime_str.replace("Z", "+00:00"))
                    if p_dt.tzinfo is None:
                        p_dt = p_dt.replace(tzinfo=timezone.utc)
                    in_window = p_dt >= cutoff_dt
                except ValueError:
                    in_window = True
            if in_window:
                posts_in_period += 1
                pid_item = str(post.get("id") or "").strip()
                if pid_item:
                    in_window_post_ids.append(pid_item)
                r_obj = post.get("reactions")
                if isinstance(r_obj, dict):
                    reactions += int((r_obj.get("summary") or {}).get("total_count") or 0)
                c_obj = post.get("comments")
                if isinstance(c_obj, dict):
                    comments += int((c_obj.get("summary") or {}).get("total_count") or 0)
                s_obj = post.get("shares")
                if isinstance(s_obj, dict):
                    shares += int(s_obj.get("count") or 0)
    except RuntimeError as exc:
        if _is_token_auth_error(exc):
            raise
        warnings.append(f"ดึงโพสต์ล่าสุดไม่สำเร็จ: {exc}")

    # 3) Page Insights: query modern 2025/2026 metrics FIRST, legacy fallbacks LAST
    reach_candidates = ("page_total_media_view_unique", "page_views_total", "page_impressions_unique")
    imp_candidates = ("page_media_view", "page_impressions")

    reach_series, _m_reach, err_reach = _fetch_insight_metric_series(
        clean_pid,
        token,
        reach_candidates,
        since_ts=since_ts,
        until_ts=until_ts,
    )
    imp_series, _m_imp, err_imp = _fetch_insight_metric_series(
        clean_pid,
        token,
        imp_candidates,
        since_ts=since_ts,
        until_ts=until_ts,
    )
    eng_series, _m_eng, err_eng = _fetch_insight_metric_series(
        clean_pid,
        token,
        ("page_post_engagements",),
        since_ts=since_ts,
        until_ts=until_ts,
    )
    if eng_series is None and err_eng:
        warnings.append(f"Engagement: {err_eng}")

    clicks_series, _m_clicks, _err_clicks = _fetch_insight_metric_series(
        clean_pid,
        token,
        ("page_consumptions_by_consumption_type",),
        since_ts=since_ts,
        until_ts=until_ts,
        dict_key="link clicks",
    )

    reach, prev_reach = _split_period_totals(reach_series, p_days)
    impressions, _prev_imp = _split_period_totals(imp_series, p_days)
    eng_from_insights, prev_engagement = _split_period_totals(eng_series, p_days)
    link_clicks, _prev_clicks = _split_period_totals(clicks_series, p_days)

    # Rolling-window fallback (week / days_28) when daily Page Insights is empty
    rolling_period = "days_28" if p_days >= 28 else ("week" if p_days >= 7 else "")
    if rolling_period and reach_series is None:
        r_roll = _fetch_insight_rolling_pair(
            clean_pid,
            token,
            reach_candidates,
            period=rolling_period,
            since_ts=since_ts,
            until_ts=until_ts,
        )
        if r_roll is not None:
            reach, prev_reach, _ = r_roll
            err_reach = ""
    if rolling_period and imp_series is None:
        i_roll = _fetch_insight_rolling_pair(
            clean_pid,
            token,
            imp_candidates,
            period=rolling_period,
            since_ts=since_ts,
            until_ts=until_ts,
        )
        if i_roll is not None:
            impressions, _prev_imp, _ = i_roll
            err_imp = ""

    # Post-level Insights fallback when Page-level Reach/Impressions returned 0
    if (reach == 0 or impressions == 0) and in_window_post_ids:
        post_reach, post_imp = _fetch_post_insights_rollup(
            in_window_post_ids,
            token,
            need_reach=(reach == 0),
            need_impressions=(impressions == 0),
        )
        if reach == 0 and post_reach > 0:
            reach = post_reach
            err_reach = ""
        if impressions == 0 and post_imp > 0:
            impressions = post_imp
            err_imp = ""

    if reach == 0 and err_reach:
        warnings.append(f"Reach: {err_reach}")
    if impressions == 0 and err_imp:
        warnings.append(f"Impressions: {err_imp}")

    # Fallback: if Meta deprecated page_post_engagements or returned 0 while post interactions exist
    post_interactions = reactions + comments + shares + link_clicks
    engagement = eng_from_insights if eng_from_insights > 0 else post_interactions

    # Clear actionable hint when Insights returned empty on an active Page
    if (
        reach == 0
        and impressions == 0
        and not err_reach
        and not err_imp
        and (posts_in_period > 0 or engagement > 0)
    ):
        if followers < 100:
            warnings.append(
                "ต้องมีสิทธิ์ read_insights ใน Token (และเพจควรมีผู้ติดตาม ≥ 100 คนจึงจะแสดง Reach/Impressions)"
            )
        else:
            warnings.append(
                "ไม่ได้รับค่า Reach/Impressions จาก Meta (กรุณาเช็กว่า Token มีสิทธิ์ read_insights หรือเชื่อมต่อ Facebook ใหม่)"
            )

    # Fallback to previous local snapshot for delta if Meta didn't return a previous window
    if prev_reach <= 0 and isinstance(prev_snapshot, dict):
        prev_reach = int(prev_snapshot.get("reach") or 0)
    if prev_engagement <= 0 and isinstance(prev_snapshot, dict):
        prev_engagement = int(prev_snapshot.get("engagement") or 0)

    reach_delta_pct = _calc_delta_pct(reach, prev_reach)
    engagement_delta_pct = _calc_delta_pct(engagement, prev_engagement)

    has_recent_update = bool(posts_in_period > 0 or reach > 0 or engagement > 0)
    if not has_recent_update:
        status = "no_data"
    elif warnings and reach == 0 and impressions == 0:
        status = "partial"
    else:
        status = "ok"

    return {
        "platform": PLATFORM,
        "page_id": clean_pid,
        "page_name": resolved_name,
        "period_days": p_days,
        "reach": reach,
        "impressions": impressions,
        "engagement": engagement,
        "reactions": reactions,
        "comments": comments,
        "shares": shares,
        "link_clicks": link_clicks,
        "followers": followers,
        "prev_reach": prev_reach,
        "prev_engagement": prev_engagement,
        "reach_delta_pct": reach_delta_pct,
        "engagement_delta_pct": engagement_delta_pct,
        "posts_in_period": posts_in_period,
        "latest_post_at": latest_post_at,
        "has_recent_update": has_recent_update,
        "status": status,
        "error": "; ".join(warnings[:2]) if warnings else "",
        "fetched_at": fetched_iso,
    }


def analyze_page_comparison(rows: list[dict]) -> dict:
    """Compare analytics rows across pages to surface leaders, growth, drops, and stale/error pages."""
    valid = [r for r in rows if isinstance(r, dict)]
    total_reach = sum(int(r.get("reach") or 0) for r in valid)
    total_impressions = sum(int(r.get("impressions") or 0) for r in valid)
    total_engagement = sum(int(r.get("engagement") or 0) for r in valid)
    total_reactions = sum(int(r.get("reactions") or 0) for r in valid)
    total_comments = sum(int(r.get("comments") or 0) for r in valid)
    total_shares = sum(int(r.get("shares") or 0) for r in valid)
    total_link_clicks = sum(int(r.get("link_clicks") or 0) for r in valid)
    total_followers = sum(int(r.get("followers") or 0) for r in valid)

    ok_rows = [r for r in valid if r.get("status") != "error"]
    top_reach = (
        max(ok_rows, key=lambda r: int(r.get("reach") or 0))
        if any(int(r.get("reach") or 0) > 0 for r in ok_rows)
        else None
    )
    top_engagement = (
        max(ok_rows, key=lambda r: int(r.get("engagement") or 0))
        if any(int(r.get("engagement") or 0) > 0 for r in ok_rows)
        else None
    )
    declining_reach = sorted(
        [
            r
            for r in ok_rows
            if r.get("reach_delta_pct") is not None and float(r["reach_delta_pct"]) < 0
        ],
        key=lambda r: float(r["reach_delta_pct"]),
    )
    growing_pages = sorted(
        [
            r
            for r in ok_rows
            if (r.get("reach_delta_pct") is not None and float(r["reach_delta_pct"]) > 0)
            or (r.get("engagement_delta_pct") is not None and float(r["engagement_delta_pct"]) > 0)
        ],
        key=lambda r: max(
            float(r["reach_delta_pct"]) if r.get("reach_delta_pct") is not None else -9999.0,
            float(r["engagement_delta_pct"]) if r.get("engagement_delta_pct") is not None else -9999.0,
        ),
        reverse=True,
    )
    error_pages = [r for r in valid if r.get("status") == "error"]
    stale_or_no_data = [
        r
        for r in valid
        if r.get("status") in ("no_data", "error")
        or (int(r.get("reach") or 0) == 0 and int(r.get("engagement") or 0) == 0)
    ]

    return {
        "page_count": len(valid),
        "total_reach": total_reach,
        "total_impressions": total_impressions,
        "total_engagement": total_engagement,
        "total_reactions": total_reactions,
        "total_comments": total_comments,
        "total_shares": total_shares,
        "total_link_clicks": total_link_clicks,
        "total_followers": total_followers,
        "top_reach": top_reach,
        "top_engagement": top_engagement,
        "declining_reach": declining_reach,
        "growing_pages": growing_pages,
        "stale_or_no_data": stale_or_no_data,
        "error_pages": error_pages,
    }
