"""
Local web server: runs on your Mac, serves a mobile-friendly page your
phone hits over Wi-Fi. All the actual work -- vision call, Discogs, BPM
lookups, printing -- happens here on the Mac; the phone is just the camera
and the review/print UI.

Run with:
    uvicorn vinyl_labeler.server:app --host 0.0.0.0 --port 8000

Then open http://<your-mac's-lan-ip>:8000 in Safari on your phone (same
Wi-Fi network as the Mac). Find the Mac's LAN IP with:
    ipconfig getifaddr en0

Uses a plain <input type=file capture> for the camera rather than the
getUserMedia live-camera API, specifically so this works over plain HTTP on
your local network -- getUserMedia requires a secure context (HTTPS or
localhost) and setting up a cert for a LAN IP is unnecessary friction here.
"""
import base64
import io
import json
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, Form, UploadFile, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config as config_mod
from . import identify as identify_mod
from . import discogs as discogs_mod
from . import bpm as bpm_mod
from . import label_render
from . import printer as printer_mod
from . import store
from . import print_queue as queue_mod
from brother_ql.devicedependent import label_type_specs

app = FastAPI()
cfg = config_mod.load_config()

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC_DIR / "index.html").read_text()


@app.get("/label-sizes")
def list_label_sizes():
    """Populates the label-type dropdown from whatever this brother_ql
    install actually supports, so it never drifts out of sync."""
    out = []
    for code, spec in label_type_specs.items():
        out.append({
            "code": code,
            "name": spec["name"],
            "kind": spec["kind"].name,  # "ENDLESS" | "DIE_CUT" | "ROUND_DIE_CUT"
        })
    return out


ALLOWED_VISION_MODELS = {"claude-haiku-4-5", "claude-sonnet-5"}


@app.post("/identify")
async def identify_photo(photo: UploadFile = File(...), model: str = Form(None)):
    if not cfg.anthropic_api_key:
        raise HTTPException(400, "Anthropic API key not set -- run `vinyl-label set-key anthropic`")

    vision_model = model if model in ALLOWED_VISION_MODELS else cfg.vision_model

    suffix = Path(photo.filename or "photo.jpg").suffix or ".jpg"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(await photo.read())
        tmp_path = Path(tmp.name)

    try:
        record = identify_mod.identify_from_photo(
            tmp_path, cfg.anthropic_api_key, vision_model, cfg.anthropic_workspace_id,
        )
    finally:
        tmp_path.unlink(missing_ok=True)

    for t in record.get("tracks", []):
        t.setdefault("highlight", False)
    return record


@app.post("/analyze-audio")
async def analyze_audio(audio: UploadFile = File(...)):
    """Tempo estimate from an uploaded audio sample (e.g. a phone recording
    off the turntable) -- the audio_analysis BPM tier, triggered per-track
    from the review screen rather than automatically, since it needs you to
    actually supply a recording."""
    suffix = Path(audio.filename or "sample.m4a").suffix or ".m4a"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(await audio.read())
        tmp_path = Path(tmp.name)
    try:
        result = bpm_mod.from_audio_sample(str(tmp_path))
    finally:
        tmp_path.unlink(missing_ok=True)
    if result is None:
        return {"bpm": None}
    return {"bpm": result["bpm"]}


class EnrichRequest(BaseModel):
    record: dict


@app.post("/enrich")
def enrich(req: EnrichRequest):
    record = req.record
    client = discogs_mod.DiscogsClient(cfg.discogs_token, cfg.discogs_user_agent)
    match = discogs_mod.confirm_tracklist(
        client, record.get("catalog_number"), record.get("artist"),
        record.get("release_title"),
    )
    record["tracklist_confidence"] = match["confidence"]
    record["discogs_matched"] = match["matched"]

    if match["matched"]:
        # Discogs' own artist/title, once matched, is worth trusting over a
        # vision read that may have caught the catalog number but missed the
        # artist/title text (small print, multi-artist-per-side layouts).
        record["artist"] = match["artist"] or record.get("artist")
        record["release_title"] = match["release_title"] or record.get("release_title")

        printed_bpm_by_pos = {
            t.get("position"): t for t in record.get("tracks", []) if t.get("printed_bpm")
        }
        record["tracks"] = match["tracklist"]
        for t in record["tracks"]:
            t["highlight"] = False
            printed = printed_bpm_by_pos.get(t.get("position"))
            if printed:
                t["printed_bpm"] = printed["printed_bpm"]

    for t in record.get("tracks", []):
        result = bpm_mod.resolve_bpm(t, record.get("artist", ""), cfg.getsongbpm_api_key)
        t["bpm"] = result["bpm"]
        t["bpm_source"] = result["source"]
        t["beatport_check_url"] = result["beatport_check_url"]

    return record


class RenderRequest(BaseModel):
    record: dict
    label_size: str = "62"
    style: dict = {}
    highlights_only: bool = False


@app.post("/render")
def render(req: RenderRequest):
    record = req.record
    if req.highlights_only:
        record = {**record, "tracks": label_render.highlights_only(record.get("tracks", []))}
    img = label_render.render_label(record, req.label_size, req.style)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return {"png_base64": b64, "width": img.width, "height": img.height}


class PrintRequest(BaseModel):
    record: dict
    label_size: str = "62"
    style: dict = {}
    dry_run: bool = False
    highlights_only: bool = False


def _with_placeholder_catalog_number(record: dict) -> dict:
    """Catalog number is mandatory for anything that will actually be
    printed -- it's the catalogue's key, and without it a reprint later
    would mean redoing the identify/Discogs/BPM pipeline from scratch
    instead of just reprinting. For a promo/white-label with no printed
    catalog number, assign a deterministic placeholder rather than
    blocking."""
    if record.get("catalog_number"):
        return record
    return {
        **record,
        "catalog_number": store.placeholder_catalog_number(
            record.get("artist", ""), record.get("release_title", "")
        ),
    }


