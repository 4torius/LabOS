"""Structural checks on the front-end assets.

There is no JavaScript test runner here and no browser, so this cannot prove
the UI behaves. What it can prove is that the files are not *broken* — which is
the failure mode that actually happens when templates are edited by string
surgery: an unbalanced brace, an unterminated string, a `<script>` left open.
Those turn into a blank page, and nothing else in the suite would notice.

It also guards the specific invented affordances that were removed, because
they came back once already.
"""
import re
import sys
from pathlib import Path

import pytest

V1_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V1_DIR))

STATIC_JS = sorted((V1_DIR / "webapp" / "static" / "js").glob("*.js"))
TEMPLATES = sorted((V1_DIR / "webapp" / "templates").glob("*.html"))


def _strip_js(source: str) -> str:
    """Remove comments and string literals, leaving only structure.

    Whatever is left must have balanced brackets. Written as a small state
    machine rather than regexes because JavaScript is not regular in the two
    places that matter here:

    * `/` is division or the start of a regex depending on what came before;
    * a template literal can contain `${ ... }`, and the expression inside is
      ordinary JavaScript that may itself contain another template literal.
      These templates do exactly that — ``html += `... ${x ? `<b>y</b>` : ''}` ``
      — so a scanner that treats a backtick literal as opaque closes the outer
      one at the inner one's opening backtick and mis-reads the rest of the
      file as a string.

    Contexts are therefore a stack: JS, template, JS-inside-interpolation, …
    """
    out = []
    # Each frame: ("js", brace_depth) or ("tmpl", None). The bottom frame is the
    # file itself; an interpolation pushes a "js" frame that ends at the `}`
    # matching its `${`.
    stack = [["js", 0]]
    i, n = 0, len(source)
    prev_significant = ""

    def frame():
        return stack[-1][0]

    while i < n:
        ch = source[i]
        nxt = source[i + 1] if i + 1 < n else ""

        if frame() == "tmpl":
            if ch == "\\":
                i += 2
                continue
            if ch == "$" and nxt == "{":
                stack.append(["js", 0])
                i += 2
                prev_significant = ""
                continue
            if ch == "`":
                stack.pop()
                prev_significant = "x"      # a literal is a value
                i += 1
                continue
            i += 1
            continue

        # ── ordinary JavaScript ────────────────────────────────────────────
        if ch == "/" and nxt == "/":
            while i < n and source[i] != "\n":
                i += 1
            continue
        if ch == "/" and nxt == "*":
            closing = source.find("*/", i + 2)
            if closing == -1:
                raise AssertionError("unterminated /* block comment")
            i = closing + 2
            continue
        if ch in "'\"":
            quote, i = ch, i + 1
            while i < n and source[i] != quote:
                if source[i] == "\\":
                    i += 1
                i += 1
            if i >= n:
                raise AssertionError(f"unterminated {quote} string literal")
            i += 1
            prev_significant = "x"
            continue
        if ch == "`":
            stack.append(["tmpl", None])
            i += 1
            continue
        if ch == "/" and prev_significant not in (")", "]", "}") and not prev_significant.isalnum():
            # A regex literal, not division.
            i += 1
            in_class = False
            while i < n:
                if source[i] == "\\":
                    i += 2
                    continue
                if source[i] == "[":
                    in_class = True
                elif source[i] == "]":
                    in_class = False
                elif source[i] == "/" and not in_class:
                    break
                i += 1
            i += 1
            prev_significant = "x"
            continue

        if ch == "{":
            stack[-1][1] += 1
        elif ch == "}":
            if stack[-1][1] == 0 and len(stack) > 1:
                # Closes the `${` that opened this frame; back into the template.
                stack.pop()
                i += 1
                continue
            stack[-1][1] -= 1

        out.append(ch)
        if not ch.isspace():
            prev_significant = ch
        i += 1

    if frame() == "tmpl":
        raise AssertionError("unterminated ` template literal")
    return "".join(out)


