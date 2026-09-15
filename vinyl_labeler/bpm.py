"""
Stage 3: BPM, ranked by how much you should trust it.

For vinyl-only pressings with no digital copy, expect a real fraction of
tracks to come back with NOTHING confirmed -- that's a fact about the data
landscape, not a bug in this tool. Tiers, best to worst:

  1. printed_on_label   -- BPM was actually printed on the pressing (from
                            the vision extraction). This is what you're
                            holding; nothing beats it when present.
  2. discogs_notes       -- BPM documented in the matched release's Discogs
                            notes field (e.g. "BPM:\nA: 138\nB: 139").
                            Discogs has no structured BPM field, but DJ-
                            culture labels/community-curated entries often
                            note it anyway -- community documentation for
                            this exact pressing, trusted on par with
                            reading it off the label yourself.
  3. getsongbpm          -- getsongbpm.com's free API. Independent of
                            Spotify (whose audio-features/tempo endpoint was
                            killed off in Nov 2024 with no official
                            replacement -- most "BPM finder" sites you'll
                            find now are either broken or running on
                            unofficial scrapers). Coverage skews mainstream;
                            per their own docs they only archive songs tied
                            to an existing album, so vinyl-only white labels
                            and promos are often simply absent.
  4. audio_analysis      -- local tempo estimate from an actual audio
                            sample of the track (e.g. a 20-30s phone
                            recording off the turntable) -- optional, but
                            it's the only tier here that measures the
                            pressing you actually have rather than looking
                            up a database entry that may be for a
                            different edit/mix. Decodes with ffmpeg and
                            estimates tempo via autocorrelation of the
                            onset-energy envelope, deliberately avoiding
                            librosa/numba: that combination has no
                            prebuilt wheel for Intel Mac + current Python,
                            and building it needs a version-matched LLVM
                            toolchain -- too fragile for a tool that
                            should just keep working.
  5. beatport_manual     -- not scraped (Beatport's search results aren't a
                            stable public API and scraping them is legally
                            grey for a redistributed tool). Instead this
                            just builds you a direct search URL to eyeball --
                            genuinely strong for progressive/house/techno
                            since labels submit their own metadata, just not
                            automatable cleanly.

Everything below writes {"bpm": float|None, "source": tier, "confidence": str}.
"""
import re
import subprocess
import urllib.parse

import numpy as np
import requests

# getsongbpm.com moved their API to this domain on 2024-09-25 (per their own
# changelog at getsongbpm.com/api). The old api.getsongbpm.com domain's
# "automatic redirect" either no longer works or routes through Cloudflare's
# bot-challenge -- confirmed by testing directly, don't revert this.
GETSONGBPM_BASE = "https://api.getsong.co"


def from_printed_label(track: dict) -> dict | None:
    bpm = track.get("printed_bpm")
    if bpm:
        return {"bpm": bpm, "source": "printed_on_label", "confidence": "high"}
    return None


def from_discogs_notes(track: dict) -> dict | None:
    """discogs.py's confirm_tracklist() attaches discogs_notes_bpm to a
    track when the matched release's notes field documents it (see
    discogs._parse_notes_bpm) -- this just promotes that into the same
    {"bpm", "source", "confidence"} shape every other tier returns."""
    bpm = track.get("discogs_notes_bpm")
    if bpm:
        return {"bpm": bpm, "source": "discogs_notes", "confidence": "high"}
    return None


