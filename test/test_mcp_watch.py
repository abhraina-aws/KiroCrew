"""Tests for kiro_crew.mcp_watch — the MCP source-config watcher.

Covers the generation/staleness signal (what drives the dashboard's "restart
sessions to apply" banner), the file-fingerprint snapshot, and the on-change
reaction (sync + probe + handler-cache invalidation).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import mcp_watch


@pytest.fixture(autouse=True)
def _reset_generation(monkeypatch):
    """Isolate the module-level counters per test."""
    monkeypatch.setattr(mcp_watch, "_config_generation", 0)
    monkeypatch.setattr(mcp_watch, "_sessions_reset_generation", 0)


class TestGenerationSignal:
    def test_fresh_gateway_is_not_stale(self) -> None:
        assert mcp_watch.sessions_stale() is False
        assert mcp_watch.current_generation() == 0

    def test_change_arms_staleness(self) -> None:
        mcp_watch.bump_generation()
        assert mcp_watch.sessions_stale() is True
        assert mcp_watch.current_generation() == 1

    def test_reset_clears_staleness(self) -> None:
        mcp_watch.bump_generation()
        mcp_watch.mark_sessions_reset()
        assert mcp_watch.sessions_stale() is False

    def test_change_after_reset_rearms(self) -> None:
        """Per-change semantics: a NEW change after a reset must re-arm."""
        mcp_watch.bump_generation()
        mcp_watch.mark_sessions_reset()
        mcp_watch.bump_generation()
        assert mcp_watch.sessions_stale() is True


class TestSnapshot:
    def test_missing_file_is_a_distinct_state(self, tmp_path, monkeypatch) -> None:
        target = tmp_path / "mcp.json"
        monkeypatch.setattr(mcp_watch, "_watched_paths", lambda: [target])
        absent = mcp_watch._snapshot()
        target.write_text("{}")
        present = mcp_watch._snapshot()
        assert absent != present
        # Appearing and disappearing are BOTH changes.
        target.unlink()
        assert mcp_watch._snapshot() == absent

    def test_content_growth_changes_fingerprint(self, tmp_path, monkeypatch) -> None:
        target = tmp_path / "mcp.json"
        target.write_text('{"mcpServers": {}}')
        monkeypatch.setattr(mcp_watch, "_watched_paths", lambda: [target])
        before = mcp_watch._snapshot()
        target.write_text('{"mcpServers": {"srv": {"command": "x"}}}')
        assert mcp_watch._snapshot() != before

    def test_watched_paths_cover_core_sources(self) -> None:
        """The watcher must at least cover the two core mcp.json scopes."""
        from kiro_crew.mcp_discovery import _mcp_json_paths

        watched = mcp_watch._watched_paths()
        for p in _mcp_json_paths():
            assert Path(p) in watched


class TestOnChange:
    @pytest.mark.asyncio
    async def test_consumed_config_change_bumps_and_refreshes(self, monkeypatch) -> None:
        """A reconcile that changed the consumed config arms the banner."""
        monkeypatch.setattr(
            mcp_watch, "_consumed_fingerprint", MagicMock(return_value=b"after")
        )
        with (
            patch("kiro_crew.mcp_discovery.sync_discovered_servers", return_value=[]) as sync,
            patch("kiro_crew.mcp_discovery.probe_all", new_callable=AsyncMock) as probe,
            patch("kiro_crew.dashboard.handlers.mcp.invalidate_probe_cache") as invalidate,
        ):
            out = await mcp_watch._on_change(b"before")
        sync.assert_called_once()
        probe.assert_awaited_once()
        invalidate.assert_called_once()
        assert mcp_watch.current_generation() == 1
        assert out == b"after", "the post-reconcile hash becomes the next baseline"

    @pytest.mark.asyncio
    async def test_disabled_only_change_still_bumps(self, monkeypatch) -> None:
        """A source entry gaining ``disabled: true`` yields an EMPTY discovery
        delta but still rewrites the agent config — the fingerprint criterion
        must catch it (discovery skips disabled entries by design)."""
        monkeypatch.setattr(
            mcp_watch, "_consumed_fingerprint", MagicMock(return_value=b"without")
        )
        with (
            patch("kiro_crew.mcp_discovery.sync_discovered_servers", return_value=[]),
            patch("kiro_crew.mcp_discovery.probe_all", new_callable=AsyncMock),
            patch("kiro_crew.dashboard.handlers.mcp.invalidate_probe_cache"),
        ):
            await mcp_watch._on_change(b"with-srv")
        assert mcp_watch.sessions_stale() is True

    @pytest.mark.asyncio
    async def test_prepoll_writer_does_not_mask_the_change(self, monkeypatch) -> None:
        """The baseline is loop-carried, not read at reaction time: a writer
        that updated the consumed config BEFORE the poll fired (App Kit
        registration writes it directly) must still produce a bump — reading
        "before" here would compare the pre-updated file to itself."""
        # The file already holds the new content when _on_change runs; only
        # the loop's baseline still remembers the pre-change state.
        monkeypatch.setattr(
            mcp_watch, "_consumed_fingerprint", MagicMock(return_value=b"new-content")
        )
        with (
            patch("kiro_crew.mcp_discovery.sync_discovered_servers", return_value=[]),
            patch("kiro_crew.mcp_discovery.probe_all", new_callable=AsyncMock),
            patch("kiro_crew.dashboard.handlers.mcp.invalidate_probe_cache"),
        ):
            await mcp_watch._on_change(b"old-baseline")
        assert mcp_watch.sessions_stale() is True

    @pytest.mark.asyncio
    async def test_noop_source_touch_does_not_arm_the_banner(self, monkeypatch) -> None:
        """A source change that leaves the consumed config byte-identical must
        not raise a banner whose remedy destroys every live session's state."""
        monkeypatch.setattr(
            mcp_watch, "_consumed_fingerprint", MagicMock(return_value=b"same")
        )
        with (
            patch("kiro_crew.mcp_discovery.sync_discovered_servers", return_value=[]),
            patch("kiro_crew.mcp_discovery.probe_all", new_callable=AsyncMock) as probe,
            patch("kiro_crew.dashboard.handlers.mcp.invalidate_probe_cache") as invalidate,
        ):
            out = await mcp_watch._on_change(b"same")
        assert mcp_watch.current_generation() == 0
        assert mcp_watch.sessions_stale() is False
        assert out == b"same"
        # Freshness work still runs — the probe cache may reference the change.
        probe.assert_awaited_once()
        invalidate.assert_called_once()

    @pytest.mark.asyncio
    async def test_sync_failure_still_bumps_generation(self) -> None:
        """The sources moved and the consumed configs may not have followed —
        a failed reconcile is exactly when the user most needs the banner."""
        with (
            patch(
                "kiro_crew.mcp_discovery.sync_discovered_servers",
                side_effect=RuntimeError("boom"),
            ),
            patch("kiro_crew.mcp_discovery.probe_all", new_callable=AsyncMock),
            patch("kiro_crew.dashboard.handlers.mcp.invalidate_probe_cache"),
        ):
            out = await mcp_watch._on_change(b"baseline")
        assert mcp_watch.current_generation() == 1
        assert out == b"baseline", "a failed reconcile must not advance the baseline"


class TestWatchLoop:
    @pytest.mark.asyncio
    async def test_loop_fires_on_change_once_per_change(self, tmp_path, monkeypatch) -> None:
        target = tmp_path / "mcp.json"
        target.write_text("{}")
        monkeypatch.setattr(mcp_watch, "_watched_paths", lambda: [target])
        monkeypatch.setattr(mcp_watch, "_POLL_SECS", 0.01)

        fired = asyncio.Event()

        async def fake_on_change(baseline: bytes) -> bytes:
            fired.set()
            return baseline

        monkeypatch.setattr(mcp_watch, "_on_change", fake_on_change)
        task = asyncio.create_task(mcp_watch.watch_mcp_sources())
        try:
            await asyncio.sleep(0.05)
            assert not fired.is_set(), "no change yet — must not fire"
            target.write_text('{"mcpServers": {"srv": {"command": "x"}}}')
            await asyncio.wait_for(fired.wait(), timeout=2)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
