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
import requests

BASE_URL = "https://api.discogs.com"


class DiscogsClient:
    def __init__(self, token: str, user_agent: str):
        self.token = token
        self.headers = {"User-Agent": user_agent}
        if token:
            self.headers["Authorization"] = f"Discogs token={token}"

    def search_release(self, catalog_number: str = None, artist: str = None,
                        title: str = None) -> list:
        params = {"type": "release", "format": "Vinyl"}
        if catalog_number:
            params["catno"] = catalog_number
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
              "release_title": str, "styles": [str, ...], "tracklist": [...],
              "confidence": "catalog_number"|"artist_title"|"none"}
    """
    results = []
    confidence = "none"

    if catalog_number:
        results = client.search_release(catalog_number=catalog_number)
        if results:
            confidence = "catalog_number"

    if not results and (artist or release_title):
        results = client.search_release(artist=artist, title=release_title)
        if results:
            confidence = "artist_title"  # noisier -- multiple pressings can share this

    if not results:
        return {"matched": False, "release_id": None, "tracklist": [], "confidence": "none"}

    release_id = results[0]["id"]
    release = client.get_release(release_id)
    tracklist = client.extract_tracklist(release)
    return {
        "matched": True,
        "release_id": release_id,
        "release_url": release.get("uri", ""),
        # Discogs' own artist/title once matched -- a confirmed catalog_number
        # match is a near-unique key, so this is worth trusting over a vision
        # read that may have caught the catalog number printed clearly but
        # missed the artist/title (small print, multi-artist-per-side
        # layouts, worn ink -- exactly where OCR-style reads fail first).
        "artist": release.get("artists_sort", ""),
        "release_title": release.get("title", ""),
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
