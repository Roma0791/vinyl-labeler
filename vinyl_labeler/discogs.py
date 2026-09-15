"""
Stage 2: confirm the tracklist against Discogs.

Discogs is the right database for this because it's built around pressings,
not digital releases -- catalog numbers, matrix/runout data and vinyl-only
white labels are exactly what it's good at. Catalog number is a near-unique
key, so we search on that first and only fall back to artist+title, which is
much noisier (remixes, multiple pressings, reissues all share a title).

Sign up for a free personal access token at:
https://www.discogs.com/settings/developers
"""
import re

import requests

BASE_URL = "https://api.discogs.com"


def _titles_plausibly_match(a: str, b: str) -> bool:
    """Loose overlap check -- normalized, share at least one significant
    (4+ char) word. Used to sanity-check a catalog_number match against
    what vision actually read off the label: catalog number is a near-
    unique key *if read correctly*, but a single misread digit can land
    on another real, valid catalog number entirely -- confirmed live,
    UR-049 misread as UR-069 (a real but different Underground Resistance
    release), matched with full "catalog_number" confidence despite being
    the wrong record. If vision didn't give us a title to compare against,
    there's nothing to sanity-check, so this doesn't second-guess."""
    def words(s):
        return {w for w in re.sub(r"[^a-z0-9\s]", " ", s.lower()).split() if len(w) >= 4}
    wa, wb = words(a or ""), words(b or "")
    if not wa or not wb:
        return True
    return bool(wa & wb)


def _parse_notes_bpm(notes: str) -> dict:
    """Extract per-position BPM from a release's free-text notes field,
    when present -- e.g. "BPM:\\nA: 138\\nB: 139". Discogs has no
    structured BPM field, but DJ-culture labels/community-curated entries
    often document it in notes anyway; when they do, it's BPM for this
    exact pressing, straight from community documentation, so it's worth
    trusting on par with reading it off the label yourself. Returns
    {position: bpm}, {} if the notes don't contain a recognizable block."""
    m = re.search(r"BPM:?[ \t]*\n((?:[A-Za-z0-9]+[ \t]*:[ \t]*\d+(?:\.\d+)?[ \t]*\n?)+)", notes or "")
    if not m:
        return {}
    out = {}
    for line in m.group(1).strip().splitlines():
        lm = re.match(r"([A-Za-z0-9]+)[ \t]*:[ \t]*(\d+(?:\.\d+)?)", line.strip())
        if lm:
            out[lm.group(1).upper()] = float(lm.group(2))
    return out


class DiscogsClient:
    def __init__(self, token: str, user_agent: str):
        self.token = token
        self.headers = {"User-Agent": user_agent}
        if token:
            self.headers["Authorization"] = f"Discogs token={token}"

    def search_release(self, catalog_number: str = None, artist: str = None,
                        title: str = None) -> list:
        params = {"type": "release"}
        if catalog_number:
            # catno is already a near-unique key -- don't also constrain by
            # format. A real release with a format tagged inconsistently on
            # Discogs' side (or a catalog number shared with a non-vinyl
            # pressing) would otherwise come back as a false "not found"
            # despite definitely existing -- reported live: a catalog
            # number read correctly off a spine photo, on its own, found
            # nothing. artist/title fallback keeps the filter since that
            # search is broader/noisier and format narrows it usefully.
            # A literal hyphen inside the query breaks Discogs' search
            # parser -- it's treated as an exclude/minus operator rather
            # than punctuation, so "UR-049" silently matches unrelated
            # releases instead of the exact catalog number printed on the
            # label. Confirmed live across two labels: replacing the
            # hyphen with a space ("UR 049") finds the correct release;
            # the literal hyphen does not, with no error to signal it.
            params["catno"] = catalog_number.replace("-", " ")
        else:
            params["format"] = "Vinyl"
        if artist:
            params["artist"] = artist
        if title:
            params["release_title"] = title
        if not any([catalog_number, artist, title]):
            return []

        r = requests.get(f"{BASE_URL}/database/search", params=params,
                          headers=self.headers, timeout=15)
        r.raise_for_status()
        return r.json().get("results", [])

    def get_release(self, release_id: int) -> dict:
        r = requests.get(f"{BASE_URL}/releases/{release_id}",
                          headers=self.headers, timeout=15)
        r.raise_for_status()
        return r.json()

    def get_master(self, master_id: int) -> dict:
        r = requests.get(f"{BASE_URL}/masters/{master_id}",
                          headers=self.headers, timeout=15)
        r.raise_for_status()
        return r.json()

    @staticmethod
    def extract_tracklist(release: dict) -> list:
        """Returns [{"position": "A1", "title": "..."}], skipping headers/indexes."""
        out = []
        for t in release.get("tracklist", []):
            if t.get("type_", "track") != "track":
                continue
            out.append({"position": t.get("position", ""), "title": t.get("title", "")})
        return out


