"""Hot-reloaded, validated no-code configuration shared by editor and game runtime."""

from __future__ import annotations

import copy
import json
import math
import threading
from pathlib import Path
from typing import Any, Mapping


DELETE_MARKER = {"$deleted": True}

WELCOME_ANNOUNCEMENT_DEFAULT = (
    "欢迎来到火影世界，群号:1097398812，本项目为免费开源，禁止售卖牟利，"
    "如果你是付费玩到本游戏，证明你被骗了。"
)

TEXT_DEFAULTS: dict[str, dict[str, Any]] = {
    "welcome": {
        "id": "welcome", "name": "登录及循环防盗公告", "enabled": True,
        "channel": "滚动公告", "text": WELCOME_ANNOUNCEMENT_DEFAULT,
        "repeatIntervalSeconds": 300,
    },
    "no_party_pk": {
        "id": "no_party_pk", "name": "无队伍时切换模式", "enabled": True,
        "channel": "黄色悬浮提示", "text": "当前没有队伍，无法切换为队伍模式",
    },
    "no_family_pk": {
        "id": "no_family_pk", "name": "无家族时切换模式", "enabled": True,
        "channel": "黄色悬浮提示", "text": "当前没有家族，无法切换为家族模式",
    },
    "activity_empty": {
        "id": "activity_empty", "name": "活动无奖励提示", "enabled": True,
        "channel": "黄色悬浮提示", "text": "当前没有可领取的活动奖励。",
    },
}

FORMULA_DEFAULTS: dict[str, dict[str, Any]] = {
    "combat": {
        "id": "combat", "name": "玩家攻击怪物", "enabled": True,
        "kind": "formula", "baseVariable": "attack_minus_defence",
        "baseValue": 0, "add": 0, "multiplier": 1.0,
        "criticalMultiplier": 2.0, "minimum": 1, "maximum": 0,
        "rounding": "四舍五入",
        "notes": "伤害先按客户端技能百分比计算，再应用这里的倍率和上下限。",
    },
    "shop_prices": {
        "id": "shop_prices", "name": "全局商店价格", "enabled": True,
        "kind": "multipliers", "buy": 1.0, "sell": 0.5, "cash": 1.0,
    },
}

