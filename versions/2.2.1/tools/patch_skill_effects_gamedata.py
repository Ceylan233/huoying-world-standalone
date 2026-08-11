#!/usr/bin/env python3
"""Keep client-authored combat effect resources on their intended skills."""

from __future__ import annotations

import argparse
import copy
import json
import os
import tempfile
import zlib
from pathlib import Path
from typing import Any

from build_language_packs import read_amf, write_amf
from fake_flash_server import Amf3Reader
from generate_item_dat import encode_value


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_GAME_DATA = (
    PROJECT_ROOT / "www" / "act_web_nya_208_isolated" / "dat" / "GameData.dat"
)

SHENLUO_EFFECT_OLD = "Skill_000_skill_0003204_effect"
SHENLUO_EFFECT_NEW = "Skill_000_skill_0004009_effect"
SHENLUO_HIT_OLD = "Skill_000_skill_0008014_hit_0"
SHENLUO_HIT_NEW = "Skill_000_skill_0004009_hit_0"
SHENLUO_EFFECT_ORIGIN = {"x": 209, "y": 255}
SHENLUO_HIT_ORIGIN = {"x": 116, "y": 238}
SHENLUO_FRAME_DELAY_MS = 80
SHENLUO_FIXED_COOLDOWN = {"cdBase": 5, "cdLevel": 0}

# Nya 2.1.1 keeps these forbidden-skill catalog rows, but several rows only
# contain their icon and info blocks.  The older decoded client is used only
# for visual fields that are entirely absent from the authoritative row.
LEGACY_VISUAL_SOURCE = PROJECT_ROOT.parent / "analysis" / "gamedata" / "GameData_new.json"
LEGACY_VISUAL_SKILLS = {
    "0004005": ("effect", "hit", "ball"),
    "0004010": ("effect",),
}

