"""Pseudonymous participant IDs (PIDs) - built, but OFF until approved.

Status: the switch exists so the rest of the app can call ``display_id`` today.
With ``privacy.pseudonymise: false`` (the default) it returns the real ID
unchanged, so nothing about current behaviour changes.

Why a *keyed* hash
------------------
A plain SHA-256 of ``CD052`` protects nothing: there are only ~11,000 possible
IDs (CD000-CD9999), and hashing all of them takes milliseconds, which reverses
every code. So a PID is

    PID = prefix + "-" + encode( HMAC-SHA256(secret key, normalised ID) )[:10]

- **Secret key**: 32 random bytes in ``privacy.key_file``, kept outside the git
  repository (e.g. on the restricted study drive) and shared only with the team.
  Without the key a PID can't be reversed or recreated.
- **Deterministic**: the same ID and key always give the same PID, so records,
  T2 exports and later batches line up.
- **Normalised input**: ``cd052``, ``CD052 `` and ``CD052`` give one PID.
- **Encoding**: 10 characters from an alphabet without the letters c, d, i, l,
  o, u, so a PID can never look like a ``CD``+digits participant ID (and so
  passes the public-record leak guard). 30^10 ~ 6e14 codes; collisions are
  checked for anyway.
- **Rotating the key** changes every PID; a key is therefore tied to a study
  and recorded (by fingerprint only) in manifests.

Open for approval: key location/custody, prefix, code length, whether exports
may include a private lookup table (ID -> PID) for the team.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from pathlib import Path
from typing import Iterable, Optional

from .config import Config

_ALPHABET = "0123456789abefghjkmnpqrstvwxyz"   # 30 symbols, no c d i l o u
_CODE_LEN = 10


class PseudoIdError(RuntimeError):
    pass


def normalise(participant: str) -> str:
    return "".join(str(participant).split()).upper()


def _encode(digest: bytes, length: int = _CODE_LEN) -> str:
    n = int.from_bytes(digest, "big")
    out = []
    for _ in range(length):
        n, r = divmod(n, len(_ALPHABET))
        out.append(_ALPHABET[r])
    return "".join(out)


def make_pid(participant: str, key: bytes, prefix: str = "P") -> str:
    if len(key) < 16:
        raise PseudoIdError("pseudonymisation key is too short (need >= 16 bytes)")
    digest = hmac.new(key, normalise(participant).encode("utf-8"), hashlib.sha256).digest()
    return f"{prefix}-{_encode(digest)}"


def key_fingerprint(key: bytes) -> str:
    """Safe to record: identifies which key was used without revealing it."""
    return hashlib.sha256(b"cdcc-pid-fingerprint:" + key).hexdigest()[:12]


def generate_key(path: Path) -> Path:
    """Create a new random key file. Refuses to overwrite an existing key."""
    path = Path(path)
    if path.exists():
        raise PseudoIdError(f"a key already exists at {path}; refusing to overwrite")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(secrets.token_hex(32) + "\n", encoding="utf-8")
    return path


def load_key(config: Config) -> bytes:
    key_file = (config.privacy.key_file or "").strip()
    if not key_file:
        raise PseudoIdError("pseudonymisation is on but privacy.key_file is not set")
    path = Path(key_file)
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise PseudoIdError(f"cannot read the pseudonymisation key: {exc}") from exc
    try:
        return bytes.fromhex(text)
    except ValueError as exc:
        raise PseudoIdError("the pseudonymisation key file is not valid hex") from exc


def enabled(config: Config) -> bool:
    return bool(config.privacy.pseudonymise)


def display_id(config: Config, participant: str, key: Optional[bytes] = None) -> str:
    """The ID to show or record: the real ID while the switch is off."""
    if not enabled(config):
        return participant
    return make_pid(participant, key if key is not None else load_key(config),
                    config.privacy.prefix or "P")


def mapping(config: Config, participants: Iterable[str]) -> dict[str, str]:
    """ID -> displayed ID for many participants, checking for collisions."""
    ids = list(dict.fromkeys(participants))
    if not enabled(config):
        return {p: p for p in ids}
    key = load_key(config)
    out = {p: display_id(config, p, key) for p in ids}
    seen: dict[str, str] = {}
    for p, pid in out.items():
        other = seen.get(pid)
        if other is not None and normalise(other) != normalise(p):
            raise PseudoIdError("two participants produced the same PID; lengthen the code")
        seen[pid] = p
    return out


def status(config: Config) -> dict[str, object]:
    """For the UI/API: whether PIDs are on and the key is usable (never the key)."""
    info: dict[str, object] = {"enabled": enabled(config), "key_configured": bool(config.privacy.key_file)}
    if enabled(config):
        try:
            info["key_fingerprint"] = key_fingerprint(load_key(config))
            info["ok"] = True
        except PseudoIdError as exc:
            info["ok"], info["error"] = False, str(exc)
    else:
        info["ok"] = True
    return info
