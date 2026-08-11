#!/usr/bin/env python3
"""Build the per-category AMF3 item catalogs expected by the Flash client."""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path
from typing import Any

TOOLS_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = TOOLS_ROOT.parent
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from fake_flash_server import Amf3Reader, GAME_DATA_PATH  # noqa: E402


ITEM_SECTIONS = (
    "XiaoHao",
    "ZhuangBei",
    "JiNengShu",
    "JiNengCanJuan",
    "LiBao",
    "RenWuWuPin",
    "CaiLiao",
    "BaoShi",
    "TeShuWuPin",
    "FuZhuWuPin",
    "BuffWuPin",
)


def encode_u29(value: int) -> bytes:
    """Encode an unsigned AMF3 29-bit integer."""
    value &= 0x1FFFFFFF
    if value < 0x80:
        return bytes((value,))
    if value < 0x4000:
        return bytes((((value >> 7) & 0x7F) | 0x80, value & 0x7F))
    if value < 0x200000:
        return bytes(
            (
                ((value >> 14) & 0x7F) | 0x80,
                ((value >> 7) & 0x7F) | 0x80,
                value & 0x7F,
            )
        )
    return bytes(
        (
            ((value >> 22) & 0x7F) | 0x80,
            ((value >> 15) & 0x7F) | 0x80,
            ((value >> 8) & 0x7F) | 0x80,
            value & 0xFF,
        )
    )


def encode_string(value: str) -> bytes:
    """Encode an inline AMF3 string value, including its marker."""
    data = value.encode("utf-8")
    return b"\x06" + encode_u29((len(data) << 1) | 1) + data


def encode_object_key(value: str) -> bytes:
    """Encode an inline AMF3 object key without a value marker."""
    data = value.encode("utf-8")
    return encode_u29((len(data) << 1) | 1) + data


def encode_value(value: Any) -> bytes:
    """Encode the JSON-compatible values used by the item GameData sections."""
    if value is None:
        return b"\x01"
    if value is False:
        return b"\x02"
    if value is True:
        return b"\x03"
    if isinstance(value, int):
        if -(1 << 28) <= value < (1 << 28):
            return b"\x04" + encode_u29(value)
        import struct

        return b"\x05" + struct.pack(">d", float(value))
    if isinstance(value, float):
        import struct

        if math.isfinite(value) and value.is_integer() and -(1 << 28) <= value < (1 << 28):
            return b"\x04" + encode_u29(int(value))
        return b"\x05" + struct.pack(">d", value)
    if isinstance(value, str):
        return encode_string(value)
    if isinstance(value, bytes):
        return b"\x0c" + encode_u29((len(value) << 1) | 1) + value
    if isinstance(value, list):
        return (
            b"\x09"
            + encode_u29((len(value) << 1) | 1)
            + b"\x01"
            + b"".join(encode_value(item) for item in value)
        )
    if isinstance(value, dict):
        # Inline dynamic object with no sealed members and an empty class name.
        return (
            b"\x0a\x0b\x01"
            + b"".join(encode_object_key(str(key)) + encode_value(item) for key, item in value.items())
            + b"\x01"
        )
    raise TypeError(f"unsupported AMF3 value: {type(value).__name__}")


def load_game_data() -> dict[str, Any]:
    """Read the authoritative GameData root used by the offline server."""
    root = Amf3Reader(GAME_DATA_PATH.read_bytes()).read_value()
    if not isinstance(root, dict):
        raise ValueError("GameData.dat root is not an AMF3 object")
    return root


def build(output_dir: Path, sections: tuple[str, ...]) -> list[tuple[str, int, int]]:
    """Write one AMF3 object per item category and return summary counts."""
    root = load_game_data()
    output_dir.mkdir(parents=True, exist_ok=True)
    result = []
    for section in sections:
        value = root.get(section)
        if not isinstance(value, dict):
            continue
        payload = encode_value(value)
        path = output_dir / f"{section}.dat"
        path.write_bytes(payload)
        decoded = Amf3Reader(payload).read_value()
        if not isinstance(decoded, dict) or len(decoded) != len(value):
            raise ValueError(f"AMF3 round-trip failed for {section}")
        result.append((section, len(value), len(payload)))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "www" / "act_web_tiyan" / "dat",
    )
    args = parser.parse_args()
    for section, count, size in build(args.output, ITEM_SECTIONS):
        print(f"{section}: {count} items, {size} bytes")


if __name__ == "__main__":
    main()