# No dedicated effect/hit SWFs exist anywhere in the supplied Nya art package
# for the remaining fallback skills. Reuse existing themed animations only for
# skills that do not enter the forbidden BeatBackEffect path.
PROJECT_VISUAL_FALLBACKS = {
    "0004007": {"effect": ("0003204", "effect")},  # Edo Tensei: soul effect
    "0004008": {
        "effect": ("0003205", "effect"),
        "hit": ("0003205", "hit"),
    },
    "0004011": {"effect": ("0003003", "effect")},
    "0004012": {"effect": ("0004009", "effect")},
}
IZANAGI_EFFECT = "Skill_000_skill_0004006_effect"
IZANAGI_EFFECT_ORIGIN = {"x": 256, "y": 256}
IZANAGI_FRAME_DELAY_MS = 120
LOCAL_SELF_CAST_SKILLS = ("0004006", "0004010", "0004011")
SERVER_TARGETED_SKILLS = ("0004007", "0004012")
PROJECT_TARGET_DESCRIPTORS = {
    "0004006": {"target": 3, "range": 1},
    "0004012": {"target": 1, "range": 4},
}
PROJECT_SPECIAL_SKILLS = {
    "0009030": {
        "name": "影分身之术",
        "book_id": 6_000_130,
        "required_skill": 0,
        "target": 3,
        "range": 1,
        "fire_local": 1,
        "cooldown": 60,
        "zq_cost": 300,
        "attribute_per_upgrade_percent": 0.2,
        "cost_reduction_per_upgrade_percent": 0.1,
        "icon_source": "0008101",
        "effect_name": "Skill_000_skill_0009030_effect",
        "effect_dimensions": (
            (50, 56),
            (96, 98),
            (95, 94),
            (96, 85),
            (101, 84),
            (105, 85),
            (108, 89),
        ),
        "effect_delay": 90,
        "effect_anchor": "feet",
        "description": "召唤1个由系统控制的影分身，1级继承自身全部属性的10%，持续30秒；2级起每升1级，分身额外继承自身属性0.2个百分点，查克拉与精华消耗降低0.1%。1级基础冷却60秒，消耗最大查克拉20%和300点查克拉精华。",
    },
    "0009031": {
        "name": "多重影分身之术",
        "book_id": 6_000_131,
        "required_skill": 9_030,
        "target": 3,
        "range": 1,
        "fire_local": 1,
        "cooldown": 120,
        "zq_cost": 900,
        "attribute_per_upgrade_percent": 0.2,
        "cost_reduction_per_upgrade_percent": 0.1,
        "icon_source": "0008101",
        "effect_name": "Skill_000_skill_0009031_effect",
        "effect_dimensions": (
            (50, 56),
            (96, 98),
            (95, 94),
            (96, 85),
            (101, 84),
            (105, 85),
            (108, 89),
        ),
        "effect_delay": 75,
        "effect_anchor": "feet",
        "description": "需要先学习影分身之术。召唤3个由系统控制的影分身，每个在1级继承自身全部属性的10%，持续30秒；2级起每升1级，每个分身额外继承自身属性0.2个百分点，查克拉与精华消耗降低0.1%。1级基础冷却120秒，消耗最大查克拉35%和900点查克拉精华。",
    },
    "0009032": {
        "name": "飞雷神之术·瞬",
        "book_id": 6_000_132,
        "required_skill": 0,
        "target": 1,
        "range": 4,
        "fire_local": 0,
        "cooldown": 20,
        "zq_cost": 300,
        "cooldown_reduction_per_upgrade_seconds": 0.1,
        "cost_reduction_per_upgrade_percent": 0.1,
        "icon_source": "0003007",
        "effect_name": "Skill_000_skill_0009032_effect",
        "effect_dimensions": (
            (110, 85),
            (126, 85),
            (127, 85),
            (130, 83),
            (125, 82),
        ),
        "effect_delay": 65,
        "effect_anchor": "feet",
        "description": "选中同图怪物、敌对玩家或队友后，瞬移至目标身后。本术只改变位置，不造成伤害。首次成功释放后5秒内可再次释放一次；5秒内未连用则进入冷却，第二次释放后立即进入冷却。1级基础冷却20秒，每次消耗最大查克拉5%和300点查克拉精华；2级起每升1级，冷却减少0.1秒，查克拉与精华消耗降低0.1%。没有合法目标时不会消耗，也不会进入冷却。",
    },
    "0009033": {
        "name": "飞雷神之术·标",
        "book_id": 6_000_133,
        "required_skill": 9_032,
        "target": 3,
        "range": 1,
        "fire_local": 1,
        "cooldown": 45,
        "zq_cost": 500,
        "cooldown_reduction_per_upgrade_seconds": 0.1,
        "cost_reduction_per_upgrade_percent": 0.1,
        "icon_source": "0003007",
        "effect_name": "Skill_000_skill_0009033_effect",
        "effect_dimensions": ((195, 195),),
        "effect_delay": 180,
        "effect_anchor": "center",
        "description": "需要先学习飞雷神之术·瞬。第一次释放在当前位置留下持续60秒的标记，不消耗资源且不进入冷却；60秒内再次释放会消耗标记并返回标记位置。1级返回时消耗最大查克拉5%和500点查克拉精华，进入45秒冷却；2级起每升1级，冷却减少0.1秒，查克拉与精华消耗降低0.1%。",
    },
}
SKILL_EFFECT_DESCRIPTIONS = {
    "0004005": "忍术作用：对目标造成260%伤害。冷却180秒，消耗最大查克拉25%和500点查克拉精华。",
    "0004006": "忍术作用：持续15秒；前5秒免疫伤害，第5至15秒内死亡会立即原地复活，并恢复到施放时生命与查克拉的一半；复活触发后立即清除此效果，无法重复触发。冷却300秒，消耗最大查克拉35%和1500点查克拉精华。",
    "0004007": "忍术作用：必须选中当前场景内尚未进入复活倒计时的死亡玩家。确认是否控制后进入5秒复活倒计时；选择控制时，目标以原属性十分之一由系统控制30秒，随后再次死亡。冷却300秒，消耗最大查克拉30%和1200点查克拉精华。",
    "0004008": "忍术作用：对目标造成450%伤害，并标记封印30秒；施术者承受同等实际伤害，但最低保留1点生命。冷却240秒，消耗最大查克拉25%和800点查克拉精华。",
    "0004010": "忍术作用：将大范围敌人拉向中心并造成三段伤害，合计360%，结束时击飞目标。冷却180秒，消耗最大查克拉30%和1000点查克拉精华。",
    "0004011": "忍术作用：对范围内敌人造成320%伤害。冷却240秒，消耗最大查克拉35%和1200点查克拉精华。",
    "0004012": "忍术作用：必须选中尚未进入复活倒计时的死亡玩家。有队伍时复活本队所有合法死亡队友，无队伍时复活当前场景内所有合法死亡玩家，均保留5秒复活倒计时；消耗和冷却按实际复活人数倍增。每人基础冷却180秒，消耗最大查克拉30%和1500点查克拉精华。",
    "0009029": "忍术作用：基础伤害150%，每级增加1.5%；基础击退200码，每级增加10码。冷却30秒，每级消耗5点查克拉精华。",
}
SKILL_EFFECT_DESCRIPTIONS.update(
    {
        skill_id: spec["description"]
        for skill_id, spec in PROJECT_SPECIAL_SKILLS.items()
    }
)

