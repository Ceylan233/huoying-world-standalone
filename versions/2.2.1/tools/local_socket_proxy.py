#!/usr/bin/env python3
"""Minimal WebSocket-to-TCP bridge used by the local Ruffle player."""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import struct
import threading
from http import HTTPStatus


PROXY_HOST = os.environ.get("NARUTO_PROXY_HOST", "127.0.0.1")
PROXY_PORT = int(os.environ.get("NARUTO_PROXY_PORT", "18681"))
GAME_HOST = os.environ.get("NARUTO_PROXY_GAME_HOST", "127.0.0.1")
GAME_PORT = int(os.environ.get("NARUTO_PROXY_GAME_PORT", "18684"))
WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_FRAME_SIZE = 2 * 1024 * 1024


def _read_exact(sock: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ConnectionError("socket closed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _read_http_headers(sock: socket.socket) -> tuple[str, dict[str, str]]:
    data = bytearray()
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("client closed before WebSocket handshake")
        data.extend(chunk)
        if len(data) > 64 * 1024:
            raise ValueError("WebSocket handshake is too large")
    head = bytes(data).split(b"\r\n\r\n", 1)[0].decode("iso-8859-1")
    lines = head.split("\r\n")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" not in line:
            continue
        name, value = line.split(":", 1)
        headers[name.strip().lower()] = value.strip()
    return lines[0], headers


def _accept_websocket(sock: socket.socket) -> None:
    request_line, headers = _read_http_headers(sock)
    if not request_line.startswith("GET "):
        raise ValueError("WebSocket handshake must use GET")
    key = headers.get("sec-websocket-key")
    if not key or headers.get("upgrade", "").lower() != "websocket":
        raise ValueError("invalid WebSocket upgrade request")
    digest = hashlib.sha1((key + WEBSOCKET_GUID).encode("ascii")).digest()
    accept = base64.b64encode(digest).decode("ascii")
    response = (
        f"HTTP/1.1 {HTTPStatus.SWITCHING_PROTOCOLS.value} Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
    )
    sock.sendall(response.encode("ascii"))


def _send_frame(sock: socket.socket, opcode: int, payload: bytes = b"") -> None:
    length = len(payload)
    if length < 126:
        header = struct.pack("!BB", 0x80 | opcode, length)
    elif length <= 0xFFFF:
        header = struct.pack("!BBH", 0x80 | opcode, 126, length)
    else:
        header = struct.pack("!BBQ", 0x80 | opcode, 127, length)
    sock.sendall(header + payload)


def _websocket_to_game(websocket: socket.socket, game: socket.socket) -> None:
    fragments = bytearray()
    fragmented_opcode: int | None = None
    while True:
        first, second = _read_exact(websocket, 2)
        final = bool(first & 0x80)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", _read_exact(websocket, 2))[0]
        elif length == 127:
            length = struct.unpack("!Q", _read_exact(websocket, 8))[0]
        if length > MAX_FRAME_SIZE:
            raise ValueError("WebSocket frame is too large")
        mask = _read_exact(websocket, 4) if masked else b""
        payload = bytearray(_read_exact(websocket, length))
        if masked:
            for index in range(length):
                payload[index] ^= mask[index % 4]

        if opcode == 0x8:
            _send_frame(websocket, 0x8, bytes(payload[:125]))
            return
        if opcode == 0x9:
            _send_frame(websocket, 0xA, bytes(payload[:125]))
            continue
        if opcode == 0xA:
            continue
        if opcode in (0x1, 0x2):
            fragments = bytearray(payload)
            fragmented_opcode = opcode
        elif opcode == 0x0 and fragmented_opcode is not None:
            fragments.extend(payload)
        else:
            continue
        if final:
            if fragmented_opcode == 0x2:
                game.sendall(fragments)
            fragments.clear()
            fragmented_opcode = None


def _game_to_websocket(game: socket.socket, websocket: socket.socket) -> None:
    try:
        while True:
            chunk = game.recv(64 * 1024)
            if not chunk:
                return
            _send_frame(websocket, 0x2, chunk)
    except (ConnectionError, OSError):
        # The downstream loop owns socket shutdown. A page reload can close the
        # shared game socket while this relay thread is blocked in recv().
        return


def _bridge(client: socket.socket, address: tuple[str, int]) -> None:
    game: socket.socket | None = None
    try:
        client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        client.settimeout(20)
        _accept_websocket(client)
        client.settimeout(None)
        game = socket.create_connection((GAME_HOST, GAME_PORT), timeout=10)
        game.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        game.settimeout(None)
        print(f"Ruffle socket connected: {address[0]}:{address[1]} -> {GAME_HOST}:{GAME_PORT}", flush=True)
        threading.Thread(
            target=_game_to_websocket,
            args=(game, client),
            name=f"ruffle-upstream-{address[1]}",
            daemon=True,
        ).start()
        _websocket_to_game(client, game)
    except (ConnectionError, OSError, ValueError) as exc:
        print(f"Ruffle socket closed: {address[0]}:{address[1]} ({exc})", flush=True)
    finally:
        if game is not None:
            try:
                game.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            game.close()
        try:
            client.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        client.close()


def serve_forever() -> None:
    """Serve Ruffle WebSocket connections until the parent process exits."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((PROXY_HOST, PROXY_PORT))
        server.listen()
        print(f"Ruffle socket proxy listening on {PROXY_HOST}:{PROXY_PORT}", flush=True)
        while True:
            client, address = server.accept()
            threading.Thread(target=_bridge, args=(client, address), daemon=True).start()


if __name__ == "__main__":
    serve_forever()