BALANCE_DEFAULTS: dict[str, dict[str, Any]] = {
    "meditation": {
        "id": "meditation", "name": "打坐与双修", "enabled": True,
        "singleIntervalSeconds": 30, "dualIntervalSeconds": 25,
        "singleExperienceMultiplier": 1.0, "dualExperienceMultiplier": 1.2,
        "singleEssenceMultiplier": 1.0, "dualEssenceMultiplier": 1.2,
    },
    "item_use": {
        "id": "item_use", "name": "道具使用", "enabled": True,
        "minimumCooldownSeconds": 1.0, "consumeOnSuccessOnly": True,
    },
    "monster_drops": {
        "id": "monster_drops", "name": "怪物掉落与双倍活动", "enabled": True,
        "designVersion": "tiered-field-grind-v3",
        # Field monsters retain renewable ordinary books, fragments, and RouQuan
        # manuals. Ultimate/forbidden books stay outside these field pools.
        "skillBookRatePerMillion": 300,
        "rouquanBookRatePerMillion": 300,
        "fragmentRatePerMillion": 300,
        "spiritRatePerMillion": 20_000,
        "cardRatePerMillion": 3_000,
        # The source is guide-authored; the rate is a conservative local design
        # value so cooking remains renewable without crowding rare drops.
        "foodMaterialRatePerMillion": 20_000,
        "heroCopyInsectRatePer10000": 500,
        "normalItemRatePerMillion": 450_000,
        "doubleDropRateMultiplier": 2.0,
        "doubleExperienceMultiplier": 2.0,
        "weekendDays": "5,6,7",
        "weekendStartTime": "10:00",
        "weekendEndTime": "20:00",
        "holidayDates": "",
        # Per-monster complete replacements authored by the legacy editor.
        # Presence of a monster id means the replacement is enabled; an empty
        # list intentionally suppresses every ordinary money/item/equipment
        # drop while quest-required drops remain handled separately.
        "dropOverrides": {},
        "specialDrops": {
            # The source monsters are guide-authored. Their 0.5% rates are local
            # long-term-grind values and are intentionally not labelled official.
            "2000015": [
                {"itemId": 16000110, "count": 1, "prob10000": 50, "bind": False, "source": "designed-grind-v1"},
            ],
            "2000019": [
                {"itemId": 16000111, "count": 1, "prob10000": 50, "bind": False, "source": "designed-grind-v1"},
            ],
            "2000020": [
                {"itemId": 16000112, "count": 1, "prob10000": 50, "bind": False, "source": "designed-grind-v1"},
            ],
            "2000021": [
                {"itemId": 16000113, "count": 1, "prob10000": 50, "bind": False, "source": "designed-grind-v1"},
            ],
            "2000022": [
                {"itemId": 16000114, "count": 1, "prob10000": 50, "bind": False, "source": "designed-grind-v1"},
            ],
            "2000023": [
                {"itemId": 16000115, "count": 1, "prob10000": 50, "bind": False, "source": "designed-grind-v1"},
            ],
            # 命运抉择精华鼠的固定精华露掉落。该副本的鼠类不是普通野外
            # 怪物，不能依赖普通掉落池，否则会被副本刷新逻辑漏掉。
            "4300000": [
                {"itemId": 15009003, "count": 1, "prob10000": 10000, "bind": False, "source": "gamedata-fate-copy"},
            ],
            # Every row is an independent roll. Dynamic pools resolve only to
            # level-appropriate GameData items; probabilities rise with the
            # scheduled world Boss tier instead of granting endgame loot early.
            "2100001": [
                {"pool": "boss_forging_material", "count": 1, "prob10000": 800, "bind": False, "source": "designed-grind-v1"},
                {"pool": "boss_summon_scroll", "count": 1, "prob10000": 200, "bind": False, "source": "designed-grind-v1"},
                {"pool": "boss_jade_chain", "count": 1, "prob10000": 100, "bind": False, "source": "designed-grind-v1"},
                {"pool": "boss_jade_level1", "count": 1, "prob10000": 250, "bind": False, "source": "designed-grind-v1"},
                {"itemId": 15008002, "count": 1, "prob10000": 100, "bind": False, "source": "designed-grind-v1"},
            ],
            "2100002": [
                {"pool": "boss_forging_material", "count": 1, "prob10000": 900, "bind": False, "source": "designed-grind-v1"},
                {"pool": "boss_summon_scroll", "count": 1, "prob10000": 200, "bind": False, "source": "designed-grind-v1"},
                {"pool": "boss_jade_chain", "count": 1, "prob10000": 100, "bind": False, "source": "designed-grind-v1"},
                {"pool": "boss_jade_level1", "count": 1, "prob10000": 300, "bind": False, "source": "designed-grind-v1"},
                {"itemId": 15008002, "count": 1, "prob10000": 125, "bind": False, "source": "designed-grind-v1"},
            ],
            "2100003": [
                {"pool": "boss_forging_material", "count": 1, "prob10000": 1000, "bind": False, "source": "designed-grind-v1"},
                {"pool": "boss_summon_scroll", "count": 1, "prob10000": 220, "bind": False, "source": "designed-grind-v1"},
                {"pool": "boss_jade_chain", "count": 1, "prob10000": 120, "bind": False, "source": "designed-grind-v1"},
                {"pool": "boss_jade_level1", "count": 1, "prob10000": 350, "bind": False, "source": "designed-grind-v1"},
                {"itemId": 15008002, "count": 1, "prob10000": 150, "bind": False, "source": "designed-grind-v1"},
            ],
            "2100004": [
                {"pool": "boss_forging_material", "count": 1, "prob10000": 1100, "bind": False, "source": "designed-grind-v1"},
                {"pool": "boss_summon_scroll", "count": 1, "prob10000": 250, "bind": False, "source": "designed-grind-v1"},
                {"pool": "boss_jade_chain", "count": 1, "prob10000": 150, "bind": False, "source": "designed-grind-v1"},
                {"pool": "boss_jade_level1", "count": 1, "prob10000": 400, "bind": False, "source": "designed-grind-v1"},
                {"itemId": 15008002, "count": 1, "prob10000": 175, "bind": False, "source": "designed-grind-v1"},
                {"itemId": 15008003, "count": 1, "prob10000": 50, "bind": False, "source": "designed-grind-v1"},
            ],
            "2100005": [
                {"pool": "boss_forging_material", "count": 1, "prob10000": 1200, "bind": False, "source": "designed-grind-v1"},
                {"pool": "boss_summon_scroll", "count": 1, "prob10000": 250, "bind": False, "source": "designed-grind-v1"},
                {"pool": "boss_jade_chain", "count": 1, "prob10000": 175, "bind": False, "source": "designed-grind-v1"},
                {"pool": "boss_jade_level1", "count": 1, "prob10000": 450, "bind": False, "source": "designed-grind-v1"},
                {"itemId": 15008002, "count": 1, "prob10000": 175, "bind": False, "source": "designed-grind-v1"},
                {"itemId": 15008003, "count": 1, "prob10000": 75, "bind": False, "source": "designed-grind-v1"},
            ],
            "2100006": [
                {"pool": "boss_forging_material", "count": 1, "prob10000": 1400, "bind": False, "source": "designed-grind-v1"},
                {"pool": "boss_summon_scroll", "count": 1, "prob10000": 100, "bind": False, "source": "designed-grind-v1"},
                {"pool": "boss_jade_chain", "count": 1, "prob10000": 200, "bind": False, "source": "designed-grind-v1"},
                {"pool": "boss_jade_level1", "count": 1, "prob10000": 500, "bind": False, "source": "designed-grind-v1"},
                {"pool": "boss_world_skill_fragment", "count": 1, "prob10000": 400, "bind": False, "source": "designed-grind-v1"},
                {"itemId": 15008002, "count": 1, "prob10000": 150, "bind": False, "source": "designed-grind-v1"},
                {"itemId": 15008003, "count": 1, "prob10000": 100, "bind": False, "source": "designed-grind-v1"},
            ],
            "2100007": [
                {"pool": "boss_forging_material", "count": 1, "prob10000": 1600, "bind": False, "source": "designed-grind-v1"},
                {"pool": "boss_summon_scroll", "count": 1, "prob10000": 100, "bind": False, "source": "designed-grind-v1"},
                {"pool": "boss_jade_chain", "count": 1, "prob10000": 225, "bind": False, "source": "designed-grind-v1"},
                {"pool": "boss_jade_level1", "count": 1, "prob10000": 550, "bind": False, "source": "designed-grind-v1"},
                {"pool": "boss_world_skill_fragment", "count": 1, "prob10000": 450, "bind": False, "source": "designed-grind-v1"},
                {"itemId": 15008002, "count": 1, "prob10000": 100, "bind": False, "source": "designed-grind-v1"},
                {"itemId": 15008003, "count": 1, "prob10000": 150, "bind": False, "source": "designed-grind-v1"},
                {"itemId": 15008004, "count": 1, "prob10000": 25, "bind": False, "source": "designed-grind-v1"},
            ],
            "2100008": [
                {"pool": "boss_forging_material", "count": 1, "prob10000": 1800, "bind": False, "source": "designed-grind-v1"},
                {"pool": "boss_summon_scroll", "count": 1, "prob10000": 100, "bind": False, "source": "designed-grind-v1"},
                {"pool": "boss_jade_chain", "count": 1, "prob10000": 250, "bind": False, "source": "designed-grind-v1"},
                {"pool": "boss_jade_level1", "count": 1, "prob10000": 600, "bind": False, "source": "designed-grind-v1"},
                {"pool": "boss_world_skill_fragment", "count": 1, "prob10000": 500, "bind": False, "source": "designed-grind-v1"},
                {"itemId": 15008003, "count": 1, "prob10000": 175, "bind": False, "source": "designed-grind-v1"},
                {"itemId": 15008004, "count": 1, "prob10000": 50, "bind": False, "source": "designed-grind-v1"},
            ],
            "2100009": [
                {"pool": "boss_forging_material", "count": 1, "prob10000": 2000, "bind": False, "source": "designed-grind-v1"},
                {"pool": "boss_summon_scroll", "count": 1, "prob10000": 100, "bind": False, "source": "designed-grind-v1"},
                {"pool": "boss_jade_chain", "count": 1, "prob10000": 300, "bind": False, "source": "designed-grind-v1"},
                {"pool": "boss_jade_level1", "count": 1, "prob10000": 700, "bind": False, "source": "designed-grind-v1"},
                {"pool": "boss_world_skill_fragment", "count": 1, "prob10000": 600, "bind": False, "source": "designed-grind-v1"},
                {"itemId": 15008003, "count": 1, "prob10000": 150, "bind": False, "source": "designed-grind-v1"},
                {"itemId": 15008004, "count": 1, "prob10000": 75, "bind": False, "source": "designed-grind-v1"},
            ],
            # The target-version announcement explicitly states this is fixed.
            "2100022": [
                {"itemId": 15029001, "count": 1, "prob10000": 10000, "bind": False, "source": "official-announcement"},
                {"itemId": 16000031, "count": 5, "prob10000": 10000, "bind": False, "source": "player-guide-designed-v1"},
                {"itemId": 15009003, "count": 10, "prob10000": 10000, "bind": False, "source": "player-guide-designed-v1"},
                {"itemId": 15009004, "count": 2, "prob10000": 3500, "bind": False, "source": "designed-grind-v1"},
                {"pool": "boss_forging_material", "count": 1, "prob10000": 2200, "bind": False, "source": "designed-grind-v1"},
                {"pool": "boss_summon_scroll", "count": 1, "prob10000": 125, "bind": False, "source": "designed-grind-v1"},
                {"pool": "boss_jade_chain", "count": 1, "prob10000": 350, "bind": False, "source": "designed-grind-v1"},
                {"pool": "boss_jade_level1", "count": 1, "prob10000": 750, "bind": False, "source": "designed-grind-v1"},
                {"pool": "boss_world_skill_fragment", "count": 1, "prob10000": 700, "bind": False, "source": "designed-grind-v1"},
                {"itemId": 15008004, "count": 1, "prob10000": 100, "bind": False, "source": "designed-grind-v1"},
            ],
            "2100023": [
                {"itemId": 15029001, "count": 1, "prob10000": 5000, "bind": False, "source": "designed-grind-v1"},
                {"itemId": 16000031, "count": 5, "prob10000": 7500, "bind": False, "source": "designed-grind-v1"},
                {"itemId": 15009003, "count": 10, "prob10000": 10000, "bind": False, "source": "designed-grind-v1"},
                {"itemId": 15009004, "count": 2, "prob10000": 5000, "bind": False, "source": "designed-grind-v1"},
                {"pool": "boss_forging_material", "count": 1, "prob10000": 2500, "bind": False, "source": "designed-grind-v1"},
                {"pool": "boss_summon_scroll", "count": 1, "prob10000": 150, "bind": False, "source": "designed-grind-v1"},
                {"pool": "boss_jade_chain", "count": 1, "prob10000": 400, "bind": False, "source": "designed-grind-v1"},
                {"pool": "boss_jade_level1", "count": 1, "prob10000": 800, "bind": False, "source": "designed-grind-v1"},
                {"pool": "boss_world_skill_fragment", "count": 1, "prob10000": 800, "bind": False, "source": "designed-grind-v1"},
                {"itemId": 15008004, "count": 1, "prob10000": 125, "bind": False, "source": "designed-grind-v1"},
                {"itemId": 16000184, "count": 1, "prob10000": 100, "bind": False, "source": "designed-grind-v1"},
            ],
            # Nya 2.1.1 adds the nine-tail soul and its 6000-piece fusion cost,
            # but does not author a renewable source for it. Keep the captured
            # three/four/eight-tail box unchanged and close the progression loop
            # through the scheduled Madara world Boss. This is a project design
            # reward, not a claimed official-server probability.
            "5400099": [
                {"itemId": 16000178, "count": 20, "prob10000": 10000, "bind": False, "scope": "primary_world_boss", "source": "project-designed-nine-tail-progression-v1"},
            ],
        },
    },
}

