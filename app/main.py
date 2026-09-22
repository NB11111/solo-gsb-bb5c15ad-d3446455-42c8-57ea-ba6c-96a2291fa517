"""FastAPI application: three-way JSON merge for configuration review."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException

from .config import settings
from .merger import Merger, parse_pointer
from .middleware import RequestSizeLimitMiddleware
from .models import Conflict, MergeRequest, MergeResponse

app = FastAPI(
    title="Three-way JSON Merge API",
    description=(
        "Merges two JSON revisions of a configuration against their common "
        "base. Changes on one side are accepted automatically; divergent "
        "changes and delete-versus-modify cases keep the base value and are "
        "reported as conflicts located by JSON Pointer (RFC 6901). "
        "'protectedPaths' marks pointers (a '*' token matches one segment) "
        "that no change may write automatically: any left/right change "
        "there, including one-sided or identical changes, keeps the base "
        "and is reported with kind 'protected_change'. Reported conflicts "
        "may be adjudicated per request via 'conflictResolutions', which "
        "maps a conflict pointer to the winning side (base, left or "
        "right); explicit adjudication also overrides protected paths. "
        "The response conflicts always keep the original three-way values."
    ),
    version="1.2.0",
)
app.add_middleware(
    RequestSizeLimitMiddleware,
    max_body_size=settings.max_request_size_bytes,
)


@app.get("/health", tags=["health"])
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post(
    "/merge",
    response_model=MergeResponse,
    tags=["merge"],
    summary="Three-way merge of JSON documents",
)
async def merge(request: MergeRequest) -> MergeResponse:
    # Validate pointers up front so malformed configuration produces a clear
    # 4xx response instead of an opaque error.
    for pointer in request.array_identity:
        try:
            parse_pointer(pointer)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"invalid arrayIdentity pointer {pointer!r}: {exc}",
            ) from exc

    # Protected pointers must be well-formed and unique (by decoded
    # reference tokens, so escaped spellings of the same pointer collide).
    protected_seen: set[tuple[str, ...]] = set()
    for pointer in request.protected_paths:
        try:
            tokens = parse_pointer(pointer)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"invalid protectedPaths pointer {pointer!r}: {exc}",
            ) from exc
        if tokens in protected_seen:
            raise HTTPException(
                status_code=400,
                detail=f"duplicate protectedPaths entry for pointer {pointer!r}",
            )
        protected_seen.add(tokens)

    try:
        merger = Merger(request.array_identity, request.protected_paths)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Validate resolution pointers up front: they must be well-formed and
    # unique (by decoded reference tokens, so escaped spellings of the same
    # pointer collide). Whether each pointer denotes a real conflict can
    # only be decided by the merger, which raises ValueError -> 400.
    resolutions: dict[str, str] = {}
    seen: set[tuple[str, ...]] = set()
    for entry in request.conflict_resolutions:
        try:
            tokens = parse_pointer(entry.pointer)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"invalid conflictResolutions pointer {entry.pointer!r}: {exc}",
            ) from exc
        if tokens in seen:
            raise HTTPException(
                status_code=400,
                detail=(
                    "duplicate conflictResolutions entry for pointer "
                    f"{entry.pointer!r}"
                ),
            )
        seen.add(tokens)
        resolutions[entry.pointer] = entry.resolution

    try:
        merged, raw_conflicts = merger.merge(
            request.base, request.left, request.right, resolutions
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    conflicts = [
        Conflict(
            pointer=conflict.path,
            kind=conflict.kind,
            base=conflict.base,
            left=conflict.left,
            right=conflict.right,
        )
        for conflict in raw_conflicts
    ]
    return MergeResponse(merged=merged, conflicts=conflicts)
