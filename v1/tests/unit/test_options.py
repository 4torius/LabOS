"""Where the values a parameter accepts come from — one reader, three layers.

The same rule as `resources.py`, applied to a different kind of fact: in the
server what the server can know, in `lab_config.yaml` what no server can speak
for. What this replaced was four `if ui_hint == "..."` branches inside
`lab_core._get_options_for_hint` with the folder walks written out in Python,
each one sitting next to a `ui_dropdowns` entry for the same hint — two sources
that could disagree, which is the shape `provides` was introduced to kill in
the resource model.

The equivalence tests at the bottom matter most: the four hints had working
hardcoded implementations, and the declarations must produce exactly what they
produced, not approximately.
"""
import json
import sys
from pathlib import Path

import pytest
import yaml

V1_DIR = Path(__file__).resolve().parents[2]
if str(V1_DIR) not in sys.path:
    sys.path.insert(0, str(V1_DIR))

from src.options import OptionSource, build_registry, parse_hint, parse_source  # noqa: E402


# ── Source kinds ──────────────────────────────────────────────────────────────

class TestSourceKinds:

    def test_a_fixed_list(self, tmp_path):
        source = parse_source({"values": ["a", "b"]}, "x", [])
        assert source.resolve(tmp_path) == ["a", "b"]

    def test_the_empty_string_is_a_real_value(self, tmp_path):
        # It is how the Tecan says "use your configured default".
        source = parse_source({"values": ["", "usb"]}, "x", [])
        assert "" in source.resolve(tmp_path)

    def test_both_spellings_of_a_fixed_list(self, tmp_path):
        # A server writes `values`; LabCore normalises it to `options` when it
        # merges a discovered declaration in, which is also what lab_config has
        # always used. One reader has to take both.
        for key in ("values", "options"):
            source = parse_source({key: ["a"]}, "x", [])
            assert source.resolve(tmp_path) == ["a"]

    def test_a_field_out_of_every_matching_file(self, tmp_path):
        folder = tmp_path / "Library" / "Plates"
        folder.mkdir(parents=True)
        (folder / "one.plate.json").write_text(json.dumps({"id": "plate_a"}))
        (folder / "two.plate.json").write_text(json.dumps({"id": "plate_b"}))
        source = parse_source(
            {"glob": "Library/Plates/*.plate.json", "field": "id"}, "x", [])
        assert source.resolve(tmp_path) == ["plate_a", "plate_b"]

    def test_fields_are_tried_in_order(self, tmp_path):
        folder = tmp_path / "Library" / "R"
        folder.mkdir(parents=True)
        (folder / "a.json").write_text(json.dumps({"parameters": {"loadName": "deep"}}))
        (folder / "b.json").write_text(json.dumps({"id": "shallow"}))
        source = parse_source(
            {"glob": "Library/R/*.json", "fields": ["parameters.loadName", "id"]}, "x", [])
        assert sorted(source.resolve(tmp_path)) == ["deep", "shallow"]

    def test_a_list_inside_one_file(self, tmp_path):
        (tmp_path / "Library").mkdir()
        (tmp_path / "Library" / "i.json").write_text(json.dumps({"load_names": ["t1", "t2"]}))
        source = parse_source(
            {"file": "Library/i.json", "list_key": "load_names"}, "x", [])
        assert source.resolve(tmp_path) == ["t1", "t2"]

    def test_objects_rendered_through_a_template(self, tmp_path):
        (tmp_path / "Library").mkdir()
        (tmp_path / "Library" / "p.json").write_text(json.dumps({"definitions": [
            {"channel": "single", "model": "p50"},
            {"channel": "single", "model": "p50"},   # duplicate
            {"channel": "eight", "model": "p20"},
        ]}))
        source = parse_source({"file": "Library/p.json", "items_key": "definitions",
                               "key_template": "{channel}/{model}"}, "x", [])
        assert source.resolve(tmp_path) == ["single/p50", "eight/p20"]

    def test_the_keys_of_a_json_file(self, tmp_path):
        (tmp_path / "Library").mkdir()
        (tmp_path / "Library" / "k.json").write_text(json.dumps({"b": 1, "a": 2}))
        source = parse_source({"json_keys_file": "Library/k.json"}, "x", [])
        assert source.resolve(tmp_path) == ["a", "b"]

    def test_the_file_stems_in_a_folder(self, tmp_path):
        folder = tmp_path / "Library" / "Recipes"
        folder.mkdir(parents=True)
        (folder / "one.json").write_text("{}")
        (folder / "notes.txt").write_text("x")
        source = parse_source(
            {"folder": "Library/Recipes", "file_extension": ".json"}, "x", [])
        assert source.resolve(tmp_path) == ["one"]

    def test_a_missing_file_or_folder_costs_only_its_own_values(self, tmp_path):
        for raw in ({"folder": "Library/Nope"},
                    {"file": "Library/none.json", "list_key": "x"},
                    {"glob": "Library/*.json", "field": "id"}):
            assert parse_source(raw, "x", []).resolve(tmp_path) == []


