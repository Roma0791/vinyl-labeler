"""
Stage 4: show what was found, per track, with its source and confidence --
and let you accept, edit, or manually fill in anything before it goes to
label rendering. Nothing gets printed silently just because a database
returned a number.
"""

SOURCE_LABELS = {
    "printed_on_label": "printed on record",
    "audio_analysis": "measured from audio",
    "getsongbpm": "GetSongBPM (unverified)",
    "none": "NOT FOUND",
}


def review_record(record: dict) -> dict:
    print("\n" + "=" * 60)
    print(f"{record.get('artist', '?')} – {record.get('release_title', '?')}")
    print(f"Catalog #: {record.get('catalog_number', '?')}  "
          f"(tracklist match: {record.get('tracklist_confidence', '?')})")
    print("=" * 60)

    for track in record.get("tracks", []):
        bpm = track.get("bpm")
        source = SOURCE_LABELS.get(track.get("bpm_source", "none"), "unknown")
        bpm_display = f"{bpm} bpm" if bpm is not None else "no bpm"
        print(f"\n  {track.get('position', '')}  {track.get('title', '')}")
        print(f"      -> {bpm_display}  [{source}]")
        if track.get("beatport_check_url"):
            print(f"      check: {track['beatport_check_url']}")

        choice = input("      accept / edit / skip? [a/e/s] (default a): ").strip().lower()
        if choice == "e":
            new_bpm = input("      enter correct BPM (blank to clear): ").strip()
            track["bpm"] = float(new_bpm) if new_bpm else None
            track["bpm_source"] = "manual" if new_bpm else "none"
        elif choice == "s":
            track["_skip_on_label"] = True

    return record
