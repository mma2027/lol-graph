"""
db.py — SQLite schema and query helpers for lol-graph.

Tables
------
players           PUUID → name + rank snapshot
games             One row per ranked solo/duo match stored
game_participants Match × PUUID membership (the edges of the raw data)
scanned_players   BFS tracking: which PUUIDs have been processed
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

DB_PATH = Path(__file__).parent / "lol_graph.db"

# ---------------------------------------------------------------------------
# Patch sort key (shared utility)
# ---------------------------------------------------------------------------

def patch_sort_key(patch: str) -> tuple[int, int]:
    """Numeric sort key for '16.9', '16.10', etc."""
    try:
        a, b = patch.split(".", 1)
        return (int(a), int(b))
    except (ValueError, AttributeError):
        return (0, 0)

# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

@contextmanager
def get_connection(db_path: Path = DB_PATH) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        PRAGMA journal_mode = WAL;
        PRAGMA synchronous  = NORMAL;
        PRAGMA foreign_keys = ON;
        PRAGMA cache_size   = -64000;
        PRAGMA temp_store   = MEMORY;
    """)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
    puuid           TEXT PRIMARY KEY,
    game_name       TEXT NOT NULL,
    tag_line        TEXT NOT NULL,
    summoner_id     TEXT,
    tier            TEXT,
    division        TEXT,
    lp              INTEGER,
    wins            INTEGER,
    losses          INTEGER,
    rank_fetched_at INTEGER,
    first_seen_at   INTEGER
);

CREATE TABLE IF NOT EXISTS games (
    match_id    TEXT PRIMARY KEY,
    game_start  INTEGER,
    duration_s  INTEGER,
    patch       TEXT,
    queue_id    INTEGER
);

CREATE TABLE IF NOT EXISTS game_participants (
    match_id    TEXT NOT NULL,
    puuid       TEXT NOT NULL,
    win         INTEGER,
    champion_id INTEGER,
    team_id     INTEGER,
    PRIMARY KEY (match_id, puuid),
    FOREIGN KEY (match_id) REFERENCES games(match_id),
    FOREIGN KEY (puuid)    REFERENCES players(puuid)
);

CREATE TABLE IF NOT EXISTS scanned_players (
    puuid       TEXT PRIMARY KEY,
    scanned_at  INTEGER
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_games_start        ON games(game_start);
CREATE INDEX IF NOT EXISTS idx_games_patch        ON games(patch);
CREATE INDEX IF NOT EXISTS idx_gp_puuid           ON game_participants(puuid);
CREATE INDEX IF NOT EXISTS idx_gp_match           ON game_participants(match_id);
CREATE INDEX IF NOT EXISTS idx_players_name       ON players(game_name, tag_line);
CREATE INDEX IF NOT EXISTS idx_players_tier       ON players(tier);
"""


def init_db(db_path: Path = DB_PATH) -> None:
    with get_connection(db_path) as conn:
        conn.executescript(_SCHEMA)

# ---------------------------------------------------------------------------
# Write helpers
# ---------------------------------------------------------------------------

def upsert_player(
    puuid: str,
    game_name: str,
    tag_line: str,
    summoner_id: Optional[str] = None,
    rank_info: Optional[dict] = None,
    db_path: Path = DB_PATH,
) -> None:
    now = int(time.time() * 1000)
    tier = division = None
    lp = wins = losses = None
    rank_fetched_at = None
    if rank_info:
        tier            = rank_info.get("tier")
        division        = rank_info.get("rank")
        lp              = rank_info.get("leaguePoints")
        wins            = rank_info.get("wins")
        losses          = rank_info.get("losses")
        rank_fetched_at = now

    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO players
                (puuid, game_name, tag_line, summoner_id,
                 tier, division, lp, wins, losses, rank_fetched_at, first_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(puuid) DO UPDATE SET
                game_name       = excluded.game_name,
                tag_line        = excluded.tag_line,
                summoner_id     = COALESCE(excluded.summoner_id, summoner_id),
                tier            = COALESCE(excluded.tier, tier),
                division        = COALESCE(excluded.division, division),
                lp              = COALESCE(excluded.lp, lp),
                wins            = COALESCE(excluded.wins, wins),
                losses          = COALESCE(excluded.losses, losses),
                rank_fetched_at = COALESCE(excluded.rank_fetched_at, rank_fetched_at)
            """,
            (puuid, game_name, tag_line, summoner_id,
             tier, division, lp, wins, losses, rank_fetched_at, now),
        )