# Nya 2.1.1 authors slots 6-20. The client renders 30 slots, so the project
# continues the same ten-cash-per-step curve only for the absent final rows.
PROJECT_SPIRIT_PACK_EXTENSIONS = {
    str(index): index * 10 for index in range(16, 26)
}


def _decode_section(value: Any) -> tuple[dict[str, Any], bool]:
    if isinstance(value, dict):
        return value, False
    if not isinstance(value, bytes):
        raise ValueError("Skill/000 is not an AMF object or compressed AMF byte array")
    payload = zlib.decompress(value)
    reader = Amf3Reader(payload)
    decoded = reader.read_value()
    if not isinstance(decoded, dict) or reader.pos != len(payload):
        raise ValueError("Skill/000 is not a complete AMF object")
    return decoded, True


def _project_effect_frames(spec: dict[str, Any]) -> dict[str, Any]:
    effect_name = str(spec["effect_name"])
    dimensions = tuple(spec["effect_dimensions"])
    anchor = str(spec.get("effect_anchor") or "feet")
    return {
        str(index): {
            "value": {
                "className": f"{effect_name}_{index}",
                "swfName": effect_name,
            },
            "origin": {
                "x": width // 2,
                "y": height // 2 if anchor == "center" else height,
            },
            "delay": int(spec["effect_delay"]),
        }
        for index, (width, height) in enumerate(dimensions)
    }


def _project_icon(skill_id: str) -> dict[str, Any]:
    swf_name = f"Skill_000_skill_{skill_id}"
    return {
        "value": {
            "className": f"{swf_name}_icon",
            "swfName": swf_name,
        },
        "origin": {"x": 17, "y": 17},
        "z": 0,
    }


def _ensure_project_spirit_pack_extensions(root: dict[str, Any]) -> int:
    spirit_value = root.get("Spirit")
    if spirit_value is None:
        raise ValueError("GameData is missing Spirit")
    spirit, spirit_compressed = _decode_section(spirit_value)
    common_value = spirit.get("common")
    if common_value is None:
        raise ValueError("GameData is missing Spirit/common")
    common, common_compressed = _decode_section(common_value)
    extend_value = common.get("extend")
    if extend_value is None:
        raise ValueError("GameData is missing Spirit/common/extend")
    extend, extend_compressed = _decode_section(extend_value)
    changed = 0
    for key, value in PROJECT_SPIRIT_PACK_EXTENSIONS.items():
        if key not in extend:
            extend[key] = value
            changed += 1
    if extend_compressed:
        common["extend"] = zlib.compress(encode_value(extend), 9)
    if common_compressed:
        spirit["common"] = zlib.compress(encode_value(common), 9)
    if spirit_compressed:
        root["Spirit"] = zlib.compress(encode_value(spirit), 9)
    return changed


