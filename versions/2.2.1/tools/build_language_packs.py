#!/usr/bin/env python3
"""Build the single Traditional GameData tree used by the formal client."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import zlib
from copy import deepcopy
from pathlib import Path
from typing import Any


TOOLS_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = TOOLS_ROOT.parent
WORKSPACE_ROOT = PROJECT_ROOT.parent
ACTIVE_ROOT = PROJECT_ROOT / "www" / "act_web_tiyan"
LANGUAGE_ROOT = PROJECT_ROOT / "www" / "act_web_tiyan_lang"
SHARED_MAINLIB_SOURCE = PROJECT_ROOT / "resources" / "formal-shared" / "MainLib.swf"
ANALYSIS_ROOT = WORKSPACE_ROOT / "analysis"
NEW_GAME_DATA = ANALYSIS_ROOT / "gamedata" / "GameData_decoded.dat"
BACKUP_ROOT = (
    PROJECT_ROOT
    / "backups"
    / "before-new-package-import-20260725-174811"
    / "www"
    / "act_web_tiyan"
)
OLD_GAME_DATA = BACKUP_ROOT / "dat" / "GameData.dat"
SYS_CFG_SOURCE = (
    PROJECT_ROOT
    / "logs"
    / "diagnostics"
    / "ui-v36-link-bounds"
    / "verify"
    / "scripts"
    / "mxw"
    / "common"
    / "gameCommon"
    / "config"
    / "SysCfg.as"
)
FFDEC_JAR = PROJECT_ROOT / "logs" / "diagnostics" / "ffdec_26.2.1" / "ffdec.jar"
DECRYPTED_ROOT = ANALYSIS_ROOT / "decrypted" / "game"
UNPACKED_ROOT = ANALYSIS_ROOT / "unpacked" / "game"

if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from fake_flash_server import Amf3Reader  # noqa: E402
from generate_item_dat import ITEM_SECTIONS, encode_value  # noqa: E402


LANGUAGE_ASSET_PATHS = (
    "swf/Effect/Effect_PaiHangBang.swf",
    "swf/LiBao/LiBao_11000102_info.swf",
    "swf/UIBmp/UIBmp_Tongshu_bakcground.swf",
    "swf/ZhuangBei/ZhuangBei_01210529_info.swf",
    "swf/ZhuangBei/ZhuangBei_01210539_info.swf",
)

# MainLib contains the executable UI/runtime and reads the page language at
# runtime. Keeping one copy avoids compiling and shipping a separate SWF per
# locale; locale-specific text belongs in GameData and small asset overrides.
SHARED_RUNTIME_ASSETS = ("asset/MainLib.swf",)
PACKAGE_UI_ROOT = DECRYPTED_ROOT / "swf" / "UISwf"

COMPLETE_SHARED_ASSET_PATHS = (
    "swf/Effect/Effect_PaiHangBang.swf",
)

LCMAP_SIMPLIFIED_CHINESE = 0x02000000
LCMAP_TRADITIONAL_CHINESE = 0x04000000
SWF_SIGNATURES = (b"FWS", b"CWS", b"ZWS")
JOB_TRANSFER_ITEM_ID = "16000182"
JOB_TRANSFER_TEMPLATE_ID = "16000181"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_amf(path: Path) -> dict[str, Any]:
    body = path.read_bytes()
    reader = Amf3Reader(body)
    value = reader.read_value()
    if not isinstance(value, dict) or reader.pos != len(body):
        raise ValueError(f"not a complete AMF3 object: {path}")
    return value


def install_job_transfer_token(value: dict[str, Any]) -> None:
    """Add the formal transfer service's visible token to the Nya item catalog."""
    catalog = value.get("FuZhuWuPin")
    strings_root = value.get("String")
    if not isinstance(catalog, dict) or not isinstance(strings_root, dict):
        raise ValueError("FuZhuWuPin catalog is missing")
    template = catalog.get(JOB_TRANSFER_TEMPLATE_ID)
    string_blob = strings_root.get("FuZhuWuPin")
    if not isinstance(template, bytes) or not isinstance(string_blob, bytes):
        raise ValueError("Transfer-token template data is missing")

    definition = Amf3Reader(zlib.decompress(template)).read_value()
    strings = Amf3Reader(zlib.decompress(string_blob)).read_value()
    if not isinstance(definition, dict) or not isinstance(strings, dict):
        raise ValueError("Transfer-token template data is invalid")
    catalog[JOB_TRANSFER_ITEM_ID] = zlib.compress(encode_value(deepcopy(definition)))
    strings[JOB_TRANSFER_ITEM_ID] = {
        "name": "转职凭证",
        "desc": "忍者转职时消耗的凭证。每次转职消耗一张。",
        "detaildesc": "用于在转职导师处更换忍术流派。",
    }
    strings_root["FuZhuWuPin"] = zlib.compress(encode_value(strings))


