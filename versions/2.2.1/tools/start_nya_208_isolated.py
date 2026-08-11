#!/usr/bin/env python3
"""Start Nya MainLib 2.0.8 with the current local game server."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import base64
import copy
import hashlib
import json
import math
import os
import struct
import sys
import time
import zlib
from pathlib import Path
from types import ModuleType


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PREVIEW_ROOT_NAME = os.environ.get(
    "NARUTO_PREVIEW_ROOT", "act_web_nya_208_isolated"
)
HTTP_PORT = int(os.environ.get("NARUTO_HTTP_PORT", "19280"))
PROXY_PORT = int(os.environ.get("NARUTO_PROXY_PORT", "19281"))
GAME_PORT = int(os.environ.get("NARUTO_PROXY_GAME_PORT", "19284"))
CHANNEL_PORT = int(os.environ.get("NARUTO_CHANNEL_PORT", str(GAME_PORT + 1)))
NATIVE_LOGIN_PORT = int(
    os.environ.get("NARUTO_NATIVE_LOGIN_PORT", str(GAME_PORT + 2))
)
DEFAULT_PUBLIC_HOST = "27.184.1.224"


def env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def resolve_network_hosts() -> tuple[bool, str, str]:
    """Resolve bind/advertise hosts while keeping public mode backward compatible."""
    local_only = env_flag("NARUTO_LOCAL_ONLY")
    if local_only:
        return True, "127.0.0.1", "127.0.0.1"
    bind_host = os.environ.get("NARUTO_BIND_HOST", "0.0.0.0").strip() or "0.0.0.0"
    public_host = os.environ.get("NARUTO_PUBLIC_HOST", DEFAULT_PUBLIC_HOST).strip()
    if public_host in {"", "127.0.0.1", "localhost", "0.0.0.0"}:
        public_host = DEFAULT_PUBLIC_HOST
    return False, bind_host, public_host


LOCAL_ONLY, BIND_HOST, PUBLIC_HOST = resolve_network_hosts()
SAVE_NAME = os.environ.get(
    "NARUTO_PREVIEW_SAVE", "nya-mainlib-208-isolated.json"
)
SERVER_SOURCE = Path(__file__).with_name("fake_flash_server.py")
CASH_CATALOG_SOURCE = Path(__file__).with_name("nya_208_cash_catalog.json")
NYA211_AUTHORITY_MANIFEST = (
    PROJECT_ROOT / "www" / "act_web_nya_208_isolated" / "nya211-gamedata-authority.json"
)

NYA208_CASH_ROW_STRUCT = struct.Struct("<iii bb ii b ii bbb ii b")
# These Nya 2.1.1 GameData items postdate the captured 2.0.8 cash catalog. Keep
# them as explicit server-side additions so category/search/detail/purchase use
# one native CashItemInfo contract without rewriting the evidence capture.
NYA208_CASH_CATALOG_ADDITIONS = {
    3: (
        (4019, 3888, 1888, 16000184),
        (4020, 688, 488, 16000185),
        (4021, 1888, 1399, 16000186),
        (4022, 3888, 2888, 16000187),
        (4023, 888, 688, 16000188),
        (4024, 888, 688, 16000189),
    ),
}

NYA208_CONFIG_DEFAULTS = {
    "autoSellPickedEquip": False,
    "autoUpgradeSkill": False,
    "unlimitedAutoBattleTime": False,
    "keySchemes": {},
    "activeKeyScheme": 1,
    "hidePetTransformAppearance": False,
    "splitEquipExtraTooltip": True,
    "equipExtraTooltipColumns": 4,
    "bgmVolume": 1.0,
    "sfxVolume": 1.0,
    "mapData": {},
}

NYA208_PORTAL_STOP_MIN_OFFSET = 0
NYA208_PORTAL_STOP_MAX_OFFSET = 70
NYA208_WORLD_BOOTSTRAP_DELAY_SECONDS = 1.0
NYA208_WORLD_READY_TIMEOUT_SECONDS = 3.0


def verify_nya211_runtime_game_data(path: Path) -> None:
    """Refuse to boot when the audited Nya 2.1.1 runtime was replaced or drifted."""
    manifest = json.loads(NYA211_AUTHORITY_MANIFEST.read_text(encoding="utf-8"))
    expected = str(manifest.get("runtimeGameDataSha256") or "").lower()
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if not expected or actual != expected:
        raise RuntimeError(
            "Nya 2.1.1 GameData authority check failed: "
            f"expected {expected or '<missing>'}, got {actual}"
        )
NYA208_SERVER_MESSAGE_OPCODE = 65
NYA208_MESSAGE_BOX_TYPE = 16

NYA208_LING_QI_FACES = (
    (10000038, 6, "须佐之男·炎"),
    (10000067, 6, "须佐之男·十拳剑"),
    (10000074, 7, "须佐之男·完全体"),
    (10000075, 8, "须佐之男·双刃"),
)

# Exact color transforms authored in Nya 2.0.8 UIConfig.lingQiColorData.
NYA208_LING_QI_COLORS = (
    (0, 1, 1, 1, 0.7, 0, 0, 0, 0),
    (1, 1.18, 0.82, 0.82, 0.7, 42, 0, 0, 0),
    (2, 1.22, 0.74, 0.68, 0.7, 58, 8, 4, 0),
    (3, 1.2, 0.92, 0.72, 0.7, 54, 24, 0, 0),
    (4, 1.18, 1.02, 0.66, 0.7, 52, 30, 0, 0),
    (5, 1.14, 1.1, 0.72, 0.7, 46, 34, 4, 0),
    (6, 1.08, 1.18, 0.76, 0.7, 26, 46, 8, 0),
    (7, 1.16, 1.16, 1.16, 0.7, 26, 26, 26, 0),
    (8, 0.7, 0.7, 0.7, 0.7, 0, 0, 0, 0),
    (9, 0.82, 0.96, 1.18, 0.7, 0, 18, 60, 0),
    (10, 0.76, 1.08, 1.08, 0.7, 0, 34, 34, 0),
    (11, 0.86, 1.14, 1, 0.7, 0, 42, 18, 0),
    (12, 0.68, 0.9, 1.22, 0.7, 0, 28, 60, 0),
    (13, 0.78, 0.82, 1.18, 0.7, 12, 20, 58, 0),
    (14, 1.06, 0.84, 1.18, 0.7, 28, 0, 52, 0),
    (15, 1.1, 0.78, 1.14, 0.7, 36, 0, 60, 0),
    (16, 1.18, 0.8, 1.02, 0.7, 50, 0, 28, 0),
    (17, 1.2, 0.88, 1.1, 0.7, 54, 24, 42, 0),
    (18, 1.16, 0.72, 0.9, 0.7, 52, 8, 32, 0),
    (19, 1.18, 0.86, 0.82, 0.7, 48, 18, 12, 0),
    (20, 0.86, 1.14, 0.86, 0.7, 16, 48, 16, 0),
    (21, 0.8, 1.2, 0.7, 0.7, 18, 56, 0, 0),
    (22, 0.72, 1.12, 0.78, 0.7, 0, 42, 16, 0),
    (23, 0.68, 1.18, 0.84, 0.7, 0, 54, 26, 0),
    (24, 0.66, 1.1, 1.06, 0.7, 0, 44, 36, 0),
    (25, 0.68, 1.02, 1.18, 0.7, 0, 34, 56, 0),
    (26, 0.72, 0.98, 1.16, 0.7, 0, 26, 50, 0),
    (27, 0.84, 0.9, 1.12, 0.7, 10, 18, 46, 0),
    (28, 1.08, 1.08, 1.08, 0.7, 20, 20, 24, 0),
    (29, 0.84, 0.84, 0.84, 0.7, 12, 12, 12, 0),
    (30, 1.12, 1.06, 0.88, 0.7, 38, 24, 8, 0),
    (31, 1.08, 0.92, 0.74, 0.7, 42, 18, 0, 0),
    (32, 1, 0.88, 0.68, 0.7, 36, 14, 0, 0),
)


def load_nya208_cash_catalog() -> dict[int, tuple[dict[str, int | bytes], ...]]:
    """Decode the captured fixed-width CashItemInfo rows once at startup."""
    captured = json.loads(CASH_CATALOG_SOURCE.read_text(encoding="utf-8"))
    catalog_rows: dict[int, list[dict[str, int | bytes]]] = {}
    for raw_category_id, encoded in captured.items():
        category_id = int(raw_category_id)
        payload = base64.b64decode(encoded)
        total, count = struct.unpack_from("<ii", payload)
        if total != count or len(payload) != 8 + count * NYA208_CASH_ROW_STRUCT.size:
            raise ValueError(f"Invalid Nya cash category capture {category_id}")
        rows: list[dict[str, int | bytes]] = []
        for index in range(count):
            start = 8 + index * NYA208_CASH_ROW_STRUCT.size
            raw = payload[start : start + NYA208_CASH_ROW_STRUCT.size]
            values = NYA208_CASH_ROW_STRUCT.unpack(raw)
            if values[15] != 0:
                raise ValueError("Captured Nya cash packages require a variable codec")
            rows.append(
                {
                    "sn": values[0],
                    "orig_price": values[1],
                    "now_price": values[2],
                    "is_lijin": values[12],
                    "item_id": values[13],
                    "raw": raw,
                }
            )
        catalog_rows[category_id] = rows
    known_sns = {
        int(row["sn"])
        for rows in catalog_rows.values()
        for row in rows
    }
    for category_id, additions in NYA208_CASH_CATALOG_ADDITIONS.items():
        rows = catalog_rows.setdefault(category_id, [])
        for sn, original_price, current_price, item_id in additions:
            if sn in known_sns:
                raise ValueError(f"Duplicate Nya cash product sn {sn}")
            raw = NYA208_CASH_ROW_STRUCT.pack(
                sn,
                original_price,
                current_price,
                1,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                item_id,
                0,
                0,
            )
            rows.append(
                {
                    "sn": sn,
                    "orig_price": original_price,
                    "now_price": current_price,
                    "is_lijin": 0,
                    "item_id": item_id,
                    "raw": raw,
                }
            )
            known_sns.add(sn)
    return {
        category_id: tuple(rows)
        for category_id, rows in catalog_rows.items()
    }


def load_current_game_server(module_name: str = "fake_flash_server") -> ModuleType:
    """Load the current server under the name expected by the preview launcher."""
    if not SERVER_SOURCE.is_file():
        raise SystemExit(f"Current game server is missing: {SERVER_SOURCE}")
    loader = importlib.machinery.SourceFileLoader(module_name, str(SERVER_SOURCE))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None:
        raise SystemExit("Unable to create the Nya isolated server module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


def patch_nya208_protocol(module: ModuleType) -> None:
    """Extend the current server's binary packets to the Nya 2.0.8 contract."""
    class Nya208CashRequest(str):
        """Tag a native Nya cash request without changing its formal text value."""

    session_class = module.FakeFlashSession
    if getattr(session_class, "_nya208_protocol_patched", False):
        return
    initialize_session = session_class.__init__
    decode_original_request = session_class._original_request_text
    build_warp_info = session_class._original_warp_char_info_payload
    build_item_payload = session_class._original_item_payload
    build_full_info = session_class._original_character_full_info_payload
    build_simple_info = getattr(
        session_class, "_original_character_simple_info_payload", None
    )
    build_spawn_player = session_class._original_spawn_player_payload
    build_shadow_clone_spawn = session_class._original_shadow_clone_spawn_payload
    build_update_stats = session_class._original_update_stats_payload
    send_original_packet = session_class.send_original_packet
    send_original_text = session_class._send_original_text
    handle_player_attack_announce = session_class.handle_player_attack_announce
    handle_attack = session_class.handle_attack
    handle_move = session_class.handle_move
    handle_monster_move = getattr(session_class, "handle_monster_move", None)
    handle_chat_message = getattr(session_class, "handle_chat_message", None)
    handle_use_item = getattr(session_class, "handle_use_item", None)
    handle_change_map = session_class._handle_change_map
    handle_char_config = session_class.handle_char_config
    handle_biguan = getattr(session_class, "handle_biguan", None)
    handle_ling_qi = session_class.handle_ling_qi
    handle_cash_category = getattr(session_class, "handle_cash_category", None)
    handle_cash_search = getattr(session_class, "handle_cash_search", None)
    handle_cash_item_info = getattr(session_class, "handle_cash_item_info", None)
    handle_cash_shopping = getattr(session_class, "handle_cash_shopping", None)
    enter_original_game = session_class.enter_original_game
    runtime_tick = session_class._runtime_tick
    handle_character_list = getattr(session_class, "_handle_character_list", None)
    handle_create_character = getattr(session_class, "_handle_create_character", None)
    handle_original_login_password = getattr(
        session_class, "_handle_original_login_password", None
    )

    state_class = getattr(module, "SinglePlayerState", None)
    if state_class is not None and not getattr(state_class, "_nya208_config_patched", False):
        load_state = state_class.from_dict

        def load_nya208_state(cls: type, payload: dict[str, object]) -> object:
            state = load_state(payload)
            raw_config = payload.get("character_config")
            if isinstance(raw_config, dict):
                for name in ("keySchemes", "mapData"):
                    value = raw_config.get(name)
                    if isinstance(value, dict):
                        state.character_config[name] = copy.deepcopy(value)
            return state

        state_class.from_dict = classmethod(load_nya208_state)
        state_class._nya208_config_patched = True

    def build_nya208_warp_info(session: object) -> bytes:
        payload = build_warp_info(session)
        stats_end = 8 + len(session._original_character_stats_payload())
        payload = (
            payload[:stats_end]
            + struct.pack(
                "<4i",
                session.character.ling_color,
                session._current_damage_reflect(),
                session._current_damage_reduce(),
                session._current_ignore_defence(),
            )
            + payload[stats_end:]
        )
        skills = [
            skill
            for skill in session.store.state.skills.values()
            if skill.skill_id != 0
        ]
        skill_count = min(1 + len(skills), 0xFFFF)
        trailing_size = 2 + 12 * skill_count + 9
        if len(payload) < trailing_size:
            raise ValueError("CharacterWarpInfo payload is unexpectedly short")
        renju_items = session._original_special_equipment_lists()["RenJu"]
        renju_payload = session._original_item_list_payload(renju_items)
        return payload[:-trailing_size] + renju_payload + payload[-trailing_size:]

    session_class._original_warp_char_info_payload = build_nya208_warp_info

    def build_nya208_item_payload(session: object, item: object) -> bytes:
        payload = build_item_payload(session, item)
        item_family = int(item.item_id) // 1_000_000
        if (
            not module.GAME_DATA_CATALOG.is_equipment_item(item.item_id)
            or item_family in {17, 19, 20}
        ):
            return payload
        if len(payload) < 8:
            raise ValueError("Equipment payload is unexpectedly short")
        # The shared server ends with reforgeTime and its formal-only linghua
        # value. Nya instead expects reforgeTime, six myth fields, then a list.
        myth_attributes = [
            values
            for values in getattr(item, "myth_attributes", ())
            if len(values) >= 2
        ]
        myth_payload = struct.pack(
            "<7i",
            int(getattr(item, "myth_forge_type", 0)),
            int(getattr(item, "myth_forge_count", 0)),
            int(getattr(item, "myth_wuxing", 0)),
            int(getattr(item, "myth_skill_id", 0)),
            int(getattr(item, "myth_skill_level", 0)),
            int(getattr(item, "myth_wuxing_level", 0)),
            len(myth_attributes),
        )
        myth_payload += b"".join(
            struct.pack("<2i", int(values[0]), int(values[1]))
            for values in myth_attributes
        )
        return payload[:-4] + myth_payload

    def build_nya208_full_info(session: object) -> bytes:
        payload = build_full_info(session)
        progression = session.store.state.progression
        role_catalog = tuple(
            getattr(
                module.GAME_DATA_CATALOG,
                "permanent_transform_card_ids",
                (),
            )
        )
        pet_catalog = tuple(
            getattr(
                module.GAME_DATA_CATALOG,
                "permanent_pet_transform_card_ids",
                (),
            )
        )
        collected_card_ids = {
            int(item_id)
            for item_id in session.store.state.transform_card_collection
        }
        role_collected = tuple(
            sorted(collected_card_ids & set(role_catalog))
        )
        pet_collected = tuple(
            sorted(collected_card_ids & set(pet_catalog))
        )
        catalog_groups = (
            role_catalog,
            pet_catalog,
            role_collected,
            pet_collected,
        )
        catalog_tail = bytearray()
        for values in catalog_groups:
            catalog_tail.extend(struct.pack("<i", len(values)))
            for value in values:
                catalog_tail.extend(struct.pack("<i", int(value)))
        if not payload.endswith(catalog_tail):
            raise ValueError("CharacterFullInfo catalog boundary was not found")
        optional_card_int_count = sum(len(values) for values in catalog_groups)
        trailing_size = (
            72
            + 20 * len(progression.spirit_items)
            + 4 * optional_card_int_count
        )
        insert_at = len(payload) - trailing_size
        if insert_at <= 0 or payload[insert_at - 1] != 0:
            raise ValueError("CharacterFullInfo lingqi boundary was not found")
        renju_items = session._original_special_equipment_lists()["RenJu"]
        payload = (
            payload[:insert_at]
            + session._original_item_list_payload(renju_items)
            + payload[insert_at:]
        )
        # Nya's other-character panel needs the persisted Sharingan stage and
        # progress between its spirit list and the Rouquan/Lunhui fields.
        # CharacterFullInfo serializes both permanent catalogs and the lifetime
        # collection entries. All four arrays belong after the Sharingan fields.
        full_info_tail = 54 + 4 * optional_card_int_count
        sharingan_insert_at = len(payload) - full_info_tail
        if sharingan_insert_at <= insert_at:
            raise ValueError("CharacterFullInfo Sharingan boundary was not found")
        stages = getattr(progression, "stages", {})
        stage_progress = getattr(progression, "stage_progress", {})
        sharingan_stage = max(1, stages.get("写轮眼", 1))
        sharingan_progress = max(0, stage_progress.get("写轮眼", 0))
        return (
            payload[:sharingan_insert_at]
            + struct.pack("<2i", sharingan_stage, sharingan_progress)
            + payload[sharingan_insert_at:]
        )

    def build_nya208_simple_info(session: object) -> bytes:
        """Append the RenJu ItemList introduced by Nya CharacterSimpleInfo."""
        if build_simple_info is None:
            raise AttributeError("Current server does not provide CharacterSimpleInfo")
        renju_items = session._original_special_equipment_lists()["RenJu"]
        return build_simple_info(session) + session._original_item_list_payload(renju_items)

    def add_nya208_spawn_ling_color(session: object, payload: bytes) -> bytes:
        """Set CharacterLook.lingQiColor without changing packet length.

        The current shared serializer already emits the Nya CharacterLook
        color field immediately after lingFace.  The previous adapter treated
        that field as missing and inserted a second int, shifting itemEffect,
        coordinates, and charType.  Nya then rejected the actor packet (and
        could close the channel while decoding it).  Keep the native packet
        layout stable and replace the existing value in place.
        """
        insert_at = (
            4
            + len(session._original_utf(session.character.name))
            + 2
            + struct.calcsize("<BBiBi")
            + len(session._original_equipped_look_payload())
            + 2
            + 4
        )
        field_end = insert_at + 4
        if field_end > len(payload):
            raise ValueError("SpawnPlayer CharacterLook boundary was not found")
        return (
            payload[:insert_at]
            + struct.pack("<i", session.character.ling_color)
            + payload[field_end:]
        )

    def build_nya208_spawn_player(session: object) -> bytes:
        return add_nya208_spawn_ling_color(session, build_spawn_player(session))

    def build_nya208_shadow_clone_spawn(session: object, clone: object) -> bytes:
        # Shadow clones use the same SpawnPlayerInfo decoder as real players.
        # Replace the already-present Nya lingQiColor field in place so their
        # foothold, coordinates, and charType remain at the native offsets.
        return add_nya208_spawn_ling_color(
            session,
            build_shadow_clone_spawn(session, clone),
        )

    def send_nya208_original_packet(
        session: object, opcode: object, payload: bytes
    ) -> None:
        opcode_value = module.op(opcode)
        lianzhan_opcode = getattr(module.ServerOpcode, "LIANZHAN", None)
        if lianzhan_opcode is not None and opcode_value == module.op(lianzhan_opcode):
            # PLAYER_LOGGEDIN arrives before Nya has installed its world/UI
            # listeners. Preserve the latest count and stage bootstrap packets
            # until the first post-login client request proves they are ready.
            # Runtime LIANZHAN packets must pass through; the client contains
            # onLianzhanCount/onLianzhanStage handlers and their SWF resources.
            if getattr(session, "_nya208_waiting_for_world_ready", False):
                action = payload[0] if payload else 0
                session._nya208_pending_lianzhan_packets[action] = (
                    opcode,
                    bytes(payload),
                )
                return
        if (
            opcode_value == module.op(module.ServerOpcode.EQUIPPED_INFO)
            and getattr(session, "_nya208_defer_equipped_bootstrap", False)
        ):
            session._nya208_pending_equipped_bootstrap = bytes(payload)
            # Nya creates DataCenter.stats while decoding WARP_CHAR_INFO, but its
            # login state machine still expects EQUIPPED_INFO first. An empty
            # list advances that state without constructing EqpData too early.
            send_original_packet(session, opcode, b"\x00")
            return
        if (
            opcode_value == module.op(module.ServerOpcode.WARP_CHAR_INFO)
            and getattr(session, "_nya208_pending_equipped_bootstrap", None)
            is not None
        ):
            # The first warp creates DataCenter.stats. Install the real equipment
            # next, then repeat the warp so Nya runs addedStats.setData(items).
            send_original_packet(session, opcode, payload)
            flush_nya208_equipped_bootstrap(session)
            send_original_packet(session, opcode, payload)
            return
        if opcode_value == module.op(module.ServerOpcode.UPDATE_CHAR_LOOK):
            if len(payload) < 6:
                raise ValueError("UPDATE_CHAR_LOOK payload is unexpectedly short")
            payload = (
                payload[:-6]
                + struct.pack("<i", session.character.ling_color)
                + payload[-6:]
            )
        elif opcode_value == module.op(module.ServerOpcode.DAMAGE_MONSTER):
            if len(payload) != 8:
                raise ValueError("DAMAGE_MONSTER payload is unexpectedly sized")
            # Both clients decode oid, hit type, damage. The formal text bridge
            # omits the middle byte; add Nya's required neutral hit type here.
            payload = payload[:4] + b"\x00" + payload[4:]
        send_original_packet(session, opcode, payload)

    session_class._original_item_payload = build_nya208_item_payload
    session_class._original_character_full_info_payload = build_nya208_full_info
    if build_simple_info is not None:
        session_class._original_character_simple_info_payload = build_nya208_simple_info
    session_class._original_spawn_player_payload = build_nya208_spawn_player
    session_class._original_shadow_clone_spawn_payload = build_nya208_shadow_clone_spawn
    session_class.send_original_packet = send_nya208_original_packet

    def flush_nya208_lianzhan_bootstrap(session: object) -> None:
        if getattr(session, "_nya208_waiting_for_world_ready", False):
            return
        pending = getattr(session, "_nya208_pending_lianzhan_packets", {})
        if not pending:
            return
        session._nya208_pending_lianzhan_packets = {}
        for action in sorted(pending):
            opcode, payload = pending[action]
            send_original_packet(session, opcode, payload)

    def flush_nya208_equipped_bootstrap(session: object) -> None:
        pending_payload = getattr(session, "_nya208_pending_equipped_bootstrap", None)
        if pending_payload is not None:
            session._nya208_pending_equipped_bootstrap = None
            session._nya208_defer_equipped_bootstrap = False
            send_original_packet(
                session,
                module.ServerOpcode.EQUIPPED_INFO,
                pending_payload,
            )

    def tick_nya208_runtime(session: object) -> None:
        if getattr(session, "_nya208_waiting_for_world_ready", False):
            ready_deadline = float(
                getattr(session, "_nya208_world_ready_deadline", 0.0)
            )
            if ready_deadline <= 0 or time.monotonic() < ready_deadline:
                flush_nya208_equipped_bootstrap(session)
                return
            # A dropped/omitted first client request must not permanently leave
            # this map without an elected monster AI controller.
            session._nya208_waiting_for_world_ready = False
            session._nya208_world_ready_deadline = 0.0
        runtime_tick(session, allow_original=True)
        flush_nya208_equipped_bootstrap(session)
        flush_nya208_lianzhan_bootstrap(session)

    def enter_nya208_original_game(session: object) -> None:
        if getattr(session, "entered_game", False):
            enter_original_game(session)
            return
        session._nya208_defer_equipped_bootstrap = True
        session._nya208_pending_equipped_bootstrap = None
        session._nya208_waiting_for_world_ready = True
        session._nya208_world_ready_deadline = (
            time.monotonic() + NYA208_WORLD_READY_TIMEOUT_SECONDS
        )
        try:
            if getattr(session, "wire_mode", "") == "original":
                # Nya sends PLAYER_LOGGEDIN before its addedToStage path has
                # finished constructing API.ui.content and the world handlers.
                # Sending the complete bootstrap immediately makes every packet
                # race those null objects and leaves MainLoader at 100%.
                time.sleep(NYA208_WORLD_BOOTSTRAP_DELAY_SECONDS)
            enter_original_game(session)
            # Safety fallback for an unexpected bootstrap without WARP_CHAR_INFO.
            flush_nya208_equipped_bootstrap(session)
        except Exception:
            session._nya208_defer_equipped_bootstrap = False
            session._nya208_pending_equipped_bootstrap = None
            session._nya208_waiting_for_world_ready = False
            session._nya208_world_ready_deadline = 0.0
            raise

    # Nya constructs EqpData from API.dataCenter.stats, which WARP_CHAR_INFO
    # creates, and only that same warp handler rebuilds addedStats. Split the
    # initial bootstrap around two ordered warp packets; later refreshes stay direct.
    session_class._runtime_tick = tick_nya208_runtime
    session_class.enter_original_game = enter_nya208_original_game

    def build_nya208_character_entry(session: object) -> bytes:
        """Serialize the native Nya login CharacterEntry contract."""
        mai_list_id = session._current_mai_list_id()
        stats = session._original_character_stats_payload()
        look = bytearray(
            struct.pack(
                "<BBiBi",
                session.character.gender & 0xFF,
                session.character.skin_color & 0xFF,
                session._current_character_face(),
                mai_list_id & 0xFF,
                session._mai_exploded_point_count(mai_list_id),
            )
        )
        look.extend(session._original_equipped_look_payload())
        look.extend(
            struct.pack(
                "<Hi",
                0,
                session._current_ling_face(),
            )
        )
        return stats + bytes(look)

    def handle_nya208_character_list(session: object, payload: str) -> None:
        """Return Nya's byte-counted native list instead of the legacy JSON list."""
        if session.wire_mode != "original":
            handle_character_list(session, payload)
            return
        account = session.accounts.account_by_id(session.account_id) if session.account_id else None
        characters = list(account.get("characters", [])) if account is not None else []
        if not characters:
            session.send_original_packet(module.ServerOpcode.CHARLIST, struct.pack("<Bi", 0, 1))
            return
        session._bind_character(characters[0])
        entry = build_nya208_character_entry(session)
        session.send_original_packet(
            module.ServerOpcode.CHARLIST,
            struct.pack("<B", 1) + entry + struct.pack("<i", 1),
        )

    def handle_nya208_original_login_password(session: object, payload: bytes) -> None:
        """Push Nya's required empty character list in the successful login turn."""
        if handle_original_login_password is None:
            return
        handle_original_login_password(session, payload)
        if session.wire_mode != "original" or not session.account_id:
            return
        account = session.accounts.account_by_id(session.account_id)
        if account is not None and not account.get("characters"):
            handle_nya208_character_list(session, "")

    def handle_nya208_create_character(session: object, payload: str) -> None:
        """Reuse formal creation and advance the Nya login bridge into the game."""
        if session.wire_mode != "original":
            handle_create_character(session, payload)
            return
        before_account = session.accounts.account_by_id(session.account_id) if session.account_id else None
        before_characters = (
            list(before_account.get("characters", []))
            if before_account is not None
            else []
        )
        before_count = len(before_characters)
        if handle_create_character is None:
            return

        # Creation is persisted before the two authored login-stage responses.
        # Resume an interrupted request without creating a second character.
        if before_characters:
            session._bind_character(before_characters[0])
            session.send_original_packet(
                module.ServerOpcode.ADD_NEW_CHAR_ENTRY,
                build_nya208_character_entry(session),
            )
            session.send_original_packet(module.ServerOpcode.LOGIN_STATUS, b"\x00")
            return
        original_send_json = session.send_json
        original_send_system_chat = session._send_system_chat
        create_errors: list[str] = []

        created_entry: dict[str, object] | None = None

        def send_create_response(value: object) -> None:
            nonlocal created_entry
            if (
                isinstance(value, dict)
                and int(value.get("jylx", -1))
                == module.op(module.ServerOpcode.ADD_NEW_CHAR_ENTRY)
                and "charInfo" in value
            ):
                created_entry = value
                return
            original_send_json(value)

        def capture_create_error(message: str) -> None:
            # Login-stage chat has no renderer yet, so preserve the formal
            # validation message and expose it through Nya's login alert path.
            create_errors.append(str(message))

        session.send_json = send_create_response
        session._send_system_chat = capture_create_error
        try:
            handle_create_character(session, payload)
        finally:
            del session.send_json
            del session._send_system_chat
        account = session.accounts.account_by_id(session.account_id) if session.account_id else None
        characters = list(account.get("characters", [])) if account is not None else []
        if len(characters) <= before_count:
            message = (
                create_errors[-1]
                if create_errors
                else "角色创建失败，请检查角色名后重试。"
            )
            session.send_original_packet(
                NYA208_SERVER_MESSAGE_OPCODE,
                bytes((NYA208_MESSAGE_BOX_TYPE,)) + session._original_utf(message),
            )
            return
        # Preserve Nya's authored create flow. The login module acknowledges the
        # new CharacterEntry, MainLoader closes the create UI, and the game then
        # performs one fresh login which alone receives the channel endpoint.
        # Sending SERVER_IP here creates a second channel session and kicks the
        # newly created character as a duplicate login.
        if created_entry is None:
            return
        session.send_original_packet(
            module.ServerOpcode.ADD_NEW_CHAR_ENTRY,
            build_nya208_character_entry(session),
        )
        session.send_original_packet(module.ServerOpcode.LOGIN_STATUS, b"\x00")

    def handle_nya208_generate_character_name(session: object, _: str) -> None:
        if session.wire_mode != "original":
            session.send_json(
                {
                    "jylx": module.ServerOpcode.GENERATED_CHAR_NAME,
                    "charName": session._next_generated_character_name(),
                }
            )
            return
        if not session.account_id:
            return
        session.send_original_packet(
            module.ServerOpcode.GENERATED_CHAR_NAME,
            session._original_utf(session._next_generated_character_name()),
        )

    if handle_character_list is not None and handle_create_character is not None:
        if handle_original_login_password is not None:
            session_class._handle_original_login_password = handle_nya208_original_login_password
        session_class._handle_character_list = handle_nya208_character_list
        session_class._handle_create_character = handle_nya208_create_character
        session_class._handle_generate_character_name = handle_nya208_generate_character_name

    def build_nya208_update_stats(session: object) -> bytes:
        payload = build_update_stats(session)
        combat = session._client_base_combat_stats()
        rows = payload[:-4] + struct.pack("<d", float(session.store.state.money))
        rows += struct.pack(
            "<BiBi",
            module.op(module.StatType.ADD_JOB_SKILL_LEVEL),
            combat[module.StatType.ADD_JOB_SKILL_LEVEL],
            module.op(module.StatType.ADD_WORLD_SKILL_LEVEL),
            combat[module.StatType.ADD_WORLD_SKILL_LEVEL],
        )
        return struct.pack("<H", 23) + rows[2:]

    def send_nya208_original_text(session: object, payload: str) -> bool:
        fields = payload.split(":")
        opcode = session._safe_int(fields[0], -1) if fields else -1
        if opcode == module.op(module.ServerOpcode.UPDATE_STATS):
            rows = []
            for entry in fields[1].split("@") if len(fields) > 1 else ():
                pair = entry.split("&", 1)
                if len(pair) == 2:
                    rows.append((session._safe_int(pair[0], 0), pair[1]))
            encoded = bytearray(struct.pack("<H", len(rows)))
            double_types = {
                module.op(module.StatType.EXP),
                module.op(module.StatType.MONEY),
                module.op(module.StatType.MONEY_BIND),
                module.op(module.StatType.CASH_BIND),
            }
            for stat_type, raw_value in rows:
                encoded.extend(struct.pack("<B", stat_type & 0xFF))
                if stat_type in double_types:
                    encoded.extend(struct.pack("<d", float(raw_value)))
                else:
                    encoded.extend(struct.pack("<i", session._safe_int(raw_value, 0)))
            session.send_original_packet(module.ServerOpcode.UPDATE_STATS, bytes(encoded))
            return True
        if (
            opcode == module.op(module.ServerOpcode.FRIENDLIST)
            and len(fields) >= 2
            and session._safe_int(fields[1], -1) == 38
        ):
            rows = [
                row.split("&")
                for row in (fields[2].split("@") if len(fields) > 2 else ())
                if row and row != "-"
            ]
            encoded = bytearray(struct.pack("<Bi", 38, len(rows)))
            for values in rows:
                values.extend(["0"] * (8 - len(values)))
                encoded.extend(
                    struct.pack(
                        "<idi",
                        session._safe_int(values[0], 0),
                        float(session._safe_int(values[1], 0)),
                        session._safe_int(values[2], 0),
                    )
                )
                encoded.extend(session._original_utf(values[3]))
                encoded.extend(session._original_utf(values[4]))
                encoded.extend(
                    struct.pack(
                        "<3i",
                        session._safe_int(values[5], -1),
                        session._safe_int(values[6], -1),
                        session._safe_int(values[7], -1),
                    )
                )
            session.send_original_packet(module.ServerOpcode.FRIENDLIST, bytes(encoded))
            return True
        if (
            opcode == module.op(module.ServerOpcode.HEAL_STATE)
            and len(fields) >= 5
            and session._safe_int(fields[1], -1) == 1
        ):
            # The formal client reads state, character, partner. Nya 2.0.8 reads
            # state, partner, character, so normalize every single/double-heal
            # transition before the shared binary encoder sees it.
            fields[3], fields[4] = fields[4], fields[3]
            payload = ":".join(fields)
        return send_original_text(session, payload)

    session_class._original_update_stats_payload = build_nya208_update_stats
    session_class._send_original_text = send_nya208_original_text

    def decode_nya208_original_request(
        session: object, opcode: int, payload: bytes
    ) -> str | None:
        if (
            getattr(session, "entered_game", False)
            and getattr(session, "_nya208_waiting_for_world_ready", False)
        ):
            # PLAYER_LOGGEDIN is decoded before enter_original_game. The next
            # client packet proves that MainLoader has created the world and its
            # handlers, so periodic monster/weather/reward packets are now safe.
            session._nya208_waiting_for_world_ready = False
            session._nya208_world_ready_deadline = 0.0
        flush_nya208_equipped_bootstrap(session)
        flush_nya208_lianzhan_bootstrap(session)
        if opcode == 2344 and payload:
            decoded = decode_original_request(session, opcode, payload)
            return decoded if decoded is not None else f"2344:{payload[0]}"
        # MainLoginServer's Nya 2.0.8 CreateChar library serializes
        # CharacterCreateInfo as `writeUTF(JSON.stringify(value))`.  The
        # formal binary decoder expects five scalar fields instead, so it
        # rejects this request before the create handler can run.  Decode the
        # authored UTF/JSON contract first and keep the legacy scalar fallback
        # for older clients.
        if opcode == module.op(module.ClientOpcode.CREATE_CHAR):
            try:
                reader = module.OriginalPacketReader(payload)
                values = json.loads(reader.utf())
                if not isinstance(values, dict):
                    raise ValueError("create character payload is not an object")
                if reader.remaining:
                    raise ValueError("trailing create character bytes")
                return json.dumps(values, ensure_ascii=False, separators=(",", ":"))
            except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
                try:
                    return decode_original_request(session, opcode, payload)
                except (IndexError, struct.error, UnicodeDecodeError, ValueError):
                    return None
        if (
            hasattr(module.ClientOpcode, "MOVE_LIFE")
            and opcode == module.op(module.ClientOpcode.MOVE_LIFE)
        ):
            return decode_original_request(session, opcode, payload)
        if (
            hasattr(module.ClientOpcode, "CHAT_MESSAGE")
            and opcode == module.op(module.ClientOpcode.CHAT_MESSAGE)
        ):
            try:
                reader = module.OriginalPacketReader(payload)
                message_type = reader.byte()
                positions = reader.integer(signed=False)
                message = reader.utf()
                client_name = reader.utf()
                client_character_id = reader.integer()
                tag = None
                if reader.remaining >= 2:
                    tag_size = reader.short(signed=False)
                    tag_payload = reader._read(tag_size)
                    if tag_payload:
                        try:
                            tag = module.Amf3Reader(
                                zlib.decompress(tag_payload)
                            ).read_value()
                        except (IndexError, ValueError, zlib.error):
                            # Item links are optional chat metadata. An unsupported or
                            # malformed AMF reference must not terminate the game socket.
                            tag = None
                request = {
                    "message_type": message_type,
                    "positions": positions,
                    "message": message,
                    "client_name": client_name,
                    "client_character_id": client_character_id,
                    "tag": tag,
                    "target_name": reader.utf(),
                    "target_id": reader.integer(),
                    "char_type": reader.integer(),
                    "target_char_type": reader.integer(),
                }
                if reader.remaining:
                    raise ValueError("trailing Nya chat bytes")
            except ValueError:
                return decode_original_request(session, opcode, payload)
            session._nya208_chat_request = request
            return str(opcode)
        if opcode == module.op(module.ClientOpcode.CHANGE_KEYMAP):
            try:
                reader = module.OriginalPacketReader(payload)
                reader.integer()  # Reserved client sequence field.
                count = reader.integer()
                if count < 0 or count > 256:
                    raise ValueError("invalid Nya key-change count")
                changes: list[dict[str, object]] = []
                for _ in range(count):
                    changes.append(
                        {
                            "key": reader.integer(),
                            "binding": {
                                "type": reader.byte(),
                                "action": reader.integer(),
                            },
                        }
                    )
                if reader.remaining:
                    raise ValueError("trailing Nya key-change bytes")
            except ValueError:
                return decode_original_request(session, opcode, payload)
            return json.dumps(changes, ensure_ascii=False, separators=(",", ":"))
        if opcode == module.op(module.ClientOpcode.STORAGE) and payload:
            action = payload[0]
            if action in {4, 5} and len(payload) >= 9:
                amount = int(struct.unpack_from("<d", payload, 1)[0])
                return f"{opcode}:{action}:{amount}"
        decoded = decode_original_request(session, opcode, payload)
        cash_opcodes = {
            module.op(module.ClientOpcode.CASH_CATEGORY),
            module.op(module.ClientOpcode.CASH_SEARCH),
            module.op(module.ClientOpcode.CASH_ITEM_INFO),
            module.op(module.ClientOpcode.CASH_SHOPPING),
        }
        if decoded is not None and opcode in cash_opcodes:
            return Nya208CashRequest(decoded)
        return decoded

    def decode_nya208_attack_request(
        session: object, opcode: int, payload: bytes
    ) -> str:
        """Decode Nya AttackInfo, whose absent pet UID is encoded as NaN."""
        reader = module.OriginalPacketReader(payload)
        is_player = reader.byte(signed=True)
        character_id = reader.integer()
        raw_pet_uid = reader.double()
        pet_uid = int(raw_pet_uid) if math.isfinite(raw_pet_uid) else 0
        target_count = reader.byte()
        damage_count = reader.byte()
        skill_trigger = reader.boolean()
        skill_level = reader.byte()
        skill_id = reader.integer()
        stance = reader.byte()
        direction = reader.byte(signed=True)
        target_ids: list[int] = []
        for _ in range(target_count):
            target_ids.append(reader.integer())
            reader.boolean()
            for _ in range(damage_count):
                reader.integer()
                reader.byte()
        fields: list[object] = [
            is_player,
            character_id,
            pet_uid,
            session.character.level,
            damage_count,
            target_count,
            (target_count << 4) + damage_count,
            str(skill_trigger).lower(),
            skill_level,
            skill_id,
            stance,
            direction,
            "@".join(str(value) for value in target_ids) or "-",
        ]
        if opcode == module.op(module.ClientOpcode.RANGED_ATTACK) and reader.remaining >= 4:
            fields.extend((reader.short(), reader.short()))
        return str(opcode) + ":" + ":".join(str(value) for value in fields)

    def handle_nya208_bounty(session: object, payload: str) -> None:
        fields = payload.split(":")
        action = session._safe_int(fields[1], -1) if len(fields) > 1 else -1
        if action == 0:
            # List response: action, filter, page, total, row count.
            body = struct.pack("<BBiii", 0, 0, 0, 0, 0)
        elif action == 1:
            # Search response: action and row count.
            body = struct.pack("<Bi", 1, 0)
        elif action == 7:
            # Mine response: action, published count, accepted count.
            body = struct.pack("<Bii", 3, 0, 0)
        elif action == 8:
            # Tracking response: action and row count.
            body = struct.pack("<Bi", 7, 0)
        else:
            # Mutating bounty actions remain disabled until settlement is implemented.
            body = struct.pack("<Bi", 2, -1) + struct.pack("<q", 0)
            body += session._original_utf("悬赏结算尚未启用")
        session.send_original_packet(2344, body)

    def handle_nya208_change_map(session: object, payload: str) -> None:
        """Pass Nya's native reason/target-object/source-portal request through unchanged."""
        session._nya208_portal_candidate = None
        handle_change_map(session, payload)

    def handle_nya208_move(session: object, payload: str) -> None:
        """Keep movement authoritative without treating portal proximity as activation."""
        handle_move(session, payload)

    def handle_nya208_monster_move(session: object, payload: str) -> None:
        """Discard obsolete client monster paths; shared server AI owns movement."""
        if handle_monster_move is None:
            return
        handle_monster_move(session, payload)

    def handle_nya208_chat_message(session: object, payload: str) -> None:
        """Broadcast the decoded Nya ChatMessage without colon-delimited data loss."""
        request = getattr(session, "_nya208_chat_request", None)
        session._nya208_chat_request = None
        if not isinstance(request, dict):
            if handle_chat_message is not None:
                handle_chat_message(session, payload)
            return
        message = str(request.get("message", "")).strip()
        if not message or session._handle_map_chat_command(message):
            return
        message_type = session._safe_int(request.get("message_type"), 3)
        if message_type == 8 and not session._consume_speaker_for_chat():
            return
        if message_type in {7, 8}:
            internal_prefix = session._hokage_charm_chat_prefix()
            message = internal_prefix.replace("[[CHARM~", "[[CHARM:") + message
        positions = session._safe_int(request.get("positions"), 0)
        if positions == 0:
            positions = {
                3: 8,
                4: 16,
                5: 32,
                6: 64,
                7: 128,
                8: 257,
            }.get(message_type, 8)
        elif message_type == 8:
            positions |= 257
        target_name = str(request.get("target_name", ""))
        target_id = session._safe_int(request.get("target_id"), 0)
        char_type = session._safe_int(
            getattr(session.character, "char_type", request.get("char_type", 0)),
            0,
        )
        target_char_type = session._safe_int(request.get("target_char_type"), 0)
        body = b"".join(
            (
                struct.pack("<BI", message_type & 0xFF, positions & 0xFFFFFFFF),
                session._original_utf(message),
                session._original_utf(session.character.name),
                struct.pack(
                    "<ii",
                    session.character.character_id,
                    session.character.level,
                ),
                module.build_original_object_buffer(request.get("tag")),
                session._original_utf(target_name),
                struct.pack("<iii", target_id, char_type, target_char_type),
            )
        )
        recipients = [session]
        hub = session.hub
        if hub is not None:
            if message_type == 4:
                target = hub.session_for_character(target_id)
                if target is None and target_name:
                    target = hub.session_for_name(target_name)
                if target is not None:
                    recipients.append(target)
            elif message_type == 5:
                party = hub.party_for(session.character.character_id)
                if party is not None:
                    recipients.extend(hub.party_sessions(party))
            elif message_type == 6:
                family = session._current_family()
                if family is not None:
                    recipients.extend(session._family_online_sessions(family))
            elif message_type in {7, 8}:
                with hub.lock:
                    recipients.extend(
                        peer
                        for peer in hub.sessions_by_character.values()
                        if peer.entered_game
                    )
            else:
                recipients.extend(hub.visible_peers(session))
        sent: set[int] = set()
        for recipient in recipients:
            marker = id(recipient)
            if marker in sent:
                continue
            sent.add(marker)
            recipient.send_original_packet(module.ServerOpcode.CHAT_MESSAGE, body)

    def handle_nya208_use_item(session: object, payload: str) -> None:
        """Use the shared threshold and authored item-cooldown implementation."""
        handle_use_item(session, payload)

    def handle_nya208_char_config(session: object, payload: str) -> None:
        """Persist the Nya-only settings that the shared server does not know."""
        stripped = session._strip_timestamp(payload).strip()
        if not stripped or stripped == "0":
            handle_char_config(session, payload)
            return
        try:
            config = json.loads(stripped)
        except (TypeError, ValueError, json.JSONDecodeError):
            handle_char_config(session, payload)
            return
        handle_char_config(session, payload)
        if not isinstance(config, dict):
            return
        updates: dict[str, object] = {}
        for name, default in NYA208_CONFIG_DEFAULTS.items():
            value = config.get(name)
            if isinstance(default, bool) and isinstance(value, bool):
                updates[name] = value
            elif isinstance(default, int) and isinstance(value, int) and not isinstance(value, bool):
                updates[name] = value
            elif isinstance(default, float) and isinstance(value, (int, float)) and not isinstance(value, bool):
                updates[name] = float(value)
            elif isinstance(default, dict) and isinstance(value, dict):
                updates[name] = copy.deepcopy(value)
        if "activeKeyScheme" in updates:
            updates["activeKeyScheme"] = max(1, min(5, int(updates["activeKeyScheme"])))
        if "equipExtraTooltipColumns" in updates:
            updates["equipExtraTooltipColumns"] = max(
                1, min(8, int(updates["equipExtraTooltipColumns"]))
            )
        for name in ("bgmVolume", "sfxVolume"):
            if name in updates:
                updates[name] = max(0.0, min(1.0, float(updates[name])))
        if not updates:
            return
        with session.store.lock:
            session.store.state.character_config.update(updates)
            session.store.save()

    def handle_nya208_biguan(session: object, payload: str) -> None:
        """Map Nya's action-1 claim onto the formal reward transaction."""
        fields = session._request_fields(payload, module.ClientOpcode.BIGUAN)
        action = session._safe_int(fields[0], -1) if fields else -1
        if action == 1:
            use_double = len(fields) > 1 and session._safe_int(fields[1], 0) != 0
            formal_action = 11 if use_double else 10
            handle_biguan(
                session,
                f"{module.op(module.ClientOpcode.BIGUAN)}:{formal_action}",
            )
            return
        handle_biguan(session, payload)

    def handle_nya208_ling_qi(session: object, payload: str) -> None:
        """Serve Nya's Sharingan-gated Totsuka appearance and color actions."""
        fields = session._request_fields(payload, module.ClientOpcode.LING)
        action = session._safe_int(fields[0], -1) if fields else -1

        equipped = session.store.state.inventory.get(
            module.op(module.EquipmentPosition.LING_QI)
        )
        has_opened_sword = (
            equipped is not None
            and equipped.item_id == module.op(module.ItemId.OPENED_TOTSUKA_BLADE)
        )
        sharingan_stage = max(
            1,
            int(session.store.state.progression.stages.get("写轮眼", 1)),
        )

        if action == module.op(module.LingQiAction.FACE_LIST):
            body = bytearray(struct.pack("<Bi", action, len(NYA208_LING_QI_FACES)))
            for face_id, required_stage, name in NYA208_LING_QI_FACES:
                body.extend(
                    struct.pack(
                        "<iiB",
                        face_id,
                        required_stage,
                        1 if has_opened_sword and sharingan_stage >= required_stage else 0,
                    )
                )
                body.extend(session._original_utf(name))
            body.extend(struct.pack("<i", len(NYA208_LING_QI_COLORS)))
            for color in NYA208_LING_QI_COLORS:
                color_id, *transform = color
                body.extend(struct.pack("<i", color_id))
                body.extend(session._original_utf("原色" if color_id == 0 else f"颜色{color_id}"))
                body.extend(struct.pack("<4f4i", *transform))
            session.send_original_packet(
                module.ServerOpcode.LING,
                bytes(body),
            )
            return

        requested_value = session._safe_int(fields[1], -1) if len(fields) > 1 else -1
        if action == module.op(module.LingQiAction.CHANGE_FACE):
            definition = next(
                (
                    row
                    for row in NYA208_LING_QI_FACES
                    if row[0] == requested_value
                ),
                None,
            )
            success = int(
                has_opened_sword
                and definition is not None
                and sharingan_stage >= definition[1]
            )
            if success:
                with session.store.lock:
                    session.character.ling_face = requested_value
                    session.store.save()
                session._send_character_look()
            session.send_original_packet(
                module.ServerOpcode.LING,
                struct.pack("<Bii", action, success, requested_value),
            )
            return

        if action == module.op(module.LingQiAction.CHANGE_COLOR):
            valid_color_ids = {row[0] for row in NYA208_LING_QI_COLORS}
            success = int(
                has_opened_sword
                and session.character.ling_face in {row[0] for row in NYA208_LING_QI_FACES}
                and requested_value in valid_color_ids
            )
            if success:
                with session.store.lock:
                    session.character.ling_color = requested_value
                    session.store.save()
                session._send_character_look()
            session.send_original_packet(
                module.ServerOpcode.LING,
                struct.pack("<Bii", action, success, requested_value),
            )
            return

        handle_ling_qi(session, payload)

    cash_catalog = load_nya208_cash_catalog()
    cash_rows_by_sn = {
        int(row["sn"]): row
        for rows in cash_catalog.values()
        for row in rows
    }
    cash_rows_by_item: dict[int, dict[str, int | bytes]] = {}
    for rows in cash_catalog.values():
        for row in rows:
            cash_rows_by_item.setdefault(int(row["item_id"]), row)

    def valid_nya208_cash_rows(
        category_id: int | None = None,
    ) -> list[dict[str, int | bytes]]:
        source = (
            cash_catalog.get(category_id, ())
            if category_id is not None
            else tuple(row for rows in cash_catalog.values() for row in rows)
        )
        return [
            row
            for row in source
            if module.GAME_DATA_CATALOG.get_item_definition(int(row["item_id"]))
            is not None
        ]

    def send_nya208_cash_rows(
        session: object,
        opcode: object,
        rows: list[dict[str, int | bytes]],
        index_begin: int,
        index_end: int,
    ) -> None:
        start = max(0, index_begin)
        page_size = max(0, index_end - start + 1)
        if page_size == 0:
            page_size = 20
        page = rows[start : start + page_size]
        body = struct.pack("<ii", len(rows), len(page))
        body += b"".join(bytes(row["raw"]) for row in page)
        session.send_original_packet(opcode, body)

    def handle_nya208_cash_category(session: object, payload: str) -> None:
        """Serve Nya's captured sn/item/price contract for all eight tabs."""
        if not isinstance(payload, Nya208CashRequest):
            if handle_cash_category is not None:
                handle_cash_category(session, payload)
            return
        fields = session._request_fields(payload, module.ClientOpcode.CASH_CATEGORY)
        category_id = session._safe_int(fields[1], 0) if len(fields) > 1 else 0
        index_begin = session._safe_int(fields[2], 0) if len(fields) > 2 else 0
        index_end = session._safe_int(fields[3], index_begin + 19) if len(fields) > 3 else index_begin + 19
        session.active_cash_category_id = category_id
        send_nya208_cash_rows(
            session,
            module.ServerOpcode.CASH_CATEGORY,
            valid_nya208_cash_rows(category_id),
            index_begin,
            index_end,
        )

    def handle_nya208_cash_search(session: object, payload: str) -> None:
        if not isinstance(payload, Nya208CashRequest):
            if handle_cash_search is not None:
                handle_cash_search(session, payload)
            return
        fields = session._request_fields(payload, module.ClientOpcode.CASH_SEARCH)
        keyword = fields[1].strip().lower() if len(fields) > 1 else ""
        minimum_price = max(0, session._safe_int(fields[2], 0)) if len(fields) > 2 else 0
        maximum_price = max(0, session._safe_int(fields[3], 0)) if len(fields) > 3 else 0
        index_begin = session._safe_int(fields[4], 0) if len(fields) > 4 else 0
        index_end = session._safe_int(fields[5], index_begin + 19) if len(fields) > 5 else index_begin + 19
        matches: list[dict[str, int | bytes]] = []
        for row in valid_nya208_cash_rows():
            definition = module.GAME_DATA_CATALOG.get_item_definition(int(row["item_id"]))
            price = int(row["now_price"])
            if definition is None or (keyword and keyword not in definition.name.lower()):
                continue
            if price < minimum_price or (maximum_price > 0 and price > maximum_price):
                continue
            matches.append(row)
        send_nya208_cash_rows(
            session,
            module.ServerOpcode.CASH_SEARCH,
            matches,
            index_begin,
            index_end,
        )

    def handle_nya208_cash_item_info(session: object, payload: str) -> None:
        if not isinstance(payload, Nya208CashRequest):
            if handle_cash_item_info is not None:
                handle_cash_item_info(session, payload)
            return
        fields = session._request_fields(payload, module.ClientOpcode.CASH_ITEM_INFO)
        item_id = session._safe_int(fields[0], 0) if fields else 0
        row = cash_rows_by_item.get(item_id)
        if (
            row is not None
            and module.GAME_DATA_CATALOG.get_item_definition(item_id) is not None
        ):
            session.send_original_packet(
                module.ServerOpcode.CASH_ITEM_INFO,
                bytes(row["raw"]),
            )

    def handle_nya208_cash_shopping(session: object, payload: str) -> None:
        """Map Nya's product sn to GameData item id and the shared transaction."""
        if not isinstance(payload, Nya208CashRequest):
            if handle_cash_shopping is not None:
                handle_cash_shopping(session, payload)
            return
        fields = session._request_fields(payload, module.ClientOpcode.CASH_SHOPPING)
        if len(fields) < 3:
            return
        shop_type = session._safe_int(fields[0], 0)
        row = cash_rows_by_sn.get(session._safe_int(fields[1], 0))
        quantity = session._safe_int(fields[2], 0)
        if row is not None:
            session._complete_cash_shop_purchase(
                int(row["item_id"]),
                quantity,
                int(row["now_price"]),
                use_bound_cash=shop_type == 1,
            )
        if shop_type == 1:
            session._send_stats(
                {module.StatType.CASH_BIND: session.store.state.coupon_money}
            )
        else:
            session.handle_cash_money("")

    def initialize_nya208_session(session: object, *args: object, **kwargs: object) -> None:
        initialize_session(session, *args, **kwargs)
        session.native_character_creation = True
        config_changed = False
        with session.store.lock:
            for name, default in NYA208_CONFIG_DEFAULTS.items():
                if name not in session.store.state.character_config:
                    session.store.state.character_config[name] = copy.deepcopy(default)
                    config_changed = True
            if config_changed:
                session.store.save()
        session._nya208_pending_equipped_bootstrap = None
        session._nya208_pending_lianzhan_packets = {}
        session._nya208_portal_candidate = None
        session._nya208_observed_map_change_at = getattr(
            session,
            "last_map_change_at",
            0.0,
        )
        session._nya208_blocked_arrival_portal = None
        session._nya208_chat_request = None
        session.handlers[2344] = (
            session.handle_bounty_action
            if hasattr(session, "handle_bounty_action")
            else session._handle_nya208_bounty
        )

    session_class._original_request_text = decode_nya208_original_request
    session_class._decode_original_attack_request = decode_nya208_attack_request
    session_class._handle_nya208_bounty = handle_nya208_bounty
    session_class.handle_player_attack_announce = handle_player_attack_announce
    session_class.handle_attack = handle_attack
    session_class.handle_move = handle_nya208_move
    if handle_monster_move is not None and hasattr(module.ClientOpcode, "MOVE_LIFE"):
        session_class.handle_monster_move = handle_nya208_monster_move
    if handle_chat_message is not None and hasattr(module.ClientOpcode, "CHAT_MESSAGE"):
        session_class.handle_chat_message = handle_nya208_chat_message
    if handle_use_item is not None and hasattr(module.ClientOpcode, "USE_ITEM"):
        session_class.handle_use_item = handle_nya208_use_item
    session_class._handle_change_map = handle_nya208_change_map
    session_class.handle_char_config = handle_nya208_char_config
    if handle_biguan is not None and hasattr(module.ClientOpcode, "BIGUAN"):
        session_class.handle_biguan = handle_nya208_biguan
    session_class.handle_ling_qi = handle_nya208_ling_qi
    cash_contract = (
        ("CASH_CATEGORY", "handle_cash_category", handle_nya208_cash_category),
        ("CASH_SEARCH", "handle_cash_search", handle_nya208_cash_search),
        ("CASH_ITEM_INFO", "handle_cash_item_info", handle_nya208_cash_item_info),
        ("CASH_SHOPPING", "handle_cash_shopping", handle_nya208_cash_shopping),
    )
    for opcode_name, method_name, handler in cash_contract:
        if hasattr(module.ClientOpcode, opcode_name) and hasattr(session_class, method_name):
            setattr(session_class, method_name, handler)
    session_class.__init__ = initialize_nya208_session
    session_class._nya208_protocol_patched = True