def _ensure_project_special_skills(
    root: dict[str, Any],
    skills: dict[str, Any],
) -> int:
    """Add project rows and keep their project-owned visual fields current."""
    changed = 0
    for skill_id, spec in PROJECT_SPECIAL_SKILLS.items():
        existing = skills.get(skill_id)
        if isinstance(existing, dict):
            effect = _project_effect_frames(spec)
            icon = _project_icon(skill_id)
            if existing.get("effect") != effect:
                existing["effect"] = effect
                changed += 1
            if existing.get("icon") != icon:
                existing["icon"] = icon
                changed += 1
            info = existing.get("info")
            if isinstance(info, dict):
                expected_cooldown = {
                    "cdBase": spec["cooldown"]
                    + spec.get("cooldown_reduction_per_upgrade_seconds", 0),
                    "cdLevel": -spec.get(
                        "cooldown_reduction_per_upgrade_seconds",
                        0,
                    ),
                }
                if info.get("cool_down") != expected_cooldown:
                    info["cool_down"] = expected_cooldown
                    changed += 1
                if info.get("consumeZqLevel") != spec["zq_cost"]:
                    info["consumeZqLevel"] = spec["zq_cost"]
                    changed += 1
            continue
        open_info: dict[str, Any] = {
            "reqLevel": 1,
            "reqBook": str(spec["book_id"]),
        }
        if spec["required_skill"]:
            open_info["reqSkill"] = spec["required_skill"]
        skills[skill_id] = {
            "projectExtension": "nya-special-v1",
            "icon": _project_icon(skill_id),
            "effect": _project_effect_frames(spec),
            "info": {
                "type_attack": {
                    "trigger": {"probBase": 100, "probLevel": 0},
                    "effect": {
                        "type": "attack",
                        "valueBase": 0,
                        "valueLevel": 0,
                        "timeBase": "0",
                        "timeLevel": 0,
                    },
                },
                "desc": {
                    "skillType": 0,
                    "range": spec["range"],
                    "target": spec["target"],
                    "name": spec["name"],
                    "desc": spec["name"],
                    "oppositeSkill": "",
                },
                "cool_down": {
                    "cdBase": spec["cooldown"]
                    + spec.get("cooldown_reduction_per_upgrade_seconds", 0),
                    "cdLevel": -spec.get(
                        "cooldown_reduction_per_upgrade_seconds",
                        0,
                    ),
                },
                # MP is percentage-based and therefore validated by the server.
                "consume": {"1": 0},
                "consumeZqLevel": spec["zq_cost"],
                "open": open_info,
                "fireLocal": spec["fire_local"],
            },
        }
        changed += 1

    string_root = root.get("String")
    if not isinstance(string_root, dict) or string_root.get("Skill") is None:
        raise ValueError("GameData is missing String/Skill")
    skill_strings, skill_strings_compressed = _decode_section(string_root["Skill"])
    for skill_id, spec in PROJECT_SPECIAL_SKILLS.items():
        if skill_id not in skill_strings:
            skill_strings[skill_id] = {
                "name": spec["name"],
                "desc": spec["description"],
                "buffDesc": spec["name"],
                "gain": "通过对应特别忍术秘籍学习。",
            }
            changed += 1
    if skill_strings_compressed:
        string_root["Skill"] = zlib.compress(encode_value(skill_strings), 9)

    books = root.get("JiNengShu")
    if not isinstance(books, dict):
        raise ValueError("GameData is missing JiNengShu")
    source_book_raw = books.get("06000129")
    source_book, _ = _decode_section(source_book_raw)
    if not isinstance(source_book, dict):
        raise ValueError("GameData is missing project book template 06000129")
    if string_root.get("JiNengShu") is None:
        raise ValueError("GameData is missing String/JiNengShu")
    book_strings, book_strings_compressed = _decode_section(
        string_root["JiNengShu"]
    )
    for skill_id, spec in PROJECT_SPECIAL_SKILLS.items():
        book_key = f"{spec['book_id']:08d}"
        if book_key not in books:
            book = copy.deepcopy(source_book)
            book["spec"]["skillid"] = int(skill_id)
            book["info"]["reqLevel"] = 1
            book["info"]["dropGroup"] = 0
            book["info"]["projectExtension"] = "nya-special-v1"
            books[book_key] = zlib.compress(encode_value(book), 9)
            changed += 1
        string_key = str(spec["book_id"])
        if string_key not in book_strings:
            book_strings[string_key] = {
                "name": f"{spec['name']}秘籍",
                "desc": f"使用后可习得《{spec['name']}》。",
            }
            changed += 1
    if book_strings_compressed:
        string_root["JiNengShu"] = zlib.compress(
            encode_value(book_strings), 9
        )
    return changed


