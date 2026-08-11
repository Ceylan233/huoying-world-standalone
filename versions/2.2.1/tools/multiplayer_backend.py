#!/usr/bin/env python3
"""Persistent accounts and lightweight multiplayer metadata for the local server."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MULTIPLAYER_ROOT = Path(
    os.environ.get(
        "NARUTO_MULTIPLAYER_ROOT",
        str(PROJECT_ROOT / "save" / "multiplayer"),
    )
)
ACCOUNT_PATH = MULTIPLAYER_ROOT / "accounts.json"
CHARACTER_ROOT = MULTIPLAYER_ROOT / "characters"
LEGACY_SAVE_PATH = PROJECT_ROOT / "save" / "singleplayer.json"
PASSWORD_ITERATIONS = 260_000
NATIVE_LAUNCH_TOKEN_TTL_SECONDS = 12 * 60 * 60.0
NATIVE_LAUNCH_TOKEN_MAX_USES = 128
MAX_CHARACTERS_PER_ACCOUNT = 1
TUTOR_STUDENT_MIN_LEVEL = 10
TUTOR_LEVEL = 60
TUTOR_MAX_STUDENTS = 5
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_.@-]{3,32}$")
CHARACTER_NAME_PATTERN = re.compile(r"^[^:@&\\/\x00-\x1f]{2,12}$")


def _password_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PASSWORD_ITERATIONS,
    )
    return "$".join(
        (
            "pbkdf2_sha256",
            str(PASSWORD_ITERATIONS),
            base64.urlsafe_b64encode(salt).decode("ascii"),
            base64.urlsafe_b64encode(digest).decode("ascii"),
        )
    )


def _password_matches(password: str, encoded: str) -> bool:
    try:
        algorithm, raw_iterations, raw_salt, raw_digest = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(raw_iterations)
        salt = base64.urlsafe_b64decode(raw_salt.encode("ascii"))
        expected = base64.urlsafe_b64decode(raw_digest.encode("ascii"))
    except (TypeError, ValueError):
        return False
    actual = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        iterations,
    )
    return hmac.compare_digest(actual, expected)


class AccountService:
    """Atomic JSON account storage shared by the HTTP launcher and game server."""

    def __init__(
        self,
        path: Path = ACCOUNT_PATH,
        character_root: Path = CHARACTER_ROOT,
        legacy_save_path: Path = LEGACY_SAVE_PATH,
    ):
        self.path = path
        self.character_root = character_root
        self.legacy_save_path = legacy_save_path
        self.lock = threading.RLock()
        self._native_launch_tokens: dict[str, tuple[int, float, int, str]] = {}
        self.data = self._load_or_bootstrap()

    def _load_or_bootstrap(self) -> dict[str, Any]:
        if self.path.is_file():
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(payload, dict) and isinstance(payload.get("accounts"), dict):
                    payload.setdefault("families", {})
                    payload.setdefault("nextFamilyId", 1)
                    for account in payload["accounts"].values():
                        account.setdefault("friends", [])
                        account.setdefault("blocked", [])
                        account.setdefault("familyId", 0)
                        account.setdefault("spouseAccountId", 0)
                        account.setdefault("bondAt", 0)
                        account.setdefault("bondItem", 0)
                        account.setdefault("bondRequests", [])
                        account.setdefault("bondHalfRingItem", 0)
                        account.setdefault("friendly", {})
                        account.setdefault("flowerSentDay", "")
                        account.setdefault("flowersSentTotal", 0)
                        account.setdefault("flowersReceivedTotal", 0)
                        account.setdefault("flowerReceiveLog", [])
                        account.setdefault("friendRequests", [])
                        account.setdefault("familyInviteId", 0)
                        account.setdefault("offlineMessages", [])
                        account.setdefault("tutorAccountId", 0)
                        account.setdefault("studentAccountIds", [])
                        account.setdefault("tutorRequests", [])
                        account.setdefault("studentRequests", [])
                        account.setdefault("tutorAutoAcceptStudent", 0)
                        account.setdefault("tutorAutoAcceptTutor", 0)
                    accounts_by_id = {
                        int(account.get("id", 0)): account
                        for account in payload["accounts"].values()
                        if int(account.get("id", 0)) > 0
                    }
                    families = payload.setdefault("families", {})
                    valid_family_ids = {
                        int(family_id)
                        for family_id in families
                        if str(family_id).isdigit() and int(family_id) > 0
                    }
                    for account in accounts_by_id.values():
                        family_id = int(account.get("familyId", 0))
                        if family_id not in valid_family_ids:
                            account["familyId"] = 0
                    for raw_family_id, family in families.items():
                        family_id = int(raw_family_id)
                        family["id"] = family_id
                        leader_id = int(family.get("leaderAccountId", 0))
                        members = {
                            int(member_id)
                            for member_id in family.get("members", [])
                            if int(member_id) in accounts_by_id
                        }
                        if leader_id in accounts_by_id:
                            members.add(leader_id)
                            accounts_by_id[leader_id]["familyId"] = family_id
                        elif members and not bool(family.get("system", False)):
                            leader_id = min(members)
                            family["leaderAccountId"] = leader_id
                            accounts_by_id[leader_id]["familyId"] = family_id
                        for member_id in tuple(members):
                            account = accounts_by_id[member_id]
                            account_family_id = int(account.get("familyId", 0))
                            if account_family_id in {0, family_id}:
                                account["familyId"] = family_id
                            elif member_id != leader_id:
                                members.remove(member_id)
                        family["members"] = sorted(members)
                        family.setdefault("applications", [])
                        family["applications"] = sorted(
                            {
                                int(account_id)
                                for account_id in family["applications"]
                                if int(account_id) in accounts_by_id
                                and int(account_id) not in members
                            }
                        )
                        raw_ranks = family.setdefault("ranks", {})
                        family["ranks"] = {
                            str(member_id): max(
                                1,
                                min(4, int(raw_ranks.get(str(member_id), 4))),
                            )
                            for member_id in members
                        }
                        if leader_id in members:
                            family["ranks"][str(leader_id)] = 1
                        raw_nicks = family.setdefault("memberNick", {})
                        family["memberNick"] = {
                            str(member_id): str(raw_nicks.get(str(member_id), ""))[:12]
                            for member_id in members
                            if str(raw_nicks.get(str(member_id), ""))
                        }
                        flag_style = int(family.get("flagStyle", 1000))
                        family["flagStyle"] = (
                            flag_style if 1000 <= flag_style <= 9999 else 1000
                        )
                        family["flagLevel"] = max(
                            1, min(10, int(family.get("flagLevel", 1)))
                        )
                        flag_color = int(family.get("flagColor", 0))
                        family["flagColor"] = (
                            flag_color if 0 <= flag_color <= 0xFFFFFF else 0
                        )
                        family["fund"] = max(0, int(family.get("fund", 0)))
                        raw_materials = family.get("materials", {})
                        family["materials"] = {
                            str(item_id): max(0, int(count))
                            for item_id, count in (
                                raw_materials.items()
                                if isinstance(raw_materials, dict)
                                else ()
                            )
                            if str(item_id).isdigit() and int(count) > 0
                        }
                        family.setdefault("autoAccept", True)
                    payload["nextFamilyId"] = max(
                        int(payload.get("nextFamilyId", 1)),
                        max(valid_family_ids, default=0) + 1,
                    )
                    self.data = payload
                    self._save_locked()
                    return payload
            except (OSError, UnicodeError, json.JSONDecodeError):
                pass
        data = {
            "version": 1,
            "nextAccountId": 1,
            "nextCharacterId": 1001,
            "nextFamilyId": 1,
            "families": {},
            "accounts": {},
        }
        self.data = data
        self._save_locked()
        return data

    def _save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.{threading.get_ident()}."
            f"{secrets.token_hex(4)}.tmp"
        )
        try:
            temporary.write_text(
                json.dumps(self.data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _key(username: str) -> str:
        return username.strip().casefold()

    def register(self, username: str, password: str) -> dict[str, Any]:
        username = username.strip()
        if not USERNAME_PATTERN.fullmatch(username):
            raise ValueError("账号需为 3-32 位字母、数字或 . _ @ -")
        if len(password) < 6 or len(password) > 128:
            raise ValueError("密码需为 6-128 位")
        key = self._key(username)
        with self.lock:
            if key in self.data["accounts"]:
                raise ValueError("账号已存在")
            account_id = int(self.data["nextAccountId"])
            self.data["nextAccountId"] = account_id + 1
            account = {
                "id": account_id,
                "username": username,
                "passwordHash": _password_hash(password),
                "createdAt": int(time.time()),
                "characters": [],
                "friends": [],
                "blocked": [],
                "familyId": 0,
                "spouseAccountId": 0,
                "bondAt": 0,
                "bondItem": 0,
                "bondRequests": [],
                "bondHalfRingItem": 0,
                "friendly": {},
                "flowerSentDay": "",
                "flowersSentTotal": 0,
                "flowersReceivedTotal": 0,
                "flowerReceiveLog": [],
                "friendRequests": [],
                "familyInviteId": 0,
                "offlineMessages": [],
                "tutorAccountId": 0,
                "studentAccountIds": [],
                "tutorRequests": [],
                "studentRequests": [],
                "tutorAutoAcceptStudent": 0,
                "tutorAutoAcceptTutor": 0,
            }
            self.data["accounts"][key] = account
            self._save_locked()
            return self.public_account(account)

    def authenticate(self, username: str, password: str) -> dict[str, Any] | None:
        with self.lock:
            account = self.data["accounts"].get(self._key(username))
            if not account or not _password_matches(password, account.get("passwordHash", "")):
                return None
            return account

    def issue_native_launch_token(
        self,
        account_id: int,
        ttl_seconds: float = NATIVE_LAUNCH_TOKEN_TTL_SECONDS,
        advertise_host: str = "",
    ) -> str:
        """Issue a short-lived, in-memory credential for a direct SWF launch."""
        token = secrets.token_urlsafe(32)
        expires_at = time.monotonic() + max(0.0, float(ttl_seconds))
        with self.lock:
            self._prune_native_launch_tokens_locked()
            # Native Flash reloads the same local movie when its disconnect dialog
            # sends nyaReconnect. Keep the capability usable for that host session;
            # it remains account-bound, in memory only, and expires the same day.
            self._native_launch_tokens[token] = (
                int(account_id),
                expires_at,
                NATIVE_LAUNCH_TOKEN_MAX_USES,
                str(advertise_host).strip(),
            )
        return token

    def consume_native_launch_token(
        self,
        username: str,
        token: str,
    ) -> dict[str, Any] | None:
        """Consume one account-bound login pass from a native host session."""
        with self.lock:
            self._prune_native_launch_tokens_locked()
            issued = self._native_launch_tokens.pop(token, None)
            if issued is None:
                return None
            account_id, expires_at, remaining_uses, advertise_host = issued
            if expires_at <= time.monotonic():
                return None
            account = self.data["accounts"].get(self._key(username))
            if account is None or int(account.get("id", 0)) != account_id:
                return None
            if remaining_uses > 1:
                self._native_launch_tokens[token] = (
                    account_id,
                    expires_at,
                    remaining_uses - 1,
                    advertise_host,
                )
            authenticated = dict(account)
            if advertise_host:
                authenticated["_nativeAdvertiseHost"] = advertise_host
            return authenticated

    def authenticate_game_login(
        self,
        username: str,
        password: str,
    ) -> dict[str, Any] | None:
        """Authenticate either a password or a one-time native launch token."""
        prefix = "launch:"
        if password.startswith(prefix):
            return self.consume_native_launch_token(username, password[len(prefix) :])
        return self.authenticate(username, password)

    def _prune_native_launch_tokens_locked(self) -> None:
        now = time.monotonic()
        expired = [
            token
            for token, (_, expires_at, _, _) in self._native_launch_tokens.items()
            if expires_at <= now
        ]
        for token in expired:
            self._native_launch_tokens.pop(token, None)

    def account_by_id(self, account_id: int) -> dict[str, Any] | None:
        with self.lock:
            return next(
                (
                    account
                    for account in self.data["accounts"].values()
                    if int(account.get("id", 0)) == int(account_id)
                ),
                None,
            )

    def character_owner(self, character_id: int) -> tuple[dict[str, Any], dict[str, Any]] | None:
        with self.lock:
            for account in self.data["accounts"].values():
                for character in account.get("characters", []):
                    if int(character.get("id", 0)) == int(character_id):
                        return account, character
        return None

    def characters(self) -> list[tuple[int, dict[str, Any]]]:
        """Return stable account/character snapshots for server-owned rankings."""
        with self.lock:
            rows = [
                (int(account.get("id", 0)), dict(character))
                for account in self.data["accounts"].values()
                for character in account.get("characters", [])
                if int(character.get("id", 0)) > 0
            ]
        return sorted(rows, key=lambda row: int(row[1]["id"]))

    def character_name_exists(self, name: str) -> bool:
        """Return whether a persisted character already owns this display name."""
        normalized = name.strip().casefold()
        if not normalized:
            return False
        with self.lock:
            return any(
                str(character.get("name", "")).casefold() == normalized
                for account in self.data["accounts"].values()
                for character in account.get("characters", [])
            )

    def character_path(self, character: dict[str, Any]) -> Path:
        return self.character_root / f"{int(character['id'])}.json"

    def create_character(
        self,
        account: dict[str, Any],
        name: str,
        job: int = 1,
        gender: int = 0,
        face: int = 300,
        hair: int = 0,
    ) -> dict[str, Any]:
        name = name.strip()
        if not CHARACTER_NAME_PATTERN.fullmatch(name):
            raise ValueError("角色名需为 2-12 位且不能包含协议分隔符")
        with self.lock:
            if len(account.get("characters", [])) >= MAX_CHARACTERS_PER_ACCOUNT:
                raise ValueError("每个账号只能创建一个角色")
            if self.character_name_exists(name):
                raise ValueError("角色名已存在")
            character_id = int(self.data["nextCharacterId"])
            used_character_ids = {
                int(character.get("id", 0))
                for stored_account in self.data["accounts"].values()
                for character in stored_account.get("characters", [])
            }
            while (
                character_id in used_character_ids
                or (self.character_root / f"{character_id}.json").is_file()
            ):
                character_id += 1
            self.data["nextCharacterId"] = character_id + 1
            normalized_job = int(job)
            if normalized_job in (1, 2, 3, 4):
                normalized_job *= 100
            if normalized_job not in (100, 200, 300, 400):
                normalized_job = 300
            normalized_gender = 1 if normalized_job in (200, 400) else 0
            normalized_face = int(face)
            if normalized_face <= 0 or normalized_face in (100, 200, 300, 400):
                normalized_face = 2 if normalized_gender else 1
            character = {
                "id": character_id,
                "name": name,
                "job": normalized_job,
                "gender": normalized_gender,
                "face": normalized_face,
                "hair": max(0, int(hair)),
                "savePath": str(self.character_root / f"{character_id}.json"),
                "createdAt": int(time.time()),
            }
            account.setdefault("characters", []).append(character)
            self._save_locked()
            return dict(character)

    def update_character_summary(self, character_id: int, **values: Any) -> None:
        owner = self.character_owner(character_id)
        if owner is None:
            return
        _, character = owner
        allowed = {"name", "job", "gender", "face", "hair", "level", "mapId", "line"}
        with self.lock:
            character.update({key: value for key, value in values.items() if key in allowed})
            self._save_locked()

    def rename_character(
        self,
        account_id: int,
        character_id: int,
        new_name: str,
    ) -> dict[str, Any]:
        """Atomically rename one owned character and retain its previous names."""
        normalized_name = new_name.strip()
        if not CHARACTER_NAME_PATTERN.fullmatch(normalized_name):
            raise ValueError("角色名需为 2-12 位且不能包含协议分隔符")
        with self.lock:
            account = self.account_by_id(account_id)
            if account is None:
                raise ValueError("账号不存在")
            character = next(
                (
                    value
                    for value in account.get("characters", [])
                    if int(value.get("id", 0)) == int(character_id)
                ),
                None,
            )
            if character is None:
                raise ValueError("角色不存在")
            if any(
                int(value.get("id", 0)) != int(character_id)
                and str(value.get("name", "")).casefold()
                == normalized_name.casefold()
                for stored_account in self.data["accounts"].values()
                for value in stored_account.get("characters", [])
            ):
                raise ValueError("角色名已存在")
            previous_name = str(character.get("name", "")).strip()
            if previous_name.casefold() == normalized_name.casefold():
                raise ValueError("新角色名不能与当前角色名相同")
            history = [
                str(value)
                for value in character.get("nameHistory", [])
                if str(value).strip()
            ]
            if previous_name and previous_name not in history:
                history.append(previous_name)
            character["name"] = normalized_name
            character["nameHistory"] = history[-20:]
            self._save_locked()
            return dict(character)

    def set_friend(self, account_id: int, target_account_id: int, enabled: bool) -> None:
        account = self.account_by_id(account_id)
        if account is None or account_id == target_account_id:
            return
        with self.lock:
            friends = {int(value) for value in account.setdefault("friends", [])}
            if enabled:
                friends.add(int(target_account_id))
            else:
                friends.discard(int(target_account_id))
            account["friends"] = sorted(friends)
            self._save_locked()

    def request_friend(self, account_id: int, target_account_id: int) -> bool:
        target = self.account_by_id(target_account_id)
        if (
            target is None
            or int(account_id) == int(target_account_id)
            or int(account_id) in {int(value) for value in target.get("blocked", [])}
        ):
            return False
        with self.lock:
            requests = {
                int(value) for value in target.setdefault("friendRequests", [])
            }
            requests.add(int(account_id))
            target["friendRequests"] = sorted(requests)
            self._save_locked()
            return True

    def accept_friend_request(
        self,
        account_id: int,
        requester_account_id: int,
    ) -> bool:
        account = self.account_by_id(account_id)
        if account is None:
            return False
        with self.lock:
            requests = {
                int(value) for value in account.setdefault("friendRequests", [])
            }
            if int(requester_account_id) not in requests:
                return False
            requests.discard(int(requester_account_id))
            account["friendRequests"] = sorted(requests)
            self.set_friend(account_id, requester_account_id, True)
            self.set_friend(requester_account_id, account_id, True)
            self._save_locked()
            return True

    def set_family_invite(self, account_id: int, family_id: int) -> None:
        """Persist the newest family invitation for reconnecting clients."""
        account = self.account_by_id(account_id)
        if account is None:
            return
        with self.lock:
            account["familyInviteId"] = max(0, int(family_id))
            self._save_locked()

    def take_family_invite(self, account_id: int) -> int:
        """Return and clear one persisted family invitation."""
        account = self.account_by_id(account_id)
        if account is None:
            return 0
        with self.lock:
            family_id = max(0, int(account.get("familyInviteId", 0)))
            account["familyInviteId"] = 0
            self._save_locked()
            return family_id

    def set_blocked(
        self,
        account_id: int,
        target_account_id: int,
        enabled: bool,
    ) -> None:
        account = self.account_by_id(account_id)
        if account is None or int(account_id) == int(target_account_id):
            return
        with self.lock:
            blocked = {int(value) for value in account.setdefault("blocked", [])}
            if enabled:
                blocked.add(int(target_account_id))
                self.set_friend(account_id, target_account_id, False)
                self.set_friend(target_account_id, account_id, False)
            else:
                blocked.discard(int(target_account_id))
            account["blocked"] = sorted(blocked)
            self._save_locked()

    def adjust_friendly(
        self,
        account_id: int,
        target_account_id: int,
        amount: int,
    ) -> int:
        """Persist one directed social-friendliness change and return the new value."""
        account = self.account_by_id(account_id)
        if account is None or int(account_id) == int(target_account_id):
            return 0
        with self.lock:
            friendly = account.setdefault("friendly", {})
            key = str(int(target_account_id))
            value = max(0, int(friendly.get(key, 100)) + int(amount))
            friendly[key] = value
            self._save_locked()
            return value

    def add_offline_message(
        self,
        account_id: int,
        from_account_id: int,
        from_character_id: int,
        from_name: str,
        message: str,
    ) -> None:
        account = self.account_by_id(account_id)
        if account is None:
            return
        with self.lock:
            messages = account.setdefault("offlineMessages", [])
            messages.append(
                {
                    "fromAccountId": int(from_account_id),
                    "fromCharacterId": int(from_character_id),
                    "fromName": str(from_name)[:12],
                    "message": str(message)[:200],
                    "time": int(time.time() * 1000),
                }
            )
            account["offlineMessages"] = messages[-100:]
            self._save_locked()

    def take_offline_messages(self, account_id: int) -> list[dict[str, Any]]:
        account = self.account_by_id(account_id)
        if account is None:
            return []
        with self.lock:
            messages = [
                dict(value) for value in account.get("offlineMessages", [])
            ]
            account["offlineMessages"] = []
            self._save_locked()
            return messages

    def _can_set_tutor_locked(
        self,
        student_account_id: int,
        tutor_account_id: int,
    ) -> bool:
        """Reject self-links, duplicate tutors, and cycles in the tutor chain."""
        student = self.account_by_id(student_account_id)
        tutor = self.account_by_id(tutor_account_id)
        student_character = student.get("characters", [{}])[0] if student else {}
        tutor_character = tutor.get("characters", [{}])[0] if tutor else {}
        student_level = int(student_character.get("level", 1))
        tutor_level = int(tutor_character.get("level", 1))
        if (
            student is None
            or tutor is None
            or int(student_account_id) == int(tutor_account_id)
            or int(student.get("tutorAccountId", 0))
            or student_level < TUTOR_STUDENT_MIN_LEVEL
            or student_level >= TUTOR_LEVEL
            or tutor_level < TUTOR_LEVEL
            or len({int(value) for value in tutor.get("studentAccountIds", [])})
            >= TUTOR_MAX_STUDENTS
        ):
            return False
        visited: set[int] = set()
        current_id = int(tutor_account_id)
        while current_id and current_id not in visited:
            if current_id == int(student_account_id):
                return False
            visited.add(current_id)
            current = self.account_by_id(current_id)
            current_id = int(current.get("tutorAccountId", 0)) if current else 0
        return True

    def set_tutor_relation(
        self,
        student_account_id: int,
        tutor_account_id: int,
    ) -> bool:
        """Create one persistent student->tutor relationship atomically."""
        with self.lock:
            if not self._can_set_tutor_locked(student_account_id, tutor_account_id):
                return False
            student = self.account_by_id(student_account_id)
            tutor = self.account_by_id(tutor_account_id)
            if student is None or tutor is None:
                return False
            student["tutorAccountId"] = int(tutor_account_id)
            students = {
                int(value) for value in tutor.setdefault("studentAccountIds", [])
            }
            students.add(int(student_account_id))
            tutor["studentAccountIds"] = sorted(students)
            tutor["tutorRequests"] = [
                int(value)
                for value in tutor.get("tutorRequests", [])
                if int(value) != int(student_account_id)
            ]
            student["studentRequests"] = [
                int(value)
                for value in student.get("studentRequests", [])
                if int(value) != int(tutor_account_id)
            ]
            self._save_locked()
            return True

    def clear_tutor_relation(
        self,
        student_account_id: int,
        tutor_account_id: int = 0,
    ) -> bool:
        """Remove both sides of a persisted tutor relationship."""
        with self.lock:
            student = self.account_by_id(student_account_id)
            if student is None:
                return False
            current_tutor_id = int(student.get("tutorAccountId", 0))
            if not current_tutor_id or (
                int(tutor_account_id) and current_tutor_id != int(tutor_account_id)
            ):
                return False
            tutor = self.account_by_id(current_tutor_id)
            student["tutorAccountId"] = 0
            if tutor is not None:
                tutor["studentAccountIds"] = [
                    int(value)
                    for value in tutor.get("studentAccountIds", [])
                    if int(value) != int(student_account_id)
                ]
            self._save_locked()
            return True

    def request_tutor(
        self,
        student_account_id: int,
        tutor_account_id: int,
    ) -> str:
        """Persist a request from a prospective student to a tutor."""
        with self.lock:
            if not self._can_set_tutor_locked(student_account_id, tutor_account_id):
                return "invalid"
            tutor = self.account_by_id(tutor_account_id)
            if tutor is None:
                return "invalid"
            setting = int(tutor.get("tutorAutoAcceptStudent", 0))
            if setting == 3:
                return "refused"
            if setting == 1:
                return (
                    "accepted"
                    if self.set_tutor_relation(student_account_id, tutor_account_id)
                    else "invalid"
                )
            requests = {
                int(value) for value in tutor.setdefault("tutorRequests", [])
            }
            requests.add(int(student_account_id))
            tutor["tutorRequests"] = sorted(requests)
            self._save_locked()
            return "pending"

    def request_student(
        self,
        tutor_account_id: int,
        student_account_id: int,
    ) -> str:
        """Persist a request from a prospective tutor to a student."""
        with self.lock:
            if not self._can_set_tutor_locked(student_account_id, tutor_account_id):
                return "invalid"
            student = self.account_by_id(student_account_id)
            if student is None:
                return "invalid"
            setting = int(student.get("tutorAutoAcceptTutor", 0))
            if setting == 3:
                return "refused"
            if setting == 1:
                return (
                    "accepted"
                    if self.set_tutor_relation(student_account_id, tutor_account_id)
                    else "invalid"
                )
            requests = {
                int(value) for value in student.setdefault("studentRequests", [])
            }
            requests.add(int(tutor_account_id))
            student["studentRequests"] = sorted(requests)
            self._save_locked()
            return "pending"

    def decide_tutor_request(
        self,
        tutor_account_id: int,
        student_account_id: int,
        accepted: bool,
    ) -> bool:
        """Resolve a student->tutor request as the target tutor."""
        with self.lock:
            tutor = self.account_by_id(tutor_account_id)
            if tutor is None:
                return False
            requests = {
                int(value) for value in tutor.setdefault("tutorRequests", [])
            }
            if int(student_account_id) not in requests:
                return False
            requests.discard(int(student_account_id))
            tutor["tutorRequests"] = sorted(requests)
            if accepted:
                return self.set_tutor_relation(student_account_id, tutor_account_id)
            self._save_locked()
            return True

    def decide_student_request(
        self,
        student_account_id: int,
        tutor_account_id: int,
        accepted: bool,
    ) -> bool:
        """Resolve a tutor->student request as the target student."""
        with self.lock:
            student = self.account_by_id(student_account_id)
            if student is None:
                return False
            requests = {
                int(value) for value in student.setdefault("studentRequests", [])
            }
            if int(tutor_account_id) not in requests:
                return False
            requests.discard(int(tutor_account_id))
            student["studentRequests"] = sorted(requests)
            if accepted:
                return self.set_tutor_relation(student_account_id, tutor_account_id)
            self._save_locked()
            return True

    def set_tutor_auto_setting(
        self,
        account_id: int,
        *,
        accept_student: int | None = None,
        accept_tutor: int | None = None,
    ) -> bool:
        account = self.account_by_id(account_id)
        if account is None:
            return False
        with self.lock:
            if accept_student is not None:
                account["tutorAutoAcceptStudent"] = max(
                    0, min(3, int(accept_student))
                )
            if accept_tutor is not None:
                account["tutorAutoAcceptTutor"] = max(
                    0, min(3, int(accept_tutor))
                )
            self._save_locked()
            return True

    def set_bond(
        self,
        first_account_id: int,
        second_account_id: int,
        item_id: int,
    ) -> bool:
        """Create one exclusive reciprocal bond and persist its start time/ring."""
        first = self.account_by_id(first_account_id)
        second = self.account_by_id(second_account_id)
        if first is None or second is None or first_account_id == second_account_id:
            return False
        with self.lock:
            if int(first.get("spouseAccountId", 0)) or int(
                second.get("spouseAccountId", 0)
            ):
                return False
            started_at = int(time.time() * 1000)
            ring_id = max(0, int(item_id))
            first["spouseAccountId"] = int(second_account_id)
            second["spouseAccountId"] = int(first_account_id)
            first["bondAt"] = second["bondAt"] = started_at
            first["bondItem"] = second["bondItem"] = ring_id
            first["bondHalfRingItem"] = ring_id + 1 if ring_id % 100 == 1 else 0
            second["bondHalfRingItem"] = ring_id + 2 if ring_id % 100 == 1 else 0
            first["bondRequests"] = []
            second["bondRequests"] = []
            first.setdefault("friendly", {})[str(second_account_id)] = max(
                100,
                int(first.setdefault("friendly", {}).get(str(second_account_id), 0)),
            )
            second.setdefault("friendly", {})[str(first_account_id)] = max(
                100,
                int(second.setdefault("friendly", {}).get(str(first_account_id), 0)),
            )
            self._save_locked()
            return True

    def request_bond(
        self,
        proposer_account_id: int,
        target_account_id: int,
        proposer_character_id: int,
        item_id: int,
    ) -> bool:
        """Persist one exclusive bond proposal so reconnecting does not lose it."""
        proposer = self.account_by_id(proposer_account_id)
        target = self.account_by_id(target_account_id)
        if (
            proposer is None
            or target is None
            or proposer_account_id == target_account_id
        ):
            return False
        with self.lock:
            if int(proposer.get("spouseAccountId", 0)) or int(
                target.get("spouseAccountId", 0)
            ):
                return False
            requests = [
                value
                for value in target.setdefault("bondRequests", [])
                if int(value.get("fromAccountId", 0)) != proposer_account_id
            ]
            requests.append(
                {
                    "fromAccountId": int(proposer_account_id),
                    "fromCharacterId": int(proposer_character_id),
                    "itemId": max(0, int(item_id)),
                    "createdAt": int(time.time()),
                }
            )
            target["bondRequests"] = requests[-10:]
            self._save_locked()
            return True

    def upgrade_bond_ring(
        self,
        account_id: int,
        item_id: int,
    ) -> tuple[bool, str, int]:
        """Upgrade both halves of one reciprocal bond to a higher whole-ring stage."""
        account = self.account_by_id(account_id)
        if account is None:
            return False, "invalid_account", 0
        with self.lock:
            spouse_id = int(account.get("spouseAccountId", 0))
            spouse = self.account_by_id(spouse_id)
            if (
                spouse is None
                or int(spouse.get("spouseAccountId", 0)) != int(account_id)
            ):
                return False, "not_bonded", 0
            ring_id = max(0, int(item_id))
            match = re.fullmatch(r"190([1-8])0001", str(ring_id))
            if match is None:
                return False, "invalid_ring", spouse_id
            new_stage = int(match.group(1))
            current_ring = max(0, int(account.get("bondItem", 0)))
            current_match = re.fullmatch(r"190([1-8])0001", str(current_ring))
            current_stage = int(current_match.group(1)) if current_match else 0
            if new_stage <= current_stage:
                return False, "stage_not_higher", spouse_id

            own_side = int(account.get("bondHalfRingItem", 0)) % 100
            spouse_side = int(spouse.get("bondHalfRingItem", 0)) % 100
            if own_side not in {2, 3}:
                own_side = 2
            if spouse_side not in {2, 3} or spouse_side == own_side:
                spouse_side = 3 if own_side == 2 else 2
            account["bondItem"] = spouse["bondItem"] = ring_id
            account["bondHalfRingItem"] = ring_id + own_side - 1
            spouse["bondHalfRingItem"] = ring_id + spouse_side - 1
            self._save_locked()
            return True, "", spouse_id

    def bond_request(
        self,
        target_account_id: int,
        proposer_character_id: int,
    ) -> dict[str, Any] | None:
        target = self.account_by_id(target_account_id)
        if target is None:
            return None
        with self.lock:
            for request in reversed(target.setdefault("bondRequests", [])):
                if int(request.get("fromCharacterId", 0)) == proposer_character_id:
                    return dict(request)
        return None

    def clear_bond_request(
        self,
        target_account_id: int,
        proposer_character_id: int,
    ) -> bool:
        target = self.account_by_id(target_account_id)
        if target is None:
            return False
        with self.lock:
            previous = list(target.setdefault("bondRequests", []))
            target["bondRequests"] = [
                value
                for value in previous
                if int(value.get("fromCharacterId", 0)) != proposer_character_id
            ]
            changed = len(previous) != len(target["bondRequests"])
            if changed:
                self._save_locked()
            return changed

    @staticmethod
    def _flower_day(timestamp: float | None = None) -> str:
        return datetime.fromtimestamp(time.time() if timestamp is None else timestamp).date().isoformat()

    def flower_gift_eligibility(
        self,
        sender_account_id: int,
        receiver_account_id: int,
        timestamp: float | None = None,
    ) -> tuple[bool, str]:
        """Enforce the client's once-per-day send and twenty-receipts-per-day rules."""
        sender = self.account_by_id(sender_account_id)
        receiver = self.account_by_id(receiver_account_id)
        if sender is None or receiver is None or sender_account_id == receiver_account_id:
            return False, "invalid_target"
        today = self._flower_day(timestamp)
        with self.lock:
            if str(sender.get("flowerSentDay", "")) == today:
                return False, "already_sent_today"
            today_receipts = sum(
                1
                for value in receiver.setdefault("flowerReceiveLog", [])
                if str(value.get("day", "")) == today
            )
            if today_receipts >= 20:
                return False, "receiver_daily_limit"
        return True, ""

    def record_flower_gift(
        self,
        sender_account_id: int,
        receiver_account_id: int,
        *,
        count: int,
        friendly: int,
        flower_item_id: int,
        sender_character_id: int,
        sender_name: str,
        sender_face: int,
        sender_job: int,
        sender_level: int,
        delivered: bool = False,
        timestamp: float | None = None,
    ) -> tuple[bool, str]:
        """Persist one flower transaction and its reciprocal friendliness atomically."""
        now = time.time() if timestamp is None else float(timestamp)
        allowed, reason = self.flower_gift_eligibility(
            sender_account_id,
            receiver_account_id,
            now,
        )
        if not allowed:
            return False, reason
        sender = self.account_by_id(sender_account_id)
        receiver = self.account_by_id(receiver_account_id)
        if sender is None or receiver is None:
            return False, "invalid_target"
        with self.lock:
            # Recheck under the mutation lock in case two senders target the same player.
            allowed, reason = self.flower_gift_eligibility(
                sender_account_id,
                receiver_account_id,
                now,
            )
            if not allowed:
                return False, reason
            count = max(1, int(count))
            friendly = max(0, int(friendly))
            sender["flowerSentDay"] = self._flower_day(now)
            sender["flowersSentTotal"] = max(
                0, int(sender.get("flowersSentTotal", 0))
            ) + count
            receiver["flowersReceivedTotal"] = max(
                0, int(receiver.get("flowersReceivedTotal", 0))
            ) + count
            sender.setdefault("friendly", {})[str(receiver_account_id)] = max(
                0,
                int(sender.setdefault("friendly", {}).get(str(receiver_account_id), 100)),
            ) + friendly
            receiver.setdefault("friendly", {})[str(sender_account_id)] = max(
                0,
                int(receiver.setdefault("friendly", {}).get(str(sender_account_id), 100)),
            ) + friendly
            log = receiver.setdefault("flowerReceiveLog", [])
            log.append(
                {
                    "day": self._flower_day(now),
                    "timestamp": int(now),
                    "senderAccountId": int(sender_account_id),
                    "senderCharacterId": int(sender_character_id),
                    "senderName": str(sender_name)[:12],
                    "senderFace": int(sender_face),
                    "senderJob": int(sender_job),
                    "senderLevel": max(1, int(sender_level)),
                    "count": count,
                    "flowerItemId": max(0, int(flower_item_id)),
                    "delivered": bool(delivered),
                }
            )
            receiver["flowerReceiveLog"] = log[-200:]
            self._save_locked()
            return True, ""

    def take_pending_flower_gifts(self, account_id: int) -> list[dict[str, Any]]:
        """Mark and return flower cues that were recorded while the receiver was offline."""
        account = self.account_by_id(account_id)
        if account is None:
            return []
        with self.lock:
            pending = [
                dict(value)
                for value in account.setdefault("flowerReceiveLog", [])
                if isinstance(value, dict) and value.get("delivered") is False
            ]
            if pending:
                for value in account["flowerReceiveLog"]:
                    if isinstance(value, dict) and value.get("delivered") is False:
                        value["delivered"] = True
                self._save_locked()
            return pending

    def flower_snapshot(
        self,
        viewer_account_id: int,
        target_account_id: int,
        timestamp: float | None = None,
    ) -> dict[str, Any] | None:
        """Return the persisted fields consumed by the client's flower panel."""
        target = self.account_by_id(target_account_id)
        if target is None:
            return None
        now = time.time() if timestamp is None else float(timestamp)
        today = datetime.fromtimestamp(now).date()
        current_week = today.isocalendar()[:2]
        with self.lock:
            logs = [
                dict(value)
                for value in target.setdefault("flowerReceiveLog", [])
                if isinstance(value, dict)
            ]
            today_logs = [
                value
                for value in logs
                if str(value.get("day", "")) == today.isoformat()
            ]
            weekly_flowers = 0
            for value in logs:
                try:
                    log_day = datetime.fromisoformat(str(value.get("day", ""))).date()
                except ValueError:
                    continue
                if log_day.isocalendar()[:2] == current_week:
                    weekly_flowers += max(0, int(value.get("count", 0)))
            friendly = 0
            if int(viewer_account_id) != int(target_account_id):
                friendly = int(
                    target.setdefault("friendly", {}).get(
                        str(int(viewer_account_id)),
                        100,
                    )
                )
            return {
                "friendly": max(0, friendly),
                "totalFlowers": max(0, int(target.get("flowersReceivedTotal", 0))),
                "weeklyFlowers": max(0, weekly_flowers),
                "todayReceiveCount": len(today_logs),
                "logs": sorted(
                    today_logs,
                    key=lambda value: int(value.get("timestamp", 0)),
                    reverse=True,
                ),
            }

    def clear_bond(self, account_id: int) -> int:
        """Remove both sides of a bond and return the former spouse account id."""
        account = self.account_by_id(account_id)
        if account is None:
            return 0
        with self.lock:
            spouse_id = int(account.get("spouseAccountId", 0))
            spouse = self.account_by_id(spouse_id)
            for value in (account, spouse):
                if value is None:
                    continue
                value["spouseAccountId"] = 0
                value["bondAt"] = 0
                value["bondItem"] = 0
                value["bondHalfRingItem"] = 0
                value["bondRequests"] = []
            self._save_locked()
            return spouse_id

    def create_family(
        self,
        account_id: int,
        name: str,
        flag_style: int = 1000,
        flag_name: str = "木叶",
        notice: str = "欢迎加入家族",
        flag_color: int = 0,
    ) -> dict[str, Any]:
        account = self.account_by_id(account_id)
        name = name.strip()
        if account is None or not CHARACTER_NAME_PATTERN.fullmatch(name):
            raise ValueError("家族名需为 2-12 位且不能包含协议分隔符")
        with self.lock:
            if int(account.get("familyId", 0)):
                raise ValueError("已经加入家族")
            if any(
                str(family.get("name", "")).casefold() == name.casefold()
                for family in self.data.setdefault("families", {}).values()
            ):
                raise ValueError("家族名已存在")
            family_id = int(self.data.setdefault("nextFamilyId", 1))
            self.data["nextFamilyId"] = family_id + 1
            family = {
                "id": family_id,
                "name": name,
                "leaderAccountId": int(account_id),
                "members": [int(account_id)],
                "notice": notice[:80].replace(":", "："),
                "flagStyle": max(1000, int(flag_style)),
                "flagName": flag_name[:12].replace(":", "："),
                "flagColor": max(0, int(flag_color)),
                "flagLevel": 1,
                "fund": 0,
                "materials": {},
                "applications": [],
                "ranks": {str(account_id): 1},
                "memberNick": {},
                "autoAccept": True,
                "createdAt": int(time.time()),
            }
            self.data["families"][str(family_id)] = family
            account["familyId"] = family_id
            self._save_locked()
            return dict(family)

    def family_by_id(self, family_id: int) -> dict[str, Any] | None:
        with self.lock:
            return self.data.setdefault("families", {}).get(str(int(family_id)))

    def families(self) -> list[dict[str, Any]]:
        with self.lock:
            return [
                dict(family)
                for family in self.data.setdefault("families", {}).values()
            ]

    def ensure_system_family(
        self,
        name: str = "木叶家族",
        flag_style: int = 1000,
        flag_name: str = "木叶旗帜",
        notice: str = "欢迎回家",
    ) -> dict[str, Any]:
        """Create the persistent public starter family once, without a fake member."""
        normalized_name = name.strip().casefold()
        with self.lock:
            for family in self.data.setdefault("families", {}).values():
                if bool(family.get("system", False)):
                    changed = False
                    normalized_values = {
                        "name": name.strip(),
                        "flagName": flag_name[:12].replace(":", "："),
                        "notice": notice[:80].replace(":", "："),
                    }
                    for key, value in normalized_values.items():
                        if family.get(key) != value:
                            family[key] = value
                            changed = True
                    if changed:
                        self._save_locked()
                    return dict(family)
                if str(family.get("name", "")).casefold() == normalized_name:
                    return dict(family)
            family_id = int(self.data.setdefault("nextFamilyId", 1))
            self.data["nextFamilyId"] = family_id + 1
            family = {
                "id": family_id,
                "name": name.strip(),
                "leaderAccountId": 0,
                "members": [],
                "notice": notice[:80].replace(":", "："),
                "flagStyle": max(1000, int(flag_style)),
                "flagName": flag_name[:12].replace(":", "："),
                "flagColor": 0,
                "flagLevel": 1,
                "fund": 0,
                "applications": [],
                "ranks": {},
                "memberNick": {},
                "autoAccept": True,
                "createdAt": int(time.time()),
                "system": True,
            }
            self.data["families"][str(family_id)] = family
            self._save_locked()
            return dict(family)

    def request_family_join(
        self,
        account_id: int,
        family_id: int,
    ) -> tuple[str, dict[str, Any] | None]:
        """Join auto-accept families immediately, otherwise persist an application."""
        account = self.account_by_id(account_id)
        family = self.family_by_id(family_id)
        if account is None or family is None or int(account.get("familyId", 0)):
            return "invalid", family
        with self.lock:
            if bool(family.get("autoAccept", True)):
                self.join_family(account_id, family_id)
                return "joined", family
            applications = {
                int(value) for value in family.setdefault("applications", [])
            }
            applications.add(int(account_id))
            family["applications"] = sorted(applications)
            self._save_locked()
            return "pending", family

    def decide_family_application(
        self,
        family_id: int,
        account_id: int,
        accepted: bool,
    ) -> dict[str, Any] | None:
        family = self.family_by_id(family_id)
        if family is None:
            return None
        with self.lock:
            family["applications"] = [
                int(value)
                for value in family.get("applications", [])
                if int(value) != int(account_id)
            ]
            if accepted:
                self.join_family(account_id, family_id)
            self._save_locked()
            return family

    def update_family(self, family_id: int, **values: Any) -> dict[str, Any] | None:
        family = self.family_by_id(family_id)
        if family is None:
            return None
        allowed = {
            "notice",
            "flagName",
            "flagStyle",
            "flagLevel",
            "autoAccept",
            "fund",
            "ranks",
            "memberNick",
            "materials",
        }
        with self.lock:
            family.update({key: value for key, value in values.items() if key in allowed})
            self._save_locked()
            return family

    def disband_family(self, family_id: int) -> list[int]:
        """Delete a family and clear each member account atomically."""
        with self.lock:
            family = self.family_by_id(family_id)
            if family is None:
                return []
            members = [int(value) for value in family.get("members", [])]
            for account_id in members:
                account = self.account_by_id(account_id)
                if account is not None:
                    account["familyId"] = 0
            self.data.setdefault("families", {}).pop(str(int(family_id)), None)
            self._save_locked()
            return members

    def set_family_leader(
        self,
        family_id: int,
        account_id: int,
    ) -> dict[str, Any] | None:
        family = self.family_by_id(family_id)
        if family is None or int(account_id) not in {
            int(value) for value in family.get("members", [])
        }:
            return None
        with self.lock:
            old_leader = int(family.get("leaderAccountId", 0))
            family["leaderAccountId"] = int(account_id)
            ranks = family.setdefault("ranks", {})
            ranks[str(old_leader)] = 4
            ranks[str(int(account_id))] = 1
            self._save_locked()
            return family

    def join_family(self, account_id: int, family_id: int) -> dict[str, Any] | None:
        account = self.account_by_id(account_id)
        family = self.family_by_id(family_id)
        if account is None or family is None:
            return None
        with self.lock:
            old_family_id = int(account.get("familyId", 0))
            if old_family_id and old_family_id != int(family_id):
                return None
            members = {int(value) for value in family.setdefault("members", [])}
            members.add(int(account_id))
            family["members"] = sorted(members)
            ranks = family.setdefault("ranks", {})
            if int(family.get("leaderAccountId", 0)) == 0:
                family["leaderAccountId"] = int(account_id)
                ranks[str(int(account_id))] = 1
            else:
                ranks.setdefault(str(int(account_id)), 4)
            account["familyId"] = int(family_id)
            self._save_locked()
            return family

    def leave_family(self, account_id: int) -> None:
        account = self.account_by_id(account_id)
        if account is None:
            return
        with self.lock:
            family_id = int(account.get("familyId", 0))
            family = self.family_by_id(family_id)
            account["familyId"] = 0
            if family is not None:
                family["members"] = [
                    int(value)
                    for value in family.get("members", [])
                    if int(value) != int(account_id)
                ]
                family.setdefault("ranks", {}).pop(str(int(account_id)), None)
                family.setdefault("memberNick", {}).pop(str(int(account_id)), None)
                if not family["members"]:
                    self.data["families"].pop(str(family_id), None)
                elif int(family.get("leaderAccountId", 0)) == int(account_id):
                    family["leaderAccountId"] = int(family["members"][0])
                    family.setdefault("ranks", {})[
                        str(int(family["members"][0]))
                    ] = 1
            self._save_locked()

    @staticmethod
    def public_account(account: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": int(account["id"]),
            "username": str(account["username"]),
            "characters": [dict(character) for character in account.get("characters", [])],
            "familyId": int(account.get("familyId", 0)),
            "spouseAccountId": int(account.get("spouseAccountId", 0)),
        }


ACCOUNT_SERVICE = AccountService()
