"""The ``discovery`` seam: which external registries may be queried.

Both discovery catalogs — skills and MCP servers — reach the public internet and
then offer to install what they return. These tests pin that a composed policy can
refuse a registry, that refusing means the provider is never registered (rather
than failing per request), and that the public default still admits everything.
"""

from __future__ import annotations

import dataclasses

import pytest

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.handlers._shared import admits_registry as _admits
from kiro_crew.platform import context as ctx_mod
from kiro_crew.platform.bootstrap import build_default_context
from kiro_crew.platform.defaults import DefaultDiscoveryPolicy


def _base_context():
    return build_default_context(KiroCrewConfig())


class _DenyAll:
    def admits_registry(self, kind: str, name: str, api_base: str) -> bool:
        return False


class _AllowOnly:
    """Allowlist by URL, the way a managed deployment would."""

    def __init__(self, allowed: str) -> None:
        self.allowed = allowed
        self.seen: list[tuple[str, str, str]] = []

    def admits_registry(self, kind: str, name: str, api_base: str) -> bool:
        self.seen.append((kind, name, api_base))
        return api_base.startswith(self.allowed)


class _Boom:
    def admits_registry(self, kind: str, name: str, api_base: str) -> bool:
        raise RuntimeError("adapter exploded")


@pytest.fixture
def _reset_registries():
    """Both provider registries are module-level singletons."""
    from kiro_crew.dashboard.handlers import discover, mcp_discover

    discover._registry = None
    mcp_discover._registry = None
    yield
    discover._registry = None
    mcp_discover._registry = None


def _with_policy(monkeypatch, policy):
    base = _base_context()
    monkeypatch.setattr(
        ctx_mod, "current_context", lambda: dataclasses.replace(base, discovery=policy)
    )


class TestDefaultAdmitsEverything:
    def test_public_default_admits_any_registry(self):
        pol = DefaultDiscoveryPolicy()
        assert pol.admits_registry("skill", "skillsh", "https://api.skills.sh")
        assert pol.admits_registry("mcp", "official", "https://registry.modelcontextprotocol.io")

    def test_default_context_carries_the_slot(self):
        assert isinstance(_base_context().discovery, DefaultDiscoveryPolicy)


class TestSkillProviderGating:
    def test_denied_provider_is_not_registered(self, monkeypatch, _reset_registries):
        from kiro_crew.dashboard.handlers import discover

        _with_policy(monkeypatch, _DenyAll())
        assert discover._build_registry().provider_names == []

    def test_admitted_provider_is_registered(self, monkeypatch, _reset_registries):
        from kiro_crew.dashboard.handlers import discover

        _with_policy(monkeypatch, DefaultDiscoveryPolicy())
        assert "skillsh" in discover._build_registry().provider_names


class TestMcpProviderGating:
    def test_denied_official_registry_is_not_registered(self, monkeypatch, _reset_registries):
        from kiro_crew.dashboard.handlers import mcp_discover

        _with_policy(monkeypatch, _DenyAll())
        assert "official" not in mcp_discover._build_registry().provider_names

    def test_allowlisting_by_url_keeps_an_internal_registry_only(
        self, monkeypatch, _reset_registries
    ):
        """The shape a managed deployment actually wants.

        Allowing only an internal base URL must drop the public MCP registry while
        the decision is made on the URL, not the provider's self-chosen name.
        """
        from kiro_crew.dashboard.handlers import mcp_discover

        pol = _AllowOnly("https://internal.example.invalid/")
        _with_policy(monkeypatch, pol)

        names = mcp_discover._build_registry().provider_names

        assert "official" not in names
        assert ("mcp", "official", "https://registry.modelcontextprotocol.io") in pol.seen


class TestFailClosed:
    def test_a_raising_policy_denies_rather_than_admits(self, monkeypatch, _reset_registries):
        """A composed policy that throws must not hand back public egress.

        Reaching this path means a managed deployment intended to restrict
        something, so admitting on error would restore the exact fetch the
        operator disabled.
        """
        _with_policy(monkeypatch, _Boom())
        assert _admits("mcp", "official", "https://example.invalid") is False

    def test_composition_error_still_propagates(self, monkeypatch):
        """The CPP invariant: a composition failure aborts, it does not degrade."""

        def boom():
            raise ctx_mod.PlatformCompositionError("no companion")

        monkeypatch.setattr(ctx_mod, "current_context", boom)
        with pytest.raises(ctx_mod.PlatformCompositionError):
            _admits("skill", "skills.sh", "https://api.skills.sh")
