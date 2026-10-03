import asyncio

from music_bot import MusicBot, Track


async def find_track(query: str, requested_by: str) -> Track:
    return await asyncio.to_thread(MusicBot._search_track, query, requested_by)
