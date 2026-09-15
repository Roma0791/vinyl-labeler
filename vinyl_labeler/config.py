"""
Config loading. Reads config.yaml for non-secret settings (printer, label
size, catalogue path). API keys are NOT read from this file -- they live in
the macOS Keychain (via `vinyl-label set-key`), which is encrypted at rest
and access-controlled by the OS, unlike a plaintext YAML file sitting in
~/.vinyl_labeler. An env var, when set, overrides the Keychain -- useful for
CI or a one-off shell without touching stored state.
"""
import os
from pathlib import Path
import keyring
import yaml

DEFAULT_CONFIG_PATH = Path.home() / ".vinyl_labeler" / "config.yaml"

KEYRING_SERVICE = "vinyl-labeler"

# name -> (keyring account, env var) -- shared by Config's properties and the
# `vinyl-label set-key` CLI command so both sides agree on where a key lives.
SECRETS = {
    "anthropic": ("anthropic_api_key", "ANTHROPIC_API_KEY"),
    "discogs": ("discogs_token", "DISCOGS_TOKEN"),
    "getsongbpm": ("getsongbpm_api_key", "GETSONGBPM_API_KEY"),
}


def _get_secret(account: str, env_var: str) -> str:
    return os.environ.get(env_var) or keyring.get_password(KEYRING_SERVICE, account) or ""


class Config:
    def __init__(self, data: dict):
        self._data = data

    # --- API keys (env var > Keychain; never read from the YAML file) ---
    @property
    def anthropic_api_key(self) -> str:
        return _get_secret(*SECRETS["anthropic"])

    @property
    def discogs_token(self) -> str:
        return _get_secret(*SECRETS["discogs"])

    @property
    def getsongbpm_api_key(self) -> str:
        return _get_secret(*SECRETS["getsongbpm"])

    @property
    def anthropic_workspace_id(self) -> str:
        # Not a secret -- just an identifier. Only required when the
        # Anthropic key isn't scoped to a single workspace (a key created
        # with "works across workspaces" needs this on every request, or
        # the API returns 400 invalid_request_error). Leave blank for a
        # single-workspace-scoped key.
        return os.environ.get("ANTHROPIC_WORKSPACE_ID") or self._data.get("anthropic_workspace_id", "")

    # --- Anthropic model for the vision extraction step ---
    @property
    def vision_model(self) -> str:
        # Haiku by default -- cheap and fast for straightforward label reads;
        # the web UI exposes a per-photo Haiku/Sonnet switch to retry a
        # specific worn/promo label with the stronger model.
        return self._data.get("vision_model", "claude-haiku-4-5")

    # --- Printer settings ---
    @property
    def printer_model(self) -> str:
        return self._data.get("printer", {}).get("model", "QL-570")

    @property
    def printer_identifier(self) -> str:
        # e.g. "usb://0x04f9:0x2028" -- run `vinyl-label discover-printer` to find yours
        return self._data.get("printer", {}).get("identifier", "")

    @property
    def printer_backend(self) -> str:
        return self._data.get("printer", {}).get("backend", "pyusb")

    @property
    def label_size(self) -> str:
        # brother_ql label-size code. Default is "62" (62mm CONTINUOUS tape),
        # not a die-cut size -- tracklists vary from 2 tracks (12" single) to
        # 8+ (an LP side-by-side), and continuous tape cuts to fit the actual
        # content instead of wasting a fixed-size label on a 2-track EP.
        # Die-cut options ("62x100", "62x29", "29x90"...) are still there if
        # you'd rather have a uniform label size for a specific storage system.
        return self._data.get("printer", {}).get("label_size", "62")

    @property
    def catalogue_path(self) -> Path:
        p = self._data.get("catalogue_path")
        # .expanduser() matters here: Path("~/x") does NOT expand "~" on its
        # own -- without it this silently writes to a literal "~" folder
        # under whatever the current working directory happens to be.
        return Path(p).expanduser() if p else Path.home() / ".vinyl_labeler" / "catalogue.json"

    @property
    def print_queue_path(self) -> Path:
        p = self._data.get("print_queue_path")
        return Path(p).expanduser() if p else Path.home() / ".vinyl_labeler" / "print_queue.json"

    @property
    def discogs_user_agent(self) -> str:
        return self._data.get(
            "discogs_user_agent", "VinylLabeler/0.1 (personal use script)"
        )


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> Config:
    if path.exists():
        with open(path) as f:
            data = yaml.safe_load(f) or {}
    else:
        data = {}
    return Config(data)
