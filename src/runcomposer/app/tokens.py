"""The ingest token (DESIGN.md §5, the security floor of a push): minted
per dispatch (or per run), stored as its hash only, and checked before a
push reads or writes anything."""

from __future__ import annotations

import hmac
import secrets
from typing import TYPE_CHECKING

from runcomposer.core.errors import AuthRequired, Forbidden
from runcomposer.core.hashing import sha256_hex

if TYPE_CHECKING:
    from .service import Service

__all__ = ["TOKEN_PREFIX", "authorize_push", "mint_token"]

TOKEN_PREFIX = "rct_"


def mint_token(service: Service, run_id: str, dispatch_id: str | None) -> str | None:
    """Mint a token for a dispatch (or for the run). Only its hash is
    stored; the plaintext exists in the return value and nowhere else.
    ``None`` when the deployment disabled tokens."""
    if not service.config.tokens_required:
        return None
    token = TOKEN_PREFIX + secrets.token_urlsafe(24)
    service.store.set_ingest_token(run_id, dispatch_id, sha256_hex(token.encode("utf-8")))
    return token


def authorize_push(service: Service, run_id: str, token: str | None) -> str | None:
    """Check a push's token **before anything is read or written**.
    Returns the dispatch the token is bound to (``None`` for a run-level
    token, or when tokens are disabled)."""
    if not service.config.tokens_required:
        return None
    if not token:
        raise AuthRequired("an ingest token is required to push results (DESIGN.md §5)")
    presented = sha256_hex(token.encode("utf-8"))
    for stored, dispatch_id in service.store.ingest_tokens(run_id).items():
        if hmac.compare_digest(stored, presented):
            return dispatch_id
    raise Forbidden("the ingest token does not match this run")
