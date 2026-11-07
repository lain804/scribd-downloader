"""Small terminal progress display with throttled redraws and logging support."""
import logging
import sys
from time import perf_counter


class ProgressBar:
    active = None

    def __init__(self, label: str = "", *, enabled: bool = True, stream=None):
        self.stream = stream if stream is not None else sys.stderr
        self.enabled = enabled and self.stream.isatty()
        self.label = f"{label}: " if label else ""
        self.phase = ""
        self.completed = self.total = 0
        self._last_draw = 0.0
        self._line = ""
        self._width = 0

    def __enter__(self):
        if not self.enabled:
            return None
        ProgressBar.active = self
        self.update("Reading document", 0, 0)
        return self.update

    def __exit__(self, exc_type, exc_value, traceback):
        if self.enabled:
            if exc_type is not None:
                status = "Interrupted" if exc_type is KeyboardInterrupt else "Failed"
                self.update(status, self.completed, self.total)
            self.stream.write("\n")
            self.stream.flush()
            ProgressBar.active = None

    def update(self, phase: str, completed: int, total: int) -> None:
        changed = phase != self.phase or total != self.total
        self.phase, self.completed, self.total = phase, completed, total
        if not self.enabled:
            return
        now = perf_counter()
        if not changed and completed != total and now - self._last_draw < 0.1:
            return
        if total:
            filled = min(20, completed * 20 // total)
            bar = "#" * filled + "-" * (20 - filled)
            self._line = f"{self.label}{phase} [{bar}] {completed}/{total} pages"
        else:
            self._line = f"{self.label}{phase}..."
        self.redraw()
        self._last_draw = now

    def clear(self) -> None:
        self.stream.write("\r" + " " * self._width + "\r")

    def redraw(self) -> None:
        self.stream.write("\r" + self._line.ljust(self._width))
        self._width = len(self._line)
        self.stream.flush()


class ProgressLoggingHandler(logging.StreamHandler):
    """Keep log messages on separate lines above the active progress bar."""

    def emit(self, record):
        bar = ProgressBar.active
        if bar is not None and bar.stream is self.stream:
            bar.clear()
        super().emit(record)
        if bar is not None and bar.stream is self.stream:
            bar.redraw()
