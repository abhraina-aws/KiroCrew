"""Durable record of the resume binding a Discord conversation expects.

Keyed by CHANNEL and kept OUTSIDE the session map, because it has to outlive the entry
whose loss it detects. EVERY public method is a coroutine doing its filesystem work in a
worker thread, and an :class:`asyncio.Lock` serializes the read-modify-write sequences
the offload would otherwise let interleave. Authority for the rationale, state table and
durability rules: ``docs/system-specs/modules/messaging.md`` § Resume-binding.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import config_dir

logger = logging.getLogger(__name__)

#: Under ``trust``, NOT the data-home root: the root is reachable by agent file tools,
#: so an injected agent could delete this record and the session-map entry together and
#: leave nothing to detect. ``trust`` is on ``security._CREW_SECRET_LEAVES``, so the gate
#: refuses agent access while the gateway persists normally.
_TRUST_SUBDIR = "trust"
_FILENAME = "discord_resume_expectations.json"


@dataclass(frozen=True)
class ResumeExpectation:
    """The session a Discord conversation is attached to, and its display title.

    ``version`` identifies THIS record among the channel's succession of records;
    a settle quotes it back so a replaced record is never clobbered.
    """

    key: str
    title: str
    version: int


def _read(path: Path) -> dict[str, ResumeExpectation]:
    """Load the store. Only an ABSENT file is empty; anything else raises, since "no
    records" reads as "never attached" and routes the turn natively."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError) as exc:
        raise ExpectationStoreError(f"could not read {path}: {exc}") from exc
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise ExpectationStoreError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ExpectationStoreError(f"{path} is not a JSON object")
    records: dict[str, ResumeExpectation] = {}
    for channel_id, value in raw.items():
        if not isinstance(value, dict) or not str(value.get("key") or ""):
            raise ExpectationStoreError(f"{path} holds a malformed row for {channel_id}")
        # Every row this store writes carries a version; a missing one is corruption.
        try:
            version = int(value["version"])
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ExpectationStoreError(
                f"{path} holds an unusable version for {channel_id}"
            ) from exc
        records[str(channel_id)] = ResumeExpectation(
            str(value["key"]),
            str(value.get("title") or ""),
            version,
        )
    return records


def _resolve(previous: Path | None) -> tuple[Path, dict[str, ResumeExpectation] | None]:
    """Resolve the store path, and read it when it is not *previous*. Both halves belong
    in one worker-thread call because ``config_dir()`` is itself filesystem work; ``None``
    records means the path was unchanged, so the steady state is a check, not a re-read."""
    path = config_dir() / _TRUST_SUBDIR / _FILENAME
    return (path, None) if path == previous else (path, _read(path))


class ExpectationStoreError(RuntimeError):
    """A record could not be made durable or read, so the caller must not proceed.
    Distinct from a compare-and-set MISS (``False``/``None``: someone else's newer record
    is authoritative) — this means the store does not know what the conversation is
    attached to, so callers refuse rather than route into an undetectable binding."""


def _write(path: Path, records: dict[str, ResumeExpectation]) -> None:
    """Persist the whole store, or raise. Never swallow: see the error above."""
    payload = {
        channel_id: {
            "key": record.key,
            "title": record.title,
            "version": record.version,
        }
        for channel_id, record in records.items()
    }
    try:
        # Owner-only dir, re-asserted per write; the FILE needs restrict_to_owner,
        # since a mode is a no-op on Windows and would leave a title readable.
        path.parent.mkdir(parents=True, exist_ok=True)
        platform_compat.chmod_safe(path.parent, 0o700)
        atomic_write(path, json.dumps(payload, indent=2), restrict_to_owner=True)
    except OSError as exc:
        raise ExpectationStoreError(f"could not persist {path}: {exc}") from exc


class ResumeExpectations:
    """Channel id → :class:`ResumeExpectation`, one small owner-only JSON file, written
    only on attach, rebind or detach. Single-writer: a pod resolves a different home."""

    def __init__(self) -> None:
        self._loaded_from: Path | None = None
        self._records: dict[str, ResumeExpectation] = {}
        self._lock = asyncio.Lock()

    async def get(self, channel_id: str) -> ResumeExpectation | None:
        async with self._lock:
            await self._synced()
            return self._records.get(channel_id)

    async def record(self, channel_id: str, key: str, title: str) -> ResumeExpectation:
        """Replace *channel_id*'s record unconditionally, returning the new one. For
        the paths that ESTABLISH the attachment — the picker's bind and the bootstrap.
        A settle uses :meth:`record_if`, acting on a record it read earlier."""
        async with self._lock:
            return await self._put(channel_id, key, title)

    async def record_if(
        self,
        channel_id: str,
        version: int,
        key: str,
        title: str,
    ) -> None:
        """Replace the record only while it is still at *version*; a lost
        compare-and-set is a no-op, so the newer record survives."""
        async with self._lock:
            await self._synced()
            current = self._records.get(channel_id)
            if current is not None and current.version == version:
                await self._put(channel_id, key, title)

    async def record_if_absent(self, channel_id: str, key: str, title: str) -> None:
        """Write only when *channel_id* has none, so a bind queued behind this lock is
        not overwritten by a restore of the retired key."""
        async with self._lock:
            await self._synced()
            if channel_id not in self._records:
                await self._put(channel_id, key, title)

    async def clear_if(self, channel_id: str, version: int) -> bool:
        """Forget the record only while it is still at *version*. ``False`` is a
        compare-and-set miss; a storage failure raises instead, never confusing them."""
        async with self._lock:
            path = await self._synced()
            current = self._records.get(channel_id)
            if current is None or current.version != version:
                return False
            await self._publish(path, {k: v for k, v in self._records.items() if k != channel_id})
            return True

    async def clear(self, channel_id: str) -> bool:
        """Forget the record outright; True when there was one to forget. Unconditional
        because the caller is the user leaving deliberately (``!unlink``, ``!new``)."""
        async with self._lock:
            path = await self._synced()
            if channel_id not in self._records:
                return False
            await self._publish(path, {k: v for k, v in self._records.items() if k != channel_id})
            return True

    async def _put(self, channel_id: str, key: str, title: str) -> ResumeExpectation:
        """Write a successor record for *channel_id*. Caller holds the lock."""
        path = await self._synced()
        current = self._records.get(channel_id)
        version = (current.version + 1) if current is not None else 1
        record = ResumeExpectation(key, title, version)
        await self._publish(path, {**self._records, channel_id: record})
        return record

    async def _publish(self, path: Path, candidate: dict[str, ResumeExpectation]) -> None:
        """Persist *candidate*, then publish: mutating first makes a failed write report
        success and leaves memory no restart can recover."""
        await asyncio.to_thread(_write, path, candidate)
        self._records = candidate

    async def _synced(self) -> Path:
        """The store path, having reloaded if the data home moved under us."""
        path, records = await asyncio.to_thread(_resolve, self._loaded_from)
        if records is not None:
            self._records = records
            self._loaded_from = path
        return path
