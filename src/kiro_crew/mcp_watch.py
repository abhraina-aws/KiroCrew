"""Watch MCP source configs and keep the consumed configs fresh.

The MCP pipeline has two halves that historically only moved when a user
clicked: SOURCE files (the dashboard store ``mcp.json``, the kiro-global
``~/.kiro/settings/mcp.json``, and any edition-contributed provider globals)
and the CONSUMED files derived from them (the agent config kiro-cli spawns
servers from, the Claude Code sidecar). Nothing reconciled the two
automatically — a server installed or hand-edited into a source file sat
invisible until someone happened to press Apply & Restart Sessions.

This module closes that loop with a small mtime poller:

* every :data:`_POLL_SECS` it snapshots the source files' ``(mtime_ns, size)``;
* on any change it runs :func:`kiro_crew.mcp_discovery.sync_discovered_servers`
  (the one serialized discover→write entry point) and re-probes, so the
  dashboard reflects the new reality without a manual sync;
* it bumps a **config generation** counter.

The generation counter is the honesty half. A kiro-cli session's MCP tool
table is frozen at process spawn — no config edit reaches a live session, ever.
The system cannot change that, but it can stop making the user guess:
:func:`sessions_stale` compares the current generation against the one recorded
when sessions were last reset (:func:`mark_sessions_reset`, called by the
reset-all path), and the dashboard surfaces the mismatch as a "restart to
apply" banner instead of silence.

A poller rather than OS file watching on purpose: three small files at a
15-second cadence costs nothing, needs no per-platform backend (inotify /
FSEvents / ReadDirectoryChangesW), and cannot miss an editor's
atomic-rename-over dance the way a naive inode watch can.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path

from kiro_crew import agent, mcp_discovery

logger = logging.getLogger(__name__)

#: Seconds between source-file snapshots. Low enough that "install → shows up"
#: feels immediate; high enough that the stat cost is unmeasurable.
_POLL_SECS = 15.0

# ── Generation state ──
#
# Plain module globals mutated only from the gateway's single event loop (the
# watcher task, the reset-all handler, and the status endpoint all run on it),
# so no lock is needed. Monotonic within one gateway process; a restart resets
# both counters together, which is correct — a fresh gateway spawns fresh
# sessions from the current config.
_config_generation: int = 0
_sessions_reset_generation: int = 0


def current_generation() -> int:
    """The number of source-config changes observed this gateway lifetime."""
    return _config_generation


def bump_generation() -> None:
    """Record that the MCP source configs changed.

    Called by the watcher when an observed source change actually altered the
    consumed configs (or when the reconcile failed and they may have).
    """
    global _config_generation
    _config_generation += 1


def mark_sessions_reset() -> None:
    """Record that all sessions were (re)started against the current config."""
    global _sessions_reset_generation
    _sessions_reset_generation = _config_generation


def sessions_stale() -> bool:
    """True when the MCP config changed after the last full session reset.

    This is the condition for the dashboard's "MCP configuration changed —
    restart sessions to apply" banner. A session's tool table freezes at
    kiro-cli spawn time, so a change with no reset after it means every live
    session is running against a config that no longer exists on disk.
    """
    return _config_generation > _sessions_reset_generation


# ── Watcher ──


def _watched_paths() -> list[Path]:
    """The source files whose content feeds the consumed configs.

    Resolved per snapshot, never captured at import: the data home can move
    under tests and pods, and the edition seam can contribute scopes late.
    """
    paths = list(mcp_discovery._mcp_json_paths())
    paths.extend(p for p, _scope in mcp_discovery._extra_scope_sources())
    return paths


def _snapshot() -> tuple[tuple[str, int, int], ...]:
    """A comparable fingerprint of the watched files.

    ``(path, mtime_ns, size)`` per file; a missing file contributes
    ``(path, -1, -1)`` so appearing and disappearing are both changes.
    Blocking (stat calls) — run via ``asyncio.to_thread``.
    """
    out: list[tuple[str, int, int]] = []
    for p in _watched_paths():
        try:
            st = p.stat()
            out.append((str(p), st.st_mtime_ns, st.st_size))
        except OSError:
            out.append((str(p), -1, -1))
    return tuple(sorted(out))


def _consumed_fingerprint() -> bytes:
    """Content hash of the consumed agent config (empty hash when absent).

    The staleness signal is about what live sessions are missing, and sessions
    read exactly this file at spawn — so "did THIS file's content change
    across the reconcile" is the honest bump criterion. It catches what
    discovery deliberately does not report (a source entry gaining
    ``disabled: true`` still removes the server here) while staying quiet on a
    no-op source touch (identical re-save), whose rebuild rewrites identical
    bytes. Blocking (file read) — call via ``asyncio.to_thread``.
    """
    try:
        path = agent.kiro_agents_dir() / agent.AGENT_FILENAME
        return hashlib.sha256(path.read_bytes()).digest()
    except OSError:
        return b""


def _reconcile_and_fingerprint() -> bytes:
    """Run the serialized reconcile; return the consumed config's content hash."""
    mcp_discovery.sync_discovered_servers()
    return _consumed_fingerprint()


