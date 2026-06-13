"""
collector.py — BFS data collection pipeline for lol-graph.

Crawls ranked solo/duo games since Season 15 start (2025-01-09),
storing all 10 participants per match as graph edges.

Usage
-----
    # Seed from master/GM/challenger ladder
    python main.py collect

    # Seed from a specific player and BFS outward
    python main.py collect --seed "SqfeWalk#NA1"
    python main.py collect --seed "SqfeWalk#NA1" --max-players 100
"""

from __future__ import annotations

import asyncio
import os
from collections import deque
from typing import Optional

import aiohttp
from dotenv import load_dotenv
from rich.console import Console
from rich.live import Live
from rich.table import Table

import db
from riot_client import RiotClient, RiotAPIError, make_session

load_dotenv()

console = Console()

# Season 15 start: 2025-01-09 00:00:00 UTC
SEASON15_START_EPOCH = 1_736_380_800  # seconds


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def _make_status_table(
    scanned: int,
    queued: int,
    games: int,
    players: int,
    current: str,
) -> Table:
    t = Table.grid(padding=(0, 2))
    t.add_column(style="dim")
    t.add_column()
    t.add_row("Scanned players",  str(scanned))
    t.add_row("Queued players",   str(queued))
    t.add_row("Games stored",     str(games))
    t.add_row("Players stored",   str(players))
    t.add_row("Current PUUID",    current[:24] + "…" if len(current) > 24 else current)
    return t


# ---------------------------------------------------------------------------
# Core: process a single player
# ---------------------------------------------------------------------------

async def _process_player(
    puuid: str,
    client: RiotClient,
    verbose: bool = False,
) -> set[str]:
    """
    Fetch ranked games for one PUUID, store them, return set of discovered PUUIDs.
    """
    discovered: set[str] = set()

    # 1. Fetch match IDs (all ranked solo/duo since Season 15 start)
    try:
        match_ids = await client.get_match_ids(
            puuid,
            count=100,
            start_time=SEASON15_START_EPOCH,
        )
    except RiotAPIError:
        return discovered

    if verbose and match_ids:
        console.log(f"  {puuid[:16]}… — {len(match_ids)} match IDs")

    # 2. Fetch + store new matches
    fetch_tasks = [
        _fetch_and_store_match(mid, client, verbose)
        for mid in match_ids
        if not db.game_exists(mid)
    ]
    if fetch_tasks:
        results = await asyncio.gather(*fetch_tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, set):
                discovered |= r
            elif isinstance(r, Exception) and verbose:
                console.log(f"  [yellow]match fetch error: {r}[/yellow]")

    # Also discover participants from already-stored matches
    # (no extra API call needed — we read participant PUUIDs from the DB)
    for mid in match_ids:
        with db.get_connection() as conn:
            rows = conn.execute(
                "SELECT puuid FROM game_participants WHERE match_id = ?", (mid,)
            ).fetchall()
        for row in rows:
            p = row["puuid"]
            if p != puuid:
                discovered.add(p)

    # 3. Resolve identity (name from DB if already set; otherwise skip—name
    #    was already stored from participant data in insert_participants)
    # Rank fetch: only if summoner_id is known
    try:
        summoner = await client.get_summoner_by_puuid(puuid)
        summoner_id = summoner.get("id", "")
        if summoner_id:
            rank_info = await client.get_rank(summoner_id)
            with db.get_connection() as conn:
                row = conn.execute(
                    "SELECT game_name, tag_line FROM players WHERE puuid = ?", (puuid,)
                ).fetchone()
            game_name = row["game_name"] if row else ""
            tag_line  = row["tag_line"]  if row else ""
            db.upsert_player(
                puuid, game_name, tag_line,
                summoner_id=summoner_id,
                rank_info=rank_info,
            )
    except RiotAPIError:
        pass

    return discovered


async def _fetch_and_store_match(
    match_id: str,
    client: RiotClient,
    verbose: bool,
) -> set[str]:
    """Fetch one match, store it, return set of participant PUUIDs."""
    try:
        data = await client.get_match(match_id)
    except RiotAPIError:
        return set()

    info     = data.get("info", {})
    metadata = data.get("metadata", {})

    # Store game row
    is_new = db.insert_game(match_id, info)
    if not is_new:
        return set()

    participants = info.get("participants", [])
    db.insert_participants(match_id, participants)

    discovered = set(metadata.get("participants", []))
    if verbose:
        console.log(f"  [green]+[/green] {match_id} ({len(participants)} participants)")
    return discovered


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------

