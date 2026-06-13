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
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

import networkx as nx

import db

_GEXF_NS = "http://www.gexf.net/1.2draft"
_VIZ_NS  = "http://www.gexf.net/1.2draft/viz"


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

def _count_to_color(count: int, min_c: int, max_c: int) -> tuple[int, int, int]:
    """Light steel blue → dark navy based on game count."""
    t = (count - min_c) / max(max_c - min_c, 1)
    r = int(173 + t * (10  - 173))
    g = int(216 + t * (25  - 216))
    b = int(230 + t * (80  - 230))
    return r, g, b


def export_graphml(
    G: nx.Graph,
    path: str | Path,
    seed_puuid: Optional[str] = None,
) -> None:
    """
    Write the graph as GEXF (Gephi's native format).

    - Node color: light blue → dark navy based on number of games played.
    - Seed node: gold (#FFD700).
    - Node size: scales with game count (1–10).
    - Edge weight: number of shared games.

    Colors and sizes are applied automatically when opened in Gephi —
    no manual Appearance configuration needed.
    """
    # Game count per player
    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT puuid, COUNT(*) AS cnt FROM game_participants GROUP BY puuid"
        ).fetchall()
    game_counts = {row["puuid"]: row["cnt"] for row in rows}

    counts = list(game_counts.values()) or [1]
    min_c  = min(counts)
    max_c  = max(counts)

    ET.register_namespace("",    _GEXF_NS)
    ET.register_namespace("viz", _VIZ_NS)

    root = ET.Element(f"{{{_GEXF_NS}}}gexf", {
        "version": "1.2",
        f"{{{_VIZ_NS}}}dummy": "",   # force xmlns:viz declaration on root
    })
    # Remove the dummy attribute we used to force the namespace declaration
    del root.attrib[f"{{{_VIZ_NS}}}dummy"]

    graph_el = ET.SubElement(root, f"{{{_GEXF_NS}}}graph", {
        "mode": "static", "defaultedgetype": "undirected",
    })

    # Node attribute declarations
    attrs_el = ET.SubElement(graph_el, f"{{{_GEXF_NS}}}attributes", {"class": "node"})
    for aid, title, atype in [
        ("0", "tier",    "string"),
        ("1", "lp",      "integer"),
        ("2", "games",   "integer"),
        ("3", "is_seed", "boolean"),
    ]:
        ET.SubElement(attrs_el, f"{{{_GEXF_NS}}}attribute", {
            "id": aid, "title": title, "type": atype,
        })

    # Edge attribute declarations
    eattrs_el = ET.SubElement(graph_el, f"{{{_GEXF_NS}}}attributes", {"class": "edge"})
    ET.SubElement(eattrs_el, f"{{{_GEXF_NS}}}attribute", {
        "id": "0", "title": "shared_games", "type": "integer",
    })

    # Nodes
    nodes_el = ET.SubElement(graph_el, f"{{{_GEXF_NS}}}nodes")
    for node, data in G.nodes(data=True):
        label   = f"{data.get('game_name', '')}#{data.get('tag_line', '')}"
        count   = game_counts.get(node, 1)
        is_seed = (node == seed_puuid)

        n_el = ET.SubElement(nodes_el, f"{{{_GEXF_NS}}}node", {
            "id": str(node), "label": label,
        })

        av_el = ET.SubElement(n_el, f"{{{_GEXF_NS}}}attvalues")
        for aid, val in [
            ("0", str(data.get("tier", ""))),
            ("1", str(data.get("lp", 0))),
            ("2", str(count)),
            ("3", "true" if is_seed else "false"),
        ]:
            ET.SubElement(av_el, f"{{{_GEXF_NS}}}attvalue", {"for": aid, "value": val})

        if is_seed:
            r, g, b = 255, 215, 0
        else:
            r, g, b = _count_to_color(count, min_c, max_c)

        ET.SubElement(n_el, f"{{{_VIZ_NS}}}color",
                      {"r": str(r), "g": str(g), "b": str(b), "a": "255"})

        size = 1.0 + 9.0 * (count - min_c) / max(max_c - min_c, 1)
        ET.SubElement(n_el, f"{{{_VIZ_NS}}}size", {"value": f"{size:.2f}"})

    # Edges
    edges_el = ET.SubElement(graph_el, f"{{{_GEXF_NS}}}edges")
    for i, (u, v, data) in enumerate(G.edges(data=True)):
        weight = data.get("weight", 1)
        e_el = ET.SubElement(edges_el, f"{{{_GEXF_NS}}}edge", {
            "id": str(i), "source": str(u), "target": str(v),
            "weight": str(weight),
        })
        av_el = ET.SubElement(e_el, f"{{{_GEXF_NS}}}attvalues")
        ET.SubElement(av_el, f"{{{_GEXF_NS}}}attvalue", {"for": "0", "value": str(weight)})

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    tree.write(str(path), encoding="utf-8", xml_declaration=True)


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
