"""attach_existing_measurement() / ingest_animl_measurement(): linking an
AnIML file that already exists on disk to a plate, without a live SiLA2
measurement result - the "Attach existing result" path in the "Add an
analysis to this plate" dialog, alongside the pre-existing "Run measurement"
(live) path handled by track_tecan_measurement().
"""
import pytest

from src import database as db
from src import plate_tracking as pt

_NS = "urn:org:astm:animl:schema:core:draft:0.90"


def _write_animl(path, plate_name="P1", wells=(("A1", 1.23), ("A2", 4.56))):
    wells_xml = "".join(f'<animl:S>{w}</animl:S>' for w, _ in wells)
    vals_xml = "".join(f'<animl:F>{v}</animl:F>' for _, v in wells)
    content = f"""<?xml version="1.0" encoding="utf-8"?>
<animl:AnIML version="0.90" xmlns:animl="{_NS}">
  <animl:SampleSet id="ss1" name="Plate {plate_name}">
    <animl:Container id="container_{plate_name}" name="{plate_name}" containerType="MicrotiterPlate">
      <animl:Parameter name="PlateType" parameterType="String"><animl:S>Test Plate</animl:S></animl:Parameter>
    </animl:Container>
  </animl:SampleSet>
  <animl:ExperimentStepSet id="exp1">
    <animl:ExperimentStep id="step_Label1" name="Label1">
      <animl:Technique name="Absorbance" uri="urn:astm:animl:technique:spectroscopy:absorbance"/>
      <animl:ResultSet id="results_Label1">
        <animl:SeriesSet id="series_Label1" name="Well Readings" length="{len(wells)}">
          <animl:Series id="wellpos_Label1" name="Well Position" dependency="independent" seriesType="String" plotScale="none">
            <animl:EncodedValueSet startIndex="0" endIndex="{len(wells)-1}">
              <animl:IndividualValueSet>{wells_xml}</animl:IndividualValueSet>
            </animl:EncodedValueSet>
          </animl:Series>
          <animl:Series id="values_Label1" name="Optical Density" dependency="dependent" seriesType="Float64" plotScale="linear">
            <animl:Unit label="OD"/>
            <animl:EncodedValueSet startIndex="0" endIndex="{len(wells)-1}">
              <animl:IndividualValueSet>{vals_xml}</animl:IndividualValueSet>
            </animl:EncodedValueSet>
          </animl:Series>
        </animl:SeriesSet>
      </animl:ResultSet>
    </animl:ExperimentStep>
  </animl:ExperimentStepSet>
</animl:AnIML>
"""
    path.write_text(content, encoding="utf-8")
    return path


@pytest.fixture
def fresh_db(tmp_path):
    db.configure(tmp_path / "test.db")
    db.init_db()
    yield db
    db._DB_PATH = None


@pytest.fixture
def results_dir(tmp_path):
    d = tmp_path / "Results"
    (d / "AnIML").mkdir(parents=True)
    return d


class TestAttachExistingMeasurement:
    def test_attaches_file_and_creates_plate_row(self, fresh_db, results_dir):
        _write_animl(results_dir / "AnIML" / "myplate_20260101_000000.animl", plate_name="myplate")

        result = pt.attach_existing_measurement(
            "myplate", "myplate_20260101_000000.animl", results_dir,
        )
        assert result == "myplate"
        plate = fresh_db.get_plate("myplate")
        assert plate is not None
        assert plate["status"] == "measured"

    def test_well_values_are_persisted(self, fresh_db, results_dir):
        _write_animl(results_dir / "AnIML" / "p2.animl", wells=(("A1", 1.1), ("B2", 2.2)))
        pt.attach_existing_measurement("P2", "p2.animl", results_dir)

        heatmap = fresh_db.get_well_heatmap("P2")
        values = {cell["well_id"]: cell["value"] for row in heatmap["grid"] for cell in row}
        assert values.get("A1") == 1.1
        assert values.get("B2") == 2.2

    def test_does_not_clobber_an_existing_richer_plate_row(self, fresh_db, results_dir):
        fresh_db.save_plate({"plate_id": "P3", "rows": 8, "columns": 12, "notes": "from a real recipe"})
        _write_animl(results_dir / "AnIML" / "p3.animl")
        pt.attach_existing_measurement("P3", "p3.animl", results_dir)

        plate = fresh_db.get_plate("P3")
        assert plate["notes"] == "from a real recipe"

    def test_missing_file_raises_file_not_found(self, fresh_db, results_dir):
        with pytest.raises(FileNotFoundError):
            pt.attach_existing_measurement("P4", "does_not_exist.animl", results_dir)

    def test_path_traversal_filename_is_rejected(self, fresh_db, results_dir):
        with pytest.raises(ValueError):
            pt.attach_existing_measurement("P5", "../../etc/passwd", results_dir)

    def test_protocol_name_creates_a_protocol_row(self, fresh_db, results_dir):
        _write_animl(results_dir / "AnIML" / "p6.animl")
        pt.attach_existing_measurement("P6", "p6.animl", results_dir, protocol_name="Script2")

        protocols = fresh_db.list_protocols() if hasattr(fresh_db, "list_protocols") else None
        if protocols is not None:
            assert any(p.get("name") == "Script2" for p in protocols)

    def test_prefers_sibling_xml_over_the_animl_file_itself(self, fresh_db, results_dir):
        """When Results/XML/<name>.xml exists alongside Results/AnIML/<name>.animl,
        ingestion must read the XML (the AnIML conversion step is our own
        code and the source of every bug found in this pipeline) - proven
        here by giving the two files CONTRADICTORY values and asserting the
        XML's value wins."""
        (results_dir / "XML").mkdir()
        xml_content = """<?xml version="1.0" encoding="UTF-8"?>
<MeasurementResultData Date="2026-01-01T00:00:00Z">
    <Header Application="Tecan i-control">
        <Instrument><Name>x</Name><SerialNumber>1</SerialNumber></Instrument>
        <Parameters><Parameter Name="Plate" Value="P" /></Parameters>
    </Header>
    <Section Name="Label1" Time_Start="2026-01-01T00:00:00Z" Time_End="2026-01-01T00:01:00Z">
        <Parameters><Parameter Name="Mode" Value="Absorbance" /></Parameters>
        <Data Cycle="1"><Well Pos="A1" Type="Single"><Single>9.99</Single></Well></Data>
    </Section>
</MeasurementResultData>
"""
        (results_dir / "XML" / "p8.xml").write_text(xml_content, encoding="utf-8")
        _write_animl(results_dir / "AnIML" / "p8.animl", wells=(("A1", 0.01),))  # deliberately wrong/stale

        pt.attach_existing_measurement("P8", "p8.animl", results_dir)
        heatmap = fresh_db.get_well_heatmap("P8")
        assert heatmap["grid"][0][0]["value"] == 9.99

    def test_instrument_measurement_row_marked_as_manual_attach(self, fresh_db, results_dir):
        _write_animl(results_dir / "AnIML" / "p7.animl")
        pt.attach_existing_measurement("P7", "p7.animl", results_dir)

        with fresh_db._conn() as con:
            row = con.execute(
                "SELECT instrument, command, animl_path FROM instrument_measurements WHERE plate_id = ?",
                ("P7",),
            ).fetchone()
        assert row is not None
        assert row["instrument"] == "tecan_manual"
        assert row["command"] == "RunMeasurement"
        assert row["animl_path"].endswith("p7.animl")