UI_ENTRY_DEFAULTS: dict[str, dict[str, Any]] = {
    "activity": {
        "id": "activity", "name": "活动", "enabled": True, "visible": True,
        "label": "活动", "order": 10, "target": "活动面板", "badge": "无",
        "unlockConditions": [],
        "tabs": [
            {"id": "limited", "label": "限时大奖", "enabled": True, "order": 10},
            {"id": "new_server", "label": "新服有礼", "enabled": True, "order": 20},
        ],
    },
    "vip": {
        "id": "vip", "name": "VIP", "enabled": True, "visible": True,
        "label": "VIP", "order": 20, "target": "VIP面板", "badge": "状态",
        "unlockConditions": [], "tabs": [],
    },
}

EVENT_TEMPLATE: dict[str, Any] = {
    "id": "template", "name": "新事件", "enabled": False,
    "trigger": "登录", "priority": 100, "cooldownSeconds": 0,
    "conditions": [], "actions": [],
    "notes": "启用后由服务端按触发时机执行；全部条件同时满足才执行。",
}

ITEM_EFFECT_TEMPLATE: dict[str, Any] = {
    "id": "0", "name": "道具效果", "enabled": False,
    "consume": True, "cooldownSeconds": 1.0, "conditions": [], "actions": [],
}