class TestSourcesAreConfined:

    @pytest.mark.parametrize("path", [
        "/etc/passwd", "../../secrets", "C:/Windows", "Nowhere/x.json", "..",
    ])
    def test_a_path_outside_library_or_results_is_refused(self, path):
        problems = []
        assert parse_source({"folder": path}, "x", problems) is None
        assert problems

    def test_a_source_with_no_recognised_shape_is_refused(self):
        problems = []
        assert parse_source({"something": "else"}, "x", problems) is None
        assert "declares no source" in problems[0]

    @pytest.mark.parametrize("raw", ["string", 7, [], None])
    def test_a_malformed_source_never_raises(self, raw):
        assert parse_source(raw, "x", []) is None


class TestHintParsing:

    def test_a_hint_needs_a_usable_name(self):
        problems = []
        assert parse_hint({"folder": "Library/X"}, "config", problems) is None
        assert parse_hint({"hint": "has space", "folder": "Library/X"}, "config", problems) is None

    def test_several_sources_are_unioned_without_duplicates(self, tmp_path):
        (tmp_path / "Library").mkdir()
        (tmp_path / "Library" / "a.json").write_text(json.dumps({"k": ["x", "y"]}))
        (tmp_path / "Library" / "b.json").write_text(json.dumps({"k": ["y", "z"]}))
        spec = parse_hint({"hint": "h", "sources": [
            {"file": "Library/a.json", "list_key": "k"},
            {"file": "Library/b.json", "list_key": "k"},
        ]}, "config", [])
        assert spec.resolve(tmp_path) == ["x", "y", "z"]

    def test_the_old_inline_shape_still_works(self, tmp_path):
        # Every shipped catalog uses it; there is no reason to make them churn.
        folder = tmp_path / "Library" / "Recipes"
        folder.mkdir(parents=True)
        (folder / "r.json").write_text("{}")
        spec = parse_hint({"hint": "recipe", "folder": "Library/Recipes",
                           "file_extension": ".json"}, "config", [])
        assert spec.resolve(tmp_path) == ["r"]

    def test_a_dynamic_hint_needs_no_filesystem_source(self):
        spec = parse_hint({"hint": "mobile_task", "dynamic": True,
                           "source_property": "AvailableTasks"}, "config", [])
        assert spec.dynamic
        assert spec.sources == ()

    def test_a_hint_that_offers_nothing_is_reported(self):
        problems = []
        parse_hint({"hint": "empty"}, "config", problems)
        assert any("offers nothing" in p for p in problems)


