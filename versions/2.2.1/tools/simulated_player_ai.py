#!/usr/bin/env python3
"""Server-owned live players used to make the local multiplayer world feel active."""

from __future__ import annotations

import json
import math
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any


VISIBLE_TICK_SECONDS = (0.18, 0.28)
BACKGROUND_TICK_SECONDS = (9.0, 12.0)
SAVE_SECONDS = 30.0
MEDITATION_REPLAY_SECONDS = 1.5
MAX_TEST_PLAYERS = 5


class NullSocket:
    """Socket-compatible sink for a server-owned session with no remote client."""

    def __init__(self, port: int):
        self.port = int(port)

    def getsockname(self) -> tuple[str, int]:
        return ("127.0.0.1", self.port)

    def sendall(self, _: bytes) -> None:
        return

    def settimeout(self, _: float) -> None:
        return

    def setsockopt(self, *_: Any) -> None:
        return

    def shutdown(self, *_: Any) -> None:
        return

    def close(self) -> None:
        return


@dataclass(frozen=True)
class SimulatedPlayerProfile:
    account_id: int
    character_id: int
    username: str
    behavior: str
    route: tuple[int, ...]
    skill_id: int
    line_id: int = 1

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SimulatedPlayerProfile":
        route = tuple(int(map_id) for map_id in value.get("route", ()) if int(map_id) > 0)
        return cls(
            account_id=int(value["accountId"]),
            character_id=int(value["characterId"]),
            username=str(value["username"]),
            behavior=str(value.get("behavior") or "travel"),
            route=route,
            skill_id=max(0, int(value.get("skillId", 0))),
            line_id=max(1, min(8, int(value.get("line", 1)))),
        )


