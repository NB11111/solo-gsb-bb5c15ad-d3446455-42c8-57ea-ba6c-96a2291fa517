"""HTTP-level tests: /health, /merge, request-size enforcement."""

from __future__ import annotations

import os

os.environ.setdefault("MAX_REQUEST_SIZE_BYTES", "10M")

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.main import app
from app.middleware import RequestSizeLimitMiddleware


client = TestClient(app)


def test_health_ok():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_merge_endpoint_full_envelope():
    payload = {
        "base": {"a": 1, "b": 2, "srv": [{"id": "x", "v": 1}]},
        "left": {"a": 10, "b": 2, "srv": [{"id": "x", "v": 1}, {"id": "y", "v": 2}]},
        "right": {"a": 1, "b": 20, "srv": [{"id": "x", "v": 3}]},
        "arrayIdentity": {"/srv": "id"},
    }
    response = client.post("/merge", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["merged"] == {
        "a": 10,
        "b": 20,
        "srv": [
            {"id": "x", "v": 3},  # right-only edit -> accepted
            {"id": "y", "v": 2},  # left-only add, sorted after x
        ],
    }
    assert body["conflicts"] == []


def test_merge_accepts_snake_case_alias_too():
    payload = {
        "base": {"srv": []},
        "left": {"srv": [{"id": "a"}]},
        "right": {"srv": []},
        "array_identity": {"/srv": "id"},
    }
    response = client.post("/merge", json=payload)
    assert response.status_code == 200
    assert response.json()["merged"] == {"srv": [{"id": "a"}]}


def test_merge_defaults_array_identity_to_empty():
    response = client.post(
        "/merge",
        json={"base": [1, 2], "left": [1, 2, 3], "right": [1, 2]},
    )
    assert response.status_code == 200
    assert response.json()["merged"] == [1, 2, 3]
    assert response.json()["conflicts"] == []


def test_unknown_field_is_rejected():
    response = client.post(
        "/merge",
        json={"base": 1, "left": 1, "right": 1, "bogus": 1},
    )
    assert response.status_code == 422


def test_invalid_pointer_returns_400():
    response = client.post(
        "/merge",
        json={
            "base": {"srv": []},
            "left": {"srv": []},
            "right": {"srv": []},
            "arrayIdentity": {"srv": "id"},  # missing leading slash
        },
    )
    assert response.status_code == 400
    assert "arrayIdentity" in response.json()["detail"]


def test_invalid_identity_field_returns_400():
    response = client.post(
        "/merge",
        json={
            "base": {"srv": []},
            "left": {"srv": []},
            "right": {"srv": []},
            "arrayIdentity": {"/srv": ""},
        },
    )
    assert response.status_code == 400


# ------------------------------------------------------- conflict resolutions


def test_merge_applies_conflict_resolutions_to_nested_objects():
    payload = {
        "base": {"db": {"host": "a", "port": 5432}},
        "left": {"db": {"host": "b", "port": 5433}},
        "right": {"db": {"host": "c", "port": 5434}},
        "conflictResolutions": [{"pointer": "/db/host", "resolution": "left"}],
    }
    response = client.post("/merge", json=payload)
    assert response.status_code == 200
    body = response.json()
    # /db/host adjudicated to the left; /db/port unlisted -> keeps base.
    assert body["merged"] == {"db": {"host": "b", "port": 5432}}
    # Conflicts still report the original three-way values for both.
    assert body["conflicts"] == [
        {"pointer": "/db/host", "kind": "both_modified",
         "base": "a", "left": "b", "right": "c"},
        {"pointer": "/db/port", "kind": "both_modified",
         "base": 5432, "left": 5433, "right": 5434},
    ]


def test_merge_applies_conflict_resolutions_to_identity_arrays():
    payload = {
        "base": {"servers": [{"id": "a", "port": 8080}, {"id": "b", "port": 80}]},
        "left": {"servers": [{"id": "b", "port": 80}]},  # deletes a
        "right": {"servers": [{"id": "a", "port": 9090}, {"id": "b", "port": 81}]},
        "arrayIdentity": {"/servers": "id"},
        "conflictResolutions": [{"pointer": "/servers/0", "resolution": "right"}],
    }
    response = client.post("/merge", json=payload)
    assert response.status_code == 200
    body = response.json()
    # The adjudicated item takes right's value; b's right-only edit is kept.
    assert body["merged"] == {
        "servers": [{"id": "a", "port": 9090}, {"id": "b", "port": 81}]
    }
    assert body["conflicts"] == [
        {
            "pointer": "/servers/0",
            "kind": "delete_vs_modify",
            "base": {"id": "a", "port": 8080},
            "left": None,
            "right": {"id": "a", "port": 9090},
        }
    ]


def test_resolution_can_accept_a_deletion():
    payload = {
        "base": {"servers": [{"id": "a"}, {"id": "b"}]},
        "left": {"servers": [{"id": "b"}]},  # deletes a
        "right": {"servers": [{"id": "a", "x": 1}, {"id": "b"}]},  # modifies a
        "arrayIdentity": {"/servers": "id"},
        "conflictResolutions": [{"pointer": "/servers/0", "resolution": "left"}],
    }
    response = client.post("/merge", json=payload)
    assert response.status_code == 200
    assert response.json()["merged"] == {"servers": [{"id": "b"}]}


def test_merge_accepts_snake_case_conflict_resolutions():
    payload = {
        "base": {"a": 1},
        "left": {"a": 2},
        "right": {"a": 3},
        "conflict_resolutions": [{"pointer": "/a", "resolution": "right"}],
    }
    response = client.post("/merge", json=payload)
    assert response.status_code == 200
    assert response.json()["merged"] == {"a": 3}


def test_resolution_for_nonexistent_pointer_returns_400():
    response = client.post(
        "/merge",
        json={
            "base": {"a": 1},
            "left": {"a": 2},
            "right": {"a": 3},
            "conflictResolutions": [{"pointer": "/nope", "resolution": "left"}],
        },
    )
    assert response.status_code == 400
    assert "does not exist" in response.json()["detail"]


def test_resolution_for_non_conflict_location_returns_400():
    # /a exists but merged cleanly (left-only edit).
    response = client.post(
        "/merge",
        json={
            "base": {"a": 1},
            "left": {"a": 2},
            "right": {"a": 1},
            "conflictResolutions": [{"pointer": "/a", "resolution": "left"}],
        },
    )
    assert response.status_code == 400
    assert "does not correspond to a reported conflict" in response.json()["detail"]


def test_duplicate_resolution_pointer_returns_400():
    response = client.post(
        "/merge",
        json={
            "base": {"a": 1},
            "left": {"a": 2},
            "right": {"a": 3},
            "conflictResolutions": [
                {"pointer": "/a", "resolution": "left"},
                {"pointer": "/a", "resolution": "right"},
            ],
        },
    )
    assert response.status_code == 400
    assert "duplicate" in response.json()["detail"]


def test_invalid_resolution_pointer_returns_400():
    response = client.post(
        "/merge",
        json={
            "base": {"a": 1},
            "left": {"a": 2},
            "right": {"a": 3},
            "conflictResolutions": [{"pointer": "a", "resolution": "left"}],
        },
    )
    assert response.status_code == 400
    assert "conflictResolutions" in response.json()["detail"]


def test_invalid_resolution_side_returns_422():
    response = client.post(
        "/merge",
        json={
            "base": {"a": 1},
            "left": {"a": 2},
            "right": {"a": 3},
            "conflictResolutions": [{"pointer": "/a", "resolution": "sideways"}],
        },
    )
    assert response.status_code == 422


def test_resolution_entry_with_unknown_field_returns_422():
    response = client.post(
        "/merge",
        json={
            "base": {"a": 1},
            "left": {"a": 2},
            "right": {"a": 3},
            "conflictResolutions": [
                {"pointer": "/a", "resolution": "left", "note": "bogus"}
            ],
        },
    )
    assert response.status_code == 422


# ------------------------------------------------------------- protectedPaths


def test_protected_paths_blocks_one_sided_change():
    payload = {
        "base": {"a": 1, "b": 2},
        "left": {"a": 99, "b": 2},
        "right": {"a": 1, "b": 20},
        "protectedPaths": ["/a"],
    }
    response = client.post("/merge", json=payload)
    assert response.status_code == 200
    body = response.json()
    # /a is protected: base kept despite a one-sided change; /b auto-merges.
    assert body["merged"] == {"a": 1, "b": 20}
    assert body["conflicts"] == [
        {"pointer": "/a", "kind": "protected_change",
         "base": 1, "left": 99, "right": 1}
    ]


def test_protected_paths_blocks_identical_change_on_both_sides():
    payload = {
        "base": {"a": 1},
        "left": {"a": 9},
        "right": {"a": 9},
        "protectedPaths": ["/a"],
    }
    response = client.post("/merge", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["merged"] == {"a": 1}
    assert body["conflicts"] == [
        {"pointer": "/a", "kind": "protected_change",
         "base": 1, "left": 9, "right": 9}
    ]


def test_protected_paths_accepts_snake_case_alias():
    response = client.post(
        "/merge",
        json={
            "base": {"a": 1},
            "left": {"a": 2},
            "right": {"a": 1},
            "protected_paths": ["/a"],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["merged"] == {"a": 1}
    assert body["conflicts"][0]["kind"] == "protected_change"


def test_protected_paths_wildcard_matches_one_segment():
    payload = {
        "base": {"o": {"x": 1, "y": 1}},
        "left": {"o": {"x": 9, "y": 1}},
        "right": {"o": {"x": 1, "y": 8}},
        "protectedPaths": ["/o/x", "/o/y"],
    }
    response = client.post("/merge", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["merged"] == {"o": {"x": 1, "y": 1}}
    assert {c["pointer"] for c in body["conflicts"]} == {"/o/x", "/o/y"}
    assert all(c["kind"] == "protected_change" for c in body["conflicts"])


def test_protected_paths_identity_array_final_pointer_and_original_path():
    # Field edit matches the item's final pointer...
    payload = {
        "base": {"servers": [
            {"id": "a", "port": 8080},
            {"id": "b", "port": 80},
        ]},
        "left": {"servers": [
            {"id": "a", "port": 8080},
            {"id": "b", "port": 9090},
        ]},
        "right": {"servers": [
            {"id": "a", "port": 8080},
            {"id": "b", "port": 80},
        ]},
        "arrayIdentity": {"/servers": "id"},
        "protectedPaths": ["/servers/1/port"],
    }
    response = client.post("/merge", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["merged"] == {"servers": [
        {"id": "a", "port": 8080},
        {"id": "b", "port": 80},
    ]}
    assert body["conflicts"] == [{
        "pointer": "/servers/1/port",
        "kind": "protected_change",
        "base": 80, "left": 9090, "right": 80,
    }]

    # ... while a deletion matches the item's original base index.
    payload["left"] = {"servers": [{"id": "b", "port": 80}]}  # deletes a
    payload["protectedPaths"] = ["/servers/0"]
    response = client.post("/merge", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["merged"] == {"servers": [
        {"id": "a", "port": 8080},
        {"id": "b", "port": 80},
    ]}
    assert body["conflicts"] == [{
        "pointer": "/servers/0",
        "kind": "protected_change",
        "base": {"id": "a", "port": 8080},
        "left": None,
        "right": {"id": "a", "port": 8080},
    }]


def test_conflict_resolution_overrides_protected_path():
    payload = {
        "base": {"a": 1},
        "left": {"a": 9},
        "right": {"a": 1},
        "protectedPaths": ["/a"],
        "conflictResolutions": [{"pointer": "/a", "resolution": "left"}],
    }
    response = client.post("/merge", json=payload)
    assert response.status_code == 200
    body = response.json()
    # Explicit adjudication overrides the protection...
    assert body["merged"] == {"a": 9}
    # ... but the conflict is still reported with the original three-way
    # values, keeping kind protected_change.
    assert body["conflicts"] == [
        {"pointer": "/a", "kind": "protected_change",
         "base": 1, "left": 9, "right": 1}
    ]


def test_unresolved_protected_conflict_keeps_base_with_other_resolution():
    payload = {
        "base": {"a": 1, "b": 1},
        "left": {"a": 2, "b": 2},
        "right": {"a": 3, "b": 3},
        "protectedPaths": ["/a"],
        "conflictResolutions": [{"pointer": "/b", "resolution": "right"}],
    }
    response = client.post("/merge", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["merged"] == {"a": 1, "b": 3}
    assert {c["pointer"] for c in body["conflicts"]} == {"/a", "/b"}


def test_protected_paths_one_sided_object_addition_is_atomic():
    # A one-sided object addition containing a hit protected descendant is
    # withheld whole; adjudicating the reported descendant writes the
    # chosen side's whole object.
    payload = {
        "base": {},
        "left": {"o": {"x": 1, "y": 2}},
        "right": {},
        "protectedPaths": ["/o/x"],
    }
    response = client.post("/merge", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["merged"] == {}
    assert body["conflicts"] == [
        {"pointer": "/o/x", "kind": "protected_change",
         "base": None, "left": 1, "right": None}
    ]

    payload["conflictResolutions"] = [
        {"pointer": "/o/x", "resolution": "left"}
    ]
    response = client.post("/merge", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["merged"] == {"o": {"x": 1, "y": 2}}
    assert body["conflicts"][0]["pointer"] == "/o/x"


def test_protected_paths_one_sided_identity_item_deletion_is_atomic():
    payload = {
        "base": {"s": [{"id": "a", "v": 1, "w": 1}, {"id": "b"}]},
        "left": {"s": [{"id": "b"}]},  # deletes item a
        "right": {"s": [{"id": "a", "v": 1, "w": 1}, {"id": "b"}]},
        "arrayIdentity": {"/s": "id"},
        "protectedPaths": ["/s/0/v"],
    }
    response = client.post("/merge", json=payload)
    assert response.status_code == 200
    body = response.json()
    # The whole deleted item is kept, not a field-wise reconstruction.
    assert body["merged"] == {
        "s": [{"id": "a", "v": 1, "w": 1}, {"id": "b"}]
    }
    assert body["conflicts"] == [
        {"pointer": "/s/0/v", "kind": "protected_change",
         "base": 1, "left": None, "right": 1}
    ]

    payload["conflictResolutions"] = [
        {"pointer": "/s/0/v", "resolution": "left"}
    ]
    response = client.post("/merge", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["merged"] == {"s": [{"id": "b"}]}


def test_invalid_protected_pointer_returns_400():
    response = client.post(
        "/merge",
        json={
            "base": {"a": 1},
            "left": {"a": 2},
            "right": {"a": 1},
            "protectedPaths": ["a"],  # missing leading slash
        },
    )
    assert response.status_code == 400
    assert "protectedPaths" in response.json()["detail"]


def test_duplicate_protected_pointer_returns_400():
    response = client.post(
        "/merge",
        json={
            "base": {"a": 1, "b": 1},
            "left": {"a": 2, "b": 2},
            "right": {"a": 1, "b": 1},
            "protectedPaths": ["/a", "/a"],
        },
    )
    assert response.status_code == 400
    assert "duplicate protectedPaths" in response.json()["detail"]


def test_protected_paths_entry_must_be_a_string():
    response = client.post(
        "/merge",
        json={
            "base": {"a": 1},
            "left": {"a": 2},
            "right": {"a": 1},
            "protectedPaths": [1],
        },
    )
    assert response.status_code == 422


def test_protected_paths_appear_in_openapi_schema():
    schema = client.get("/openapi.json").json()
    properties = schema["components"]["schemas"]["MergeRequest"]["properties"]
    assert "protectedPaths" in properties
    conflict_props = schema["components"]["schemas"]["Conflict"]["properties"]
    assert "protected_change" in conflict_props["kind"]["description"]


# ------------------------------------------------------------- size limiting


@pytest.fixture
def limited_client():
    small = FastAPI()

    @small.post("/echo")
    async def echo(request: Request) -> dict:
        return {"size": len(await request.body())}

    small.add_middleware(RequestSizeLimitMiddleware, max_body_size=100)
    return TestClient(small)


def test_request_under_limit_passes(limited_client):
    response = limited_client.post("/echo", content=b"x" * 100)
    assert response.status_code == 200
    assert response.json() == {"size": 100}


def test_request_over_declared_content_length_rejected(limited_client):
    response = limited_client.post(
        "/echo",
        content=b"x" * 101,
        headers={"Content-Length": "101"},
    )
    assert response.status_code == 413
    assert "maximum allowed size" in response.json()["detail"]


def test_chunked_oversized_body_rejected_even_without_content_length():
    small = FastAPI()

    @small.post("/echo")
    async def echo(request: Request):
        return {"size": len(await request.body())}

    small.add_middleware(RequestSizeLimitMiddleware, max_body_size=100)

    def chunks():
        yield b"x" * 60
        yield b"x" * 60  # total 120 > 100

    with TestClient(small) as tc:
        response = tc.post(
            "/echo",
            content=chunks(),
            headers={"Transfer-Encoding": "chunked"},
        )
    assert response.status_code == 413


def test_size_setting_parses_suffixes():
    assert _parse("10M") == 10 * 1024 * 1024
    assert _parse("256K") == 256 * 1024
    assert _parse("1G") == 1024**3
    assert _parse("0") == 0


def _parse(value: str) -> int:
    from app.config import _parse_size

    return _parse_size(value)
