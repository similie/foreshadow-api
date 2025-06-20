import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, List
from .cache import ICacheBackend


class LocalStorage(ICacheBackend):
    """
    A simple in‐memory key/value store with per‐key TTL.
    Keys added with expire=0 never expire (unless explicitly deleted).
    You can call `extend(key, new_ttl_seconds)` at any time to reset a key's TTL.
    A background daemon thread removes expired keys automatically.
    """

    def __init__(self):
        self.data: Dict[str, Any] = {}
        # data_time maps key -> {"created_at": datetime, "ttl": int}
        self.data_time: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._stop_event = threading.Event()

        # Prepare (but don’t start) the cleaner thread
        self._cleaner_thread = threading.Thread(
            target=self._cleanup_thread,
            daemon=True,
            name="LocalStorage-Cleaner"
        )
        self._started = False

    def _ensure_cleaner_running(self):
        """Start the cleaner thread once, on first call."""
        if not self._started:
            with self._lock:
                if not self._started:
                    self._started = True
                    self._cleaner_thread.start()

    def available(self, key: str) -> bool:
        with self._lock:
            if key not in self.data:
                return False
            if key not in self.data_time:
                return True
            return not self._is_expired(key)

    def set(self, key: str, value: Any, expire: int = 0):
        """
        Store `value` under `key`.  If expire > 0, the key will live for `expire` seconds.
        If expire == 0, key never expires (until manually deleted or overwritten).
        """
        with self._lock:
            self.data[key] = value
            if expire > 0:
                self._set_key_to_now(key, expire)
            else:
                self.data_time.pop(key, None)

        self._ensure_cleaner_running()

    def get(self, key: str) -> Optional[Any]:
        """
        Retrieve the value if present and not expired.  If expired, delete and return None.
        """
        with self._lock:
            if key not in self.data:
                return None
            if key in self.data_time and self._is_expired(key):
                # Expired—remove and return None
                self._delete_no_lock(key)
                return None
            return self.data[key]

    def delete(self, key: str):
        """
        Remove a key (and its TTL) if present.
        """
        with self._lock:
            self._delete_no_lock(key)
        self._ensure_cleaner_running()

    def extend(self, key: str, expire: int = 0):
        """
        If `expire > 0` and `key` exists, reset its TTL to `expire` seconds from now.
        Even if key was originally set with no TTL, this will add a TTL now.
        """
        with self._lock:
            if key in self.data and expire > 0:
                self._set_key_to_now(key, expire)
        self._ensure_cleaner_running()

    def _is_expired(self, key: str) -> bool:
        """
        Return True if `key` is known to have a TTL and that TTL has elapsed.
        """
        details = self.data_time.get(key)
        if not details:
            return False
        return (datetime.now() - details["created_at"]).total_seconds() > details["ttl"]

    def _set_key_to_now(self, key: str, expire: int):
        """
        Reset or create the TTL entry for `key` to `expire` seconds from this moment.
        """
        self.data_time[key] = {
            "created_at": datetime.now(),
            "ttl": expire
        }

    def _delete_no_lock(self, key: str):
        """
        Delete `key` and its TTL from both dicts without acquiring lock.
        (Caller must hold self._lock already.)
        """
        self.data.pop(key, None)
        self.data_time.pop(key, None)

    def stop(self):
        """
        Stop the background cleaner thread (blocking until it finishes).
        """
        self._stop_event.set()
        if self._started:
            self._cleaner_thread.join()

    def _cleanup_thread(self):
        """
        Background thread: at each iteration, recompute “how many seconds until
        the next key is due to expire,” then sleep exactly that many seconds
        (capped at 60 s).  When it wakes, it deletes any expired key(s), then
        recomputes the next sleep interval.
        """
        while not self._stop_event.is_set():
            with self._lock:
                if not self.data_time:
                    # No expiring keys—sleep one minute
                    next_sleep = 60
                else:
                    now = datetime.now()
                    soonest: Optional[float] = None
                    expired: List[str] = []

                    # Find expired keys and next expiration time
                    for k, details in list(self.data_time.items()):
                        expires_at = details["created_at"] + timedelta(seconds=details["ttl"])
                        delta = (expires_at - now).total_seconds()
                        if delta <= 0:
                            expired.append(k)
                        else:
                            if soonest is None or delta < soonest:
                                soonest = delta

                    # Remove expired
                    for k in expired:
                        self._delete_no_lock(k)

                    # Determine sleep interval
                    if soonest is None:
                        next_sleep = 60
                    else:
                        next_sleep = min(soonest, 60)

            time.sleep(next_sleep)
# class LocalStorage:
#     """
#     A simple in‐memory key/value store with per‐key TTL.
#     Keys added with expire=0 never expire (unless explicitly deleted).
#     You can call `extend(key, new_ttl_seconds)` at any time to reset a key's TTL.
#     A background daemon thread removes expired keys automatically.
#     """

#     def __init__(self):
#         self.data: Dict[str, Any] = {}
#         # data_time maps key -> {"created_at": datetime, "ttl": int}
#         self.data_time: Dict[str, Dict[str, Any]] = {}
#         self._lock = threading.Lock()
#         self._stop_event = threading.Event()

