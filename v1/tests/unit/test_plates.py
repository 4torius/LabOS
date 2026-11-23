"""Following plates step by step for the Workflow Builder (src/plates.py)."""
from src.plates import PlateStep, track

OWNERS = {"slot:tecan": "tecan", "slot:opentrons": "opentrons"}
owner = OWNERS.get

PLACE_OT = dict(carrier="mobile", acquires=frozenset({"slot:opentrons"}))
PICK_OT = dict(carrier="mobile", releases=frozenset({"slot:opentrons"}))
PLACE_TC = dict(carrier="mobile", acquires=frozenset({"slot:tecan"}))
PICK_TC = dict(carrier="mobile", releases=frozenset({"slot:tecan"}))
RECIPE = dict(carrier="opentrons", needs=frozenset({"slot:opentrons"}))
READ = dict(carrier="tecan", needs=frozenset({"slot:tecan"}))


def _run(*steps):
    return track([PlateStep(number=i + 1, plate=p, **kw) for i, (p, kw) in enumerate(steps)], owner)


def _warnings(reports):
    return [(r.number, w) for r in reports for w in r.warnings]


def test_one_plate_through_both_instruments_draws_no_warning():
    reports = _run(("A", PLACE_OT), ("A", RECIPE), ("A", PICK_OT),
                   ("A", PLACE_TC), ("A", READ), ("A", PICK_TC))
    assert _warnings(reports) == []
    assert reports[1].after == {"A": "in opentrons"}
    assert reports[-1].after == {"A": "on mobile"}


def test_reading_a_plate_that_is_still_on_the_deck():
    reports = _run(("A", PLACE_OT), ("A", READ))
    assert _warnings(reports) == [
        (2, "works on plate A in tecan, but at this point it is in opentrons")]


def test_two_plates_on_one_deck():
    reports = _run(("A", PLACE_OT), ("B", PLACE_OT))
    assert _warnings(reports) == [
        (2, "puts plate B into opentrons, but plate A is still in it")]


def test_taking_out_a_plate_that_is_not_there():
    reports = _run(("A", PLACE_TC), ("B", PICK_TC))
    assert "plate A is in it" in _warnings(reports)[0][1]


def test_the_robot_cannot_carry_two_plates():
    reports = _run(("A", PLACE_OT), ("B", PLACE_TC), ("A", PICK_OT), ("B", PICK_TC))
    assert (4, "mobile would be carrying plates A, B at once") in _warnings(reports)


def test_a_plate_nobody_moved_yet_is_not_known_and_not_warned_about():
    # Loaded by hand: the read has no step that put the plate in the reader.
    assert _warnings(_run(("A", READ))) == []


def test_an_unlabelled_step_still_occupies_the_place():
    reports = _run(("", PLACE_TC), ("A", PLACE_TC))
    assert "a plate with no label is still in it" in _warnings(reports)[0][1]


LOAD = dict(carrier="manual_station", move="onto:mobile")
COLLECT = dict(carrier="manual_station", move="off")


def test_the_operator_loading_a_second_plate_onto_a_loaded_robot():
    reports = _run(("A", LOAD), ("B", LOAD))
    assert _warnings(reports) == [(2, "mobile would be carrying plates A, B at once")]


def test_a_plate_the_operator_took_away_is_forgotten():
    reports = _run(("A", LOAD), ("A", PLACE_TC), ("A", PICK_TC), ("A", COLLECT),
                   ("B", LOAD), ("B", PLACE_TC))
    assert _warnings(reports) == []
    assert reports[3].after == {}


def test_the_two_plate_example_with_the_operator_in_it():
    reports = _run(("A", LOAD), ("A", PLACE_OT), ("A", RECIPE), ("A", PICK_OT),
                   ("A", PLACE_TC), ("A", READ), ("B", LOAD), ("B", PLACE_OT),
                   ("B", RECIPE), ("A", PICK_TC), ("A", COLLECT), ("B", PICK_OT),
                   ("B", PLACE_TC), ("B", READ), ("B", PICK_TC), ("B", COLLECT))
    assert _warnings(reports) == []


def test_forgetting_to_take_a_plate_off_the_robot():
    # A in the reader while B is pipetted, then they swap - Esempio_Due_Piastre.
    reports = _run(("A", PLACE_OT), ("A", RECIPE), ("A", PICK_OT), ("A", PLACE_TC),
                   ("A", READ), ("B", PLACE_OT), ("B", RECIPE), ("A", PICK_TC),
                   ("B", PICK_OT), ("B", PLACE_TC), ("B", READ), ("B", PICK_TC))
    # Steps 8-9: the robot takes A out of the reader, then B off the deck
    # while still holding A. That is the real hazard and it is reported.
    # With no operator step, A is never taken off the robot, so every later
    # robot move with B on board says so again.
    assert _warnings(reports)[0] == (9, "mobile would be carrying plates A, B at once")
