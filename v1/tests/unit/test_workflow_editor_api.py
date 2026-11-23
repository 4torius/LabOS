"""The two routes the workflow editor uses to check a workflow before running it.

`/api/workflows/validate` answers what a run would refuse, without running.
`/api/workflows/simulate` runs it in the simulated lab and returns the
timeline. No SiLA2 server is running here, which is the point for validate:
an offline lab must give a readable answer, not an exception.
"""
import json
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


def _wf(*steps):
    return {"WorkflowName": "Editor", "SchemaVersion": "1.0", "Steps": [
        {"StepNumber": i + 1, "Instrument": inst, "Action": action,
         "Parameters": params, "DependsOn": deps}
        for i, (inst, action, params, deps) in enumerate(steps)]}


class TestValidate:

    def test_a_dependency_cycle_is_reported_on_its_step(self, client):
        body = client.post("/api/workflows/validate", json=_wf(
            ("delay", "Wait", {"seconds": 0}, [2]),
            ("delay", "Wait", {"seconds": 0}, [1]))).json()
        assert body["valid"] is False
        cycles = [e for e in body["errors"] if "cycle" in e["message"].lower()]
        assert cycles and cycles[0]["step"] in (1, 2)

    def test_a_correct_workflow_of_local_steps_is_valid(self, client):
        body = client.post("/api/workflows/validate", json=_wf(
            ("delay", "Wait", {"seconds": 0}, []),
            ("delay", "Wait", {"seconds": 0}, [1]))).json()
        assert body == {"valid": True, "errors": [], "warnings": [], "implied": {}, "plates": {}}

    def test_an_instrument_that_is_off_is_named(self, client):
        body = client.post("/api/workflows/validate", json=_wf(
            ("tecan", "PlateOut", {}, []))).json()
        assert body["valid"] is False
        assert body["errors"][0]["step"] == 1

    def test_no_dependencies_at_all_is_a_warning(self, client):
        body = client.post("/api/workflows/validate", json=_wf(
            ("delay", "Wait", {"seconds": 0}, []),
            ("delay", "Wait", {"seconds": 0}, []))).json()
        assert body["valid"] is True
        assert body["warnings"] and body["warnings"][0]["field"] == "DependsOn"

    def test_garbage_is_an_answer_not_a_500(self, client):
        response = client.post("/api/workflows/validate", json={"Steps": "nope"})
        assert response.status_code == 200
        assert response.json()["valid"] is False


class TestSimulate:

    def test_the_simulated_lab_runs_the_workflow_and_returns_its_timeline(self, client):
        video = json.loads((V1_DIR / "Library/Workflows/Video.workflow.json")
                           .read_text(encoding="utf-8"))
        body = client.post("/api/workflows/simulate",
                           json={"workflow": video, "max_concurrency": 0}).json()
        assert body["status"] == "ok", body
        assert body["success"] is True
        assert body["clashes"] == []
        assert len(body["timeline"]) == len(video["Steps"])
        assert body["timeline"][0]["label"].startswith("mobile.ExecuteTask")
