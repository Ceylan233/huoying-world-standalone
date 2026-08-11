#!/usr/bin/env python3
"""Start the isolated unpacked-client preview on alternate local ports."""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PREVIEW_ROOT_NAME = os.environ.get("NARUTO_PREVIEW_ROOT", "act_web_hysj_preview")
PREVIEW_PAGE = os.environ.get("NARUTO_PREVIEW_PAGE", "hysj-preview.html")
PREVIEW_ROOT = PROJECT_ROOT / "www" / PREVIEW_ROOT_NAME
HTTP_PORT = int(os.environ.get("NARUTO_HTTP_PORT", "18780"))
PROXY_PORT = int(os.environ.get("NARUTO_PROXY_PORT", "18781"))
GAME_PORT = int(os.environ.get("NARUTO_PROXY_GAME_PORT", "18784"))
CHANNEL_PORT = int(os.environ.get("NARUTO_CHANNEL_PORT", str(GAME_PORT)))
NATIVE_LOGIN_PORT = int(os.environ.get("NARUTO_NATIVE_LOGIN_PORT", str(GAME_PORT)))
PREVIEW_URL = f"http://127.0.0.1:{HTTP_PORT}/{PREVIEW_PAGE}"
PREVIEW_PORTS = tuple(
    dict.fromkeys(
        (HTTP_PORT, PROXY_PORT, GAME_PORT, CHANNEL_PORT, NATIVE_LOGIN_PORT)
    )
)

os.environ["NARUTO_HTTP_PORT"] = str(HTTP_PORT)
os.environ["NARUTO_PROXY_PORT"] = str(PROXY_PORT)
os.environ["NARUTO_PROXY_GAME_PORT"] = str(GAME_PORT)
os.environ["NARUTO_SERVER_PORT"] = str(GAME_PORT)
os.environ["NARUTO_GAME_DATA_PATH"] = str(PREVIEW_ROOT / "dat" / "GameData.dat")
os.environ["NARUTO_SAVE_PATH"] = str(
    PROJECT_ROOT / "save" / os.environ.get("NARUTO_PREVIEW_SAVE", "hysj-preview.json")
)
os.environ["NARUTO_PREVIEW_ASSET_PREFIX"] = PREVIEW_ROOT_NAME.rstrip("/") + "/"

from fake_flash_server import (  # noqa: E402
    serve_flash_policy_forever,
    serve_forever as serve_game,
)
from static_flash_http import serve_forever as serve_http  # noqa: E402

try:
    # Ruffle is intentionally not bundled in the native 2.2.0 package.  Keep
    # this legacy preview helper optional so the normal local server can start
    # without that removed client-side dependency.
    from local_socket_proxy import serve_forever as serve_socket_proxy  # noqa: E402
except ModuleNotFoundError:
    serve_socket_proxy = None


def attach_headless_logs() -> None:
    log_directory = PROJECT_ROOT / "logs"
    log_directory.mkdir(parents=True, exist_ok=True)
    if sys.stdout is None:
        sys.stdout = (log_directory / "hysj-preview.out.log").open(
            "a", encoding="utf-8", buffering=1
        )
    if sys.stderr is None:
        sys.stderr = (log_directory / "hysj-preview.err.log").open(
            "a", encoding="utf-8", buffering=1
        )


def is_port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.25):
            return True
    except OSError:
        return False


def open_preview_when_ready() -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            with urlopen(PREVIEW_URL, timeout=1):
                webbrowser.open(PREVIEW_URL)
                return
        except (OSError, URLError):
            time.sleep(0.25)


def main() -> None:
    attach_headless_logs()
    if not PREVIEW_ROOT.is_dir():
        raise SystemExit(f"Preview client is missing: {PREVIEW_ROOT}")
    occupied = [port for port in PREVIEW_PORTS if is_port_open(port)]
    if occupied:
        raise SystemExit(f"Preview ports are already occupied: {occupied}")
    threading.Thread(target=serve_http, name="hysj-preview-http", daemon=True).start()
    threading.Thread(
        target=serve_flash_policy_forever,
        name="hysj-preview-flash-policy",
        daemon=True,
    ).start()
    if serve_socket_proxy is not None:
        threading.Thread(
            target=serve_socket_proxy,
            name="hysj-preview-local-socket",
            daemon=True,
        ).start()
    if os.environ.get("NARUTO_NO_BROWSER") != "1":
        threading.Thread(target=open_preview_when_ready, daemon=True).start()
    print(f"HYSJ unpacked-client preview: {PREVIEW_URL}", flush=True)
    serve_game()


if __name__ == "__main__":
    main()