def confirm_tracklist(client: DiscogsClient, catalog_number: str, artist: str,
                       release_title: str) -> dict:
    """
    Tries catalog number first (near-unique), falls back to artist+title.
    Returns {"matched": bool, "release_id": int|None, "artist": str,
              "release_title": str, "catalog_number": str,
              "styles": [str, ...], "tracklist": [...],
              "confidence": "catalog_number"|"artist_title"|"none"}
    """
    results = []
    confidence = "none"

    if catalog_number:
        candidates = client.search_release(catalog_number=catalog_number)
        # Sanity-check against vision's own release_title before trusting
        # any of these as a near-unique match -- see _titles_plausibly_match.
        if release_title:
            candidates = [c for c in candidates
                          if _titles_plausibly_match(release_title, c.get("title", ""))]
        if candidates:
            # Discogs often has several near-duplicate listings for the
            # same catalog number (different data entries, reissues,
            # mispresses...) -- search results already carry a
            # community.have count, so prefer the most-owned one as the
            # best proxy for "the canonical/most complete entry" rather
            # than whatever Discogs' search happened to rank first.
            candidates.sort(key=lambda c: c.get("community", {}).get("have", 0), reverse=True)
            results = candidates
            confidence = "catalog_number"

    if not results and (artist or release_title):
        results = client.search_release(artist=artist, title=release_title)
        if results:
            confidence = "artist_title"  # noisier -- multiple pressings can share this

    if not results and release_title:
        # Last resort: title alone, dropping a possibly-wrong artist guess.
        # Common on a label with no distinct artist text printed (just a
        # label/crew name) -- vision can mistake a track title for the
        # artist, and an artist+title search with a wrong artist term
        # tends to find nothing even when the title alone would.
        results = client.search_release(title=release_title)
        if results:
            confidence = "artist_title"  # same noisy tier -- no artist to add confidence

    if not results:
        return {"matched": False, "release_id": None, "tracklist": [], "confidence": "none"}

    release_id = results[0]["id"]
    release = client.get_release(release_id)
    tracklist = client.extract_tracklist(release)
    notes_bpm = _parse_notes_bpm(release.get("notes", ""))
    for t in tracklist:
        bpm = notes_bpm.get((t.get("position") or "").upper())
        if bpm:
            t["discogs_notes_bpm"] = bpm

    # Notes-BPM coverage varies release-to-release -- confirmed live, a
    # release picked for one track's BPM can be missing it for another.
    # If any track is still missing it, check the master's designated
    # main_release (Discogs' own "canonical" pick, distinct from the
    # community.have ranking above though they often coincide) as one
    # extra, bounded source to fill gaps -- not every version under the
    # master (could be dozens; not worth the extra API calls per record),
    # just this one well-reasoned fallback.
    if any(not t.get("discogs_notes_bpm") for t in tracklist) and release.get("master_id"):
        try:
            master = client.get_master(release["master_id"])
            main_release_id = master.get("main_release")
            if main_release_id and main_release_id != release_id:
                main_release = client.get_release(main_release_id)
                main_notes_bpm = _parse_notes_bpm(main_release.get("notes", ""))
                for t in tracklist:
                    if not t.get("discogs_notes_bpm"):
                        bpm = main_notes_bpm.get((t.get("position") or "").upper())
                        if bpm:
                            t["discogs_notes_bpm"] = bpm
        except requests.RequestException:
            pass  # best-effort supplementary lookup -- a failure here shouldn't break the match

    return {
        "matched": True,
        "release_id": release_id,
        "release_url": release.get("uri", ""),
        # Discogs' own artist/title/catno once matched -- worth trusting
        # over a vision read that may have caught the catalog number
        # printed clearly but misread a digit (small print, worn ink,
        # barcode-adjacent text -- exactly where OCR-style reads slip),
        # missed the artist/title, or both. Confirmed live: a spine photo's
        # catalog number was off by one digit, catno search correctly found
        # nothing, artist_title fallback found the right release -- but the
        # original wrong catno was kept in the saved record instead of
        # being corrected here, exactly the gap this fixes.
        "artist": release.get("artists_sort", ""),
        "release_title": release.get("title", ""),
        "catalog_number": results[0].get("catno", ""),
        # Discogs' style taxonomy ("Tribal", "Tech House") is far more
        # specific and DJ-relevant than a generic per-track genre tag would
        # be -- falls back to the broader "genres" field ("Electronic") only
        # if the release has no styles listed at all.
        "styles": release.get("styles") or release.get("genres") or [],
        "tracklist": tracklist,
        "confidence": confidence,
        "candidate_count": len(results),
    }


def distribute_styles_to_tracks(tracks: list, styles: list) -> None:
    """Assigns a per-track "genre" in place, positionally: track i gets
    styles[i]. Discogs doesn't have a real per-track genre/style concept --
    style is a release-level tag list -- so positional mapping is the best
    available approximation. Releases usually list fewer styles than
    tracks, so any track past the end of the list gets the last style
    rather than nothing, on the assumption the release's final/overall
    style still applies to the rest of the tracklist."""
    if not styles:
        for t in tracks:
            t["genre"] = ""
        return
    for i, t in enumerate(tracks):
        t["genre"] = styles[i] if i < len(styles) else styles[-1]