def main() -> None:
    preview_root = PROJECT_ROOT / "www" / PREVIEW_ROOT_NAME
    from patch_skill_effects_gamedata import patch_game_data

    patch_game_data(preview_root / "dat" / "GameData.dat")
    verify_nya211_runtime_game_data(preview_root / "dat" / "GameData.dat")
    os.environ["NARUTO_PREVIEW_ROOT"] = PREVIEW_ROOT_NAME
    os.environ["NARUTO_PREVIEW_SAVE"] = SAVE_NAME
    os.environ["NARUTO_HTTP_PORT"] = str(HTTP_PORT)
    os.environ["NARUTO_PROXY_PORT"] = str(PROXY_PORT)
    os.environ["NARUTO_PROXY_GAME_PORT"] = str(GAME_PORT)
    os.environ["NARUTO_SERVER_PORT"] = str(GAME_PORT)
    os.environ["NARUTO_CHANNEL_PORT"] = str(CHANNEL_PORT)
    os.environ["NARUTO_NATIVE_LOGIN_PORT"] = str(NATIVE_LOGIN_PORT)
    host_values = {
        "NARUTO_HTTP_HOST": BIND_HOST,
        "NARUTO_PROXY_HOST": BIND_HOST,
        "NARUTO_SERVER_HOST": BIND_HOST,
        "NARUTO_FLASH_POLICY_HOST": BIND_HOST,
        "NARUTO_PROXY_GAME_HOST": "127.0.0.1",
    }
    for name, value in host_values.items():
        if LOCAL_ONLY:
            os.environ[name] = value
        else:
            os.environ.setdefault(name, value)
    # Public mode must receive a routable address. Local packages explicitly
    # opt into loopback so stale public shell variables cannot leak into them.
    os.environ["NARUTO_PUBLIC_HOST"] = PUBLIC_HOST
    os.environ["NARUTO_ADVERTISE_HOST"] = PUBLIC_HOST
    if LOCAL_ONLY:
        os.environ.setdefault("NARUTO_GM_PASSWORD", "NyaLocal#208")
    os.environ["NARUTO_GAME_DATA_PATH"] = str(preview_root / "dat" / "GameData.dat")
    os.environ["NARUTO_SAVE_PATH"] = str(PROJECT_ROOT / "save" / SAVE_NAME)
    os.environ["NARUTO_PREVIEW_ASSET_PREFIX"] = PREVIEW_ROOT_NAME.rstrip("/") + "/"
    os.environ.setdefault("NARUTO_NO_BROWSER", "1")
    game_server = load_current_game_server()
    game_server.ACCOUNT_SERVICE.ensure_system_family()
    patch_nya208_protocol(game_server)

    from start_hysj_preview import main as start_preview

    start_preview()


if __name__ == "__main__":
    main()
