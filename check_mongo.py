import asyncio
import os

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

load_dotenv()


async def main() -> None:
    client = AsyncIOMotorClient(
        os.environ["MONGODB_URI"],
        serverSelectionTimeoutMS=8000,
    )
    print(await client.admin.command("ping"))


asyncio.run(main())