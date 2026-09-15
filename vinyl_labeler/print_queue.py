"""
Print queue: JSON-file-backed list of label jobs deferred for later (e.g.
the QL-570 is out of tape). Separate from store.py's catalogue -- that's
keyed one-entry-per-catalog_number, while the queue is an ordered list of
print JOBS (a job carries the label_size/style/highlights_only choices
alongside the record, since those are what actually gets printed).

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


def enqueue(queue_path: Path, record: dict, label_size: str, style: dict,
            highlights_only: bool) -> dict:
    jobs = _load(queue_path)
    job = {
        "id": uuid.uuid4().hex,
        "record": record,
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
