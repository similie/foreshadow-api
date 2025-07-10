import redis
import os
from typing import Any, Optional
from .cache import ICacheBackend
class RedisCacheBackend(ICacheBackend):
    def __init__(self, host=os.getenv("REDIS_HOST", "localhost"), port=int(os.getenv("REDIS_PORT", 6379)), db=int(os.getenv("REDIS_DB", 0))):
        self._client = redis.StrictRedis(host=host, port=port, db=db)

    @property
    def client(self) -> redis.StrictRedis:
        """
        Expose the raw StrictRedis client for advanced operations (hashes,
        pipelines, etc.).
        """
        return self._client

    def get(self, key: str) -> Optional[Any]:
        data = self.client.get(key)
        if data is None:
            return None
        # Up to you to handle serialization (e.g. pickling)
        return data

    def available(self, key: str) -> bool:
        return bool(self.client.exists(key))

    def set(self, key: str, value: Any, expire: int = 86400):
        # e.g., store raw bytes or pickled
        self.client.set(key, value, ex=expire)

    def extend(self, key: str, expire: int = 0):
        if expire > 0:
            self.client.expire(key, expire)

    def delete(self, key: str):
        self.client.delete(key)
