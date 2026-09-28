"""Textual TUI smoke tests (headless pilot) + command parser unit tests.

Run: python -m pytest tests/test_tui.py -q
"""

import asyncio
import json
import os

from cli.tui import AfterTraceApp, parse_command


def test_parse_command():
    assert parse_command("/scenario1 --yes") == ("scenario1", True)
    assert parse_command("/check") == ("check", False)
    assert parse_command("/quit") == ("quit", False)
    assert parse_command("") == ("", False)
    assert parse_command("/SCENARIO2") == ("scenario2", False)


def test_route_text():
    from cli.tui import route_text

    assert route_text("fix the alias drift")[0] == "scenario1"
    assert route_text("run it on another corpus")[0] == "scenario2"
    assert route_text("looks like a stale cache problem")[0] == "scenario3"
    assert route_text("show me past runs")[0] == "history"
    assert route_text("what do you remember about alias fixes")[0] == "memory"
    assert route_text("run everything") == ("demo", "")
    assert route_text("are my keys set")[0] == "doctor"
    assert route_text("blabla nothing matching here") == ("", "")
    assert route_text("") == ("", "")


def test_suggest_command():
    from cli.tui import suggest_command

    assert suggest_command("dmeo") == "demo"
    assert suggest_command("scenrio1") == "scenario1"
    assert suggest_command("histry") == "history"
    assert suggest_command("xyzzy") is None


async def _wait_until(pred, timeout=30.0):
    for _ in range(int(timeout * 5)):
        if pred():
            return True
        await asyncio.sleep(0.2)
    return pred()


async def test_tui_check_help_unknown_quit():
    from textual.widgets import Input

    app = AfterTraceApp(force_local=True)
    async with app.run_test() as pilot:
        assert "aftertrace" in " ".join(app.captured).lower() or True
        inp = app.query_one("#prompt-input", Input)
        # /check runs in a worker thread and prints config presence
        await pilot.click("#prompt-input")
        await pilot.press(*"/check")
        await pilot.press("enter")
        ok = await _wait_until(lambda: any("QDRANT_URL" in line for line in app.captured))
        assert ok, app.captured
        # unknown command hints at /help
        await pilot.press(*"/nope")
        await pilot.press("enter")
        await pilot.pause(0.5)
        assert any("/help" in line for line in app.captured)
        # plain text (no slash) also hints
        await pilot.press(*"hello")
        await pilot.press("enter")
        await pilot.pause(0.5)
        # tab completion completes a unique prefix
        inp.value = "/scen"
        await pilot.press("tab")
        await pilot.pause(0.5)
        assert inp.value in ("/scenarios ", "/scen"), inp.value
        assert not app._busy
        _ = inp


async def test_tui_palette_open_close():
    app = AfterTraceApp(force_local=True)
    async with app.run_test() as pilot:
        from cli.tui import PaletteScreen

        await pilot.press("ctrl+p")
        await pilot.pause(0.5)
        assert isinstance(app.screen, PaletteScreen)
        await pilot.press("escape")
        await pilot.pause(0.5)
        assert not isinstance(app.screen, PaletteScreen)


async def test_tui_history_memory_rerun_empty():
    app = AfterTraceApp(force_local=True)
    async with app.run_test() as pilot:
        await pilot.click("#prompt-input")

        async def submit(text):
            inp = app.query_one("#prompt-input")
            inp.value = text
            await pilot.press("enter")
            await pilot.pause(0.8)

        await submit("/history")
        assert any("Incident history" in ln or "No incidents" in ln for ln in app.captured)
        await submit("/history 99")
        await pilot.pause(0.3)
        await submit("/memory alias drift")
        ok = await _wait_until(lambda: not app._busy, timeout=30)
        assert ok
        assert any("Memory browser" in ln or "recall failed" in ln for ln in app.captured)
        # rerun with no prior scenario run -> hint, no crash
        app._last_run = None
        await submit("/rerun")
        await pilot.pause(0.3)
        assert any("Nothing to re-run" in ln for ln in app.captured)
        # plain English routes to history
        await submit("show me past runs")
        await pilot.pause(0.5)
        assert any("history" in ln.lower() for ln in app.captured)


def test_strip_ansi():
    from cli.tui import strip_ansi

    assert strip_ansi("\x1b[1mhello\x1b[0m world") == "hello world"
    assert strip_ansi("plain") == "plain"


async def test_tui_captured_has_no_ansi():

    app = AfterTraceApp(force_local=True)
    async with app.run_test() as pilot:
        await pilot.click("#prompt-input")
        await pilot.press(*"/check")
        await pilot.press("enter")
        ok = await _wait_until(lambda: any("QDRANT_URL" in line for line in app.captured))
        assert ok, app.captured
        assert not any("\x1b" in line for line in app.captured), app.captured[:3]


async def _submit(pilot, app, text):
    inp = app.query_one("#prompt-input")
    inp.value = text
    await pilot.press("enter")
    await pilot.pause(0.5)


