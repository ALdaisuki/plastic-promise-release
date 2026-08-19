"""Resolve the derived embedding-index identity from the active runtime contract."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path


class EmbeddingIndexIdentityError(RuntimeError):
    """The configured derived-index identity cannot be proven safely."""


def configured_embedding_index_identity(
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """Return the governed or legacy derived-index identity.

    A governed server must resolve the identity from its active control-plane
    projection.  Missing or malformed governed state is a hard failure and may
    never fall through to legacy provider discovery or ``fallback-zero``.
    """

    env = environ if environ is not None else os.environ
    if str(env.get("PP_CONTROL_PLANE", "0")).strip() == "1":
        from plastic_promise.control_plane.store import ControlPlaneConfigStore

        try:
            root = env.get("PP_CONTROL_ROOT")
            if not root:
                sqlite_path = Path(env.get("PLASTIC_DB_PATH") or "data/db/plastic_memory.db")
                state_root = (
                    sqlite_path.parent.parent
                    if sqlite_path.parent.name == "db"
                    else sqlite_path.parent
                )
                root = str(state_root / "control")
            store = ControlPlaneConfigStore.open_existing_readonly(
                root,
                base_env=env,
            )
            # Shadow generation is intentionally evaluated against a staged
            # revision before activation.  Keep the normal runtime active-only
            # behavior, but honor the same explicit pin used by
            # ``node_runtime_bootstrap`` when a build supplies
            # ``PP_CONTROL_REVISION_ID``.
            revision_id = str(env.get("PP_CONTROL_REVISION_ID") or "").strip()
            snapshot = store.get_revision(revision_id) if revision_id else store.safe_config()
            routing = snapshot.config.get("node_routing")
            if not isinstance(routing, Mapping) or routing.get("enabled") is not True:
                raise EmbeddingIndexIdentityError("governed_embedding_identity_unavailable")
            identity = routing.get("embedding_required_identity")
            if not isinstance(identity, str) or not identity.startswith("sha256:"):
                raise EmbeddingIndexIdentityError("governed_embedding_identity_unavailable")
            return identity
        except EmbeddingIndexIdentityError:
            raise
        except Exception as exc:
            raise EmbeddingIndexIdentityError("governed_control_projection_unavailable") from exc

    if (
        "EMBEDDER_PROVIDER" not in env
        and "EMBED_MODEL" not in env
        and str(env.get("PP_MEMORY_CHUNKING", "off")).strip().casefold() != "structure-v1"
    ):
        return None

    from plastic_promise.core.memory_index import effective_embedding_model_name

    try:
        identity = effective_embedding_model_name()
    except (TypeError, ValueError) as exc:
        raise EmbeddingIndexIdentityError(str(exc)) from exc
    if not isinstance(identity, str) or not identity.strip():
        raise EmbeddingIndexIdentityError("runtime_embedding_identity_invalid")
    return identity.strip()
