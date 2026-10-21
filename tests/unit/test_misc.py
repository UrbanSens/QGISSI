"""Registry validation, code guard, scrubbing, pricing and Markdown rendering."""
import pytest

from qgissi.core.code_guard import check_code
from qgissi.core.pricing import cost_usd, format_cost
from qgissi.core.scrub import scrub
from qgissi.providers.base import Usage
from qgissi.tools.registry import ToolDef, ToolRegistry
from qgissi.ui.markdown import to_html

DEF = ToolDef("t", "d", lambda ctx, **k: k, parameters={
    "n": {"type": "integer"}, "x": {"type": "number"}, "flag": {"type": "boolean"},
    "mode": {"type": "string", "enum": ["fast", "slow"]}, "items": {"type": "array", "items": {"type": "integer"}},
    "obj": {"type": "object"}}, required=["n"])


def test_lenient_coercion_for_weaker_models():
    clean, errors = ToolRegistry().validate(DEF, {"n": "4", "x": "2.5", "flag": "yes", "mode": "FAST",
                                                  "items": "[1, 2]", "obj": "{\"a\": 1}", "extra": 1})
    assert not errors
    assert clean == {"n": 4, "x": 2.5, "flag": True, "mode": "fast", "items": [1, 2], "obj": {"a": 1}}


def test_validation_errors_are_specific():
    _, errors = ToolRegistry().validate(DEF, {"x": "abc", "mode": "medium"})
    assert "'x' must be of type number" in errors and any("one of" in e for e in errors)
    assert "missing required argument 'n'" in errors


def test_annotations_follow_risk():
    destructive = ToolDef("d", "", None, risk="destructive")
    assert destructive.annotations() == {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": False}


@pytest.mark.parametrize("code,ok", [
    ("result = layer('a').featureCount()", True),
    ("names = ['a']\nnames.remove('a')", True),
    ("import subprocess", False),
    ("import os\nos.system('ls')", False),
    ("import shutil\nshutil.rmtree('/')", False),
    ("from pathlib import Path\nPath('x').unlink()", False),
    ("x = ().__class__.__bases__[0].__subclasses__()", False),
    ("eval('1+1')", False),
    ("QgsProject.instance().clear()", False),
    ("import sys\nsys.settrace(None)", False),
    ("def broken(:\n  pass", False),
])
def test_code_guard(code, ok):
    assert check_code(code).ok is ok


def test_code_guard_flags_file_writes():
    assert check_code("open('out.txt', 'w').write('x')").writes_files


def test_scrub_masks_secrets():
    text = ("key sk-ant-api03-abcdefghijklmnopqrstuv, Bearer abcdefghijklmnop, AIzaSyA1234567890123456789012345678901, "
            "postgres://user:hunter2@db/x, url?token=abc123&x=1")
    out = scrub(text)
    for secret in ("abcdefghijklmnopqrstuv", "hunter2", "abc123", "AIzaSyA1234567890"):
        assert secret not in out
    assert "x=1" in out


def test_pricing_known_and_unknown_models():
    usage = Usage(input_tokens=1_000_000, output_tokens=100_000, cache_read_tokens=1_000_000, cache_write_tokens=0)
    assert cost_usd("claude-opus-5-5", usage) == pytest.approx(4.0 + 2.0 + 0.2)
    assert cost_usd("unknown-model", usage, "openai") is None
    assert cost_usd("unknown-model", usage, "openai", {"unknown-model": [1, 2]}) == pytest.approx(1.0 + 0.2 + 0.1)
    assert cost_usd("mistral:latest", usage, "ollama") == 0
    long = Usage(input_tokens=200_000, output_tokens=0)
    assert cost_usd("claude-haiku-5-5", long) == pytest.approx(0.1)  # long-context tier ($0.50/MTok)
    assert format_cost(None) == "n/a" and format_cost(0.004) == "$0.0040"


def test_markdown_rendering():
    html = to_html("# Title\n\n**bold** and `code`\n\n- a\n- b\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n```\nx < y\n```")
    assert "<h3>Title</h3>" in html and "<b>bold</b>" in html and "<code>code</code>" in html
    assert "<ul><li>a</li><li>b</li></ul>" in html and "<td>1</td>" in html and "x &lt; y" in html
    assert "<script>" not in to_html("<script>alert(1)</script>")
