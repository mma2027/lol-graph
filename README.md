# lol-graph

A League of Legends player co-game graph. Crawls ranked solo/duo matches since
Season 15 (January 2025) and builds a network where **nodes are players** and
**edges connect players who appeared in the same game**. Use it to find the
shortest chain of shared games between any two players, identify hub accounts
that link many communities, or export the graph to Gephi for visualization.

## How it works

1. **Collect** — BFS from a seed player (or the master+ ladder). For each
   player, fetches their recent ranked match IDs and stores all 10 participants
   per game.
2. **Graph** — A self-join on `game_participants` turns the SQLite data into a
   NetworkX graph (or a SQL-only BFS if the graph is too large to load).
3. **Analyze** — Shortest paths, hub rankings, neighbor lookups, GraphML export.

---

## Installation

```bash
cd lol-graph
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate

pip install -r requirements.txt
```

Copy `.env.example` to `.env` and add your Riot API key:

```bash
cp .env.example .env
# then edit .env
```

`.env`:
```
RIOT_API_KEY=RGAPI-xxxx-your-key-here
RIOT_PLATFORM=na1
```

> **API key note** — A development key (100 req/2 min) works for small seeds
> (`--max-players 50`). A production key is needed for large-scale crawls.
> The client enforces a 18 req/s semaphore that stays safely under the dev-key
> 20 req/s burst limit.

---

## Usage

### 1. Collect data

```bash
# Seed from the NA master/GM/challenger ladder (broad coverage)
python main.py collect

# Seed from a specific player and expand outward
python main.py collect --seed "SqfeWalk#NA1"

# Limit the BFS to N players (good for testing)
python main.py collect --seed "SqfeWalk#NA1" --max-players 50

# Print every match fetched
python main.py collect --seed "SqfeWalk#NA1" --verbose
```

Collection is **resume-safe** — re-running picks up where it left off. Only
games since Season 15 start (2025-01-09) are fetched.

### 2. Database stats

```bash
python main.py stats
```

Shows total games, players, scanned/queued counts, and a games-by-patch
breakdown.

### 3. Shortest path between two players

```bash
python main.py path "SqfeWalk#NA1" "Faker#T1"
```

Finds the shortest chain of shared games connecting the two players and prints
each hop with the number of times that pair played together.

```
  Path length: 3 hop(s)

   0  SqfeWalk#NA1 (DIAMOND I 85LP)   ── played together 4x ──▶
   1  SomePlayer#NA1                  ── played together 1x ──▶
   2  AnotherPlayer#KR                ── played together 2x ──▶
   3  Faker#T1 (CHALLENGER I 1200LP)
```

By default this loads the full graph into memory (fast for repeated queries).
Use `--sql` to skip the graph load and run a SQL BFS instead (slower per query
but uses much less RAM):

```bash
python main.py path "A#NA1" "B#NA1" --sql
```

### 4. Hub players

```bash
python main.py hubs           # top 20 by connection count
python main.py hubs --top 50
```

Shows which players are most connected — accounts that link many different
communities together.

### 5. Player neighborhood

```bash
python main.py neighbors "SqfeWalk#NA1"            # direct teammates/opponents
python main.py neighbors "SqfeWalk#NA1" --depth 2  # friends-of-friends
```

Lists all players reachable within N hops, sorted by number of shared games.

### 6. Export to GraphML (Gephi / yEd)

```bash
python main.py export                        # → lol_graph.graphml
python main.py export --out my_graph.graphml
```

Exports the full graph with node labels (`GameName#TagLine`), tier, and LP as
attributes. Open in [Gephi](https://gephi.org) and run ForceAtlas2 to see the
cluster structure.

### 7. Reset

```bash
python main.py reset
```

Deletes and reinitializes the database. Requires typing `yes` to confirm.

---

## Project structure

```
lol-graph/
├── .env                  # API key (not committed)
├── .env.example
├── requirements.txt
├── main.py               # CLI entry point
├── collector.py          # Async BFS data collection
├── graph.py              # NetworkX graph construction + analysis
├── analyzer.py           # Rich terminal output
├── db.py                 # SQLite schema + query helpers
└── riot_client.py        # Async Riot Games API client
```

**Database** (`lol_graph.db`, created automatically):

| Table | Contents |
|---|---|
| `players` | PUUID → name, rank snapshot |
| `games` | One row per match (start time, duration, patch, queue) |
| `game_participants` | Match × PUUID membership — the raw graph edges |
| `scanned_players` | BFS tracking; `scanned_at IS NULL` = queued but not yet processed |

---

## Roadmap

- [ ] Web UI with D3.js force-directed graph
- [ ] Per-patch edge filtering (only show connections from a specific patch)
- [ ] Rank-filtered hub list (e.g. hub players who are Diamond+)
- [ ] Smurf detection (same account appearing in very different rank brackets)
- [ ] Champion overlap heatmap between frequently connected players