def _replace_resource_names(value: Any, replacements: dict[str, str]) -> int:
    changed = 0
    if isinstance(value, dict):
        for key, child in value.items():
            if isinstance(child, str):
                updated = child
                for old, new in replacements.items():
                    updated = updated.replace(old, new)
                if updated != child:
                    value[key] = updated
                    changed += 1
            else:
                changed += _replace_resource_names(child, replacements)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            if isinstance(child, str):
                updated = child
                for old, new in replacements.items():
                    updated = updated.replace(old, new)
                if updated != child:
                    value[index] = updated
                    changed += 1
            else:
                changed += _replace_resource_names(child, replacements)
    return changed


def _shenluo_effect_frames() -> dict[str, Any]:
    return {
        str(index): {
            "value": {
                "className": f"{SHENLUO_EFFECT_NEW}_{index}",
                "swfName": SHENLUO_EFFECT_NEW,
            },
            "origin": dict(SHENLUO_EFFECT_ORIGIN),
            "delay": SHENLUO_FRAME_DELAY_MS,
        }
        for index in range(14)
    }


def _shenluo_hit_frames() -> dict[str, Any]:
    frames: dict[str, Any] = {
        str(index): {
            "value": {
                "className": f"{SHENLUO_HIT_NEW}_{index}",
                "swfName": SHENLUO_HIT_NEW,
            },
            "origin": dict(SHENLUO_HIT_ORIGIN),
            "delay": SHENLUO_FRAME_DELAY_MS,
        }
        for index in range(7)
    }
    frames["hitAfter"] = 800
    return {"0": frames}


def _izanagi_effect_frames() -> dict[str, Any]:
    return {
        str(index): {
            "value": {
                "className": f"{IZANAGI_EFFECT}_{index}",
                "swfName": IZANAGI_EFFECT,
            },
            "origin": dict(IZANAGI_EFFECT_ORIGIN),
            "delay": IZANAGI_FRAME_DELAY_MS,
        }
        for index in range(11)
    }


def _load_legacy_visuals(skills: dict[str, Any]) -> dict[str, Any]:
    if not LEGACY_VISUAL_SOURCE.is_file():
        raise ValueError(
            f"missing legacy visual compatibility source: {LEGACY_VISUAL_SOURCE}"
        )
    source = json.loads(LEGACY_VISUAL_SOURCE.read_text(encoding="utf-8"))
    section = source.get("Skill", {}).get("000", {}).get("skill", {})
    if not isinstance(section, dict):
        raise ValueError("legacy visual compatibility source is missing Skill/000/skill")
    return section


