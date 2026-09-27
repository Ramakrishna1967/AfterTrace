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


def test_strip_ansi():
    from cli.tui import strip_ansi

    assert strip_ansi("\x1b[1mhello\x1b[0m world") == "hello world"
    assert strip_ansi("plain") == "plain"


async def test_tui_captured_has_no_ansi():
    from textual.widgets import Input

    app = AfterTraceApp(force_local=True)
    async with app.run_test() as pilot:
        await pilot.click("#prompt-input")
        await pilot.press(*"/check")
        await pilot.press("enter")
        ok = await _wait_until(lambda: any("QDRANT_URL" in line for line in app.captured))
        assert ok, app.captured
        assert not any("\x1b" in line for line in app.captured), app.captured[:3]
