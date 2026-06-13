"""
main.py — CLI entry point for lol-graph.

Commands
--------
collect     Collect ranked games via BFS
stats       Show collection statistics
path        Shortest path between two players
hubs        Most-connected players in the graph
neighbors   All players within N hops of a player
export      Export graph to GraphML for Gephi
reset       Wipe the database
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table, box

import db
import graph as gph
import analyzer
from collector import run_collection

console = Console()

HELP_TEXT = """
[bold cyan]lol-graph[/bold cyan] — League of Legends player connection graph

[bold]Commands[/bold]
  collect                  Collect ranked games (BFS from ladder or seed player)
  stats                    Show database + graph statistics
  path  A B                Shortest path between two players
  hubs  [--top N]          Most-connected players
  neighbors NAME [--depth] Players within N hops
  export [--out FILE]      Export graph to GraphML
  reset                    Clear all data
"""


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

def cmd_collect(args: argparse.Namespace) -> None:
    db.init_db()
    asyncio.run(
        run_collection(
            seed=args.seed,
            max_players=args.max_players,
            verbose=args.verbose,
        )
    )


def cmd_stats(args: argparse.Namespace) -> None:
    stats = db.get_collection_stats()

    t = Table.grid(padding=(0, 2))
    t.add_column(style="dim")
    t.add_column()
    t.add_row("Total games",    str(stats["total_games"]))
    t.add_row("Total players",  str(stats["total_players"]))
    t.add_row("Scanned",        str(stats["scanned"]))
    t.add_row("Queued",         str(stats["queued"]))
    console.print()
    console.print(Panel(t, title="[bold cyan]Collection Stats[/bold cyan]", border_style="cyan", expand=False))

    if stats["games_by_patch"]:
        pt = Table(title="Games by Patch", box=box.SIMPLE_HEAVY)
        pt.add_column("Patch",  style="bold")
        pt.add_column("Games",  justify="right")
        for patch, cnt in stats["games_by_patch"].items():
            pt.add_row(patch, str(cnt))
        console.print(pt)


def cmd_path(args: argparse.Namespace) -> None:
    use_sql = args.sql
    G = None
    if not use_sql:
        console.print("[cyan]Building graph from database…[/cyan]")
        G = gph.build_graph()

    analyzer.print_path(args.player_a, args.player_b, G=G, use_sql=use_sql)


def cmd_hubs(args: argparse.Namespace) -> None:
    console.print("[cyan]Building graph from database…[/cyan]")
    G = gph.build_graph()
    analyzer.print_hubs(G, n=args.top)


def cmd_neighbors(args: argparse.Namespace) -> None:
    console.print("[cyan]Building graph from database…[/cyan]")
    G = gph.build_graph()
    analyzer.print_neighbors(args.player, depth=args.depth, G=G)


def cmd_export(args: argparse.Namespace) -> None:
    out = Path(args.out)
    console.print("[cyan]Building graph from database…[/cyan]")
    G = gph.build_graph()
    console.print(f"[cyan]Exporting to {out}…[/cyan]")
    gph.export_graphml(G, out)
    console.print(f"[green]Saved: {out}[/green]")
    analyzer.print_graph_stats(G)


def cmd_reset(args: argparse.Namespace) -> None:
    confirm = input("This will delete all data. Type 'yes' to confirm: ")
    if confirm.strip().lower() != "yes":
        console.print("[yellow]Aborted.[/yellow]")
        return
    db_path = db.DB_PATH
    if db_path.exists():
        db_path.unlink()
        console.print(f"[green]Deleted {db_path}[/green]")
    db.init_db()
    console.print("[green]Database reset.[/green]")


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="lol-graph",
        description="League of Legends player co-game graph",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="command")

    # collect
    cp = sub.add_parser("collect", help="Collect ranked games")
    cp.add_argument("--seed",        metavar="NAME#TAG", help="Seed from a specific player")
    cp.add_argument("--max-players", metavar="N", type=int, help="Stop after N players scanned")
    cp.add_argument("--verbose",     action="store_true", help="Print every match fetched")

    # stats
    sub.add_parser("stats", help="Show collection stats")

    # path
    pp = sub.add_parser("path", help="Shortest path between two players")
    pp.add_argument("player_a", metavar="A", help="First player (GameName#TAG)")
    pp.add_argument("player_b", metavar="B", help="Second player (GameName#TAG)")
    pp.add_argument("--sql", action="store_true",
                    help="Use SQL BFS (no full graph load; slower per query)")

    # hubs
    hp = sub.add_parser("hubs", help="Most-connected players")
    hp.add_argument("--top", metavar="N", type=int, default=20, help="Number of hubs to show")

    # neighbors
    np_ = sub.add_parser("neighbors", help="Players within N hops of a player")
    np_.add_argument("player", metavar="NAME#TAG", help="Player to query")
    np_.add_argument("--depth", metavar="N", type=int, default=1, help="Hop depth (default 1)")

    # export
    ep = sub.add_parser("export", help="Export graph to GraphML")
    ep.add_argument("--out", metavar="FILE", default="lol_graph.graphml",
                    help="Output file (default: lol_graph.graphml)")

    # reset
    sub.add_parser("reset", help="Wipe the database")

    return p


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

DISPATCH = {
    "collect":   cmd_collect,
    "stats":     cmd_stats,
    "path":      cmd_path,
    "hubs":      cmd_hubs,
    "neighbors": cmd_neighbors,
    "export":    cmd_export,
    "reset":     cmd_reset,
}


def main() -> None:
    db.init_db()

    parser = build_parser()
    args   = parser.parse_args()

    if args.command is None:
        console.print(Panel(HELP_TEXT, border_style="cyan", expand=False))
        return

    handler = DISPATCH.get(args.command)
    if handler:
        handler(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
