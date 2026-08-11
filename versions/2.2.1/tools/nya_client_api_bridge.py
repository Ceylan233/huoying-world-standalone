#!/usr/bin/env python3
"""PHP-compatible launcher API bridge for NyaMicroClient / NyaClient.

Maps api_login.php and related endpoints onto the local AccountService and
native Flash launch tokens used by flash-native.html.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from http import HTTPStatus
from threading import Lock
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlsplit

from multiplayer_backend import ACCOUNT_SERVICE


SESSION_TTL_SECONDS = 12 * 60 * 60.0
SESSION_LOCK = Lock()
# session_id -> (account_id, username, expires_monotonic)
LAUNCHER_SESSIONS: dict[str, tuple[int, str, float]] = {}

CAPTCHA_PNG = bytes(
    [
        0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 0x00, 0x00, 0x00, 0x0D,
        0x49, 0x48, 0x44, 0x52, 0x00, 0x00, 0x00, 0x50, 0x00, 0x00, 0x00, 0x28,
        0x08, 0x02, 0x00, 0x00, 0x00, 0x1F, 0x7A, 0x72, 0xF2, 0x00, 0x00, 0x00,
        0x0C, 0x49, 0x44, 0x41, 0x54, 0x78, 0x9C, 0x63, 0xF8, 0xCF, 0xC0, 0x00,
        0x00, 0x00, 0x03, 0x00, 0x01, 0x00, 0x05, 0xFE, 0xD4, 0xEF, 0x00, 0x00,
        0x00, 0x00, 0x49, 0x45, 0x4E, 0x44, 0xAE, 0x42, 0x60, 0x82,
    ]
)

MICROCLIENT_GET_ROUTES = {
    "/captcha.php",
    "/api_servers.php",
    "/api_game_launch.php",
    "/api_launcher_news.php",
    "/api_launcher_routes.php",
}
MICROCLIENT_POST_ROUTES = {
    "/api_login.php",
    "/api_register.php",
    "/api_change_password.php",
}


def _prune_sessions_locked() -> None:
    now = time.monotonic()
    expired = [
        session_id
        for session_id, (_, _, expires_at) in LAUNCHER_SESSIONS.items()
        if expires_at <= now
    ]
    for session_id in expired:
        LAUNCHER_SESSIONS.pop(session_id, None)


def issue_launcher_session(account_id: int, username: str) -> str:
    """Create a PHPSESSID-compatible launcher session for api_game_launch."""
    session_id = secrets.token_urlsafe(24)
    with SESSION_LOCK:
        _prune_sessions_locked()
        LAUNCHER_SESSIONS[session_id] = (
            int(account_id),
            str(username),
            time.monotonic() + SESSION_TTL_SECONDS,
        )
    return session_id


def resolve_launcher_session(cookie_header: str, session_id: str = "") -> dict[str, Any] | None:
    """Resolve an account from Cookie PHPSESSID or an explicit session id."""
    resolved = (session_id or "").strip()
    if not resolved and cookie_header:
        for part in cookie_header.split(";"):
            name, separator, value = part.strip().partition("=")
            if separator and name.strip().casefold() == "phpsessid":
                resolved = value.strip()
                break
    if not resolved:
        return None
    with SESSION_LOCK:
        _prune_sessions_locked()
        payload = LAUNCHER_SESSIONS.get(resolved)
        if payload is None:
            return None
        account_id, username, expires_at = payload
        # Sliding renewal keeps multi-window launches usable.
        LAUNCHER_SESSIONS[resolved] = (
            account_id,
            username,
            time.monotonic() + SESSION_TTL_SECONDS,
        )
    account = ACCOUNT_SERVICE.account_by_id(account_id)
    if account is None:
        return None
    if ACCOUNT_SERVICE._key(str(account.get("username", ""))) != ACCOUNT_SERVICE._key(username):
        return None
    return account


def _query_values(path: str) -> dict[str, str]:
    query = urlsplit(path).query
    values: dict[str, str] = {}
    for pair in query.split("&") if query else ():
        key, separator, value = pair.partition("=")
        if separator:
            values[unquote(key)] = unquote(value.replace("+", " "))
    return values


def _read_payload(handler: Any) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0") or 0)
    if length <= 0 or length > 64 * 1024:
        raise ValueError("请求内容无效")
    raw = handler.rfile.read(length)
    content_type = str(handler.headers.get("Content-Type", "")).lower()
    text = raw.decode("utf-8")
    if "application/json" in content_type or text.lstrip().startswith("{"):
        payload = json.loads(text)
        if not isinstance(payload, dict):
            raise ValueError("请求内容无效")
        return payload
    form = parse_qs(text, keep_blank_values=True)
    return {key: (values[-1] if values else "") for key, values in form.items()}


def _encode_flash_vars(values: dict[str, str]) -> str:
    return "&".join(
        f"{quote_component(key)}={quote_component(value)}"
        for key, value in values.items()
    )


def quote_component(value: str) -> str:
    return quote(str(value), safe="")


def _build_link_buttons(entries: list[dict[str, object]]) -> str:
    rows: list[str] = []
    for entry in entries:
        label = str(entry.get("label", "")).replace(";", "").replace(",", "")
        url = str(entry.get("url", "")).replace(";", "%3B").replace(",", "%2C")
        if label and url.lower().startswith(("http://", "https://")):
            highlight = "1" if entry.get("highlight") else "0"
            rows.append(",".join((label, "url", url, highlight)))
    return ";".join(rows)


def _runtime_launch_context(handler: Any) -> dict[str, Any]:
    from static_flash_http import (
        PREVIEW_ASSET_PREFIX,
        RUNTIME_INSTANCE_ID,
        runtime_advertise_host,
        runtime_origin,
        runtime_top_links,
    )

    request_host = handler.headers.get("X-Forwarded-Host", "").strip()
    if not request_host:
        request_host = handler.headers.get("Host", "")
    origin = runtime_origin(
        request_host,
        handler.headers.get("X-Forwarded-Proto", ""),
    )
    asset_prefix = PREVIEW_ASSET_PREFIX.strip("/")
    native_asset_base_url = f"{origin}/{asset_prefix}/"
    try:
        from fake_flash_server import ADVERTISE_HOST, NATIVE_LOGIN_PORT, SERVER_PORT

        advertise_host = runtime_advertise_host(request_host) or ADVERTISE_HOST
        game_port = int(SERVER_PORT)
        native_game_port = int(NATIVE_LOGIN_PORT)
    except (ImportError, AttributeError, TypeError, ValueError):
        advertise_host = runtime_advertise_host(request_host)
        game_port = int(os.environ.get("NARUTO_SERVER_PORT", "19284"))
        native_game_port = int(
            os.environ.get("NARUTO_NATIVE_LOGIN_PORT", str(game_port + 2))
        )
    return {
        "origin": origin,
        "advertiseHost": advertise_host or "127.0.0.1",
        "nativeGamePort": native_game_port,
        "gamePort": game_port,
        "nativeAssetBaseUrl": native_asset_base_url,
        "nativeMovieUrl": native_asset_base_url + "Main.swf",
        "runtimeInstanceId": RUNTIME_INSTANCE_ID,
        "topLinks": runtime_top_links(
            request_host,
            handler.headers.get("X-Forwarded-Proto", ""),
        ),
    }


def _send_bridge_json(
    handler: Any,
    payload: dict[str, object],
    status: HTTPStatus = HTTPStatus.OK,
    set_cookie: str = "",
) -> None:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    if set_cookie:
        handler.send_header("Set-Cookie", set_cookie)
    handler.end_headers()
    handler.wfile.write(body)


def _send_bytes(
    handler: Any,
    body: bytes,
    content_type: str,
    extra_headers: dict[str, str] | None = None,
) -> None:
    handler.send_response(HTTPStatus.OK)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    for name, value in (extra_headers or {}).items():
        handler.send_header(name, value)
    handler.end_headers()
    handler.wfile.write(body)


def handle_microclient_get(handler: Any) -> bool:
    """Serve NyaMicroClient GET endpoints. Returns True when handled."""
    route = handler.path.split("?", 1)[0]
    if route not in MICROCLIENT_GET_ROUTES:
        return False

    if route == "/captcha.php":
        token = secrets.token_hex(8)
        _send_bytes(
            handler,
            CAPTCHA_PNG,
            "image/png",
            {"X-Captcha-Token": token},
        )
        return True

    if route == "/api_servers.php":
        try:
            from fake_flash_server import DEFAULT_HUB

            with DEFAULT_HUB.lock:
                online = sum(
                    1
                    for session in DEFAULT_HUB.sessions_by_character.values()
                    if session.entered_game
                )
        except (ImportError, AttributeError):
            online = 0
        _send_bridge_json(
            handler,
            {
                "success": True,
                "data": [
                    {
                        "id": 1,
                        "name": "第1区 木叶村",
                        "open": True,
                        "is_new": True,
                        "status": "online",
                        "online": online,
                        "online_count": online,
                    }
                ],
            },
        )
        return True

    if route == "/api_launcher_news.php":
        _send_bridge_json(
            handler,
            {
                "success": True,
                "data": {
                    "title": "本地一键端",
                    "html": (
                        "<b>Nya 本地测试环境</b><br/>"
                        "1. 微端登录已桥接 api_login.php<br/>"
                        "2. 进游戏使用 password=launch:token<br/>"
                        "3. 存档位于 save 目录"
                    ),
                    "content": "本地一键端已就绪，可直接登录进游戏。",
                    "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                },
            },
        )
        return True

    if route == "/api_launcher_routes.php":
        local_url = os.environ.get("NARUTO_LOCAL_API_URL", "http://127.0.0.1:19280/")
        _send_bridge_json(
            handler,
            {
                "success": True,
                "data": {
                    "routes": [
                        {
                            "id": "local",
                            "name": "本地一键端",
                            "url": local_url,
                            "ftp_host": "127.0.0.1",
                            "ftp_port": 21,
                            "ftp_user": "anonymous",
                            "ftp_pass": "",
                            "ftp_path": "/micro",
                            "tag": "127.0.0.1:19280",
                        }
                    ]
                },
            },
        )
        return True

    if route == "/api_game_launch.php":
        values = _query_values(handler.path)
        server_id = int(values.get("serverid") or values.get("serverId") or "1")
        account = resolve_launcher_session(handler.headers.get("Cookie", ""))
        if account is None:
            _send_bridge_json(
                handler,
                {"success": False, "message": "登录会话已过期，请重新登录"},
                HTTPStatus.UNAUTHORIZED,
            )
            return True

        request_host = handler.headers.get("X-Forwarded-Host", "").strip()
        if not request_host:
            request_host = handler.headers.get("Host", "")
        from static_flash_http import runtime_advertise_host

        advertise_host = runtime_advertise_host(request_host)
        launch_token = ACCOUNT_SERVICE.issue_native_launch_token(
            int(account["id"]),
            advertise_host=advertise_host,
        )
        public = ACCOUNT_SERVICE.public_account(account)
        characters = public.get("characters") or []
        character = characters[0] if characters else None
        username = str(public["username"])
        runtime = _runtime_launch_context(handler)
        host = str(runtime["advertiseHost"])
        port = str(runtime["nativeGamePort"] or runtime["gamePort"])
        asset_base = str(runtime["nativeAssetBaseUrl"])
        movie = (
            f"{asset_base.rstrip('/')}/Main.swf"
            f"?runtime={quote_component(str(runtime['runtimeInstanceId']))}"
            f"&create=native-microclient"
        )
        flash_vars = {
            "ip": host,
            "server": host,
            "port": port,
            "baseUrl": asset_base,
            "language": "zh-Hans",
            "simplifiedChinese": "1",
            "userName": username,
            "username": username,
            "password": f"launch:{launch_token}",
            "userId": str(character["id"] if character else 0),
            "characterId": str(character["id"] if character else 0),
            "serverId": str(server_id),
            "line": "1",
            "linkButtons": _build_link_buttons(list(runtime["topLinks"])),
            "partner": "gongyi",
            "local": "1",
            "enableDownload": "1",
            "forbidXiaoFei": "0",
            "fightNotice": "0",
            "hideXianFa": "0",
            "hideFavMenu": "0",
            "kuafucharge": "1",
            "huigui": "1",
            "shituSwitch": "1",
            "gameSwitch": (
                "lunhuiyan:0_kunchong:0_jieyin:0_zhongrenkaoshi:0_huoyuedu:0_"
                "chongwuroughun:0_famillyshop:0_mysteryshop:0_juedou:0"
            ),
        }
        encoded_flash_vars = _encode_flash_vars(flash_vars)
        now = str(int(time.time()))
        _send_bridge_json(
            handler,
            {
                "success": True,
                "data": {
                    "serverName": "第1区 木叶村",
                    "server_name": "第1区 木叶村",
                    "serverId": server_id,
                    "server_id": server_id,
                    "baseUrl": asset_base,
                    "base_url": asset_base,
                    "swfUrl": movie,
                    "swf_url": movie,
                    "userId": str(character["id"] if character else public["id"]),
                    "user_id": str(character["id"] if character else public["id"]),
                    "userName": username,
                    "user_name": username,
                    "sign": "local-" + secrets.token_hex(8),
                    "time": now,
                    "launchTicket": launch_token,
                    "launch_ticket": launch_token,
                    "flashVars": encoded_flash_vars,
                    "flash_vars": encoded_flash_vars,
                },
            },
        )
        return True

    return False


def handle_microclient_post(handler: Any) -> bool:
    """Serve NyaMicroClient POST endpoints. Returns True when handled."""
    route = handler.path.split("?", 1)[0]
    if route not in MICROCLIENT_POST_ROUTES:
        return False

    try:
        payload = _read_payload(handler)
        username = str(payload.get("username", "")).strip()
        password = str(payload.get("password", ""))

        if route == "/api_login.php":
            account = ACCOUNT_SERVICE.authenticate(username, password)
            if account is None:
                _send_bridge_json(
                    handler,
                    {"success": False, "message": "账号或密码错误"},
                    HTTPStatus.UNAUTHORIZED,
                )
                return True
            session_id = issue_launcher_session(int(account["id"]), str(account["username"]))
            _send_bridge_json(
                handler,
                {
                    "success": True,
                    "data": {
                        "session_id": session_id,
                        "username": str(account["username"]),
                        "account_id": int(account["id"]),
                    },
                },
                set_cookie=f"PHPSESSID={session_id}; Path=/; HttpOnly",
            )
            return True

        if route == "/api_register.php":
            email = str(payload.get("email", "")).strip()
            confirm = str(payload.get("confirm_password", password))
            if password != confirm:
                raise ValueError("两次密码不一致")
            account = ACCOUNT_SERVICE.register(username, password)
            # Email is accepted for contract compatibility but not persisted yet.
            _ = email
            _send_bridge_json(
                handler,
                {
                    "success": True,
                    "data": {
                        "username": account["username"],
                        "account_id": int(account["id"]),
                    },
                    "message": "注册成功",
                },
            )
            return True

        if route == "/api_change_password.php":
            current_password = str(payload.get("current_password", ""))
            new_password = str(payload.get("new_password", ""))
            confirm_new = str(payload.get("confirm_new_password", new_password))
            if new_password != confirm_new:
                raise ValueError("两次新密码不一致")
            ACCOUNT_SERVICE.change_password(username, current_password, new_password)
            _send_bridge_json(
                handler,
                {"success": True, "message": "密码已修改"},
            )
            return True
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        _send_bridge_json(
            handler,
            {"success": False, "message": str(exc) or "请求失败"},
            HTTPStatus.BAD_REQUEST,
        )
        return True

    return False