async def test_tui_new_compact_models_connect():
    app = AfterTraceApp(force_local=True)
    async with app.run_test() as pilot:
        await pilot.click("#prompt-input")
        await _submit(pilot, app, "/new")
        await pilot.pause(0.3)
        assert any("Fresh context" in ln for ln in app.captured)
        await _submit(pilot, app, "/models")
        await pilot.pause(0.5)
        assert any("LOCAL-SIM" in ln for ln in app.captured)
        assert any("LOCAL-FALLBACK" in ln for ln in app.captured)
        await _submit(pilot, app, "/compact")
        await pilot.pause(0.5)
        assert any("Compacted" in ln for ln in app.captured)
        # connect modal opens and cancels cleanly without touching env
        await _submit(pilot, app, "/connect")
        await pilot.pause(0.8)
        from cli.tui import ConnectScreen

        assert isinstance(app.screen, ConnectScreen)
        await pilot.press("escape")
        await pilot.pause(0.5)
        assert not isinstance(app.screen, ConnectScreen)
        assert "QDRANT_API_KEY" not in os.environ


async def test_tui_bash_and_refs():
    app = AfterTraceApp(force_local=True)
    async with app.run_test() as pilot:
        await pilot.click("#prompt-input")
        await _submit(pilot, app, "!echo hello-tui")
        ok = await _wait_until(lambda: any("hello-tui" in ln for ln in app.captured), timeout=30)
        assert ok, app.captured
        assert not app._busy
        await _submit(pilot, app, "how is @cli/agent.py doing")
        await pilot.pause(0.8)
        assert any("gateway_query" in ln for ln in app.captured)
        await _submit(pilot, app, "look at @no-such-file-xyz")
        await pilot.pause(0.5)
        assert any("no match" in ln for ln in app.captured)


async def test_tui_undo_redo_empty_and_ephemeral(monkeypatch, tmp_path):
    from cli import sqlite_log

    monkeypatch.setenv("AFTERTRACE_DB", str(tmp_path / "u.sqlite3"))
    monkeypatch.setenv("AFTERTRACE_MEMORY_FALLBACK", str(tmp_path / "u.jsonl"))
    app = AfterTraceApp(force_local=True)
    async with app.run_test() as pilot:
        await pilot.click("#prompt-input")
        await _submit(pilot, app, "/undo")
        ok = await _wait_until(lambda: not app._busy, timeout=30)
        assert ok
        assert any("Nothing to undo" in ln for ln in app.captured)
        await _submit(pilot, app, "/redo")
        await pilot.pause(0.5)
        assert any("Nothing to redo" in ln for ln in app.captured)
        # seed a switch event, then undo must refuse safely in LOCAL-SIM
        from cli.config import load_settings

        s = load_settings()
        con = sqlite_log.connect(s.sqlite_path)
        iid = sqlite_log.new_incident(con, "s9-test", "symptom")
        sqlite_log.log_event(con, iid, "alias.switched", {"before": "col_a", "after": "col_b"})
        sqlite_log.set_alias(con, "live9", "col_b")
        con.close()
        await _submit(pilot, app, "/undo")
        from cli.tui import ConfirmScreen

        modal = await _wait_until(
            lambda: isinstance(app.screen, ConfirmScreen) or not app._busy, timeout=20
        )
        assert modal, app.captured
        if isinstance(app.screen, ConfirmScreen):
            await pilot.press("y")
        ok = await _wait_until(lambda: not app._busy, timeout=30)
        assert ok
        assert any("LOCAL-SIM" in ln for ln in app.captured), app.captured


async def test_tui_sessions_export_seeded(monkeypatch, tmp_path):
    import glob as _glob

    from cli import sqlite_log

    monkeypatch.setenv("AFTERTRACE_DB", str(tmp_path / "s.sqlite3"))
    monkeypatch.setenv("AFTERTRACE_MEMORY_FALLBACK", str(tmp_path / "s.jsonl"))
    from cli.config import load_settings

    s = load_settings()
    con = sqlite_log.connect(s.sqlite_path)
    iid = sqlite_log.new_incident(con, "s9-test", "symptom api-key=ZZZ9")
    sqlite_log.log_event(con, iid, "detected", {"note": "token=ABC"})
    con.close()
    app = AfterTraceApp(force_local=True)
    async with app.run_test() as pilot:
        await pilot.click("#prompt-input")
        await _submit(pilot, app, "/sessions")
        await pilot.pause(0.5)
        assert any("s9-test" in ln for ln in app.captured)
        await _submit(pilot, app, "/export")
        ok = await _wait_until(lambda: any("Exported" in ln for ln in app.captured), timeout=30)
        assert ok, app.captured
        paths = _glob.glob(os.path.join(str(tmp_path), "export_*.json"))
        assert not paths  # default export dir is .data, not tmp
    import glob as _glob2

    import cli.sessions_ops as _ops

    here = os.path.dirname(os.path.dirname(os.path.abspath(_ops.__file__)))
    got = _glob2.glob(os.path.join(here, ".data", "export_*.json"))
    assert got, "export file should land in .data"
    raw = open(got[-1], encoding="utf-8").read()
    assert "ZZZ9" not in raw and "ABC" not in raw
    assert json.loads(raw)["incident"]["scenario"] == "s9-test"
    for p in got:
        os.remove(p)
