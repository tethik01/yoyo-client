"""The clock that makes memory continuous.

One background thread inside the API process. It does no work itself — it decides *when*, and
hands the actual sweep to the job runner, so every automatic run appears in the Jobs list with
its log, its duration and its failures. Work that happens invisibly is work you cannot debug,
and a memory system quietly not running is indistinguishable from one that has nothing to say.

Two triggers, for two different failure modes:

* **Idle.** A conversation quiet for `idle_minutes` gets swept. This is the one that makes
  memory feel current — you stop talking, and a few minutes later the claims are waiting.
* **Nightly.** A catch-up at a fixed hour. The idle trigger only fires while the server is
  running; the nightly pass covers the evenings the laptop was shut, the hours MyAIServer was
  unreachable, and any slice a failed extraction left behind.

A thread rather than an OS scheduler: Windows Task Scheduler and cron would both need Yoyo
installed as a service, and neither can see whether the API is already mid-sweep. This starts
and stops with `yoyo serve`, which is exactly the lifetime it should have.

**It never runs two sweeps at once** — the job runner's single slot already guarantees that,
and a second concurrent sweep would race on the same watermarks.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime

log = logging.getLogger(__name__)

#: Checked this often regardless of config, so a paused scheduler still notices when the
#: config changes. Cheap: one SQLite query.
TICK_SECONDS = 30


class Scheduler:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_nightly: str | None = None
        self.last_tick: str | None = None
        self.last_reason: str | None = None

    # -------------------------------------------------------------- lifecycle ---

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="yoyo-scheduler", daemon=True)
        self._thread.start()
        log.info("memory scheduler started")

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        log.info("memory scheduler stopped")

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # ------------------------------------------------------------------ loop ---

    def _loop(self) -> None:
        # One sweep at startup, before the first tick. The most common reason memory is
        # stale is that the laptop was closed, and waiting a full poll interval to notice
        # would make every morning start behind.
        self._safely(self._catch_up, "startup catch-up")
        while not self._stop.wait(TICK_SECONDS):
            self._safely(self.tick, "tick")

    def _safely(self, fn, what: str) -> None:  # noqa: ANN001
        try:
            fn()
        except Exception:  # noqa: BLE001 - a scheduler that dies on one bad tick is worse
            log.exception("memory scheduler: %s failed", what)

    def tick(self, now: datetime | None = None) -> str | None:
        """One decision. Returns the reason a sweep was queued, or None.

        Pure enough to test: pass `now` and assert on the reason rather than on wall-clock
        behaviour, which would make the test slow and flaky in equal measure.
        """
        from .memory import pipeline

        cfg = pipeline.load_config()
        now = now or datetime.now()
        self.last_tick = now.isoformat(timespec="seconds")

        if not cfg.enabled:
            return None

        # Nightly first: it is the catch-up, and running it when there is also idle work to
        # do would otherwise queue two sweeps in the same minute for the same conversations.
        stamp = f"{now.date()}"
        if now.hour == cfg.nightly_hour and self._last_nightly != stamp:
            self._last_nightly = stamp
            return self._queue("nightly catch-up")

        if pipeline.candidates(idle_minutes=cfg.idle_minutes, limit=1):
            return self._queue("a conversation went idle")
        return None

    def _catch_up(self) -> str | None:
        from .memory import pipeline

        cfg = pipeline.load_config()
        if not cfg.enabled:
            return None
        if pipeline.candidates(idle_minutes=cfg.idle_minutes, limit=1):
            return self._queue("server started with unread conversations")
        return None

    def _queue(self, reason: str) -> str:
        from . import jobs

        # If a sweep is already queued or running, do not stack another. The runner would
        # serialise them anyway, but a queue full of identical pending sweeps makes the Jobs
        # list useless for seeing what is actually happening.
        for row in jobs.recent(limit=5, kind="memory-sweep"):
            if row["status"] in {"queued", "running"}:
                return reason

        job_id = jobs.create("memory-sweep", {"reason": reason})
        jobs.start(job_id)
        self.last_reason = reason
        log.info("queued memory sweep (%s) as job %s", reason, job_id)
        return reason


#: One per process, started by the API's lifespan.
scheduler = Scheduler()
