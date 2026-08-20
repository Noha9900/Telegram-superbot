"""
database.py
-----------
Async MongoDB access layer (Motor). One connection-pooled client is created
at import time and reused everywhere (Motor/PyMongo already pool internally,
so we must NOT create a new client per request).

Collections & schemas (see README for full field descriptions):

users        {_id: user_id, name, username, warn_count, warns: [...],
              is_vip, vip_plan, vip_expiry, joined_date, last_seen,
              messages_count, media_count, active_hours: {...}, is_banned}

chats        {_id: chat_id, title, rules, fsub_channels: [...],
              auto_duplicate_clean, welcome_config: {...}, warn_limit}

filestore    {_id: token, file_ids: [...], owner_id, is_public, is_revoked,
              access_count, created_at}

transactions {_id: tx_id, user_id, chat_id, amount, plan, status,
              date, invoice_id, screenshot_file_id}

file_hashes  {_id: hash, chat_id, file_id, message_id, media_type, created_at}
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ASCENDING, DESCENDING

from config import Config

logger = logging.getLogger("database")


class Database:
    def __init__(self, uri: str, db_name: str):
        # A single client instance manages an internal connection pool;
        # reuse it across the whole app lifetime.
        self._client = AsyncIOMotorClient(uri, maxPoolSize=50, minPoolSize=5)
        self.db = self._client[db_name]

        self.users = self.db.users
        self.chats = self.db.chats
        self.filestore = self.db.filestore
        self.transactions = self.db.transactions
        self.file_hashes = self.db.file_hashes

    async def ensure_indexes(self):
        """Create indexes idempotently. Call once on startup."""
        await self.users.create_index("is_vip")
        await self.users.create_index("vip_expiry")
        await self.filestore.create_index("_id", unique=True)
        await self.transactions.create_index([("user_id", ASCENDING)])
        await self.transactions.create_index([("status", ASCENDING)])
        await self.file_hashes.create_index([("chat_id", ASCENDING), ("_id", ASCENDING)])
        logger.info("MongoDB indexes ensured.")

    async def ping(self) -> bool:
        try:
            await self._client.admin.command("ping")
            return True
        except Exception as e:
            logger.critical(f"MongoDB ping failed: {e}")
            return False

    # ---------------------------------------------------------------- users
    async def add_user(self, user_id: int, name: str, username: Optional[str] = None):
        await self.users.update_one(
            {"_id": user_id},
            {
                "$setOnInsert": {
                    "name": name,
                    "username": username,
                    "warn_count": 0,
                    "warns": [],
                    "is_vip": False,
                    "vip_plan": None,
                    "vip_expiry": None,
                    "joined_date": datetime.now(timezone.utc),
                    "messages_count": 0,
                    "media_count": 0,
                    "active_hours": {},
                    "is_banned": False,
                },
                "$set": {"last_seen": datetime.now(timezone.utc), "name": name},
            },
            upsert=True,
        )

    async def get_user(self, user_id: int) -> Optional[dict]:
        return await self.users.find_one({"_id": user_id})

    async def all_user_ids(self):
        cursor = self.users.find({}, {"_id": 1})
        return [doc["_id"] async for doc in cursor]

    async def track_activity(self, user_id: int, is_media: bool = False):
        hour = str(datetime.now(timezone.utc).hour)
        inc = {"messages_count": 1}
        if is_media:
            inc["media_count"] = 1
        await self.users.update_one(
            {"_id": user_id},
            {"$inc": {**inc, f"active_hours.{hour}": 1},
             "$set": {"last_seen": datetime.now(timezone.utc)}},
        )

    # ---------------------------------------------------------------- warns
    async def add_warn(self, chat_id: int, user_id: int, reason: str, warn_limit: int = 3):
        doc = await self.users.find_one_and_update(
            {"_id": user_id},
            {
                "$inc": {"warn_count": 1},
                "$push": {"warns": {"chat_id": chat_id, "reason": reason,
                                     "date": datetime.now(timezone.utc)}},
            },
            upsert=True,
            return_document=True,
        )
        count = doc.get("warn_count", 1) if doc else 1
        return count, count >= warn_limit

    async def reset_warns(self, user_id: int):
        await self.users.update_one(
            {"_id": user_id}, {"$set": {"warn_count": 0, "warns": []}}
        )

    # ---------------------------------------------------------------- chats
    async def get_chat(self, chat_id: int) -> dict:
        chat = await self.chats.find_one({"_id": chat_id})
        if not chat:
            chat = {
                "_id": chat_id,
                "rules": None,
                "fsub_channels": [],
                "auto_duplicate_clean": False,
                "welcome_config": {
                    "enabled": True,
                    "text": "👋 Welcome {name} to {group_name}!",
                    "banner": None,
                },
                "warn_limit": 3,
            }
            await self.chats.insert_one(chat)
        return chat

    async def update_chat(self, chat_id: int, update: dict):
        await self.chats.update_one({"_id": chat_id}, {"$set": update}, upsert=True)

    # ------------------------------------------------------------- VIP subs
    async def set_vip(self, user_id: int, plan: str, expiry: datetime):
        await self.users.update_one(
            {"_id": user_id},
            {"$set": {"is_vip": True, "vip_plan": plan, "vip_expiry": expiry}},
            upsert=True,
        )

    async def revoke_vip(self, user_id: int):
        await self.users.update_one(
            {"_id": user_id},
            {"$set": {"is_vip": False, "vip_plan": None, "vip_expiry": None}},
        )

    async def expiring_vips(self, before: datetime):
        cursor = self.users.find({"is_vip": True, "vip_expiry": {"$lte": before}})
        return [doc async for doc in cursor]

    # ------------------------------------------------------------ filestore
    async def create_filestore_entry(self, token: str, file_ids: list, owner_id: int,
                                      is_public: bool = True):
        await self.filestore.insert_one({
            "_id": token,
            "file_ids": file_ids,
            "owner_id": owner_id,
            "is_public": is_public,
            "is_revoked": False,
            "access_count": 0,
            "created_at": datetime.now(timezone.utc),
        })

    async def get_filestore_entry(self, token: str) -> Optional[dict]:
        entry = await self.filestore.find_one({"_id": token})
        if entry and not entry.get("is_revoked"):
            await self.filestore.update_one({"_id": token}, {"$inc": {"access_count": 1}})
        return entry

    async def revoke_filestore_entry(self, token: str):
        await self.filestore.update_one({"_id": token}, {"$set": {"is_revoked": True}})

    # --------------------------------------------------------- transactions
    async def create_transaction(self, tx_id: str, user_id: int, chat_id: int,
                                  amount: float, plan: str, screenshot_file_id: str = None):
        await self.transactions.insert_one({
            "_id": tx_id,
            "user_id": user_id,
            "chat_id": chat_id,
            "amount": amount,
            "plan": plan,
            "status": "pending",
            "date": datetime.now(timezone.utc),
            "invoice_id": None,
            "screenshot_file_id": screenshot_file_id,
        })

    async def set_transaction_status(self, tx_id: str, status: str, invoice_id: str = None):
        update = {"status": status}
        if invoice_id:
            update["invoice_id"] = invoice_id
        await self.transactions.update_one({"_id": tx_id}, {"$set": update})

    # -------------------------------------------------------- duplicate hash
    async def find_duplicate(self, chat_id: int, file_hash: str) -> Optional[dict]:
        return await self.file_hashes.find_one({"_id": file_hash, "chat_id": chat_id})

    async def store_hash(self, file_hash: str, chat_id: int, file_id: str,
                          message_id: int, media_type: str):
        await self.file_hashes.update_one(
            {"_id": file_hash, "chat_id": chat_id},
            {"$setOnInsert": {
                "file_id": file_id, "message_id": message_id,
                "media_type": media_type, "created_at": datetime.now(timezone.utc),
            }},
            upsert=True,
        )


db = Database(Config.MONGO_URI, Config.MONGO_DB_NAME)