def windows_chinese_map(value: str, flags: int) -> str | None:
    """Use Windows' Chinese locale mapping when available."""
    if not value or os.name != "nt":
        return value
    function = ctypes.windll.kernel32.LCMapStringEx
    # Use a null-terminated source so Win32 counts UTF-16 code units rather
    # than Python code points; non-BMP characters otherwise truncate.
    length = function("zh-TW", flags, value, -1, None, 0, None, None, 0)
    if length <= 0:
        return None
    buffer = ctypes.create_unicode_buffer(length)
    written = function(
        "zh-TW", flags, value, -1, buffer, length, None, None, 0
    )
    if written <= 0:
        return None
    # ctypes exposes non-BMP output as UTF-16 surrogate code points on
    # Windows. Normalize them before the AMF3 UTF-8 encoder sees the string.
    return (
        buffer[: max(0, written - 1)]
        .encode("utf-16-le", errors="surrogatepass")
        .decode("utf-16-le")
    )


def load_character_maps(path: Path) -> tuple[dict[int, str], dict[int, str]]:
    source = path.read_text(encoding="utf-8")
    traditional_match = re.search(
        r'TRADITIONAL_MAP:String\s*=\s*"([^"]+)"', source
    )
    simplified_match = re.search(r'SIMPLIFIED_MAP:String\s*=\s*"([^"]+)"', source)
    if not traditional_match or not simplified_match:
        raise ValueError(f"Chinese character maps are missing from {path}")
    traditional = traditional_match.group(1)
    simplified = simplified_match.group(1)
    if len(traditional) != len(simplified):
        raise ValueError("Chinese character maps have different lengths")
    to_simplified = str.maketrans({old: new for old, new in zip(traditional, simplified)})
    reverse: dict[str, str] = {}
    for old, new in zip(traditional, simplified):
        reverse.setdefault(new, old)
    return to_simplified, str.maketrans(reverse)


class TextConverter:
    def __init__(self, sys_cfg_source: Path = SYS_CFG_SOURCE) -> None:
        self.fallback_simplified, self.fallback_traditional = load_character_maps(
            sys_cfg_source
        )

    def simplified(self, value: str) -> str:
        mapped = windows_chinese_map(value, LCMAP_SIMPLIFIED_CHINESE)
        # LCMapStringEx intentionally leaves some region-specific forms such
        # as 後 untouched. Always finish with the client-derived character map
        # so generated GameData and SWF text cannot remain mixed-script.
        return (mapped if mapped is not None else value).translate(
            self.fallback_simplified
        )

    def traditional(self, value: str) -> str:
        mapped = windows_chinese_map(value, LCMAP_TRADITIONAL_CHINESE)
        return (mapped if mapped is not None else value).translate(
            self.fallback_traditional
        )