def _assert_balanced(source: str, label: str) -> None:
    pairs = {")": "(", "]": "[", "}": "{"}
    stack = []
    for index, ch in enumerate(source):
        if ch in "([{":
            stack.append((ch, index))
        elif ch in pairs:
            if not stack:
                pytest.fail(f"{label}: unexpected '{ch}' with nothing open")
            opener, _ = stack.pop()
            if opener != pairs[ch]:
                pytest.fail(f"{label}: '{opener}' closed by '{ch}'")
    if stack:
        pytest.fail(f"{label}: {len(stack)} unclosed {stack[-1][0]}")


class TestStaticModules:

    def test_the_shared_modules_exist(self):
        names = {p.name for p in STATIC_JS}
        assert {"labos-core.js", "labos-ws.js", "labos-params.js"} <= names

    def test_param_form_shared_is_gone(self):
        """Superseded by labos-params.js; two copies is how it drifted before."""
        assert not (V1_DIR / "webapp" / "static" / "js" / "param-form-shared.js").exists()

    @pytest.mark.parametrize("path", STATIC_JS, ids=lambda p: p.name)
    def test_module_is_structurally_sound(self, path):
        _assert_balanced(_strip_js(path.read_text(encoding="utf-8")), path.name)

    @pytest.mark.parametrize("path", STATIC_JS, ids=lambda p: p.name)
    def test_module_exposes_itself_on_the_one_global(self, path):
        source = path.read_text(encoding="utf-8")
        assert "LabOS" in source, f"{path.name} defines nothing shared"


class TestTemplateScripts:

    @pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.name)
    def test_script_tags_are_balanced(self, path):
        source = path.read_text(encoding="utf-8")
        opened = len(re.findall(r"<script\b", source))
        closed = len(re.findall(r"</script>", source))
        assert opened == closed, f"{path.name}: {opened} <script> vs {closed} </script>"

    @pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.name)
    def test_inline_script_blocks_are_structurally_sound(self, path):
        source = path.read_text(encoding="utf-8")
        for index, block in enumerate(re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>",
                                                 source, re.S)):
            # Jinja expressions are not JavaScript; they are substituted before
            # the browser ever sees the file.
            block = re.sub(r"\{\{.*?\}\}|\{%.*?%\}", "0", block, flags=re.S)
            _assert_balanced(_strip_js(block), f"{path.name} block {index + 1}")


class TestRemovedAffordances:
    """Each of these offered the operator something that does not exist."""

    def test_no_incubator_in_the_workflow_toolbox(self):
        source = (V1_DIR / "webapp" / "templates" / "workflows.html").read_text(encoding="utf-8")
        assert 'data-type="incubator"' not in source

    def test_no_hardcoded_labware_load_names(self):
        """tiprack_1000ul and friends match nothing Opentrons knows."""
        source = (V1_DIR / "webapp" / "templates" / "dashboard.html").read_text(encoding="utf-8")
        for invented in ("'tiprack_1000ul'", "'wellplate_96'", "'reservoir_12'"):
            assert invented not in source, f"{invented} is back in the labware picker"

    def test_no_hardcoded_locations(self):
        source = (V1_DIR / "webapp" / "templates" / "dashboard.html").read_text(encoding="utf-8")
        for invented in ("'rack_1'", "'rack_2'", "'rack_3'"):
            assert invented not in source


class TestWebSocketModule:

    @property
    def source(self):
        return (V1_DIR / "webapp" / "static" / "js" / "labos-ws.js").read_text(encoding="utf-8")

    def test_the_heartbeat_checks_the_socket_is_open(self):
        """It used to call ws.send() unguarded on a timer, throwing every 30
        seconds for as long as the socket was down."""
        assert "readyState === WebSocket.OPEN" in self.source

    def test_reconnection_backs_off(self):
        assert "MAX_RECONNECT_DELAY" in self.source

    def test_a_failing_handler_cannot_take_out_the_others(self):
        """The status bar and the intervention modal are on this socket too."""
        assert "try {" in self.source and "console.error" in self.source

    def test_base_template_subscribes_rather_than_switching_on_type(self):
        base = (V1_DIR / "webapp" / "templates" / "base.html").read_text(encoding="utf-8")
        assert "LabOS.ws.on(" in base
        assert "function handleWebSocketMessage" not in base