def _fill_forbidden_skill_visuals(skills: dict[str, Any]) -> int:
    """Fill absent visuals without replacing any Nya 2.1.1 authored value."""
    changed = 0
    needs_legacy_visuals = any(
        isinstance(skills.get(skill_id), dict)
        and any(field not in skills[skill_id] for field in fields)
        for skill_id, fields in LEGACY_VISUAL_SKILLS.items()
    )
    legacy_skills = _load_legacy_visuals(skills) if needs_legacy_visuals else {}
    for skill_id, fields in LEGACY_VISUAL_SKILLS.items():
        skill = skills.get(skill_id)
        legacy = legacy_skills.get(skill_id)
        if not isinstance(skill, dict):
            raise ValueError(f"GameData is missing skill {skill_id}")
        if not isinstance(legacy, dict) and any(field not in skill for field in fields):
            raise ValueError(f"legacy visual source is missing skill {skill_id}")
        for field in fields:
            if field in skill:
                continue
            value = legacy.get(field) if isinstance(legacy, dict) else None
            if not isinstance(value, dict):
                raise ValueError(
                    f"legacy visual source is missing {skill_id}/{field}"
                )
            skill[field] = copy.deepcopy(value)
            changed += 1

    for skill_id, mappings in PROJECT_VISUAL_FALLBACKS.items():
        skill = skills.get(skill_id)
        if not isinstance(skill, dict):
            raise ValueError(f"GameData is missing skill {skill_id}")
        for field, (source_id, source_field) in mappings.items():
            if field in skill:
                continue
            source_value = skills.get(source_id, {}).get(source_field)
            if not isinstance(source_value, dict):
                raise ValueError(
                    f"visual fallback source is missing {source_id}/{source_field}"
                )
            skill[field] = copy.deepcopy(source_value)
            changed += 1

    izanagi = skills.get("0004006")
    if not isinstance(izanagi, dict):
        raise ValueError("GameData is missing skill 0004006")
    izanagi_effect = _izanagi_effect_frames()
    if izanagi.get("effect") != izanagi_effect:
        izanagi["effect"] = izanagi_effect
        changed += 1

    # Keep the authored split Chibaku animation and the lightweight Gedo
    # fallback. The freeze was caused by pull/knock-up BeatBackEffect packets,
    # not these frame resources; movement and damage remain server-authoritative.
    gedo = skills.get("0004011")
    if isinstance(gedo, dict) and "hit" in gedo:
        gedo.pop("hit", None)
        changed += 1

    for skill_id in LOCAL_SELF_CAST_SKILLS:
        skill = skills.get(skill_id)
        info = skill.get("info") if isinstance(skill, dict) else None
        if not isinstance(info, dict):
            raise ValueError(f"GameData skill {skill_id} is missing info")
        if info.get("fireLocal") != 1:
            info["fireLocal"] = 1
            changed += 1
    for skill_id in SERVER_TARGETED_SKILLS:
        skill = skills.get(skill_id)
        info = skill.get("info") if isinstance(skill, dict) else None
        if not isinstance(info, dict):
            raise ValueError(f"GameData skill {skill_id} is missing info")
        if info.get("fireLocal") != 0:
            info["fireLocal"] = 0
            changed += 1
    for skill_id, descriptor in PROJECT_TARGET_DESCRIPTORS.items():
        skill = skills.get(skill_id)
        info = skill.get("info") if isinstance(skill, dict) else None
        desc = info.get("desc") if isinstance(info, dict) else None
        if not isinstance(desc, dict):
            raise ValueError(f"GameData skill {skill_id} is missing info/desc")
        for field, value in descriptor.items():
            if desc.get(field) != value:
                desc[field] = value
                changed += 1
    return changed


def _patch_skill_descriptions(root: dict[str, Any]) -> int:
    """Append audited project mechanics without discarding Nya's lore text."""
    string_root = root.get("String")
    if not isinstance(string_root, dict) or string_root.get("Skill") is None:
        raise ValueError("GameData is missing String/Skill")
    strings, was_compressed = _decode_section(string_root["Skill"])
    changed = 0
    for skill_id, effect_description in SKILL_EFFECT_DESCRIPTIONS.items():
        row = strings.get(skill_id)
        if not isinstance(row, dict):
            raise ValueError(f"GameData String/Skill is missing {skill_id}")
        project_description = skill_id in PROJECT_SPECIAL_SKILLS
        lore = str(row.get("desc") or "").split("\n项目实际效果：", 1)[0]
        lore = lore.split("\n忍术作用：", 1)[0].rstrip()
        updated = (
            effect_description
            if project_description
            else f"{lore}\n{effect_description}" if lore else effect_description
        )
        if row.get("desc") != updated:
            row["desc"] = updated
            changed += 1
    if was_compressed:
        string_root["Skill"] = zlib.compress(encode_value(strings), 9)
    return changed


