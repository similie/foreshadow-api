import time

class TimeLogger:
    def __init__(self, off = False):
        self.off = off
        # Start the clock immediately upon creating the object
        self.start_time = time.perf_counter()

    def log(self, label="Time elapsed"):
        """
        Prints how many milliseconds have elapsed since this object was created.
        Optionally pass in a label for clarity.
        """
        if self.off:
            return

        elapsed_seconds = time.perf_counter() - self.start_time
        elapsed_ms = elapsed_seconds * 1000
        print(f"{label}: {elapsed_ms:.3f} ms")

    def reset(self):
        """
        Resets the start time to now.
        """
        self.start_time = time.perf_counter()
