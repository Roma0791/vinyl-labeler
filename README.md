# vinyl-labeler

Photo of a record label/cover → confirmed tracklist → BPM (tiered by
reliability) → printed label on a Brother QL-570.

Built and tested end-to-end against a real QL-570 over USB, a live Discogs
account, and an actual vinyl collection. Follow the setup order below so
any problem surfaces at the cheapest possible step.

## 1. Install

```bash
brew install libusb          # needed for pyusb to talk to the QL-570 over USB
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
pip install -e .             # installs the `vinyl-label` command
```

## 2. Get your API keys

- **Anthropic**: console.anthropic.com → API Keys. This is a separate key
  from your claude.ai login — the script authenticates independently.
- **Discogs**: discogs.com/settings/developers → generate a personal access
  token. Free.
- **GetSongBPM**: getsongbpm.com/api → sign up. Free tier. Coverage is
  weighted toward mainstream/album releases — see the BPM section below for
  what to expect on underground/vinyl-only pressings.

Keys are stored in the macOS Keychain, not in a plaintext file:

```bash
vinyl-label set-key anthropic
vinyl-label set-key discogs
vinyl-label set-key getsongbpm
vinyl-label key-status          # confirm all three resolve, without printing them
```

Each prompt hides your input and nothing is written to disk or shell
history. An env var (`ANTHROPIC_API_KEY`, `DISCOGS_TOKEN`,
`GETSONGBPM_API_KEY`) overrides the Keychain if set, for a one-off shell.

Copy `config.example.yaml` to `~/.vinyl_labeler/config.yaml` for the
non-secret settings (printer, label size, catalogue path).

## 3. Find your printer identifier — do this before anything else

```bash
vinyl-label discover-printer
```

If you get `usb.core.NoBackendError: No backend available`: `pyusb` needs
the `libusb` C library, which `pip install` doesn't provide --
`brew install libusb` (this is step 1 above, easy to skip).

If it runs but finds nothing: check the USB cable/port and that the printer
is powered on. Some Brother QL models have a standalone "Editor Lite" mode
that needs disabling for USB printing, but the QL-570 doesn't appear to have
this feature (single steady LED, no mode toggle) -- don't chase that if
you're on a QL-570.

Copy the identifier it prints (something like `usb://0x04f9:0x2028`) into
`printer.identifier` in your config.

Then test printing in isolation, before the rest of the pipeline is in the
loop:

```bash
vinyl-label print some_test_record.json --dry-run
```

This renders and saves a PNG without touching the printer — open it and
check the layout looks right at actual label width first. Once that looks
right, drop `--dry-run` on a real test label before running a full record
through it.

## 4a. The phone workflow (recommended) — a local web app, not a native app

No Apple Developer account needed. This runs a small server on your Mac;
your phone just opens a page in Safari over your home Wi-Fi.

```bash
uvicorn vinyl_labeler.server:app --host 0.0.0.0 --port 8000
```

`--host 0.0.0.0` matters — the default (`127.0.0.1`) only accepts
connections from the Mac itself, which is invisible to your phone. Find the
Mac's LAN IP:

```bash
ipconfig getifaddr en0
```

Then on your iPhone (same Wi-Fi network), open `http://<that-ip>:8000`.
First run, macOS will prompt to allow incoming connections for
Python/uvicorn — allow it, or the phone can't reach it at all.

What you get on the page:

- **Take photo** → auto-runs identify + Discogs + BPM lookup, no extra tap.
- **Any track with no BPM found is flagged in red** with an inline field to
  type one in — or hit **Tap tempo**, tap along with the track ~5–8 times,
  and it computes BPM from your tap intervals and fills the field for you.
- Every field is editable — artist, title, catalog #, each track's
  position/title/BPM — plus a **Re-run lookup** button for after you fix a
  misread catalog number.
- **Highlight** checkbox per track — the label render makes that track bold
  and larger, for flagging the actual song(s) worth playing on an EP.
- **Library tab** — every record you've actually printed (not dry-runs)
  lands in a searchable table: artist, title, catalog #, track count, last
  printed date, print count. "Edit / Reprint" loads it straight back into
  the review screen — fix a typo, re-tap a BPM, whatever — and printing
  again updates that same entry rather than creating a duplicate row.
- **Label / tape type** dropdown is populated live from whatever your
  installed `brother_ql` actually supports — continuous rolls and pre-cut
  sizes both listed, tagged as which is which.
- Font-size fields for BPM vs. track details independently, so BPM can run
  large while everything else stays compact — **Preview** renders it
  exactly as it'll print before you commit tape to it.
