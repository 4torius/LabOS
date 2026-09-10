"""End-to-end checks against the real FastAPI app, without any hardware.

These exercise the actual routes rather than the functions behind them, so a
route that is registered but broken — a missing import, a signature that no
longer matches, a handler referencing something that was removed — fails here
rather than the first time an operator clicks it.

No SiLA2 server is running during these tests, which is deliberate: how the
system behaves when the instruments are off is exactly the case that used to
produce confusing answers.
"""
import sys
from pathlib import Path

import pytest

V1_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V1_DIR))

fastapi_testclient = pytest.importorskip("fastapi.testclient")


@pytest.fixture(scope="module")
def client():
    from webapp.app import create_app
    with fastapi_testclient.TestClient(create_app()) as test_client:
        yield test_client


class TestPagesRender:
    """Every page returns HTML. A template that fails to render is a 500 the
    moment somebody opens it."""

    @pytest.mark.parametrize("path", [
        "/", "/dashboard", "/workflows", "/results", "/hardware",
        "/settings", "/operator", "/database", "/batch",
    ])
    def test_page(self, client, path):
        response = client.get(path)
        assert response.status_code == 200, f"{path} -> {response.status_code}"
        assert "text/html" in response.headers["content-type"]

    @pytest.mark.parametrize("path", ["/plates", "/runs", "/recipes"])
    def test_old_bookmarks_still_land_somewhere(self, client, path):
        assert client.get(path).status_code == 200


class TestConfigApi:

    def test_get_config_returns_the_real_sections(self, client):
        config = client.get("/api/config").json()
        for section in ("servers", "workflow", "discovery", "webapp"):
            assert section in config

    def test_saving_an_invalid_config_is_refused(self, client):
        response = client.post("/api/config", json={"servers": {"broken": {"port": "not-a-number"}}})
        assert response.status_code == 400


class TestRunsApi:
    """The background-run contract."""

    def test_no_run_is_active_at_rest(self, client):
        body = client.get("/api/runs").json()
        assert body["active"] is None

    def test_live_status_is_inactive(self, client):
        assert client.get("/api/workflows/live-status").json()["active"] is False

    def test_an_unknown_run_is_404_not_a_crash(self, client):
        assert client.get("/api/runs/no-such-run").status_code == 404

    def test_steps_of_an_unknown_run_is_an_empty_timeline(self, client):
        body = client.get("/api/runs/no-such-run/steps").json()
        assert body["steps"] == []

    @pytest.mark.parametrize("action", ["pause", "resume", "abort"])
    def test_controlling_nothing_says_idle_rather_than_failing(self, client, action):
        assert client.post(f"/api/workflows/{action}").json()["status"] == "idle"

    def test_starting_a_workflow_with_an_unknown_instrument_is_reported(self, client):
        """The instruments are off, so this cannot get far — but it must fail
        with a message about instruments, not an exception."""
        response = client.post("/api/workflows/execute", json={
            "WorkflowName": "APITest",
            "Steps": [{"StepNumber": 1, "Instrument": "does_not_exist",
                       "Action": "Nope", "Parameters": {}, "DependsOn": []}],
        })
        assert response.status_code == 200
        body = response.json()
        if body.get("started"):
            # It was accepted as a background run; `status` is the run's own
            # state, the same vocabulary GET /api/runs/{id} speaks.
            assert body["run_id"]
            assert body["status"] in ("running", "failed", "error", "completed")
        else:
            assert body["status"] in ("error", "busy")


class TestInstrumentsApi:

    def test_devices_is_a_list_even_with_nothing_running(self, client):
        assert isinstance(client.get("/api/devices").json(), list)

    def test_the_command_catalog_never_invents_instruments(self, client):
        """A static fallback catalog used to be served here when discovery
        failed, advertising an incubator that does not exist and mobile
        commands that were never implemented."""
        body = client.get("/api/instruments/commands").json()
        assert "incubator" not in body.get("instruments", {})
        assert body.get("source") != "config_file"

    def test_locations_are_discovered_not_hardcoded(self, client):
        body = client.get("/api/dynamic-options/location").json()
        names = {(o if isinstance(o, str) else o.get("id")) for o in body.get("options", [])}
        assert "incubator" not in names
        assert not {"rack_1", "rack_2", "rack_3"} & names

    def test_an_unknown_device_is_reported_by_name(self, client):
        body = client.post("/api/devices/nope/command",
                           json={"command": "X", "parameters": {}}).json()
        assert body["status"] == "error"
        assert "nope" in body["error"]


class TestFileListings:

    @pytest.mark.parametrize("path", [
        "/api/files/recipes", "/api/files/hal", "/api/files/workflows",
        "/api/files/liquidclasses", "/api/files/analyses",
    ])
    def test_library_listings_answer_without_the_robot(self, client, path):
        """These read Library/ from disk. They used to be SiLA2 commands, which
        meant the file list was unavailable whenever the instrument was off."""
        assert client.get(path).status_code == 200

    def test_workflows_carry_a_schema_version(self, client):
        workflows = client.get("/api/workflows").json()
        assert workflows, "no workflows found in Library/Workflows"
        first = client.get(f"/api/workflows/{workflows[0]['id']}").json()
        assert first.get("SchemaVersion") == "1.0"


class TestImagesApi:

    def test_listing_deck_photos_works_before_any_exist(self, client):
        assert "images" in client.get("/api/images").json()

    def test_a_missing_image_is_404(self, client):
        assert client.get("/api/images/not-there.jpg").status_code == 404

    def test_path_traversal_is_refused(self, client):
        response = client.get("/api/images/..%2F..%2Flab_config.yaml")
        assert response.status_code in (403, 404)


class TestDatabaseApi:

    @pytest.mark.parametrize("path", ["/api/db/stats", "/api/db/runs", "/api/db/plates"])
    def test_endpoints_answer(self, client, path):
        assert client.get(path).status_code == 200


class TestHardwareApi:

    def test_active_config_is_readable(self, client):
        assert "filename" in client.get("/api/hardware/active").json()

    def test_config_list(self, client):
        assert "configs" in client.get("/api/hardware/configs").json()

    def test_a_config_outside_the_folder_is_refused(self, client):
        response = client.get("/api/hardware/configs/..%2F..%2Flab_config.yaml")
        assert response.status_code in (403, 404)
