from typing import Any, Optional
from .cache import ICacheBackend
class InMemoryCacheBackend(ICacheBackend):
    def __init__(self):
        self.store = {}

    def get(self, key: str) -> Optional[Any]:
        return self.store.get(key)

    def set(self, key: str, value: Any, expire: int = 0):
        # ignoring "expire" for demonstration
        self.store[key] = value

    def delete(self, key: str):
        if key in self.store:
            del self.store[key]
