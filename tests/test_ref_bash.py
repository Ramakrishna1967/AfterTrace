"""Unit tests for @ file refs and ! bash tool."""

import os

from cli import bash_tool, file_ref


def test_extract_refs():
    assert file_ref.extract_refs("how is auth in @cli/agent.py?") == ["cli/agent.py"]
    assert file_ref.extract_refs("compare @docs/a and @b.txt") == ["docs/a", "b.txt"]
    assert file_ref.extract_refs("no refs here") == []


def test_resolve_exact_and_traversal_guard(tmp_path):
    root = tmp_path / "proj"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "note.txt").write_text("hi", encoding="utf-8")
    assert file_ref.resolve_ref("sub/note.txt", str(root)) == ["sub/note.txt"]
    assert file_ref.resolve_ref("../outside.txt", str(root)) == []
    assert file_ref.resolve_ref("..", str(root)) == []


def test_resolve_fuzzy_and_skip_dirs(tmp_path):
    root = tmp_path / "proj"
    (root / "cli").mkdir(parents=True)
    (root / ".data").mkdir(parents=True)
    (root / "cli" / "agent.py").write_text("x", encoding="utf-8")
    (root / ".data" / "agent.py").write_text("x", encoding="utf-8")
    hits = file_ref.resolve_ref("agent", str(root))
    assert "cli/agent.py" in hits
    assert ".data/agent.py" not in hits


def test_read_ref_caps_redacts_and_skips_binary(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "k.txt").write_text("api-key=ABC123\n" + "y" * 9000, encoding="utf-8")
    text, note = file_ref.read_ref("k.txt", str(root))
    assert "ABC123" not in text
    assert "[redacted]" in text
    assert "truncated" in note
    (root / "b.bin").write_bytes(b"\x00\x01\x02")
    text, note = file_ref.read_ref("b.bin", str(root))
    assert text == "" and "binary" in note
    text, note = file_ref.read_ref("../escape.txt", str(root))
    assert text == "" and "outside" in note


def test_bash_echo_and_failure():
    r = bash_tool.run_bash("echo hello-aftertrace")
    assert r["exit"] == 0 and "hello-aftertrace" in r["output"]
    r = bash_tool.run_bash("")
    assert r["exit"] == 2
    r = bash_tool.run_bash("exit 3")
    assert r["exit"] == 3


def test_bash_timeout_and_cap():
    r = bash_tool.run_bash("python -c \"print('z'*9000)\"", cap=100)
    assert r["truncated"] is True and len(r["output"]) == 100
    r = bash_tool.run_bash('python -c "import time; time.sleep(30)"', timeout=1)
    assert r["exit"] == 124 and "timed out" in r["note"]
    assert os.path.isdir(bash_tool.project_root())
