"""Interactive terminal UI for AFTERTRACE, OpenCode-TUI style.

Full-screen dark UI: block logo, centered prompt box with status line,
hints, rotating tips, transcript, bottom bar. Slash commands run the same
scenario/check/reset flows as the argparse CLI, inside worker threads.
Approval prompts appear as modal dialogs (never stdin).

Run: python -m cli tui [--local]
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import os
import re

from rich.console import Console
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Center, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, OptionList, RichLog, Static
from textual.widgets._option_list import Option

VERSION = "0.1.0"

# 5x5 hollow outline glyphs, OpenCode-logo style (only what "aftertrace" needs).
_GLYPHS = {
    "a": [" ███ ", "█   █", "█████", "█   █", "█   █"],
    "f": ["█████", "█    ", "████ ", "█    ", "█    "],
    "t": ["█████", "  █  ", "  █  ", "  █  ", "  █  "],
    "e": [" ████", "█    ", "████ ", "█    ", " ████"],
    "r": ["████ ", "█   █", "████ ", "█  █ ", "█   █"],
    "c": [" ████", "█    ", "█    ", "█    ", " ████"],
}


def block_logo() -> Text:
    """Two-tone block-letter 'aftertrace': dim 'after', bright 'trace'."""
    rows = []
    for i in range(5):
        rows.append(" ".join(_GLYPHS[ch][i] for ch in "after"))
    left = rows
    rows = []
    for i in range(5):
        rows.append(" ".join(_GLYPHS[ch][i] for ch in "trace"))
    right = rows
    out = Text()
    for i in range(5):
        if i:
            out.append("\n")
        out.append(left[i], style="bold #6e6e72")
        out.append("  ")
        out.append(right[i], style="bold #f2f2f4")
    return out


COMMANDS = (
    ("/scenarios", "List the three recovery runs"),
    ("/scenario1", "Cold incident: alias drift, no prior memory"),
    ("/scenario2", "Memory-assisted transfer on a new corpus"),
    ("/scenario3", "Reject a wrong recalled fix (stale cache)"),
    ("/demo", "Ordered demo: reset -> s1 -> s2 -> s3"),
    ("/doctor", "Env + dependency + DB check"),
    ("/history", "Browse past incidents (add a number for detail)"),
    ("/memory", "Browse retained experience (optional query words)"),
    ("/rerun", "Repeat the last scenario/demo run"),
    ("/new", "Fresh transcript context (keeps the log on disk)"),
    ("/sessions", "List recorded sessions"),
    ("/export", "Save an incident as redacted JSON (optional number)"),
    ("/undo", "Revert the last alias switch (asks approval)"),
    ("/redo", "Re-apply an undone alias switch (asks approval)"),
    ("/compact", "Collapse the transcript to key outcomes"),
    ("/models", "Show backend readiness"),
    ("/connect", "Set Cloud keys for this session (never stored)"),
    ("/check", "Show config presence (no secrets)"),
    ("/reset", "Clear local log + fallback memory"),
    ("/clear", "Clear the transcript"),
    ("/help", "Show this help"),
    ("/quit", "Exit the TUI"),
)

TIPS = (
    "Run /scenarios to list the three recovery runs",
    "Memory proposes — live evidence disposes. Approval is always yours.",
    "Scenario 3 rejects a recalled fix when the alias is already correct",
    "Append --yes to skip the approval prompt (non-interactive)",
    "No Cloud keys? Everything runs labeled LOCAL-SIM / LOCAL-FALLBACK",
)


def parse_command(text: str) -> tuple[str, bool]:
    """Split '/scenario1 --yes' -> ('scenario1', True). Pure, unit-tested."""
    parts = text.strip().split()
    if not parts:
        return "", False
    cmd = parts[0][1:] if parts[0].startswith("/") else parts[0]
    return cmd.lower(), "--yes" in parts[1:]


def suggest_command(cmd: str) -> str | None:
    """Closest known command for a typo ('dmeo' -> 'demo'). Pure, unit-tested."""
    import difflib

    known = [c.lstrip("/") for c, _ in COMMANDS] + ["quit", "exit", "q"]
    matches = difflib.get_close_matches(cmd.lower(), known, n=1, cutoff=0.6)
    return matches[0] if matches else None


# keyword -> command for plain-text input. Order matters (first match wins).
_INTENT_RULES = (
    (("histor", "past run", "previous", "log"), "history"),
    (("memor", "recall", "remember", "learned"), "memory"),
    (("cache", "stale cache", "wrong fix", "reject"), "scenario3"),
    (("transfer", "new corpus", "another", "again", "second"), "scenario2"),
    (("alias", "drift", "pointer", "old revision", "stale revision"), "scenario1"),
    (("demo", "all three", "everything", "full run"), "demo"),
    (("status", "config", "setup", "keys", "check", "doctor"), "doctor"),
    (("reset", "wipe", "clean slate", "fresh start"), "reset"),
    (("help", "command", "what can you"), "help"),
    (("quit", "exit", "bye"), "quit"),
    (("fix", "repair", "run", "incident", "broken", "wrong"), "scenarios"),
)


def route_text(text: str) -> tuple[str, str]:
    """Map plain English to (command, rest). Pure, unit-tested.

    Returns ('', '') when nothing matches; the caller then shows a hint.
    'scenarios' means ambiguous fix intent -> show the chooser list.
    """
    low = text.strip().lower()
    if not low:
        return "", ""
    for keywords, cmd in _INTENT_RULES:
        if any(k in low for k in keywords):
            if cmd == "memory":
                # keep the query words after stripping trigger words
                rest = low
                for k in ("memory", "recall", "remember", "remembered", "learned", "my", "the"):
                    rest = rest.replace(k, " ")
                return cmd, " ".join(rest.split())
            return cmd, ""
    return "", ""


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def strip_ansi(text: str) -> str:
    """Plain-text copy for logs/tests; the RichLog gets the styled version."""
    return _ANSI_RE.sub("", text)


class TUISink:
    """Rich Console target that forwards lines into a RichLog from any thread."""

    def __init__(self, app: AfterTraceApp, log: RichLog):
        self._app = app
        self._log = log

    def write(self, text: str) -> int:
        stripped = text.strip("\n")
        if stripped.strip():
            self._app.captured.append(strip_ansi(stripped))
            self._app.call_from_thread(self._log.write, Text.from_ansi(stripped))
        return len(text)

    def flush(self) -> None:
        pass

    @property
    def encoding(self) -> str:
        return "utf-8"


class ConfirmScreen(ModalScreen[bool]):
    """y/n approval modal for repair proposals."""

    BINDINGS = [("y", "approve", "Approve"), ("n", "deny", "Cancel"), ("escape", "deny", "Cancel")]

    def __init__(self, proposal: str):
        super().__init__()
        self._proposal = proposal

    def compose(self) -> ComposeResult:
        yield Static("Proposed repair (requires approval)", id="confirm-title")
        yield Static(self._proposal, id="confirm-body")
        yield Static("[y] approve    [n] cancel", id="confirm-hint")

    def action_approve(self) -> None:
        self.dismiss(True)

    def action_deny(self) -> None:
        self.dismiss(False)


class PaletteScreen(ModalScreen[str | None]):
    """ctrl+p command palette."""

    BINDINGS = [("escape", "close", "Close")]

    def compose(self) -> ComposeResult:
        items = [Option(f"{cmd}  —  {desc}", id=cmd) for cmd, desc in COMMANDS]
        yield OptionList(*items, id="palette")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def action_close(self) -> None:
        self.dismiss(None)


_CONNECT_FIELDS = (
    ("QDRANT_URL", "QDRANT_URL (https://...qdrant.cloud)", False),
    ("QDRANT_API_KEY", "QDRANT_API_KEY", True),
    ("HINDSIGHT_BASE_URL", "HINDSIGHT_BASE_URL", False),
    ("HINDSIGHT_API_KEY", "HINDSIGHT_API_KEY", True),
)


class ConnectScreen(ModalScreen[bool]):
    """BYOK form: Cloud keys apply to this session only, never stored or shown."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        yield Static("Connect Cloud backends — session only, never stored:", id="confirm-title")
        with Vertical(id="connect-box"):
            for var, label, secret in _CONNECT_FIELDS:
                yield Static(label, classes="connect-label")
                yield Input(password=secret, id=f"connect-{var}")
            with Horizontal(id="connect-buttons"):
                yield Button("Save", id="connect-save", variant="primary")
                yield Button("Cancel", id="connect-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "connect-save":
            values = {}
            for var, _label, _secret in _CONNECT_FIELDS:
                try:
                    values[var] = self.query_one(f"#connect-{var}", Input).value.strip()
                except Exception:
                    values[var] = ""
            self.app._pending_connect = values  # type: ignore[attr-defined]
            self.dismiss(True)
        else:
            self.dismiss(False)

    def action_cancel(self) -> None:
        self.dismiss(False)


class AfterTraceApp(App):
    """OpenCode-style TUI shell around the AFTERTRACE flows."""

    CSS = """
    Screen { background: #0d0d0f; }
    #topgap { height: 1fr; }
    #bottomgap { height: 1fr; }
    #transcript { height: auto; max-height: 40%; background: transparent;
                  border: none; margin: 0 2; }
    #center { height: auto; width: 100%; align: center middle; padding: 1 0; }
    #logo { height: 5; text-align: center; }
    #prompt-box { width: 68; max-width: 68; height: auto; border: none;
                  border-left: tall #2f81f7; background: #141416; padding: 1 2; }
    #prompt-input { border: none; height: 1; background: transparent; }
    #prompt-input:focus { border: none; }
    #statusline { height: 1; }
    #hints { width: 68; max-width: 68; height: 1; text-align: right; }
    #tips { height: 1; text-align: center; }
    #bottombar { dock: bottom; height: 1; background: #0d0d0f; color: #4a4a4e; }
    #cwd { width: 1fr; }
    #ver { width: auto; }
    ConfirmScreen, PaletteScreen { align: center middle; }
    #confirm-title { width: 76; text-align: center; color: #e6b800; text-style: bold; }
    #confirm-body { width: 76; max-height: 12; border: solid #3a3a40;
                   background: #141416; padding: 1 2; }
    #confirm-hint { width: 76; text-align: center; color: #555558; }
    #connect-box { width: 76; border: solid #3a3a40; background: #141416; padding: 1 2; }
    #connect-box Input { border: solid #3a3a40; margin-bottom: 1; }
    #connect-box Input:focus { border: solid #2f81f7; }
    .connect-label { color: #555558; }
    #connect-buttons { height: auto; align: center middle; }
    #connect-buttons Button { margin: 0 1; }
    #palette { width: 76; max-height: 14; border: solid #3a3a40; background: #141416; }
    """

    BINDINGS = [
        Binding("ctrl+p", "palette", "Commands", priority=True),
        Binding("ctrl+q", "quit_app", "Quit"),
        Binding("ctrl+r", "rerun", "Re-run"),
        Binding("tab", "complete", "Complete", priority=True),
    ]

    def __init__(self, force_local: bool = False):
        super().__init__()
        self._force_local = force_local
        self.captured: list[str] = []
        self._busy = False
        self._tip_idx = 0
        self._tips_widget: Static | None = None
        self._last_run: tuple[str, bool] | None = None
        self._history: list[tuple[str, str, str, str]] = []
        self._undone: list[dict] = []
        self._pending_connect: dict[str, str] = {}

    def compose(self) -> ComposeResult:
        yield Vertical(id="topgap")
        yield RichLog(id="transcript", highlight=False, markup=False)
        with Vertical(id="center"):
            yield Static(block_logo(), id="logo")
            with Center():
                with Vertical(id="prompt-box"):
                    yield Input(
                        placeholder='Ask anything...  "/scenario1 to detect alias drift"',
                        id="prompt-input",
                    )
                    yield Static("", id="statusline")
            with Center():
                yield Static("", id="hints")
            self._tips_widget = Static("", id="tips")
            yield self._tips_widget
        yield Vertical(id="bottomgap")
        with Horizontal(id="bottombar"):
            yield Static(os.path.basename(os.getcwd()) or os.getcwd(), id="cwd")
            yield Static(VERSION, id="ver")

    def on_mount(self) -> None:
        self._render_statusline()
        self._render_hints()
        self._render_tip()
        self.set_interval(12, self._next_tip)
        self.query_one("#prompt-input", Input).focus()

    # ----- static chrome -----
    def _mode(self) -> tuple[str, str, str, str]:
        from .config import load_settings as _ls

        s = _ls()
        if self._force_local or not s.qdrant_configured:
            return ("Local", "SIM + FALLBACK", s.bank_id, "demo")
        return ("Cloud", "Qdrant + Hindsight", s.bank_id, "live")

    def _render_statusline(self) -> None:
        a, b, bank, c = self._mode()
        t = Text()
        t.append(a, style="bold #2f81f7")
        t.append("  ·  ")
        t.append(b, style="#e8e8ea")
        t.append(f"  bank {bank}", style="#555558")
        t.append("  ·  ")
        t.append(c, style="#b58900")
        self.query_one("#statusline", Static).update(t)

    def _render_hints(self) -> None:
        t = Text()
        t.append("tab", style="bold #e8e8ea")
        t.append(" scenarios   ", style="#555558")
        t.append("ctrl+p", style="bold #e8e8ea")
        t.append(" commands", style="#555558")
        self.query_one("#hints", Static).update(t)

    def _render_tip(self) -> None:
        if self._tips_widget is not None:
            self._tips_widget.update(f"● Tip  {TIPS[self._tip_idx % len(TIPS)]}")

    def _next_tip(self) -> None:
        self._tip_idx += 1
        self._render_tip()

    # ----- transcript -----
    def _write_line(self, text: str) -> None:
        self.captured.append(text)
        self.query_one("#transcript", RichLog).write(Text(text))

    # ----- input -----
    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return
        self._write_line(f"> {text}")
        if text.startswith("!"):
            self._run_bash(text[1:])
            return
        from .file_ref import extract_refs

        refs = extract_refs(text)
        body = re.sub(r"@[A-Za-z0-9_.\-/\\]+", "", text).strip()
        if refs:
            self._inject_refs(refs)
        if body.startswith("/"):
            cmd, auto_yes = parse_command(body)
            rest = [p for p in body.split()[1:] if p != "--yes"]
            self._dispatch(cmd, auto_yes, rest)
        elif body:
            cmd, rest = route_text(body)
            if not cmd:
                self._write_line("Not sure what you mean — try /help, /scenarios, or /demo.")
            elif cmd == "scenarios":
                self._dispatch(cmd, False, [])
            else:
                self._write_line(f"Understood as /{cmd} — running.")
                self._dispatch(cmd, False, [rest] if rest else [])
        # else: file refs only — already acknowledged above.

    def _inject_refs(self, frags: list[str]) -> None:
        from .file_ref import read_ref, resolve_ref

        for frag in frags[:4]:
            hits = resolve_ref(frag)
            if not hits:
                self._write_line(f"@{frag}: no match under project root.")
            elif len(hits) == 1:
                text, note = read_ref(hits[0])
                if not text:
                    self._write_line(f"@{hits[0]}: {note}.")
                    continue
                self._write_line(f"@{hits[0]}{note}:")
                lines = text.splitlines()
                for ln in lines[:60]:
                    self._write_line("  " + ln[:200])
                if len(lines) > 60:
                    self._write_line("  ... (see the file for the rest)")
            else:
                self._write_line(f"@{frag}: {len(hits)} matches — be more specific:")
                for h in hits[:8]:
                    self._write_line(f"  @{h}")

    def _run_bash(self, cmd: str) -> None:
        if self._busy:
            self._write_line("A run is already in progress — wait for it to finish.")
            return
        if not cmd.strip():
            self._write_line("Empty command. Try !echo hello.")
            return
        self._busy = True

        def _run() -> None:
            try:
                from .bash_tool import run_bash

                r = run_bash(cmd)
                lines = [f"!{cmd.strip()} (exit {r['exit']})"]
                out = (r["output"] or "").splitlines()[:40]
                lines += ["  " + ln[:200] for ln in out] if out else ["  (no output)"]
                if r.get("note"):
                    lines.append("  " + str(r["note"]).strip())
                for ln in lines:
                    self.call_from_thread(self._write_line, ln)
            except Exception as e:
                self.call_from_thread(self._write_line, f"[! failed: {e}]")
            finally:
                self._busy = False

        self.run_worker(_run, thread=True, exclusive=True, description="bash")

    def action_complete(self) -> None:
        try:
            inp = self.query_one("#prompt-input", Input)
        except Exception:
            return
        cur = inp.value
        if not cur.startswith("/"):
            inp.value = "/"
            return
        frag = cur[1:].split()[0] if cur[1:] else ""
        matches = [c for c, _ in COMMANDS if c[1:].startswith(frag)]
        if len(matches) == 1:
            inp.value = matches[0] + " "
        elif matches:
            self._write_line("  " + "   ".join(matches))

    def action_palette(self) -> None:
        async def _open() -> None:
            choice = await self.push_screen_wait(PaletteScreen())
            if choice:
                self._write_line(f"> {choice}")
                cmd, auto_yes = parse_command(choice)
                self._dispatch(cmd, auto_yes)

        self.run_worker(_open, exclusive=False, description="palette")

    def action_quit_app(self) -> None:
        self.exit()

    def action_rerun(self) -> None:
        if self._busy:
            self._write_line("A run is already in progress — wait for it to finish.")
            return
        if not self._last_run:
            self._write_line("Nothing to re-run yet — try /scenario1 or /demo first.")
            return
        cmd, auto_yes = self._last_run
        self._write_line(f"> /rerun (repeating /{cmd})")
        self._dispatch(cmd, auto_yes, [])

    # ----- dispatch -----
    def _show_history(self, arg: str) -> None:
        """Incident history browser. Bare /history lists; /history N shows detail."""
        from . import sqlite_log
        from .config import load_settings as _ls

        settings = _ls()
        try:
            con = sqlite_log.connect(settings.sqlite_path)
        except Exception as e:
            self._write_line(f"No incident log yet ({e}). Run /demo first.")
            return
        try:
            rows = con.execute(
                "SELECT id, scenario, state, created_at FROM incidents ORDER BY created_at"
            ).fetchall()
        except Exception:
            self._write_line("No incident log yet. Run /demo first.")
            con.close()
            return
        items = [(r[0], r[1], r[2], r[3]) for r in rows]
        if arg.strip().isdigit():
            idx = int(arg.strip()) - 1
            if 0 <= idx < len(items):
                iid, scen, state, _ = items[idx]
                self._write_line(f"[{idx + 1}] {scen} — {state} ({iid[:8]})")
                try:
                    evs = con.execute(
                        "SELECT kind, created_at FROM events WHERE incident_id=? ORDER BY seq",
                        (iid,),
                    ).fetchall()
                except Exception:
                    evs = []
                for kind, _at in evs:
                    self._write_line(f"    · {kind}")
                if not evs:
                    self._write_line("    (no events recorded)")
            else:
                self._write_line(
                    f"No incident #{arg.strip()} — {len(items)} recorded. Try /history."
                )
            con.close()
            return
        if not items:
            self._write_line("No incidents recorded yet. Run /demo or /scenario1 first.")
            con.close()
            return
        self._history = items
        self._write_line(f"Incident history ({len(items)}):")
        for i, (iid, scen, state, at) in enumerate(items, 1):
            self._write_line(f"  {i}. {scen} — {state}   ({iid[:8]}, {str(at)[:16]})")
        self._write_line("Type /history N for the event trail of entry N.")
        con.close()

    def _compact(self) -> None:
        """Collapse the transcript to key outcome lines (deterministic, local only)."""
        lines = list(self.captured)
        keys = (
            "RESOLVED",
            "REJECTED",
            "MISMATCH",
            "PASS",
            "FAIL",
            "retained",
            "exit code",
            "switched",
            "invalidated",
            "recall:",
        )
        keep = [ln for ln in lines if any(k in ln for k in keys)][-30:]
        self.query_one("#transcript", RichLog).clear()
        self.captured.clear()
        self._write_line(f"Compacted {len(lines)} lines -> {len(keep)} key outcomes:")
        for ln in keep:
            self._write_line("  " + ln[:160])

    def _show_models(self) -> None:
        """Backend readiness from real settings. No network calls, no guessing."""
        import sys

        from .config import load_settings as _ls

        s = _ls()
        if s.qdrant_configured:
            host = (s.qdrant_url or "").split("//")[-1].split("/")[0][:40]
            self._write_line(f"vectors: Qdrant Cloud ready ({host})")
        else:
            from .config import VECTOR_DIM

            self._write_line(
                f"vectors: LOCAL-SIM fixture mode (dim {VECTOR_DIM}, hashes not embeddings)"
            )
        if s.hindsight_configured:
            self._write_line(f"memory: Hindsight Cloud ready (bank {s.bank_id})")
        else:
            self._write_line("memory: LOCAL-FALLBACK file mode")
        self._write_line(f"runtime: python {sys.version.split()[0]} (Textual TUI)")

    def _open_connect(self) -> None:
        async def _open() -> None:
            try:
                ok = await self.push_screen_wait(ConnectScreen())
            except Exception:
                return
            if not ok:
                self._write_line("Connect cancelled — nothing changed.")
                return
            vals = dict(getattr(self, "_pending_connect", {}))
            self._pending_connect = {}
            applied = [k for k, v in vals.items() if v]
            for k in applied:
                os.environ[k] = vals[k]
            for k in [k for k in vals if k not in applied]:
                os.environ.pop(k, None)
            self.call_from_thread(self._render_statusline)
            if applied:
                self.call_from_thread(
                    self._write_line,
                    "Connected for this session: " + ", ".join(applied) + " (never stored).",
                )
            else:
                self.call_from_thread(self._write_line, "All keys cleared — back to local modes.")

        self.run_worker(_open, exclusive=False, description="connect")

    def _export_flow(self, arg: str) -> None:
        from .config import load_settings as _ls
        from .sessions_ops import export_incident, list_incidents

        try:
            settings = _ls()
            if not arg.strip():
                items = list_incidents(settings)
                if not items:
                    self.call_from_thread(self._write_line, "Nothing to export — run /demo first.")
                    return
                arg = items[-1]["id"]
            res = export_incident(settings, arg.strip())
            if "error" in res:
                self.call_from_thread(self._write_line, f"Export failed: {res['error']}")
            else:
                self.call_from_thread(
                    self._write_line,
                    f"Exported {res['events']} events -> {res['path']} (secrets redacted).",
                )
        except Exception as e:
            self.call_from_thread(self._write_line, f"[export failed: {e}]")
        finally:
            self._busy = False

    def _undo_redo_flow(self, cmd: str) -> None:
        from . import sqlite_log
        from .config import load_settings as _ls
        from .qdrant_store import QdrantStore
        from .sessions_ops import alias_for_collection, find_last_mutation

        try:
            settings = _ls()
            if cmd == "redo":
                if not self._undone:
                    self.call_from_thread(self._write_line, "Nothing to redo — /undo first.")
                    return
                op = self._undone.pop()
                alias, frm, to = op["alias"], op["from"], op["to"]
                verb, evt = "Re-apply", "alias.reapplied"
            else:
                mut = find_last_mutation(settings)
                if not mut:
                    self.call_from_thread(
                        self._write_line, "Nothing to undo — no alias switch recorded."
                    )
                    return
                if mut["kind"] != "alias.switched":
                    self.call_from_thread(
                        self._write_line,
                        f"Cannot undo {mut['kind']} — only alias switches are reversible "
                        "(cache data is deleted, not moved).",
                    )
                    return
                data = mut["data"]
                before, after = data.get("before"), data.get("after")
                if not before or not after:
                    self.call_from_thread(
                        self._write_line, "Cannot undo — journal entry lacks before/after."
                    )
                    return
                alias = alias_for_collection(settings, after)
                if not alias:
                    self.call_from_thread(
                        self._write_line,
                        "Cannot undo — live alias already moved on from that collection.",
                    )
                    return
                frm, to = after, before
                verb, evt = "Revert", "alias.reverted"
                op = {
                    "alias": alias,
                    "from": frm,
                    "to": to,
                    "incident_id": mut.get("incident_id", ""),
                }
            proposal = (
                f"{verb} alias {alias}: {frm} -> {to}\n"
                f"Live state will be re-verified before the write."
            )
            if not self._make_approver()(proposal):
                self.call_from_thread(self._write_line, f"{verb} cancelled — no writes made.")
                if cmd == "redo":
                    self._undone.append(op)
                return
            store = QdrantStore(settings, force_local=self._force_local)
            try:
                res = store.switch_alias(alias, frm, to)
            except Exception as e:
                if getattr(store, "local", False):
                    self.call_from_thread(
                        self._write_line,
                        "Cannot revert here — LOCAL-SIM holds no persistent vector state "
                        "across commands. Set Cloud keys for reversible operations.",
                    )
                else:
                    self.call_from_thread(
                        self._write_line,
                        f"Revert failed ({e}). Route left gated — reconcile before retrying.",
                    )
                if cmd == "redo":
                    self._undone.append(op)
                return
            con = sqlite_log.connect(settings.sqlite_path)
            try:
                sqlite_log.set_alias(con, alias, to)
                sqlite_log.log_event(
                    con, op["incident_id"], evt, {"alias": alias, "before": frm, "after": to}
                )
            finally:
                try:
                    con.close()
                except Exception:
                    pass
            if cmd == "undo":
                self._undone.append(
                    {
                        "alias": alias,
                        "from": to,
                        "to": frm,
                        "incident_id": op.get("incident_id", ""),
                    }
                )
            self.call_from_thread(
                self._write_line, f"{verb}ed: {res['before']} -> {res['after']} (verified)."
            )
        except Exception as e:
            self.call_from_thread(self._write_line, f"[{cmd} failed: {e}]")
        finally:
            self._busy = False

    def _dispatch(self, cmd: str, auto_yes: bool, rest: list[str] | None = None) -> None:
        rest = [r for r in (rest or []) if r != "--yes"]
        if cmd in ("quit", "exit"):
            self.exit()
            return
        if cmd == "clear":
            self.query_one("#transcript", RichLog).clear()
            return
        if cmd == "help":
            self._write_line(
                "Commands: "
                + ", ".join(c for c, _ in COMMANDS)
                + "   (append --yes to skip approval; just describe what you want in plain words)"
            )
            return
        if cmd == "scenarios":
            self._write_line(
                "/scenario1 — cold alias-drift fix   /scenario2 — memory-assisted transfer"
            )
            self._write_line(
                "/scenario3 — reject wrong recalled fix (stale cache)   /demo — all three in order"
            )
            self._write_line("/history · /memory · /rerun · /check · /doctor · /reset")
            return
        if cmd == "history":
            self._show_history(rest[0] if rest else "")
            return
        if cmd == "memory":
            arg = " ".join(rest)
            self._busy = True
            self.run_worker(
                lambda: self._run_flow("memory", False, arg),
                thread=True,
                exclusive=True,
                description="aftertrace-memory",
            )
            return
        if cmd == "rerun":
            self.action_rerun()
            return
        if cmd in (
            "new",
            "sessions",
            "export",
            "undo",
            "redo",
            "compact",
            "summarize",
            "models",
            "connect",
        ):
            if cmd == "new":
                self.query_one("#transcript", RichLog).clear()
                self.captured.clear()
                self._last_run = None
                self._undone.clear()
                self._write_line("Fresh context. Transcript cleared; on-disk log kept. Try /demo.")
                return
            if cmd == "sessions":
                self._show_history("")
                return
            if cmd in ("compact", "summarize"):
                self._compact()
                return
            if cmd == "models":
                self._show_models()
                return
            if cmd == "connect":
                self._open_connect()
                return
            if self._busy:
                self._write_line("A run is already in progress — wait for it to finish.")
                return
            self._busy = True
            if cmd == "export":
                arg = " ".join(rest)
                self.run_worker(
                    lambda: self._export_flow(arg),
                    thread=True,
                    exclusive=True,
                    description="export",
                )
            elif cmd in ("undo", "redo"):
                self.run_worker(
                    lambda: self._undo_redo_flow(cmd), thread=True, exclusive=True, description=cmd
                )
            return
        if self._busy:
            self._write_line("A run is already in progress — wait for it to finish.")
            return
        if cmd in ("scenario1", "scenario2", "scenario3", "check", "reset", "doctor", "demo"):
            if cmd in ("scenario1", "scenario2", "scenario3", "demo"):
                self._last_run = (cmd, auto_yes)
            self._busy = True
            self.run_worker(
                lambda: self._run_flow(cmd, auto_yes),
                thread=True,
                exclusive=True,
                description=f"aftertrace-{cmd}",
            )
        else:
            hint = suggest_command(cmd)
            if hint:
                self._write_line(f"Unknown command /{cmd}. Did you mean /{hint}?")
            else:
                self._write_line(f"Unknown command /{cmd}. Try /help.")

    def _run_flow(self, cmd: str, auto_yes: bool, arg: str = "") -> None:
        from .config import load_settings as _ls
        from .scenarios import run_scenario1, run_scenario2, run_scenario3

        settings = _ls()
        log = self.query_one("#transcript", RichLog)
        try:
            width = max(40, self.screen.size.width - 8)
        except Exception:
            width = 96
        tconsole = Console(
            file=TUISink(self, log),
            force_terminal=True,
            color_system="truecolor",
            width=width,
            legacy_windows=False,
        )
        try:
            if cmd == "check":
                tconsole.print(
                    "[bold]AFTERTRACE config check[/bold] (presence only, values never printed)"
                )
                for name, present in [
                    ("QDRANT_URL", bool(settings.qdrant_url)),
                    ("QDRANT_API_KEY", bool(settings.qdrant_api_key)),
                    ("HINDSIGHT_BASE_URL", bool(settings.hindsight_base_url)),
                    ("HINDSIGHT_API_KEY", bool(settings.hindsight_api_key)),
                ]:
                    tconsole.print(f"  {name}: {'SET' if present else 'MISSING'}")
                tconsole.print(f"  bank: {settings.bank_id}")
                return
            if cmd == "doctor":
                from .__main__ import cmd_doctor as _doctor

                _doctor(settings, out=tconsole)
                return
            if cmd == "demo":
                from .__main__ import cmd_demo as _demo

                code = _demo(
                    settings,
                    auto_yes=auto_yes,
                    force_local=self._force_local,
                    out=tconsole,
                    approver=self._make_approver(),
                )
                self.call_from_thread(self._write_line, f"[demo exit code {code}]")
                return
            if cmd == "reset":
                import os as _os

                for path in [
                    settings.sqlite_path,
                    settings.sqlite_path + "-wal",
                    settings.sqlite_path + "-shm",
                ]:
                    try:
                        if _os.path.exists(path):
                            _os.remove(path)
                    except Exception:
                        pass
                here = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
                fb = _os.path.join(here, ".data", "memory_fallback.jsonl")
                try:
                    if _os.path.exists(fb):
                        _os.remove(fb)
                except Exception:
                    pass
                self.call_from_thread(
                    self._write_line, "reset done. Run /scenario1 -> /scenario2 -> /scenario3."
                )
                return
            if cmd == "memory":
                from .memory_store import MemoryStore

                query = arg.strip() or "incident alias drift cache revision repair"
                _mode = MemoryStore(settings, force_local=self._force_local).mode_label
                tconsole.print(f"[bold]Memory browser[/bold] ({_mode})")
                try:
                    store = MemoryStore(settings, console=tconsole, force_local=self._force_local)
                    hits = store.recall(query, max_tokens=1500)
                    store.close()
                except Exception as e:
                    tconsole.print(f"[red]recall failed: {e}[/red]")
                    return
                if not hits:
                    tconsole.print(
                        "[dim]No retained experience matches. Run /demo first to create some.[/dim]"
                    )
                    return
                for i, h in enumerate(hits[:5], 1):
                    snippet = (h.text or "").replace("\n", " ")[:280]
                    tconsole.print(f"  [cyan]{i}.[/cyan] doc={h.document_id or '?'}")
                    tconsole.print(f"     {snippet}...")
                return
            runners = {
                "scenario1": run_scenario1,
                "scenario2": run_scenario2,
                "scenario3": run_scenario3,
            }
            code = runners[cmd](
                settings,
                tconsole,
                auto_yes=auto_yes,
                force_local=self._force_local,
                approver=self._make_approver(),
            )
            self.call_from_thread(self._write_line, f"[{cmd} exit code {code}]")
        except Exception as e:
            self.call_from_thread(self._write_line, f"[{cmd} failed: {e}]")
        finally:
            self._busy = False

    # ----- approval bridge (worker thread -> modal -> bool) -----
    def _make_approver(self):
        def approve(proposal: str) -> bool:
            fut: concurrent.futures.Future[bool] = concurrent.futures.Future()
            self.call_from_thread(self._ask_approval, proposal, fut)
            try:
                return bool(fut.result(timeout=600))
            except Exception:
                return False

        return approve

    def _ask_approval(self, proposal: str, fut: concurrent.futures.Future) -> None:
        async def _run() -> None:
            try:
                result = await self.push_screen_wait(ConfirmScreen(proposal))
            except Exception:
                result = False
            if not fut.done():
                fut.set_result(bool(result))

        self.run_worker(_run, exclusive=False, description="approval-modal")


def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(
        prog="aftertrace-tui", description="Interactive AFTERTRACE terminal UI."
    )
    p.add_argument("--local", action="store_true", help="Force local simulation modes.")
    args = p.parse_args(argv)
    AfterTraceApp(force_local=args.local).run()
    return 0


def layout_report() -> int:
    """Headless layout report at the REAL terminal size + code version.

    Paste the output when the TUI looks wrong — it shows exactly where
    every widget lands and proves which commit is running.
    """
    import shutil
    import subprocess

    async def _run() -> None:
        cols, rows = shutil.get_terminal_size()
        print(f"terminal={cols}x{rows}")
        try:
            import textual

            print(f"textual={textual.__version__}")
        except Exception as e:
            print(f"textual=unknown ({e})")
        app = AfterTraceApp(force_local=True)
        async with app.run_test(size=(cols, rows)) as pilot:
            await pilot.pause(0.5)
            for sel in (
                "#transcript",
                "#center",
                "#logo",
                "#prompt-box",
                "#prompt-input",
                "#statusline",
                "#hints",
                "#tips",
                "#bottombar",
            ):
                try:
                    print(sel, app.query_one(sel).region)
                except Exception as e:
                    print(sel, "ERR", type(e).__name__)

    try:
        rev = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=15
        ).stdout.strip()
        print("commit=" + (rev or "unknown"))
    except Exception:
        print("commit=unknown")
    asyncio.run(_run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
