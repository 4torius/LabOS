"""Tests for LabCore._server_to_instrument: shared-port instrument identity.

Two SiLA2 servers can end up sharing the same configured port (e.g. a stale
server left running on an old host during a redeploy, plus the new one on a
different host). Without a host check, the instrument ID collapses onto
whichever server was seen last — see src/lab_core.py::_server_to_instrument().
"""
import yaml

from src.discovery import PnPServer
from src.lab_core import LabCore


def _make_core(tmp_path, *, configured_host="192.168.11.22", port=50053):
    (tmp_path / "lab_config.yaml").write_text(
        yaml.dump({"servers": {"mobile": {"port": port, "host": configured_host}}})
    )
    return LabCore(base_dir=tmp_path)


class TestSharedPortInstrumentIdentity:
    def test_foreign_host_on_shared_port_does_not_overwrite_configured_instrument(self, tmp_path):
        core = _make_core(tmp_path)
        port_to_key, port_to_host = core._build_port_to_key()

        good_server = PnPServer(name="GoFaGo Mobile Robot", host="192.168.11.22", port=50053, server_online=True)
        good_instrument = core._server_to_instrument(good_server, port_to_key, port_to_host)
        core._instruments[good_instrument.id] = good_instrument

        stale_server = PnPServer(name="GoFaGo Mobile Robot", host="10.16.0.61", port=50053, server_online=False)
        stale_instrument = core._server_to_instrument(stale_server, port_to_key, port_to_host)

        assert good_instrument.id == "mobile"
        assert stale_instrument.id != "mobile"
        # The registered "mobile" slot must remain the good, online server.
        assert core._instruments["mobile"] is good_instrument
        assert core._instruments["mobile"].status == "online"

    def test_matching_configured_host_claims_the_configured_id(self, tmp_path):
        core = _make_core(tmp_path)
        port_to_key, port_to_host = core._build_port_to_key()

        server = PnPServer(name="GoFaGo Mobile Robot", host="192.168.11.22", port=50053, server_online=True)
        instrument = core._server_to_instrument(server, port_to_key, port_to_host)

        assert instrument.id == "mobile"

    def test_reconnect_from_new_host_for_already_registered_instrument_is_accepted(self, tmp_path):
        """A legitimate host change for an already-registered instrument (e.g.
        DHCP lease change) should still update the same instrument slot rather
        than being rejected as foreign."""
        core = _make_core(tmp_path)
        port_to_key, port_to_host = core._build_port_to_key()

        original_server = PnPServer(name="GoFaGo Mobile Robot", host="192.168.11.22", port=50053, server_online=True)
        original_instrument = core._server_to_instrument(original_server, port_to_key, port_to_host)
        core._instruments[original_instrument.id] = original_instrument

        # Same device re-announces from a new IP that matches the previously
        # registered instance's host, not the (stale) configured one.
        new_ip_server = PnPServer(name="GoFaGo Mobile Robot", host="192.168.11.22", port=50053, server_online=True)
        new_ip_instrument = core._server_to_instrument(new_ip_server, port_to_key, port_to_host)

        assert new_ip_instrument.id == "mobile"

    def test_no_configured_host_falls_back_to_port_only_matching(self, tmp_path):
        (tmp_path / "lab_config.yaml").write_text(
            yaml.dump({"servers": {"mobile": {"port": 50053}}})
        )
        core = LabCore(base_dir=tmp_path)
        port_to_key, port_to_host = core._build_port_to_key()

        server = PnPServer(name="GoFaGo Mobile Robot", host="10.16.0.61", port=50053, server_online=True)
        instrument = core._server_to_instrument(server, port_to_key, port_to_host)

        assert instrument.id == "mobile"
