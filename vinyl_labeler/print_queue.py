"""
Print queue: JSON-file-backed list of label jobs deferred for later (e.g.
the QL-570 is out of tape). Separate from store.py's catalogue -- that's
keyed one-entry-per-catalog_number, while the queue is an ordered list of
print JOBS (label_size/style/highlights_only -- the choices that actually
affect what gets printed).

A job stores a catalog_number REFERENCE, not a snapshot of the record --
the record itself is always saved to the catalogue at enqueue time (see
server.py's /queue endpoint) and dereferenced live at print time. This is
deliberate: a stored snapshot would go stale the moment someone edits the
record via the Library tab before it's actually printed, and silently
printing/showing the old data would be worse than the sync bug it was
meant to avoid. With a reference, an edit just works -- there's nothing
to keep in sync because there's only one copy of the data.

Same file-per-collection-scale assumption as store.py: a personal queue
is at most a handful to a few dozen deep, so a plain read-modify-write
JSON file is simpler than a database and stays inspectable.
"""
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path


def _load(queue_path: Path) -> list:
    if not queue_path.exists():
        return []
    text = queue_path.read_text().strip()
    return json.loads(text) if text else []


def _save(queue_path: Path, jobs: list) -> None:
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    queue_path.write_text(json.dumps(jobs, indent=2))


def list_jobs(queue_path: Path) -> list:
    return _load(queue_path)


def enqueue(queue_path: Path, catalog_number: str, label_size: str, style: dict,
            highlights_only: bool) -> dict:
    jobs = _load(queue_path)
    job = {
        "id": uuid.uuid4().hex,
        "catalog_number": catalog_number,
        "label_size": label_size,
        "style": style or {},
        "highlights_only": highlights_only,
        "queued_at": datetime.now(timezone.utc).isoformat(),
    }
    jobs.append(job)
    _save(queue_path, jobs)
    return job


def remove(queue_path: Path, job_id: str) -> bool:
    jobs = _load(queue_path)
    remaining = [j for j in jobs if j["id"] != job_id]
    if len(remaining) == len(jobs):
        return False
    _save(queue_path, remaining)
    return True
