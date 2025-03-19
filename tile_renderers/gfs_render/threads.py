import concurrent.futures
from multiprocessing import cpu_count

class ConcurrencyService:
    """
    Wraps a ThreadPoolExecutor for concurrency tasks.
    """
    def __init__(self, max_workers=None):
        if max_workers is None:
            max_workers = cpu_count()
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)

    def submit(self, fn, *args, **kwargs):
        return self.executor.submit(fn, *args, **kwargs)

    def shutdown(self, wait=True):
        self.executor.shutdown(wait=wait)
