"""The public-web deploy destination is closable by the operator (issue #3599).

Before this, ``deploy-web-aws`` was the one publish destination exempt from the
``capabilities.publish`` chokepoint: the provider row was appended to
``/api/publish-providers`` unconditionally and ``POST /api/deploy/deploy``
consulted no ceiling at all. These tests pin the three properties that make the
destination genuinely closable rather than merely hidden:

1. the provider row disappears from the registry when the destination is denied;
2. ``/api/deploy/deploy`` answers 403 on its own, so a caller that never reads the
   registry (a direct POST, or the internal-secret MCP preview) is refused too;
3. ``/api/deploy/pending/{id}/confirm`` answers 403 **before** claiming the entry,
   so an entry created while the destination was open is neither deployable nor
   silently consumed after the operator closes it.

Plus one wiring test that goes through the REAL decision (no stubbed gate) via the
operator's ``publish.allowed_destinations`` config, because every mock-level test
above would still pass if the handler called a function that always permitted.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from kiro_crew.deploy import handlers


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _NonRestrictedState:
    _restricted_keys: set = set()
    _slots: dict = {}


class _Req:
    """Non-restricted dashboard request."""

    def __init__(self, body=None, *, match_info=None, headers=None):
        self._body = body or {}
        self.headers = headers if headers is not None else {"X-Session-Key": "dashboard:ui"}
        self.app = {"state": _NonRestrictedState()}
        self.match_info = match_info or {}
        self.method = "POST"

    async def json(self):
        return self._body

    async def read(self):
        return json.dumps(self._body).encode()


# ── /api/deploy/deploy ──────────────────────────────────────────────────────

def test_deploy_denied_when_destination_closed(monkeypatch):
    """A denied destination returns 403 and never reaches the deploy engine."""
    monkeypatch.setattr(
        handlers, "publish_denied_reason", lambda _req, _pid: "closed by policy"
    )

    async def _must_not_run(_params):  # pragma: no cover - reached only on regression
        raise AssertionError("_do_deploy ran despite a denied destination")

    monkeypatch.setattr(handlers, "_do_deploy", _must_not_run)
    resp = _run(handlers._handle_deploy(_Req({"site_id": "x", "artifact_slug": "a"})))
    assert resp.status == 403
    assert "closed by policy" in resp.text


def test_deploy_asks_about_the_deploy_web_destination(monkeypatch):
    """The gate is consulted for ``deploy-web-aws``, not some other provider id."""
    seen: list[str] = []

    def _record(_req, provider_id):
        seen.append(provider_id)
        return None

    monkeypatch.setattr(handlers, "publish_denied_reason", _record)
    monkeypatch.setattr(handlers, "_do_deploy", lambda _p: _permitted())
    _run(handlers._handle_deploy(_Req({"site_id": "x"})))
    assert seen == ["deploy-web-aws"]


async def _permitted():
    return 200, {"requires_confirm": True}


def test_deploy_proceeds_when_permitted(monkeypatch):
    monkeypatch.setattr(handlers, "publish_denied_reason", lambda _req, _pid: None)
    monkeypatch.setattr(handlers, "_do_deploy", lambda _p: _permitted())
    resp = _run(handlers._handle_deploy(_Req({"site_id": "x"})))
    assert resp.status == 200


def test_denials_carry_a_machine_readable_code(monkeypatch):
    """Both 403s carry `code`, not prose alone.

    The dashboard renders ``error`` verbatim into a localized UI, so a coded
    denial is the only translatable one — and this denial is the message a whole
    team sees after an operator closes the destination.
    """
    monkeypatch.setattr(
        handlers, "publish_denied_reason", lambda _req, _pid: "closed by policy"
    )
    deploy = _run(handlers._handle_deploy(_Req({"site_id": "x"})))
    pending = _run(handlers._handle_pending_confirm(_Req(match_info={"id": "p1"})))
    for resp in (deploy, pending):
        assert resp.status == 403
        assert json.loads(resp.text)["code"] == "publish_destination_disabled"


def test_the_gate_never_runs_on_the_event_loop():
    """Both deploy call sites offload the decision to a thread.

    ``publish_denied_reason`` reads the trust-root policy, every governance
    profile and ``config.json`` from disk. Run inline it stalls the whole gateway
    (and its heartbeat) on a slow or contended data home — for every caller, not
    just the one publishing. The provider-registry call site is offloaded for the
    same reason, so this pins all three rather than leaving one shape behind.
    """
    import inspect

    for fn in (handlers._handle_deploy, handlers._handle_pending_confirm):
        src = inspect.getsource(fn)
        assert "publish_denied_reason" in src, f"{fn.__name__} lost the gate entirely"
        assert "asyncio.to_thread(" in src, (
            f"{fn.__name__} calls publish_denied_reason on the event loop"
        )

    from kiro_crew.apps import routes

    registry_src = inspect.getsource(routes.handle_publish_providers)
    assert "asyncio.to_thread(" in registry_src


def test_internal_secret_preview_is_denied_too(monkeypatch):
    """The MCP ``deploy_artifact`` preview rides this endpoint, so it is gated too.

    The preview is the surface that tells an agent the destination exists; a closed
    destination must not advertise one.
    """
    monkeypatch.setattr(
        handlers, "publish_denied_reason", lambda _req, _pid: "closed by policy"
    )
    req = _Req({"site_id": "x"}, headers={"X-Internal-Secret": "s"})
    resp = _run(handlers._handle_deploy(req))
    assert resp.status == 403


# ── /api/deploy/pending/{id}/confirm ────────────────────────────────────────

def test_pending_confirm_denied_without_consuming_the_entry(monkeypatch):
    """403 lands BEFORE claim_pending, so the entry survives the refusal.

    Order matters: claiming is destructive (the entry is removed so concurrent
    confirms cannot double-deploy), so gating after the claim would turn every
    denied confirm into a silently discarded pending deploy.
    """
    monkeypatch.setattr(
        handlers, "publish_denied_reason", lambda _req, _pid: "closed by policy"
    )
    from kiro_crew.deploy import pending as pending_mod

    def _must_not_claim(_entry_id):  # pragma: no cover - reached only on regression
        raise AssertionError("claim_pending ran despite a denied destination")

    monkeypatch.setattr(pending_mod, "claim_pending", _must_not_claim)
    resp = _run(handlers._handle_pending_confirm(_Req(match_info={"id": "p1"})))
    assert resp.status == 403
    assert "closed by policy" in resp.text


# ── GET /api/publish-providers ──────────────────────────────────────────────

def test_provider_registry_omits_closed_destination(monkeypatch):
    from kiro_crew.apps import routes

    monkeypatch.setattr(routes, "list_apps", lambda: [])
    monkeypatch.setattr(
        routes, "publish_denied_reason", lambda _req, _pid: "closed by policy"
    )
    resp = _run(routes.handle_publish_providers(_Req()))
    ids = [p["id"] for p in json.loads(resp.text)["providers"]]
    assert "deploy-web-aws" not in ids


def test_provider_registry_lists_open_destination(monkeypatch):
    from kiro_crew.apps import routes

    monkeypatch.setattr(routes, "list_apps", lambda: [])
    monkeypatch.setattr(routes, "publish_denied_reason", lambda _req, _pid: None)
    resp = _run(routes.handle_publish_providers(_Req()))
    rows = json.loads(resp.text)["providers"]
    assert [p["id"] for p in rows] == ["deploy-web-aws"]
    # The row still carries the fields the Publish panel renders from.
    assert rows[0]["endpoint"] == "/api/deploy/deploy"
    assert rows[0]["origin"] == "core"


# ── the real decision, not a stub ───────────────────────────────────────────

@pytest.fixture()
def narrowed_allowlist(monkeypatch):
    """Operator config permits only the internal registry — deploy is excluded."""
    from kiro_crew.config.loader import KiroCrewConfig, PublishConfig

    cfg = KiroCrewConfig.load()
    cfg.publish = PublishConfig(allowed_destinations=["internal-registry"])
    monkeypatch.setattr(KiroCrewConfig, "load", staticmethod(lambda: cfg))
    return cfg


def test_config_allowlist_reaches_the_deploy_endpoint(narrowed_allowlist, monkeypatch):
    """End-to-end through the REAL gate: config narrowing closes /api/deploy/deploy.

    Every test above stubs ``publish_denied_reason``, so all of them would still
    pass if the handler consulted a gate that never denied. This one exercises the
    genuine decision path (governance ceiling ungoverned by default → config
    allowlist) and is what actually proves the operator's knob is wired through.
    """

    async def _must_not_run(_params):  # pragma: no cover - reached only on regression
        raise AssertionError("_do_deploy ran despite a narrowed allowlist")

    monkeypatch.setattr(handlers, "_do_deploy", _must_not_run)
    resp = _run(handlers._handle_deploy(_Req({"site_id": "x"})))
    assert resp.status == 403
    assert "deploy-web-aws" in resp.text


def test_deploy_permitted_when_allowlist_names_it(monkeypatch):
    from kiro_crew.config.loader import KiroCrewConfig, PublishConfig

    cfg = KiroCrewConfig.load()
    cfg.publish = PublishConfig(allowed_destinations=["deploy-web-aws"])
    monkeypatch.setattr(KiroCrewConfig, "load", staticmethod(lambda: cfg))
    monkeypatch.setattr(handlers, "_do_deploy", lambda _p: _permitted())
    resp = _run(handlers._handle_deploy(_Req({"site_id": "x"})))
    assert resp.status == 200


# ── one decision, not two ───────────────────────────────────────────────────

def test_artifact_publish_and_deploy_share_one_decision():
    """The artifact-publish alias must stay the shared helper, never a fork.

    Two copies of an authorization decision drift; the point of moving it into
    ``publish_governance`` was that a policy change lands on every publish surface
    at once.
    """
    from kiro_crew.dashboard.handlers import artifacts as art
    from kiro_crew.publish_governance import publish_denied_reason

    assert art.publish_denied_reason is publish_denied_reason
    # The alias forwards rather than reimplementing: its body is a single call.
    import inspect

    src = inspect.getsource(art._publish_governance_denied)
    assert "publish_denied_reason(request, provider_name)" in src
    assert "governance_permits" not in src
