"""`GET /api/resources`, and the Settings panel that renders it.

One query with two consumers: the panel shows the operator what is protecting
what, and anything planning around the instruments needs the same answer. The
model is invisible while it works, so this endpoint is the only way anybody
finds out that a claim does not resolve before a workflow is refused.

It is read-only on purpose. What a server declares about itself is not editable
from the web interface, because two sources that can disagree about a safety
rule is precisely the failure the model avoids.
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

V1_DIR = Path(__file__).resolve().parents[2]
if str(V1_DIR) not in sys.path:
    sys.path.insert(0, str(V1_DIR))

from src.catalog import parse_catalog  # noqa: E402
from webapp.routes.instruments import create_instruments_router  # noqa: E402

OPENTRONS = {"resources": {
    "provides": ["instrument:opentrons", "deck:opentrons"],
    "commands": {"ExecuteRecipe": {"claims": ["instrument:opentrons", "deck:opentrons"]},
                 "EmergencyStop": {"control": True}},
}}
MOBILE = {"resources": {
    "provides": ["instrument:mobile"],
    "commands": {"ExecuteTask": {
        "claims": ["instrument:mobile"],
        "by_parameter": {"TaskName": {"place_on_OT": ["deck:opentrons"]}}}},
}}
TYPO = {"resources": {
    "provides": ["instrument:mobile"],
    "commands": {"ExecuteTask": {"claims": ["instrument:mobile", "deck:OT"]}},
}}


class _FakeState:
    def add_log(self, *a, **k):
        pass


def _endpoint(catalogs, held=None):
    instruments = [
        SimpleNamespace(id=key, name=key.title(), catalog=parse_catalog(raw))
        for key, raw in catalogs.items()
    ]
    executor = SimpleNamespace(_ledger=SimpleNamespace(held=dict(held or {})))
    lab_core = SimpleNamespace(
        list_instruments=lambda: instruments,
        _workflow_executor=executor,
    )
    router = create_instruments_router(_FakeState(), None, lab_core, V1_DIR, V1_DIR / "Library")
    for route in router.routes:
        if getattr(route, "path", None) == "/api/resources":
            return route.endpoint
    raise AssertionError("/api/resources is not registered")


def _get(catalogs, held=None):
    return asyncio.run(_endpoint(catalogs, held)())


class TestTheTopologyItReports:

    def test_it_names_an_owner_for_every_resource(self):
        payload = _get({"opentrons": OPENTRONS, "mobile": MOBILE})
        assert payload["providers"]["deck:opentrons"] == "opentrons"
        assert payload["providers"]["instrument:mobile"] == "mobile"

    def test_it_shows_who_reaches_into_somebody_elses_space(self):
        # The relationship the model exists for has to be visible, or nobody
        # can tell whether it is in place.
        payload = _get({"opentrons": OPENTRONS, "mobile": MOBILE})
        assert "mobile.ExecuteTask" in payload["claimants"]["deck:opentrons"]

    def test_it_carries_display_names_for_the_page(self):
        payload = _get({"opentrons": OPENTRONS})
        assert payload["instrument_names"]["opentrons"] == "Opentrons"

    def test_a_clean_lab_reports_ok(self):
        payload = _get({"opentrons": OPENTRONS, "mobile": MOBILE})
        assert payload["ok"] is True
        assert payload["unresolved"] == []


class TestItSurfacesWhatIsWrong:

    def test_an_unresolved_claim_is_reported_with_a_message(self):
        payload = _get({"opentrons": OPENTRONS, "mobile": TYPO})
        assert payload["ok"] is False
        assert payload["unresolved"][0]["resource"] == "deck:OT"
        assert "no instrument provides" in payload["unresolved"][0]["message"]

    def test_an_undeclared_instrument_is_listed(self):
        payload = _get({"opentrons": OPENTRONS, "thermo": {}})
        assert "thermo" in payload["undeclared"]


class TestWhatIsHeldRightNow:

    def test_it_reports_the_live_ledger(self):
        payload = _get({"opentrons": OPENTRONS, "mobile": MOBILE},
                       held={"deck:opentrons": "step-7"})
        assert payload["held"] == {"deck:opentrons": "step-7"}

    def test_nothing_held_between_runs(self):
        payload = _get({"opentrons": OPENTRONS})
        assert payload["held"] == {}

    def test_it_survives_an_executor_with_no_ledger(self):
        lab_core = SimpleNamespace(
            list_instruments=lambda: [], _workflow_executor=SimpleNamespace())
        router = create_instruments_router(
            _FakeState(), None, lab_core, V1_DIR, V1_DIR / "Library")
        endpoint = next(r.endpoint for r in router.routes
                        if getattr(r, "path", None) == "/api/resources")
        assert asyncio.run(endpoint())["held"] == {}


class TestThePanel:

    PAGE = (V1_DIR / "webapp" / "templates" / "settings.html").read_text(encoding="utf-8")

    def test_the_tab_exists_and_loads_the_endpoint(self):
        assert 'data-bs-target="#tab-resources"' in self.PAGE
        assert 'id="tab-resources"' in self.PAGE
        assert "fetch('/api/resources')" in self.PAGE

    def test_every_value_from_a_server_is_escaped(self):
        # Resource ids, instrument names and problem text all arrive from a
        # server on another machine, and the page builds HTML by string
        # concatenation. Every interpolation has to go through esc() or who()
        # (which escapes), or be something the page computed itself.
        import re
        panel = self.PAGE.split("// \u2500\u2500 Resources ")[1].split("</script>")[0]
        # Safe: escaped at the point of use (esc / who / LabOS.escapeHtml),
        # built by the page from already-escaped pieces (`rows`, the .map()
        # joins), or a literal chosen by a ternary.
        safe = re.compile(
            r"^(esc\(|who\(|LabOS\.escapeHtml\(|rows$|.*\.map\(|"
            r"data\.ok \?|aliases\.length \?|holder\s*\?|reaching\.length\s*\?)")
        unescaped = [
            expression for expression in re.findall(r"\$\{([^}]*)\}", panel)
            if not safe.match(expression.strip())
        ]
        assert unescaped == [], f"interpolated without escaping: {unescaped}"

    def test_it_does_not_offer_to_edit_what_a_server_declares(self):
        panel = self.PAGE.split("// ── Resources ")[1].split("</script>")[0]
        for verb in ("POST", "PUT", "DELETE"):
            assert verb not in panel, (
                "the panel must stay read-only for server declarations"
            )
