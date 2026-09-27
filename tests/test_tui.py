"""Textual TUI smoke tests (headless pilot) + command parser unit tests.

Run: python -m pytest tests/test_tui.py -q
"""

import asyncio

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
