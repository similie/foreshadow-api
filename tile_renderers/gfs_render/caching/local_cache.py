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

    def set(self, key, value, expire: int = 0):
        with self._lock:
            self.data[key] = value
            if expire > 0:
                self.data_time[key] = datetime.now()

    def get(self, key):
        with self._lock:
            if key in self.data:
                if key in self.data_time:
                    # Check if the cached entry has expired
                    if (datetime.now() - self.data_time[key]).total_seconds() > CACHE_TTL:
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

    def _cleanup_thread(self):
        while not self._stop_event.is_set():
            time.sleep(CACHE_TTL)  # Sleep for the TTL duration
            now = datetime.now()
            keys_to_delete = []
            with self._lock:
                # Identify keys that have expired
                for key, timestamp in list(self.data_time.items()):
                    if (now - timestamp).total_seconds() > CACHE_TTL:
                        keys_to_delete.append(key)
                # Delete the expired keys
                for key in keys_to_delete:
                    self.delete(key)

    def stop(self):
        """Stop the cleaner thread gracefully."""
        self._stop_event.set()
        self._cleaner_thread.join()
