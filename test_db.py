import asyncio

from sqlalchemy import text

from db.session import get_db_session, init_engine


async def test():
    await init_engine()
    async for session in get_db_session():
        try:
            await session.execute(text("SET LOCAL statement_timeout = '10'"))
            # trigger a timeout
            await session.execute(text("SELECT pg_sleep(0.05)"))
        except Exception as e:
            print("Caught exception:", type(e))
        finally:
            # The transaction might be aborted now
            try:
                await session.execute(text("RESET statement_timeout"))
                print("RESET succeeded")
            except Exception as e:
                print("RESET failed:", type(e))

        try:
            await session.execute(text("SELECT 1"))
            print("SELECT 1 succeeded")
        except Exception as e:
            print("SELECT 1 failed:", type(e))


asyncio.run(test())
