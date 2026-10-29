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

    @pytest.mark.parametrize("panel", [
        "// \u2500\u2500 Resources ",
        "async function loadDropdownSources",
    ])
    def test_every_value_from_a_server_is_escaped(self, panel):
        # Resource ids, instrument names, hint names and problem text all
        # arrive from a server on another machine, and these panels build HTML
        # by string concatenation. Every interpolation must either run a value
        # through an escaper or be something the page assembled itself from
        # already-escaped pieces.
        import re
        body = self.PAGE.split(panel)[1].split("</script>")[0]
        escapers = ("esc(", "who(", "LabOS.escapeHtml(")
        unescaped = []
        for expression in re.findall(r"\$\{([^}]*)\}", body):
            text = expression.strip()
            if any(e in text for e in escapers) or ".map(" in text:
                continue
            # A bare identifier is a variable this function built a line
            # earlier, already escaped; a value that came from the server is
            # reached through a property access or an index, so that is the
            # shape worth flagging. A ternary over literals is safe too.
            if re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", text):
                continue
            if "?" in text and "'" in text:
                continue
            unescaped.append(text)
        assert unescaped == [], f"interpolated without escaping: {unescaped}"

    def test_it_does_not_offer_to_edit_what_a_server_declares(self):
        panel = self.PAGE.split("// ── Resources ")[1].split("</script>")[0]
        for verb in ("POST", "PUT", "DELETE"):
            assert verb not in panel, (
                "the panel must stay read-only for server declarations"
            )


class TestTheDropdownSourcesEndpoint:
    """`GET /api/dropdowns` — the companion to /api/resources for pickers.

    An empty dropdown looks identical whether a server never declared it or the
    folder behind it is empty, and an operator has no way to tell. This says
    which, and where each hint was declared.
    """

    @staticmethod
    def _endpoint():
        import asyncio as _asyncio
        from src.lab_core import LabCore
        core = LabCore(V1_DIR)
        router = create_instruments_router(
            _FakeState(), None, core, V1_DIR, V1_DIR / "Library")
        endpoint = next(r.endpoint for r in router.routes
                        if getattr(r, "path", None) == "/api/dropdowns")
        return _asyncio.run(endpoint())

    def test_the_shipped_pickers_are_listed_with_their_origin(self):
        payload = self._endpoint()
        for hint in ("plate", "labware", "tiprack", "pipette", "mobile_task"):
            assert hint in payload["hints"], f"{hint} is not declared anywhere"
            assert payload["hints"][hint]["origin"] in ("config",) or \
                payload["hints"][hint]["origin"].startswith("server:")

    def test_none_of_the_shipped_pickers_is_empty(self):
        # An empty one means a declaration that resolves to nothing, which is a
        # picker an operator cannot use.
        payload = self._endpoint()
        empty = [h for h, e in payload["hints"].items() if e["count"] == 0]
        assert empty == [], f"these pickers offer nothing: {empty}"

    def test_the_shipped_declarations_have_no_problems(self):
        assert self._endpoint()["problems"] == []

    def test_a_dynamic_picker_says_which_property_it_reads(self):
        payload = self._endpoint()
        mobile = payload["hints"]["mobile_task"]
        assert mobile["dynamic"] is True
        assert mobile["source_property"] == "AvailableTasks"
        assert mobile["sources"] == [], (
            "a snapshot folder must not be listed as a source of values"
        )

    def test_it_survives_without_lab_core(self):
        import asyncio as _asyncio
        router = create_instruments_router(
            _FakeState(), None, None, V1_DIR, V1_DIR / "Library")
        endpoint = next(r.endpoint for r in router.routes
                        if getattr(r, "path", None) == "/api/dropdowns")
        assert _asyncio.run(endpoint())["hints"] == {}


class TestTheDropdownPanel:

    PAGE = (V1_DIR / "webapp" / "templates" / "settings.html").read_text(encoding="utf-8")

    def test_the_card_loads_the_endpoint(self):
        assert "fetch('/api/dropdowns')" in self.PAGE
        assert 'id="dropdowns-body"' in self.PAGE

    def test_it_is_loaded_when_the_tab_is_opened(self):
        assert "loadResources(); loadDropdownSources()" in self.PAGE

    def test_it_does_not_offer_to_edit_a_declaration(self):
        panel = self.PAGE.split("async function loadDropdownSources")[1].split("</script>")[0]
        for verb in ("POST", "PUT", "DELETE"):
            assert verb not in panel