def build_traditional_value(
    new_value: Any,
    old_value: Any,
    converter: TextConverter,
    stats: dict[str, int],
) -> Any:
    """Preserve new structure while reusing path-matched authored Traditional text."""
    if isinstance(new_value, bytes):
        try:
            new_reader = Amf3Reader(zlib.decompress(new_value))
            new_inner = new_reader.read_value()
        except (ValueError, zlib.error):
            return new_value
        old_inner = None
        if isinstance(old_value, bytes):
            try:
                old_reader = Amf3Reader(zlib.decompress(old_value))
                old_inner = old_reader.read_value()
            except (ValueError, zlib.error):
                old_inner = None
        localized = build_traditional_value(new_inner, old_inner, converter, stats)
        return zlib.compress(encode_value(localized), 9)
    if isinstance(new_value, dict):
        old_dict = old_value if isinstance(old_value, dict) else {}
        return {
            key: build_traditional_value(
                item, old_dict.get(key), converter, stats
            )
            for key, item in new_value.items()
        }
    if isinstance(new_value, list):
        old_list = old_value if isinstance(old_value, list) else []
        return [
            build_traditional_value(
                item,
                old_list[index] if index < len(old_list) else None,
                converter,
                stats,
            )
            for index, item in enumerate(new_value)
        ]
    if not isinstance(new_value, str):
        return deepcopy(new_value)

    stats["strings"] += 1
    if isinstance(old_value, str):
        if old_value == new_value:
            stats["unchanged_strings"] += 1
            return new_value
        if converter.simplified(old_value) == new_value:
            stats["authored_traditional_strings"] += 1
            return old_value
    # Newer content has no authored Traditional counterpart. Keep its source
    # text unchanged instead of fabricating a machine-converted translation.
    stats["untranslated_strings"] += 1
    return new_value


def build_simplified_value(value: Any, converter: TextConverter) -> Any:
    """Normalize every authored string in the Simplified client data tree.

    New package content does not always have a legacy Traditional counterpart,
    so path-matching alone leaves those strings in mixed script. Non-text data
    and compressed AMF blobs retain their exact structure.
    """
    if isinstance(value, bytes):
        try:
            decompressed = zlib.decompress(value)
            reader = Amf3Reader(decompressed)
            inner = reader.read_value()
            if reader.pos != len(decompressed):
                return value
        except (ValueError, zlib.error):
            return value
        return zlib.compress(encode_value(build_simplified_value(inner, converter)), 9)
    if isinstance(value, dict):
        return {key: build_simplified_value(item, converter) for key, item in value.items()}
    if isinstance(value, list):
        return [build_simplified_value(item, converter) for item in value]
    if isinstance(value, str):
        return converter.simplified(value)
    return deepcopy(value)


def normalize_numeric_config_order(value: dict[str, Any]) -> None:
    """Keep numeric UI configuration tables in their authored numeric order."""
    etc = value.get("Etc")
    if not isinstance(etc, dict):
        return
    raw_ranking = etc.get("PaiHangBang")
    if not isinstance(raw_ranking, bytes):
        return
    try:
        decompressed = zlib.decompress(raw_ranking)
        reader = Amf3Reader(decompressed)
        ranking = reader.read_value()
    except (ValueError, zlib.error):
        return
    if not isinstance(ranking, dict) or reader.pos != len(decompressed):
        return
    ordered = dict(
        sorted(
            ranking.items(),
            key=lambda entry: int(entry[0]) if str(entry[0]).isdigit() else sys.maxsize,
        )
    )
    etc["PaiHangBang"] = zlib.compress(encode_value(ordered), 9)