- **Dry run** checkbox on the print step, same as the CLI's `--dry-run` —
  leave it on until you've confirmed the printer connection works.

One deliberate technical choice worth knowing: the camera capture uses a
plain `<input type=file capture>` rather than a live camera preview
(`getUserMedia`). The latter needs a secure context (HTTPS or localhost) to
work in Safari at all — setting up a TLS cert just for a LAN IP is
unnecessary friction for a tool that never leaves your home network. The
plain file input just opens the native camera app and hands back the
photo, and works fine over plain HTTP.

## 4b. Command line (scriptable / batch)

```bash
vinyl-label process photo_of_label.jpg
```

This chains all four stages and pauses at the review step so you approve
or correct each track's BPM before anything gets printed. You can also run
the stages individually — useful while you're still trusting the output:

```bash
vinyl-label identify photo.jpg --out record.json
vinyl-label enrich record.json
vinyl-label review record.json
vinyl-label print record.json
```

## What to expect from BPM, honestly

For a genuinely vinyl-only underground/white-label pressing, don't be
surprised if a real fraction of tracks come back with nothing confirmed —
that's the state of BPM data in 2026, not a bug here. In order of what to
trust:

1. **Printed on the label itself** — some pressings (a lot of 90s/2000s
   trance and hard house especially) print BPM/key directly on the vinyl.
   When the photo extraction catches this, it's the most trustworthy figure
   there is, because it's literally what you're holding.
2. **GetSongBPM** — real, free, independent of Spotify (whose own
   audio-features/tempo endpoint was killed in November 2024 with no
   official replacement — most "BPM finder" sites you'll find now are
   either broken or running on unofficial data). Its own documentation says
   it only archives songs tied to an existing album, so vinyl-only
   promos/white labels are often simply not in it.
3. **Beatport, checked by eye** — not automated. Beatport doesn't have a
   stable public search API, and scraping their search results is legally
   grey for a tool like this, so instead the label render carries a direct
   search-link per track for you to glance at. For progressive/house/techno
   specifically this is usually your best real-world source, since labels
   submit their own metadata — it just can't be wired in cleanly.
4. **Audio analysis** — optional, `pip install librosa`, then pass a short
   recording of the actual track (phone mic off the turntable is fine) and
   the tool will measure tempo off it directly. Slower per record, but it's
   the only tier that checks the pressing you actually own rather than a
   database entry that might be for a different edit.

Anything unconfirmed prints with a trailing `?` on the label rather than a
bare number, so a guess never quietly reads as a fact once it's on paper.

Config (`~/.vinyl_labeler/config.yaml`) is loaded once when the server
starts — restart it after changing API keys or the printer identifier.
FastAPI's auto-generated interactive docs are at `/docs` if you want to
poke the API endpoints directly.

## Known rough edges to expect on first real runs

- **GetSongBPM response parsing** (`bpm.py`) follows their published
  `lookup=song:X artist:Y` pattern, but the exact JSON field names weren't
  verifiable from the sandbox this was built in (the domain isn't reachable
  there). If it silently returns nothing, check the real response shape
  against their docs and adjust the two lines that read `data["search"]`
  and `top["tempo"]`.
- **Discogs artist+title fallback** (used when there's no legible catalog
  number) is noisy — a title can match several pressings/reissues. It logs
  which confidence tier matched; treat `artist_title` matches as worth a
  second look, not gospel.
- **Vision extraction** is a draft, not ground truth, particularly on worn
  or handwritten labels — that's exactly why the enrich step cross-checks
  against Discogs rather than trusting the photo alone.

## The catalogue

Every record you actually print (dry-runs don't count) is saved to
`~/.vinyl_labeler/catalogue.json`, keyed by catalog number. Reprinting the
same catalog number after an edit updates that entry in place — rather than
piling up duplicate rows — and bumps its print count. This file is what
backs the Library tab in the web UI; it's plain JSON, so it's fine to open
directly if you ever want to script something against it.

## Credits

Built on top of:

- [Discogs](https://www.discogs.com) — the release/tracklist database this
  tool is built around; vinyl-only white labels and pressing-level detail
  are exactly what it's good at.
- [GetSongBPM](https://getsongbpm.com) — one tier of the BPM lookup chain.
- [brother_ql](https://github.com/pklaus/brother_ql) — the QL-570 printer
  protocol implementation.
- [Claude](https://www.anthropic.com/claude) — reads the label/cover photo.
