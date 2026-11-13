"""The builder's dependency bookkeeping, exercised in a real JS engine.

`dependsOn` holds 1-based POSITIONS, not stable ids, and the builder reorders
and deletes steps by splicing an array. Those two facts used to be left to
disagree: deleting step 1 turned "depends on 2" into a reference to whatever
ended up in position 2.

That was cosmetic while nothing read the dependencies. It stopped being
cosmetic when DependsOn became what orders execution — a stale number now
silently reorders or drops a physical step. The remapping lives in
labos-core.js as two pure functions so it can be run here rather than only
eyeballed.
"""
import json
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

V1_DIR = Path(__file__).resolve().parents[2]
CORE_JS = V1_DIR / "webapp" / "static" / "js" / "labos-core.js"
BUILDER = (V1_DIR / "webapp" / "templates" / "workflows.html").read_text(encoding="utf-8")

node = shutil.which("node")
needs_node = pytest.mark.skipif(node is None, reason="node is not installed")


def _run(expression, steps, *args):
    """Evaluate one LabOS helper in node and return its result."""
    script = textwrap.dedent(f"""
        global.window = global;
        require({str(CORE_JS)!r});
        const steps = {json.dumps(steps)};
        process.stdout.write(JSON.stringify(
            LabOS.{expression}(steps, ...{json.dumps(list(args))})));
    """)
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _chain(n):
    """A straight chain: step 1 first, each later step on the previous one."""
    return [{"dependsOn": [] if i == 0 else [i]} for i in range(n)]


@needs_node
class TestDeletingAStep:

    def test_the_chain_closes_up(self):
        # 1→2→3→4, delete the middle: the survivors stay a chain.
        assert _run("dependenciesAfterRemove", _chain(4), 1) == [[], [1], [2]]

    def test_deleting_the_first_step_drops_the_reference_to_it(self):
        # The exact old bug: this used to leave [[1],[2]], so the new first
        # step depended on itself and the rest pointed one place too far.
        assert _run("dependenciesAfterRemove", _chain(3), 0) == [[], [1]]

    def test_deleting_the_last_step_changes_nothing_earlier(self):
        assert _run("dependenciesAfterRemove", _chain(3), 2) == [[], [1]]

    def test_a_step_with_several_dependencies_keeps_the_survivors(self):
        steps = [{"dependsOn": []}, {"dependsOn": []}, {"dependsOn": [1, 2]}]
        assert _run("dependenciesAfterRemove", steps, 0) == [[], [1]]

    def test_removing_the_only_step_leaves_nothing(self):
        assert _run("dependenciesAfterRemove", [{"dependsOn": []}], 0) == []


@needs_node
class TestMovingAStep:

    def test_a_reference_follows_the_step_it_pointed_at(self):
        # Move the last step of 1→2→3→4 to the front. It still depends on what
        # was step 3, which is now in position 4 — a backwards edge, which is a
        # valid DAG and runs in the author's order. Rewriting the graph to match
        # the new positions would change what they asked for.
        assert _run("dependenciesAfterMove", _chain(4), 3, 0) == [[4], [], [2], [3]]

    def test_swapping_two_adjacent_steps_keeps_the_edge_between_them(self):
        assert _run("dependenciesAfterMove", _chain(2), 0, 1) == [[2], []]

    def test_moving_a_step_that_nothing_references_is_a_reorder_only(self):
        steps = [{"dependsOn": []}, {"dependsOn": [1]}, {"dependsOn": []}]
        assert _run("dependenciesAfterMove", steps, 2, 0) == [[], [], [2]]

    def test_a_move_that_goes_nowhere_is_the_identity(self):
        assert _run("dependenciesAfterMove", _chain(3), 1, 1) == [[], [1], [2]]


@needs_node
class TestRoundTrips:

    def test_delete_then_the_graph_is_still_acyclic_and_in_range(self):
        remaining = _run("dependenciesAfterRemove", _chain(5), 2)
        for position, deps in enumerate(remaining, start=1):
            for dep in deps:
                assert 1 <= dep <= len(remaining)
                assert dep != position, "a step was left depending on itself"

    def test_move_preserves_how_many_edges_there_are(self):
        before = _chain(5)
        after = _run("dependenciesAfterMove", before, 4, 0)
        assert sum(len(s["dependsOn"]) for s in before) == sum(len(d) for d in after)


class TestTheBuilderUsesThem:
    """Structural: the helpers only help if the splice sites actually call them."""

    def test_a_new_step_is_chained_onto_the_previous_one(self):
        assert "dependsOn: workflowSteps.length > 0 ? [workflowSteps.length] : []" in BUILDER, (
            "a new step must default to depending on the one before it"
        )

    @pytest.mark.parametrize("site,helper", [
        ("function removeStep", "LabOS.dependenciesAfterRemove"),
        ("function moveStep", "LabOS.dependenciesAfterMove"),
    ])
    def test_every_splice_site_renumbers(self, site, helper):
        body = BUILDER.split(site)[1].split("\nfunction ")[0]
        assert "splice" in body, f"{site} no longer splices — check this test"
        assert helper in body, f"{site} splices without renumbering dependencies"

    def test_the_helpers_are_in_the_shared_module(self):
        source = CORE_JS.read_text(encoding="utf-8")
        for helper in ("dependenciesAfterRemove", "dependenciesAfterMove"):
            assert f"LabOS.{helper} = function" in source


@needs_node
class TestDeletingClosesOverTheRemovedStep:
    """Deleting a step must not quietly remove an ordering constraint.

    Dropping the reference would be the literal reading, and the wrong one: in
    a sequential workflow it leaves the next step with no predecessor, so two
    physical steps that were ordered become eligible to run at the same time.
    Whatever depended on the removed step inherits what it depended on.
    """

    def test_the_middle_of_a_chain_closes_up(self):
        assert _run("dependenciesAfterRemove", _chain(3), 1) == [[], [1]]

    def test_a_longer_chain_stays_a_chain(self):
        after = _run("dependenciesAfterRemove", _chain(5), 2)
        assert after == [[], [1], [2], [3]]

    def test_a_fan_in_keeps_every_surviving_edge(self):
        steps = [{"dependsOn": []}, {"dependsOn": []},
                 {"dependsOn": [1, 2]}, {"dependsOn": [3]}]
        # Delete step 3: step 4 inherits both of its predecessors.
        assert _run("dependenciesAfterRemove", steps, 2) == [[], [], [1, 2]]

    def test_a_step_does_not_inherit_a_reference_to_itself(self):
        # Removed step 2 depended on 3, and 3 depended on 2. Closing over it
        # must not leave 3 depending on 3.
        steps = [{"dependsOn": []}, {"dependsOn": [3]}, {"dependsOn": [2]}]
        after = _run("dependenciesAfterRemove", steps, 1)
        for position, deps in enumerate(after, start=1):
            assert position not in deps

    def test_removing_a_root_leaves_its_dependents_as_roots(self):
        after = _run("dependenciesAfterRemove", _chain(3), 0)
        assert after[0] == [], "the step after a deleted root should be a root"

    def test_no_duplicate_edges_are_introduced(self):
        # Step 4 already depends on 1, and inherits 1 again through step 3.
        steps = [{"dependsOn": []}, {"dependsOn": [1]},
                 {"dependsOn": [1]}, {"dependsOn": [1, 3]}]
        after = _run("dependenciesAfterRemove", steps, 2)
        for deps in after:
            assert len(deps) == len(set(deps))
