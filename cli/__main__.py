"""CLI entry: python -m cli.scenario1|s2|s3  OR  python -m cli --help.

Commands (each a fresh process):
  python -m cli scenario1 --yes [--local]
  python -m cli scenario2 --yes [--local]
  python -m cli scenario3 --yes [--local]
  python -m cli check   (show env/config status, no secrets)

--local forces LOCAL-SIM + LOCAL-FALLBACK memory even if Cloud creds exist.
Without Cloud creds the tool automatically uses local modes and labels output.
"""

from __future__ import annotations

import argparse
import sys

from rich.console import Console

from .config import load_settings
from .scenarios import run_scenario1, run_scenario2, run_scenario3

console = Console()


def cmd_check(settings, args) -> int:
    console.print("[bold]AFTERTRACE config check[/bold] (presence only, values never printed)")
    for name, present in [
        ("QDRANT_URL", bool(settings.qdrant_url)),
        ("QDRANT_API_KEY", bool(settings.qdrant_api_key)),
        ("HINDSIGHT_BASE_URL", bool(settings.hindsight_base_url)),
        ("HINDSIGHT_API_KEY", bool(settings.hindsight_api_key)),
    ]:
        console.print(
            f"  {name}: {'[green]SET[/green]' if present else '[yellow]MISSING[/yellow]'}"
        )
    console.print(f"  bank: {settings.bank_id}")
    console.print(f"  sqlite: {settings.sqlite_path}")
    if not settings.qdrant_configured:
        console.print("[yellow]Qdrant Cloud not configured -> LOCAL-SIM vector mode.[/yellow]")
    if not settings.hindsight_configured:
        console.print(
            "[yellow]Hindsight Cloud not configured -> LOCAL-FALLBACK memory mode.[/yellow]"
        )
    if args.local:
        console.print("[dim]--local forced.[/dim]")
    return 0


def cmd_reset(settings, out=None) -> int:
    import os

    out = out or console
    for path in [
        settings.sqlite_path,
        settings.sqlite_path + "-wal",
        settings.sqlite_path + "-shm",
    ]:
        try:
            if os.path.exists(path):
                os.remove(path)
                out.print(f"removed {path}")
        except Exception as e:
            out.print(f"[yellow]could not remove {path}: {e}[/yellow]")
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    fb = os.environ.get(
        "AFTERTRACE_MEMORY_FALLBACK", os.path.join(here, ".data", "memory_fallback.jsonl")
    )
    try:
        if os.path.exists(fb):
            os.remove(fb)
            out.print(f"removed {fb}")
    except Exception as e:
        out.print(f"[yellow]could not remove {fb}: {e}[/yellow]")
    out.print("[green]reset done. Run scenario1 -> scenario2 -> scenario3.[/green]")
    return 0


def cmd_doctor(settings, out=None) -> int:
    """Unified environment check: config presence + dependency probes + DB writable."""
    out = out or console
    out.print("[bold]AFTERTRACE doctor[/bold] (presence only, values never printed)")
    ok = True
    for mod in ("httpx", "pydantic", "rich", "qdrant_client", "hindsight_client", "textual"):
        try:
            __import__(mod)
            out.print(f"  dep {mod}: [green]ok[/green]")
        except Exception:
            out.print(f"  dep {mod}: [red]MISSING (pip install {mod})[/red]")
            ok = False
    import os

    dbdir = os.path.dirname(os.path.abspath(settings.sqlite_path))
    try:
        os.makedirs(dbdir, exist_ok=True)
        probe = os.path.join(dbdir, ".write_probe")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
        out.print(f"  db dir writable: [green]ok[/green] ({dbdir})")
    except Exception as e:
        out.print(f"  db dir writable: [red]FAIL: {e}[/red]")
        ok = False
    for name, present in [
        ("QDRANT_URL", bool(settings.qdrant_url)),
        ("QDRANT_API_KEY", bool(settings.qdrant_api_key)),
        ("HINDSIGHT_BASE_URL", bool(settings.hindsight_base_url)),
        ("HINDSIGHT_API_KEY", bool(settings.hindsight_api_key)),
    ]:
        status = "[green]SET[/green]" if present else "[yellow]MISSING[/yellow]"
        suffix = "" if present else " (local modes will be used)"
        out.print(f"  {name}: {status}{suffix}")
    if not settings.qdrant_configured or not settings.hindsight_configured:
        out.print(
            "[dim]Tip: without Cloud keys the demo runs labeled LOCAL-SIM / LOCAL-FALLBACK.[/dim]"
        )
    out.print("[dim]Next: python -m cli demo --local --yes[/dim]")
    return 0 if ok else 1