def _normalize_artist(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def from_getsongbpm(api_key: str, artist: str, title: str) -> dict | None:
    """Verified live against api.getsong.co with a real key: a match
    returns {"search": [{...}]}, a miss returns {"search": {"error": "no
    result"}} -- a dict, not an empty list, which is why the type check
    below matters.

    Uses type=song with a plain title (no "song:"/"artist:" prefixes) --
    the combined type=both "song:X artist:Y" syntax the docs describe
    returns {"error": "no result"} even for a definitely-correct pair
    (confirmed live against St Germain / Rose Rouge, an album with full
    Discogs data). A title-only search reliably returns matches with each
    result's artist, exactly like getsongbpm.com's own website search
    behaves -- so this searches by title, then picks the result whose
    artist matches (normalized). Does NOT fall back to the top result when
    none match: confirmed live that a generic one-word title (e.g.
    "Jaguar", "Ascension") returns entirely unrelated songs by other
    artists with no connection to the one being searched, and silently
    trusting the top hit there returns a confidently wrong BPM with
    nothing to signal it's a guess -- worse than returning nothing, which
    the UI already handles ("BPM not found -- enter manually or tap it
    below")."""
    if not api_key or not artist or not title:
        return None
    params = {
        "api_key": api_key,
        "type": "song",
        "lookup": title,
    }
    try:
        r = requests.get(f"{GETSONGBPM_BASE}/search/", params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError):
        return None

    # On no match the API returns {"search": {"error": "no result"}} -- a
    # dict, not an empty list -- so check the type before indexing.
    results = data.get("search")
    if not isinstance(results, list) or not results:
        return None

    target = _normalize_artist(artist)
    top = next(
        (r for r in results if _normalize_artist(r.get("artist", {}).get("name", "")) == target),
        None,
    )
    if top is None:
        return None
    tempo = top.get("tempo")
    if not tempo:
        return None
    return {
        "bpm": round(float(tempo)),
        "source": "getsongbpm",
        "confidence": "medium",
        "matched_title": top.get("title"),
    }


def beatport_search_url(artist: str, title: str) -> str:
    q = urllib.parse.quote(f"{artist} {title}".strip())
    return f"https://www.beatport.com/search?q={q}"


def from_audio_sample(audio_path: str) -> dict | None:
    """Optional tier -- estimates tempo from an audio sample via
    autocorrelation of the onset-energy envelope. No claim to being as
    robust as a full beat-tracking model, but four-on-the-floor house/
    techno kicks (the common case here) are exactly the kind of strong,
    regular periodicity this suits well. Known limitation shared by any
    simple tempo estimator: it can lock onto half or double the true
    tempo (e.g. 65 vs 130) -- cross-check with Tap Tempo or your ear
    before trusting a result that looks off.
    """
    sr = 22050
    try:
        proc = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", audio_path, "-ac", "1", "-ar", str(sr),
             "-f", "f32le", "-"],
            capture_output=True, check=True, timeout=30,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return None

    samples = np.frombuffer(proc.stdout, dtype=np.float32)
    if samples.size < sr * 2:  # need at least ~2s to say anything meaningful
        return None

    # Onset-strength envelope: RMS energy per 10ms frame, then the
    # frame-to-frame rise (onsets, not decays) -- a cheap, standard proxy
    # for percussive hits without a full onset-detection model.
    hop = int(sr * 0.01)
    n_frames = samples.size // hop
    if n_frames < 20:
        return None
    frames = samples[: n_frames * hop].reshape(n_frames, hop)
    energy = np.sqrt(np.mean(frames.astype(np.float64) ** 2, axis=1))
    onset = np.diff(energy, prepend=energy[0])
    onset[onset < 0] = 0
    onset = onset - onset.mean()

    # Autocorrelate and take the strongest peak within a plausible dance-
    # music tempo range (60-200 BPM).
    frame_rate = 1.0 / 0.01
    min_lag = int(frame_rate * 60 / 200)
    max_lag = int(frame_rate * 60 / 60)
    if max_lag >= onset.size:
        return None
    autocorr = np.correlate(onset, onset, mode="full")[onset.size - 1:]
    window = autocorr[min_lag:max_lag]
    if window.size == 0 or not np.isfinite(window).all() or window.max() <= 0:
        return None
    best_lag = min_lag + int(np.argmax(window))
    bpm = 60.0 * frame_rate / best_lag
    return {"bpm": round(bpm), "source": "audio_analysis", "confidence": "high"}


def resolve_bpm(track: dict, artist: str, getsongbpm_key: str,
                 audio_sample_path: str = None) -> dict:
    """Runs the tiers in order, returns the first hit plus the Beatport
    check-link regardless (cheap, always useful as a second opinion)."""
    result = from_printed_label(track)
    if result is None:
        result = from_discogs_notes(track)
    if result is None and audio_sample_path:
        result = from_audio_sample(audio_sample_path)
    if result is None:
        result = from_getsongbpm(getsongbpm_key, artist, track.get("title", ""))
    if result is None:
        result = {"bpm": None, "source": "none", "confidence": "unconfirmed"}

    result["beatport_check_url"] = beatport_search_url(artist, track.get("title", ""))
    return result