def insert_game(match_id: str, info: dict, db_path: Path = DB_PATH) -> bool:
    """Insert a game row. Returns True if newly inserted, False if already existed."""
    patch = ""
    version = info.get("gameVersion", "")
    parts = version.split(".")
    if len(parts) >= 2:
        patch = f"{parts[0]}.{parts[1]}"

    with get_connection(db_path) as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO games (match_id, game_start, duration_s, patch, queue_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                match_id,
                info.get("gameStartTimestamp"),
                info.get("gameDuration"),
                patch,
                info.get("queueId"),
            ),
        )
        return cur.rowcount > 0


def insert_participants(match_id: str, participants: list[dict], db_path: Path = DB_PATH) -> None:
    with get_connection(db_path) as conn:
        for p in participants:
            puuid = p.get("puuid", "")
            if not puuid or puuid == "BOT":
                continue
            conn.execute(
                """
                INSERT OR IGNORE INTO game_participants
                    (match_id, puuid, win, champion_id, team_id)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    match_id,
                    puuid,
                    1 if p.get("win") else 0,
                    p.get("championId"),
                    p.get("teamId"),
                ),
            )
            # Ensure player row exists (name will be updated later if needed)
            game_name = p.get("riotIdGameName") or p.get("summonerName") or ""
            tag_line  = p.get("riotIdTagline") or ""
            conn.execute(
                """
                INSERT OR IGNORE INTO players (puuid, game_name, tag_line, first_seen_at)
                VALUES (?, ?, ?, ?)
                """,
                (puuid, game_name, tag_line, int(time.time() * 1000)),
            )


def mark_scanned(puuid: str, db_path: Path = DB_PATH) -> None:
    with get_connection(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO scanned_players (puuid, scanned_at) VALUES (?, ?)",
            (puuid, int(time.time() * 1000)),
        )


def queue_player(puuid: str, db_path: Path = DB_PATH) -> None:
    with get_connection(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO scanned_players (puuid, scanned_at) VALUES (?, NULL)",
            (puuid,),
        )


def is_scanned(puuid: str, db_path: Path = DB_PATH) -> bool:
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT scanned_at FROM scanned_players WHERE puuid = ?", (puuid,)
        ).fetchone()
        return row is not None and row["scanned_at"] is not None


def game_exists(match_id: str, db_path: Path = DB_PATH) -> bool:
    with get_connection(db_path) as conn:
        return conn.execute(
            "SELECT 1 FROM games WHERE match_id = ?", (match_id,)
        ).fetchone() is not None

# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------

def find_player_by_name(
    game_name: str,
    tag_line: str,
    db_path: Path = DB_PATH,
) -> Optional[sqlite3.Row]:
    with get_connection(db_path) as conn:
        return conn.execute(
            "SELECT * FROM players WHERE LOWER(game_name) = LOWER(?) AND LOWER(tag_line) = LOWER(?)",
            (game_name, tag_line),
        ).fetchone()


def get_player(puuid: str, db_path: Path = DB_PATH) -> Optional[sqlite3.Row]:
    with get_connection(db_path) as conn:
        return conn.execute(
            "SELECT * FROM players WHERE puuid = ?", (puuid,)
        ).fetchone()


def get_players_by_tier(tier: str, db_path: Path = DB_PATH) -> list[sqlite3.Row]:
    with get_connection(db_path) as conn:
        return conn.execute(
            "SELECT * FROM players WHERE UPPER(tier) = UPPER(?) ORDER BY lp DESC",
            (tier,),
        ).fetchall()


def get_collection_stats(db_path: Path = DB_PATH) -> dict:
    with get_connection(db_path) as conn:
        total_games   = conn.execute("SELECT COUNT(*) FROM games").fetchone()[0]
        total_players = conn.execute("SELECT COUNT(*) FROM players").fetchone()[0]
        scanned       = conn.execute(
            "SELECT COUNT(*) FROM scanned_players WHERE scanned_at IS NOT NULL"
        ).fetchone()[0]
        queued        = conn.execute(
            "SELECT COUNT(*) FROM scanned_players WHERE scanned_at IS NULL"
        ).fetchone()[0]
        patch_rows    = conn.execute(
            "SELECT patch, COUNT(*) AS cnt FROM games GROUP BY patch"
        ).fetchall()
        games_by_patch = {
            row["patch"]: row["cnt"]
            for row in sorted(patch_rows, key=lambda r: patch_sort_key(r["patch"]), reverse=True)
        }

    return {
        "total_games":   total_games,
        "total_players": total_players,
        "scanned":       scanned,
        "queued":        queued,
        "games_by_patch": games_by_patch,
    }
