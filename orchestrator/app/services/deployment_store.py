"""
In-memory store for deployment records.

DELIBERATELY simple for the testing phase: a process-local dict.

This means: records vanish on restart, and this will NOT work correctly
if you ever run more than one orchestrator instance/worker (each gets
its own dict). Both are fine for testing against SST Lab from a single
process. The moment you need multi-instance or persistence across
restarts - move this to Redis or PostgreSQL, which is mentioned in
SOP 3's "memory lives in Skysecure infrastructure" principle anyway.
The DeploymentStore interface below is intentionally narrow so swapping
the backing store later doesn't ripple through the rest of the app.
"""
from threading import Lock, Event
from typing import Optional

from app.models.deployment import DeploymentRecord


class DeploymentStore:
    def __init__(self):
        self._records: dict[str, DeploymentRecord] = {}
        self._lock = Lock()
        # Threading events and results used to hand off the user's Power Platform
        # environment selection from the API handler back to the blocked background
        # deployment thread waiting in _select_pp_environment().
        self._env_selection_events: dict[str, Event] = {}
        self._env_selection_results: dict[str, str | dict] = {}

    def save(self, record: DeploymentRecord) -> None:
        with self._lock:
            self._records[record.deployment_id] = record

    def get(self, deployment_id: str) -> DeploymentRecord | None:
        with self._lock:
            return self._records.get(deployment_id)

    def list_all(self) -> list[DeploymentRecord]:
        with self._lock:
            return list(self._records.values())

    # -----------------------------------------------------------------------
    # Power Platform environment selection handshake
    # -----------------------------------------------------------------------

    def create_env_selection_event(self, deployment_id: str) -> None:
        """Create the threading.Event that the background deployment thread will
        wait on. Called by the deployment service before setting status to
        AWAITING_ENVIRONMENT_SELECTION."""
        with self._lock:
            self._env_selection_events[deployment_id] = Event()

    def set_env_selection(self, deployment_id: str, selection: str | dict) -> bool:
        """Called by the API endpoint when the user submits their environment
        selection or creation request. Unblocks the waiting deployment thread.
        Returns True if a waiting thread was found, False if no thread was waiting."""
        with self._lock:
            event = self._env_selection_events.get(deployment_id)
            if not event:
                return False
            self._env_selection_results[deployment_id] = selection
            event.set()
            return True

    def wait_for_env_selection(self, deployment_id: str, timeout: float = 600.0) -> Optional[str | dict]:
        """Blocks the calling (deployment background) thread until the user
        selects or creates an environment via the wizard or the timeout expires.
        Returns the selection payload (instance_url str or new_environment dict), or None on timeout."""
        event: Optional[Event] = None
        with self._lock:
            event = self._env_selection_events.get(deployment_id)
        if not event:
            return None
        completed = event.wait(timeout=timeout)
        with self._lock:
            result = self._env_selection_results.pop(deployment_id, None)
            self._env_selection_events.pop(deployment_id, None)
        return result if completed else None


# Single shared instance for the process - imported wherever needed.
store = DeploymentStore()
