import threading
import time
from datetime import datetime
from .cache import ICacheBackend, CACHE_TTL

class LocalStorage(ICacheBackend):
    def __init__(self):
        self.data = {}
        self.data_time = {}
        self._lock = threading.Lock()  # for thread-safety
        self._stop_event = threading.Event()
        # Start the cleaner thread as a daemon
        self._cleaner_thread = threading.Thread(target=self._cleanup_thread, daemon=True)
        self._cleaner_thread.start()

    def available(self, key: str) -> bool:
        with self._lock:
            return key in self.data

    def set(self, key, value, expire: int = 0):
        with self._lock:
            self.data[key] = value
            if expire > 0:
                self._set_key_to_now(key, expire)

    def get(self, key):
        with self._lock:
            if key in self.data:
                if key in self.data_time:
                    # Check if the cached entry has expired
                    if (self._is_expired(key)):
                        self.delete(key)
                        return None
                return self.data.get(key)
            return None

    def delete(self, key: str):
        with self._lock:
            if key in self.data:
                del self.data[key]
            if key in self.data_time:
                del self.data_time[key]

    def _is_expired(self, key: str) -> bool:
        if key in self.data_time:
            timestamp = self.data_time[key]['created_at']
            ttl = self.data_time[key]['ttl']
            return (datetime.now() - timestamp).total_seconds() > ttl
        return False

    def _cleanup_thread(self):
        while not self._stop_event.is_set():
            time.sleep(60)  # Sleep for the TTL duration
            keys_to_delete = []
            with self._lock:
                # Identify keys that have expired
                for key, details in list(self.data_time.items()):
                    if self._is_expired(key):
                        keys_to_delete.append(key)
                print(f"Expired keys: {keys_to_delete}")
                # Delete the expired keys
                for key in keys_to_delete:
                    self.delete(key)

    def _set_key_to_now(self, key: str, expire: int = CACHE_TTL):
        with self._lock:
            self.data_time[key] = {
                'created_at': datetime.now(),
                'ttl': expire
            }

    def extend(self, key: str, expire: int = 0):
        with self._lock:
            if key in self.data and key in self.data_time and expire > 0:
                self._set_key_to_now(key, expire)

    def stop(self):
        """Stop the cleaner thread gracefully."""
        self._stop_event.set()
        self._cleaner_thread.join()
