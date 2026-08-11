#!/usr/bin/env python3
"""Administrative operations used by the responsive GM web panel."""

from __future__ import annotations

import json
import math
import secrets
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from gm_state import GM_STATE, MANUAL_RANK_TYPES
from storage_backend import get_document_store, scope_key_for_path


CURRENCY_FIELDS = {
    "money": "金币",
    "bound_money": "绑定金币",
    "cash_money": "元宝",
    "coupon_money": "礼券",
    "recharge_total": "累计充值",
    "zq": "查克拉精华",
}

# Character AP (attribute points) — the stat allocations earned by levelling.
# Corresponding fields live on `CharacterState`. `remaining_ap` is the free pool.
AP_ATTRIBUTE_FIELDS: dict[str, str] = {
    "ap_atk": "攻击属性点",
    "ap_def": "防御属性点",
    "ap_dex": "敏捷属性点",
    "ap_phy": "体力属性点",
    "remaining_ap": "剩余属性点",
}
# Native CharacterStats stores every AP field as an unsigned 16-bit value.
# Keeping the GM maximum at that boundary prevents a valid web operation from
# creating a character that the original client cannot deserialize safely.
GM_AP_MAX = 65_535

# Keep GM writes below the signed native protocol boundary. The slightly lower
# project limit gives every client-side arithmetic path headroom while matching
# the operator-facing 21亿 rule.
GM_CURRENCY_MAX = 2_100_000_000


def _currency_limit(server: Any, currency: str) -> int:
    """Return the strict GM limit for one persisted currency field."""
    if currency == "zq":
        return min(GM_CURRENCY_MAX, int(server.MAX_ZQ))
    return GM_CURRENCY_MAX


def _server() -> Any:
    import fake_flash_server as server

    return server


def _character_summary(character_id: int) -> dict[str, Any]:
    server = _server()
    for account_id, summary in server.ACCOUNT_SERVICE.characters():
        if int(summary.get("id", 0)) == int(character_id):
            return {"accountId": account_id, **summary}
    raise ValueError("角色不存在")


def _session(character_id: int) -> Any | None:
    server = _server()
    with server.DEFAULT_HUB.lock:
        session = server.DEFAULT_HUB.sessions_by_character.get(int(character_id))
    return session if session is not None and session.entered_game else None


def _store(character_id: int) -> tuple[Any, dict[str, Any], Any | None]:
    server = _server()
    summary = _character_summary(character_id)
    session = _session(character_id)
    if session is not None:
        return session.store, summary, session
    path = server.ACCOUNT_SERVICE.character_path(summary)
    return server.DEFAULT_HUB.character_store(path), summary, None


def _character_skill_compatible(server: Any, skill_id: int, job: int) -> bool:
    """Match the same character-skill ownership rule enforced by the game client."""
    return bool(
        server.GAME_DATA_CATALOG.get_skill_definition(int(skill_id)) is not None
        and (
            int(skill_id) < 10_000
            or int(skill_id) in server.COMMON_PROBABILITY_SKILL_IDS
            or int(skill_id) // 10_000 == int(job)
        )
    )


def _normalize_and_validate_character_state(
    server: Any,
    store: Any,
    session: Any | None,
) -> Any:
    """Round-trip one GM result and exercise native login serializers before commit."""
    normalized = server.SinglePlayerState.from_dict(store.state.to_dict())
    normalized.normalize_item_quantities()
    for field_name in (
        "money",
        "bound_money",
        "cash_money",
        "coupon_money",
        "recharge_total",
        "storage_money",
    ):
        value = int(getattr(normalized, field_name, 0))
        if not 0 <= value <= GM_CURRENCY_MAX:
            raise ValueError(f"{field_name} exceeds the safe client range")

    previous = store.state
    store.state = normalized
    try:
        if session is not None:
            # These are the packets used during native login and profile refresh.
            # Building them without sending catches struct overflows and malformed
            # GM equipment/pet data before the new save becomes durable.
            session._original_character_stats_payload()
            session._original_warp_char_info_payload()
            session._original_character_full_info_payload()
    except Exception:
        store.state = previous
        raise
    return normalized


