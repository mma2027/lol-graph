"""
analyzer.py — Rich terminal output for lol-graph queries.

Entry points
------------
print_path(name_tag_a, name_tag_b, use_sql)
print_hubs(G, n)
print_neighbors(name_tag, depth, G)
print_graph_stats(G)
"""

from __future__ import annotations

from typing import Optional

import networkx as nx
from rich.console import Console
from rich.panel import Panel
from rich.table import Table, box

import db
import graph as gph

console = Console()


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _resolve_player(name_tag: str) -> Optional[str]:
    """Look up a player PUUID from 'GameName#TagLine' or just 'GameName'."""
    if "#" in name_tag:
        game_name, tag_line = name_tag.rsplit("#", 1)
    else:
        game_name, tag_line = name_tag, ""

    player = db.find_player_by_name(game_name, tag_line)
    if player:
        return player["puuid"]

    if not tag_line:
        # Try fuzzy match on game_name only
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT puuid FROM players WHERE LOWER(game_name) = LOWER(?)",
                (game_name,),
            ).fetchone()
        if row:
            return row["puuid"]

    return None


def _display_name(puuid: str) -> str:
    player = db.get_player(puuid)
    if player:
        name = player["game_name"]
        tag  = player["tag_line"]
        tier = player["tier"] or ""
        div  = player["division"] or ""
        lp   = player["lp"]
        rank_str = f" ({tier} {div} {lp}LP)".rstrip() if tier else ""
        return f"{name}#{tag}{rank_str}"
    return puuid[:16] + "…"


# ---------------------------------------------------------------------------
# Path analysis
# ---------------------------------------------------------------------------

def print_path(
    name_tag_a: str,
    name_tag_b: str,
    G: Optional[nx.Graph] = None,
    use_sql: bool = False,
) -> None:
    """Print the shortest co-game path between two players."""
    puuid_a = _resolve_player(name_tag_a)
    puuid_b = _resolve_player(name_tag_b)

    if not puuid_a:
        console.print(f"[red]Player '{name_tag_a}' not found in database.[/red]")
        return
    if not puuid_b:
        console.print(f"[red]Player '{name_tag_b}' not found in database.[/red]")
        return

    if puuid_a == puuid_b:
        console.print("[yellow]Same player![/yellow]")
        return

    console.print()
    console.print(Panel(
        f"[bold]{name_tag_a}[/bold]  →  [bold]{name_tag_b}[/bold]",
        title="[bold cyan]Six Degrees of League[/bold cyan]",
        border_style="cyan",
        expand=False,
    ))

    # Choose SQL BFS (no graph load) or NetworkX
    if use_sql or G is None:
        path = gph.sql_shortest_path(puuid_a, puuid_b, max_depth=6)
    else:
        try:
            path = gph.shortest_path(G, puuid_a, puuid_b)
        except nx.NetworkXNoPath:
            path = None
        except nx.NodeNotFound as e:
            console.print(f"[red]Node not in graph: {e}[/red]")
            return

    if path is None:
        console.print("[yellow]No path found within 6 hops.[/yellow]")
        return

    console.print(f"\n  [green]Path length: {len(path) - 1} hop(s)[/green]\n")

    t = Table(box=box.SIMPLE, show_header=False)
    t.add_column("Step", justify="right", style="dim", min_width=4)
    t.add_column("Player", style="bold")
    t.add_column("Edge")

    for i, puuid in enumerate(path):
        name = _display_name(puuid)
        if i < len(path) - 1:
            # Find a shared game for the edge label
            next_puuid = path[i + 1]
            shared = _shared_game_count(puuid, next_puuid)
            edge   = f"── played together {shared}x ──▶"
        else:
            edge = ""
        t.add_row(str(i), name, edge)

    console.print(t)