class TestTheThreeLayers:

    SERVER = [{"hint": "recipe", "folder": "Library/Recipes", "file_extension": ".json"}]

    def test_a_server_declaration_wins_and_says_where_it_came_from(self):
        registry = build_registry({"opentrons": self.SERVER})
        assert registry.hints["recipe"].origin == "server:opentrons"

    def test_the_config_cannot_override_a_declaring_server(self):
        # The same precedence as resources, for the same reason: an instrument
        # is the authority on itself, or adding one means editing the core.
        registry = build_registry(
            {"opentrons": self.SERVER},
            {"opentrons": [{"hint": "recipe", "folder": "Library/Elsewhere"}]},
        )
        assert registry.hints["recipe"].origin == "server:opentrons"
        assert any("is ignored" in p for p in registry.problems)

    def test_the_config_speaks_for_a_server_that_cannot(self):
        registry = build_registry(
            {"mobile": []},
            {"mobile": [{"hint": "mobile_task", "dynamic": True,
                         "source_property": "AvailableTasks"}]},
        )
        assert registry.hints["mobile_task"].origin == "config"

    def test_lab_wide_hints_come_from_ui_dropdowns(self):
        registry = build_registry({}, None, {"plate": {
            "sources": [{"glob": "Library/Labware/Plates/*.plate.json", "field": "id"}]}})
        assert "plate" in registry.hints

    def test_a_hint_two_servers_both_offer_is_not_an_error(self):
        # Two liquid handlers both reading recipes is normal.
        registry = build_registry({"a": self.SERVER, "b": self.SERVER})
        assert registry.problems == []
        assert registry.hints["recipe"].origin in ("server:a", "server:b")

    def test_a_parameter_resolves_back_to_its_hint(self):
        registry = build_registry(
            {"opentrons": [{"hint": "recipe", "folder": "Library/Recipes",
                            "parameter_names": ["RecipeName"]}]})
        assert registry.hint_for_parameter("RecipeName") == "recipe"
        assert registry.hint_for_parameter("Nope") == ""


# ── The part that must not have changed ───────────────────────────────────────

class TestTheShippedHintsMatchTheOldHardcodedLogic:
    """These four had working implementations in code. The declarations have to
    reproduce them exactly, not approximately — they feed the pickers an
    operator uses to choose labware, and a missing entry is a run that fails."""

    @pytest.fixture(scope="class")
    @staticmethod
    def registry():
        config = yaml.safe_load((V1_DIR / "lab_config.yaml").read_text(encoding="utf-8"))
        return build_registry({}, None, config.get("ui_dropdowns"))

    @staticmethod
    def _old_plate():
        folder = V1_DIR / "Library" / "Labware" / "Plates"
        return sorted(json.loads(f.read_text(encoding="utf-8-sig"))["id"]
                      for f in folder.glob("*.plate.json"))

    @staticmethod
    def _old_labware():
        names, root = set(), V1_DIR / "Library" / "Labware"
        for plate in root.glob("Plates/*.plate.json"):
            names.add(json.loads(plate.read_text(encoding="utf-8-sig"))["id"])
        for reservoir in root.glob("Reservoirs/*.json"):
            data = json.loads(reservoir.read_text(encoding="utf-8-sig"))
            load_name = (data.get("parameters") or {}).get("loadName") or data.get("id")
            if load_name:
                names.add(load_name)
        index = root / "TipRacks" / "index.json"
        if index.exists():
            names.update(json.loads(index.read_text(encoding="utf-8-sig")).get("load_names", []))
        return sorted(names)

    @staticmethod
    def _old_tiprack():
        index = V1_DIR / "Library" / "Labware" / "TipRacks" / "index.json"
        return json.loads(index.read_text(encoding="utf-8-sig")).get("load_names", [])

    @staticmethod
    def _old_pipette():
        index = V1_DIR / "Library" / "Labware" / "Pipettes" / "index.json"
        data, seen, out = json.loads(index.read_text(encoding="utf-8-sig")), set(), []
        for d in data.get("definitions", []):
            key = f"{d['channel']}/{d['model']}"
            if key not in seen:
                seen.add(key)
                out.append(key)
        return out

    @pytest.mark.parametrize("hint", ["plate", "labware", "tiprack", "pipette"])
    def test_the_declaration_offers_exactly_what_the_code_did(self, registry, hint):
        old = getattr(self, f"_old_{hint}")()
        new = registry.options(V1_DIR, hint)
        assert sorted(new) == sorted(old), (
            f"{hint}: only in old {sorted(set(old) - set(new))}, "
            f"only in new {sorted(set(new) - set(old))}"
        )
        assert old, f"{hint} resolved to nothing — the test proves nothing"

    def test_the_shipped_declarations_have_no_problems(self, registry):
        assert registry.problems == []

    def test_mobile_task_does_not_offer_its_own_filenames(self, registry):
        # Declaring the snapshot folder as a *source* made `_options` — the
        # snapshot index file — show up as a pickable robot task.
        spec = registry.hints["mobile_task"]
        assert spec.dynamic
        assert "_options" not in spec.resolve(V1_DIR)
