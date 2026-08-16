"""Shared input validators.

Deliberately dependency-free so both the request models (app/models/) and the
FastAPI dependencies (app/dependencies.py) can import it without a cycle.
"""

import re

# device_id and client_id are interpolated into the frame storage path as
# "<device_id>/<client_id>.jpg" (app/services/frame_service.py::_store_jpeg).
# Unconstrained, "../../.." escapes the storage root — an unauthenticated write
# primitive, since the ingestion tier is anonymous by design. Constrain the charset
# at the edge rather than trusting every consumer downstream.
#
# '.' is a legal character (ids may contain it), so the charset alone still admits a
# bare '.' or '..', which traverse a level. Those two are excluded separately because
# Pydantic v2's Rust regex engine has no look-around — hence is_safe_id() rather than
# a single pattern shared as a Field(pattern=...).
#
# The Android client sends UUID.randomUUID().toString() for both ids, so this is not
# a v1 wire-contract break.
_SAFE_ID_CHARSET = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_RESERVED_PATH_SEGMENTS = frozenset({".", ".."})


def is_safe_id(value: str) -> bool:
    """True if ``value`` is safe to use as a single filesystem path segment."""
    return bool(_SAFE_ID_CHARSET.match(value)) and value not in _RESERVED_PATH_SEGMENTS
