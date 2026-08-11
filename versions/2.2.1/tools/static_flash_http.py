#!/usr/bin/env python3
"""Static HTTP server that serves PHP files as HTML for the Flash client."""

from __future__ import annotations

import json
import base64
import hashlib
import hmac
import ipaddress
import mimetypes
import os
import secrets
import socket
import subprocess
import tempfile
import time
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlsplit
from urllib.request import Request, urlopen

from multiplayer_backend import ACCOUNT_SERVICE


HTTP_HOST = os.environ.get("NARUTO_HTTP_HOST", "127.0.0.1")
HTTP_PORT = int(os.environ.get("NARUTO_HTTP_PORT", "18680"))
RUNTIME_INSTANCE_ID = f"{os.getpid()}-{time.time_ns()}"
WWW_ROOT = Path(
    os.environ.get(
        "NARUTO_WWW_ROOT",
        str(Path(__file__).resolve().parents[1] / "www"),
    )
)
PRIVATE_ROOTS = ((WWW_ROOT / "editor_workspace").resolve(),)
LOCAL_ASSET_PREFIX = os.environ.get("NARUTO_LOCAL_ASSET_PREFIX", "act_web_tiyan/")
PREVIEW_ASSET_PREFIX = os.environ.get(
    "NARUTO_PREVIEW_ASSET_PREFIX", "act_web_hysj_preview/"
)
LEGACY_PREVIEW_ASSET_PREFIX = "act_web_hysj_preview/"
ONLINE_ASSET_ROOTS = (
    "https://foxrun.pubnar.com/act_web_tw/",
    "https://foxrun.pubnar.com/act_web_tw_cn/",
)
ONLINE_ASSET_VERSION = "2012071101"
ONLINE_REFERER = "https://foxrun.pubnar.com/run.php"
ONLINE_TIMEOUT_SECONDS = 20
GM_PASSWORD = os.environ.get("NARUTO_GM_PASSWORD", "NyaLocal#208")
SWF_SIGNATURES = (b"CWS", b"FWS", b"ZWS")
BINARY_ASSET_SIGNATURES = {
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".gif": (b"GIF87a", b"GIF89a"),
    ".mp3": (b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"),
}
CACHE_WRITE_LOCK = Lock()
NATIVE_FLASH_BUILD_LOCK = Lock()
PLAYER_API_TOKEN_LOCK = Lock()
PLAYER_API_TOKEN_TTL_SECONDS = 12 * 60 * 60
PLAYER_API_TOKENS: dict[str, tuple[int, float]] = {}
OPERATIONS_STATE_LOCK = Lock()
OPERATIONS_ARM_TTL_SECONDS = 15 * 60
OPERATIONS_SESSION_TTL_SECONDS = 15 * 60
OPERATIONS_NATIVE_TICKET_TTL_SECONDS = 30
OPERATIONS_ARM_COOKIE = "naruto_ops_arm"
OPERATIONS_SESSION_COOKIE = "naruto_ops_session"
OPERATIONS_UNLOCK_CODE_SHA256 = (
    "27cd7ff0e723ccb50f3a7a95b5853021b5b90d4fedfc3a7f6cb5e77d828040c2"
)
OPERATIONS_ARMS: dict[str, tuple[str, float]] = {}
OPERATIONS_SESSIONS: dict[str, tuple[str, float]] = {}
OPERATIONS_NATIVE_TICKETS: dict[str, tuple[str, float]] = {}
NATIVE_FLASH_HOST_SOURCE = Path(__file__).with_name("native_flash_host.cs")
NATIVE_FLASH_HOST_EXE = Path(__file__).with_name("native_flash_host.exe")
NATIVE_FLASH_CSC = Path(r"C:\Windows\Microsoft.NET\Framework\v4.0.30319\csc.exe")


def _constant_time_text_equal(left: str, right: str) -> bool:
    """Compare arbitrary Unicode secrets without hmac's ASCII-only str path."""
    try:
        return hmac.compare_digest(
            str(left).encode("utf-8"),
            str(right).encode("utf-8"),
        )
    except (UnicodeError, ValueError):
        return False


LOCAL_SWF_ALIASES = {
    # The original catalog references these legacy BuffWuPin names, but this package
    # only contains their equivalent local consumable/buff artwork.
    "act_web_tiyan/swf/BuffWuPin/BuffWuPin_18000001_info.swf": (
        "act_web_tiyan/swf/XiaoHao/XiaoHao_03000001_info[1].swf"
    ),
    "act_web_tiyan/swf/BuffWuPin/BuffWuPin_18002001_info.swf": (
        "act_web_tiyan/swf/BuffWuPin/BuffWuPin_18003003_info[1].swf"
    ),
    "act_web_tiyan/swf/BuffWuPin/BuffWuPin_18004001_info.swf": (
        "act_web_tiyan/swf/XiaoHao/XiaoHao_03000006_info[1].swf"
    ),
}
LOCAL_PATH_ALIASES = {
    # Keep the common transposed filename usable for friend-test links.
    "ruflle.html": "ruffle.html",
    # Some mobile keyboards drop the final character from the launcher URL.
    "ruffle.htm": "ruffle.html",
    # MainLib resolves this relative to the top-level Ruffle page, while the packaged
    # map belongs to the original act_web_tiyan application directory.
    "localization/t2s.txt": "act_web_tiyan/localization/t2s.txt",
}


def runtime_origin(request_host: str, forwarded_proto: str = "") -> str:
    """Return a safe public origin derived from the reverse-proxy request."""
    scheme = forwarded_proto.split(",", 1)[0].strip().lower()
    if scheme not in {"http", "https"}:
        scheme = "https" if HTTP_PORT == 443 else "http"
    host = request_host.strip()
    parsed = urlsplit(f"//{host}")
    if (
        not host
        or parsed.netloc != host
        or not parsed.hostname
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        fallback_host = HTTP_HOST if HTTP_HOST not in {"", "0.0.0.0"} else "127.0.0.1"
        host = fallback_host if HTTP_PORT in {80, 443} else f"{fallback_host}:{HTTP_PORT}"
    return f"{scheme}://{host}"


def runtime_advertise_host(request_host: str) -> str:
    """Resolve the request's public host to the IPv4 required by native Flash."""
    host = request_host.split(",", 1)[0].strip()
    parsed = urlsplit(f"//{host}")
    hostname = parsed.hostname or ""
    if hostname.lower() == "localhost":
        return "127.0.0.1"
    try:
        address = ipaddress.ip_address(hostname)
        if address.version == 4:
            return str(address)
    except ValueError:
        pass
    if hostname:
        try:
            return socket.gethostbyname(hostname)
        except OSError:
            pass
    fallback = os.environ.get("NARUTO_ADVERTISE_HOST", "127.0.0.1").strip()
    try:
        return str(ipaddress.IPv4Address(fallback))
    except ipaddress.AddressValueError:
        return "127.0.0.1"


def issue_player_api_token(account_id: int) -> str:
    """Issue one short-lived browser token without repeatedly hashing passwords."""
    token = secrets.token_urlsafe(32)
    now = time.time()
    with PLAYER_API_TOKEN_LOCK:
        expired = [
            value
            for value, (_, expires_at) in PLAYER_API_TOKENS.items()
            if expires_at <= now
        ]
        for value in expired:
            PLAYER_API_TOKENS.pop(value, None)
        PLAYER_API_TOKENS[token] = (
            int(account_id),
            now + PLAYER_API_TOKEN_TTL_SECONDS,
        )
    return token


def player_api_account(token: str) -> dict[str, object] | None:
    """Resolve a valid browser token to its current persisted account."""
    now = time.time()
    with PLAYER_API_TOKEN_LOCK:
        payload = PLAYER_API_TOKENS.get(str(token))
        if payload is None:
            return None
        account_id, expires_at = payload
        if expires_at <= now:
            PLAYER_API_TOKENS.pop(str(token), None)
            return None
    return ACCOUNT_SERVICE.account_by_id(account_id)


def runtime_top_links(request_host: str, forwarded_proto: str = "") -> list[dict[str, object]]:
    """Build server-owned top links while allowing the public domain to change."""
    origin = runtime_origin(request_host, forwarded_proto)
    definitions = (
        ("官网", "NARUTO_OFFICIAL_URL", "{origin}/index.php"),
        ("收藏", "NARUTO_FAVORITE_URL", "{origin}/nya-208-preview.html"),
        ("下载", "NARUTO_DOWNLOAD_URL", "{origin}/index.php?download=1"),
    )
    links: list[dict[str, object]] = []
    for label, environment_name, fallback_template in definitions:
        template = os.environ.get(environment_name, "").strip() or fallback_template
        url = template.replace("{origin}", origin)
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            url = fallback_template.replace("{origin}", origin)
        links.append(
            {
                "label": label,
                "url": url,
                "highlight": environment_name == "NARUTO_DOWNLOAD_URL",
            }
        )
    return links


class FlashStaticHandler(SimpleHTTPRequestHandler):
    """Serve legacy Flash game assets with browser-friendly MIME types."""

    extensions_map = SimpleHTTPRequestHandler.extensions_map.copy()
    extensions_map.update(
        {
            ".php": "text/html; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".swf": "application/x-shockwave-flash",
            ".dat": "application/octet-stream",
            ".wasm": "application/wasm",
        }
    )

    def __init__(self, *args: object, directory: str | None = None, **kwargs: object):
        """Always serve the repository's www directory regardless of shell location."""
        super().__init__(*args, directory=directory or str(WWW_ROOT), **kwargs)

    def end_headers(self) -> None:
        """Cache immutable game assets without hiding patched runtime files."""
        request_path = urlsplit(self.path).path
        if self._is_cacheable_asset(request_path):
            self.send_header("Cache-Control", "public, max-age=3600")
        else:
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
        super().end_headers()

    @staticmethod
    def _is_cacheable_asset(request_path: str) -> bool:
        """Return whether a resource is stable enough for short public caching."""
        if request_path.startswith("/act_web_nya_208_isolated/"):
            return False
        runtime_suffixes = (
            "/Main.swf",
            "/asset/MainLib.swf",
            "/asset/MainLoginServer.swf",
            "/asset/MainCreateChar.swf",
            "/asset/effect.swf",
            "/dat/GameData.dat",
            "/dat/version.dat",
        )
        if request_path.endswith(runtime_suffixes):
            return False
        suffix = Path(request_path).suffix.lower()
        return suffix in {
            ".swf",
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".mp3",
            ".wasm",
            ".js",
            ".txt",
        }

    def do_GET(self) -> None:
        """Serve the dynamic SWF index used by the local UI reference page."""
        route = self.path.split("?", 1)[0]
        # NyaMicroClient uses the PHP-compatible launcher contract rather than
        # the browser-only /api/account/* endpoints. Keep it in the same local
        # HTTP process so the launcher and the game always share one account
        # store and one native launch-token service.
        try:
            from nya_client_api_bridge import handle_microclient_get

            if handle_microclient_get(self):
                return
        except ImportError:
            pass
        if route == "/ops/native-launch":
            self._consume_native_operations_ticket()
            return
        if route in {"/ops", "/ops/", "/ops/index.html"}:
            if not self._require_operations_session(page_request=True):
                return
            original_path = self.path
            self.path = "/gm/index.html"
            try:
                super().do_GET()
            finally:
                self.path = original_path
            return
        admin_route, operations_request = self._administrative_route(route)
        if operations_request and not self._require_operations_session():
            return
        if admin_route == "/api/gm/status":
            if not operations_request and not self._require_gm_auth():
                return
            try:
                from gm_admin import status

                self._send_json({"ok": True, **status()})
            except (ImportError, AttributeError, OSError, TypeError, ValueError) as exc:
                self._send_json(
                    {"ok": False, "error": str(exc) or "GM 状态读取失败"},
                    HTTPStatus.BAD_REQUEST,
                )
            return
        if admin_route == "/api/gm/items":
            if not operations_request and not self._require_gm_auth():
                return
            try:
                from gm_admin import item_rows

                query = urlsplit(self.path).query
                values: dict[str, str] = {}
                for pair in query.split("&") if query else ():
                    key, separator, value = pair.partition("=")
                    if separator:
                        values[unquote(key)] = unquote(value.replace("+", " "))
                self._send_json(
                    {
                        "ok": True,
                        "items": item_rows(
                            values.get("q", ""),
                            int(values.get("limit", 120)),
                        ),
                    }
                )
            except (ImportError, AttributeError, OSError, TypeError, ValueError) as exc:
                self._send_json(
                    {"ok": False, "error": str(exc) or "道具目录读取失败"},
                    HTTPStatus.BAD_REQUEST,
                )
            return
        if self.path.split("?", 1)[0] == "/api/runtime/config":
            request_host = self.headers.get("X-Forwarded-Host", "").strip()
            if not request_host:
                request_host = self.headers.get("Host", "")
            origin = runtime_origin(
                request_host,
                self.headers.get("X-Forwarded-Proto", ""),
            )
            asset_prefix = PREVIEW_ASSET_PREFIX.strip("/")
            proxy_port = int(os.environ.get("NARUTO_PROXY_PORT", "18681"))
            native_asset_base_url = f"{origin}/{asset_prefix}/"
            try:
                from fake_flash_server import (
                    ADVERTISE_HOST,
                    NATIVE_LOGIN_PORT,
                    SERVER_PORT,
                )

                advertise_host = runtime_advertise_host(request_host)
                game_port = SERVER_PORT
                native_game_port = NATIVE_LOGIN_PORT
            except (ImportError, AttributeError):
                advertise_host = runtime_advertise_host(request_host)
                game_port = int(os.environ.get("NARUTO_SERVER_PORT", "18684"))
                native_game_port = int(
                    os.environ.get("NARUTO_NATIVE_LOGIN_PORT", str(game_port))
                )
            self._send_json(
                {
                    "ok": True,
                    "runtimeInstanceId": RUNTIME_INSTANCE_ID,
                    "advertiseHost": advertise_host,
                    "previewRoot": asset_prefix,
                    "proxyPort": proxy_port,
                    "gamePort": game_port,
                    "nativeGamePort": native_game_port,
                    "nativeAssetBaseUrl": native_asset_base_url,
                    "nativeMovieUrl": native_asset_base_url + "Main.swf",
                    "topLinks": runtime_top_links(
                        request_host,
                        self.headers.get("X-Forwarded-Proto", ""),
                    ),
                }
            )
            return
        if self.path.split("?", 1)[0] == "/api/multiplayer/status":
            try:
                from fake_flash_server import DEFAULT_HUB

                with DEFAULT_HUB.lock:
                    online = [
                        {
                            "account": session.account_name,
                            "characterId": session.character.character_id,
                            "name": session.character.name,
                            "level": session.character.level,
                            "line": session.line_id,
                            "mapId": session.character.map_id,
                            "copyId": session.active_copy_id or 0,
                            "simulated": bool(
                                getattr(session, "server_simulated", False)
                            ),
                            "behavior": str(
                                getattr(session, "simulated_behavior", "")
                            ),
                        }
                        for session in DEFAULT_HUB.sessions_by_character.values()
                        if session.entered_game
                    ]
            except (ImportError, AttributeError):
                online = []
            self._send_json(
                {
                    "ok": True,
                    "mode": "multiplayer",
                    "maxCharacters": 1,
                    "onlineCount": len(online),
                    "online": online,
                }
            )
            return
        if self._is_private_path():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if self.path.split("?", 1)[0] == "/__swf_index.json":
            self._send_swf_index()
            return
        local_alias = self._find_local_path_alias()
        if local_alias:
            self.path = local_alias
        fallback = self._find_swf_name_fallback()
        if fallback:
            self.path = fallback
        if self._serve_online_swf_if_missing(include_body=True):
            return
        if self._serve_online_binary_if_missing(include_body=True):
            return
        super().do_GET()

    def do_POST(self) -> None:
        """Handle the local account launcher without exposing save files."""
        route = self.path.split("?", 1)[0]
        try:
            from nya_client_api_bridge import handle_microclient_post

            if handle_microclient_post(self):
                return
        except ImportError:
            pass
        if route == "/api/ops/arm":
            self._arm_operations_window()
            return
        if route == "/api/ops/disarm":
            self._disarm_operations_window()
            return
        if route == "/api/ops/unlock":
            self._unlock_operations_session()
            return
        if route == "/api/ops/native-ticket":
            self._issue_native_operations_ticket()
            return
        admin_route, operations_request = self._administrative_route(route)
        if operations_request and not self._require_operations_session():
            return
        if route in {
            "/api/player/gm-notifications",
            "/api/player/gm-notifications/read",
        }:
            try:
                payload = self._read_json_body()
                character_id = int(payload.get("characterId", 0))
                token = self.headers.get("X-Naruto-Player-Token", "")
                account = player_api_account(token)
                if account is None:
                    self._send_json(
                        {"ok": False, "error": "登录会话已过期，请重新登录"},
                        HTTPStatus.UNAUTHORIZED,
                    )
                    return
                owned_ids = {
                    int(character.get("id", 0))
                    for character in account.get("characters", [])
                }
                if character_id <= 0 and owned_ids:
                    character_id = min(owned_ids)
                if character_id <= 0 or character_id not in owned_ids:
                    self._send_json(
                        {"ok": False, "error": "角色不属于当前账号"},
                        HTTPStatus.FORBIDDEN,
                    )
                    return
                from gm_admin import player_notifications

                result = player_notifications(
                    character_id,
                    mark_read=route.endswith("/read"),
                )
                self._send_json({"ok": True, **result})
            except (ImportError, AttributeError, OSError, TypeError, ValueError) as exc:
                self._send_json(
                    {"ok": False, "error": str(exc) or "GM通知读取失败"},
                    HTTPStatus.BAD_REQUEST,
                )
            return
        if admin_route == "/api/gm/action":
            if not operations_request and not self._require_gm_auth():
                return
            try:
                payload = self._read_json_body()
                action = str(payload.pop("action", ""))
                from gm_admin import run_action

                result = run_action(action, payload)
                self._send_json({"ok": True, "result": result})
            except (ImportError, AttributeError, OSError, TypeError, ValueError) as exc:
                self._send_json(
                    {"ok": False, "error": str(exc) or "GM 操作失败"},
                    HTTPStatus.BAD_REQUEST,
                )
            return
        if route == "/api/native-flash/launch":
            self._launch_native_flash()
            return
        if route not in {
            "/api/account/login",
            "/api/account/register",
        }:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 64 * 1024:
                raise ValueError("请求内容无效")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("请求内容无效")
            username = str(payload.get("username", ""))
            password = str(payload.get("password", ""))
            native_flash_value = payload.get("nativeFlash")
            native_flash = native_flash_value is True or str(native_flash_value).lower() in {
                "1",
                "true",
                "yes",
            }
            request_host = self.headers.get("X-Forwarded-Host", "").strip()
            if not request_host:
                request_host = self.headers.get("Host", "")
            native_advertise_host = runtime_advertise_host(request_host)
            if route == "/api/account/register":
                account = ACCOUNT_SERVICE.register(username, password)
                response = {
                    "ok": True,
                    "account": account,
                    "playerApiToken": issue_player_api_token(int(account["id"])),
                }
                if native_flash:
                    response["nativeLaunchToken"] = (
                        ACCOUNT_SERVICE.issue_native_launch_token(
                            account["id"],
                            advertise_host=native_advertise_host,
                        )
                    )
                self._send_json(response)
                return
            account = ACCOUNT_SERVICE.authenticate(username, password)
            if account is None:
                self._send_json(
                    {"ok": False, "error": "账号或密码错误"},
                    HTTPStatus.UNAUTHORIZED,
                )
                return
            response = {
                "ok": True,
                "account": ACCOUNT_SERVICE.public_account(account),
                "playerApiToken": issue_player_api_token(int(account["id"])),
            }
            if native_flash:
                response["nativeLaunchToken"] = (
                    ACCOUNT_SERVICE.issue_native_launch_token(
                        account["id"],
                        advertise_host=native_advertise_host,
                    )
                )
            self._send_json(response)
        except (UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            self._send_json(
                {"ok": False, "error": str(exc) or "请求失败"},
                HTTPStatus.BAD_REQUEST,
            )

    def _require_gm_auth(self) -> bool:
        """Require the configured GM password for every administrative request."""
        supplied = self.headers.get("X-Naruto-GM-Password", "")
        # Keep the current configured password while accepting the historic
        # default used by the earlier one-click packages. This lets an existing
        # GM operator continue using NyaLocal#208 after a package update.
        accepted = (GM_PASSWORD, "NyaLocal#208")
        if any(value and _constant_time_text_equal(supplied, value) for value in accepted):
            return True
        self._send_json(
            {"ok": False, "error": "GM 口令错误"},
            HTTPStatus.UNAUTHORIZED,
        )
        return False

    def _arm_operations_window(self) -> None:
        """Arm the sequence only while the in-game keyboard window is open."""
        if not self._valid_operations_origin():
            return
        token = secrets.token_urlsafe(32)
        expires_at = time.time() + OPERATIONS_ARM_TTL_SECONDS
        with OPERATIONS_STATE_LOCK:
            self._purge_operations_state_locked()
            OPERATIONS_ARMS[token] = (str(self.client_address[0]), expires_at)
        self._send_json(
            {"ok": True, "expiresIn": OPERATIONS_ARM_TTL_SECONDS},
            headers={"Set-Cookie": self._operations_cookie(OPERATIONS_ARM_COOKIE, token, OPERATIONS_ARM_TTL_SECONDS)},
        )

    def _disarm_operations_window(self) -> None:
        """Invalidate the keyboard-window arm when that window closes."""
        token = self._cookie_value(OPERATIONS_ARM_COOKIE)
        if token:
            with OPERATIONS_STATE_LOCK:
                OPERATIONS_ARMS.pop(token, None)
        self._send_json(
            {"ok": True},
            headers={"Set-Cookie": self._operations_cookie(OPERATIONS_ARM_COOKIE, "", 0)},
        )

    def _unlock_operations_session(self) -> None:
        """Exchange the armed keyboard-window sequence for a short local session."""
        try:
            if not self._valid_operations_origin():
                return
            arm_token = self._cookie_value(OPERATIONS_ARM_COOKIE)
            now = time.time()
            with OPERATIONS_STATE_LOCK:
                self._purge_operations_state_locked(now)
                armed = OPERATIONS_ARMS.get(arm_token)
            if armed is None or armed[0] != str(self.client_address[0]) or armed[1] <= now:
                self._send_json(
                    {"ok": False, "error": "keyboard window is not armed"},
                    HTTPStatus.UNAUTHORIZED,
                )
                return
            payload = self._read_json_body(max_length=1024)
            supplied_digest = hashlib.sha256(
                str(payload.get("code", "")).encode("utf-8")
            ).hexdigest()
            if not secrets.compare_digest(supplied_digest, OPERATIONS_UNLOCK_CODE_SHA256):
                self._send_json(
                    {"ok": False, "error": "operations unlock failed"},
                    HTTPStatus.UNAUTHORIZED,
                )
                return
            session_token = secrets.token_urlsafe(32)
            expires_at = now + OPERATIONS_SESSION_TTL_SECONDS
            with OPERATIONS_STATE_LOCK:
                OPERATIONS_ARMS.pop(arm_token, None)
                OPERATIONS_SESSIONS[session_token] = (
                    str(self.client_address[0]),
                    expires_at,
                )
            self._send_json(
                {"ok": True, "expiresIn": OPERATIONS_SESSION_TTL_SECONDS},
                headers={
                    "Set-Cookie": self._operations_cookie(
                        OPERATIONS_SESSION_COOKIE,
                        session_token,
                        OPERATIONS_SESSION_TTL_SECONDS,
                    )
                },
            )
        except (UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            self._send_json(
                {"ok": False, "error": str(exc) or "operations unlock failed"},
                HTTPStatus.BAD_REQUEST,
            )

    def _issue_native_operations_ticket(self) -> None:
        """Create a one-use browser handoff for the native Flash host callback."""
        try:
            client_id = self._operations_client_id()
            if not self._operations_client_is_loopback(client_id):
                configured_secret = os.environ.get(
                    "NARUTO_OPS_REMOTE_SECRET", ""
                ).strip()
                supplied_secret = self.headers.get("X-Nya-Ops-Key", "").strip()
                forwarded_proto = self.headers.get(
                    "X-Forwarded-Proto", ""
                ).split(",", 1)[0].strip().lower()
                if (
                    not configured_secret
                    or not supplied_secret
                    or not _constant_time_text_equal(supplied_secret, configured_secret)
                    or forwarded_proto != "https"
                ):
                    self._send_json(
                        {"ok": False, "error": "remote operations access denied"},
                        HTTPStatus.FORBIDDEN,
                    )
                    return
            if self.headers.get("X-Nya-Native-Client", "").strip() != "NyaMicroClient/2.2.0":
                self._send_json(
                    {"ok": False, "error": "native client required"},
                    HTTPStatus.FORBIDDEN,
                )
                return
            payload = self._read_json_body(max_length=1024)
            supplied_digest = hashlib.sha256(
                str(payload.get("code", "")).encode("utf-8")
            ).hexdigest()
            if not secrets.compare_digest(supplied_digest, OPERATIONS_UNLOCK_CODE_SHA256):
                self._send_json(
                    {"ok": False, "error": "operations unlock failed"},
                    HTTPStatus.UNAUTHORIZED,
                )
                return

            ticket = secrets.token_urlsafe(32)
            expires_at = time.time() + OPERATIONS_NATIVE_TICKET_TTL_SECONDS
            with OPERATIONS_STATE_LOCK:
                self._purge_operations_state_locked()
                OPERATIONS_NATIVE_TICKETS[ticket] = (
                    client_id,
                    expires_at,
                )
            self._send_json(
                {
                    "ok": True,
                    "launch": "/ops/native-launch?ticket=" + quote(ticket, safe=""),
                    "expiresIn": OPERATIONS_NATIVE_TICKET_TTL_SECONDS,
                }
            )
        except (UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            self._send_json(
                {"ok": False, "error": str(exc) or "operations unlock failed"},
                HTTPStatus.BAD_REQUEST,
            )

    def _consume_native_operations_ticket(self) -> None:
        """Set the browser session cookie and redirect after a one-use handoff."""
        client_id = self._operations_client_id()
        query = urlsplit(self.path).query
        ticket = ""
        for pair in query.split("&") if query else ():
            key, separator, value = pair.partition("=")
            if separator and unquote(key) == "ticket":
                ticket = unquote(value.replace("+", " "))
                break
        now = time.time()
        with OPERATIONS_STATE_LOCK:
            self._purge_operations_state_locked(now)
            issued = OPERATIONS_NATIVE_TICKETS.pop(ticket, None)
            if issued is None or issued[0] != client_id or issued[1] <= now:
                session_token = ""
            else:
                session_token = secrets.token_urlsafe(32)
                OPERATIONS_SESSIONS[session_token] = (
                    client_id,
                    now + OPERATIONS_SESSION_TTL_SECONDS,
                )
        if not session_token:
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", "/ops/")
        self.send_header(
            "Set-Cookie",
            self._operations_cookie(
                OPERATIONS_SESSION_COOKIE,
                session_token,
                OPERATIONS_SESSION_TTL_SECONDS,
            ),
        )
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _valid_operations_origin(self) -> bool:
        if not self._operations_client_is_loopback(self._operations_client_id()):
            self._send_json(
                {"ok": False, "error": "operations access is local only"},
                HTTPStatus.FORBIDDEN,
            )
            return False
        origin = self.headers.get("Origin", "").strip()
        request_host = self.headers.get("Host", "").strip()
        if not origin or origin != runtime_origin(request_host):
            self._send_json(
                {"ok": False, "error": "invalid operations origin"},
                HTTPStatus.FORBIDDEN,
            )
            return False
        return True

    def _require_operations_session(self, page_request: bool = False) -> bool:
        if self._has_operations_session():
            return True
        if page_request:
            self.send_error(HTTPStatus.NOT_FOUND)
        else:
            self._send_json(
                {"ok": False, "error": "operations session required"},
                HTTPStatus.UNAUTHORIZED,
            )
        return False

    def _has_operations_session(self) -> bool:
        token = self._cookie_value(OPERATIONS_SESSION_COOKIE)
        if not token:
            return False
        now = time.time()
        with OPERATIONS_STATE_LOCK:
            self._purge_operations_state_locked(now)
            session = OPERATIONS_SESSIONS.get(token)
            if session is None:
                return False
            client_ip, expires_at = session
            if client_ip != self._operations_client_id() or expires_at <= now:
                OPERATIONS_SESSIONS.pop(token, None)
                return False
        return True

    def _operations_client_id(self) -> str:
        """Use a trusted local reverse proxy's first forwarded client address."""
        direct = str(self.client_address[0])
        trust_proxy = os.environ.get("NARUTO_OPS_TRUST_PROXY", "").strip().lower()
        if self._is_loopback_client() and trust_proxy in {"1", "true", "yes"}:
            forwarded = self.headers.get("X-Forwarded-For", "").split(",", 1)[0].strip()
            try:
                if forwarded:
                    return str(ipaddress.ip_address(forwarded))
            except ValueError:
                pass
        try:
            return str(ipaddress.ip_address(direct))
        except ValueError:
            return direct

    @staticmethod
    def _operations_client_is_loopback(client_id: str) -> bool:
        try:
            return ipaddress.ip_address(client_id).is_loopback
        except ValueError:
            return False

    def _cookie_value(self, name: str) -> str:
        cookie_header = self.headers.get("Cookie", "")
        if not cookie_header:
            return ""
        try:
            cookies = SimpleCookie()
            cookies.load(cookie_header)
            morsel = cookies.get(name)
            return morsel.value if morsel is not None else ""
        except (AttributeError, KeyError):
            return ""

    def _operations_cookie(self, name: str, value: str, max_age: int) -> str:
        cookie = (
            f"{name}={value}; Path=/; Max-Age={max_age}; "
            "HttpOnly; SameSite=Strict"
        )
        if self.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip().lower() == "https":
            cookie += "; Secure"
        return cookie

    def _is_loopback_client(self) -> bool:
        try:
            return ipaddress.ip_address(self.client_address[0]).is_loopback
        except ValueError:
            return False

    @staticmethod
    def _purge_operations_state_locked(now: float | None = None) -> None:
        timestamp = time.time() if now is None else now
        for table in (
            OPERATIONS_ARMS,
            OPERATIONS_SESSIONS,
            OPERATIONS_NATIVE_TICKETS,
        ):
            expired = [
                token
                for token, (_, expires_at) in table.items()
                if expires_at <= timestamp
            ]
            for token in expired:
                table.pop(token, None)

    @staticmethod
    def _administrative_route(route: str) -> tuple[str, bool]:
        prefix = "/api/ops/"
        if route.startswith(prefix):
            return "/api/gm/" + route[len(prefix) :], True
        return route, False

    def _read_json_body(self, max_length: int = 256 * 1024) -> dict[str, object]:
        """Read one bounded JSON object from an administrative request."""
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > max_length:
            raise ValueError("请求内容无效")
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("请求内容必须是对象")
        return payload

    def _launch_native_flash(self) -> None:
        """Start the fixed local ActiveX host for this instance's Nya client."""
        try:
            if not ipaddress.ip_address(self.client_address[0]).is_loopback:
                raise PermissionError("原生 Flash 只能从本机启动。")
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 64 * 1024:
                raise ValueError("原生 Flash 启动参数无效。")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("原生 Flash 启动参数无效。")
            movie = str(payload.get("movie", ""))
            flash_vars = str(payload.get("flashvars", ""))
            title = str(payload.get("title", "火影世界"))[:80]
            parsed_movie = urlsplit(movie)
            movie_hostname = (parsed_movie.hostname or "").lower()
            try:
                movie_is_loopback = ipaddress.ip_address(movie_hostname).is_loopback
            except ValueError:
                movie_is_loopback = movie_hostname == "localhost"
            expected_path = "/" + PREVIEW_ASSET_PREFIX.strip("/") + "/Main.swf"
            if (
                parsed_movie.scheme not in {"http", "https"}
                or parsed_movie.path != expected_path
                or not movie_is_loopback
                or parsed_movie.port != HTTP_PORT
            ):
                raise ValueError("只允许启动当前本机 Nya 2.0.8 客户端。")
            if len(flash_vars) > 48 * 1024 or "password=launch%3A" not in flash_vars:
                raise ValueError("原生 Flash 登录票据无效。")
            self._ensure_native_flash_host()
            native_log_path = WWW_ROOT.parent / "logs" / "native-flash-host.log"
            native_log_path.parent.mkdir(parents=True, exist_ok=True)
            config_lines = [
                base64.b64encode(value.encode("utf-8")).decode("ascii")
                for value in (movie, flash_vars, title, str(native_log_path))
            ]
            handle, config_path = tempfile.mkstemp(
                prefix="naruto-native-flash-", suffix=".launch"
            )
            try:
                with os.fdopen(handle, "w", encoding="ascii", newline="\n") as stream:
                    stream.write("\n".join(config_lines))
                subprocess.Popen(
                    [str(NATIVE_FLASH_HOST_EXE), config_path],
                    cwd=str(WWW_ROOT.parent),
                    close_fds=True,
                )
            except Exception:
                Path(config_path).unlink(missing_ok=True)
                raise
            self._send_json({"ok": True, "mode": "standalone-activex"})
        except (OSError, ValueError, PermissionError, json.JSONDecodeError) as exc:
            self._send_json(
                {"ok": False, "error": str(exc) or "原生 Flash 启动失败。"},
                HTTPStatus.BAD_REQUEST,
            )

    @staticmethod
    def _ensure_native_flash_host() -> None:
        """Compile the tiny x86 host when its source is newer than the executable."""
        with NATIVE_FLASH_BUILD_LOCK:
            if (
                NATIVE_FLASH_HOST_EXE.is_file()
                and NATIVE_FLASH_HOST_EXE.stat().st_mtime
                >= NATIVE_FLASH_HOST_SOURCE.stat().st_mtime
            ):
                return
            if not NATIVE_FLASH_CSC.is_file():
                raise OSError("缺少 32 位 .NET Framework 编译器。")
            result = subprocess.run(
                [
                    str(NATIVE_FLASH_CSC),
                    "/nologo",
                    "/target:winexe",
                    "/platform:x86",
                    "/optimize+",
                    "/reference:System.dll",
                    "/reference:System.Drawing.dll",
                    "/reference:System.Windows.Forms.dll",
                    f"/out:{NATIVE_FLASH_HOST_EXE}",
                    str(NATIVE_FLASH_HOST_SOURCE),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                detail = (result.stdout + result.stderr).strip()
                raise OSError("原生 Flash 宿主编译失败：" + detail)

    def do_HEAD(self) -> None:
        """Apply the same SWF filename fallback to HEAD requests."""
        route = self.path.split("?", 1)[0]
        if route in {"/ops", "/ops/", "/ops/index.html"}:
            if not self._require_operations_session(page_request=True):
                return
            original_path = self.path
            self.path = "/gm/index.html"
            try:
                super().do_HEAD()
            finally:
                self.path = original_path
            return
        if self._is_private_path():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        local_alias = self._find_local_path_alias()
        if local_alias:
            self.path = local_alias
        fallback = self._find_swf_name_fallback()
        if fallback:
            self.path = fallback
        if self._serve_online_swf_if_missing(include_body=False):
            return
        if self._serve_online_binary_if_missing(include_body=False):
            return
        super().do_HEAD()

    def _is_private_path(self) -> bool:
        """Keep editor state under the web root inaccessible to the game server."""
        request_path = unquote(urlsplit(self.path).path).lstrip("/\\")
        candidate = (WWW_ROOT / request_path).resolve()
        return any(candidate == root or root in candidate.parents for root in PRIVATE_ROOTS)

    def _find_local_path_alias(self) -> str | None:
        """Map legacy and preview URLs to the single active resource tree."""
        split = urlsplit(self.path)
        request_path = unquote(split.path).lstrip("/")
        alias = LOCAL_PATH_ALIASES.get(request_path)
        if alias and (WWW_ROOT / alias).is_file():
            return "/" + alias + (("?" + split.query) if split.query else "")
        # Some packaged SWFs retain the original application prefix in their
        # internal URLs. In an isolated preview, prefer the overlaid root for
        # those requests so UI layouts are actually loaded from the isolation.
        if (
            PREVIEW_ASSET_PREFIX != LEGACY_PREVIEW_ASSET_PREFIX
            and request_path.startswith(LEGACY_PREVIEW_ASSET_PREFIX)
        ):
            relative = request_path[len(LEGACY_PREVIEW_ASSET_PREFIX) :]
            isolated = WWW_ROOT / PREVIEW_ASSET_PREFIX / relative
            if isolated.is_file():
                mapped = PREVIEW_ASSET_PREFIX + relative
                return "/" + mapped + (("?" + split.query) if split.query else "")
        if request_path.startswith(PREVIEW_ASSET_PREFIX):
            preview_path = WWW_ROOT / request_path
            fallback = LOCAL_ASSET_PREFIX + request_path[len(PREVIEW_ASSET_PREFIX) :]
            if not preview_path.is_file():
                # Route absent isolated assets through the validated online cache.
                # The cache helpers only accept known SWF/image/audio signatures.
                return "/" + fallback + (("?" + split.query) if split.query else "")
        return None

    def _find_swf_name_fallback(self) -> str | None:
        """Map missing legacy names like asset.swf to asset[1].swf when present."""
        split = urlsplit(self.path)
        request_path = unquote(split.path).lstrip("/")
        if not request_path.endswith(".swf"):
            return None
        root = WWW_ROOT
        local_path = root / request_path
        if local_path.exists():
            return None
        alias = LOCAL_SWF_ALIASES.get(request_path)
        if alias:
            alias_path = root / alias
            if alias_path.exists():
                relative = "/" + alias_path.relative_to(root).as_posix()
                return relative + (("?" + split.query) if split.query else "")
        fallback_path = local_path.with_name(local_path.stem + "[1]" + local_path.suffix)
        if not fallback_path.exists():
            return None
        relative = "/" + fallback_path.relative_to(root).as_posix()
        return relative + (("?" + split.query) if split.query else "")

    def _serve_online_swf_if_missing(self, include_body: bool) -> bool:
        """Download, cache, and serve one missing SWF from an original online asset root."""
        split = urlsplit(self.path)
        request_path = unquote(split.path).lstrip("/")
        if not request_path.startswith(LOCAL_ASSET_PREFIX) or not request_path.endswith(".swf"):
            return False
        local_path = (WWW_ROOT / request_path).resolve()
        try:
            local_path.relative_to(WWW_ROOT.resolve())
        except ValueError:
            return False
        if local_path.is_file():
            return False
        relative_path = request_path[len(LOCAL_ASSET_PREFIX) :]
        body = self._download_online_swf(relative_path)
        if body is None:
            return False
        self._cache_asset(local_path, body)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/x-shockwave-flash")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if include_body:
            self.wfile.write(body)
        print(f"[online cache] {request_path} ({len(body)} bytes)", flush=True)
        return True

    def _serve_online_binary_if_missing(self, include_body: bool) -> bool:
        """Fetch missing original image/audio assets, validate signatures, and cache atomically."""
        split = urlsplit(self.path)
        request_path = unquote(split.path).lstrip("/")
        suffix = Path(request_path).suffix.lower()
        signatures = BINARY_ASSET_SIGNATURES.get(suffix)
        if not request_path.startswith(LOCAL_ASSET_PREFIX) or signatures is None:
            return False
        local_path = (WWW_ROOT / request_path).resolve()
        try:
            local_path.relative_to(WWW_ROOT.resolve())
        except ValueError:
            return False
        if local_path.is_file():
            return False
        relative_path = request_path[len(LOCAL_ASSET_PREFIX) :]
        body = self._download_online_binary(relative_path, signatures)
        if body is None:
            return False
        self._cache_asset(local_path, body)
        content_type = mimetypes.guess_type(local_path.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if include_body:
            self.wfile.write(body)
        print(f"[online cache] {request_path} ({len(body)} bytes)", flush=True)
        return True

    @staticmethod
    def _download_online_binary(relative_path: str, signatures: tuple[bytes, ...]) -> bytes | None:
        """Try original roots and accept only image/audio payloads with a known signature."""
        encoded_path = quote(relative_path, safe="/")
        for online_root in ONLINE_ASSET_ROOTS:
            online_url = f"{online_root}{encoded_path}?{ONLINE_ASSET_VERSION}"
            request = Request(
                online_url,
                headers={"Referer": ONLINE_REFERER, "User-Agent": "Mozilla/5.0 FlashSinglePlayer/1.0"},
            )
            try:
                with urlopen(request, timeout=ONLINE_TIMEOUT_SECONDS) as response:
                    body = response.read()
            except (HTTPError, URLError, TimeoutError, OSError) as exc:
                print(f"[online fallback] {online_url}: {exc}", flush=True)
                continue
            if body.startswith(signatures):
                return body
            print(f"[online fallback] rejected invalid binary asset: {online_url}", flush=True)
        return None

    @staticmethod
    def _download_online_swf(relative_path: str, report_errors: bool = True) -> bytes | None:
        """Try known online roots and reject HTML or corrupt data before caching."""
        online_relative_path = relative_path.replace("[1].swf", ".swf")
        encoded_path = quote(online_relative_path, safe="/")
        for online_root in ONLINE_ASSET_ROOTS:
            online_url = f"{online_root}{encoded_path}?{ONLINE_ASSET_VERSION}"
            request = Request(
                online_url,
                headers={
                    "Referer": ONLINE_REFERER,
                    "User-Agent": "Mozilla/5.0 FlashSinglePlayer/1.0",
                },
            )
            try:
                with urlopen(request, timeout=ONLINE_TIMEOUT_SECONDS) as response:
                    body = response.read()
            except (HTTPError, URLError, TimeoutError, OSError) as exc:
                if report_errors:
                    print(f"[online fallback] {online_url}: {exc}", flush=True)
                continue
            if body.startswith(SWF_SIGNATURES):
                return body
            if report_errors:
                print(f"[online fallback] rejected invalid SWF: {online_url}", flush=True)
        return None

    @staticmethod
    def _cache_asset(local_path: Path, body: bytes) -> None:
        """Persist one validated resource with atomic replacement for concurrent requests."""
        with CACHE_WRITE_LOCK:
            if local_path.is_file():
                return
            local_path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path = local_path.with_name(f".{local_path.name}.tmp")
            temporary_path.write_bytes(body)
            temporary_path.replace(local_path)

    def _send_swf_index(self) -> None:
        """Return all SWF resources grouped by their first resource directory."""
        root = WWW_ROOT
        swf_root = root / "act_web_tiyan" / "swf"
        entries = []
        if swf_root.exists():
            for file_path in sorted(swf_root.rglob("*.swf")):
                relative = file_path.relative_to(root).as_posix()
                parts = file_path.relative_to(swf_root).parts
                category = parts[0] if len(parts) > 1 else "root"
                entries.append(
                    {
                        "path": "/" + relative,
                        "category": category,
                        "name": file_path.name,
                        "isUiLibrary": category == "UISwf",
                    }
                )
        body = json.dumps({"entries": entries}, ensure_ascii=False).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(
        self,
        payload: dict[str, object],
        status: HTTPStatus = HTTPStatus.OK,
        headers: dict[str, str] | None = None,
    ) -> None:
        """Return one no-cache UTF-8 JSON response."""
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)


def serve_forever() -> None:
    """Start the static HTTP server for LAN clients."""
    server = ThreadingHTTPServer((HTTP_HOST, HTTP_PORT), FlashStaticHandler)
    print(f"Static Flash HTTP server listening on {HTTP_HOST}:{HTTP_PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    serve_forever()
