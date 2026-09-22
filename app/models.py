"""Request/response models for the merge API."""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

# Any JSON document: object, array, string, number, boolean or null.
JsonValue = Optional[Any]

# Sides a conflict can be adjudicated to.
ResolutionSide = Literal["base", "left", "right"]


class ConflictResolution(BaseModel):
    """One adjudication: which side wins at a reported conflict."""

    model_config = ConfigDict(extra="forbid")

    pointer: str = Field(
        ...,
        description=(
            "JSON Pointer (RFC 6901) of a reported conflict, exactly as it "
            "appears in the response 'conflicts' (after identity-array "
            "indices are resolved)."
        ),
    )
    resolution: ResolutionSide = Field(
        ...,
        description=(
            "Side whose value is placed at the conflict location in the "
            "merged document. Choosing a side on which the location is "
            "deleted removes it; 'base' restores the default outcome."
        ),
    )


class MergeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    base: JsonValue = Field(
        ..., description="Common ancestor of the two revisions."
    )
    left: JsonValue = Field(..., description="The left-hand revision.")
    right: JsonValue = Field(..., description="The right-hand revision.")
    array_identity: dict[str, str] = Field(
        default_factory=dict,
        alias="arrayIdentity",
        description=(
            "Mapping from a JSON Pointer (RFC 6901) of an object array to the "
            "name of the item field that uniquely identifies an entry. The "
            "token '*' matches a single positional index, e.g. "
            "'/servers/*/disks' -> 'name'."
        ),
    )
    conflict_resolutions: list[ConflictResolution] = Field(
        default_factory=list,
        alias="conflictResolutions",
        description=(
            "Adjudications for reported conflicts. Each entry binds a "
            "conflict pointer to the winning side; conflicts not listed keep "
            "the base value. Malformed, duplicated, or non-conflict pointers "
            "are rejected with HTTP 400. The response 'conflicts' always "
            "report the original three-way values."
        ),
    )
    protected_paths: list[str] = Field(
        default_factory=list,
        alias="protectedPaths",
        description=(
            "JSON Pointers (RFC 6901) of locations that must never be "
            "written automatically. A single '*' token matches one path "
            "segment. When either revision changes a protected location - "
            "even a one-sided change or an identical change on both sides - "
            "the base value is kept and a conflict with kind "
            "'protected_change' carrying the three-way values is reported. "
            "Fields of objects and identity-array items match their final "
            "reported pointer; deletions match their original path. An "
            "explicit conflictResolutions entry for the reported pointer "
            "overrides the protection; unresolved protected conflicts keep "
            "the base value. Malformed or duplicate pointers are rejected "
            "with HTTP 400."
        ),
    )


class Conflict(BaseModel):
    pointer: str = Field(..., description="JSON Pointer (RFC 6901) of the conflict.")
    kind: str = Field(
        ...,
        description=(
            "both_modified: both sides changed the value differently; "
            "delete_vs_modify: one side deleted what the other modified; "
            "both_added: both sides added different values at a path absent "
            "from base; protected_change: a protectedPaths location was "
            "changed by either revision (one-sided or identical changes "
            "included); the base value is kept until adjudicated."
        ),
    )
    base: JsonValue
    left: JsonValue
    right: JsonValue


class MergeResponse(BaseModel):
    merged: JsonValue
    conflicts: list[Conflict]
