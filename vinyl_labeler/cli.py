import json
import os
from pathlib import Path

import click
import keyring

from . import config as config_mod
from . import identify as identify_mod
from . import discogs as discogs_mod
from . import bpm as bpm_mod
from . import review as review_mod
from . import label_render
from . import printer as printer_mod
from . import store


@click.group()
@click.option("--config", "config_path", type=click.Path(),
              default=str(config_mod.DEFAULT_CONFIG_PATH))
@click.pass_context
def cli(ctx, config_path):
    ctx.obj = config_mod.load_config(Path(config_path))


# (min length, required prefix or None) -- used only to catch an obviously
# wrong paste (wrong field copied, truncated clipboard) right after entry.
_KEY_SHAPE = {
    "anthropic": (40, "sk-ant-"),
    "discogs": (10, None),
    "getsongbpm": (10, None),
}


@cli.command(name="set-key")
@click.argument("service", type=click.Choice(sorted(config_mod.SECRETS)))
def set_key(service):
    """Store an API key/token in the macOS Keychain (e.g. `vinyl-label set-key anthropic`).

    Never written to disk -- the prompt doesn't echo and nothing touches
    config.yaml or shell history. Since hidden input gives zero feedback
    (nothing prints as you type or paste, by design -- same as a `sudo`
    password prompt), this prints a short fingerprint (length + a few
    characters from each end, never the full value) right after so a wrong
    paste is obvious immediately instead of surfacing later as a 401.
    """
    account, _env_var = config_mod.SECRETS[service]
    value = click.prompt(f"Paste your {service} API key/token", hide_input=True).strip()
    if not value:
        raise click.ClickException("No value entered -- nothing stored.")

    min_len, prefix = _KEY_SHAPE.get(service, (1, None))
    fingerprint = f"{len(value)} chars, {value[:4]}...{value[-4:]}" if len(value) > 8 else f"{len(value)} chars"
    if len(value) < min_len or (prefix and not value.startswith(prefix)):
        expected = f"starting with '{prefix}', " if prefix else ""
        click.echo(f"Warning: that looks wrong for a {service} key ({expected}usually {min_len}+ "
                   f"characters) -- got {fingerprint}. Storing it anyway, but double-check the source.")
    else:
        click.echo(f"Looks right: {fingerprint}")

    keyring.set_password(config_mod.KEYRING_SERVICE, account, value)
    click.echo(f"Stored in macOS Keychain (look for '{config_mod.KEYRING_SERVICE}' in Keychain Access if you ever need to remove it).")


@cli.command(name="key-status")
def key_status():
    """Show which API keys currently resolve, and from where -- values are never printed."""
    for service, (account, env_var) in config_mod.SECRETS.items():
        if os.environ.get(env_var):
            source = f"env var {env_var}"
        elif keyring.get_password(config_mod.KEYRING_SERVICE, account):
            source = "Keychain"
        else:
            source = None
        click.echo(f"{service:12} {'set (' + source + ')' if source else 'NOT SET'}")


@cli.command()
@click.pass_obj
def discover_printer(cfg):
    """List connected label printers -- use this to find printer_identifier."""
    try:
        found = printer_mod.discover_printers(backend=cfg.printer_backend)
    except Exception as e:
        raise click.ClickException(
            f"{e}\nIf this is 'No backend available': the pyusb backend needs the "
            "libusb C library -- `brew install libusb` (a pip install alone isn't enough)."
        )
    if not found:
        click.echo("No printers found. Check the USB cable/port and that the "
                    "printer is powered on. (Some QL models have an 'Editor Lite' "
                    "standalone mode that needs disabling for USB printing -- the "
                    "QL-570 doesn't appear to have this feature.)")
    for f in found:
        click.echo(f)


@cli.command()
@click.argument("photo", type=click.Path(exists=True))
@click.option("--out", type=click.Path(), default="record.json",
              help="Where to save the working JSON for the later stages.")
@click.pass_obj
def identify(cfg, photo, out):
    """Stage 1: read a label/cover photo."""
    result = identify_mod.identify_from_photo(
        Path(photo), cfg.anthropic_api_key, cfg.vision_model, cfg.anthropic_workspace_id,
    )
    Path(out).write_text(json.dumps(result, indent=2))
    click.echo(f"Draft extraction written to {out} -- legibility: {result.get('legibility')}")
    if result.get("notes"):
        click.echo(f"Notes: {result['notes']}")


