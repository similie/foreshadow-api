from typing import Any, Optional

CACHE_TTL = 3600

class ICacheBackend:
    """
    Interface for a cache backend.
    """
    def get(self, key: str) -> Optional[Any]:
        raise NotImplementedError

    def set(self, key: str, value: Any, expire: int = 0):
        raise NotImplementedError

    def delete(self, key: str):
        raise NotImplementedError