class SimulatedPlayerActor:
    """One persistent character plus a small real-time behavior state machine."""

    def __init__(self, server: Any, hub: Any, profile: SimulatedPlayerProfile):
        self.server = server
        self.hub = hub
        self.profile = profile
        owner = hub.accounts.character_owner(profile.character_id)
        if owner is None or int(owner[0].get("id", 0)) != profile.account_id:
            raise ValueError(f"simulated character {profile.character_id} has no matching account")
        save_path = hub.accounts.character_path(owner[1])
        if not save_path.is_file():
            raise ValueError(f"simulated character save is missing: {save_path}")
        store = hub.character_store(save_path)
        self.session = server.FakeFlashSession(
            NullSocket(server.SERVER_PORT),
            ("127.0.0.1", 40_000 + profile.character_id % 20_000),
            "127.0.0.1",
            store,
            hub,
        )
        self.session.account_id = profile.account_id
        self.session.account_name = profile.username
        self.session.selected_character_id = profile.character_id
        self.session.line_id = profile.line_id
        self.session.wire_mode = "text"
        self.session.entered_game = True
        self.session.server_simulated = True
        self.session.simulated_behavior = profile.behavior
        self.session.simulated_primary_behavior = profile.behavior
        self.session._prepare_map_objects()
        self._ground_session_position()
        self.random = random.Random(profile.character_id * 7919)
        self.target_object_id = 0
        self.target_x = self.session.x
        self.target_y = self.session.y
        self.next_attack_at = 0.0
        self.next_decision_at = 0.0
        self.next_map_change_at = time.monotonic() + self.random.uniform(50.0, 110.0)
        self.next_save_at = time.monotonic() + self.random.uniform(5.0, SAVE_SECONDS)
        self.next_meditation_replay_at = 0.0
        self.meditation_target_x = self.session.x
        self.meditation_target_y = self.session.y
        self.route_index = 0
        self.last_broadcast_action = 1
        # The manager runs all actors on one thread.  Use the real elapsed time
        # between ticks so a busy tick never makes the next absolute position
        # arrive before the velocity tween has finished on remote clients.
        self.last_tick_at = time.monotonic()
        self.motion_interval_seconds = max(VISIBLE_TICK_SECONDS)
        self.closed = False
        self.hub.enter_world(self.session)
        self._enter_behavior(profile.behavior, time.monotonic())

    @property
    def behavior(self) -> str:
        return str(self.session.simulated_behavior)

    def _broadcast(self, payload: str) -> None:
        for peer in self.hub.visible_peers(self.session):
            if getattr(peer, "server_simulated", False):
                continue
            peer.send_text(payload)

    def real_observer_count(self) -> int:
        return sum(
            1
            for peer in self.hub.visible_peers(self.session)
            if not getattr(peer, "server_simulated", False)
        )

    def close(self) -> None:
        """Persist and remove this server-owned session from the live world."""
        if self.closed:
            return
        self.closed = True
        try:
            with self.session.store.lock:
                self.session.store.save()
            self.hub.accounts.update_character_summary(
                self.session.character.character_id,
                level=self.session.character.level,
                mapId=self.session.character.map_id,
                line=self.session.line_id,
            )
        finally:
            self.hub.leave_world(self.session)
            self.session.entered_game = False

    def _ground_session_position(self) -> None:
        """Keep the simulated authority on the same footholds used by clients."""
        current_map = self.session.current_map
        if current_map is None:
            return
        x, y, foothold = current_map.ground_position(
            self.session.x,
            self.session.y,
            self.session.character.foothold,
        )
        self.session.x = int(x)
        self.session.y = int(y)
        self.session.character.x = self.session.x
        self.session.character.y = self.session.y
        self.session.character.foothold = int(foothold)

    def _enter_behavior(self, behavior: str, now: float) -> None:
        if self.session.healing_active:
            self._broadcast(
                f"{self.server.op(self.server.ServerOpcode.HEAL_STATE)}:1:"
                f"{self.server.op(self.server.HealState.STOPPED)}:"
                f"{self.session.character.character_id}:0"
            )
        self.session.healing_active = False
        self.session.healing_partner_id = 0
        self.session.simulated_behavior = behavior
        self.target_object_id = 0
        self.next_decision_at = now
        if behavior == "meditate":
            self._choose_meditation_target()
            self.next_meditation_replay_at = 0.0

    def _choose_meditation_target(self) -> None:
        current_map = self.session.current_map
        points: list[tuple[int, int]] = []
        if current_map is not None:
            points.extend((npc.x, npc.y) for npc in current_map.npcs)
            points.extend((portal.x, portal.y) for portal in current_map.portals.values())
        if not points:
            self.meditation_target_x = self.session.x
            self.meditation_target_y = self.session.y
            return
        point_index = self.profile.character_id % len(points)
        lane = (self.profile.character_id // max(1, len(points))) % 7 - 3
        base_x, base_y = points[point_index]
        target_x, target_y, _ = current_map.ground_position(
            base_x + lane * 42,
            base_y,
            0,
        )
        self.meditation_target_x = target_x
        self.meditation_target_y = target_y

    def _choose_walk_target(self) -> None:
        current_map = self.session.current_map
        points: list[tuple[int, int]] = []
        if current_map is not None:
            points.extend((npc.x, npc.y) for npc in current_map.npcs)
            points.extend((monster.x, monster.y) for monster in self.session.monsters.values())
            points.extend((portal.x, portal.y) for portal in current_map.portals.values())
        if points:
            self.target_x, self.target_y = self.random.choice(points)
            self.target_x += (self.profile.character_id % 7 - 3) * 24
        else:
            self.target_x = self.session.x + self.random.randint(-260, 260)
            self.target_y = self.session.y

    def _broadcast_move(self, next_x: int, next_y: int) -> bool:
        """Publish one grounded walk step and report whether it moved.

        A target on another foothold can be projected back to the current
        foothold at exactly the same coordinates.  Sending a walk animation
        for that no-op makes remote clients show an AI player running in
        place.  Treat that case as a stop instead of emitting another walk
        packet.
        """
        session = self.session
        previous_x = session.x
        previous_y = session.y
        previous_foothold = int(session.character.foothold)
        if session.current_map is not None:
            next_x, next_y, foothold = session.current_map.ground_position(
                int(next_x),
                int(next_y),
                session.character.foothold,
            )
            session.character.foothold = int(foothold)
        moved = (
            int(next_x) != previous_x
            or int(next_y) != previous_y
            or int(session.character.foothold) != previous_foothold
        )
        if not moved:
            # Do not leave a stale walk animation running forever when the
            # target is outside the current floor or was clamped to an edge.
            if self.last_broadcast_action in (6, 7):
                self._broadcast_action(1, force=True)
            return False
        session.x = int(next_x)
        session.y = int(next_y)
        session.character.x = session.x
        session.character.y = session.y
        session.direction = 1 if session.x >= previous_x else -1
        interval = max(0.05, min(1.0, float(self.motion_interval_seconds)))
        velocity_x = round((session.x - previous_x) / interval)
        # The position has already been projected onto a foothold. Sending the
        # slope delta as vertical velocity makes the remote client's gravity
        # solver fight the foothold and produces the familiar ground-scrape.
        velocity_y = 0
        # MovementList.newState is MovementStatus, not MovementAction. A walk
        # is action 3, encoded as 6 (right) or 7 (left); 3 is a standing state.
        movement_state = 6 if session.direction > 0 else 7
        self.last_broadcast_action = movement_state
        duration_ms = round(interval * 1000)
        payload = ":".join(
            str(value)
            for value in (
                self.server.op(self.server.ServerOpcode.MOVE_PLAYER),
                0,
                session.character.character_id,
                0,
                0,
                session.x,
                session.y,
                velocity_x,
                velocity_y,
                2,
                movement_state,
                duration_ms,
                session.character.foothold,
            )
        )
        self._broadcast(payload)
        return True

    def _broadcast_action(self, action: int, *, force: bool = False) -> None:
        """Publish a zero-distance action transition for remote clients."""
        action = int(action)
        movement_status = (action << 1) | (0 if self.session.direction > 0 else 1)
        if not force and movement_status == self.last_broadcast_action:
            return
        session = self.session
        payload = ":".join(
            str(value)
            for value in (
                self.server.op(self.server.ServerOpcode.MOVE_PLAYER),
                0,
                session.character.character_id,
                0,
                0,
                session.x,
                session.y,
                0,
                0,
                2,
                movement_status,
                0,
                session.character.foothold,
            )
        )
        self._broadcast(payload)
        self.last_broadcast_action = movement_status

    def _walk_toward(self, x: int, y: int, speed: float = 115.0) -> bool:
        current_map = self.session.current_map
        if current_map is not None:
            # Walk on the actor's current floor.  The real client changes
            # floors through ladders/portals; feeding a diagonal target into
            # ground_position instead causes repeated zero-distance steps.
            current_floor = current_map.footholds.get(
                int(self.session.character.foothold)
            )
            target_x, target_y, target_foothold = current_map.ground_position(
                int(x), int(y), 0
            )
            if current_floor is not None and current_floor.x1 != current_floor.x2:
                if target_foothold != current_floor.foothold_id:
                    target_x = max(current_floor.left, min(current_floor.right, int(x)))
                    target_y = current_floor.y_at(target_x)
                x, y = target_x, target_y
        dx = float(x - self.session.x)
        dy = float(y - self.session.y)
        distance = math.hypot(dx, dy)
        if distance <= 18.0:
            if self.last_broadcast_action in (6, 7):
                self._broadcast_action(1, force=True)
            return True
        interval = max(0.05, min(1.0, float(self.motion_interval_seconds)))
        step = min(distance, speed * interval)
        next_x = round(self.session.x + dx / distance * step)
        next_y = round(self.session.y + dy / distance * step)
        moved = self._broadcast_move(next_x, next_y)
        # A floor edge or a non-walkable target is a completed route point;
        # let the behavior choose another target rather than retrying forever.
        return distance <= step + 18.0 or not moved

    def _normal_monsters(self) -> list[Any]:
        result = []
        for monster in self.session.monsters.values():
            if monster.hp <= 0 or monster.respawn_at:
                continue
            definition = self.server.GAME_DATA_CATALOG.get_monster_definition(monster.template_id)
            if definition is None or definition.boss:
                continue
            result.append(monster)
        return result

    def _refresh_due_monsters_without_player_controller(self, now: float) -> None:
        if self.hub.monster_controller(self.session) is not None:
            return
        for monster in self.session.monsters.values():
            if monster.hp > 0 or not monster.respawn_at or now < monster.respawn_at:
                continue
            definition = self.server.GAME_DATA_CATALOG.get_monster_definition(
                monster.template_id
            )
            monster.x = monster.spawn_x
            monster.y = monster.spawn_y
            monster.foothold = monster.spawn_foothold
            self.session._configure_monster_variant(monster, definition)
            monster.respawn_at = 0.0
            monster.target_character_id = 0
            monster.next_move_at = 0.0
            monster.next_attack_at = 0.0
            monster.client_velocity_x = 0
            monster.client_velocity_y = 0
            monster.client_movement_action = 1
            monster.last_movement_broadcast_at = 0.0
            monster.movement_distance_remainder = 0.0
            monster.patrol_moving = False
            monster.skill_statuses.clear()
            self.session._send_visible(self.session._monster_spawn_payload(monster))

    def _tick_farm(self, now: float) -> None:
        self._refresh_due_monsters_without_player_controller(now)
        monsters = self._normal_monsters()
        target = self.session.monsters.get(self.target_object_id)
        if target not in monsters:
            ordered_monsters = sorted(monsters, key=lambda value: value.object_id)
            target = (
                ordered_monsters[self.profile.character_id % len(ordered_monsters)]
                if ordered_monsters
                else None
            )
            self.target_object_id = target.object_id if target is not None else 0
        if target is None:
            if now >= self.next_decision_at:
                self._choose_walk_target()
                self.next_decision_at = now + self.random.uniform(4.0, 9.0)
            self._walk_toward(self.target_x, self.target_y, 95.0)
            return
        approach_x = target.x + (self.profile.character_id % 5 - 2) * 24
        if not self._walk_toward(approach_x, target.y, 130.0):
            return
        if now < self.next_attack_at:
            return
        self.next_attack_at = now + self.random.uniform(1.1, 2.2)
        skill_id = self.profile.skill_id
        self.session._broadcast_player_attack_info(
            character_id=self.session.character.character_id,
            skill_id=skill_id,
            skill_level=max(1, min(20, self.session.character.level // 8)),
            x=self.session.x,
            y=self.session.y,
            monsters=(target.object_id,),
            stance=0,
            direction=self.session.direction,
            tag={"semantic": "simulated_farm"},
        )
        with self.hub.lock:
            if target.hp <= 0 or target.respawn_at:
                return
            requested = max(1, round(target.max_hp * self.random.uniform(0.035, 0.075)))
            damage = self.session._monster_damage_amount(target, requested)
            target.hp -= damage
            self.session._send_visible(
                f"{self.server.op(self.server.ServerOpcode.DAMAGE_MONSTER)}:"
                f"{target.object_id}:{damage}"
            )
            self.session._send_visible(
                f"{self.server.op(self.server.ServerOpcode.SHOW_MONSTER_HP)}:"
                f"{target.object_id}:{round(target.hp * 100 / target.max_hp)}"
            )
            if target.hp == 0:
                self.session._handle_monster_killed(target, critical=False)
                self.target_object_id = 0

    def _tick_meditate(self, now: float) -> None:
        if not self._walk_toward(
            self.meditation_target_x,
            self.meditation_target_y,
            90.0,
        ):
            return
        # The original client renders the sitting body only after receiving
        # the explicit sit action. Sending HEAL_STATE alone creates the aura
        # but leaves a prior/invalid movement state on remote players.
        if not self.session.healing_active:
            self.session.healing_active = True
            self.next_meditation_replay_at = 0.0
        if now < self.next_meditation_replay_at:
            return
        self.next_meditation_replay_at = now + MEDITATION_REPLAY_SECONDS
        self._broadcast_action(7, force=True)
        self._broadcast(
            f"{self.server.op(self.server.ServerOpcode.HEAL_STATE)}:1:"
            f"{self.server.op(self.server.HealState.SINGLE)}:"
            f"{self.session.character.character_id}:0"
        )
        progression = self.session.store.state.progression
        progression.meditation_experience_remainder += max(1, self.session.character.level // 4)
        progression.meditation_chakra_remainder += max(1, self.session.character.level // 12)

    def _tick_walk_behavior(self, now: float, *, speed: float) -> None:
        if now >= self.next_decision_at:
            self._choose_walk_target()
            self.next_decision_at = now + self.random.uniform(7.0, 15.0)
        if self._walk_toward(self.target_x, self.target_y, speed):
            self.next_decision_at = min(self.next_decision_at, now + self.random.uniform(1.5, 4.0))

    def _change_route_map(self, now: float) -> None:
        if not self.profile.route or now < self.next_map_change_at:
            return
        self.route_index = (self.route_index + 1) % len(self.profile.route)
        target_map_id = self.profile.route[self.route_index]
        if target_map_id != self.session.character.map_id:
            self.session._warp_to_map(target_map_id)
            self._ground_session_position()
            self._broadcast_action(1, force=True)
        self.next_map_change_at = now + self.random.uniform(55.0, 125.0)
        self.next_decision_at = now + self.random.uniform(2.0, 6.0)

    def tick(self, now: float) -> None:
        elapsed = max(0.05, min(1.0, now - self.last_tick_at))
        self.last_tick_at = now
        self.motion_interval_seconds = elapsed
        behavior = self.behavior
        if behavior == "farm":
            self._tick_farm(now)
        elif behavior == "meditate":
            self._tick_meditate(now)
        elif behavior == "quest":
            self._tick_walk_behavior(now, speed=105.0)
            self._change_route_map(now)
        else:
            self._tick_walk_behavior(now, speed=120.0)
            self._change_route_map(now)
        if now >= self.next_save_at:
            with self.session.store.lock:
                self.session.store.save()
            self.hub.accounts.update_character_summary(
                self.session.character.character_id,
                level=self.session.character.level,
                mapId=self.session.character.map_id,
                line=self.session.line_id,
            )
            self.next_save_at = now + SAVE_SECONDS + self.random.uniform(0.0, 8.0)


class ManagerState(str, Enum):
    DISABLED = "disabled"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"


@dataclass
class ActorRuntime:
    actor: SimulatedPlayerActor
    next_due_at: float
    state: str = "idle"
    tick_count: int = 0
    error_count: int = 0
    visible_observers: int = 0
    last_tick_at: float = 0.0
    last_delay_ms: float = 0.0
    max_delay_ms: float = 0.0
    last_error: str = ""
    in_flight: bool = False


class SimulatedPlayerManager:
    def __init__(self, server: Any, hub: Any, config_path: Path):
        self.server = server
        self.hub = hub
        self.config_path = config_path
        self.actors: list[SimulatedPlayerActor] = []
        self.runtimes: dict[int, ActorRuntime] = {}
        self.thread: threading.Thread | None = None
        self.executor: ThreadPoolExecutor | None = None
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.state = ManagerState.DISABLED
        self.started_at = 0.0
        self.stopped_at = 0.0
        self.tick_count = 0
        self.error_count = 0
        self.delay_total_ms = 0.0
        self.max_delay_ms = 0.0
        self.last_error = ""

    def _load_profiles(self) -> list[SimulatedPlayerProfile]:
        if not self.config_path.is_file():
            raise ValueError("模拟玩家配置不存在；不会自动创建或恢复旧人机")
        payload = json.loads(self.config_path.read_text(encoding="utf-8"))
        raw_players = payload.get("players", ())
        if not isinstance(raw_players, list):
            raise ValueError("模拟玩家配置 players 必须是数组")
        profiles = [
            SimulatedPlayerProfile.from_dict(value)
            for value in raw_players
            if isinstance(value, dict) and bool(value.get("enabled", True))
        ]
        if not profiles:
            raise ValueError("模拟玩家配置中没有已启用的测试角色")
        if len(profiles) > MAX_TEST_PLAYERS:
            raise ValueError(
                f"第一阶段最多启用 {MAX_TEST_PLAYERS} 个测试角色，当前配置为 {len(profiles)} 个"
            )
        character_ids = [profile.character_id for profile in profiles]
        if len(character_ids) != len(set(character_ids)):
            raise ValueError("模拟玩家配置包含重复角色")
        account_ids = [profile.account_id for profile in profiles]
        if len(account_ids) != len(set(account_ids)):
            raise ValueError("模拟玩家配置包含同一账号的多个角色")
        return profiles

    def _reject_online_character_conflicts(
        self,
        profiles: list[SimulatedPlayerProfile],
    ) -> None:
        with self.hub.lock:
            conflicts = [
                (profile.account_id, profile.character_id)
                for profile in profiles
                if (
                    (
                        session := self.hub.sessions_by_character.get(
                            profile.character_id
                        )
                    )
                    is not None
                    and session.entered_game
                    and not getattr(session, "server_simulated", False)
                )
                or (
                    (
                        account_session := self.hub.sessions_by_account.get(
                            profile.account_id
                        )
                    )
                    is not None
                    and account_session.entered_game
                    and not getattr(account_session, "server_simulated", False)
                )
            ]
        if conflicts:
            raise ValueError(
                "以下测试账号或角色当前由真人在线使用，已拒绝启动："
                + "、".join(
                    f"账号 {account_id} / 角色 {character_id}"
                    for account_id, character_id in conflicts
                )
            )

    def start(self) -> int:
        with self.condition:
            if self.state in {ManagerState.STARTING, ManagerState.RUNNING}:
                return len(self.actors)
            self.state = ManagerState.STARTING
            self.last_error = ""
        created: list[SimulatedPlayerActor] = []
        try:
            profiles = self._load_profiles()
            self._reject_online_character_conflicts(profiles)
            now = time.monotonic()
            for profile in profiles:
                created.append(SimulatedPlayerActor(self.server, self.hub, profile))
            with self.condition:
                self.actors = created
                self.runtimes = {
                    actor.profile.character_id: ActorRuntime(actor=actor, next_due_at=now)
                    for actor in created
                }
                self.started_at = time.time()
                self.stopped_at = 0.0
                self.tick_count = 0
                self.error_count = 0
                self.delay_total_ms = 0.0
                self.max_delay_ms = 0.0
                self.state = ManagerState.RUNNING
                self.executor = ThreadPoolExecutor(
                    max_workers=len(created),
                    thread_name_prefix="simulated-player-actor",
                )
                self.thread = threading.Thread(
                    target=self._run,
                    name="simulated-player-ai",
                    daemon=True,
                )
                self.thread.start()
            print(
                f"[simulated-player-ai] started {len(created)} live players",
                flush=True,
            )
            return len(created)
        except Exception as exc:
            for actor in reversed(created):
                try:
                    actor.close()
                except Exception:
                    pass
            with self.condition:
                self.actors = []
                self.runtimes = {}
                self.last_error = str(exc)
                self.error_count += 1
                self.state = ManagerState.ERROR
            raise

    def stop(self, timeout: float = 5.0) -> int:
        with self.condition:
            if self.state in {ManagerState.DISABLED, ManagerState.STOPPED}:
                self.state = ManagerState.STOPPED
                return 0
            self.state = ManagerState.STOPPING
            thread = self.thread
            executor = self.executor
            actor_count = len(self.actors)
            self.condition.notify_all()
        if thread is not None and thread is not threading.current_thread():
            thread.join(max(0.0, float(timeout)))
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)
        close_errors: list[str] = []
        for actor in tuple(self.actors):
            try:
                actor.close()
            except Exception as exc:
                close_errors.append(str(exc))
        with self.condition:
            self.actors = []
            self.runtimes = {}
            self.thread = None
            self.executor = None
            self.stopped_at = time.time()
            if close_errors:
                self.error_count += len(close_errors)
                self.last_error = close_errors[-1]
                self.state = ManagerState.ERROR
            else:
                self.state = ManagerState.STOPPED
        print(f"[simulated-player-ai] stopped {actor_count} live players", flush=True)
        return actor_count

    def reload(self) -> int:
        self.stop()
        return self.start()

    def _next_interval(self, visible: bool) -> float:
        low, high = VISIBLE_TICK_SECONDS if visible else BACKGROUND_TICK_SECONDS
        return random.uniform(low, high)

    def _run_actor(self, runtime: ActorRuntime, now: float) -> None:
        delay_ms = max(0.0, (now - runtime.next_due_at) * 1000.0)
        runtime.state = "running"
        error_message = ""
        try:
            runtime.visible_observers = runtime.actor.real_observer_count()
            runtime.actor.tick(now)
            runtime.tick_count += 1
            runtime.last_tick_at = time.time()
            runtime.last_delay_ms = delay_ms
            runtime.max_delay_ms = max(runtime.max_delay_ms, delay_ms)
            runtime.state = "idle"
        except Exception as exc:
            runtime.error_count += 1
            runtime.last_error = str(exc)
            runtime.state = "error"
            error_message = str(exc)
            print(
                f"[simulated-player-ai] character={runtime.actor.profile.character_id}: {exc}",
                flush=True,
            )
        finally:
            with self.condition:
                self.tick_count += 1
                self.delay_total_ms += delay_ms
                self.max_delay_ms = max(self.max_delay_ms, delay_ms)
                if error_message:
                    self.error_count += 1
                    self.last_error = error_message
                runtime.next_due_at = time.monotonic() + self._next_interval(
                    runtime.visible_observers > 0
                )
                runtime.in_flight = False
                self.condition.notify_all()

    def _run(self) -> None:
        while True:
            with self.condition:
                if self.state is not ManagerState.RUNNING:
                    return
                available = [
                    runtime
                    for runtime in self.runtimes.values()
                    if not runtime.in_flight
                ]
                if not available:
                    self.condition.wait(1.0)
                    continue
                now = time.monotonic()
                runtime = min(available, key=lambda value: value.next_due_at)
                wait_seconds = runtime.next_due_at - now
                if wait_seconds > 0:
                    self.condition.wait(wait_seconds)
                    continue
                runtime.in_flight = True
                executor = self.executor
            if executor is None:
                with self.condition:
                    runtime.in_flight = False
                    self.condition.notify_all()
                return
            try:
                executor.submit(self._run_actor, runtime, time.monotonic())
            except RuntimeError as exc:
                with self.condition:
                    runtime.in_flight = False
                    self.error_count += 1
                    self.last_error = str(exc)
                    self.condition.notify_all()

    def wake_for_observer(self, session: Any) -> int:
        if getattr(session, "server_simulated", False) or not getattr(
            session, "entered_game", False
        ):
            return 0
        now = time.monotonic()
        woken = 0
        with self.condition:
            if self.state is not ManagerState.RUNNING:
                return 0
            for runtime in self.runtimes.values():
                actor_session = runtime.actor.session
                if (
                    actor_session.line_id == session.line_id
                    and actor_session.character.map_id == session.character.map_id
                    and actor_session.copy_instance_id == session.copy_instance_id
                ):
                    runtime.next_due_at = min(runtime.next_due_at, now)
                    woken += 1
            if woken:
                self.condition.notify_all()
        return woken

    def status(self) -> dict[str, Any]:
        configured_count = 0
        config_error = ""
        if self.config_path.is_file():
            try:
                configured_count = len(self._load_profiles())
            except Exception as exc:
                config_error = str(exc)
        now = time.time()
        monotonic_now = time.monotonic()
        with self.condition:
            actor_rows = [
                {
                    "characterId": runtime.actor.profile.character_id,
                    "name": runtime.actor.session.character.name,
                    "behavior": runtime.actor.behavior,
                    "line": runtime.actor.session.line_id,
                    "mapId": runtime.actor.session.character.map_id,
                    "state": runtime.state,
                    "visibleObservers": runtime.visible_observers,
                    "tickCount": runtime.tick_count,
                    "errorCount": runtime.error_count,
                    "lastDelayMs": round(runtime.last_delay_ms, 2),
                    "maxDelayMs": round(runtime.max_delay_ms, 2),
                    "nextTickInMs": round(max(0.0, runtime.next_due_at - monotonic_now) * 1000),
                    "lastError": runtime.last_error,
                }
                for runtime in self.runtimes.values()
            ]
            return {
                "state": self.state.value,
                "configured": self.config_path.is_file() and not config_error,
                "configPath": str(self.config_path),
                "configuredPlayerCount": configured_count,
                "maxTestPlayers": MAX_TEST_PLAYERS,
                "runningPlayerCount": len(self.actors),
                "visiblePlayerCount": sum(
                    1 for runtime in self.runtimes.values() if runtime.visible_observers > 0
                ),
                "tickCount": self.tick_count,
                "errorCount": self.error_count,
                "averageDelayMs": round(
                    self.delay_total_ms / self.tick_count if self.tick_count else 0.0,
                    2,
                ),
                "maxDelayMs": round(self.max_delay_ms, 2),
                "uptimeSeconds": round(now - self.started_at) if self.started_at else 0,
                "lastError": config_error or self.last_error,
                "actors": actor_rows,
            }


_MANAGER: SimulatedPlayerManager | None = None


def get_simulated_player_manager(server: Any, hub: Any) -> SimulatedPlayerManager:
    global _MANAGER
    if _MANAGER is None:
        config_path = hub.accounts.path.with_name("simulated_players.json")
        _MANAGER = SimulatedPlayerManager(server, hub, config_path)
    return _MANAGER


def start_simulated_players(server: Any, hub: Any) -> SimulatedPlayerManager | None:
    """Start configured actors once after protocol patches have been installed."""
    manager = get_simulated_player_manager(server, hub)
    if not manager.config_path.is_file():
        return None
    manager.start()
    return manager


def notify_real_session_visibility(session: Any) -> int:
    manager = _MANAGER
    return manager.wake_for_observer(session) if manager is not None else 0