def _run_character_action_transaction(
    action: str,
    handler: Any,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Rollback a character save when a GM handler or client serializer fails."""
    character_id = int(payload.get("characterId", 0) or 0)
    if character_id <= 0:
        return handler(payload)
    server = _server()
    store, _, session = _store(character_id)
    with store.lock:
        snapshot = store.state.to_dict()
        snapshot_level = int(store.state.character.level)
    try:
        result = handler(payload)
        with store.lock:
            store.state = _normalize_and_validate_character_state(
                server,
                store,
                session,
            )
            store.save()
        return result
    except Exception as exc:
        try:
            with store.lock:
                store.state = server.SinglePlayerState.from_dict(snapshot)
                store.save()
            server.ACCOUNT_SERVICE.update_character_summary(
                character_id,
                level=snapshot_level,
            )
        except Exception as rollback_exc:
            raise ValueError(
                f"GM action {action} failed and save rollback also failed: {rollback_exc}"
            ) from exc
        print(
            f"[gm-rollback] action={action} characterId={character_id} error={exc}",
            flush=True,
        )
        raise ValueError(
            f"GM action {action} failed; the character save was restored: {exc}"
        ) from exc


def player_rows() -> list[dict[str, Any]]:
    server = _server()
    rows: list[dict[str, Any]] = []
    for account_id, summary in server.ACCOUNT_SERVICE.characters():
        character_id = int(summary.get("id", 0))
        session = _session(character_id)
        store = session.store if session is not None else server.DEFAULT_HUB.character_store(
            server.ACCOUNT_SERVICE.character_path(summary)
        )
        state = store.state
        rows.append(
            {
                "accountId": account_id,
                "characterId": character_id,
                "name": state.character.name,
                "level": state.character.level,
                "gender": state.character.gender,
                "online": session is not None,
                "line": session.line_id if session is not None else int(summary.get("line", 1)),
                "mapId": state.character.map_id,
                "hp": state.character.hp,
                "maxHp": session._current_max_hp() if session is not None else state.character.max_hp,
                "money": state.money,
                "boundMoney": state.bound_money,
                "cashMoney": state.cash_money,
                "couponMoney": state.coupon_money,
                "rechargeTotal": state.recharge_total,
                "zq": state.character.zq,
            }
        )
    return sorted(rows, key=lambda row: (not row["online"], row["characterId"]))


def item_rows(query: str = "", limit: int = 120) -> list[dict[str, Any]]:
    server = _server()
    keyword = str(query).strip().casefold()
    rows: list[dict[str, Any]] = []
    for section, item_ids in server.GAME_DATA_CATALOG.shop_item_ids_by_section().items():
        for item_id in item_ids:
            definition = server.GAME_DATA_CATALOG.get_item_definition(item_id)
            if definition is None:
                continue
            if keyword and keyword not in definition.name.casefold() and keyword not in str(item_id):
                continue
            rows.append(
                {
                    "itemId": item_id,
                    "name": definition.name,
                    "section": section,
                    "stackMax": server._item_stack_limit(item_id),
                }
            )
            if len(rows) >= max(1, min(500, int(limit))):
                return rows
    return rows


def spirit_rows() -> list[dict[str, Any]]:
    """Return every levelable Will template for the GM grant panel."""
    server = _server()
    color_names = {
        "green": "绿色",
        "blue": "蓝色",
        "pueple": "紫色",
        "pink": "粉色",
        "red": "红色",
        "lpink": "六道",
    }
    rows: list[dict[str, Any]] = []
    for item_id, definition in sorted(server.GAME_DATA_CATALOG.spirit_items().items()):
        spec = definition.get("spec") if isinstance(definition, dict) else None
        color = str(spec.get("color") or "") if isinstance(spec, dict) else ""
        maximum_experience = server.GAME_DATA_CATALOG.spirit_level_threshold(
            item_id, 20
        )
        if color not in color_names or maximum_experience <= 0:
            continue
        attributes = server.GAME_DATA_CATALOG.spirit_attribute_values(item_id, 20)
        rows.append(
            {
                "itemId": int(item_id),
                "name": server.GAME_DATA_CATALOG.spirit_name(item_id),
                "color": color,
                "colorName": color_names[color],
                "level": 20,
                "experience": int(maximum_experience),
                "attributes": attributes,
            }
        )
    return rows


def carve_rows() -> list[dict[str, Any]]:
    """Return the four authored equipment secret seals and their maximum values."""
    server = _server()
    type_names = {
        "ATK_RATE": "攻击加成",
        "DAMAGE_REFLECT": "伤害反弹",
        "IGNORE_DEF": "无视防御",
        "DAMAGE_REDUCE": "伤害减免",
    }
    root = server.GAME_DATA_CATALOG.get_equipment_system_config("carve")
    values = root.get("value") if isinstance(root.get("value"), dict) else {}
    rows: list[dict[str, Any]] = []
    for raw_carve_id, config in sorted(values.items(), key=lambda row: int(row[0])):
        if not isinstance(config, dict):
            continue
        carve_type = str(config.get("type") or "")
        rows.append(
            {
                "carveId": int(raw_carve_id),
                "name": str(config.get("name") or f"秘印{raw_carve_id}"),
                "type": carve_type,
                "typeName": type_names.get(carve_type, carve_type),
                "maxValue": max(1, int(config.get("value_max") or 1)),
            }
        )
    return rows


def max_gem_rows() -> list[dict[str, Any]]:
    """Return the six terminal level-10 ordinary attribute gems."""
    server = _server()
    attribute_names = {
        int(server.EquipmentAttribute.MAX_HP): "生命",
        int(server.EquipmentAttribute.ATTACK): "攻击",
        int(server.EquipmentAttribute.DEFENCE): "防御",
        int(server.EquipmentAttribute.CRITICAL): "暴击",
        int(server.EquipmentAttribute.EVASION): "闪避",
        int(server.EquipmentAttribute.ATTACK_SPEED): "攻击速度",
    }
    rows: list[dict[str, Any]] = []
    for raw_item_id in server.GAME_DATA_CATALOG.raw_item_sections.get("BaoShi", {}):
        item_id = int(raw_item_id)
        if item_id // 100_000 != 130:
            continue
        inlay = server.GAME_DATA_CATALOG.get_stone_inlay(item_id)
        progression = server.GAME_DATA_CATALOG.get_stone_progression(item_id)
        definition = server.GAME_DATA_CATALOG.get_item_definition(item_id)
        if (
            inlay is None
            or progression is None
            or definition is None
            or inlay[2] > 0
            or progression[1] > 0
            or inlay[0] != 10
            or inlay[1] not in attribute_names
        ):
            continue
        rows.append(
            {
                "itemId": item_id,
                "name": definition.name,
                "attributeType": int(inlay[1]),
                "attributeName": attribute_names[int(inlay[1])],
                "level": int(inlay[0]),
                "value": int(inlay[3]),
            }
        )
    return sorted(rows, key=lambda row: (row["attributeType"], row["itemId"]))


def myth_attribute_rows() -> list[dict[str, Any]]:
    """Return the six unique attribute types supported by native myth forging."""
    server = _server()
    return [
        {"type": int(attribute), "name": name, "maxValue": 18}
        for attribute, name in (
            (server.EquipmentAttribute.MAX_HP, "生命百分比"),
            (server.EquipmentAttribute.MAX_MP, "查克拉百分比"),
            (server.EquipmentAttribute.ATTACK, "攻击百分比"),
            (server.EquipmentAttribute.DEFENCE, "防御百分比"),
            (server.EquipmentAttribute.CRITICAL, "暴击百分比"),
            (server.EquipmentAttribute.EVASION, "闪避百分比"),
        )
    ]


def additional_attribute_rows() -> list[dict[str, Any]]:
    """Return every attribute authored for ordinary character and pet refining."""
    server = _server()
    return [
        {"type": int(attribute), "name": name}
        for attribute, name in (
            (server.EquipmentAttribute.MAX_HP, "生命上限"),
            (server.EquipmentAttribute.MAX_MP, "查克拉上限"),
            (server.EquipmentAttribute.ATTACK, "攻击"),
            (server.EquipmentAttribute.DEFENCE, "防御"),
            (server.EquipmentAttribute.CRITICAL, "暴击"),
            (server.EquipmentAttribute.EVASION, "闪避"),
        )
    ]


def ap_attribute_rows() -> list[dict[str, Any]]:
    """Return character AP stat attributes with their display names and max cap."""
    return [
        {"key": key, "name": name, "maxValue": GM_AP_MAX}
        for key, name in AP_ATTRIBUTE_FIELDS.items()
    ]


def special_percent_rows() -> list[dict[str, Any]]:
    """Wash special attributes: percent slots (3 on gear, 4 on transform cards).
    Value range is 1~5; max value is always 5."""
    server = _server()
    attrs = server.EquipmentAttribute
    return [
        {"type": int(attrs.MAX_HP), "name": "生命上限%", "valueMax": 5},
        {"type": int(attrs.MAX_MP), "name": "查克拉上限%", "valueMax": 5},
        {"type": int(attrs.ATTACK), "name": "攻击%", "valueMax": 5},
        {"type": int(attrs.DEFENCE), "name": "防御%", "valueMax": 5},
        {"type": int(attrs.CRITICAL), "name": "暴击%", "valueMax": 5},
        {"type": int(attrs.EVASION), "name": "闪避%", "valueMax": 5},
        {"type": int(attrs.ATTACK_SPEED), "name": "攻击速度%", "valueMax": 5},
        {"type": int(attrs.WALK_SPEED), "name": "移动速度%", "valueMax": 5},
        {"type": int(attrs.ATTACK_RATE), "name": "攻击率%", "valueMax": 5},
    ]


def special_fixed_rows() -> list[dict[str, Any]]:
    """Wash special attributes: fixed-value slots (always 3 lines).
    Max value formula: 100 + stage * 150 (applied per-equipment later)."""
    server = _server()
    attrs = server.EquipmentAttribute
    return [
        {"type": int(attrs.MAX_HP), "name": "生命值上限"},
        {"type": int(attrs.MAX_MP), "name": "查克拉上限"},
        {"type": int(attrs.ATTACK), "name": "攻击"},
        {"type": int(attrs.DEFENCE), "name": "防御"},
        {"type": int(attrs.CRITICAL), "name": "暴击"},
        {"type": int(attrs.EVASION), "name": "闪避"},
    ]


def special_effect_rows() -> list[dict[str, Any]]:
    """Rare passive buffs shown on the last line of the special-wash panel.

    Names match the client-side Chinese labels:
      1 = 牛鬼 (八尾), 2 = 守鹤 (一尾), 3 = 矶抚 (三尾).
    """
    return [
        {"type": 1, "name": "效果①（牛鬼之力）"},
        {"type": 2, "name": "效果②（守鹤之力）"},
        {"type": 3, "name": "效果③（矶抚之力）"},
    ]


# 五行映射（客户端实际显示）：1=水 2=风 3=火 4=土 5=雷
MYTH_WUXING_NAMES = {
    1: "水",
    2: "风",
    3: "火",
    4: "土",
    5: "雷",
}


def myth_wuxing_rows() -> list[dict[str, Any]]:
    """Return the five myth wuxing elements with max level."""
    return [
        {"type": 1, "name": "水", "maxLevel": 3},
        {"type": 2, "name": "风", "maxLevel": 3},
        {"type": 3, "name": "火", "maxLevel": 3},
        {"type": 4, "name": "土", "maxLevel": 3},
        {"type": 5, "name": "雷", "maxLevel": 3},
    ]


def equipment_position_rows() -> dict[str, list[dict[str, Any]]]:
    """Return selectable character and pet equipment positions."""
    server = _server()
    character_names = {
        server.EquipmentPosition.WEAPON: "武器",
        server.EquipmentPosition.GLOVES: "手套",
        server.EquipmentPosition.CLOTHES: "衣服",
        server.EquipmentPosition.HEADBAND: "护额",
        server.EquipmentPosition.BELT: "腰带",
        server.EquipmentPosition.SHOES: "鞋子",
        server.EquipmentPosition.HAT: "帽子",
        server.EquipmentPosition.NECKLACE: "项链",
        server.EquipmentPosition.RING: "戒指",
        server.EquipmentPosition.BRACELET: "手镯",
        server.EquipmentPosition.BAG: "忍具包",
        server.EquipmentPosition.CLOAK: "披风",
    }
    pet_names = {
        server.PetEquipmentPosition.ARMOR: "兽铠",
        server.PetEquipmentPosition.CLAW: "兽爪",
        server.PetEquipmentPosition.CHARM: "兽符",
        server.PetEquipmentPosition.HELMET: "兽盔",
    }
    return {
        "character": [
            {"position": int(position), "name": name}
            for position, name in character_names.items()
        ],
        "pet": [
            {"position": int(position), "name": name}
            for position, name in pet_names.items()
        ],
    }


def myth_skill_rows() -> list[dict[str, Any]]:
    """Return the complete native myth-forge skill pool used by this client."""
    return [
        {"skillId": skill_id, "name": f"神话技能 {skill_id}", "maxLevel": 9}
        for skill_id in (101, 102, 103, 104, 105, 201, 202, 203, 301, 302)
    ]


def tale_attribute_rows() -> list[dict[str, Any]]:
    """Return every native legendary core attribute at its authored maximum."""
    server = _server()
    names = {
        int(server.EquipmentAttribute.MAX_HP): "生命上限",
        int(server.EquipmentAttribute.MAX_MP): "查克拉上限",
        int(server.EquipmentAttribute.ATTACK): "攻击",
        int(server.EquipmentAttribute.DEFENCE): "防御",
        int(server.EquipmentAttribute.ATTACK_SPEED): "攻击速度",
        int(server.EquipmentAttribute.WALK_SPEED): "移动速度",
        int(server.EquipmentAttribute.ATTACK_RATE): "攻击加成",
        int(server.EquipmentAttribute.DAMAGE_REFLECT): "伤害反弹",
        int(server.EquipmentAttribute.DAMAGE_REDUCE): "伤害减免",
        int(server.EquipmentAttribute.IGNORE_DEFENCE): "无视防御",
    }
    root = server.GAME_DATA_CATALOG.get_equipment_system_config("taleAttributes")
    rows: list[dict[str, Any]] = []
    for config in root.values():
        if not isinstance(config, dict):
            continue
        attribute = server.EQUIPMENT_ATTRIBUTE_BY_NAME.get(
            str(config.get("type") or "").lower()
        )
        if attribute is None:
            continue
        attribute_type = int(attribute)
        rows.append(
            {
                "type": attribute_type,
                "name": names.get(attribute_type, str(config.get("type") or attribute_type)),
                "maxValue": int(config.get("valueTo") or 0),
            }
        )
    return rows


def event_rows() -> list[dict[str, Any]]:
    server = _server()
    rows: list[dict[str, Any]] = [
        {"key": "world-boss", "name": "世界 Boss 宇智波斑"},
        {"key": "muye-gift", "name": "木叶馈赠"},
    ]
    seen: set[int] = set()
    for event in server.GAME_DATA_CATALOG.scheduled_monster_events():
        group_id = int(event.get("groupId", 0))
        if group_id in seen:
            continue
        seen.add(group_id)
        monster_names = []
        for template_id in event.get("mobs", ()):
            definition = server.GAME_DATA_CATALOG.get_monster_definition(int(template_id))
            if definition.name not in monster_names:
                monster_names.append(definition.name)
        rows.append(
            {
                "key": f"monster-event:{group_id}",
                "name": " / ".join(monster_names) or f"怪物事件 {group_id}",
            }
        )
    for row in server.GAME_DATA_CATALOG.wang_boss_entries():
        definition = server.GAME_DATA_CATALOG.get_monster_definition(int(row["mobId"]))
        rows.append(
            {"key": f"boss-wang:{row['index']}", "name": f"Boss {definition.name}"}
        )
    return rows


def _event_display_name(event_key: str) -> str:
    key = str(event_key).strip()
    return next(
        (
            str(row["name"])
            for row in event_rows()
            if str(row.get("key", "")) == key
        ),
        key or "未知事件",
    )


def _online_sessions(server: Any) -> list[Any]:
    with server.DEFAULT_HUB.lock:
        return [
            session
            for session in server.DEFAULT_HUB.sessions_by_character.values()
            if session.entered_game
        ]


def _announcement_highlights(raw_highlights: Any) -> tuple[str, ...]:
    if isinstance(raw_highlights, str):
        values = raw_highlights.replace("，", ",").replace("\n", ",").split(",")
    elif isinstance(raw_highlights, (list, tuple)):
        values = raw_highlights
    else:
        values = ()
    return tuple(
        dict.fromkeys(
            str(value).strip()[:40]
            for value in values
            if str(value).strip()
        )
    )[:10]


def _publish_world_announcement(
    message: str,
    *,
    notice_type: str = "marquee",
    highlights: Any = (),
) -> int:
    """Send one native announcement and its comprehensive-channel record."""
    text = str(message).strip()
    if not text:
        raise ValueError("公告内容不能为空")
    if len(text) > 500:
        raise ValueError("公告内容不能超过 500 个字符")
    normalized_type = str(notice_type).strip().lower()
    if normalized_type not in {"marquee", "green"}:
        raise ValueError("公告类型必须是白色跑马灯或绿色大字")
    server = _server()
    sessions = _online_sessions(server)
    if not sessions:
        return 0
    if normalized_type == "green":
        sessions[0]._broadcast_rare_event(
            text,
            highlights=_announcement_highlights(highlights),
        )
    else:
        for session in sessions:
            session._send_scrolled_announcement(text)
            session._send_comprehensive_chat(text)
    return len(sessions)


def catalog() -> dict[str, Any]:
    server = _server()
    weather_names = {
        0: "晴天",
        1: "小雨",
        2: "中雨",
        3: "大雨",
        4: "小雪",
        5: "中雪",
        6: "大雪",
        7: "薄雾",
        8: "大雾",
        9: "浓雾",
    }

    def _safe(call, default):
        try:
            return call()
        except Exception:
            return default

    return {
        "currencies": [
            {
                "key": key,
                "name": name,
                "maxValue": _currency_limit(server, key),
            }
            for key, name in CURRENCY_FIELDS.items()
        ],
        "buffs": [
            {"type": int(buff), "name": buff.name}
            for buff in server.CombatBuffType
        ],
        "spirits": _safe(spirit_rows, []),
        "events": _safe(event_rows, []),
        "weather": [
            {"type": -1, "name": "恢复自动天气"},
            *[
                {"type": weather_type, "name": weather_names[weather_type]}
                for weather_type in range(10)
            ],
        ],
        "rankings": [
            {"key": "MENGMEI_PH", "name": "美女榜"},
            {"key": "SHUAIGE_PH", "name": "帅哥榜"},
            {"key": "REBUG_CHARA", "name": "玩家代表榜"},
        ],
        "carves": _safe(carve_rows, []),
        "maxGems": _safe(max_gem_rows, []),
        "mythAttributes": _safe(myth_attribute_rows, []),
        "additionalAttributes": _safe(additional_attribute_rows, []),
        "equipmentPositions": _safe(
            equipment_position_rows, {"character": [], "pet": []}
        ),
        "mythSkills": _safe(myth_skill_rows, []),
        "taleAttributes": _safe(tale_attribute_rows, []),
        "mythWuxing": _safe(myth_wuxing_rows, []),
        "apAttributes": _safe(ap_attribute_rows, []),
        "specialPercentAttrs": _safe(special_percent_rows, []),
        "specialFixedAttrs": _safe(special_fixed_rows, []),
        "specialEffects": _safe(special_effect_rows, []),
    }


def _sync_player(session: Any) -> None:
    session._send_all_stats()
    session.handle_cash_money("")
    session._send_activity_panel()
    session._send_vip_status()
    session._send_ranking_title_update(visible=True)


def _test_skill_ids(server: Any) -> tuple[int, ...]:
    """Return every character skill that the current GameData can expose.

    Skill books in the authoritative JiNengShu section are the source of the
    ordinary profession/world/other skill list.  The project-only forbidden
    and special rows have no normal book entry, so include their explicit
    runtime definitions as well.  Pet books are 7xxxxxx and intentionally stay
    out of the character skill list.
    """
    skill_ids: set[int] = set()
    for item_id in server.GAME_DATA_CATALOG.shop_item_ids_by_section().get(
        "JiNengShu", ()
    ):
        item_id = int(item_id)
        if not 6_000_000 <= item_id < 7_000_000:
            continue
        item = server.GAME_DATA_CATALOG.get_item_definition(item_id)
        if item is None or int(item.skill_id) <= 0:
            continue
        skill_id = int(item.skill_id)
        if server.GAME_DATA_CATALOG.get_skill_definition(skill_id) is not None:
            skill_ids.add(skill_id)

    skill_ids.update(int(skill_id) for skill_id in server.FORBIDDEN_SKILL_BALANCE)
    skill_ids.update(int(skill_id) for skill_id in server.PROJECT_SPECIAL_SKILL_BALANCE)
    # These relationship skills are still character skills and are useful for
    # testing, but their normal lifecycle remains owned by the bond system.
    skill_ids.update(int(skill_id) for skill_id in server.NON_UPGRADABLE_BOND_SKILL_IDS)
    return tuple(
        sorted(
            skill_id
            for skill_id in skill_ids
            if server.GAME_DATA_CATALOG.get_skill_definition(skill_id) is not None
        )
    )


def grant_all_skills(payload: dict[str, Any]) -> dict[str, Any]:
    """Grant all current character skills to one account for manual testing."""
    character_id = int(payload.get("characterId", 0))
    server = _server()
    store, _, session = _store(character_id)
    character_job = int(store.state.character.job)
    skill_ids = tuple(
        skill_id
        for skill_id in _test_skill_ids(server)
        if _character_skill_compatible(server, skill_id, character_job)
    )
    if not skill_ids:
        raise ValueError("authoritative GameData contains no character skills")
    requested_level = max(1, min(120, int(payload.get("level", 1))))
    updates: list[Any] = []
    added = 0
    with store.lock:
        for skill_id in skill_ids:
            skill = store.state.skills.get(skill_id)
            if skill is None:
                skill = server.SkillState(skill_id=skill_id, level=requested_level)
                store.state.skills[skill_id] = skill
                added += 1
            elif int(skill.level) <= 0:
                skill.level = requested_level
            updates.append(skill)
        notice_body = (
            f"GM granted {len(updates)} character skills for testing "
            f"({added} newly learned, level {requested_level})."
        )
        _append_notification_locked(
            store,
            kind="skills",
            title="GM skills grant",
            body=notice_body,
        )
        store.save()

    if session is not None:
        for skill in updates:
            session._send_skill_update(skill)
        # Passive and resistance skills change derived stats immediately.
        session._send_all_stats()
    _push_online_notification(session, notice_body)
    return {
        "characterId": character_id,
        "skillCount": len(updates),
        "added": added,
        "level": requested_level,
        "skillIds": [int(skill.skill_id) for skill in updates],
    }


def _append_notification_locked(
    store: Any,
    *,
    kind: str,
    title: str,
    body: str,
) -> dict[str, Any]:
    """Append one display-only GM notification while the character store is locked."""
    notification = {
        "id": f"{time.time_ns():x}-{secrets.token_hex(4)}",
        "kind": str(kind)[:24],
        "title": str(title)[:80],
        "body": str(body)[:500],
        "createdAt": int(time.time()),
        "readAt": 0,
    }
    store.state.gm_notifications.append(notification)
    store.state.gm_notifications = store.state._sanitize_gm_notifications(
        store.state.gm_notifications
    )
    return dict(notification)


def _push_online_notification(session: Any | None, body: str) -> None:
    """Give native Flash users an immediate fallback while the web icon updates."""
    if session is not None:
        session._send_system_chat(f"GM礼物已到账：{body}")


def player_notifications(
    character_id: int,
    *,
    mark_read: bool = False,
) -> dict[str, Any]:
    """Return one character's GM notification history and optionally mark it read."""
    store, _, _ = _store(int(character_id))
    now = int(time.time())
    with store.lock:
        notifications = store.state._sanitize_gm_notifications(
            store.state.gm_notifications
        )
        changed = notifications != store.state.gm_notifications
        if mark_read:
            for notification in notifications:
                if int(notification.get("readAt") or 0) <= 0:
                    notification["readAt"] = now
                    changed = True
        if changed:
            store.state.gm_notifications = notifications
            store.save()
        unread = sum(
            1
            for notification in notifications
            if int(notification.get("readAt") or 0) <= 0
        )
        rows = [dict(notification) for notification in reversed(notifications)]
    return {
        "characterId": int(character_id),
        "unread": unread,
        "notifications": rows,
    }


def change_currency(payload: dict[str, Any]) -> dict[str, Any]:
    server = _server()
    character_id = int(payload.get("characterId", 0))
    currency = str(payload.get("currency", ""))
    operation = str(payload.get("operation", "add"))
    amount = int(payload.get("amount", 0))
    if currency not in CURRENCY_FIELDS:
        raise ValueError("不支持的货币")
    if operation not in {"add", "set"}:
        raise ValueError("货币操作必须是增加/扣除或设为")
    store, _, session = _store(character_id)
    with store.lock:
        target = store.state.character if currency == "zq" else store.state
        current = int(getattr(target, currency))
        value = amount if operation == "set" else current + amount
        limit = _currency_limit(server, currency)
        currency_name = CURRENCY_FIELDS[currency]
        if value < 0:
            raise ValueError(
                f"{currency_name}调整后不能低于 0；当前为 {current:,}，本次未执行"
            )
        if value > limit:
            raise ValueError(
                f"{currency_name}上限为 {limit:,}；调整后将达到 {value:,}，本次未发放"
            )
        setattr(target, currency, value)
        if operation == "set":
            notice_body = f"GM将您的{currency_name}调整为 {value:,}。"
        elif amount >= 0:
            notice_body = (
                f"GM向您发放了{currency_name} {amount:,}，当前共有 {value:,}。"
            )
        else:
            notice_body = (
                f"GM调整了您的{currency_name} {amount:,}，当前共有 {value:,}。"
            )
        _append_notification_locked(
            store,
            kind="currency",
            title="GM货币发放",
            body=notice_body,
        )
        store.save()
    if session is not None:
        _sync_player(session)
    _push_online_notification(session, notice_body)
    return {"characterId": character_id, "currency": currency, "value": value}


def _add_offline_item(server: Any, store: Any, item_id: int, quantity: int) -> Any:
    definition = server.GAME_DATA_CATALOG.get_item_definition(item_id)
    if definition is None:
        raise ValueError("道具不存在")
    stack_limit = server._item_stack_limit(item_id)
    if quantity <= 0:
        raise ValueError("道具发放数量必须是正整数，本次未发放")
    if quantity > stack_limit:
        raise ValueError(
            f"【{definition.name}】单次发放堆叠上限为 {stack_limit}；"
            f"请求数量为 {quantity}，本次未发放"
        )
    can_trade = item_id not in server.GAME_DATA_CATALOG.bind_item_ids()
    equipment = server.GAME_DATA_CATALOG.get_equipment_definition(
        item_id
    ) or server.GAME_DATA_CATALOG.get_pet_equipment_definition(item_id)
    if equipment is None:
        for item in store.state.inventory.values():
            if (
                item.slot > 0
                and item.item_id == item_id
                and item.can_trade == can_trade
                and server._safe_item_quantity(item.item_id, item.quantity) + quantity
                <= stack_limit
            ):
                item.quantity = (
                    server._safe_item_quantity(item.item_id, item.quantity) + quantity
                )
                return item
    slot = next(
        (
            slot
            for slot in range(1, store.state.bag_capacity + 1)
            if slot not in store.state.inventory
        ),
        0,
    )
    if slot <= 0:
        raise ValueError("背包已满")
    item = server.InventoryItem(
        item_id=item_id,
        slot=slot,
        quantity=quantity,
        unique_id=int(time.time_ns() % 9_000_000_000),
        can_trade=can_trade,
    )
    if equipment is not None:
        if (
            equipment.position == server.EquipmentPosition.BADGE
            or (
                equipment.position == server.EquipmentPosition.SPECIAL
                and equipment.type_name == "EquipS1"
            )
        ):
            item.aptitude = 0
        item.base_attr_type = equipment.base_attr_type
        item.base_attr_value = equipment.base_attr_value
        item.max_endure = equipment.max_endure
        item.endure = equipment.max_endure
        if equipment.type_name == "RenJu":
            item.plus_attributes = [
                [int(attribute_type), int(value)]
                for attribute_type, value in equipment.additional_attributes
            ]
        if equipment.position == server.EquipmentPosition.TRANSFORM:
            server.apply_nya208_transform_card_profile(item)
            if item_id not in store.state.transform_card_collection:
                store.state.transform_card_collection.append(item_id)
                store.state.transform_card_collection.sort()
    store.state.inventory[slot] = item
    return item


def grant_item(payload: dict[str, Any]) -> dict[str, Any]:
    server = _server()
    character_id = int(payload.get("characterId", 0))
    item_id = int(payload.get("itemId", 0))
    quantity = int(payload.get("quantity", 1))
    definition = server.GAME_DATA_CATALOG.get_item_definition(item_id)
    if definition is None:
        raise ValueError("道具不存在")
    stack_limit = server._item_stack_limit(item_id)
    if quantity <= 0:
        raise ValueError("道具发放数量必须是正整数，本次未发放")
    if quantity > stack_limit:
        raise ValueError(
            f"【{definition.name}】单次发放堆叠上限为 {stack_limit}；"
            f"请求数量为 {quantity}，本次未发放"
        )
    store, _, session = _store(character_id)
    with store.lock:
        if session is not None:
            item, created = session._add_inventory_item(item_id, quantity)
        else:
            before = set(store.state.inventory)
            item = _add_offline_item(server, store, item_id, quantity)
            created = item.slot not in before
        notice_body = (
            f"GM向您发放了【{definition.name}】×{quantity}，"
            "奖励已直接放入背包。"
        )
        _append_notification_locked(
            store,
            kind="item",
            title="GM道具发放",
            body=notice_body,
        )
        store.save()
    if session is not None:
        if created:
            session._send_inventory_add(item, from_drop=0)
        else:
            session._send_inventory_update(item, from_drop=0)
    _push_online_notification(session, notice_body)
    return {
        "characterId": character_id,
        "itemId": item_id,
        "quantity": item.quantity,
        "slot": item.slot,
    }


def clear_player_inventory(payload: dict[str, Any]) -> dict[str, Any]:
    """清空角色背包（仅未穿戴物品）、仓库、意志背包（仅未装备意志）。

    payload.scopes: ["bag"] / ["storage"] / ["spirit"] 任意组合，
    全部三个即“一键全部清空”。已穿戴装备与已装备意志不会删除。
    """
    server = _server()
    character_id = int(payload.get("characterId", 0))
    scopes = payload.get("scopes") or []
    if not isinstance(scopes, list) or not scopes:
        raise ValueError("请选择要清空的范围")
    valid = {"bag", "storage", "spirit"}
    if any(scope not in valid for scope in scopes):
        raise ValueError("清空范围只能是 bag / storage / spirit")
    store, _, session = _store(character_id)
    removed_bag: list[int] = []
    removed_storage: list[int] = []
    removed_spirit = 0
    with store.lock:
        if "bag" in scopes:
            removed_bag = [slot for slot in store.state.inventory if slot > 0]
            for slot in removed_bag:
                del store.state.inventory[slot]
        if "storage" in scopes:
            removed_storage = list(store.state.storage.keys())
            store.state.storage.clear()
        if "spirit" in scopes:
            kept = [
                item
                for item in store.state.progression.spirit_items
                if item.slot < 0
            ]
            removed_spirit = len(store.state.progression.spirit_items) - len(kept)
            store.state.progression.spirit_items = kept
        summary_parts = []
        if removed_bag:
            summary_parts.append(f"背包 {len(removed_bag)} 件")
        if removed_storage:
            summary_parts.append(f"仓库 {len(removed_storage)} 件")
        if removed_spirit:
            summary_parts.append(f"意志背包 {removed_spirit} 个")
        _append_notification_locked(
            store,
            kind="item",
            title="GM清空物品",
            body=f"GM 已清空：{'；'.join(summary_parts) or '无物品可清空'}。",
        )
        store.save()
    if session is not None:
        for slot in removed_bag:
            session._send_inventory_clear(slot, server.InventoryType.BAG)
        for slot in removed_storage:
            session.send_text(
                f"{server.op(server.ServerOpcode.OPEN_STORAGE)}:5:{slot}"
            )
        if removed_spirit:
            session._send_spirit_inventory(True)
    _push_online_notification(
        session,
        f"已清空{'、'.join(summary_parts) or '无物品'}",
    )
    return {
        "characterId": character_id,
        "bagRemoved": len(removed_bag),
        "storageRemoved": len(removed_storage),
        "spiritRemoved": removed_spirit,
    }


def redemption_codes_list(payload: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Return every persisted redemption code for the GM panel."""
    server = _server()
    codes = server.DEFAULT_HUB.redemption_codes
    return [
        {
            "code": code,
            "rewards": record.get("rewards") or [],
            "maxUses": int(record.get("maxUses", 1)),
            "usedCount": len(record.get("usedBy") or []),
            "usedBy": list(record.get("usedBy") or []),
            "expiresAt": int(record.get("expiresAt", 0)),
            "note": str(record.get("note") or ""),
            "createdAt": int(record.get("createdAt", 0)),
        }
        for code, record in sorted(codes.items())
    ]


def create_redemption_code(payload: dict[str, Any]) -> dict[str, Any]:
    """Generate one in-game redemption code with custom item rewards."""
    server = _server()
    raw_rewards = payload.get("rewards") or []
    if not isinstance(raw_rewards, list) or not raw_rewards:
        raise ValueError("请至少配置一条奖励")
    rewards: list[dict[str, Any]] = []
    for raw in raw_rewards:
        item_id = int(raw.get("itemId") or 0)
        quantity = max(1, int(raw.get("quantity") or 1))
        definition = server.GAME_DATA_CATALOG.get_item_definition(item_id)
        if definition is None:
            raise ValueError(f"奖励道具 {item_id} 不存在")
        stack_limit = server._item_stack_limit(item_id)
        if quantity > stack_limit:
            raise ValueError(
                f"【{definition.name}】单次发放堆叠上限为 {stack_limit}"
            )
        rewards.append(
            {
                "itemId": item_id,
                "name": definition.name,
                "quantity": quantity,
            }
        )
    requested_code = str(payload.get("code") or "").strip().upper()
    code = requested_code or (
        "NTS-"
        + "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(4))
        + "-"
        + "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(4))
        + "-"
        + "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(4))
    )
    with server.DEFAULT_HUB.lock:
        if code in server.DEFAULT_HUB.redemption_codes:
            raise ValueError("该兑换码已存在，请更换")
        server.DEFAULT_HUB.redemption_codes[code] = {
            "rewards": rewards,
            "maxUses": max(1, int(payload.get("maxUses", 1))),
            "usedBy": [],
            "expiresAt": max(0, int(payload.get("expiresAt", 0))),
            "note": str(payload.get("note") or "")[:120],
            "createdAt": int(time.time()),
        }
        server.DEFAULT_HUB.persist_redemption_codes()
    return {
        "code": code,
        "rewards": rewards,
        "maxUses": max(1, int(payload.get("maxUses", 1))),
        "expiresAt": max(0, int(payload.get("expiresAt", 0))),
    }


def delete_redemption_code(payload: dict[str, Any]) -> dict[str, Any]:
    """Remove one redemption code; already-issued rewards are not revoked."""
    server = _server()
    code = str(payload.get("code") or "").strip().upper()
    with server.DEFAULT_HUB.lock:
        if code not in server.DEFAULT_HUB.redemption_codes:
            raise ValueError("兑换码不存在")
        del server.DEFAULT_HUB.redemption_codes[code]
        server.DEFAULT_HUB.persist_redemption_codes()
    return {"code": code}


def get_news(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the current server-side news announcement."""
    server = _server()
    news = dict(server.DEFAULT_HUB.news)
    news["entries"] = list(news.get("entries") or [])
    return news


def update_news(payload: dict[str, Any]) -> dict[str, Any]:
    """Overwrite the news announcement; takes effect when the news tab reopens."""
    server = _server()
    title = str(payload.get("title") or "")
    subtitle = str(payload.get("subtitle") or "")
    raw_entries = payload.get("entries") or []
    if not isinstance(raw_entries, list):
        raise ValueError("新闻条目必须是列表")
    entries = [str(value) for value in raw_entries[:100]]
    with server.DEFAULT_HUB.lock:
        server.DEFAULT_HUB.news = {
            "title": title[:2000],
            "subtitle": subtitle[:500],
            "entries": entries,
        }
        server.DEFAULT_HUB.persist_news()
    return {"ok": True, "entries": len(entries)}


def get_activity_overrides(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the effective (authored + overridden) activity configuration."""
    server = _server()
    catalog = server.GAME_DATA_CATALOG
    hub_overrides = dict(server.DEFAULT_HUB.activity_overrides)
    # effective first-login gifts
    first_login = list(hub_overrides.get("firstLogin") or server.FakeFlashSession.FIRST_LOGIN_GIFTS)
    everyday = {}
    for day in range(1, 8):
        rows = catalog.everyday_gift_rewards(day)
        everyday[str(day)] = [list(row) for row in rows]
    online = [list(row) for row in catalog.monthly_online_rewards()]
    vip = {}
    for level in range(1, 13):
        row = catalog.vip_config(level)
        if row:
            vip[str(level)] = {
                "charge": int(row.get("charge") or 0),
                "name": f"VIP{level}",
            }
    return {
        "firstLogin": first_login,
        "everydayGift": everyday,
        "online": online,
        "vip": vip,
        "overrides": hub_overrides,
    }


def update_activity_overrides(payload: dict[str, Any]) -> dict[str, Any]:
    """Save GM activity overrides; server grants use them immediately."""
    server = _server()
    overrides: dict[str, Any] = {}
    if "firstLogin" in payload:
        rows = []
        for raw in payload["firstLogin"] or []:
            if isinstance(raw, (list, tuple)) and len(raw) >= 3:
                rows.append(
                    [
                        max(1, int(raw[0])),
                        int(raw[1]),
                        max(1, int(raw[2])),
                    ]
                )
        if rows:
            overrides["firstLogin"] = rows
    if "everydayGift" in payload:
        days = {}
        for day in range(1, 8):
            raw_rows = (payload["everydayGift"] or {}).get(str(day)) or []
            rows = []
            for raw in raw_rows:
                if isinstance(raw, (list, tuple)) and len(raw) >= 2:
                    rows.append([int(raw[0]), max(1, int(raw[1]))])
            if rows:
                days[str(day)] = rows
        if days:
            overrides["everydayGift"] = days
    if "online" in payload:
        rows = []
        for raw in payload["online"] or []:
            if isinstance(raw, (list, tuple)) and len(raw) >= 2:
                rows.append([max(1, int(raw[0])), max(0, int(raw[1]))])
        if rows:
            overrides["online"] = rows
    if "vip" in payload:
        vip = {}
        for level, row in (payload["vip"] or {}).items():
            if isinstance(row, dict) and "charge" in row:
                vip[str(level)] = {"charge": max(0, int(row["charge"]))}
        if vip:
            overrides["vip"] = vip
    with server.DEFAULT_HUB.lock:
        server.DEFAULT_HUB.activity_overrides = overrides
        server.DEFAULT_HUB.persist_activity_overrides()
    return {"ok": True, "sections": sorted(overrides.keys())}


def _highest_purple_job_set(server: Any, job: int) -> list[tuple[Any, Any]]:
    """Select the highest authored ordinary equipment for every character slot."""
    job_family = (int(job) // 100) * 100 if int(job) >= 100 else int(job)
    compatible_jobs = {0, int(job), job_family}
    by_position: dict[int, tuple[Any, Any]] = {}
    for raw_item_id in server.GAME_DATA_CATALOG.shop_item_ids_by_section().get(
        "ZhuangBei", ()
    ):
        item_id = int(raw_item_id)
        equipment_type = item_id // 10_000
        if not 101 <= equipment_type <= 112:
            continue
        equipment = server.GAME_DATA_CATALOG.get_equipment_definition(item_id)
        item = server.GAME_DATA_CATALOG.get_item_definition(item_id)
        if (
            equipment is None
            or item is None
            or int(equipment.required_job) not in compatible_jobs
        ):
            continue
        position = int(equipment.position)
        current = by_position.get(position)
        rank = (
            int(equipment.required_level),
            int(equipment.stage),
            int(equipment.required_job) != 0,
            item_id,
        )
        if current is None:
            by_position[position] = (item, equipment)
            continue
        current_item, current_equipment = current
        current_rank = (
            int(current_equipment.required_level),
            int(current_equipment.stage),
            int(current_equipment.required_job) != 0,
            int(current_item.item_id),
        )
        if rank > current_rank:
            by_position[position] = (item, equipment)
    return [by_position[position] for position in sorted(by_position)]


def _apply_purple_equipment_profile(server: Any, item: Any, equipment: Any) -> None:
    """Apply the original purple aptitude and four authored secondary attributes."""
    item.aptitude = 4
    plus_root = server.GAME_DATA_CATALOG.get_equipment_system_config("plus")
    stage_root = plus_root.get(f"stage{equipment.stage}")
    type_root = (
        stage_root.get(equipment.type_name)
        if isinstance(stage_root, dict)
        else None
    )
    if not isinstance(type_root, dict):
        return
    attributes: list[list[int]] = []
    for config in (
        value for _, value in sorted(type_root.items()) if isinstance(value, dict)
    ):
        attribute = server.EQUIPMENT_ATTRIBUTE_BY_NAME.get(
            str(config.get("type") or "").lower()
        )
        if attribute is None:
            continue
        value_from = int(config.get("valueFrom") or 1)
        value_to = max(value_from, int(config.get("valueTo") or value_from))
        rolled_value = value_from + secrets.randbelow(value_to - value_from + 1)
        attributes.append([int(attribute), rolled_value])
        if len(attributes) >= 4:
            break
    item.plus_attributes = attributes


def grant_highest_purple_job_set(payload: dict[str, Any]) -> dict[str, Any]:
    """Grant a complete 12-slot highest-level purple set for the selected job."""
    server = _server()
    character_id = int(payload.get("characterId", 0))
    store, _, session = _store(character_id)
    job = int(store.state.character.job)
    selected = _highest_purple_job_set(server, job)
    if len(selected) != 12:
        raise ValueError(
            f"职业 {job} 的最高级套装数据不完整：应有12件，实际找到{len(selected)}件"
        )

    occupied_slots = {
        int(slot) for slot in store.state.inventory if int(slot) > 0
    }
    free_slots = [
        slot
        for slot in range(1, int(store.state.bag_capacity) + 1)
        if slot not in occupied_slots
    ]
    if len(free_slots) < len(selected):
        raise ValueError(
            f"背包空位不足：需要{len(selected)}格，目前只有{len(free_slots)}格；本次未发放"
        )

    granted: list[Any] = []
    with store.lock:
        for item_definition, equipment in selected:
            if session is not None:
                item, _ = session._add_inventory_item(item_definition.item_id, 1)
            else:
                item = _add_offline_item(
                    server, store, int(item_definition.item_id), 1
                )
            _apply_purple_equipment_profile(server, item, equipment)
            granted.append(item)
        names = [
            server.GAME_DATA_CATALOG.get_item_definition(item.item_id).name
            for item in granted
        ]
        notice_body = (
            f"GM向您发放了职业{job}最高级紫色装备一整套，共{len(granted)}件，"
            "已放入背包。"
        )
        _append_notification_locked(
            store,
            kind="equipment",
            title="GM职业紫装发放",
            body=notice_body,
        )
        store.save()

    if session is not None:
        for item in granted:
            session._send_inventory_add(item, from_drop=0)
    _push_online_notification(session, notice_body)
    return {
        "characterId": character_id,
        "job": job,
        "count": len(granted),
        "aptitude": 4,
        "itemIds": [int(item.item_id) for item in granted],
        "names": names,
    }


def _highest_pet_equipment_set(server: Any) -> list[tuple[Any, Any]]:
    """Select the terminal level-120 item for each of the four real pet gear slots."""
    by_position: dict[int, tuple[Any, Any]] = {}
    for raw_item_id in server.GAME_DATA_CATALOG.raw_equipment:
        item_id = int(raw_item_id)
        if not 201 <= item_id // 10_000 <= 204:
            continue
        equipment = server.GAME_DATA_CATALOG.get_pet_equipment_definition(item_id)
        item = server.GAME_DATA_CATALOG.get_item_definition(item_id)
        if equipment is None or item is None:
            continue
        position = int(equipment.position)
        current = by_position.get(position)
        rank = (int(equipment.required_level), int(equipment.stage), item_id)
        if current is None:
            by_position[position] = (item, equipment)
            continue
        old_item, old_equipment = current
        old_rank = (
            int(old_equipment.required_level),
            int(old_equipment.stage),
            int(old_item.item_id),
        )
        if rank > old_rank:
            by_position[position] = (item, equipment)
    return [by_position[position] for position in sorted(by_position)]


def _maximum_plus_attributes(server: Any, item: Any) -> list[list[int]]:
    """Build the five native refine lines at each authored stage/type maximum."""
    equipment = server.GAME_DATA_CATALOG.get_equipment_definition(item.item_id)
    if equipment is None:
        equipment = server.GAME_DATA_CATALOG.get_pet_equipment_definition(item.item_id)
    if equipment is None:
        return []
    plus_root = server.GAME_DATA_CATALOG.get_equipment_system_config("plus")
    stage_root = plus_root.get(f"stage{equipment.stage}")
    type_root = stage_root.get(equipment.type_name) if isinstance(stage_root, dict) else None
    if not isinstance(type_root, dict):
        return []
    rows: list[list[int]] = []
    used: set[int] = set()
    for config in type_root.values():
        if not isinstance(config, dict):
            continue
        attribute = server.EQUIPMENT_ATTRIBUTE_BY_NAME.get(
            str(config.get("type") or "").lower()
        )
        if attribute is None or int(attribute) in used:
            continue
        rows.append([int(attribute), int(config.get("valueTo") or 0)])
        used.add(int(attribute))
        if len(rows) >= 5:
            break
    return rows


def grant_highest_pet_equipment_set(payload: dict[str, Any]) -> dict[str, Any]:
    """Grant one full four-piece set of the highest authored pet equipment."""
    server = _server()
    character_id = int(payload.get("characterId", 0))
    store, _, session = _store(character_id)
    selected = _highest_pet_equipment_set(server)
    if len(selected) != 4:
        raise ValueError(f"最高级宠物装备数据不完整：应有4件，实际找到{len(selected)}件")
    free_count = sum(
        1
        for slot in range(1, int(store.state.bag_capacity) + 1)
        if slot not in store.state.inventory
    )
    if free_count < 4:
        raise ValueError(f"背包空位不足：需要4格，目前只有{free_count}格；本次未发放")
    granted: list[Any] = []
    with store.lock:
        for item_definition, equipment in selected:
            if session is not None:
                item, _ = session._add_inventory_item(int(item_definition.item_id), 1)
            else:
                item = _add_offline_item(server, store, int(item_definition.item_id), 1)
            item.aptitude = 4
            item.base_attr_type = int(equipment.base_attr_type)
            item.base_attr_value = int(equipment.base_attr_value)
            item.max_endure = int(equipment.max_endure)
            item.endure = int(equipment.max_endure)
            item.plus_attributes = _maximum_plus_attributes(server, item)[:4]
            granted.append(item)
        names = [
            server.GAME_DATA_CATALOG.get_item_definition(item.item_id).name
            for item in granted
        ]
        notice_body = "GM向您发放了120级最高级紫色宠物装备一整套，共4件，已放入背包。"
        _append_notification_locked(
            store, kind="equipment", title="GM最高级宠物装备发放", body=notice_body
        )
        store.save()
    if session is not None:
        for item in granted:
            session._send_inventory_add(item, from_drop=0)
    _push_online_notification(session, notice_body)
    return {
        "characterId": character_id,
        "count": len(granted),
        "level": 120,
        "aptitude": 4,
        "itemIds": [int(item.item_id) for item in granted],
        "names": names,
    }


def apply_max_carve(payload: dict[str, Any]) -> dict[str, Any]:
    """Apply one selected authored maximum secret seal to matching character gear."""
    server = _server()
    character_id = int(payload.get("characterId", 0))
    carve_id = int(payload.get("carveId", 0))
    scope = str(payload.get("scope", "equipped"))
    if scope not in {"equipped", "bag", "all"}:
        raise ValueError("刻印装备范围无效")

    root = server.GAME_DATA_CATALOG.get_equipment_system_config("carve")
    values = root.get("value") if isinstance(root.get("value"), dict) else {}
    carve = values.get(str(carve_id)) if isinstance(values.get(str(carve_id)), dict) else None
    carve_types = {
        "ATK_RATE": server.EquipmentAttribute.ATTACK_RATE,
        "DAMAGE_REFLECT": server.EquipmentAttribute.DAMAGE_REFLECT,
        "IGNORE_DEF": server.EquipmentAttribute.IGNORE_DEFENCE,
        "DAMAGE_REDUCE": server.EquipmentAttribute.DAMAGE_REDUCE,
    }
    if carve is None or str(carve.get("type") or "") not in carve_types:
        raise ValueError("所选刻印不存在")
    carve_type = carve_types[str(carve.get("type"))]
    carve_value = max(1, int(carve.get("value_max") or 1))
    carve_name = str(carve.get("name") or f"秘印{carve_id}")

    store, _, session = _store(character_id)
    targets: list[Any] = []
    for item in store.state.inventory.values():
        equipment_type = int(item.item_id) // 10_000
        if not 101 <= equipment_type <= 112:
            continue
        if scope == "equipped" and item.slot >= 0:
            continue
        if scope == "bag" and item.slot <= 0:
            continue
        targets.append(item)
    if not targets:
        scope_name = {"equipped": "已穿戴", "bag": "背包", "all": "穿戴及背包"}[scope]
        raise ValueError(f"{scope_name}范围内没有可刻印的普通人物装备")

    with store.lock:
        for item in targets:
            item.carve_id = carve_id
            item.carve_type = int(carve_type)
            item.carve_value = carve_value
            if (
                item.can_trade
                and item.item_id not in server.GAME_DATA_CATALOG.never_bind_item_ids()
            ):
                item.can_trade = False
        notice_body = (
            f"GM已为您的{len(targets)}件装备统一打上满级【{carve_name}】"
            f"（效果值{carve_value}）。"
        )
        _append_notification_locked(
            store,
            kind="equipment",
            title="GM满级刻印",
            body=notice_body,
        )
        store.save()

    if session is not None:
        for item in sorted(targets, key=lambda value: value.slot):
            session._refresh_inventory_item(item)
        if any(item.slot < 0 for item in targets):
            session._clamp_resources_after_equipment_change()
            session._send_all_stats()
    _push_online_notification(session, notice_body)
    return {
        "characterId": character_id,
        "carveId": carve_id,
        "name": carve_name,
        "value": carve_value,
        "scope": scope,
        "count": len(targets),
        "slots": [int(item.slot) for item in targets],
    }


def _standard_equipment_targets(store: Any, scope: str) -> list[Any]:
    """Select ordinary 101..112 character gear from one requested container scope."""
    if scope not in {"equipped", "bag", "all"}:
        raise ValueError("装备范围无效")
    targets: list[Any] = []
    for item in store.state.inventory.values():
        if not 101 <= int(item.item_id) // 10_000 <= 112:
            continue
        if scope == "equipped" and item.slot >= 0:
            continue
        if scope == "bag" and item.slot <= 0:
            continue
        targets.append(item)
    return targets


def _collect_equipment_targets(
    store: Any, scope: str, equipment_kind: str = "character"
) -> tuple[list[tuple[Any, Any | None, Any | None]], dict[int, Any]]:
    """Select character (101-112) or pet (201-204) gear, returning (item, definition, pet) tuples."""
    if scope not in {"equipped", "bag", "all"}:
        raise ValueError("装备范围无效")
    if equipment_kind not in {"character", "pet"}:
        raise ValueError("装备类型无效")
    server = _server()
    targets: list[tuple[Any, Any | None, Any | None]] = []
    affected_pets: dict[int, Any] = {}
    if equipment_kind == "character":
        for item in store.state.inventory.values():
            if not 101 <= int(item.item_id) // 10_000 <= 112:
                continue
            if scope == "equipped" and item.slot >= 0:
                continue
            if scope == "bag" and item.slot <= 0:
                continue
            definition = server.GAME_DATA_CATALOG.get_equipment_definition(item.item_id)
            targets.append((item, definition, None))
    else:
        if scope in {"bag", "all"}:
            for item in store.state.inventory.values():
                if not 201 <= int(item.item_id) // 10_000 <= 204:
                    continue
                definition = server.GAME_DATA_CATALOG.get_pet_equipment_definition(item.item_id)
                if definition is None:
                    continue
                targets.append((item, definition, None))
        if scope in {"equipped", "all"}:
            for pet in store.state.pets.values():
                for item in pet.equipment.values():
                    if not 201 <= int(item.item_id) // 10_000 <= 204:
                        continue
                    definition = server.GAME_DATA_CATALOG.get_pet_equipment_definition(item.item_id)
                    if definition is None:
                        continue
                    targets.append((item, definition, pet))
                    affected_pets[int(pet.unique_id)] = pet
    return targets, affected_pets


def _additional_attribute_maximum(
    server: Any, equipment: Any, attribute_type: int
) -> int:
    """Resolve a selected refine line at the target stage's authored maximum.

    Falls back across every authored stage/type so that any equipment part can
    carry any attribute, even when the native data does not list an explicit
    maximum for the requested stage/type combination.
    """
    plus_root = server.GAME_DATA_CATALOG.get_equipment_system_config("plus")
    stage_root = plus_root.get(f"stage{equipment.stage}")
    maximum = 0
    # 1) Direct match: same stage + same equipment type_name
    if isinstance(stage_root, dict):
        direct = stage_root.get(equipment.type_name)
        if isinstance(direct, dict):
            for row in direct.values():
                if not isinstance(row, dict):
                    continue
                attribute = server.EQUIPMENT_ATTRIBUTE_BY_NAME.get(
                    str(row.get("type") or "").lower()
                )
                if attribute is not None and int(attribute) == attribute_type:
                    maximum = max(maximum, int(row.get("valueTo") or 0))
        if maximum <= 0:
            # 2) Same stage, any equipment type_name (weaker fallback)
            for type_root in stage_root.values():
                if not isinstance(type_root, dict):
                    continue
                for row in type_root.values():
                    if not isinstance(row, dict):
                        continue
                    attribute = server.EQUIPMENT_ATTRIBUTE_BY_NAME.get(
                        str(row.get("type") or "").lower()
                    )
                    if attribute is not None and int(attribute) == attribute_type:
                        maximum = max(maximum, int(row.get("valueTo") or 0))
    if maximum <= 0 and isinstance(plus_root, dict):
        # 3) Any stage, any equipment type (strong cross-equipment fallback)
        for any_stage in plus_root.values():
            if not isinstance(any_stage, dict):
                continue
            for type_root in any_stage.values():
                if not isinstance(type_root, dict):
                    continue
                for row in type_root.values():
                    if not isinstance(row, dict):
                        continue
                    attribute = server.EQUIPMENT_ATTRIBUTE_BY_NAME.get(
                        str(row.get("type") or "").lower()
                    )
                    if attribute is not None and int(attribute) == attribute_type:
                        maximum = max(maximum, int(row.get("valueTo") or 0))
    if maximum <= 0:
        # 4) Hard-coded cap so that truly missing authored data never blocks GM
        fallback_by_attribute = {
            int(server.EquipmentAttribute.ATTACK): 3888,
            int(server.EquipmentAttribute.MAX_HP): 42000,
            int(server.EquipmentAttribute.MAX_MP): 21000,
            int(server.EquipmentAttribute.DEFENCE): 2100,
            int(server.EquipmentAttribute.CRITICAL): 60,
            int(server.EquipmentAttribute.EVASION): 150,
            int(server.EquipmentAttribute.ATTACK_SPEED): 18,
            int(server.EquipmentAttribute.WALK_SPEED): 120,
            int(server.EquipmentAttribute.ATTACK_RATE): 30,
            int(server.EquipmentAttribute.DAMAGE_REDUCE): 30,
            int(server.EquipmentAttribute.DAMAGE_REFLECT): 30,
            int(server.EquipmentAttribute.IGNORE_DEFENCE): 25,
        }
        maximum = int(fallback_by_attribute.get(int(attribute_type), 1000))
    return maximum


def customize_additional_attributes(payload: dict[str, Any]) -> dict[str, Any]:
    """Replace up to ten refine lines on a selected character or pet gear part."""
    server = _server()
    character_id = int(payload.get("characterId", 0))
    equipment_kind = str(payload.get("equipmentKind", "character"))
    scope = str(payload.get("scope", "equipped"))
    position = int(payload.get("position", 0))
    raw_types = payload.get("attributeTypes", [])
    if equipment_kind not in {"character", "pet"}:
        raise ValueError("装备类型必须是人物装备或宠物装备")
    if scope not in {"equipped", "bag", "all"}:
        raise ValueError("附加属性处理范围无效")
    if not isinstance(raw_types, list):
        raise ValueError("附加属性选择格式无效")
    attribute_types = [int(value) for value in raw_types]
    valid_rows = {int(row["type"]): row for row in additional_attribute_rows()}
    if not 1 <= len(attribute_types) <= 10:
        raise ValueError("附加属性必须选择1至10条")
    if any(value not in valid_rows for value in attribute_types):
        raise ValueError("包含游戏不支持的附加属性")

    store, _, session = _store(character_id)
    targets: list[tuple[Any, Any, Any | None]] = []
    affected_pets: dict[int, Any] = {}
    if equipment_kind == "character":
        for item in store.state.inventory.values():
            definition = server.GAME_DATA_CATALOG.get_equipment_definition(item.item_id)
            if definition is None or not 101 <= int(item.item_id) // 10_000 <= 112:
                continue
            if position and int(definition.position) != position:
                continue
            if scope == "equipped" and item.slot >= 0:
                continue
            if scope == "bag" and item.slot <= 0:
                continue
            targets.append((item, definition, None))
    else:
        if scope in {"bag", "all"}:
            for item in store.state.inventory.values():
                definition = server.GAME_DATA_CATALOG.get_pet_equipment_definition(item.item_id)
                if definition is None or not 201 <= int(item.item_id) // 10_000 <= 204:
                    continue
                if position and int(definition.position) != position:
                    continue
                targets.append((item, definition, None))
        if scope in {"equipped", "all"}:
            for pet in store.state.pets.values():
                for item in pet.equipment.values():
                    definition = server.GAME_DATA_CATALOG.get_pet_equipment_definition(item.item_id)
                    if definition is None or not 201 <= int(item.item_id) // 10_000 <= 204:
                        continue
                    if position and int(definition.position) != position:
                        continue
                    targets.append((item, definition, pet))
                    affected_pets[int(pet.unique_id)] = pet
    if not targets:
        raise ValueError("所选部位和范围内没有可修改的装备")

    maximum_ratio = int(server.FakeFlashSession.MAX_FORGE_ADDITIVE_RATIO)
    with store.lock:
        for item, definition, _pet in targets:
            values = [
                [attribute_type, _additional_attribute_maximum(server, definition, attribute_type)]
                for attribute_type in attribute_types
            ]
            item.plus_attributes = values
            item.forge_additive_ratio = maximum_ratio
        names = "、".join(str(valid_rows[value]["name"]) for value in attribute_types)
        kind_name = "人物" if equipment_kind == "character" else "宠物"
        notice_body = (
            f"GM已为您的{len(targets)}件{kind_name}装备设置{len(attribute_types)}条"
            f"满值附加属性：{names}。"
        )
        _append_notification_locked(
            store, kind="equipment", title="GM自定义装备附加属性", body=notice_body
        )
        store.save()
    if session is not None:
        embedded_ids = {
            id(item)
            for item, _definition, pet in targets
            if pet is not None
        }
        for item, _definition, _pet in targets:
            if id(item) not in embedded_ids:
                session._refresh_inventory_item(item)
        for pet in affected_pets.values():
            session.send_text(
                f"{server.op(server.ServerOpcode.PET_ACTION)}:"
                f"{server.op(server.PetResponseAction.UPDATE)}:"
                f"{session._encode_pet_update(pet)}"
            )
        if equipment_kind == "character" and any(item.slot < 0 for item, _, _ in targets):
            session._clamp_resources_after_equipment_change()
            session._send_all_stats()
    _push_online_notification(session, notice_body)
    return {
        "characterId": character_id,
        "equipmentKind": equipment_kind,
        "scope": scope,
        "position": position,
        "equipmentCount": len(targets),
        "lineCount": len(attribute_types),
        "attributeTypes": attribute_types,
        "attributeNames": [valid_rows[value]["name"] for value in attribute_types],
        "petCount": len(affected_pets),
    }


def set_equipment_special_attributes(payload: dict[str, Any]) -> dict[str, Any]:
    """GM一键设置装备【洗练特性属性】——对应装备打造面板「特性属性」标签页。

    槽位结构：
        普通装备：3 条百分比槽（值 1~5，满值 5） + 3 条固定值槽（最大 = 100 + stage × 150）
        变身卡 ：4 条百分比槽                          + 3 条固定值槽
        附带字段：special_level（总条数）、special_effect_type（1~3 对应守鹤之力等）、
                  afterimage_flag（0/1 残影/buff 开关）

    三种模式：
        ``max_all`` —— 一键满属性：百分比槽填6种通用属性的前3种（HP/MP/攻击），满值5%；
                       固定值槽填（HP/攻击/防御）满值；特殊效果默认拉满（type=3 / flag=1）。
                       也允许从前端自选 percentAttrs/fixedAttrs/effectType/flag 覆盖默认。
        ``unify``   —— 统一一个属性：用户选一个通用属性（HP/MP/攻击/防御/暴击/闪避之一），
                       所有百分比槽和固定值槽全部填这个属性，数值拉满。
        ``single``  —— 自选单个属性（可选 percent 或 fixed 或 all），把对应槽全部填满
                       该属性，其余槽保持默认最大6条组合（或者一起填也行）。
    """
    server = _server()
    character_id = int(payload.get("characterId", 0))
    equipment_kind = str(payload.get("equipmentKind", "character"))
    scope = str(payload.get("scope", "equipped"))
    position = int(payload.get("position", 0))
    mode = str(payload.get("mode", "max_all"))
    if mode not in {"max_all", "unify", "single"}:
        raise ValueError("特性属性操作模式无效")
    if equipment_kind not in {"character", "pet"}:
        raise ValueError("装备类型必须是人物装备或宠物装备")
    if scope not in {"equipped", "bag", "all"}:
        raise ValueError("特性属性处理范围无效")

    attrs = server.EquipmentAttribute
    percent_types = {int(row["type"]) for row in special_percent_rows()}
    fixed_types = {int(row["type"]) for row in special_fixed_rows()}
    common_types = sorted(percent_types & fixed_types)  # HP/MP/攻击/防御/暴击/闪避 6种
    if mode in {"unify", "single"}:
        attribute_type = int(payload.get("attributeType", 0))
        if mode == "unify" and attribute_type not in common_types:
            raise ValueError("「统一属性」必须选择HP/MP/攻击/防御/暴击/闪避之一")
        if mode == "single" and attribute_type not in percent_types:
            raise ValueError("请选择有效的特性属性")
        slot_mode = str(payload.get("slotMode", "all"))  # percent/fixed/all
    # max_all 允许从前端覆盖默认属性组合
    percent_attrs_payload = payload.get("percentAttrs")  # list[int] | None
    fixed_attrs_payload = payload.get("fixedAttrs")
    effect_type = int(payload.get("effectType", 3))
    effect_type = max(1, min(3, effect_type)) if effect_type > 0 else 3
    flag = int(payload.get("afterimageFlag", 1))
    flag = 1 if flag > 0 else 0

    store, _, session = _store(character_id)
    targets: list[tuple[Any, Any, Any | None]] = []
    affected_pets: dict[int, Any] = {}

    def _is_transform_card(item: Any) -> bool:
        return int(item.item_id) // 10000 in {121, 205}

    def _definition(item: Any, is_tc: bool):
        if is_tc:
            return None
        return (
            server.GAME_DATA_CATALOG.get_equipment_definition(item.item_id)
            or server.GAME_DATA_CATALOG.get_pet_equipment_definition(item.item_id)
        )

    if equipment_kind == "character":
        for item in store.state.inventory.values():
            item_family = int(item.item_id) // 10000
            is_tc = _is_transform_card(item)
            definition = _definition(item, is_tc)
            if not is_tc:
                if definition is None or not 101 <= item_family <= 112:
                    continue
                if int(getattr(item, "aptitude", 0)) < 5:
                    continue  # 只有橙装/紫装进化以上 才能洗练特性
                if position and int(definition.position) != position:
                    continue
            else:
                if not is_tc:
                    continue
            if scope == "equipped" and item.slot >= 0:
                continue
            if scope == "bag" and item.slot <= 0:
                continue
            targets.append((item, definition, None))
    else:
        if scope in {"bag", "all"}:
            for item in store.state.inventory.values():
                if not 201 <= int(item.item_id) // 10000 <= 204:
                    continue
                definition = server.GAME_DATA_CATALOG.get_pet_equipment_definition(item.item_id)
                if definition is None or int(getattr(item, "aptitude", 0)) < 5:
                    continue
                if position and int(definition.position) != position:
                    continue
                targets.append((item, definition, None))
        if scope in {"equipped", "all"}:
            for pet in store.state.pets.values():
                for item in pet.equipment.values():
                    if not 201 <= int(item.item_id) // 10000 <= 204:
                        continue
                    definition = server.GAME_DATA_CATALOG.get_pet_equipment_definition(item.item_id)
                    if definition is None or int(getattr(item, "aptitude", 0)) < 5:
                        continue
                    if position and int(definition.position) != position:
                        continue
                    targets.append((item, definition, pet))
                    affected_pets[int(pet.unique_id)] = pet
    if not targets:
        raise ValueError("所选范围内没有可打特性属性的橙装/变身卡（需要橙色及以上品质）")

    def _build_attributes(definition: Any, is_transform_card: bool) -> tuple[list[list[int]], int]:
        """Return (special_attributes, stage) for the given equipment definition."""
        percent_count = 4 if is_transform_card else 3
        fixed_count = 3
        if definition is not None:
            stage = max(1, int(getattr(definition, "stage", 1)) or 1)
        else:
            stage = 8  # 变身卡默认阶段
        fixed_max = 100 + stage * 150

        def _fill_percent(types: list[int]) -> list[list[int]]:
            """Fill percent slots by cycling through `types`."""
            result: list[list[int]] = []
            i = 0
            while len(result) < percent_count:
                attribute_type = int(types[i % len(types)])
                result.append([attribute_type, 5])
                i += 1
            return result

        def _fill_fixed(types: list[int]) -> list[list[int]]:
            result: list[list[int]] = []
            i = 0
            while len(result) < fixed_count:
                attribute_type = int(types[i % len(types)])
                result.append([attribute_type, fixed_max])
                i += 1
            return result

        if mode == "max_all":
            if percent_attrs_payload and isinstance(percent_attrs_payload, list) and percent_attrs_payload:
                p_types = [int(v) for v in percent_attrs_payload if int(v) in percent_types] or common_types
            else:
                p_types = [int(attrs.ATTACK), int(attrs.ATTACK_SPEED), int(attrs.MAX_MP)]
            if fixed_attrs_payload and isinstance(fixed_attrs_payload, list) and fixed_attrs_payload:
                f_types = [int(v) for v in fixed_attrs_payload if int(v) in fixed_types] or common_types
            else:
                f_types = [int(attrs.ATTACK), int(attrs.DEFENCE), int(attrs.MAX_HP)]
            return _fill_percent(p_types) + _fill_fixed(f_types), stage

        if mode == "unify":
            # unify attribute_type 必须在 common_types，两边都可以打
            return (
                _fill_percent([attribute_type]) + _fill_fixed([attribute_type]),
                stage,
            )

        # mode == "single"
        percent_list: list[list[int]] = []
        fixed_list: list[list[int]] = []
        if slot_mode in {"percent", "all"} and attribute_type in percent_types:
            percent_list = _fill_percent([attribute_type])
        if slot_mode in {"fixed", "all"} and attribute_type in fixed_types:
            fixed_list = _fill_fixed([attribute_type])
        if slot_mode == "all" and attribute_type not in fixed_types:
            # 属性不在 fixed 里（攻击速度/移动速度/攻击率），fixed 补成默认组合
            fixed_list = _fill_fixed(common_types[:3])
        if slot_mode == "all" and attribute_type not in percent_types:
            percent_list = _fill_percent(common_types[:3])
        if not percent_list and not fixed_list:
            return [], stage
        return percent_list + fixed_list, stage

    with store.lock:
        equipment_stats: list[dict[str, Any]] = []
        for item, definition, _pet in targets:
            is_tc = _is_transform_card(item)
            attr_list, _stage = _build_attributes(definition, is_tc)
            if not attr_list:
                continue
            item.special_attributes = [list(row) for row in attr_list]
            item.special_level = len(attr_list)
            item.special_effect_type = effect_type
            item.afterimage_flag = flag
            # 统计输出
            counter: dict[int, int] = {}
            for attribute_type, _v in attr_list:
                counter[int(attribute_type)] = counter.get(int(attribute_type), 0) + 1
            equipment_stats.append(
                {
                    "itemId": int(item.item_id),
                    "count": len(attr_list),
                    "types": counter,
                }
            )
        if not equipment_stats:
            return {
                "characterId": character_id,
                "equipmentCount": 0,
                "types": [],
            }
        # 通知内容（汇总）
        total_counter: dict[int, int] = {}
        for stat in equipment_stats:
            for t, c in stat["types"].items():
                total_counter[t] = total_counter.get(t, 0) + c
        all_percent_names = {int(r["type"]): r["name"] for r in special_percent_rows()}
        all_fixed_names = {int(r["type"]): r["name"] for r in special_fixed_rows()}
        name_map = dict(all_percent_names)
        name_map.update(all_fixed_names)
        summary_parts = [
            f"{name_map.get(t, f'类型{t}')}×{c}"
            for t, c in sorted(total_counter.items())
        ]
        kind_name = "人物" if equipment_kind == "character" else "宠物"
        mode_names = {"max_all": "一键满属性", "unify": "统一属性", "single": "自选属性"}
        notice_body = (
            f"GM已为您的{len(equipment_stats)}件{kind_name}橙装/变身卡执行特性属性"
            f"【{mode_names[mode]}】，共 {sum(s['count'] for s in equipment_stats)} 条："
            f"{'，'.join(summary_parts)}；特效Type={effect_type}，残影Flag={'开' if flag else '关'}。"
        )
        _append_notification_locked(
            store, kind="equipment", title="GM一键装备特性属性", body=notice_body
        )
        store.save()
    if session is not None:
        embedded_ids = {id(item) for item, _definition, pet in targets if pet is not None}
        for item, _definition, _pet in targets:
            if id(item) not in embedded_ids:
                session._refresh_inventory_item(item)
        for pet in affected_pets.values():
            session.send_text(
                f"{server.op(server.ServerOpcode.PET_ACTION)}:"
                f"{server.op(server.PetResponseAction.UPDATE)}:"
                f"{session._encode_pet_update(pet)}"
            )
        if equipment_kind == "character" and any(item.slot < 0 for item, _, _ in targets):
            session._clamp_resources_after_equipment_change()
            session._send_all_stats()
    _push_online_notification(session, notice_body)
    type_summary = [
        {
            "type": t,
            "name": name_map.get(t, f"类型{t}"),
            "count": c,
        }
        for t, c in sorted(total_counter.items())
    ]
    return {
        "characterId": character_id,
        "equipmentKind": equipment_kind,
        "scope": scope,
        "position": position,
        "mode": mode,
        "equipmentCount": len(equipment_stats),
        "lineCount": sum(s["count"] for s in equipment_stats),
        "types": type_summary,
        "effectType": effect_type,
        "afterimageFlag": flag,
    }


def max_strength_equipment(payload: dict[str, Any]) -> dict[str, Any]:
    """Set matching ordinary equipment to the authored maximum strength level 11."""
    server = _server()
    character_id = int(payload.get("characterId", 0))
    equipment_kind = str(payload.get("equipmentKind", "character"))
    scope = str(payload.get("scope", "equipped"))
    store, _, session = _store(character_id)
    targets, affected_pets = _collect_equipment_targets(store, scope, equipment_kind)
    if not targets:
        raise ValueError("所选范围内没有可强化的普通人物装备")
    items = [t[0] for t in targets]
    config = server.GAME_DATA_CATALOG.get_equipment_system_config("strength").get(
        "level11"
    )
    if not isinstance(config, dict):
        raise ValueError("游戏数据中缺少11级强化配置")
    ratio = int(config.get("addRatio") or 0)
    with store.lock:
        for item, definition, _pet in targets:
            if definition is None:
                continue
            item.base_attr_type = int(definition.base_attr_type)
            item.base_attr_value = int(definition.base_attr_value)
            item.strength_level = 11
            item.strength_value = round(item.base_attr_value * ratio / 100)
            item.strength_info = [True] * 11
            item.crash_time = 0
        notice_body = f"GM已将您的{len(items)}件装备强化至最高11级。"
        _append_notification_locked(
            store,
            kind="equipment",
            title="GM装备满强化",
            body=notice_body,
        )
        store.save()
    if session is not None:
        embedded_ids = {id(item) for item, _definition, pet in targets if pet is not None}
        for item in sorted(items, key=lambda value: value.slot):
            if id(item) not in embedded_ids:
                session._refresh_inventory_item(item)
        for pet in affected_pets.values():
            session.send_text(
                f"{server.op(server.ServerOpcode.PET_ACTION)}:"
                f"{server.op(server.PetResponseAction.UPDATE)}:"
                f"{session._encode_pet_update(pet)}"
            )
        if equipment_kind == "character" and any(item.slot < 0 for item in items):
            session._clamp_resources_after_equipment_change()
            session._send_all_stats()
    _push_online_notification(session, notice_body)
    return {
        "characterId": character_id,
        "equipmentKind": equipment_kind,
        "scope": scope,
        "equipmentCount": len(items),
        "strengthLevel": 11,
        "strengthRatio": ratio,
    }


def max_inlay_equipment(payload: dict[str, Any]) -> dict[str, Any]:
    """Replace every socket on compatible gear with one selected maximum-level gem."""
    server = _server()
    character_id = int(payload.get("characterId", 0))
    equipment_kind = str(payload.get("equipmentKind", "character"))
    gem_item_id = int(payload.get("gemItemId", 0))
    scope = str(payload.get("scope", "equipped"))
    gem = server.GAME_DATA_CATALOG.get_stone_inlay(gem_item_id)
    progression = server.GAME_DATA_CATALOG.get_stone_progression(gem_item_id)
    definition = server.GAME_DATA_CATALOG.get_item_definition(gem_item_id)
    valid_ids = {int(row["itemId"]) for row in max_gem_rows()}
    if (
        gem_item_id not in valid_ids
        or gem is None
        or progression is None
        or definition is None
    ):
        raise ValueError("所选宝石不是可用的满级普通宝石")
    stone_level, attribute_type, skill_id, attribute_value = gem
    attribute_names = {
        int(value): name for name, value in server.EQUIPMENT_ATTRIBUTE_BY_NAME.items()
    }
    attribute_name = attribute_names.get(int(attribute_type), "")

    store, _, session = _store(character_id)
    candidates, affected_pets = _collect_equipment_targets(store, scope, equipment_kind)
    if not candidates:
        raise ValueError("所选范围内没有可操作的装备")
    inlay_config = server.GAME_DATA_CATALOG.get_equipment_system_config("inlay")
    targets: list[tuple[Any, Any, Any | None]] = []
    for item, equipment, pet in candidates:
        allowed = (
            str(inlay_config.get(equipment.type_name) or "").split(";")
            if equipment is not None
            else []
        )
        if attribute_name in allowed:
            targets.append((item, equipment, pet))
    if not targets:
        raise ValueError("所选范围内没有能够镶嵌该类宝石的装备")
    items = [t[0] for t in targets]

    total_sockets = 0
    with store.lock:
        for item, _definition, _pet in targets:
            socket_count = 6 if int(item.aptitude) >= 5 else 5
            item.inlays = [
                [
                    gem_item_id,
                    int(stone_level),
                    int(attribute_type),
                    int(skill_id),
                    int(attribute_value),
                ]
                for _ in range(socket_count)
            ]
            total_sockets += socket_count
            if (
                item.can_trade
                and gem_item_id in server.GAME_DATA_CATALOG.bind_item_ids()
                and item.item_id not in server.GAME_DATA_CATALOG.never_bind_item_ids()
            ):
                item.can_trade = False
        notice_body = (
            f"GM已为您的{len(items)}件兼容装备镶满【{definition.name}】，"
            f"共{total_sockets}个孔。"
        )
        _append_notification_locked(
            store,
            kind="equipment",
            title="GM装备满镶嵌",
            body=notice_body,
        )
        store.save()
    if session is not None:
        embedded_ids = {id(item) for item, _definition, pet in targets if pet is not None}
        for item in sorted(items, key=lambda value: value.slot):
            if id(item) not in embedded_ids:
                session._refresh_inventory_item(item)
        for pet in affected_pets.values():
            session.send_text(
                f"{server.op(server.ServerOpcode.PET_ACTION)}:"
                f"{server.op(server.PetResponseAction.UPDATE)}:"
                f"{session._encode_pet_update(pet)}"
            )
        if equipment_kind == "character" and any(item.slot < 0 for item in items):
            session._clamp_resources_after_equipment_change()
            session._send_all_stats()
    _push_online_notification(session, notice_body)
    return {
        "characterId": character_id,
        "equipmentKind": equipment_kind,
        "scope": scope,
        "gemItemId": gem_item_id,
        "gemName": definition.name,
        "gemLevel": int(stone_level),
        "equipmentCount": len(items),
        "socketCount": total_sockets,
    }


def max_born_equipment(payload: dict[str, Any]) -> dict[str, Any]:
    """Set the native born bonus to its real maximum: 100% of base attribute."""
    server = _server()
    character_id = int(payload.get("characterId", 0))
    equipment_kind = str(payload.get("equipmentKind", "character"))
    scope = str(payload.get("scope", "equipped"))
    store, _, session = _store(character_id)
    targets, affected_pets = _collect_equipment_targets(store, scope, equipment_kind)
    if not targets:
        raise ValueError("所选范围内没有可打造天生属性的普通人物装备")
    items = [t[0] for t in targets]
    with store.lock:
        for item, definition, _pet in targets:
            if definition is None:
                continue
            item.base_attr_type = int(definition.base_attr_type)
            item.base_attr_value = int(definition.base_attr_value)
            item.born_attr_value = int(item.base_attr_value)
        notice_body = f"GM已将您的{len(items)}件装备天生属性提升至满值100%。"
        _append_notification_locked(
            store,
            kind="equipment",
            title="GM装备满天生",
            body=notice_body,
        )
        store.save()
    if session is not None:
        embedded_ids = {id(item) for item, _definition, pet in targets if pet is not None}
        for item in sorted(items, key=lambda value: value.slot):
            if id(item) not in embedded_ids:
                session._refresh_inventory_item(item)
        for pet in affected_pets.values():
            session.send_text(
                f"{server.op(server.ServerOpcode.PET_ACTION)}:"
                f"{server.op(server.PetResponseAction.UPDATE)}:"
                f"{session._encode_pet_update(pet)}"
            )
        if equipment_kind == "character" and any(item.slot < 0 for item in items):
            session._clamp_resources_after_equipment_change()
            session._send_all_stats()
    _push_online_notification(session, notice_body)
    return {
        "characterId": character_id,
        "equipmentKind": equipment_kind,
        "scope": scope,
        "equipmentCount": len(items),
        "bornPercent": 100,
    }


def max_myth_attributes_equipment(payload: dict[str, Any]) -> dict[str, Any]:
    """Replace myth lines with one to ten user-selected perfect maximum lines.

    Supports repeating the same attribute type multiple times, plus optional
    per-slot custom values so the GM can override the native 18% perfect cap
    per individual line when needed.
    """
    server = _server()
    character_id = int(payload.get("characterId", 0))
    equipment_kind = str(payload.get("equipmentKind", "character"))
    scope = str(payload.get("scope", "equipped"))
    raw_types = payload.get("attributeTypes", [])
    raw_values = payload.get("attributeValues", None)
    if not isinstance(raw_types, list):
        raise ValueError("梦幻属性选择格式无效")
    if raw_values is not None and not isinstance(raw_values, list):
        raise ValueError("梦幻属性自定义数值格式无效")
    attribute_types = [int(value) for value in raw_types]
    valid_rows = {int(row["type"]): row for row in myth_attribute_rows()}
    default_cap = max(int(row["maxValue"]) for row in valid_rows.values()) if valid_rows else 18
    if not 1 <= len(attribute_types) <= 10:
        raise ValueError("梦幻属性必须选择1至10条")
    if any(value not in valid_rows for value in attribute_types):
        raise ValueError("包含游戏不支持的梦幻属性")

    # Optional per-slot custom values: fall back to default_cap when missing.
    if raw_values is None:
        attribute_values = [int(valid_rows[value]["maxValue"]) for value in attribute_types]
    else:
        attribute_values = []
        for index, attribute_type in enumerate(attribute_types):
            if index < len(raw_values) and raw_values[index] is not None:
                v = int(raw_values[index])
                if v <= 0:
                    v = int(valid_rows[attribute_type]["maxValue"])
            else:
                v = int(valid_rows[attribute_type]["maxValue"])
            attribute_values.append(v)

    store, _, session = _store(character_id)
    targets, affected_pets = _collect_equipment_targets(store, scope, equipment_kind)
    if not targets:
        raise ValueError("所选范围内没有可设置梦幻属性的普通人物装备")
    items = [t[0] for t in targets]
    forge_length = len(attribute_types)
    # Keep forge_type / forge_count inside the client's int32 / 5-slot protocol
    # so that myth results can still be packed and rendered correctly even when
    # the GM writes more than 5 custom lines.  The client receives the full
    # list of lines via the `myth_attributes` field (a `@` separated string)
    # and `myth_forge_count` is only used to gate the native forge UI.
    if forge_length >= 5:
        perfect_forge_type = 33333
        forge_count_for_client = 5
    else:
        perfect_forge_type = int("3" * forge_length) if forge_length > 0 else 0
        forge_count_for_client = forge_length
    with store.lock:
        for item, _definition, _pet in targets:
            item.myth_attributes = [
                [attribute_type, attribute_values[index]]
                for index, attribute_type in enumerate(attribute_types)
            ]
            item.myth_forge_count = forge_count_for_client
            item.myth_forge_type = perfect_forge_type
            item.myth_wuxing = int(item.myth_wuxing) if 1 <= int(item.myth_wuxing) <= 5 else 1
            item.myth_wuxing_level = 3
            if item.myth_skill_id > 0:
                item.myth_skill_level = 9
        names = "、".join(
            f"{valid_rows[attribute_type]['name']} +{attribute_values[i]}%"
            for i, attribute_type in enumerate(attribute_types)
        )
        notice_body = (
            f"GM已将您的{len(items)}件装备梦幻词条设为自定义满值：{names}。"
        )
        _append_notification_locked(
            store,
            kind="equipment",
            title="GM装备满梦幻属性",
            body=notice_body,
        )
        store.save()
    if session is not None:
        embedded_ids = {id(item) for item, _definition, pet in targets if pet is not None}
        for item in sorted(items, key=lambda value: value.slot):
            if id(item) not in embedded_ids:
                session._refresh_inventory_item(item)
        for pet in affected_pets.values():
            session.send_text(
                f"{server.op(server.ServerOpcode.PET_ACTION)}:"
                f"{server.op(server.PetResponseAction.UPDATE)}:"
                f"{session._encode_pet_update(pet)}"
            )
        if equipment_kind == "character" and any(item.slot < 0 for item in items):
            session._clamp_resources_after_equipment_change()
            session._send_all_stats()
    _push_online_notification(session, notice_body)
    return {
        "characterId": character_id,
        "equipmentKind": equipment_kind,
        "scope": scope,
        "equipmentCount": len(items),
        "attributeTypes": attribute_types,
        "attributeValues": attribute_values,
        "attributeNames": [
            f"{valid_rows[value]['name']} +{attribute_values[i]}%"
            for i, value in enumerate(attribute_types)
        ],
        "lineCount": forge_length,
        "maxValue": default_cap,
    }


def max_refine_equipment(payload: dict[str, Any]) -> dict[str, Any]:
    """Max every authored refine line and the native additive refine ratio."""
    server = _server()
    character_id = int(payload.get("characterId", 0))
    equipment_kind = str(payload.get("equipmentKind", "character"))
    scope = str(payload.get("scope", "equipped"))
    store, _, session = _store(character_id)
    candidates, affected_pets = _collect_equipment_targets(store, scope, equipment_kind)
    if not candidates:
        raise ValueError("所选范围内没有可操作的装备")
    targets = [t for t in candidates if _maximum_plus_attributes(server, t[0])]
    if not targets:
        raise ValueError("所选范围内没有可精炼的普通人物装备")
    items = [t[0] for t in targets]
    maximum_ratio = int(server.FakeFlashSession.MAX_FORGE_ADDITIVE_RATIO)
    with store.lock:
        for item, _definition, _pet in targets:
            item.plus_attributes = _maximum_plus_attributes(server, item)
            item.forge_additive_ratio = maximum_ratio
        notice_body = (
            f"GM已将您的{len(items)}件装备精炼词条及精炼附加比例提升至满值"
            f"（附加{maximum_ratio}%）。"
        )
        _append_notification_locked(store, kind="equipment", title="GM装备满精炼", body=notice_body)
        store.save()
    if session is not None:
        embedded_ids = {id(item) for item, _definition, pet in targets if pet is not None}
        for item in sorted(items, key=lambda value: value.slot):
            if id(item) not in embedded_ids:
                session._refresh_inventory_item(item)
        for pet in affected_pets.values():
            session.send_text(
                f"{server.op(server.ServerOpcode.PET_ACTION)}:"
                f"{server.op(server.PetResponseAction.UPDATE)}:"
                f"{session._encode_pet_update(pet)}"
            )
        if equipment_kind == "character" and any(item.slot < 0 for item in items):
            session._clamp_resources_after_equipment_change()
            session._send_all_stats()
    _push_online_notification(session, notice_body)
    return {
        "characterId": character_id,
        "equipmentKind": equipment_kind,
        "scope": scope,
        "equipmentCount": len(items),
        "additiveRatio": maximum_ratio,
    }


def evolve_legendary_equipment(payload: dict[str, Any]) -> dict[str, Any]:
    """Promote ordinary gear to aptitude 5 and give it a maximum native tale line."""
    server = _server()
    character_id = int(payload.get("characterId", 0))
    equipment_kind = str(payload.get("equipmentKind", "character"))
    scope = str(payload.get("scope", "equipped"))
    store, _, session = _store(character_id)
    targets, affected_pets = _collect_equipment_targets(store, scope, equipment_kind)
    if not targets:
        raise ValueError("所选范围内没有可进化的普通人物装备")
    items = [t[0] for t in targets]
    tale_rows = server.GAME_DATA_CATALOG.get_equipment_system_config("taleAttributes")
    maximum_by_type: dict[int, int] = {}
    for row in tale_rows.values():
        if not isinstance(row, dict):
            continue
        attribute = server.EQUIPMENT_ATTRIBUTE_BY_NAME.get(str(row.get("type") or "").lower())
        if attribute is not None:
            maximum_by_type[int(attribute)] = int(row.get("valueTo") or 0)
    with store.lock:
        for item, _definition, _pet in targets:
            item.aptitude = 5
            if not item.plus_attributes:
                item.plus_attributes = _maximum_plus_attributes(server, item)
            tale_type = int(item.base_attr_type)
            if tale_type not in maximum_by_type:
                tale_type = int(server.EquipmentAttribute.MAX_HP)
            item.tale_attr_type = tale_type
            item.tale_attr_value = maximum_by_type.get(tale_type, 0)
        notice_body = f"GM已将您的{len(items)}件装备进化为传说品质，并赋予满值传说属性。"
        _append_notification_locked(store, kind="equipment", title="GM装备进化传说", body=notice_body)
        store.save()
    if session is not None:
        embedded_ids = {id(item) for item, _definition, pet in targets if pet is not None}
        for item in sorted(items, key=lambda value: value.slot):
            if id(item) not in embedded_ids:
                session._refresh_inventory_item(item)
        for pet in affected_pets.values():
            session.send_text(
                f"{server.op(server.ServerOpcode.PET_ACTION)}:"
                f"{server.op(server.PetResponseAction.UPDATE)}:"
                f"{session._encode_pet_update(pet)}"
            )
        if equipment_kind == "character" and any(item.slot < 0 for item in items):
            session._clamp_resources_after_equipment_change()
            session._send_all_stats()
    _push_online_notification(session, notice_body)
    return {
        "characterId": character_id,
        "equipmentKind": equipment_kind,
        "scope": scope,
        "equipmentCount": len(items),
        "aptitude": 5,
    }


def customize_tale_attribute(payload: dict[str, Any]) -> dict[str, Any]:
    """Set one selected legendary core line to maximum on one equipment part."""
    server = _server()
    character_id = int(payload.get("characterId", 0))
    scope = str(payload.get("scope", "equipped"))
    position = int(payload.get("position", 0))
    attribute_type = int(payload.get("attributeType", 0))
    rows = {int(row["type"]): row for row in tale_attribute_rows()}
    selected = rows.get(attribute_type)
    if selected is None:
        raise ValueError("所选传说属性不存在")
    store, _, session = _store(character_id)
    candidates = _standard_equipment_targets(store, scope)
    targets: list[Any] = []
    for item in candidates:
        definition = server.GAME_DATA_CATALOG.get_equipment_definition(item.item_id)
        if definition is None or (position and int(definition.position) != position):
            continue
        if int(item.aptitude) < 5:
            continue
        targets.append(item)
    if not targets:
        raise ValueError("所选部位和范围内没有传说品质装备")
    with store.lock:
        for item in targets:
            item.tale_attr_type = attribute_type
            item.tale_attr_value = int(selected["maxValue"])
        notice_body = (
            f"GM已将您的{len(targets)}件传说装备属性修改为满值"
            f"【{selected['name']} +{selected['maxValue']}】。"
        )
        _append_notification_locked(
            store, kind="equipment", title="GM传说装备属性修改", body=notice_body
        )
        store.save()
    if session is not None:
        for item in sorted(targets, key=lambda value: value.slot):
            session._refresh_inventory_item(item)
        if any(item.slot < 0 for item in targets):
            session._clamp_resources_after_equipment_change()
            session._send_all_stats()
    _push_online_notification(session, notice_body)
    return {
        "characterId": character_id,
        "scope": scope,
        "position": position,
        "equipmentCount": len(targets),
        "attributeType": attribute_type,
        "attributeName": selected["name"],
        "value": int(selected["maxValue"]),
    }


def max_myth_forge_equipment(payload: dict[str, Any]) -> dict[str, Any]:
    """Complete all five perfect myth steps while preserving selected myth lines."""
    server = _server()
    character_id = int(payload.get("characterId", 0))
    equipment_kind = str(payload.get("equipmentKind", "character"))
    scope = str(payload.get("scope", "equipped"))
    position = int(payload.get("position", 0))
    myth_skill_id = int(payload.get("mythSkillId", 101))
    valid_skills = {int(row["skillId"]): row for row in myth_skill_rows()}
    if myth_skill_id not in valid_skills:
        raise ValueError("所选神话技能不在原始技能池中")
    store, _, session = _store(character_id)
    candidates, affected_pets = _collect_equipment_targets(store, scope, equipment_kind)
    if not candidates:
        raise ValueError("所选范围内没有可操作的装备")
    targets = [
        t for t in candidates
        if t[1] is not None and (not position or int(t[1].position) == position)
    ]
    if not targets:
        raise ValueError("所选部位和范围内没有可进行神话打造的普通人物装备")
    items = [t[0] for t in targets]
    available_types = [int(row["type"]) for row in myth_attribute_rows()]
    with store.lock:
        for item, _definition, _pet in targets:
            chosen = [
                int(values[0]) for values in item.myth_attributes
                if len(values) >= 2 and int(values[0]) in available_types
            ]
            chosen = list(dict.fromkeys(chosen))[:5]
            chosen.extend(value for value in available_types if value not in chosen)
            chosen = chosen[:5]
            item.myth_attributes = [[value, 18] for value in chosen]
            item.myth_forge_count = 5
            item.myth_forge_type = 33333
            item.myth_wuxing = int(item.myth_wuxing) if 1 <= int(item.myth_wuxing) <= 5 else 1
            item.myth_wuxing_level = 3
            item.myth_skill_id = myth_skill_id
            item.myth_skill_level = 9
        skill_name = str(valid_skills[myth_skill_id]["name"])
        notice_body = (
            f"GM已完成您的{len(items)}件装备五阶段完美神话打造，"
            f"附带9级{skill_name}。"
        )
        _append_notification_locked(store, kind="equipment", title="GM一键神话打造", body=notice_body)
        store.save()
    if session is not None:
        embedded_ids = {id(item) for item, _definition, pet in targets if pet is not None}
        for item in sorted(items, key=lambda value: value.slot):
            if id(item) not in embedded_ids:
                session._refresh_inventory_item(item)
        for pet in affected_pets.values():
            session.send_text(
                f"{server.op(server.ServerOpcode.PET_ACTION)}:"
                f"{server.op(server.PetResponseAction.UPDATE)}:"
                f"{session._encode_pet_update(pet)}"
            )
        if equipment_kind == "character" and any(item.slot < 0 for item in items):
            session._clamp_resources_after_equipment_change()
            session._send_all_stats()
    _push_online_notification(session, notice_body)
    return {
        "characterId": character_id,
        "equipmentKind": equipment_kind,
        "scope": scope,
        "position": position,
        "equipmentCount": len(items),
        "forgeCount": 5,
        "mythSkillId": myth_skill_id,
        "mythSkillName": skill_name,
        "mythSkillLevel": 9,
    }


def set_myth_wuxing(payload: dict[str, Any]) -> dict[str, Any]:
    """Set myth wuxing element and level on equipment."""
    server = _server()
    character_id = int(payload.get("characterId", 0))
    equipment_kind = str(payload.get("equipmentKind", "character"))
    scope = str(payload.get("scope", "equipped"))
    position = int(payload.get("position", 0))
    wuxing_type = int(payload.get("wuxingType", 0))
    wuxing_level = int(payload.get("wuxingLevel", 3))
    if wuxing_type not in MYTH_WUXING_NAMES:
        raise ValueError("五行类型必须是水、风、火、土、雷之一")
    if not 1 <= wuxing_level <= 3:
        raise ValueError("五行等级必须是1至3级")
    if equipment_kind not in {"character", "pet"}:
        raise ValueError("装备类型无效")
    if scope not in {"equipped", "bag", "all"}:
        raise ValueError("处理范围无效")
    store, _, session = _store(character_id)
    targets, affected_pets = _collect_equipment_targets(store, scope, equipment_kind)
    if position:
        targets = [t for t in targets if t[1] is not None and int(t[1].position) == position]
    if not targets:
        raise ValueError("所选范围内没有可设置五行的装备")
    items = [t[0] for t in targets]
    wuxing_name = MYTH_WUXING_NAMES[wuxing_type]
    with store.lock:
        for item, _definition, _pet in targets:
            item.myth_wuxing = wuxing_type
            item.myth_wuxing_level = wuxing_level
        notice_body = (
            f"GM已将您的{len(items)}件装备五行设置为【{wuxing_name}】Lv.{wuxing_level}。"
        )
        _append_notification_locked(
            store, kind="equipment", title="GM修改五行", body=notice_body
        )
        store.save()
    if session is not None:
        embedded_ids = {id(item) for item, _definition, pet in targets if pet is not None}
        for item in sorted(items, key=lambda value: value.slot):
            if id(item) not in embedded_ids:
                session._refresh_inventory_item(item)
        for pet in affected_pets.values():
            session.send_text(
                f"{server.op(server.ServerOpcode.PET_ACTION)}:"
                f"{server.op(server.PetResponseAction.UPDATE)}:"
                f"{session._encode_pet_update(pet)}"
            )
        if equipment_kind == "character" and any(item.slot < 0 for item in items):
            session._clamp_resources_after_equipment_change()
            session._send_all_stats()
    _push_online_notification(session, notice_body)
    return {
        "characterId": character_id,
        "equipmentKind": equipment_kind,
        "scope": scope,
        "equipmentCount": len(items),
        "wuxingType": wuxing_type,
        "wuxingName": wuxing_name,
        "wuxingLevel": wuxing_level,
    }


def grant_max_spirit(payload: dict[str, Any]) -> dict[str, Any]:
    """Grant one authored level-20 Will to the bag or a free equipped slot."""
    server = _server()
    character_id = int(payload.get("characterId", 0))
    item_id = int(payload.get("spiritItemId", 0))
    placement = str(payload.get("placement", "bag"))
    if placement not in {"bag", "equip"}:
        raise ValueError("意志放置方式无效")

    definition = server.GAME_DATA_CATALOG.spirit_items().get(item_id)
    spec = definition.get("spec") if isinstance(definition, dict) else None
    color_name = str(spec.get("color") or "") if isinstance(spec, dict) else ""
    color_id = server.FakeFlashSession.SPIRIT_COLOR_IDS.get(color_name, 0)
    maximum_experience = server.GAME_DATA_CATALOG.spirit_level_threshold(item_id, 20)
    if color_id <= 1 or color_name == "yellow" or maximum_experience <= 0:
        raise ValueError("该意志不存在或不能升级到20级")

    store, _, session = _store(character_id)
    expanded_pack = False
    with store.lock:
        progression = store.state.progression
        occupied = {int(item.slot) for item in progression.spirit_items}
        if placement == "equip":
            equip_limit = server.GAME_DATA_CATALOG.spirit_equip_limit(
                store.state.character.level
            )
            slot = next(
                (candidate for candidate in range(-1, -equip_limit - 1, -1)
                 if candidate not in occupied),
                0,
            )
            if slot == 0:
                raise ValueError("角色当前没有空闲的意志装备槽")
        else:
            current_limit = max(5, min(server.FakeFlashSession.SPIRIT_PACK_MAX,
                                       int(progression.spirit_pack_limit)))
            slot = next(
                (candidate for candidate in range(1, current_limit + 1)
                 if candidate not in occupied),
                0,
            )
            if slot == 0 and current_limit < server.FakeFlashSession.SPIRIT_PACK_MAX:
                current_limit += 1
                progression.spirit_pack_limit = current_limit
                slot = current_limit
                expanded_pack = True
            if slot == 0:
                raise ValueError("意志背包已满，请先清理意志")

        spirit = server.SpiritState(
            item_id=item_id,
            color=color_id,
            slot=slot,
            experience=int(maximum_experience),
            level=20,
        )
        progression.spirit_items.append(spirit)
        spirit_name = server.GAME_DATA_CATALOG.spirit_name(item_id)
        destination = "装备槽" if slot < 0 else "意志背包"
        notice_body = f"GM向您发放了满级意志【{spirit_name}】，已放入{destination}。"
        _append_notification_locked(
            store,
            kind="spirit",
            title="GM满级意志发放",
            body=notice_body,
        )
        store.save()

    if session is not None:
        if expanded_pack:
            session._send_spirit_limits()
        session._send_spirit_add_or_update(spirit, 2)
        if slot < 0:
            session._refresh_spirit_combat_stats()
    _push_online_notification(session, notice_body)
    return {
        "characterId": character_id,
        "spiritItemId": item_id,
        "name": spirit_name,
        "level": 20,
        "experience": int(maximum_experience),
        "slot": slot,
        "placement": placement,
    }


def change_level(payload: dict[str, Any]) -> dict[str, Any]:
    server = _server()
    character_id = int(payload.get("characterId", 0))
    operation = str(payload.get("operation", "set"))
    amount = int(payload.get("amount", 1))
    store, _, session = _store(character_id)
    with store.lock:
        character = store.state.character
        # Repair the specific corrupt shape produced by an older GM level-set path
        # before applying a new delta, so setting the same level is also restorative.
        server.repair_level_attribute_baseline(character)
        old_level = character.level
        target = amount if operation == "set" else old_level + amount
        target = max(1, min(len(server.EXP_BY_LEVEL) - 1, target))
        delta = target - old_level
        character.level = target
        character.exp = 0
        character.max_hp = max(1, character.max_hp + delta * 25)
        character.max_mp = max(1, character.max_mp + delta * 10)
        character.ap_atk = max(0, character.ap_atk + delta)
        character.ap_def = max(0, character.ap_def + delta)
        character.ap_dex = max(0, character.ap_dex + delta)
        character.ap_phy = max(0, character.ap_phy + delta)
        character.remaining_ap = max(0, character.remaining_ap + delta * 4)
        if session is not None:
            character.hp = session._current_max_hp()
            character.mp = session._current_max_mp()
        else:
            character.hp = character.max_hp
            character.mp = character.max_mp
        if operation == "set":
            notice_body = f"GM将您的等级调整为 {target} 级。"
        else:
            notice_body = (
                f"GM调整了您的等级 {delta:+d} 级，当前为 {target} 级。"
            )
        _append_notification_locked(
            store,
            kind="level",
            title="GM等级调整",
            body=notice_body,
        )
        store.save()
    server.ACCOUNT_SERVICE.update_character_summary(character_id, level=target)
    if session is not None:
        last_level_gift = session._grant_due_level_gifts()
        session.send_text(
            f"{server.op(server.ServerOpcode.GIFT_INFO)}:3:{last_level_gift}"
        )
        _sync_player(session)
        session._refresh_all_original_quests()
    _push_online_notification(session, notice_body)
    return {"characterId": character_id, "level": target}


def kill_player(payload: dict[str, Any]) -> dict[str, Any]:
    server = _server()
    character_id = int(payload.get("characterId", 0))
    session = _session(character_id)
    if session is None:
        raise ValueError("角色不在线")
    with session.store.lock:
        session.character.hp = 0
        notice_body = "GM执行了角色死亡操作。"
        _append_notification_locked(
            session.store,
            kind="operation",
            title="GM操作通知",
            body=notice_body,
        )
        session.store.save()
    session._send_stats({server.StatType.HP: 0})
    session._send_player_dead_box()
    _push_online_notification(session, notice_body)
    return {"characterId": character_id, "killed": True}


def reset_copies(payload: dict[str, Any]) -> dict[str, Any]:
    character_id = int(payload.get("characterId", 0))
    store, _, session = _store(character_id)
    with store.lock:
        progress = store.state.progression.stage_progress
        for key in tuple(progress):
            if str(key).startswith("副本:"):
                progress[key] = 0
        notice_body = "GM已为您重置副本次数。"
        _append_notification_locked(
            store,
            kind="copy",
            title="GM副本重置",
            body=notice_body,
        )
        store.save()
    if session is not None:
        session._send_copy_history()
    _push_online_notification(session, notice_body)
    return {"characterId": character_id, "reset": True}


def grant_buff(payload: dict[str, Any]) -> dict[str, Any]:
    character_id = int(payload.get("characterId", 0))
    buff_type = int(payload.get("buffType", 0))
    value_percent = float(payload.get("valuePercent", 0))
    if not math.isfinite(value_percent):
        raise ValueError("Buff percentage must be a finite number")
    value_percent = max(-10_000.0, min(10_000.0, value_percent))
    duration_ms = max(1_000, min(7 * 24 * 60 * 60 * 1_000, int(payload.get("durationMs", 60_000))))
    skill_id = max(0, int(payload.get("skillId", 0)))
    store, _, session = _store(character_id)
    server = _server()
    if buff_type not in {int(value) for value in server.CombatBuffType}:
        raise ValueError("Unknown combat buff type")
    duration_seconds = max(1, duration_ms // 1_000)
    notice_body = (
        f"GM向您发放了 Buff（类型 {buff_type}，效果 {value_percent:+g}%，"
        f"持续 {duration_seconds} 秒）。"
    )
    if session is not None:
        session._apply_timed_skill_buff(
            buff_type=buff_type,
            skill_id=skill_id,
            skill_level=1,
            value_percent=value_percent,
            duration_ms=duration_ms,
        )
        with store.lock:
            _append_notification_locked(
                store,
                kind="buff",
                title="GM Buff发放",
                body=notice_body,
            )
            store.save()
    else:
        with store.lock:
            store.state.active_skill_buffs[buff_type] = {
                "skill_id": skill_id,
                "skill_level": 1,
                "value_percent": value_percent,
                "expires_at": int(time.time() * 1_000) + duration_ms,
            }
            _append_notification_locked(
                store,
                kind="buff",
                title="GM Buff发放",
                body=notice_body,
            )
            store.save()
    _push_online_notification(session, notice_body)
    return {"characterId": character_id, "buffType": buff_type, "durationMs": duration_ms}


def set_weather(payload: dict[str, Any]) -> dict[str, Any]:
    weather_type = int(payload.get("weatherType", -1))
    duration_seconds = max(0, int(payload.get("durationSeconds", 0)))
    GM_STATE.set_weather(None if weather_type < 0 else weather_type, duration_seconds)
    server = _server()
    with server.DEFAULT_HUB.lock:
        sessions = list(server.DEFAULT_HUB.sessions_by_character.values())
    for session in sessions:
        if session.entered_game:
            session.weather_period = -1
            session._refresh_weather(force=True)
            session._schedule_weather_refresh(delay_seconds=0.5, attempts=3)
    return {"weatherType": weather_type, "durationSeconds": duration_seconds}


def start_event(payload: dict[str, Any]) -> dict[str, Any]:
    event_key = str(payload.get("eventKey", "")).strip()
    duration_seconds = max(10, min(24 * 60 * 60, int(payload.get("durationSeconds", 3600))))
    state = GM_STATE.start_event(event_key, duration_seconds)
    server = _server()
    sessions = _online_sessions(server)
    for session in sessions:
        session._sync_scheduled_events(publish=True, force=True)
    event_name = _event_display_name(event_key)
    display_minutes = max(1, (duration_seconds + 59) // 60)
    message = f"【GM事件】{event_name}已经开启，持续{display_minutes}分钟。"
    recipients = _publish_world_announcement(message, notice_type="marquee")
    return {
        "eventKey": event_key,
        "eventName": event_name,
        "announcement": message,
        "recipients": recipients,
        **state,
    }


def stop_event(payload: dict[str, Any]) -> dict[str, Any]:
    event_key = str(payload.get("eventKey", "")).strip()
    GM_STATE.stop_event(event_key)
    server = _server()
    sessions = _online_sessions(server)
    for session in sessions:
        session._sync_scheduled_events(publish=True, force=True)
    event_name = _event_display_name(event_key)
    message = f"【GM事件】{event_name}已经结束。"
    recipients = _publish_world_announcement(message, notice_type="marquee")
    return {
        "eventKey": event_key,
        "eventName": event_name,
        "announcement": message,
        "recipients": recipients,
        "stopped": True,
    }


def publish_announcement(payload: dict[str, Any]) -> dict[str, Any]:
    message = str(payload.get("message", "")).strip()
    notice_type = str(payload.get("noticeType", "marquee")).strip().lower()
    highlights = _announcement_highlights(payload.get("highlights"))
    recipients = _publish_world_announcement(
        message,
        notice_type=notice_type,
        highlights=highlights,
    )
    return {
        "message": message,
        "noticeType": notice_type,
        "highlights": list(highlights),
        "recipients": recipients,
    }


def refresh_bosses(payload: dict[str, Any]) -> dict[str, Any]:
    server = _server()
    template_filter = max(0, int(payload.get("templateId", 0)))
    refreshed = 0
    refresh_world_boss_music = False
    wave_memorial_lines: set[int] = set()
    visited_worlds: set[int] = set()
    with server.DEFAULT_HUB.lock:
        sessions = [
            session
            for session in server.DEFAULT_HUB.sessions_by_character.values()
            if session.entered_game
        ]
    for session in sessions:
        state = session.world_state
        if state is None or id(state) in visited_worlds:
            continue
        visited_worlds.add(id(state))
        for monster in state.monsters.values():
            definition = server.GAME_DATA_CATALOG.get_monster_definition(monster.template_id)
            if not definition.boss or (template_filter and monster.template_id != template_filter):
                continue
            monster.x = monster.spawn_x
            monster.y = monster.spawn_y
            monster.foothold = monster.spawn_foothold
            monster.hp = monster.max_hp
            monster.respawn_at = 0.0
            monster.target_character_id = 0
            monster.skill_statuses.clear()
            server.DEFAULT_HUB.clear_boss_respawn(session, monster)
            session._send_visible(session._monster_spawn_payload(monster))
            refreshed += 1
            if (
                monster.template_id == session.WORLD_BOSS_TEMPLATE_ID
                and session.character.map_id == session.WORLD_BOSS_MAP_ID
            ):
                refresh_world_boss_music = True
            if any(
                monster.template_id == memorial_template_id
                for _, memorial_template_id in session.WAVE_MEMORIAL_BOSS_SPECS
            ):
                wave_memorial_lines.add(session.line_id)
        spawned = session._ensure_world_boss_spawn(force=True)
        if spawned is not None and (not template_filter or spawned.template_id == template_filter):
            session._send_visible(session._monster_spawn_payload(spawned))
            refresh_world_boss_music = True
    if refresh_world_boss_music:
        for session in sessions:
            if session.entered_game:
                session._broadcast_world_boss_music_config()
                break
    for line_id in wave_memorial_lines:
        representative = next(
            (
                session
                for session in sessions
                if session.entered_game and session.line_id == line_id
            ),
            None,
        )
        if representative is not None:
            representative._broadcast_wave_memorial_state(False)
    return {"refreshed": refreshed, "templateId": template_filter}


def set_ranking(payload: dict[str, Any]) -> dict[str, Any]:
    rank_type = str(payload.get("rankType", ""))
    if rank_type not in MANUAL_RANK_TYPES:
        raise ValueError("不支持的人工榜单")
    raw_ids = payload.get("characterIds", [])
    if not isinstance(raw_ids, list):
        raise ValueError("榜单角色必须是数组")
    valid_ids = {int(row["characterId"]) for row in player_rows()}
    character_ids = [int(value) for value in raw_ids if int(value) in valid_ids]
    result = GM_STATE.set_rankings(rank_type, character_ids)
    server = _server()
    with server.DEFAULT_HUB.lock:
        sessions = [
            session
            for session in server.DEFAULT_HUB.sessions_by_character.values()
            if session.entered_game
        ]
    for session in sessions:
        session._send_ranking_title_update(visible=True)
    return {"rankType": rank_type, "characterIds": list(result)}


def simulated_player_control(payload: dict[str, Any]) -> dict[str, Any]:
    operation = str(payload.get("operation", "status")).strip().lower()
    server = _server()
    from simulated_player_ai import get_simulated_player_manager

    manager = get_simulated_player_manager(server, server.DEFAULT_HUB)
    if operation == "start":
        manager.start()
    elif operation == "stop":
        manager.stop()
    elif operation == "reload":
        manager.reload()
    elif operation != "status":
        raise ValueError("模拟玩家操作必须是启动、停止、重载或查询状态")
    return manager.status()


def _simulated_players_config_path(server: Any) -> Path:
    return server.DEFAULT_HUB.accounts.path.with_name("simulated_players.json")


def _load_simulated_players_config(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    players = payload.get("players", []) if isinstance(payload, dict) else []
    return [value for value in players if isinstance(value, dict)]


def _write_simulated_players_config(path: Path, players: list[dict[str, Any]]) -> None:
    path.write_text(
        json.dumps({"players": players}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def add_simulated_player(payload: dict[str, Any]) -> dict[str, Any]:
    """Copy one real character into a new random simulated-player account."""
    server = _server()
    from simulated_player_ai import MAX_TEST_PLAYERS, get_simulated_player_manager

    source_character_id = int(payload.get("characterId", 0))
    behavior = str(payload.get("behavior") or "hunt").strip().lower()
    if behavior not in {"hunt", "farm", "meditate", "quest", "travel"}:
        behavior = "hunt"
    quantity = max(1, int(payload.get("quantity", 1)))

    store, summary, session = _store(source_character_id)
    with store.lock:
        source_document = get_document_store().read(
            "characters",
            f"{source_character_id}.json",
        )
    if not source_document:
        raise ValueError("源角色存档不存在")
    source_data = json.loads(source_document)
    source_character = source_data.get("character", {})
    source_name = str(source_character.get("name") or "忍者")
    source_job = int(source_character.get("job", 300))
    source_gender = int(source_character.get("gender", 0))
    source_face = int(source_character.get("face", 1))
    source_hair = int(source_character.get("hair", 0))

    config_path = _simulated_players_config_path(server)
    players = _load_simulated_players_config(config_path)
    if len(players) >= MAX_TEST_PLAYERS:
        raise ValueError(
            f"模拟玩家数量已达上限 {MAX_TEST_PLAYERS}，请先删除再创建"
        )

    created: list[dict[str, Any]] = []
    for _ in range(min(quantity, MAX_TEST_PLAYERS - len(players))):
        username = f"sim{secrets.token_hex(4)}"
        public = server.ACCOUNT_SERVICE.register(username, "sim123456")
        account = server.ACCOUNT_SERVICE.account_by_id(int(public["id"]))
        if account is None:
            continue
        suffix = str(secrets.randbelow(900) + 100)
        name = (source_name[:8] + suffix)[:12]
        character = server.ACCOUNT_SERVICE.create_character(
            account,
            name,
            source_job,
            source_gender,
            source_face,
            source_hair,
        )
        new_id = int(character["id"])
        data = json.loads(source_document)
        data["character"]["character_id"] = new_id
        data["character"]["name"] = name
        # 新角色创建时间晚于复制存档的闭关/培养时间戳时，_bind_character 会把
        # 它判定为“孤儿存档碰撞”并重置成默认档。清空这类时间戳避免重置。
        progression = data.setdefault("progression", {})
        if isinstance(progression, dict):
            progression["biguan_updated_at"] = 0
        # 模拟玩家作为独立账号从干净状态开始：清除宿主身上的伊邪那岐等
        # 主动 Buff，死亡后走正常的 4~7 秒复活流程。
        data["active_skill_buffs"] = {}
        get_document_store().write(
            "characters",
            f"{new_id}.json",
            json.dumps(data, ensure_ascii=False),
        )
        entry = {
            "accountId": account["id"],
            "characterId": new_id,
            "username": username,
            "behavior": behavior,
            "route": [],
            "skillId": 0,
            "line": 1,
            "enabled": True,
        }
        players.append(entry)
        created.append(entry)

    _write_simulated_players_config(config_path, players)
    manager = get_simulated_player_manager(server, server.DEFAULT_HUB)
    manager.reload()
    return {
        "created": created,
        "configuredCount": len(players),
        "max": MAX_TEST_PLAYERS,
    }


def remove_simulated_player(payload: dict[str, Any]) -> dict[str, Any]:
    """Delete one configured simulated player, its account, and its save."""
    server = _server()
    from simulated_player_ai import get_simulated_player_manager

    target_character_id = int(payload.get("characterId", 0))
    config_path = _simulated_players_config_path(server)
    players = _load_simulated_players_config(config_path)
    remaining = [
        player
        for player in players
        if int(player.get("characterId", 0)) != target_character_id
    ]
    removed = len(players) - len(remaining)
    if removed == 0:
        raise ValueError("配置中没有该模拟玩家")

    owner = server.ACCOUNT_SERVICE.character_owner(target_character_id)
    if owner is not None:
        account, _ = owner
        key = str(account.get("username", "")).strip().casefold()
        with server.ACCOUNT_SERVICE.lock:
            if key in server.ACCOUNT_SERVICE.data["accounts"]:
                del server.ACCOUNT_SERVICE.data["accounts"][key]
                server.ACCOUNT_SERVICE._save_locked()
        get_document_store().delete("characters", f"{target_character_id}.json")

    _write_simulated_players_config(config_path, remaining)
    manager = get_simulated_player_manager(server, server.DEFAULT_HUB)
    manager.reload()
    # Actor close() can rewrite the save during reload; clean it once more so
    # deletion leaves no leftover file on disk.
    if owner is not None:
        get_document_store().delete("characters", f"{target_character_id}.json")
    return {"removed": removed, "configuredCount": len(remaining)}


def _apply_cultivation_system(
    store: Any,
    server: Any,
    state_key: str,
    config_name: str,
    max_stage: int,
) -> dict[str, Any]:
    """Set one cultivation system to its maximum authored stage and progress."""
    progression = store.state.progression
    progression.stages[state_key] = max_stage
    for key in tuple(progression.stage_progress):
        if key == state_key or key.startswith(f"{state_key}:"):
            del progression.stage_progress[key]
    if state_key == "八门":
        for list_id in range(1, server.GAME_DATA_CATALOG.mai_max_list_id() + 1):
            mai_root = server.GAME_DATA_CATALOG.get_mai_list(list_id).get("mai")
            if not isinstance(mai_root, dict):
                continue
            for raw_mai_id, gate in mai_root.items():
                points = gate.get("pts") if isinstance(gate, dict) else None
                if isinstance(points, dict) and points:
                    progression.stage_progress[f"八门:{list_id}:{raw_mai_id}"] = len(points)
    elif state_key == "手里剑":
        progression.stage_progress["手里剑"] = 99_999
        progression.stage_progress["手里剑:开启"] = 1
        progression.stages["手里剑:等级"] = 4
    else:
        requirement = server.GAME_DATA_CATALOG.cultivation_progress_requirement(
            config_name,
            max_stage,
        )
        if requirement > 0:
            progression.stage_progress[state_key] = requirement
    return {"stateKey": state_key, "maxStage": max_stage}


def _refresh_cultivation_system(session: Any, server: Any, state_key: str) -> None:
    """Replay the same cultivation panel packets sent by the login bootstrap."""
    if state_key == "八门":
        session._send_mai_character_progress(server.GAME_DATA_CATALOG.mai_max_list_id())
        session._send_mai_statistics()
    elif state_key == "手里剑":
        session._send_an_qi_state()
        session._send_cultivation_blessing(server.ServerOpcode.AN_QI, "手里剑")
    elif state_key == "结印":
        session._send_jie_yin_state()
    elif state_key == "柔拳":
        session._send_progress_cultivation_state(
            server.ServerOpcode.ROU_QUAN,
            "柔拳",
            "RouQuan",
        )
        session._send_cultivation_blessing(server.ServerOpcode.ROU_QUAN, "柔拳")
    elif state_key == "写轮眼":
        session._send_progress_cultivation_state(
            server.ServerOpcode.XIELUNYAN_ACTION,
            "写轮眼",
            "XieLunYan",
        )
        session._send_xielunyan_blessing()
    elif state_key == "轮眼":
        session._send_progress_cultivation_state(
            server.ServerOpcode.LUN_HUI_YAN,
            "轮眼",
            "LunHuiYan",
        )
        session._send_cultivation_blessing(server.ServerOpcode.LUN_HUI_YAN, "轮眼")


def _run_cultivation_max(
    payload: dict[str, Any],
    state_key: str,
    config_name: str,
    max_stage: int,
    label: str,
) -> dict[str, Any]:
    """Apply one cultivation system, persist it, and refresh the online panel."""
    character_id = int(payload.get("characterId", 0))
    server = _server()
    store, _, session = _store(character_id)
    with store.lock:
        result = _apply_cultivation_system(
            store,
            server,
            state_key,
            config_name,
            max_stage,
        )
        notice_body = f"GM已一键拉满{label}（{max_stage}阶）。"
        _append_notification_locked(
            store,
            kind="cultivation",
            title="GM修炼全开",
            body=notice_body,
        )
        store.save()
    if session is not None:
        _refresh_cultivation_system(session, server, state_key)
        _sync_player(session)
    _push_online_notification(session, notice_body)
    return {"characterId": character_id, **result}


def cultivation_max_mai(payload: dict[str, Any]) -> dict[str, Any]:
    server = _server()
    return _run_cultivation_max(
        payload,
        "八门",
        "BaMen",
        server.GAME_DATA_CATALOG.mai_max_list_id(),
        "八门遁甲",
    )


def cultivation_max_rouquan(payload: dict[str, Any]) -> dict[str, Any]:
    server = _server()
    return _run_cultivation_max(
        payload,
        "柔拳",
        "RouQuan",
        server.GAME_DATA_CATALOG.cultivation_max_stage("RouQuan"),
        "柔拳",
    )


def cultivation_max_anqi(payload: dict[str, Any]) -> dict[str, Any]:
    server = _server()
    return _run_cultivation_max(
        payload,
        "手里剑",
        "AnQi",
        server.GAME_DATA_CATALOG.cultivation_max_stage("AnQi"),
        "手里剑",
    )


def cultivation_max_jieyin(payload: dict[str, Any]) -> dict[str, Any]:
    server = _server()
    return _run_cultivation_max(
        payload,
        "结印",
        "JieYin",
        server.GAME_DATA_CATALOG.cultivation_max_stage("JieYin"),
        "结印",
    )


def cultivation_max_xielunyan(payload: dict[str, Any]) -> dict[str, Any]:
    server = _server()
    return _run_cultivation_max(
        payload,
        "写轮眼",
        "XieLunYan",
        server.GAME_DATA_CATALOG.cultivation_max_stage("XieLunYan"),
        "写轮眼",
    )


def cultivation_max_lunhuiyan(payload: dict[str, Any]) -> dict[str, Any]:
    server = _server()
    return _run_cultivation_max(
        payload,
        "轮眼",
        "LunHuiYan",
        server.GAME_DATA_CATALOG.cultivation_max_stage("LunHuiYan"),
        "轮回六道",
    )


def cultivation_max_all(payload: dict[str, Any]) -> dict[str, Any]:
    character_id = int(payload.get("characterId", 0))
    server = _server()
    store, _, session = _store(character_id)
    systems = (
        ("八门", "BaMen", server.GAME_DATA_CATALOG.mai_max_list_id()),
        ("柔拳", "RouQuan", server.GAME_DATA_CATALOG.cultivation_max_stage("RouQuan")),
        ("手里剑", "AnQi", server.GAME_DATA_CATALOG.cultivation_max_stage("AnQi")),
        ("结印", "JieYin", server.GAME_DATA_CATALOG.cultivation_max_stage("JieYin")),
        (
            "写轮眼",
            "XieLunYan",
            server.GAME_DATA_CATALOG.cultivation_max_stage("XieLunYan"),
        ),
        ("轮眼", "LunHuiYan", server.GAME_DATA_CATALOG.cultivation_max_stage("LunHuiYan")),
    )
    with store.lock:
        updated = [
            _apply_cultivation_system(
                store,
                server,
                state_key,
                config_name,
                max_stage,
            )
            for state_key, config_name, max_stage in systems
        ]
        notice_body = (
            f"GM已一键拉满全部修炼系统（"
            f"{', '.join(result['stateKey'] for result in updated)}）。"
        )
        _append_notification_locked(
            store,
            kind="cultivation",
            title="GM修炼一键全开",
            body=notice_body,
        )
        store.save()
    if session is not None:
        for state_key, _, _ in systems:
            _refresh_cultivation_system(session, server, state_key)
        _sync_player(session)
    _push_online_notification(session, notice_body)
    return {
        "characterId": character_id,
        "updated": [result["stateKey"] for result in updated],
    }


def learn_all_skills(payload: dict[str, Any]) -> dict[str, Any]:
    character_id = int(payload.get("characterId", 0))
    server = _server()
    store, _, session = _store(character_id)
    character_level = store.state.character.level
    character_job = int(store.state.character.job)
    candidates: list[int] = []
    for skill_id in _test_skill_ids(server):
        if skill_id in server.NON_UPGRADABLE_BOND_SKILL_IDS:
            continue
        if not _character_skill_compatible(server, skill_id, character_job):
            continue
        definition = server.GAME_DATA_CATALOG.get_skill_definition(skill_id)
        if definition is None or definition.required_level > character_level:
            continue
        candidates.append(skill_id)
    updates: list[Any] = []
    added = 0
    with store.lock:
        for skill_id in candidates:
            skill = store.state.skills.get(skill_id)
            if skill is None:
                skill = server.SkillState(skill_id=skill_id, level=1)
                store.state.skills[skill_id] = skill
                added += 1
            elif int(skill.level) <= 0:
                skill.level = 1
            updates.append(skill)
        notice_body = f"GM已一键学习 {len(updates)} 个技能（新增 {added} 个）。"
        _append_notification_locked(
            store,
            kind="skills",
            title="GM技能学习",
            body=notice_body,
        )
        store.save()
    if session is not None:
        for skill in updates:
            session._send_skill_update(skill)
        session._send_all_stats()
    _push_online_notification(session, notice_body)
    return {
        "characterId": character_id,
        "skillCount": len(updates),
        "added": added,
        "learned": added,
        "skillIds": [int(skill.skill_id) for skill in updates],
    }


def max_skill_levels(payload: dict[str, Any]) -> dict[str, Any]:
    character_id = int(payload.get("characterId", 0))
    server = _server()
    store, _, session = _store(character_id)
    maximum = min(
        store.state.character.level,
        len(server.EXP_BY_LEVEL) - 1,
    )
    updates: list[Any] = []
    with store.lock:
        for skill in store.state.skills.values():
            if int(skill.level) <= 0:
                continue
            skill.level = maximum
            skill.master_level = 0
            updates.append(skill)
        notice_body = f"GM已将 {len(updates)} 个技能等级刷满至 {maximum} 级。"
        _append_notification_locked(
            store,
            kind="skills",
            title="GM技能刷满",
            body=notice_body,
        )
        store.save()
    if session is not None:
        for skill in updates:
            session._send_skill_update(skill)
        session._send_all_stats()
    _push_online_notification(session, notice_body)
    return {
        "characterId": character_id,
        "skillCount": len(updates),
        "maximumLevel": maximum,
    }


def complete_quests_achievements(payload: dict[str, Any]) -> dict[str, Any]:
    character_id = int(payload.get("characterId", 0))
    server = _server()
    store, _, session = _store(character_id)
    progression = store.state.progression
    with store.lock:
        keys = set(progression.claimed_achievements)
        keys.update(progression.notified_achievements)
        for tab_id in server.GAME_DATA_CATALOG.achievement_tab_ids:
            for definition in server.GAME_DATA_CATALOG.achievement_definitions(tab_id):
                keys.add(f"{tab_id}:{definition.index}")
        progression.claimed_achievements = sorted(keys)
        progression.notified_achievements = sorted(keys)
        quest_count = 0
        for quest_id in server.GAME_DATA_CATALOG.quest_ids:
            progress = store.state.original_quests.get(quest_id)
            if progress is None:
                progress = server.OriginalQuestProgress(
                    state=int(server.QuestState.COMPLETED),
                    finish_count=1,
                )
                store.state.original_quests[quest_id] = progress
                quest_count += 1
            elif int(progress.state) != int(server.QuestState.COMPLETED):
                progress.state = int(server.QuestState.COMPLETED)
                progress.finish_count = max(1, int(progress.finish_count))
                quest_count += 1
        if int(store.state.quest.state) != int(server.QuestState.COMPLETED):
            store.state.quest.state = int(server.QuestState.COMPLETED)
        notice_body = f"GM已一键完成成就 {len(keys)} 个、任务 {quest_count} 个。"
        _append_notification_locked(
            store,
            kind="quests",
            title="GM任务成就全开",
            body=notice_body,
        )
        store.save()
    if session is not None:
        _sync_player(session)
        session._send_npc_quest_status()
    _push_online_notification(session, notice_body)
    return {
        "characterId": character_id,
        "achievements": len(keys),
        "quests": quest_count,
    }


def complete_card_collection(payload: dict[str, Any]) -> dict[str, Any]:
    character_id = int(payload.get("characterId", 0))
    server = _server()
    store, _, session = _store(character_id)
    with store.lock:
        node_keys: list[str] = []
        for level in server.GAME_DATA_CATALOG.card_levels:
            for node_id in range(100):
                definition = server.GAME_DATA_CATALOG.get_card_node(level, node_id)
                if definition is None:
                    continue
                node_key = f"{level}:{node_id}"
                store.state.card_submissions[node_key] = {
                    requirement.item_id: requirement.count
                    for requirement in definition.items
                }
                node_keys.append(node_key)
        store.state.card_claimed_nodes = sorted(
            set(store.state.card_claimed_nodes) | set(node_keys)
        )
        notice_body = f"GM已一键完成卡牌收集，共点亮 {len(node_keys)} 个节点。"
        _append_notification_locked(
            store,
            kind="cards",
            title="GM卡牌全开",
            body=notice_body,
        )
        store.save()
    if session is not None:
        for level in server.GAME_DATA_CATALOG.card_levels:
            session._send_card_level_info(level)
    _push_online_notification(session, notice_body)
    return {
        "characterId": character_id,
        "nodeCount": len(node_keys),
        "completed": len(node_keys),
    }


def complete_collection_album(payload: dict[str, Any]) -> dict[str, Any]:
    character_id = int(payload.get("characterId", 0))
    server = _server()
    store, _, session = _store(character_id)
    with store.lock:
        role_catalog = set(server.GAME_DATA_CATALOG.permanent_transform_card_ids)
        pet_catalog = set(server.GAME_DATA_CATALOG.permanent_pet_transform_card_ids)
        all_cards = sorted(role_catalog | pet_catalog)
        collected_before = len(store.state.transform_card_collection)
        store.state.transform_card_collection = sorted(set(store.state.transform_card_collection) | set(all_cards))
        collected_after = len(store.state.transform_card_collection)
        notice_body = (
            f"GM已一键完成藏品图鉴，收集进度 {collected_before} → {collected_after}，"
            f"共 {len(all_cards)} 张变身卡。"
        )
        _append_notification_locked(
            store,
            kind="collection",
            title="GM图鉴全开",
            body=notice_body,
        )
        store.save()
    if session is not None:
        session._send_character_look()
        session._send_full_character_info()
    _push_online_notification(session, notice_body)
    return {
        "characterId": character_id,
        "totalCards": len(all_cards),
        "collectedBefore": collected_before,
        "collectedAfter": collected_after,
    }


def complete_pve(payload: dict[str, Any]) -> dict[str, Any]:
    character_id = int(payload.get("characterId", 0))
    server = _server()
    store, _, session = _store(character_id)
    with store.lock:
        total_levels = len(server.GAME_DATA_CATALOG.pve_levels())
        store.state.progression.pve_progress = total_levels
        store.state.progression.pve_points = 999_999
        notice_body = (
            f"GM已一键完成百炼忍传，通关 {total_levels} 层，"
            f"忍传点数 999999。"
        )
        _append_notification_locked(
            store,
            kind="pve",
            title="GM百炼忍传全开",
            body=notice_body,
        )
        store.save()
    if session is not None:
        session._send_pve_status()
    _push_online_notification(session, notice_body)
    return {
        "characterId": character_id,
        "pveProgress": total_levels,
        "pvePoints": 999_999,
        "progress": total_levels,
        "points": 999_999,
    }


def pet_potential_boost(payload: dict[str, Any]) -> dict[str, Any]:
    """Increase one attached pet potential without requiring a relog."""
    character_id = int(payload.get("characterId", 0))
    potential_type = str(payload.get("potentialType") or "").lower()
    fields = {
        "atk": ("potential_real_atk", "potential_total_atk", "攻击潜力"),
        "def": ("potential_real_def", "potential_total_def", "防御潜力"),
        "dex": ("potential_real_dex", "potential_total_dex", "灵巧潜力"),
        "str": ("potential_real_str", "potential_total_str", "健体潜力"),
    }
    selected = fields.get(potential_type)
    if selected is None:
        raise ValueError("未知潜力类型")
    server = _server()
    store, _, session = _store(character_id)
    pet = next((value for value in store.state.pets.values()
                if value.position == server.op(server.PetPosition.ATTACHED)), None)
    if pet is None:
        raise ValueError("该角色没有附身宠物")
    real_field, total_field, label = selected
    with store.lock:
        old_real = max(0, getattr(pet, real_field))
        old_total = max(old_real, getattr(pet, total_field), 0)
        setattr(pet, total_field, old_total + 1_000_000)
        setattr(pet, real_field, min(getattr(pet, total_field), old_real + 1_000_000))
        notice_body = f"GM已将附身宠物【{pet.name}】的{label}提升 1000000 点。"
        _append_notification_locked(store, kind="pet", title="GM附身宠物潜力速成", body=notice_body)
        store.save()
    if session is not None:
        session.send_text(
            f"{server.op(server.ServerOpcode.PET_ACTION)}:"
            f"{server.op(server.PetResponseAction.UPDATE)}:{session._encode_pet_update(pet)}"
        )
        session._send_all_stats()
    _push_online_notification(session, notice_body)
    return {"characterId": character_id, "petId": int(pet.unique_id), "potentialType": potential_type,
            "total": getattr(pet, total_field), "real": getattr(pet, real_field)}


def collect_all_transform_cards(payload: dict[str, Any]) -> dict[str, Any]:
    """Collect every authored player and pet transform card exactly once."""
    server = _server()
    character_id = int(payload.get("characterId", 0))
    store, _, session = _store(character_id)
    card_ids = tuple(dict.fromkeys([
        *server.GAME_DATA_CATALOG.permanent_transform_card_ids,
        *server.GAME_DATA_CATALOG.permanent_pet_transform_card_ids,
    ]))
    collected = 0
    with store.lock:
        next_unique = 1 + max(
            (int(item.unique_id) for item in [
                *store.state.inventory.values(),
                *store.state.transform_card_storage.values(),
            ]),
            default=0,
        )
        for item_id in card_ids:
            if item_id in store.state.transform_card_storage:
                continue
            item = server.InventoryItem(
                item_id=int(item_id), slot=0, quantity=1,
                unique_id=next_unique, can_trade=False,
            )
            next_unique += 1
            server.apply_nya208_transform_card_profile(item)
            store.state.transform_card_storage[int(item_id)] = item
            if int(item_id) not in store.state.transform_card_collection:
                store.state.transform_card_collection.append(int(item_id))
            collected += 1
        store.state.transform_card_collection.sort()
        notice_body = f"GM已收集 {collected} 张变身卡。"
        _append_notification_locked(store, kind="collection", title="GM图鉴全开", body=notice_body)
        store.save()
    if session is not None:
        session._send_hokage_collection_info()
        _sync_player(session)
    _push_online_notification(session, notice_body)
    return {"characterId": character_id, "collected": collected, "total": len(card_ids)}


def clear_inventory(payload: dict[str, Any]) -> dict[str, Any]:
    """Compatibility action that clears bag slots while preserving equipment."""
    result = clear_player_inventory({**payload, "scopes": ["bag"]})
    return {
        **result,
        "cleared": int(result.get("bagRemoved", 0)),
    }


def set_player_attributes(payload: dict[str, Any]) -> dict[str, Any]:
    """Manipulate a player's AP (attribute point) stats in one of three modes.

    Mode ``max_all``   → every AP field (4 primaries + remaining pool) is set to
                         the GM maximum in a single shot.
    Mode ``uniform``   → all 4 primary APs receive the same user value; the
                         remaining pool is optionally also set separately.
    Mode ``single``    → change one explicitly selected AP field (key in
                         AP_ATTRIBUTE_FIELDS) either to the GM max or to the
                         specific numeric value provided by the operator.
    """
    character_id = int(payload.get("characterId", 0))
    mode = str(payload.get("mode", "max_all"))
    if mode not in {"max_all", "uniform", "single"}:
        raise ValueError("属性操作模式无效")
    store, _, session = _store(character_id)
    changed: dict[str, tuple[int, int]] = {}

    def _apply_field(key: str, new_value: int) -> None:
        if key not in AP_ATTRIBUTE_FIELDS:
            raise ValueError(f"不支持的属性字段: {key}")
        value = int(max(0, min(GM_AP_MAX, int(new_value))))
        current = int(getattr(store.state.character, key, 0))
        if current != value:
            setattr(store.state.character, key, value)
            changed[key] = (current, value)

    with store.lock:
        if mode == "max_all":
            for key in AP_ATTRIBUTE_FIELDS:
                _apply_field(key, GM_AP_MAX)
        elif mode == "uniform":
            uniform_value = int(payload.get("uniformValue", GM_AP_MAX))
            remaining_value = payload.get("remainingValue")
            for key in ("ap_atk", "ap_def", "ap_dex", "ap_phy"):
                _apply_field(key, uniform_value)
            if remaining_value is not None:
                _apply_field("remaining_ap", int(remaining_value))
            else:
                _apply_field("remaining_ap", uniform_value)
        else:
            key = str(payload.get("attribute", ""))
            raw_value = payload.get("value")
            if raw_value is None or str(raw_value) in {"", "max"}:
                _apply_field(key, GM_AP_MAX)
            else:
                _apply_field(key, int(raw_value))
        if not changed:
            return {
                "characterId": character_id,
                "mode": mode,
                "changed": {},
            }
        store.save()
    if session is not None:
        session._send_all_stats()
        session._send_full_character_info()
    formatted_changes = {
        AP_ATTRIBUTE_FIELDS[key]: {"before": before, "after": after}
        for key, (before, after) in changed.items()
    }
    notice_body = (
        f"GM角色属性操作（{mode}）完成，修改字段："
        + "，".join(
            f"{name} {before}→{after}"
            for name, (before, after) in formatted_changes.items()
        )
    )
    _push_online_notification(session, notice_body)
    return {
        "characterId": character_id,
        "mode": mode,
        "changed": formatted_changes,
    }


def _summarize_save(scope: str, key: str, raw: str | None) -> dict[str, Any]:
    """Extract a short human-readable summary from one document."""
    if raw is None:
        return {"valid": False, "error": "缺失"}
    try:
        obj = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {"valid": False, "error": "JSON 解析失败"}
    if not isinstance(obj, dict):
        return {"valid": False, "error": "根节点不是对象"}
    if scope == "characters":
        character = obj.get("character") or {}
        return {
            "valid": True,
            "characterId": character.get("character_id"),
            "name": character.get("name"),
            "level": character.get("level"),
            "money": obj.get("money"),
        }
    if scope == "multiplayer" and key == "accounts.json":
        return {
            "valid": True,
            "accounts": len(obj.get("accounts") or {}),
        }
    if scope == "root" and key == "gm-state.json":
        return {"valid": True, "type": "GM状态"}
    return {"valid": True}


def save_rows(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Summarize every document in the active save store."""
    store = get_document_store()
    rows: list[dict[str, Any]] = []
    for scope in ("root", "multiplayer", "characters"):
        for key in store.list(scope):
            raw = store.read(scope, key)
            row = {"scope": scope, "key": key, "size": len(raw or "")}
            row.update(_summarize_save(scope, key, raw))
            rows.append(row)
    return {"saves": rows, "backend": "sqlite" if "Sqlite" in type(store).__name__ else "json"}


def save_detail(payload: dict[str, Any]) -> dict[str, Any]:
    """Return one document's raw payload for the GM viewer."""
    scope = str(payload.get("scope", ""))
    key = str(payload.get("key", ""))
    if not scope or not key:
        raise ValueError("scope/key 必填")
    raw = get_document_store().read(scope, key)
    if raw is None:
        raise ValueError("文档不存在")
    return {"scope": scope, "key": key, "payload": raw}


def repair_saves(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize one or every document in the save store."""
    server = _server()
    store = get_document_store()
    scope = str(payload.get("scope", ""))
    key = str(payload.get("key", ""))
    if scope and key:
        targets = [(scope, key)]
    else:
        targets = [
            (current_scope, current_key)
            for current_scope in ("root", "multiplayer", "characters")
            for current_key in store.list(current_scope)
        ]
    results: list[dict[str, Any]] = []
    for current_scope, current_key in targets:
        raw = store.read(current_scope, current_key)
        if raw is None:
            results.append(
                {"scope": current_scope, "key": current_key, "status": "missing"}
            )
            continue
        try:
            obj = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            results.append(
                {
                    "scope": current_scope,
                    "key": current_key,
                    "status": "error",
                    "detail": f"JSON 解析失败: {exc}",
                }
            )
            continue
        if not isinstance(obj, dict):
            results.append(
                {
                    "scope": current_scope,
                    "key": current_key,
                    "status": "error",
                    "detail": "根节点必须是对象",
                }
            )
            continue
        if current_scope == "characters":
            try:
                state = server.SinglePlayerState.from_dict(obj)
                state.normalize_item_quantities()
                normalized = state.to_dict()
            except Exception as exc:  # noqa: BLE001 - surface any repair failure
                results.append(
                    {
                        "scope": current_scope,
                        "key": current_key,
                        "status": "error",
                        "detail": str(exc),
                    }
                )
                continue
            changed = normalized != obj
            if changed:
                store.write(
                    current_scope,
                    current_key,
                    json.dumps(normalized, ensure_ascii=False, indent=2),
                )
            results.append(
                {
                    "scope": current_scope,
                    "key": current_key,
                    "status": "ok",
                    "changed": changed,
                    "detail": "已规范化" if changed else "无需修改",
                }
            )
        else:
            results.append(
                {
                    "scope": current_scope,
                    "key": current_key,
                    "status": "ok",
                    "changed": False,
                    "detail": "结构有效",
                }
            )
    return {"results": results}


def _backup_relative_path(scope: str, key: str) -> Path:
    if scope == "root":
        return Path(key)
    if scope == "multiplayer":
        return Path("multiplayer") / key
    if scope == "characters":
        return Path("multiplayer") / "characters" / key
    raise ValueError("scope 无效")


def save_backups(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """List every save_backup_* snapshot and the documents it contains."""
    project_root = Path(__file__).resolve().parent.parent
    backups: list[dict[str, Any]] = []
    for directory in sorted(project_root.glob("save_backup_*")):
        if not directory.is_dir():
            continue
        documents: list[dict[str, Any]] = []
        for path in sorted(directory.rglob("*.json")):
            if not path.is_file():
                continue
            relative = path.relative_to(directory)
            parts = tuple(part for part in relative.parts if part != "save")
            if len(parts) == 1:
                scope, key = "root", parts[0]
            elif parts[0] == "multiplayer" and len(parts) == 2:
                scope, key = "multiplayer", parts[1]
            elif (
                len(parts) >= 3
                and parts[0] == "multiplayer"
                and parts[1] == "characters"
            ):
                scope, key = "characters", "/".join(parts[2:])
            else:
                scope, key = parts[0], "/".join(parts[1:])
            documents.append(
                {"scope": scope, "key": key, "size": path.stat().st_size}
            )
        backups.append({"name": directory.name, "documents": documents})
    return {"backups": backups}


def restore_save(payload: dict[str, Any]) -> dict[str, Any]:
    """Restore one document from a save_backup_* snapshot into the live store."""
    backup = str(payload.get("backup", ""))
    scope = str(payload.get("scope", ""))
    key = str(payload.get("key", ""))
    if not backup or not scope or not key:
        raise ValueError("backup/scope/key 必填")
    project_root = Path(__file__).resolve().parent.parent
    backup_dir = project_root / backup
    if not backup_dir.is_dir():
        raise ValueError("备份不存在")
    source = backup_dir / _backup_relative_path(scope, key)
    if not source.is_file():
        source = backup_dir / "save" / _backup_relative_path(scope, key)
    if not source.is_file():
        raise ValueError("该备份中没有此文档")
    content = source.read_text(encoding="utf-8")
    get_document_store().write(scope, key, content)
    return {"restored": True, "scope": scope, "key": key, "backup": backup}


def restore_save_all(payload: dict[str, Any]) -> dict[str, Any]:
    """Restore every document from one backup snapshot into the live store."""
    backup = str(payload.get("backup", ""))
    if not backup:
        raise ValueError("backup 必填")
    project_root = Path(__file__).resolve().parent.parent
    backup_dir = project_root / backup
    if not backup_dir.is_dir():
        raise ValueError("备份不存在")
    store = get_document_store()
    restored = 0
    for path in sorted(backup_dir.rglob("*.json")):
        if not path.is_file():
            continue
        relative = path.relative_to(backup_dir)
        parts = tuple(part for part in relative.parts if part != "save")
        if len(parts) == 1:
            scope, key = "root", parts[0]
        elif parts[0] == "multiplayer" and len(parts) == 2:
            scope, key = "multiplayer", parts[1]
        elif (
            len(parts) >= 3
            and parts[0] == "multiplayer"
            and parts[1] == "characters"
        ):
            scope, key = "characters", "/".join(parts[2:])
        else:
            scope, key = parts[0], "/".join(parts[1:])
        store.write(scope, key, path.read_text(encoding="utf-8"))
        restored += 1
    return {"restored": restored, "backup": backup}


ACTIONS = {
    "currency": change_currency,
    "item": grant_item,
    "clear-inventory": clear_player_inventory,
    "redemption-codes": redemption_codes_list,
    "create-redemption-code": create_redemption_code,
    "delete-redemption-code": delete_redemption_code,
    "news": get_news,
    "update-news": update_news,
    "activity-overrides": get_activity_overrides,
    "update-activity-overrides": update_activity_overrides,
    "highest-purple-job-set": grant_highest_purple_job_set,
    "highest-pet-equipment-set": grant_highest_pet_equipment_set,
    "max-spirit": grant_max_spirit,
    "level": change_level,
    "all-skills": grant_all_skills,
    "kill": kill_player,
    "reset-copies": reset_copies,
    "buff": grant_buff,
    "weather": set_weather,
    "start-event": start_event,
    "stop-event": stop_event,
    "announcement": publish_announcement,
    "refresh-bosses": refresh_bosses,
    "ranking": set_ranking,
    "cultivation-max-all": cultivation_max_all,
    "cultivation-max-mai": cultivation_max_mai,
    "cultivation-max-rouquan": cultivation_max_rouquan,
    "cultivation-max-anqi": cultivation_max_anqi,
    "cultivation-max-jieyin": cultivation_max_jieyin,
    "cultivation-max-xielunyan": cultivation_max_xielunyan,
    "cultivation-max-lunhuiyan": cultivation_max_lunhuiyan,
    "learn-all-skills": learn_all_skills,
    "max-skill-levels": max_skill_levels,
    "complete-quests-achievements": complete_quests_achievements,
    "complete-card-collection": complete_card_collection,
    "complete-collection-album": complete_collection_album,
    "complete-pve": complete_pve,
    "pet-potential-boost": pet_potential_boost,
    "max-carve-equipment": apply_max_carve,
    "max-strength-equipment": max_strength_equipment,
    "max-inlay-equipment": max_inlay_equipment,
    "max-born-equipment": max_born_equipment,
    "max-myth-attributes-equipment": max_myth_attributes_equipment,
    "max-refine-equipment": max_refine_equipment,
    "evolve-legendary-equipment": evolve_legendary_equipment,
    "customize-additional-attributes": customize_additional_attributes,
    "customize-tale-attribute": customize_tale_attribute,
    "max-myth-forge-equipment": max_myth_forge_equipment,
    "set-myth-wuxing": set_myth_wuxing,
    "set-player-attributes": set_player_attributes,
    "set-equipment-special-attributes": set_equipment_special_attributes,
    "saves-list": save_rows,
    "save-detail": save_detail,
    "save-repair": repair_saves,
    "save-backups": save_backups,
    "save-restore": restore_save,
    "save-restore-all": restore_save_all,
}


def run_action(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    handler = ACTIONS.get(str(action))
    if handler is None:
        raise ValueError("未知 GM 操作")
    return _run_character_action_transaction(str(action), handler, payload)


def status() -> dict[str, Any]:
    return {
        "players": player_rows(),
        "catalog": catalog(),
        "state": GM_STATE.snapshot(),
        "simulatedPlayers": {
            "configured": False,
            "state": "disabled",
            "actors": [],
            "configuredPlayers": [],
            "lastError": "AI人机功能已移除",
        },
    }