#         # Start cleanup thread (daemon so it won’t block program exit)
#         self._cleaner_thread = threading.Thread(target=self._cleanup_thread, daemon=True)
#         self._started = False
#     #  self._cleaner_thread.start()

#     def _enabled_threadstart(self):
#         if self._started:
#             return
#         with self._lock:
#             self._started = True
#             self._cleaner_thread.start()



#     def available(self, key: str) -> bool:
#         with self._lock:
#             if key not in self.data:
#                 return False
#             # If key has no TTL entry, it’s immortal
#             if key not in self.data_time:
#                 return True
#             # Otherwise, check expiry
#             return not self._is_expired(key)

#     def set(self, key: str, value: Any, expire: int = 0):
#         """
#         Store `value` under `key`.  If expire > 0, the key will live for `expire` seconds.
#         If expire == 0, key never expires (until manually deleted or overwritten).
#         """

#         with self._lock:
#             self.data[key] = value
#             if expire > 0:
#                 # Create or update TTL entry
#                 self._set_key_to_now(key, expire)
#             else:
#                 # If someone previously set a TTL, remove it (immortal now)
#                 if key in self.data_time:
#                     del self.data_time[key]

#         self._enabled_threadstart()

#     def get(self, key: str) -> Optional[Any]:
#         """
#         Retrieve the value if present and not expired.  If expired, delete and return None.
#         """
#         with self._lock:
#             if key not in self.data:
#                 return None
#             if key in self.data_time and self._is_expired(key):
#                 # Already expired—delete and return None
#                 self._delete_no_lock(key)
#                 return None
#             return self.data[key]
#         self._enabled_threadstart()

#     def delete(self, key: str):
#         """
#         Remove a key (and its TTL) if present.
#         """
#         with self._lock:
#             self._delete_no_lock(key)
#         self._enabled_threadstart()

#     def extend(self, key: str, expire: int = 0):
#         """
#         If `expire > 0` and `key` exists, reset its TTL to `expire` seconds from now.
#         Even if key was originally set with no TTL, this will add a TTL now.
#         """
#         with self._lock:
#             if key in self.data and expire > 0:
#                 self._set_key_to_now(key, expire)
#         self._enabled_threadstart()

#     def _is_expired(self, key: str) -> bool:
#         """
#         Return True if `key` is known to have a TTL and that TTL has elapsed.
#         """
#         if key not in self.data_time:
#             return False
#         details = self.data_time[key]
#         created = details["created_at"]
#         ttl = details["ttl"]
#         return (datetime.now() - created).total_seconds() > ttl

#     def _set_key_to_now(self, key: str, expire: int):
#         """
#         Reset or create the TTL entry for `key` to `expire` seconds from this moment.
#         """
#         self.data_time[key] = {
#             "created_at": datetime.now(),
#             "ttl": expire
#         }

#     def _delete_no_lock(self, key: str):
#         """
#         Delete `key` and its TTL from both dicts without acquiring lock.
#         (Caller must hold self._lock already.)
#         """
#         if key in self.data:
#             del self.data[key]
#         if key in self.data_time:
#             del self.data_time[key]

#     def stop(self):
#         """
#         Stop the background cleaner thread (blocking until it finishes).
#         """
#         self._stop_event.set()
#         self._cleaner_thread.join()

#     def _cleanup_thread(self):
#         """
#         Background thread: at each iteration, recompute “how many seconds until
#         the next key is due to expire,” then sleep exactly that many seconds (capped at 60 s).
#         When it wakes, it deletes any expired key(s), then recomputes the next sleep interval.
#         """
#         while not self._stop_event.is_set():
#             with self._lock:
#                 if not self.data_time:
#                     # No timed keys at all—sleep a minute before checking again.
#                     next_sleep = 60
#                 else:
#                     now = datetime.now()
#                     soonest_expiration: Optional[float] = None
#                     expired_keys: List[str] = []

#                     # Find which keys have TTL expired, and the minimum positive remaining TTL
#                     for key, details in list(self.data_time.items()):
#                         created = details["created_at"]
#                         ttl = details["ttl"]
#                         expires_at = created + timedelta(seconds=ttl)
#                         delta = (expires_at - now).total_seconds()

#                         if delta <= 0:
#                             # expired
#                             expired_keys.append(key)
#                         else:
#                             if soonest_expiration is None or delta < soonest_expiration:
#                                 soonest_expiration = delta

#                     # Delete all expired keys
#                     for key in expired_keys:
#                         self._delete_no_lock(key)

#                     # Decide how long to sleep:
#                     if soonest_expiration is None:
#                         # Either we just deleted all keys, or no keys left—sleep 60 s
#                         next_sleep = 60
#                     else:
#                         # Sleep until the next key hits zero, but at most 60 s
#                         next_sleep = min(soonest_expiration, 60)

#             # Outside the lock, actually sleep; if stop is set during sleep, we’ll break out next loop
#             time.sleep(next_sleep)