BUFF_TEMPLATE: dict[str, Any] = {
    "id": "custom", "name": "自定义增益", "enabled": False,
    "durationSeconds": 60, "stackMode": "刷新时间", "maximumStacks": 1,
    "modifiers": [], "startText": "", "endText": "",
}


RUNTIME_ENTITY_TYPES = frozenset({
    "game_texts", "balance_rules", "formulas", "activities", "automation_events",
    "item_effects", "buffs", "ui_entries", "exchange_recipes", "npc_shops", "cash_shop",
})


def deep_merge(source: Any, patch: Any) -> Any:
    if isinstance(source, dict) and isinstance(patch, dict):
        result = copy.deepcopy(source)
        for key, value in patch.items():
            result[key] = deep_merge(result[key], value) if key in result else copy.deepcopy(value)
        return result
    return copy.deepcopy(patch)


class RuntimeConfig:
    """Read atomically-written editor overlays with mtime caching and safe fallbacks."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._stamp: tuple[int, int] | None = None
        self._payload: dict[str, Any] = {"overrides": {}}

    def _load(self) -> dict[str, Any]:
        try:
            stat = self.path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            return {"overrides": {}}
        with self._lock:
            if stamp == self._stamp:
                return self._payload
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                return self._payload
            if not isinstance(payload, dict) or not isinstance(payload.get("overrides", {}), dict):
                return self._payload
            self._payload = payload
            self._stamp = stamp
            return self._payload

    def revision(self) -> int:
        return _as_int(self._load().get("revision"), 0)

    def override(self, entity_type: str, entity_id: int | str) -> dict[str, Any]:
        value = self._load().get("overrides", {}).get(entity_type, {}).get(str(entity_id), {})
        if not isinstance(value, dict) or value.get("$deleted") is True:
            return {}
        return copy.deepcopy(value)

    def entity(self, entity_type: str, entity_id: int | str, default: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return deep_merge(dict(default or {}), self.override(entity_type, entity_id))

    def entities(self, entity_type: str) -> dict[str, dict[str, Any]]:
        values = self._load().get("overrides", {}).get(entity_type, {})
        if not isinstance(values, dict):
            return {}
        return {
            str(key): copy.deepcopy(value)
            for key, value in values.items()
            if isinstance(value, dict) and value.get("$deleted") is not True
        }

    def text(self, text_id: str, fallback: str) -> str:
        config = self.entity("game_texts", text_id, TEXT_DEFAULTS.get(text_id))
        if config.get("enabled", True) is False:
            return ""
        value = config.get("text")
        return str(value) if isinstance(value, str) else fallback

    def number(self, entity_type: str, entity_id: str, field: str, fallback: float) -> float:
        value = self.entity(entity_type, entity_id).get(field, fallback)
        try:
            result = float(value)
        except (TypeError, ValueError):
            return fallback
        return result if math.isfinite(result) else fallback

    def formula(self, formula_id: str, value: float, *, critical: bool = False) -> int:
        config = self.entity("formulas", formula_id, FORMULA_DEFAULTS.get(formula_id))
        if not config or config.get("enabled", True) is False:
            return max(0, round(value))
        result = (float(value) + _as_float(config.get("baseValue"), 0.0) + _as_float(config.get("add"), 0.0))
        result *= max(0.0, _as_float(config.get("multiplier"), 1.0))
        if critical:
            result *= max(0.0, _as_float(config.get("criticalMultiplier"), 1.0))
        minimum = max(0, _as_int(config.get("minimum"), 0))
        maximum = max(0, _as_int(config.get("maximum"), 0))
        rounded = math.floor(result) if config.get("rounding") == "向下取整" else math.ceil(result) if config.get("rounding") == "向上取整" else round(result)
        if maximum:
            rounded = min(maximum, rounded)
        return max(minimum, rounded)


def _as_int(value: Any, fallback: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return fallback


def _as_float(value: Any, fallback: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return fallback
    return result if math.isfinite(result) else fallback


def condition_matches(condition: Mapping[str, Any], context: Mapping[str, Any]) -> bool:
    """Evaluate one no-code condition without evaluating user-authored expressions."""
    kind = str(condition.get("type") or "always")
    operator = str(condition.get("operator") or "等于")
    expected = condition.get("value")
    keys = {
        "等级": "level", "VIP等级": "vip_level", "地图": "map_id", "职业": "job",
        "金币": "money", "元宝": "cash", "点券": "coupon", "家族状态": "has_family",
        "队伍状态": "has_party", "道具数量": "item_count", "任务状态": "quest_state",
    }
    if kind == "总是":
        return True
    actual = context.get(keys.get(kind, kind))
    if operator == "等于":
        return str(actual) == str(expected)
    if operator == "不等于":
        return str(actual) != str(expected)
    try:
        left, right = float(actual), float(expected)
    except (TypeError, ValueError):
        return False
    return {
        "大于": left > right, "大于等于": left >= right,
        "小于": left < right, "小于等于": left <= right,
    }.get(operator, False)


def conditions_match(conditions: Any, context: Mapping[str, Any]) -> bool:
    return isinstance(conditions, list) and all(
        isinstance(item, dict) and condition_matches(item, context) for item in conditions
    )
