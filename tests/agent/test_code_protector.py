import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent.code_protector import CodeProtector


def test_fenced_block_roundtrip():
    text = "Run this:\n```python\nprint('hello')\n```\nThen restart."
    p = CodeProtector()
    protected = p.protect(text)
    assert "print" not in protected
    assert p.restore(protected) == text


def test_inline_code_roundtrip():
    text = "Use the `pip install` command with `--user` flag."
    p = CodeProtector()
    protected = p.protect(text)
    assert "pip install" not in protected
    assert p.restore(protected) == text


def test_html_tag_roundtrip():
    text = "Click the <b>Start</b> button to begin."
    p = CodeProtector()
    protected = p.protect(text)
    assert "<b>" not in protected
    assert p.restore(protected) == text


def test_placeholder_indexing():
    text = "First `a` then `b`."
    p = CodeProtector()
    protected = p.protect(text)
    assert "__PROTECTED_BLOCK_0__" in protected
    assert "__PROTECTED_BLOCK_1__" in protected
    assert p.integrity_ok(protected)


def test_integrity_detects_missing_placeholder():
    p = CodeProtector()
    protected = p.protect("Code: `x = 1`")
    assert p.integrity_ok(protected)
    assert not p.integrity_ok(protected.replace("__PROTECTED_BLOCK_0__", ""))


def test_no_blocks_is_noop():
    text = "Plain text with no code at all."
    p = CodeProtector()
    protected = p.protect(text)
    assert protected == text
    assert p.restore(protected) == text
