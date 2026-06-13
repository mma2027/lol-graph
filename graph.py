"""
graph.py — Build and analyze the player co-game graph using NetworkX.

Graph semantics
---------------
- Nodes  : player PUUIDs
- Edges  : two players appeared in the same ranked solo/duo match
- Edge weight : number of shared games (higher = played together more)

The graph is built lazily from the SQLite DB via a self-join on
game_participants, so no large in-memory structure is required for
queries that only need a local neighborhood.

Functions
---------
build_graph(patches=None, min_shared=1)  → nx.Graph
shortest_path(G, puuid_a, puuid_b)       → list[str]  (PUUIDs)
top_hubs(G, n=20)                        → list[(puuid, degree)]
graph_stats(G)                           → dict
export_graphml(G, path)                  → None
find_neighbors(G, puuid, depth=1)        → set[str]
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

import networkx as nx

import db


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------

def build_graph(
    patches: Optional[list[str]] = None,
    min_shared: int = 1,
) -> nx.Graph:
    """
    Build a NetworkX Graph from game_participants.

    Parameters
    ----------
    patches     : optional list of patch strings to filter (e.g. ["16.9", "16.10"])
    min_shared  : minimum number of shared games to include an edge (default 1)
    """
    G: nx.Graph = nx.Graph()

    with db.get_connection() as conn:
        # Add all players as nodes with attributes
        player_rows = conn.execute(
            "SELECT puuid, game_name, tag_line, tier, division, lp FROM players"
        ).fetchall()
        for p in player_rows:
            G.add_node(
                p["puuid"],
                game_name=p["game_name"],
                tag_line=p["tag_line"],
                tier=p["tier"] or "",
                division=p["division"] or "",
                lp=p["lp"] or 0,
            )

        # Self-join: find all pairs of players who shared a match
        patch_filter = ""
        params: list = []
        if patches:
            placeholders = ",".join("?" * len(patches))
            patch_filter = f"JOIN games g ON g.match_id = a.match_id WHERE g.patch IN ({placeholders})"
            params = list(patches)

        query = f"""
            SELECT a.puuid AS puuid_a, b.puuid AS puuid_b, COUNT(*) AS shared
            FROM game_participants a
            JOIN game_participants b
                ON a.match_id = b.match_id AND a.puuid < b.puuid
            {patch_filter}
            GROUP BY a.puuid, b.puuid
            HAVING shared >= ?
        """
        params.append(min_shared)

        rows = conn.execute(query, params).fetchall()

    for row in rows:
        G.add_edge(
            row["puuid_a"],
            row["puuid_b"],
            weight=row["shared"],
        )

    return G


# ---------------------------------------------------------------------------
# Path queries
# ---------------------------------------------------------------------------

def shortest_path(
    G: nx.Graph,
    puuid_a: str,
    puuid_b: str,
) -> list[str]:
    """
    Return the shortest path between two players as a list of PUUIDs.
    Raises nx.NetworkXNoPath if no path exists.
    Raises nx.NodeNotFound if either node is missing.
    """
    return nx.shortest_path(G, puuid_a, puuid_b)


def find_neighbors(
    G: nx.Graph,
    puuid: str,
    depth: int = 1,
) -> set[str]:
    """
    Return all PUUIDs reachable from `puuid` within `depth` hops (excluding self).
    """
    if puuid not in G:
        return set()
    nodes = nx.ego_graph(G, puuid, radius=depth).nodes()
    return set(nodes) - {puuid}


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def top_hubs(G: nx.Graph, n: int = 20) -> list[tuple[str, int]]:
    """Return the top-n nodes by degree (most connections)."""
    degree_view = sorted(G.degree(), key=lambda x: x[1], reverse=True)
    return degree_view[:n]


def graph_stats(G: nx.Graph) -> dict:
    """Return basic graph statistics."""
    components = list(nx.connected_components(G))
    largest    = max(components, key=len) if components else set()
    return {
        "nodes":               G.number_of_nodes(),
        "edges":               G.number_of_edges(),
        "components":          len(components),
        "largest_component":   len(largest),
        "avg_degree":          round(
            sum(d for _, d in G.degree()) / max(G.number_of_nodes(), 1), 2
        ),
        "density":             round(nx.density(G), 6),
    }


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def export_graphml(G: nx.Graph, path: str | Path) -> None:
    """
    Write the graph to a GraphML file readable by Gephi / yEd.
    Node attribute 'label' is set to 'GameName#TagLine' for display.
    """
    # GraphML requires string attributes; cast everything
    H = nx.Graph()
    for node, data in G.nodes(data=True):
        label = f"{data.get('game_name', '')}#{data.get('tag_line', '')}"
        H.add_node(
            node,
            label=label,
            tier=str(data.get("tier", "")),
            lp=str(data.get("lp", 0)),
        )
    for u, v, data in G.edges(data=True):
        H.add_edge(u, v, weight=str(data.get("weight", 1)))

    nx.write_graphml(H, str(path))


# ---------------------------------------------------------------------------
# Lightweight SQL-only path (no full graph needed)
# ---------------------------------------------------------------------------

def sql_shortest_path(puuid_a: str, puuid_b: str, max_depth: int = 6) -> Optional[list[str]]:
    """
    BFS shortest path using SQL queries only — avoids loading the full graph.
    Good for quick lookups when the graph is too large to hold in memory.

    Returns list of PUUIDs from A to B, or None if not found within max_depth.
    """
    with db.get_connection() as conn:
        return _bfs_sql(conn, puuid_a, puuid_b, max_depth)


def _bfs_sql(
    conn: sqlite3.Connection,
    start: str,
    end: str,
    max_depth: int,
) -> Optional[list[str]]:
    if start == end:
        return [start]

    visited: dict[str, str | None] = {start: None}  # puuid -> parent
    frontier = [start]

    for _ in range(max_depth):
        if not frontier:
            break
        placeholders = ",".join("?" * len(frontier))
        rows = conn.execute(
            f"""
            SELECT DISTINCT b.puuid
            FROM game_participants a
            JOIN game_participants b ON a.match_id = b.match_id
            WHERE a.puuid IN ({placeholders}) AND b.puuid != a.puuid
            """,
            frontier,
        ).fetchall()

        next_frontier: list[str] = []
        for row in rows:
            p = row["puuid"]
            if p not in visited:
                # Find which frontier node is the parent
                parent = _find_parent(conn, frontier, p)
                visited[p] = parent
                if p == end:
                    return _reconstruct(visited, start, end)
                next_frontier.append(p)

        frontier = next_frontier

    return None


def _find_parent(
    conn: sqlite3.Connection,
    candidates: list[str],
    child: str,
) -> str:
    """Find which candidate co-appeared in a game with child."""
    placeholders = ",".join("?" * len(candidates))
    row = conn.execute(
        f"""
        SELECT a.puuid
        FROM game_participants a
        JOIN game_participants b ON a.match_id = b.match_id
        WHERE a.puuid IN ({placeholders}) AND b.puuid = ?
        LIMIT 1
        """,
        candidates + [child],
    ).fetchone()
    return row["puuid"] if row else candidates[0]


def _reconstruct(visited: dict[str, str | None], start: str, end: str) -> list[str]:
    path = []
    node: str | None = end
    while node is not None:
        path.append(node)
        node = visited.get(node)
    path.reverse()
    return path
