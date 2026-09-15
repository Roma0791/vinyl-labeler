"""
Catalogue store, keyed by catalog_number. This backs the "Library" table in
the web UI -- reprinting after an edit updates the existing entry rather
than appending a duplicate, so the table always shows current state, with
a print_count/timestamps trail for history.

Single JSON file, not a database -- a personal vinyl collection is a few
hundred to a few thousand entries at most, well within what a plain
read-modify-write JSON file handles fine, and it stays human-readable/
diffable if you ever want to look at it directly.
"""
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path


def placeholder_catalog_number(artist: str, release_title: str) -> str:
    """Deterministic placeholder for a record with no real catalog number
    (promo/white-label) -- slugged from artist+title so scanning the same
    record again later lands on this same catalogue entry instead of a
    fresh duplicate. Falls back to a random suffix only if both are blank
    (nothing to slug)."""
    slug = re.sub(r"[^A-Z0-9]+", "-", f"{artist} {release_title}".upper()).strip("-")
    return f"NOCAT-{slug}" if slug else f"NOCAT-{uuid.uuid4().hex[:8].upper()}"


def load_all(catalogue_path: Path) -> dict:
    if not catalogue_path.exists():
        return {}
    text = catalogue_path.read_text().strip()
    return json.loads(text) if text else {}


def get(catalogue_path: Path, catalog_number: str) -> dict | None:
    if not catalog_number:
        return None
    return load_all(catalogue_path).get(catalog_number)


def upsert(catalogue_path: Path, record: dict, count_as_print: bool = True) -> dict:
    """Insert or update by catalog_number. Returns the stored entry
    (record plus first_processed_at/last_processed_at/print_count).

    count_as_print=False saves/updates the data (e.g. so it's editable via
    the Library tab) without bumping print_count or last_processed_at --
    used when queuing a print for later, since queuing isn't printing."""
    catno = record.get("catalog_number")
    if not catno:
        raise ValueError("Record has no catalog_number -- can't be saved to the catalogue.")

    catalogue_path.parent.mkdir(parents=True, exist_ok=True)
    all_records = load_all(catalogue_path)
    existing = all_records.get(catno)
    now = datetime.now(timezone.utc).isoformat()

    entry = dict(record)
    entry["first_processed_at"] = existing["first_processed_at"] if existing else now
    if count_as_print:
        entry["last_processed_at"] = now
        entry["print_count"] = existing.get("print_count", 0) + 1 if existing else 1
    else:
        entry["last_processed_at"] = existing.get("last_processed_at") if existing else None
        entry["print_count"] = existing.get("print_count", 0) if existing else 0

    all_records[catno] = entry
    catalogue_path.write_text(json.dumps(all_records, indent=2))
    return entry


def delete(catalogue_path: Path, catalog_number: str) -> bool:
    all_records = load_all(catalogue_path)
    if catalog_number not in all_records:
        return False
    del all_records[catalog_number]
    catalogue_path.write_text(json.dumps(all_records, indent=2))
    return True
