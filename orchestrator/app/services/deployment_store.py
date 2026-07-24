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
from threading import Lock

from app.models.deployment import DeploymentRecord


class DeploymentStore:
    def __init__(self):
        self._records: dict[str, DeploymentRecord] = {}
        self._lock = Lock()

    def save(self, record: DeploymentRecord) -> None:
        with self._lock:
            self._records[record.deployment_id] = record

    def get(self, deployment_id: str) -> DeploymentRecord | None:
        with self._lock:
            return self._records.get(deployment_id)

    def list_all(self) -> list[DeploymentRecord]:
        with self._lock:
            return list(self._records.values())


# Single shared instance for the process - imported wherever needed.
store = DeploymentStore()
