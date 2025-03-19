import redis
from typing import Any, Optional
from .cache import ICacheBackend
class RedisCacheBackend(ICacheBackend):
    def __init__(self, host="localhost", port=6379, db=0):
        self.client = redis.StrictRedis(host=host, port=port, db=db)

    def get(self, key: str) -> Optional[Any]:
        data = self.client.get(key)
        if data is None:
            return None
        # Up to you to handle serialization (e.g. pickling)
        return data

    def set(self, key: str, value: Any, expire: int = 86400):
        # e.g., store raw bytes or pickled
        self.client.set(key, value, ex=expire)

    def delete(self, key: str):
        self.client.delete(key)