def write_amf(path: Path, value: dict[str, Any]) -> None:
    payload = encode_value(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    if read_amf(path) != value:
        raise ValueError(f"AMF3 round-trip mismatch: {path}")


def write_catalogs(root: Path, value: dict[str, Any]) -> list[dict[str, Any]]:
    records = []
    for section in ITEM_SECTIONS:
        catalog = value.get(section)
        if not isinstance(catalog, dict):
            continue
        path = root / "dat" / f"{section}.dat"
        write_amf(path, catalog)
        records.append(
            {
                "kind": "catalog",
                "locale": root.name,
                "path": path.as_posix(),
                "entries": len(catalog),
                "size": path.stat().st_size,
                "sha256": sha256(path),
            }
        )
    return records


def package_source(relative: str) -> Path:
    decrypted = DECRYPTED_ROOT / relative
    if decrypted.is_file():
        return decrypted
    unpacked = UNPACKED_ROOT / relative
    if unpacked.is_file():
        return unpacked
    active = ACTIVE_ROOT / relative
    if active.is_file():
        return active
    raise FileNotFoundError(relative)


def copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def convert_air_swf(source: Path, destination: Path) -> None:
    """Convert the lowercase cWS desktop envelope to a browser-readable SWF."""
    body = source.read_bytes()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if body.startswith(SWF_SIGNATURES):
        shutil.copy2(source, destination)
        return
    if not body.startswith(b"cWS"):
        raise ValueError(f"unsupported SWF signature {body[:3]!r}: {source}")
    if not FFDEC_JAR.is_file():
        raise FileNotFoundError(FFDEC_JAR)
    environment = os.environ.copy()
    environment["APPDATA"] = str(PROJECT_ROOT / "logs" / "diagnostics" / "ffdec-appdata")
    Path(environment["APPDATA"]).mkdir(parents=True, exist_ok=True)
    command = [
        "java",
        "-Djava.awt.headless=true",
        "-jar",
        str(FFDEC_JAR),
        "-decompress",
        str(source),
        str(destination),
    ]
    completed = subprocess.run(
        command, env=environment, capture_output=True, text=True, timeout=120
    )
    if completed.returncode != 0 or not destination.is_file():
        raise RuntimeError(
            f"FFDec failed for {source}: {completed.stdout}\n{completed.stderr}"
        )
    if not destination.read_bytes().startswith(SWF_SIGNATURES):
        raise ValueError(f"FFDec did not produce a browser SWF: {destination}")


def build_assets() -> list[dict[str, Any]]:
    records = []
    for relative in LANGUAGE_ASSET_PATHS:
        old_source = BACKUP_ROOT / relative
        if not old_source.is_file():
            raise FileNotFoundError(old_source)
        simple_source = package_source(relative)
        simple_destination = LANGUAGE_ROOT / "zh-Hans" / relative
        traditional_destination = LANGUAGE_ROOT / "zh-Hant" / relative
        if simple_source.read_bytes().startswith(b"cWS"):
            convert_air_swf(simple_source, simple_destination)
        else:
            copy_file(simple_source, simple_destination)
        if relative in COMPLETE_SHARED_ASSET_PATHS:
            copy_file(simple_destination, traditional_destination)
        else:
            copy_file(old_source, traditional_destination)
        for locale, destination in (
            ("zh-Hans", simple_destination),
            ("zh-Hant", traditional_destination),
        ):
            if destination.suffix.lower() == ".swf" and not destination.read_bytes().startswith(
                SWF_SIGNATURES
            ):
                raise ValueError(f"invalid browser SWF: {destination}")
            records.append(
                {
                    "kind": "asset",
                    "locale": locale,
                    "path": relative,
                    "size": destination.stat().st_size,
                    "sha256": sha256(destination),
                }
            )
    return records


def consolidate_shared_runtime() -> dict[str, Any]:
    """Make the language packs use one runtime SWF and report its checksum."""
    source = SHARED_MAINLIB_SOURCE
    if not source.is_file():
        raise FileNotFoundError(source)
    shared_destination = ACTIVE_ROOT / "asset" / "MainLib.swf"
    copy_file(source, shared_destination)
    removed: list[str] = []
    for locale in ("zh-Hans", "zh-Hant"):
        duplicate = LANGUAGE_ROOT / locale / "asset" / "MainLib.swf"
        if duplicate.is_file():
            duplicate.unlink()
            removed.append(duplicate.as_posix())
    return {
        "path": shared_destination.as_posix(),
        "sha256": sha256(shared_destination),
        "size": shared_destination.stat().st_size,
        "removed_language_duplicates": removed,
    }


def import_missing_package_ui_assets() -> dict[str, Any]:
    """Import only absent extracted UI layouts into the formal shared tree."""
    if not PACKAGE_UI_ROOT.is_dir():
        raise FileNotFoundError(PACKAGE_UI_ROOT)
    installed: list[dict[str, Any]] = []
    imported_this_run = 0
    active_ui_root = ACTIVE_ROOT / "swf" / "UISwf"
    for source in sorted(PACKAGE_UI_ROOT.rglob("*")):
        if not source.is_file():
            continue
        destination = active_ui_root / source.relative_to(PACKAGE_UI_ROOT)
        if not destination.exists():
            copy_file(source, destination)
            imported_this_run += 1
        if sha256(destination) != sha256(source):
            continue
        installed.append(
            {
                "path": destination.relative_to(ACTIVE_ROOT).as_posix(),
                "size": destination.stat().st_size,
                "sha256": sha256(destination),
                "source": source.as_posix(),
            }
        )
    return {
        "count": len(installed),
        "imported_this_run": imported_this_run,
        "files": installed,
    }


def build(output_root: Path = ACTIVE_ROOT) -> dict[str, Any]:
    for required in (NEW_GAME_DATA, OLD_GAME_DATA, SYS_CFG_SOURCE, BACKUP_ROOT):
        if not required.exists():
            raise FileNotFoundError(required)

    source_game_data = read_amf(NEW_GAME_DATA)
    converter = TextConverter()
    simplified = build_simplified_value(source_game_data, converter)
    authored_traditional = read_amf(OLD_GAME_DATA)
    stats = {
        "mode": "single-traditional-gamedata",
        "strings": 0,
        "unchanged_strings": 0,
        "authored_traditional_strings": 0,
        "generated_traditional_strings": 0,
        "untranslated_strings": 0,
    }
    traditional = build_traditional_value(
        simplified,
        authored_traditional,
        TextConverter(),
        stats,
    )

    write_amf(output_root / "dat" / "GameData.dat", traditional)
    records = write_catalogs(output_root, traditional)

    manifest = {
        "mode": "single-traditional-gamedata",
        "default_locale": "zh-Hant",
        "sections": len(traditional),
        "stats": stats,
        "records": records,
        "traditional_game_data_sha256": sha256(
            output_root / "dat" / "GameData.dat"
        ),
    }
    manifest_path = output_root / "traditional-gamedata-manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def build_simplified(
    output_root: Path,
    source_path: Path = ACTIVE_ROOT / "dat" / "GameData.dat",
) -> dict[str, Any]:
    """Build a Simplified-only copy while preserving the source data contract."""
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    source = read_amf(source_path)
    simplified = build_simplified_value(source, TextConverter())
    install_job_transfer_token(simplified)
    normalize_numeric_config_order(simplified)
    write_amf(output_root / "dat" / "GameData.dat", simplified)
    records = write_catalogs(output_root, simplified)
    manifest = {
        "mode": "single-simplified-gamedata",
        "default_locale": "zh-Hans",
        "source": source_path.as_posix(),
        "source_sha256": sha256(source_path),
        "sections": len(simplified),
        "records": records,
        "simplified_game_data_sha256": sha256(
            output_root / "dat" / "GameData.dat"
        ),
    }
    manifest_path = output_root / "simplified-gamedata-manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ACTIVE_ROOT)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--simplified-only", action="store_true")
    args = parser.parse_args()
    if args.simplified_only:
        manifest = build_simplified(
            args.output,
            args.source or ACTIVE_ROOT / "dat" / "GameData.dat",
        )
        print(f"Simplified GameData tree: {args.output}")
    else:
        manifest = build(args.output)
        print(f"Traditional GameData tree: {args.output}")
    print(f"Sections: {manifest['sections']}")
    if "stats" in manifest:
        print(json.dumps(manifest["stats"], ensure_ascii=False))
    print(f"Records: {len(manifest['records'])}")


if __name__ == "__main__":
    main()
