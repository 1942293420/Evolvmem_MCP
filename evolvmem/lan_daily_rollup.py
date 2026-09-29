"""Daily per-project rollup maintenance inside the LAN capture worker.

Frozen rules (pinned by ``tests/test_lan_daily_rollup.py``):

- The LAN runtime's capture worker calls :meth:`LanDailyRollup.check` only
  after it has processed uploads and session extractions for that tick, so
  durable capture work always wins over summary maintenance.
- One local calendar day is attempted at most once, at or after the daily
  time (default 04:17 local).  A process start after that time catches the
  same day up; a completed day is never repeated.
- A failed attempt is recorded but never marks the day complete, and the next
  attempt waits at least one hour (also in memory, so a failing state write
  cannot turn the worker into a five-second retry loop).
- Every user batch runs under the same ``adapter.lock`` the HTTP dispatch
  uses, so no two writers touch one namespace at once.  The model call itself
  releases that lock (the ``lan_tools.process_pending`` unlocked-LLM
  convention) and its response is discarded when the namespace memory
  revision moved while the model worked; the day then stays failed and
  retries an hour later.
- Only the owner personal namespace (``jiangli``) is rolled up. The other
  user and curated public library are neither read for private summaries
  nor written here.
- Project discovery stays inside the generator: the existing
  ``ContextService.rollup_projects`` API runs
  ``ProjectRollupGenerator.rollup_all`` and its own source enumeration.  An
  unchanged source set short-circuits inside the generator, so no model call
  is made and the day is still recorded as checked.
- Credentials are re-read every round from the owner namespace's configured
  ``llm_credentials.json``; a missing or invalid file fails the day with a
  stable reason code and never renders the endpoint or key.
- One private JSON state file records the local date, the last check, and
  per-user per-project status/reason/context_id/covered_through only: no
  layer text, prompts, URLs, or credentials.  It is written atomically
  through a 0600 temporary file plus :func:`os.replace`.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import tempfile
import threading
import time

logger = logging.getLogger(__name__)

STATE_VERSION = 1
DEFAULT_HOUR = 4
DEFAULT_MINUTE = 17
DEFAULT_RETRY_SECONDS = 3600.0
STATE_FILENAME = "lan_daily_rollup.json"

_USERS = ("jiangli",)
_COMPLETED = "completed"
_FAILED = "failed"
_FAILED_REASON = "rollup_failed"


class LanDailyRollup:
    """One daily, lock-aware rollup check for the owner personal namespace.

    ``now`` is an optional ``callable() -> float`` epoch-seconds clock used by
    tests; production uses the wall clock, and the local calendar date/time
    come from :func:`time.localtime` exactly like the documented 04:17 local
    schedule.
    """

    def __init__(
        self,
        adapter,
        *,
        now=None,
        state_path=None,
        hour: int = DEFAULT_HOUR,
        minute: int = DEFAULT_MINUTE,
        retry_seconds: float = DEFAULT_RETRY_SECONDS,
    ) -> None:
        if now is not None and not callable(now):
            raise TypeError("now must be a callable returning epoch seconds")
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError("daily time must be a valid local hour and minute")
        if retry_seconds < 0:
            raise ValueError("retry_seconds must not be negative")
        self.adapter = adapter
        self._now = now if now is not None else time.time
        self._hour = hour
        self._minute = minute
        self._retry_seconds = float(retry_seconds)
        data_dir = Path(adapter.runtime.settings.data_dir)
        self._state_path = (
            Path(state_path) if state_path is not None else data_dir / STATE_FILENAME
        )
        self._check_lock = threading.Lock()
        self._last_attempt_at: float | None = None

    @property
    def runtime(self):
        return self.adapter.runtime

    @property
    def state_path(self) -> Path:
        return self._state_path

    # ---- schedule ----

    def check(self) -> bool:
        """Run today's rollup once it is due; True when an attempt was made."""
        if not self._check_lock.acquire(blocking=False):
            return False
        try:
            return self._check_due()
        finally:
            self._check_lock.release()

    def _check_due(self) -> bool:
        now = float(self._now())
        local = time.localtime(now)
        today = time.strftime("%Y-%m-%d", local)
        state = self._read_state()
        if state.get("date") == today and state.get("status") == _COMPLETED:
            return False
        if (local.tm_hour, local.tm_min) < (self._hour, self._minute):
            return False
        last = self._last_attempt_time(state)
        if last is not None and now - last < self._retry_seconds:
            return False
        self._last_attempt_at = now
        self._run(today, now)
        return True

    def _last_attempt_time(self, state) -> float | None:
        previous = state.get("last_check_at")
        if type(previous) not in (int, float):
            previous = None
        if self._last_attempt_at is not None and (
            previous is None or self._last_attempt_at > previous
        ):
            previous = self._last_attempt_at
        return float(previous) if previous is not None else None

    # ---- one attempt ----

    def _run(self, today: str, now: float) -> dict:
        payload = {
            "version": STATE_VERSION,
            "date": today,
            "last_check_at": now,
            "last_check_local": time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(now)
            ),
            "status": _FAILED,
            "reason": "provider_unavailable",
            "users": {},
        }
        credentials = self._load_credentials()
        if credentials is not None:
            llm = self._llm_callable(credentials)
            for user in _USERS:
                payload["users"][user] = self._run_user(user, llm)
            failed = any(
                entry["status"] != _COMPLETED
                for entry in payload["users"].values()
            )
            payload["status"] = _FAILED if failed else _COMPLETED
            payload["reason"] = _FAILED_REASON if failed else ""
        try:
            self._write_state(payload)
        except Exception:
            # The in-memory retry window still prevents a tight retry loop.
            logger.warning("daily project rollup state could not be persisted")
        return payload

    def _run_user(self, user: str, llm) -> dict:
        entry = {"status": _FAILED, "reason": _FAILED_REASON, "projects": {}}
        try:
            server = self.runtime.server_for(user)
        except Exception:
            entry["reason"] = "namespace_unavailable"
            return entry
        unlocked = self._unlocked_llm(user, llm)
        try:
            with self.adapter.lock:
                reports = server.context_service.rollup_projects(llm=unlocked)
        except Exception:
            # One project's exception aborts that user's batch; the day stays
            # incomplete so the hourly retry can pick the batch up again.
            return entry
        projects: dict[str, dict] = {}
        failed = False
        for report in reports:
            projects[report.project] = {
                "status": report.status,
                "reason": report.reason,
                "context_id": report.context_id,
                "covered_through": report.covered_through,
            }
            failed = failed or report.status == _FAILED
        entry["projects"] = projects
        entry["status"] = _FAILED if failed else _COMPLETED
        entry["reason"] = _FAILED_REASON if failed else ""
        return entry

    def _unlocked_llm(self, user: str, llm):
        """Release the dispatch lock around the model call, as process_pending does."""
        adapter = self.adapter
        lock = adapter.lock

        def call(prompt):
            revision = adapter._memory_revision(user)
            lock.release()
            try:
                response = llm(prompt)
            finally:
                lock.acquire()
            # Never summarize a source snapshot that moved while the model
            # worked; returning None records a retryable failure.
            return response if adapter._memory_revision(user) == revision else None

        return call

    # ---- credentials ----

    def _load_credentials(self):
        from evolvmem import kimi_hooks

        path = Path(self.runtime.settings.owner_data_dir) / "llm_credentials.json"
        return kimi_hooks._load_llm_config(log_errors=False, config_path=path)

    @staticmethod
    def _llm_callable(credentials):
        from evolvmem import kimi_hooks

        return kimi_hooks._llm_callable(credentials)

    # ---- private state file ----

    def _read_state(self) -> dict:
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _write_state(self, payload: dict) -> None:
        directory = self._state_path.parent
        directory.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=self._state_path.name + ".",
            suffix=".tmp",
            dir=directory,
            delete=False,
        )
        temporary = Path(handle.name)
        try:
            with handle:
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            os.replace(temporary, self._state_path)
        except BaseException:
            try:
                temporary.unlink()
            except OSError:
                pass
            raise