def _shared_game_count(puuid_a: str, puuid_b: str) -> int:
    with db.get_connection() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM game_participants a
            JOIN game_participants b ON a.match_id = b.match_id
            WHERE a.puuid = ? AND b.puuid = ?
            """,
            (puuid_a, puuid_b),
        ).fetchone()
    return row["cnt"] if row else 0


# ---------------------------------------------------------------------------
# Hub analysis
# ---------------------------------------------------------------------------

def print_hubs(G: nx.Graph, n: int = 20) -> None:
    """Print the most-connected players in the graph."""
    hubs = gph.top_hubs(G, n)

    console.print()
    console.print(Panel(
        f"Top {n} most-connected players",
        title="[bold cyan]Graph Hubs[/bold cyan]",
        border_style="cyan",
        expand=False,
    ))

    t = Table(box=box.SIMPLE_HEAVY)
    t.add_column("Rank", justify="right", style="dim")
    t.add_column("Player", style="bold", min_width=24)
    t.add_column("Rank/LP", justify="right")
    t.add_column("Connections", justify="right")

    for i, (puuid, degree) in enumerate(hubs, 1):
        player = db.get_player(puuid)
        if player:
            name     = f"{player['game_name']}#{player['tag_line']}"
            tier     = player["tier"] or "-"
            div      = player["division"] or ""
            lp       = player["lp"] or 0
            rank_str = f"{tier} {div} {lp}LP".strip()
        else:
            name     = puuid[:20] + "…"
            rank_str = "-"
        t.add_row(str(i), name, rank_str, str(degree))

    console.print(t)


# ---------------------------------------------------------------------------
# Neighbors
# ---------------------------------------------------------------------------

def print_neighbors(
    name_tag: str,
    depth: int = 1,
    G: Optional[nx.Graph] = None,
) -> None:
    """Print all players within `depth` hops of a given player."""
    puuid = _resolve_player(name_tag)
    if not puuid:
        console.print(f"[red]Player '{name_tag}' not found in database.[/red]")
        return

    if G is None:
        console.print("[yellow]No graph loaded — building from DB (may be slow)…[/yellow]")
        G = gph.build_graph()

    neighbors = gph.find_neighbors(G, puuid, depth)

    console.print()
    console.print(Panel(
        f"[bold]{name_tag}[/bold]  ·  depth={depth}  ·  {len(neighbors)} neighbors",
        title="[bold cyan]Player Neighborhood[/bold cyan]",
        border_style="cyan",
        expand=False,
    ))

    if not neighbors:
        console.print("[yellow]No neighbors found.[/yellow]")
        return

    t = Table(box=box.SIMPLE_HEAVY)
    t.add_column("Player", style="bold", min_width=24)
    t.add_column("Rank/LP", justify="right")
    t.add_column("Shared Games", justify="right")

    rows = []
    for np_puuid in neighbors:
        shared = _shared_game_count(puuid, np_puuid) if depth == 1 else 0
        player = db.get_player(np_puuid)
        if player:
            name     = f"{player['game_name']}#{player['tag_line']}"
            tier     = player["tier"] or "-"
            div      = player["division"] or ""
            lp       = player["lp"] or 0
            rank_str = f"{tier} {div} {lp}LP".strip()
        else:
            name     = np_puuid[:20] + "…"
            rank_str = "-"
        rows.append((shared, name, rank_str))

    for shared, name, rank_str in sorted(rows, key=lambda r: -r[0]):
        t.add_row(name, rank_str, str(shared) if depth == 1 else "—")

    console.print(t)


# ---------------------------------------------------------------------------
# Graph statistics
# ---------------------------------------------------------------------------

def print_graph_stats(G: nx.Graph) -> None:
    stats = gph.graph_stats(G)

    console.print()
    t = Table.grid(padding=(0, 2))
    t.add_column(style="dim")
    t.add_column()
    t.add_row("Nodes (players)",     str(stats["nodes"]))
    t.add_row("Edges (co-games)",    str(stats["edges"]))
    t.add_row("Components",          str(stats["components"]))
    t.add_row("Largest component",   str(stats["largest_component"]))
    t.add_row("Average degree",      str(stats["avg_degree"]))
    t.add_row("Graph density",       str(stats["density"]))
    console.print(Panel(t, title="[bold cyan]Graph Statistics[/bold cyan]", border_style="cyan", expand=False))
