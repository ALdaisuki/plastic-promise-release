"""Canonical, content-free identity for compute-owned structured JSON intents."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping


def structured_intent_digest(
    *,
    project_id: str,
    intent_id: str,
    schema_id: str,
    user_payload: Mapping[str, object],
) -> str:
    """Return the wire-stable digest without exposing provider prompt material."""

    canonical_payload = json.dumps(
        dict(user_payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    material = "\x1f".join((project_id, intent_id, schema_id, canonical_payload))
    return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()