@cli.command()
@click.argument("working_json", type=click.Path(exists=True))
@click.pass_obj
def enrich(cfg, working_json):
    """Stage 2+3: confirm tracklist via Discogs, then resolve BPM per track."""
    record = json.loads(Path(working_json).read_text())

    client = discogs_mod.DiscogsClient(cfg.discogs_token, cfg.discogs_user_agent)
    match = discogs_mod.confirm_tracklist(
        client, record.get("catalog_number"), record.get("artist"),
        record.get("release_title"),
    )
    record["tracklist_confidence"] = match["confidence"]

    if match["matched"]:
        # Discogs' own artist/title/tracklist are the source of truth once
        # matched -- a vision read can catch the catalog number printed
        # clearly but miss the artist/title text (small print, multi-artist
        # -per-side layouts, worn ink). Carry over any printed_bpm we
        # already read off the photo by position.
        record["artist"] = match["artist"] or record.get("artist")
        record["release_title"] = match["release_title"] or record.get("release_title")
        printed_bpm_by_pos = {
            t.get("position"): t for t in record.get("tracks", []) if t.get("printed_bpm")
        }
        record["tracks"] = match["tracklist"]
        for t in record["tracks"]:
            printed = printed_bpm_by_pos.get(t.get("position"))
            if printed:
                t["printed_bpm"] = printed["printed_bpm"]
        discogs_mod.distribute_styles_to_tracks(record["tracks"], match["styles"])
    # else: keep whatever the photo extraction gave us, confidence stays "none"

    for t in record.get("tracks", []):
        result = bpm_mod.resolve_bpm(
            t, record.get("artist", ""), cfg.getsongbpm_api_key,
        )
        t["bpm"] = result["bpm"]
        t["bpm_source"] = result["source"]
        t["beatport_check_url"] = result["beatport_check_url"]

    Path(working_json).write_text(json.dumps(record, indent=2))
    found = sum(1 for t in record["tracks"] if t.get("bpm") is not None)
    click.echo(f"Tracklist confidence: {match['confidence']}. "
               f"BPM found for {found}/{len(record['tracks'])} tracks.")


@cli.command()
@click.argument("working_json", type=click.Path(exists=True))
@click.pass_obj
def review(cfg, working_json):
    """Stage 4: interactive accept/edit/skip before printing."""
    record = json.loads(Path(working_json).read_text())
    record = review_mod.review_record(record)
    Path(working_json).write_text(json.dumps(record, indent=2))


@cli.command(name="print")
@click.argument("working_json", type=click.Path(exists=True))
@click.option("--dry-run", is_flag=True, help="Save a PNG instead of printing.")
@click.option("--preview-path", type=click.Path(), default="label_preview.png")
@click.option("--highlights-only", is_flag=True,
              help="Only put highlight=True tracks on the label. The catalogue still stores the full tracklist.")
@click.pass_obj
def print_it(cfg, working_json, dry_run, preview_path, highlights_only):
    """Stage 5+6: render the label and send it to the QL-570."""
    record = json.loads(Path(working_json).read_text())
    record["tracks"] = [t for t in record.get("tracks", []) if not t.get("_skip_on_label")]

    # Catalog number is mandatory for a real print -- it's the catalogue's
    # key, and without it a reprint later would mean redoing the identify/
    # Discogs/BPM pipeline from scratch instead of just reprinting. For a
    # promo/white-label with no printed catalog number, assign a
    # deterministic placeholder rather than blocking.
    if not dry_run and not record.get("catalog_number"):
        record["catalog_number"] = store.placeholder_catalog_number(
            record.get("artist", ""), record.get("release_title", "")
        )
        Path(working_json).write_text(json.dumps(record, indent=2))
        click.echo(f"No catalog number -- assigned placeholder: {record['catalog_number']}")

    label_tracks = label_render.highlights_only(record["tracks"]) if highlights_only else record["tracks"]
    image = label_render.render_label({**record, "tracks": label_tracks}, cfg.label_size)
    printer_mod.print_label(
        image, cfg.printer_model, cfg.label_size, cfg.printer_identifier,
        backend=cfg.printer_backend, dry_run=dry_run,
        dry_run_path=Path(preview_path),
    )

    existing = store.get(cfg.catalogue_path, record.get("catalog_number"))
    if existing:
        click.echo(f"Note: catalog #{record.get('catalog_number')} was already "
                    f"logged before (printed {existing.get('print_count', 0)} time(s) "
                    f"prior, last on {existing.get('last_processed_at')}).")
    if not dry_run:
        store.upsert(cfg.catalogue_path, record)


@cli.command()
@click.argument("photo", type=click.Path(exists=True))
@click.option("--dry-run", is_flag=True)
@click.pass_context
def process(ctx, photo, dry_run):
    """Convenience: run identify -> enrich -> review -> print in sequence."""
    working = "record.json"
    ctx.invoke(identify, photo=photo, out=working)
    ctx.invoke(enrich, working_json=working)
    ctx.invoke(review, working_json=working)
    ctx.invoke(print_it, working_json=working, dry_run=dry_run,
               preview_path="label_preview.png")


if __name__ == "__main__":
    cli()
