"""Admission, rate-limit, and replay policy for OneBot messages."""

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from time import monotonic

from channels.onebot.config import OneBotSettings
from channels.onebot.models import ParsedOneBotMessage


class AdmissionOutcome(str, Enum):
    ALLOW = "allow"
    IGNORE = "ignore"
    RATE_LIMITED = "rate_limited"


@dataclass(slots=True)
class _Bucket:
    tokens: float
    updated_at: float
    last_seen_at: float


class SenderRateLimiter:
    def __init__(
        self,
        *,
        rate_per_minute: int,
        burst: int,
        clock: Callable[[], float] = monotonic,
        idle_ttl_seconds: float = 600,
    ) -> None:
        self._rate_per_second = rate_per_minute / 60
        self._burst = burst
        self._clock = clock
        self._idle_ttl_seconds = idle_ttl_seconds
        self._buckets: dict[tuple[int, int], _Bucket] = {}

    @property
    def tracked_sender_count(self) -> int:
        return len(self._buckets)

    def allow(self, self_id: int, user_id: int) -> bool:
        now = self._clock()
        key = (self_id, user_id)
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _Bucket(
                tokens=float(self._burst),
                updated_at=now,
                last_seen_at=now,
            )
            self._buckets[key] = bucket
        else:
            elapsed = max(0.0, now - bucket.updated_at)
            bucket.tokens = min(
                float(self._burst),
                bucket.tokens + elapsed * self._rate_per_second,
            )
            bucket.updated_at = now
            bucket.last_seen_at = now

        if bucket.tokens < 1:
            return False
        bucket.tokens -= 1
        return True

    def prune(self) -> None:
        now = self._clock()
        expired = [
            key
            for key, bucket in self._buckets.items()
            if now - bucket.last_seen_at > self._idle_ttl_seconds
        ]
        for key in expired:
            self._buckets.pop(key, None)


class OneBotAdmissionPolicy:
    def __init__(
        self,
        settings: OneBotSettings,
        limiter: SenderRateLimiter,
    ) -> None:
        self._settings = settings
        self._limiter = limiter

    def admit(
        self,
        message: ParsedOneBotMessage,
    ) -> AdmissionOutcome:
        if message.message_type == "private":
            if message.user_id not in self._settings.allowed_user_ids:
                return AdmissionOutcome.IGNORE
        elif (
            message.group_id not in self._settings.allowed_group_ids
            or not message.mentioned_bot
        ):
            return AdmissionOutcome.IGNORE

        if not self._limiter.allow(message.self_id, message.user_id):
            return AdmissionOutcome.RATE_LIMITED
        return AdmissionOutcome.ALLOW


class RecentMessageRegistry:
    def __init__(
        self,
        *,
        clock: Callable[[], float] = monotonic,
        ttl_seconds: float = 600,
        max_entries: int = 10_000,
    ) -> None:
        self._clock = clock
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._claims: OrderedDict[str, float] = OrderedDict()

    def claim(self, stable_message_id: str) -> bool:
        self.prune()
        if stable_message_id in self._claims:
            return False
        self._claims[stable_message_id] = (
            self._clock() + self._ttl_seconds
        )
        while len(self._claims) > self._max_entries:
            self._claims.popitem(last=False)
        return True

    def release(self, stable_message_id: str) -> None:
        self._claims.pop(stable_message_id, None)

    def prune(self) -> None:
        now = self._clock()
        expired = [
            message_id
            for message_id, expires_at in self._claims.items()
            if expires_at <= now
        ]
        for message_id in expired:
            self._claims.pop(message_id, None)
