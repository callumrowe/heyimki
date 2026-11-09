"""
Keep the api's recent output in memory, so Ki (get_service_logs) and the web
app's log panel can read it without access to Docker.

install() tees stdout and stderr into a ring buffer and adds a logging handler
for uvicorn's error logger, whose handler was set up before this module ran.
"""
import collections
import logging
import sys

MAX_LINES = 3000
_LINES: collections.deque[str] = collections.deque(maxlen=MAX_LINES)


class _Tee:
    def __init__(self, stream):
        self._stream = stream
        self._partial = ""

    def write(self, text):
        written = self._stream.write(text)
        self._partial += text
        *lines, self._partial = self._partial.split("\n")
        _LINES.extend(lines)
        return written

    def flush(self):
        self._stream.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


class _BufferHandler(logging.Handler):
    def emit(self, record):
        try:
            _LINES.extend(self.format(record).split("\n"))
        except Exception:
            pass


def install() -> None:
    if isinstance(sys.stdout, _Tee):
        return
    sys.stdout = _Tee(sys.stdout)
    sys.stderr = _Tee(sys.stderr)
    handler = _BufferHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    logging.getLogger("uvicorn.error").addHandler(handler)


def tail(lines: int = 200) -> str:
    lines = max(1, min(lines, MAX_LINES))
    return "\n".join(list(_LINES)[-lines:])
