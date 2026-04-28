import asyncio

from core.config import Config
from db.session import get_engine, get_sync_engine


async def main():
    print(f"Config DB URL: {Config.DATABASE_URL}")
    print(f"Sync Engine: {get_sync_engine()}")
    print(f"Async Engine: {get_engine()}")


asyncio.run(main())