def _render_for_print(record: dict, label_size: str, style: dict, highlights_only: bool):
    # highlights_only affects what's rendered onto the label only -- the
    # catalogue always stores the full tracklist regardless of this toggle.
    label_record = record
    if highlights_only:
        label_record = {**record, "tracks": label_render.highlights_only(record.get("tracks", []))}
    return label_render.render_label(label_record, label_size, style)


def _print_job(record: dict, label_size: str, style: dict, highlights_only: bool) -> dict:
    """Renders, sends to the physical printer, and logs to the catalogue.
    Raises on printer failure. Shared by /print (real prints) and the
    print-queue endpoints below -- `record` must already have a
    catalog_number (see _with_placeholder_catalog_number)."""
    img = _render_for_print(record, label_size, style, highlights_only)
    printer_mod.print_label(
        img, cfg.printer_model, label_size, cfg.printer_identifier,
        backend=cfg.printer_backend, dry_run=False,
    )
    existing = store.get(cfg.catalogue_path, record.get("catalog_number"))
    entry = store.upsert(cfg.catalogue_path, record)
    return {
        "printed": True,
        "catalog_number": record.get("catalog_number"),
        "already_logged_before": existing is not None,
        "print_count": entry["print_count"],
    }


@app.post("/print")
def print_endpoint(req: PrintRequest):
    if req.dry_run:
        img = _render_for_print(req.record, req.label_size, req.style, req.highlights_only)
        try:
            printer_mod.print_label(
                img, cfg.printer_model, req.label_size, cfg.printer_identifier,
                backend=cfg.printer_backend, dry_run=True,
                dry_run_path=Path(tempfile.gettempdir()) / "vinyl_label_dryrun.png",
            )
        except Exception as e:
            raise HTTPException(500, f"Print failed: {e}")
        existing = store.get(cfg.catalogue_path, req.record.get("catalog_number"))
        return {
            "printed": False,
            "catalog_number": req.record.get("catalog_number"),
            "already_logged_before": existing is not None,
            "print_count": (existing or {}).get("print_count", 0),
        }

    record = _with_placeholder_catalog_number(req.record)
    try:
        return _print_job(record, req.label_size, req.style, req.highlights_only)
    except Exception as e:
        raise HTTPException(500, f"Print failed: {e}")


class QueueRequest(BaseModel):
    record: dict
    label_size: str = "62"
    style: dict = {}
    highlights_only: bool = False


@app.post("/queue")
def enqueue_endpoint(req: QueueRequest):
    record = _with_placeholder_catalog_number(req.record)
    return queue_mod.enqueue(cfg.print_queue_path, record, req.label_size, req.style, req.highlights_only)


@app.get("/queue")
def list_queue():
    return queue_mod.list_jobs(cfg.print_queue_path)


@app.delete("/queue/{job_id}")
def delete_queue_job(job_id: str):
    if not queue_mod.remove(cfg.print_queue_path, job_id):
        raise HTTPException(404, "Queue job not found")
    return {"removed": True}


@app.post("/queue/{job_id}/print")
def print_queue_job(job_id: str):
    job = next((j for j in queue_mod.list_jobs(cfg.print_queue_path) if j["id"] == job_id), None)
    if not job:
        raise HTTPException(404, "Queue job not found")
    try:
        result = _print_job(job["record"], job["label_size"], job.get("style", {}), job.get("highlights_only", False))
    except Exception as e:
        raise HTTPException(500, f"Print failed: {e}")
    queue_mod.remove(cfg.print_queue_path, job_id)
    return result


@app.post("/queue/print-all")
def print_all_queue():
    """Prints every queued job in order. Stops at the first failure (e.g.
    the printer runs out of tape again) so the rest stay queued rather
    than being attempted and failing one by one."""
    results = []
    for job in queue_mod.list_jobs(cfg.print_queue_path):
        try:
            result = _print_job(job["record"], job["label_size"], job.get("style", {}), job.get("highlights_only", False))
            queue_mod.remove(cfg.print_queue_path, job["id"])
            results.append({"id": job["id"], "ok": True, **result})
        except Exception as e:
            results.append({"id": job["id"], "ok": False, "error": str(e)})
            break
    return {"results": results}


@app.get("/catalogue")
def list_catalogue():
    """Summary rows for the Library table -- kept light (no full tracklists)
    since this is meant to render as a scrollable table, not a detail view."""
    all_records = store.load_all(cfg.catalogue_path)
    rows = []
    for catno, rec in all_records.items():
        rows.append({
            "catalog_number": catno,
            "artist": rec.get("artist", ""),
            "release_title": rec.get("release_title", ""),
            "track_count": len(rec.get("tracks", [])),
            "last_processed_at": rec.get("last_processed_at", ""),
            "print_count": rec.get("print_count", 0),
        })
    rows.sort(key=lambda r: r["last_processed_at"], reverse=True)
    return rows


@app.get("/catalogue/lookup")
def lookup_catalogue_record(catalog_number: str):
    """Query param rather than a path param -- catalog numbers routinely
    contain slashes and spaces, which don't survive as URL path segments."""
    rec = store.get(cfg.catalogue_path, catalog_number)
    if rec is None:
        raise HTTPException(404, "No record with that catalog number.")
    return rec


@app.delete("/catalogue/lookup")
def delete_catalogue_record(catalog_number: str):
    deleted = store.delete(cfg.catalogue_path, catalog_number)
    if not deleted:
        raise HTTPException(404, "No record with that catalog number.")
    return {"deleted": True}
