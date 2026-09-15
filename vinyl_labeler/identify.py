"""
Stage 1: photo -> structured draft.

Sends the label/cover photo to Claude with vision and asks for a strict JSON
extraction. This is a DRAFT, not ground truth -- it gets cross-checked
against Discogs in the enrich stage. Worn labels, promo stamps and small
runout text can still fool it, so every field carries a confidence note the
review step surfaces.

Importantly: if the record itself has BPM/key printed on the label (common
on 90s/2000s trance, techno and hard house pressings), this stage captures
that too -- it's the single most trustworthy BPM source there is, because
it's literally what's on the pressing you're holding.
"""
import base64
import json
import mimetypes
from pathlib import Path

import anthropic

EXTRACTION_PROMPT = """You are reading a photo of a vinyl record label or sleeve for a personal \
cataloguing tool. Extract what you can see and return ONLY a JSON object, no \
other text, no markdown fences.

Schema:
{
  "artist": string or null,
  "release_title": string or null,
  "record_label": string or null,       // the imprint/label name, not the artist
  "catalog_number": string or null,
  "side": string or null,               // e.g. "A", "AA", "B1" if visible
  "tracks": [
    {
      "position": string or null,       // e.g. "A1"
      "title": string or null,
      "printed_bpm": number or null,    // ONLY if a BPM figure is actually printed on the label
      "printed_key": string or null     // ONLY if a key is actually printed on the label
    }
  ],
  "legibility": "clear" | "partial" | "poor",
  "notes": string       // anything ambiguous, worn, handwritten, or guessed
}

Do not invent a catalog number, BPM or key that isn't visibly printed. If a \
field isn't visible or you're not confident, use null and say why in notes."""


def _encode_image(path: Path) -> dict:
    mime, _ = mimetypes.guess_type(str(path))
    if mime is None:
        mime = "image/jpeg"
    data = base64.standard_b64encode(path.read_bytes()).decode("utf-8")
    return {"type": "base64", "media_type": mime, "data": data}


def identify_from_photo(photo_path: Path, api_key: str, model: str, workspace_id: str = "") -> dict:
    # workspace_id is only required for a key that isn't scoped to a single
    # workspace ("works across workspaces" in the Console) -- such a key
    # returns 400 invalid_request_error without this header on every request.
    extra_headers = {"anthropic-workspace-id": workspace_id} if workspace_id else {}
    client = anthropic.Anthropic(api_key=api_key, default_headers=extra_headers)
    image_block = _encode_image(photo_path)

    response = client.messages.create(
        model=model,
        max_tokens=1500,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "image", "source": image_block},
                    {"type": "text", "text": EXTRACTION_PROMPT},
                ],
            }
        ],
    )

    text = "".join(block.text for block in response.content if block.type == "text")
    text = text.strip()
    # Strip stray markdown fences if the model adds them despite instructions.
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"Could not parse a JSON extraction from the model's response. "
            f"Raw response was:\n{text}"
        ) from e
