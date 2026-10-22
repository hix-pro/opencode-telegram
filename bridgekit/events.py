"""Server-sent events client for ``/api/event``.

The stream is the low-latency signal for turn lifecycle and text deltas. The
server's contract for this endpoint is explicit: *a slow consumer overflows and
fails the stream, and events during disconnection are missed*. Two consequences
shape this module:

1. Frames are handed off to a queue the instant they are parsed. Nothing here
   ever blocks on a consumer.
2. Reconnection is automatic with capped backoff, because missed events are
   expected. :mod:`bridgekit.progress` treats the stream as an accelerator, not
   as the source of truth - the authoritative completion signal is still the
   idle marker polled from the session's messages.
"""

from __future__ import annotations

import json
import logging
import threading
import time

import requests

log = logging.getLogger("bridge.events")

BACKOFF_MIN = 1.0
BACKOFF_MAX = 30.0

# Below this lifetime, a clean stream return is not a success: the server
# accepted the connection and closed it without ever really streaming (EOF
# on connect, a hung backend behind a socket proxy). Count it as a failure,
# or a broken endpoint would sit in a 1-second reconnect loop with spotless
# health counters.
MIN_LIVELY_SECONDS = 30.0

# Event types the bridge reacts to.
EXECUTION_STARTED = "session.execution.started"
EXECUTION_SUCCEEDED = "session.execution.succeeded"
EXECUTION_FAILED = "session.execution.failed"
TEXT_STARTED = "session.text.started"
TEXT_DELTA = "session.text.delta"
TEXT_ENDED = "session.text.ended"
STEP_STARTED = "session.step.started"
STEP_ENDED = "session.step.ended"
USAGE_UPDATED = "session.usage.updated"
SESSION_RENAMED = "session.renamed"


class EventStream:
    """Background reader that fans events out to per-session listeners."""

    def __init__(
        self,
        base_url: str,
        auth: tuple[str, str],
        timeout: int = 30,
        connect_timeout: int = 15,
    ) -> None:
        self.base = base_url.rstrip("/")
        self.auth = auth
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        # A copy, so tests can relax the liveness rule per instance.
        self.min_lively = MIN_LIVELY_SECONDS

        self._listeners: dict[str, list] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        # Health, surfaced by /health.
        self.connected = False
        self.last_event_at = 0.0
        self.reconnects = 0
        self.frames = 0
        # Consecutive failures and the latest reason, so an outage is visible
        # without flooding the log: page 1 tells the story, page 2+ is detail.
        self.drops = 0
        self.last_error = ""

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="events", daemon=True)
        self._thread.start()
        log.info("Event stream starting")

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------- listeners

    def listen(self, session_id: str, callback) -> None:
        with self._lock:
            self._listeners.setdefault(session_id, []).append(callback)

    def unlisten(self, session_id: str) -> None:
        with self._lock:
            self._listeners.pop(session_id, None)

    # ---------------------------------------------------------------- worker

    def _run(self) -> None:
        backoff = BACKOFF_MIN
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self._consume()
            except requests.exceptions.RequestException as exc:
                self.connected = False
                self.reconnects += 1
                self._note_drop(exc)
            except Exception as exc:  # keep the daemon alive whatever happens
                self.connected = False
                self.reconnects += 1
                self._note_drop(exc, error=True)
            else:
                lifetime = time.monotonic() - started
                if lifetime < self.min_lively and not self._stop.is_set():
                    # Clean return, but the stream never really lived. This
                    # used to read as a success and reset the backoff to 1s
                    # against a broken endpoint, with spotless counters.
                    self.connected = False
                    self.reconnects += 1
                    self._note_drop(RuntimeError(f"stream closed after {lifetime:.1f}s"))
                else:
                    # A connection that held for a while: the outage is over.
                    self.drops = 0
                    self.last_error = ""
                    backoff = BACKOFF_MIN

            if self._stop.is_set():
                break
            time.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX)

    def _note_drop(self, exc: BaseException, error: bool = False) -> None:
        """Record one failed attempt without spamming the log.

        A server restart produces a refusal every attempt for minutes on
        end; one WARNING documents the outage and the recovery line closes
        it, so further consecutive drops go to DEBUG - they are counted in
        ``reconnects`` for /health, not lost, just quieter. Every tenth
        drop logs again so a really long outage stays visible.
        """
        self.drops += 1
        self.last_error = str(exc)
        level = logging.ERROR if error else logging.WARNING
        if self.drops > 1 and self.drops % 10 != 0:
            level = logging.DEBUG
        log.log(level, "Event stream dropped (%d in a row): %s", self.drops, exc)

    def _consume(self) -> None:
        # A context manager so the socket is released on stop, on a dropped
        # stream, and on any exception between here and the reader loop.
        with requests.get(
            f"{self.base}/api/event",
            auth=self.auth,
            stream=True,
            timeout=(self.connect_timeout, self.timeout),
            headers={"Accept": "text/event-stream"},
        ) as resp:
            resp.raise_for_status()
            # SSE is UTF-8 by specification, but requests guesses the charset
            # of a text/* response from headers and falls back to
            # ISO-8859-1 when the server sends no charset - which OpenCode
            # does not. Without this line, every non-ASCII delta would
            # arrive as mojibake.
            resp.encoding = "utf-8"
            self.connected = True
            extra = f" after {self.drops} failed attempt(s)" if self.drops else ""
            log.info("Event stream connected%s", extra)
            self.last_error = ""

            for raw in resp.iter_lines(decode_unicode=True):
                if self._stop.is_set():
                    return
                if not raw or raw.startswith(":") or not raw.startswith("data:"):
                    continue
                payload = raw[5:].strip()
                if not payload:
                    continue
                try:
                    event = json.loads(payload)
                except json.JSONDecodeError:
                    continue

                self._dispatch(event)

    def _dispatch(self, event: dict) -> None:
        self.frames += 1
        self.last_event_at = time.time()
        kind = event.get("type")
        session_id = ((event.get("data") or {}).get("sessionID")) or (
            event.get("durable") or {}
        ).get("aggregateID")

        if session_id:
            with self._lock:
                listeners = list(self._listeners.get(session_id, ()))
            for callback in listeners:
                try:
                    callback(kind, event)
                except Exception:
                    log.exception("Event listener failed for %s", session_id)