def patch_game_data(path: Path) -> bool:
    root = read_amf(path)
    skill_root = root.get("Skill")
    if not isinstance(skill_root, dict) or "000" not in skill_root:
        raise ValueError("GameData is missing Skill/000")
    section, was_compressed = _decode_section(skill_root["000"])
    skills = section.get("skill")
    skill = skills.get("0004009") if isinstance(skills, dict) else None
    if not isinstance(skill, dict):
        raise ValueError("GameData is missing skill 0004009")

    changed = _ensure_project_special_skills(root, skills)
    changed += _ensure_project_spirit_pack_extensions(root)
    changed += _fill_forbidden_skill_visuals(skills)
    changed += _patch_skill_descriptions(root)
    changed += _replace_resource_names(
        skill,
        {
            SHENLUO_EFFECT_OLD: SHENLUO_EFFECT_NEW,
            SHENLUO_HIT_OLD: SHENLUO_HIT_NEW,
        },
    )
    info = skill.get("info")
    if not isinstance(info, dict):
        raise ValueError("GameData skill 0004009 is missing info")
    if info.get("fireLocal") != 0:
        # Match Huangquan Swamp: first find an attackable unit in the authored
        # range, then start the cast. A local-fire skill animates before the
        # server can reject an empty target list.
        info["fireLocal"] = 0
        changed += 1
    if info.get("cool_down") != SHENLUO_FIXED_COOLDOWN:
        # This skill always has a five-second cooldown. Replacing the whole
        # table prevents level rows or later client-side bonuses from changing it.
        info["cool_down"] = dict(SHENLUO_FIXED_COOLDOWN)
        changed += 1
    effect = _shenluo_effect_frames()
    if skill.get("effect") != effect:
        skill["effect"] = effect
        changed += 1
    hit = _shenluo_hit_frames()
    if skill.get("hit") != hit:
        skill["hit"] = hit
        changed += 1
    if not changed:
        changed = 0
    if was_compressed:
        skill_root["000"] = zlib.compress(encode_value(section), 9)

    string_root = root.get("String")
    if isinstance(string_root, dict) and string_root.get("Npc") is not None:
        npc_strings, npc_strings_compressed = _decode_section(string_root["Npc"])
        welfare = npc_strings.get("00000076")
        if isinstance(welfare, dict) and welfare.get("func") != "福利官":
            welfare["func"] = "福利官"
            changed += 1
        if npc_strings_compressed:
            string_root["Npc"] = zlib.compress(encode_value(npc_strings), 9)

    etc_root = root.get("Etc")
    if isinstance(etc_root, dict) and etc_root.get("PaiHangBang") is not None:
        rankings, rankings_compressed = _decode_section(etc_root["PaiHangBang"])
        ranking_names = {
            "19": "美女榜",
            "20": "玩家代表榜",
            "21": "帅哥榜",
        }
        for ranking_id, name in ranking_names.items():
            ranking = rankings.get(ranking_id)
            if not isinstance(ranking, dict):
                continue
            for field_name in ("name", "title"):
                if ranking.get(field_name) != name:
                    ranking[field_name] = name
                    changed += 1
        if rankings_compressed:
            etc_root["PaiHangBang"] = zlib.compress(encode_value(rankings), 9)

    if not changed:
        return False

    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    os.close(handle)
    temporary = Path(temporary_name)
    try:
        write_amf(temporary, root)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path, default=[DEFAULT_GAME_DATA])
    args = parser.parse_args()
    for path in args.paths:
        changed = patch_game_data(path)
        print(f"{path}: {'updated' if changed else 'already current'}")


if __name__ == "__main__":
    main()