async def _on_change(baseline: bytes) -> bytes:
    """React to a source change: sync the consumed configs and re-probe.

    *baseline* is the consumed config's fingerprint captured BEFORE this
    change was observed — held across poll cycles by the watch loop, not read
    here. Reading "before" at reaction time would miss any writer that
    updated the consumed config ahead of the poll (App Kit registration
    writes it directly): the pre-updated file compares equal to itself and
    stale sessions get no banner. Returns the post-reconcile fingerprint for
    the loop to carry as the next baseline.
    """
    try:
        after = await asyncio.to_thread(_reconcile_and_fingerprint)
    except Exception:
        # A failed reconcile is exactly when the user most needs the signal:
        # the sources moved and the consumed configs may not have followed,
        # so err on the side of showing the banner.
        bump_generation()
        logger.warning("MCP auto-sync after config change failed", exc_info=True)
        after = baseline
    else:
        if after != baseline:
            # Only a reconcile that actually changed the consumed config makes
            # live sessions stale. A no-op source touch (identical re-save,
            # comment edit) must not raise a banner whose remedy destroys
            # every live session's in-flight state.
            bump_generation()
            logger.info("MCP source config changed; consumed config updated")
        else:
            logger.info("MCP source config changed; consumed configs already current")
    # Re-probe so the dashboard's status reflects the new config without
    # waiting out the cache TTLs. Failures are the probe's own to report per
    # server; a wholesale failure here only delays freshness.
    try:
        await mcp_discovery.probe_all()
    except Exception:
        logger.debug("MCP re-probe after config change failed", exc_info=True)
    # Invalidate the handler-level response cache so the next GET /api/mcp
    # serves the fresh results instead of a pre-change snapshot.
    try:
        # Genuine circular import: dashboard.handlers.mcp imports this module
        # at top level (for the config-status endpoint), so importing it back
        # at module scope would be a cycle. Function-scope is the exception,
        # not an oversight.
        from kiro_crew.dashboard.handlers.mcp import invalidate_probe_cache

        invalidate_probe_cache()
    except Exception:
        logger.debug("MCP handler cache invalidation failed", exc_info=True)
    return after


async def watch_mcp_sources() -> None:
    """Poll the MCP source configs forever; reconcile + signal on change.

    Started once at gateway boot. The first source snapshot and the first
    consumed-config fingerprint are the baselines — boot itself already runs
    ``install_agent()``, so the state at start is by definition synced and
    generation 0 is "current". The consumed fingerprint is carried across
    cycles (see :func:`_on_change` for why reading it at reaction time would
    miss pre-poll writers).
    """
    try:
        last = await asyncio.to_thread(_snapshot)
    except Exception:
        logger.debug("MCP watch baseline snapshot failed", exc_info=True)
        last = ()
    try:
        baseline = await asyncio.to_thread(_consumed_fingerprint)
    except Exception:
        logger.debug("MCP watch consumed-config baseline failed", exc_info=True)
        baseline = b""
    while True:
        await asyncio.sleep(_POLL_SECS)
        try:
            cur = await asyncio.to_thread(_snapshot)
        except Exception:
            logger.debug("MCP watch snapshot failed", exc_info=True)
            continue
        if cur != last:
            last = cur
            baseline = await _on_change(baseline)
