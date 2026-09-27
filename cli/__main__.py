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
    console.print("[bold]AFTERTRACE MVP config check[/bold] (presence only, values never printed)")
    for name, present in [
        ("QDRANT_URL", bool(settings.qdrant_url)),
        ("QDRANT_API_KEY", bool(settings.qdrant_api_key)),
        ("HINDSIGHT_BASE_URL", bool(settings.hindsight_base_url)),
        ("HINDSIGHT_API_KEY", bool(settings.hindsight_api_key)),
    ]:
        console.print(f"  {name}: {'[green]SET[/green]' if present else '[yellow]MISSING[/yellow]'}")
    console.print(f"  bank: {settings.bank_id}")
    console.print(f"  sqlite: {settings.sqlite_path}")
    if not settings.qdrant_configured:
        console.print("[yellow]Qdrant Cloud not configured -> LOCAL-SIM vector mode.[/yellow]")
    if not settings.hindsight_configured:
        console.print("[yellow]Hindsight Cloud not configured -> LOCAL-FALLBACK memory mode.[/yellow]")
    if args.local:
        console.print("[dim]--local forced.[/dim]")
    return 0


def cmd_reset(settings) -> int:
    import os

    for path in [settings.sqlite_path, settings.sqlite_path + "-wal", settings.sqlite_path + "-shm"]:
        try:
            if os.path.exists(path):
                os.remove(path)
                console.print(f"removed {path}")
        except Exception as e:
            console.print(f"[yellow]could not remove {path}: {e}[/yellow]")
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    fb = os.path.join(here, ".data", "memory_fallback.jsonl")
    try:
        if os.path.exists(fb):
            os.remove(fb)
            console.print(f"removed {fb}")
    except Exception as e:
        console.print(f"[yellow]could not remove {fb}: {e}[/yellow]")
    console.print("[green]reset done. Run scenario1 -> scenario2 -> scenario3.[/green]")
    return 0


def main(argv: list[str] | None = None) -> int:
    settings = load_settings()
    p = argparse.ArgumentParser(prog="aftertrace-mvp", description="Memory-guided incident-recovery CLI (hackathon MVP).")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, help_text in [
        ("scenario1", "Cold incident: alias drift, no prior memory."),
        ("scenario2", "Memory-assisted transfer on NEW corpus (fresh process)."),
        ("scenario3", "Reject wrong recalled alias fix; stale-cache cause."),
        ("check", "Show config presence (no secrets)."),
        ("reset", "Clear local SQLite log + fallback memory (fresh demo)."),
    ]:
        s = sub.add_parser(name, help=help_text)
        s.add_argument("--yes", action="store_true", help="Auto-approve repair (non-interactive).")
        s.add_argument("--local", action="store_true", help="Force local simulation modes.")
    args = p.parse_args(argv)
    force_local = bool(getattr(args, "local", False))
    auto_yes = bool(getattr(args, "yes", False))
    if args.cmd == "check":
        return cmd_check(settings, args)
    if args.cmd == "reset":
        return cmd_reset(settings)
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
