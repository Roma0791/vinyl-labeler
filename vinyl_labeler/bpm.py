"""
Stage 3: BPM, ranked by how much you should trust it.

For vinyl-only pressings with no digital copy, expect a real fraction of
tracks to come back with NOTHING confirmed -- that's a fact about the data
landscape, not a bug in this tool. Tiers, best to worst:

  1. printed_on_label   -- BPM was actually printed on the pressing (from
                            the vision extraction). This is what you're
                            holding; nothing beats it when present.
  2. getsongbpm          -- getsongbpm.com's free API. Independent of
                            Spotify (whose audio-features/tempo endpoint was
                            killed off in Nov 2024 with no official
                            replacement -- most "BPM finder" sites you'll
                            find now are either broken or running on
                            unofficial scrapers). Coverage skews mainstream;
                            per their own docs they only archive songs tied
                            to an existing album, so vinyl-only white labels
                            and promos are often simply absent.
  3. audio_analysis      -- local tempo estimate from an actual audio
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
  4. beatport_manual     -- not scraped (Beatport's search results aren't a
                            stable public API and scraping them is legally
                            grey for a redistributed tool). Instead this
                            just builds you a direct search URL to eyeball --
                            genuinely strong for progressive/house/techno
                            since labels submit their own metadata, just not
                            automatable cleanly.

Everything below writes {"bpm": float|None, "source": tier, "confidence": str}.
"""
import subprocess
import urllib.parse

import numpy as np
import requests

GETSONGBPM_BASE = "https://api.getsongbpm.com"


def from_printed_label(track: dict) -> dict | None:
    bpm = track.get("printed_bpm")
    if bpm:
        return {"bpm": bpm, "source": "printed_on_label", "confidence": "high"}
    return None


def from_getsongbpm(api_key: str, artist: str, title: str) -> dict | None:
    """
    NOTE: field names below follow getsongbpm.com's published API pattern
    (lookup=song:X artist:Y). Their exact response shape isn't verifiable
    from this sandbox (the domain isn't reachable here) -- confirm against
    https://getsongbpm.com/api once you have a key, and adjust the
    response-parsing lines below if the JSON keys differ.
    """
    if not api_key or not artist or not title:
        return None
    params = {
        "api_key": api_key,
        "type": "both",
        "lookup": f"song:{title} artist:{artist}",
    }
    try:
        r = requests.get(f"{GETSONGBPM_BASE}/search/", params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError):
        return None

    results = data.get("search") or []
    if not results:
        return None
    top = results[0]
    tempo = top.get("tempo")
    if not tempo:
        return None
    return {
        "bpm": float(tempo),
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
    return {"bpm": round(bpm, 1), "source": "audio_analysis", "confidence": "high"}


def resolve_bpm(track: dict, artist: str, getsongbpm_key: str,
                 audio_sample_path: str = None) -> dict:
    """Runs the tiers in order, returns the first hit plus the Beatport
    check-link regardless (cheap, always useful as a second opinion)."""
    result = from_printed_label(track)
    if result is None and audio_sample_path:
        result = from_audio_sample(audio_sample_path)
    if result is None:
        result = from_getsongbpm(getsongbpm_key, artist, track.get("title", ""))
    if result is None:
        result = {"bpm": None, "source": "none", "confidence": "unconfirmed"}

    result["beatport_check_url"] = beatport_search_url(artist, track.get("title", ""))
    return result
