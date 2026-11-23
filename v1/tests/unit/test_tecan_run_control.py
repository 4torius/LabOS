"""Abort, pause and resume on the Tecan, as far as Python can see them.

The behaviour itself lives in the C# bridge (TecanBridge.RunControl.cs) and
can only run on the Windows PC with the reader. What is pinned here is that
the SiLA server passes the three commands through, reports an abort as an
abort rather than an instrument failure, and that an emergency stop can always
be sent to the reader.
"""
import ast
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

V1_DIR = Path(__file__).resolve().parents[2]
TECAN = V1_DIR / "SiLA2" / "TecanM200SiLA2Server"


@pytest.fixture
def impl(monkeypatch):
    monkeypatch.syspath_prepend(str(TECAN))
    for name in [m for m in sys.modules if m == "generated" or m.startswith("generated.")
                 or m == "src" or m.startswith("src.")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "src.tecan_bridge_client",
                        SimpleNamespace(TecanBridgeClient=object))
    from src.plate_reader_impl import PlateReaderImpl
    import generated.platereaderservice as feature
    reader = PlateReaderImpl.__new__(PlateReaderImpl)
    yield reader, feature
    for name in [m for m in sys.modules if m == "generated" or m.startswith("generated.")
                 or m == "src" or m.startswith("src.")]:
        sys.modules.pop(name, None)


class FakeBridge:
    def __init__(self, abort=(True, True, "Measurement aborted"),
                 pause=(True, "Pause requested"), resume=(True, "Measurement resumed"),
                 chunks=()):
        self._abort, self._pause, self._resume, self._chunks = abort, pause, resume, chunks

    def abort_measurement(self):
        return self._abort

    def pause_measurement(self):
        return self._pause

    def resume_measurement(self):
        return self._resume

    def get_is_connected(self):
        return True

    def get_operational_status(self):
        return "Idle"

    def run_measurement(self, *args):
        yield from self._chunks


class TestTheCommands:

    def test_abort_reports_whether_anything_was_interrupted(self, impl):
        reader, _ = impl
        reader._bridge = FakeBridge()
        assert reader.AbortMeasurement(metadata={}).Aborted is True

    def test_abort_on_an_idle_reader_succeeds(self, impl):
        # The emergency stop sends it whatever the reader is doing.
        reader, _ = impl
        reader._bridge = FakeBridge(abort=(True, False, "No measurement was running"))
        response = reader.AbortMeasurement(metadata={})
        assert response.Aborted is False and "No measurement" in response.Message

    def test_an_abort_that_did_not_stop_it_is_an_error(self, impl):
        reader, feature = impl
        reader._bridge = FakeBridge(abort=(False, False, "did not stop within 150 s"))
        with pytest.raises(feature.AbortFailed):
            reader.AbortMeasurement(metadata={})

    def test_pause_and_resume_pass_through(self, impl):
        reader, feature = impl
        reader._bridge = FakeBridge()
        assert "Pause" in reader.PauseMeasurement(metadata={}).Message
        assert "resumed" in reader.ResumeMeasurement(metadata={}).Message
        reader._bridge = FakeBridge(pause=(False, "No measurement is running"))
        with pytest.raises(feature.NoMeasurementRunning):
            reader.PauseMeasurement(metadata={})

    def test_an_aborted_measurement_is_not_reported_as_a_failure(self, impl):
        reader, feature = impl
        reader._bridge = FakeBridge(chunks=[{
            "error": "MeasurementAborted: Measurement aborted by operator", "aborted": True}])
        instance = SimpleNamespace(begin_execution=lambda: None)
        with pytest.raises(feature.MeasurementAborted):
            reader.RunMeasurement("TEST_ABS", "P1", "", "", metadata={}, instance=instance)


class TestTheBridge:
    """Checks on the C# source that do not need a compiler."""

    SOURCE = (TECAN / "bridge" / "Instrument" / "TecanBridge.cs").read_text(encoding="utf-8")
    CONTROL = (TECAN / "bridge" / "Instrument" / "TecanBridge.RunControl.cs").read_text(encoding="utf-8")

    def test_the_run_goes_through_the_listeners(self):
        assert "RunWithControl(runGuid);" in self.SOURCE
        assert "_server.Run(runGuid);" not in self.SOURCE

    def test_an_abort_is_caught_by_the_measurement_and_not_by_the_csv_export(self):
        # It once landed on the inner try around the CSV export, where it
        # compiled and would have reported an abort as an instrument error.
        worker = self.SOURCE[self.SOURCE.index("RunWithControl(runGuid);"):]
        csv = worker.index("Failed to generate CSV file")
        aborted = worker.index("catch (OperationCanceledException ex)")
        completed = worker.index('UpdateStatus("Idle", "Measurement completed");')
        assert csv < completed < aborted

    def test_run_state_is_reset_whatever_happens(self):
        finally_block = self.SOURCE[self.SOURCE.index("RunWithControl(runGuid);"):]
        finally_block = finally_block[finally_block.index("finally"):]
        assert "EndRunControl();" in finally_block[:600]

    def test_pause_only_holds_between_kinetic_cycles(self):
        assert 'KineticPauseKey = "KINETIC.PAUSE"' in self.CONTROL

    def test_both_proto_copies_agree(self):
        bridge = (TECAN / "bridge" / "Protos" / "PlateReaderService.proto").read_text(encoding="utf-8")
        python = (TECAN / "src" / "TecanLegacyBridge.proto").read_text(encoding="utf-8")
        assert bridge.replace("\r\n", "\n") == python.replace("\r\n", "\n")
        assert "rpc AbortMeasurement" in bridge


class TestTheLauncher:

    def test_a_stale_bridge_binary_is_rebuilt(self):
        source = (TECAN / "src" / "bridge_launcher.py").read_text(encoding="utf-8")
        assert "elif _sources_newer_than_exe():" in source
        ast.parse(source)

    def test_a_pc_without_the_dotnet_sdk_still_starts_the_old_binary(self, monkeypatch):
        monkeypatch.syspath_prepend(str(TECAN))
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "tecan_bridge_launcher", TECAN / "src" / "bridge_launcher.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        def _no_dotnet(*args, **kwargs):
            raise FileNotFoundError("dotnet")
        monkeypatch.setattr(module.subprocess, "run", _no_dotnet)
        assert module.BridgeLauncher()._build() is False