async def _seed_from_player(
    game_name: str,
    tag_line: str,
    client: RiotClient,
) -> Optional[str]:
    """Resolve a Riot ID to PUUID and queue them."""
    try:
        acct = await client.get_account_by_riot_id(game_name, tag_line)
    except RiotAPIError as e:
        console.print(f"[red]Could not resolve {game_name}#{tag_line}: {e}[/red]")
        return None
    puuid = acct.get("puuid", "")
    if puuid:
        db.upsert_player(puuid, acct.get("gameName", game_name), acct.get("tagLine", tag_line))
        db.queue_player(puuid)
    return puuid or None


async def _seed_from_ladder(client: RiotClient) -> int:
    """Seed from master/GM/challenger ladder. Returns number of players queued."""
    count = 0
    for tier in ("challenger", "grandmaster", "master"):
        try:
            entries = await client.get_ladder(tier)
        except RiotAPIError as e:
            console.print(f"[yellow]Could not fetch {tier} ladder: {e}[/yellow]")
            continue
        console.print(f"  {tier.title()}: {len(entries)} players")
        # Ladder entries have summonerId but not PUUID — we need to resolve each.
        # To avoid blowing the rate limit upfront, just store as unresolved seeds.
        # We'll resolve them during BFS when they're dequeued.
        # Instead, batch-fetch PUUIDs for the first 200 to bootstrap quickly.
        for entry in entries[:200]:
            summoner_id = entry.get("summonerId", "")
            if not summoner_id:
                continue
            try:
                summoner = await asyncio.wait_for(
                    client._get(
                        f"https://{client.platform}.api.riotgames.com"
                        f"/lol/summoner/v4/summoners/{summoner_id}"
                    ),
                    timeout=10,
                )
                puuid = summoner.get("puuid", "")
                if puuid:
                    name = entry.get("summonerName", "")
                    db.upsert_player(puuid, name, "")
                    db.queue_player(puuid)
                    count += 1
            except (RiotAPIError, asyncio.TimeoutError):
                pass
    return count


# ---------------------------------------------------------------------------
# Main BFS loop
# ---------------------------------------------------------------------------

async def run_collection(
    seed: Optional[str] = None,
    max_players: Optional[int] = None,
    verbose: bool = False,
) -> None:
    platform = os.getenv("RIOT_PLATFORM", "na1")

    async with make_session() as session:
        client = RiotClient(platform=platform, session=session)

        # ── Seed ────────────────────────────────────────────────────────────
        if seed:
            if "#" in seed:
                game_name, tag_line = seed.rsplit("#", 1)
            else:
                game_name, tag_line = seed, ""
            console.print(f"[cyan]Seeding from player: {seed}[/cyan]")
            puuid = await _seed_from_player(game_name, tag_line, client)
            if not puuid:
                return
            console.print(f"  PUUID: {puuid}")
        else:
            # Check if we already have queued players (resume mode)
            stats = db.get_collection_stats()
            if stats["queued"] == 0 and stats["scanned"] == 0:
                console.print("[cyan]Seeding from master+ ladder…[/cyan]")
                n = await _seed_from_ladder(client)
                console.print(f"  Queued {n} players from ladder")
            else:
                console.print(
                    f"[cyan]Resuming: {stats['queued']} queued, "
                    f"{stats['scanned']} already scanned[/cyan]"
                )

        # ── BFS ─────────────────────────────────────────────────────────────
        console.print("[cyan]Starting BFS collection…[/cyan]")

        with Live(console=console, refresh_per_second=2) as live:
            while True:
                # Fetch next unscanned PUUID from DB
                with db.get_connection() as conn:
                    row = conn.execute(
                        "SELECT puuid FROM scanned_players WHERE scanned_at IS NULL LIMIT 1"
                    ).fetchone()

                if row is None:
                    live.stop()
                    console.print("[green]Queue exhausted — collection complete.[/green]")
                    break

                puuid = row["puuid"]

                stats = db.get_collection_stats()
                if max_players is not None and stats["scanned"] >= max_players:
                    live.stop()
                    console.print(
                        f"[green]Reached --max-players {max_players}. Stopping.[/green]"
                    )
                    break

                live.update(_make_status_table(
                    stats["scanned"],
                    stats["queued"],
                    stats["total_games"],
                    stats["total_players"],
                    puuid,
                ))

                # Process player
                discovered = await _process_player(puuid, client, verbose)

                # Queue newly discovered players
                for p in discovered:
                    if not db.is_scanned(p):
                        db.queue_player(p)

                db.mark_scanned(puuid)

    # Final stats
    stats = db.get_collection_stats()
    console.print()
    console.print(f"[bold]Done.[/bold]")
    console.print(f"  Games:   {stats['total_games']}")
    console.print(f"  Players: {stats['total_players']}")
    console.print(f"  Scanned: {stats['scanned']}")
