"""
riot_client.py — Async Riot Games API client for lol-graph.

Endpoints used
--------------
- /riot/account/v1/accounts/by-riot-id/{gameName}/{tagLine}
- /lol/summoner/v4/summoners/by-puuid/{puuid}
- /lol/league/v4/entries/by-summoner/{summonerId}
- /lol/league/v4/{challenger|grandmaster|master}leagues/by-queue/{queue}
- /lol/match/v5/matches/by-puuid/{puuid}/ids
- /lol/match/v5/matches/{matchId}
"""

from __future__ import annotations

import asyncio
import os
from typing import Optional

import aiohttp
from dotenv import load_dotenv

load_dotenv()

PLATFORM_TO_ROUTING: dict[str, str] = {
    "na1":  "americas", "br1":  "americas", "la1":  "americas", "la2":  "americas",
    "euw1": "europe",   "eun1": "europe",   "tr1":  "europe",   "ru":   "europe",
    "kr":   "asia",     "jp1":  "asia",
    "oc1":  "sea",      "sg2":  "sea",      "ph2":  "sea",      "tw2":  "sea",
    "vn2":  "sea",      "th2":  "sea",
}

RANKED_SOLO_QUEUE = "RANKED_SOLO_5x5"
RANKED_SOLO_QUEUE_ID = 420

_SEMAPHORE: asyncio.Semaphore | None = None


def _get_semaphore() -> asyncio.Semaphore:
    global _SEMAPHORE
    if _SEMAPHORE is None:
        _SEMAPHORE = asyncio.Semaphore(18)
    return _SEMAPHORE


class RiotAPIError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(f"[{status}] {message}")


class RiotClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        platform: str = "na1",
        session: Optional[aiohttp.ClientSession] = None,
    ):
        self.api_key  = api_key or os.getenv("RIOT_API_KEY", "")
        self.platform = platform.lower()
        self.routing  = PLATFORM_TO_ROUTING.get(self.platform, "americas")
        self._session = session

    async def _get(self, url: str, params: Optional[dict] = None) -> dict | list:
        sem     = _get_semaphore()
        headers = {"X-Riot-Token": self.api_key}

        async with sem:
            session    = self._session
            owns       = session is None
            if owns:
                session = aiohttp.ClientSession()
            try:
                async with session.get(
                    url, headers=headers,
                    params=params or {},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    elif resp.status == 429:
                        retry_after = int(resp.headers.get("Retry-After", "5"))
                        await asyncio.sleep(retry_after + 1.0)
                    elif resp.status == 404:
                        raise RiotAPIError(404, f"Not found: {url}")
                    elif resp.status in (401, 403):
                        raise RiotAPIError(resp.status, "Check your API key")
                    else:
                        body = await resp.text()
                        raise RiotAPIError(resp.status, f"{url} → {body[:200]}")
            finally:
                if owns:
                    await session.close()

        return await self._get(url, params)

    async def get_account_by_riot_id(self, game_name: str, tag_line: str) -> dict:
        url = (
            f"https://{self.routing}.api.riotgames.com"
            f"/riot/account/v1/accounts/by-riot-id/{game_name}/{tag_line}"
        )
        return await self._get(url)

    async def get_summoner_by_puuid(self, puuid: str) -> dict:
        url = (
            f"https://{self.platform}.api.riotgames.com"
            f"/lol/summoner/v4/summoners/by-puuid/{puuid}"
        )
        return await self._get(url)

    async def get_rank(self, summoner_id: str) -> Optional[dict]:
        url = (
            f"https://{self.platform}.api.riotgames.com"
            f"/lol/league/v4/entries/by-summoner/{summoner_id}"
        )
        try:
            entries: list = await self._get(url)
            for e in entries:
                if e.get("queueType") == RANKED_SOLO_QUEUE:
                    return e
            return None
        except RiotAPIError:
            return None

    async def get_ladder(self, tier: str = "challenger") -> list[dict]:
        tier = tier.lower()
        path = {"challenger": "challengerleagues", "grandmaster": "grandmasterleagues"}.get(
            tier, "masterleagues"
        )
        url  = (
            f"https://{self.platform}.api.riotgames.com"
            f"/lol/league/v4/{path}/by-queue/{RANKED_SOLO_QUEUE}"
        )
        data: dict = await self._get(url)
        return data.get("entries", [])

    async def get_match_ids(
        self,
        puuid: str,
        count: int = 100,
        start_time: Optional[int] = None,
    ) -> list[str]:
        """Fetch up to `count` ranked solo/duo match IDs since `start_time` (epoch s)."""
        url = (
            f"https://{self.routing}.api.riotgames.com"
            f"/lol/match/v5/matches/by-puuid/{puuid}/ids"
        )
        all_ids: list[str] = []
        page_size = min(count, 100)
        offset    = 0

        while len(all_ids) < count:
            params: dict = {
                "queue": RANKED_SOLO_QUEUE_ID,
                "start": offset,
                "count": page_size,
            }
            if start_time is not None:
                params["startTime"] = start_time
            try:
                batch: list = await self._get(url, params)
            except RiotAPIError:
                break
            all_ids.extend(batch)
            if len(batch) < page_size:
                break
            offset += page_size

        return all_ids[:count]

    async def get_match(self, match_id: str) -> dict:
        url = (
            f"https://{self.routing}.api.riotgames.com"
            f"/lol/match/v5/matches/{match_id}"
        )
        return await self._get(url)


def make_session() -> aiohttp.ClientSession:
    connector = aiohttp.TCPConnector(limit=32, ttl_dns_cache=300)
    return aiohttp.ClientSession(connector=connector)