def cmd_demo(settings, auto_yes: bool, force_local: bool, out=None, approver=None) -> int:
    """One-command ordered demo: reset -> s1 -> s2 -> s3. Aborts on first failure."""
    out = out or console
    out.print("[bold]AFTERTRACE demo: reset -> scenario1 -> scenario2 -> scenario3[/bold]")
    cmd_reset(settings, out=out)
    steps = [
        ("scenario1", run_scenario1),
        ("scenario2", run_scenario2),
        ("scenario3", run_scenario3),
    ]
    for name, fn in steps:
        out.print(f"[bold cyan]===== {name} =====[/bold cyan]")
        code = fn(settings, out, auto_yes=auto_yes, force_local=force_local, approver=approver)
        if code != 0:
            out.print(
                f"[red]{name} exited {code}; demo aborted. Fix above, then re-run demo.[/red]"
            )
            return code
    out.print(
        "[bold green]DEMO COMPLETE: s1 fixed cold, s2 transfer via memory,"
        " s3 rejected wrong fix.[/bold green]"
    )
    out.print("[dim]Next: python -m cli tui --local (interactive UI) or re-run demo.[/dim]")
    return 0


def main(argv: list[str] | None = None) -> int:
    settings = load_settings()
    p = argparse.ArgumentParser(
        prog="aftertrace", description="Memory-guided incident recovery CLI."
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, help_text in [
        ("scenario1", "Cold incident: alias drift, no prior memory."),
        ("scenario2", "Memory-assisted transfer on NEW corpus (fresh process)."),
        ("scenario3", "Reject wrong recalled alias fix; stale-cache cause."),
        ("check", "Show config presence (no secrets)."),
        ("doctor", "Unified env + dependency + DB check (no secrets)."),
        ("demo", "One-command ordered demo: reset -> s1 -> s2 -> s3."),
        ("sessions", "List recorded incidents."),
        ("export", "Save an incident as redacted JSON."),
        ("reset", "Clear local SQLite log + fallback memory (fresh demo)."),
        ("tui", "Interactive full-screen terminal UI (OpenCode style)."),
        ("tui-layout", "Headless TUI geometry report (paste output when UI looks wrong)."),
    ]:
        s = sub.add_parser(name, help=help_text)
        s.add_argument("--yes", action="store_true", help="Auto-approve repair (non-interactive).")
        s.add_argument("--local", action="store_true", help="Force local simulation modes.")
    sub.choices["export"].add_argument(
        "export_target", nargs="?", default="", help="Incident id (prefix ok); defaults to latest."
    )
    args = p.parse_args(argv)
    force_local = bool(getattr(args, "local", False))
    auto_yes = bool(getattr(args, "yes", False))
    if args.cmd == "check":
        return cmd_check(settings, args)
    if args.cmd == "doctor":
        return cmd_doctor(settings)
    if args.cmd == "demo":
        return cmd_demo(settings, auto_yes=auto_yes, force_local=force_local)
    if args.cmd == "sessions":
        from .sessions_ops import list_incidents

        items = list_incidents(settings)
        if not items:
            console.print("No incidents recorded yet. Run demo first.")
            return 0
        for i, it in enumerate(items, 1):
            console.print(f"  {i}. {it['scenario']} — {it['state']} ({it['id'][:8]})")
        return 0
    if args.cmd == "export":
        from .sessions_ops import export_incident, list_incidents

        target = getattr(args, "export_target", "") or ""
        if not target:
            items = list_incidents(settings)
            if not items:
                console.print("Nothing to export — run demo first.")
                return 2
            target = items[-1]["id"]
        res = export_incident(settings, target)
        if "error" in res:
            console.print(f"[red]{res['error']}[/red]")
            return 2
        console.print(f"Exported {res['events']} events -> {res['path']} (secrets redacted).")
        return 0
    if args.cmd == "reset":
        return cmd_reset(settings)
    if args.cmd == "tui":
        from .tui import main as tui_main

        return tui_main(["--local"] if force_local else [])
    if args.cmd == "tui-layout":
        from .tui import layout_report

        return layout_report()
    if args.cmd == "scenario1":
        return run_scenario1(settings, console, auto_yes=auto_yes, force_local=force_local)
    if args.cmd == "scenario2":
        return run_scenario2(settings, console, auto_yes=auto_yes, force_local=force_local)
    if args.cmd == "scenario3":
        return run_scenario3(settings, console, auto_yes=auto_yes, force_local=force_local)
    p.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
