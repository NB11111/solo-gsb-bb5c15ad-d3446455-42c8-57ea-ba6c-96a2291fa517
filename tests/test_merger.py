"""Tests for the three-way merge engine (no HTTP layer)."""

from __future__ import annotations

import pytest

from app.merger import Merger, json_equal, parse_pointer


def merge(base, left, right, identity=None):
    merged, conflicts = Merger(identity).merge(base, left, right)
    return merged, [
        (c.path, c.kind, c.base, c.left, c.right) for c in conflicts
    ]


# ----------------------------------------------------------------- acceptance


def test_unrelated_field_changes_are_both_accepted():
    merged, conflicts = merge(
        {"a": 1, "b": 2},
        {"a": 10, "b": 2},
        {"a": 1, "b": 20},
    )
    assert merged == {"a": 10, "b": 20}
    assert conflicts == []


def test_identical_change_on_both_sides_is_merged_once():
    merged, conflicts = merge({"a": 1}, {"a": 9}, {"a": 9})
    assert merged == {"a": 9}
    assert conflicts == []


def test_deletion_on_both_sides_is_accepted():
    merged, conflicts = merge({"a": 1, "b": 2}, {"b": 2}, {"b": 2})
    assert merged == {"b": 2}
    assert conflicts == []


def test_deletion_on_one_side_with_other_unchanged_is_accepted():
    merged, conflicts = merge({"a": 1, "b": 2}, {"a": 1, "b": 2}, {"b": 2})
    assert merged == {"b": 2}
    assert conflicts == []


def test_single_sided_addition_is_accepted():
    merged, conflicts = merge({"a": 1}, {"a": 1}, {"a": 1, "b": 2})
    assert merged == {"a": 1, "b": 2}
    assert conflicts == []


def test_null_is_a_real_value_not_a_deletion():
    merged, conflicts = merge({"a": 1}, {"a": None}, {"a": None})
    assert merged == {"a": None}
    assert conflicts == []


# ------------------------------------------------------------------ conflicts


def test_both_modified_scalar_conflict_keeps_base():
    merged, conflicts = merge({"a": 1}, {"a": 2}, {"a": 3})
    assert merged == {"a": 1}
    assert conflicts == [("/a", "both_modified", 1, 2, 3)]


def test_delete_vs_modify_keeps_base_both_directions():
    merged, conflicts = merge({"a": 1}, {}, {"a": 3})
    assert merged == {"a": 1}
    assert conflicts == [("/a", "delete_vs_modify", 1, None, 3)]

    merged, conflicts = merge({"a": 1}, {"a": 3}, {})
    assert merged == {"a": 1}
    assert conflicts == [("/a", "delete_vs_modify", 1, 3, None)]


def test_null_vs_scalar_is_a_both_modified_conflict():
    merged, conflicts = merge({"a": 1}, {"a": None}, {"a": 2})
    assert merged == {"a": 1}
    assert conflicts == [("/a", "both_modified", 1, None, 2)]


def test_both_added_with_different_values_conflicts():
    merged, conflicts = merge({}, {"a": 1}, {"a": 2})
    assert merged == {"a": 1}
    assert conflicts == [("/a", "both_added", None, 1, 2)]


def test_field_level_conflicts_inside_object_merge():
    # Both sides edit the same nested object: identical edits are merged,
    # independent edits are both accepted, only divergent edits conflict.
    base = {"o": {"same": 1, "x": 1, "y": 1, "z": 1}}
    left = {"o": {"same": 9, "x": 10, "y": 1, "z": 1}}
    right = {"o": {"same": 9, "x": 1, "y": 20, "z": 30}}
    merged, conflicts = merge(base, left, right)
    assert merged == {
        "o": {"same": 9, "x": 10, "y": 20, "z": 30}
    }
    assert conflicts == []

    # And when one field genuinely diverges, only that field is reported.
    base = {"o": {"x": 1, "y": 1}}
    left = {"o": {"x": 11, "y": 1}}
    right = {"o": {"x": 12, "y": 20}}
    merged, conflicts = merge(base, left, right)
    assert merged == {"o": {"x": 1, "y": 20}}
    assert conflicts == [("/o/x", "both_modified", 1, 11, 12)]


def test_conflict_pointers_are_sorted():
    base = {"m": {"b": 1, "a": 1}, "z": 1, "a": 1}
    left = {"m": {"b": 2, "a": 2}, "z": 2, "a": 2}
    right = {"m": {"b": 3, "a": 3}, "z": 3, "a": 3}
    _, conflicts = merge(base, left, right)
    assert [c[0] for c in conflicts] == ["/a", "/m/a", "/m/b", "/z"]


def test_pointer_tokens_with_slash_and_tilde_are_escaped():
    merged, conflicts = merge(
        {"a/b": 1, "c~d": 1},
        {"a/b": 2, "c~d": 2},
        {"a/b": 3, "c~d": 3},
    )
    assert merged == {"a/b": 1, "c~d": 1}
    assert {c[0] for c in conflicts} == {"/a~1b", "/c~0d"}


def test_root_level_conflict_uses_empty_pointer():
    merged, conflicts = merge(1, 2, 3)
    assert merged == 1
    assert conflicts == [("", "both_modified", 1, 2, 3)]


def test_bool_never_equals_number():
    assert json_equal(True, 1) is False
    merged, _ = merge({"a": True}, {"a": 1}, {"a": 1})
    # Both sides made the same (Python-loose but JSON-distinct) change.
    assert merged == {"a": 1}


# ------------------------------------------------------------ plain arrays


def test_array_without_identity_is_an_atomic_value():
    merged, conflicts = merge({"a": [1, 2]}, {"a": [1, 2, 3]}, {"a": [1, 2]})
    assert merged == {"a": [1, 2, 3]}
    assert conflicts == []

    merged, conflicts = merge({"a": [1, 2]}, {"a": [1, 2, 3]}, {"a": [9]})
    assert merged == {"a": [1, 2]}
    assert conflicts == [("/a", "both_modified", [1, 2], [1, 2, 3], [9])]


# -------------------------------------------------------- identity arrays


def test_identity_array_merges_adds_removes_and_edits():
    base = {
        "srv": [
            {"id": "a", "v": 1},
            {"id": "b", "v": 2},
            {"id": "c", "v": 3},
        ]
    }
    left = {
        "srv": [
            {"id": "a", "v": 10},  # edited on left
            {"id": "c", "v": 3},
            {"id": "e", "v": 9},
        ]
    }
    right = {
        "srv": [
            {"id": "a", "v": 1},
            {"id": "b", "v": 20},  # edited on right
            {"id": "d", "v": 8},  # added on right
        ]
    }
    merged, conflicts = merge(base, left, right, {"/srv": "id"})
    # a: left-only edit -> accepted.
    # b: left deleted it, right modified it -> base retained + conflict.
    # c: right deleted it, left untouched -> deletion accepted.
    # d/e: one-sided adds, sorted by identity.
    assert merged == {
        "srv": [
            {"id": "a", "v": 10},
            {"id": "b", "v": 2},
            {"id": "d", "v": 8},
            {"id": "e", "v": 9},
        ]
    }
    assert conflicts == [
        ("/srv/1", "delete_vs_modify",
         {"id": "b", "v": 2}, None, {"id": "b", "v": 20})
    ]


def test_identity_array_preserves_base_order_and_sorts_new_items():
    base = {"srv": [{"id": "z"}, {"id": "a"}, {"id": "m"}]}
    left = {"srv": [{"id": "z"}, {"id": "a"}, {"id": "m"}, {"id": 9}, {"id": 1}]}
    right = {"srv": [{"id": "z"}, {"id": "a"}, {"id": "m"}, {"id": "b"}]}
    merged, _ = merge(base, left, right, {"/srv": "id"})
    ids = [item["id"] for item in merged["srv"]]
    # Existing items keep base order; new items are sorted by identity.
    assert ids == ["z", "a", "m", 1, 9, "b"]


def test_identity_new_item_ordering_by_type():
    # null < booleans < numbers < strings.
    base = {"srv": [{"id": "base"}]}
    left = {
        "srv": [
            {"id": "base"},
            {"id": "z"},
            {"id": 2},
            {"id": True},
            {"id": None},
        ]
    }
    right = {"srv": [{"id": "base"}]}
    merged, _ = merge(base, left, right, {"/srv": "id"})
    assert [x["id"] for x in merged["srv"]] == ["base", None, True, 2, "z"]


def test_identity_array_delete_vs_modify_conflict_keeps_base_item():
    base = {"srv": [{"id": "a", "v": 1}, {"id": "b", "v": 2}]}
    left = {"srv": [{"id": "b", "v": 2}]}  # deletes a
    right = {"srv": [{"id": "a", "v": 99}, {"id": "b", "v": 2}]}  # edits a
    merged, conflicts = merge(base, left, right, {"/srv": "id"})
    assert merged == {"srv": [{"id": "a", "v": 1}, {"id": "b", "v": 2}]}
    assert conflicts == [
        ("/srv/0", "delete_vs_modify",
         {"id": "a", "v": 1}, None, {"id": "a", "v": 99})
    ]


def test_identity_array_field_conflict_pointer_survives_earlier_deletes():
    # c (index 2) is deleted by right; b (index 1) has a field conflict.
    base = {"srv": [{"id": "a"}, {"id": "b", "v": 1}, {"id": "c"}]}
    left = {"srv": [{"id": "a"}, {"id": "b", "v": 11}]}
    right = {"srv": [{"id": "a"}, {"id": "b", "v": 12}]}
    merged, conflicts = merge(base, left, right, {"/srv": "id"})
    assert merged == {"srv": [{"id": "a"}, {"id": "b", "v": 1}]}
    assert [c[0] for c in conflicts] == ["/srv/1/v"]
    assert conflicts[0][1] == "both_modified"


def test_identity_array_same_new_id_added_differently_conflicts():
    base = {"srv": []}
    left = {"srv": [{"id": "n", "v": 1}]}
    right = {"srv": [{"id": "n", "v": 2}]}
    merged, conflicts = merge(base, left, right, {"/srv": "id"})
    assert merged == {"srv": [{"id": "n", "v": 1}]}
    assert conflicts == [
        ("/srv", "both_added", None, {"id": "n", "v": 1}, {"id": "n", "v": 2})
    ]


def test_nested_identity_array_via_wildcard_pointer():
    base = {
        "servers": [
            {"id": "s1", "disks": [{"name": "d1", "size": 10}, {"name": "d2", "size": 20}]}
        ]
    }
    left = {
        "servers": [
            {"id": "s1", "disks": [{"name": "d1", "size": 100}, {"name": "d2", "size": 20}]}
        ]
    }
    right = {
        "servers": [
            {"id": "s1", "disks": [{"name": "d1", "size": 10}, {"name": "d3", "size": 30}]}
        ]
    }
    merged, conflicts = merge(
        base, left, right,
        {"/servers": "id", "/servers/*/disks": "name"},
    )
    assert merged == {
        "servers": [
            {"id": "s1", "disks": [
                {"name": "d1", "size": 100},
                {"name": "d3", "size": 30},
            ]}
        ]
    }
    assert conflicts == []


def test_non_eligible_identity_array_falls_back_to_atomic():
    # Scalar items cannot carry the identity field.
    base = {"srv": [1, 2]}
    left = {"srv": [1, 2, 3]}
    right = {"srv": [1, 2, 4]}
    merged, conflicts = merge(base, left, right, {"/srv": "id"})
    assert merged == {"srv": [1, 2]}
    assert [c[1] for c in conflicts] == ["both_modified"]


def test_duplicate_identity_on_a_side_falls_back_to_atomic():
    base = {"srv": [{"id": "a"}]}
    left = {"srv": [{"id": "a"}, {"id": "a"}]}
    right = {"srv": [{"id": "b"}]}
    merged, conflicts = merge(base, left, right, {"/srv": "id"})
    assert merged == {"srv": [{"id": "a"}]}
    assert [c[1] for c in conflicts] == ["both_modified"]


# ------------------------------------------------------------- determinism


def test_merge_is_deterministic():
    base = {"srv": [{"id": "a", "v": 1}, {"id": "b", "v": 2}]}
    left = {"srv": [{"id": "b", "v": 2}, {"id": "z"}, {"id": "a", "v": 11}]}
    right = {"srv": [{"id": "a", "v": 1}, {"id": "b", "v": 22}, {"id": "a2"}]}
    identity = {"/srv": "id"}
    first = Merger(identity).merge(base, left, right)
    for _ in range(5):
        assert Merger(identity).merge(base, left, right) == first


# ----------------------------------------------------------------- pointers


def test_parse_pointer_forms():
    assert parse_pointer("") == ()
    assert parse_pointer("/a/b") == ("a", "b")
    assert parse_pointer("/a~1b/c~0d") == ("a/b", "c~d")
    assert parse_pointer("/servers/*/disks") == ("servers", "*", "disks")


def test_parse_pointer_rejects_bad_forms():
    with pytest.raises(ValueError):
        parse_pointer("a/b")


# -------------------------------------------------------------- resolutions


def merge_resolved(base, left, right, identity=None, resolutions=None):
    merged, conflicts = Merger(identity).merge(base, left, right, resolutions)
    return merged, [
        (c.path, c.kind, c.base, c.left, c.right) for c in conflicts
    ]


def test_resolution_picks_left_or_right_on_both_modified():
    base = {"o": {"x": 1}}
    left = {"o": {"x": 11}}
    right = {"o": {"x": 12}}

    merged, conflicts = merge_resolved(base, left, right, resolutions={"/o/x": "left"})
    assert merged == {"o": {"x": 11}}
    # The reported conflict keeps the original three-way values.
    assert conflicts == [("/o/x", "both_modified", 1, 11, 12)]

    merged, _ = merge_resolved(base, left, right, resolutions={"/o/x": "right"})
    assert merged == {"o": {"x": 12}}

    merged, _ = merge_resolved(base, left, right, resolutions={"/o/x": "base"})
    assert merged == {"o": {"x": 1}}


def test_unlisted_conflicts_keep_base():
    base = {"a": 1, "b": 1}
    left = {"a": 2, "b": 2}
    right = {"a": 3, "b": 3}
    merged, conflicts = merge_resolved(
        base, left, right, resolutions={"/a": "right"}
    )
    assert merged == {"a": 3, "b": 1}
    assert [c[0] for c in conflicts] == ["/a", "/b"]


def test_resolution_delete_vs_modify_can_accept_either_side():
    base = {"a": 1, "k": 1}
    left = {"a": 1}  # deletes k
    right = {"a": 1, "k": 9}  # modifies k

    # Accept the deletion.
    merged, conflicts = merge_resolved(base, left, right, resolutions={"/k": "left"})
    assert merged == {"a": 1}
    assert conflicts == [("/k", "delete_vs_modify", 1, None, 9)]

    # Accept the modification.
    merged, _ = merge_resolved(base, left, right, resolutions={"/k": "right"})
    assert merged == {"a": 1, "k": 9}


def test_resolution_both_added_base_drops_the_value():
    base = {}
    left = {"a": 1}
    right = {"a": 2}

    merged, conflicts = merge_resolved(base, left, right, resolutions={"/a": "right"})
    assert merged == {"a": 2}
    assert conflicts == [("/a", "both_added", None, 1, 2)]

    # "base" means: as in base, i.e. the key stays absent.
    merged, _ = merge_resolved(base, left, right, resolutions={"/a": "base"})
    assert merged == {}


def test_resolution_on_identity_array_item_delete_vs_modify():
    base = {"srv": [{"id": "a", "v": 1}, {"id": "b", "v": 2}]}
    left = {"srv": [{"id": "b", "v": 2}]}  # deletes a
    right = {"srv": [{"id": "a", "v": 99}, {"id": "b", "v": 2}]}  # edits a

    # Accept the deletion: the item disappears and later items shift.
    merged, conflicts = merge_resolved(
        base, left, right, {"/srv": "id"}, {"/srv/0": "left"}
    )
    assert merged == {"srv": [{"id": "b", "v": 2}]}
    # The conflict is still reported at its original (base-kept) pointer.
    assert conflicts == [
        ("/srv/0", "delete_vs_modify",
         {"id": "a", "v": 1}, None, {"id": "a", "v": 99})
    ]

    # Accept the modification: the item is replaced in its base position.
    merged, _ = merge_resolved(
        base, left, right, {"/srv": "id"}, {"/srv/0": "right"}
    )
    assert merged == {"srv": [{"id": "a", "v": 99}, {"id": "b", "v": 2}]}


def test_resolution_on_identity_array_field_conflict():
    base = {"srv": [{"id": "a"}, {"id": "b", "v": 1}]}
    left = {"srv": [{"id": "a"}, {"id": "b", "v": 11}]}
    right = {"srv": [{"id": "a"}, {"id": "b", "v": 12}]}
    merged, conflicts = merge_resolved(
        base, left, right, {"/srv": "id"}, {"/srv/1/v": "right"}
    )
    assert merged == {"srv": [{"id": "a"}, {"id": "b", "v": 12}]}
    assert conflicts == [("/srv/1/v", "both_modified", 1, 11, 12)]


def test_resolutions_track_identities_across_index_shifts():
    # Resolving /srv/0 to accept the deletion shifts item b from index 1 to
    # 0, but /srv/1/v must still apply to item b (resolutions are keyed by
    # the pointers reported for the base-kept merge).
    base = {"srv": [{"id": "a", "v": 1}, {"id": "b", "v": 1}]}
    left = {"srv": [{"id": "b", "v": 11}]}  # deletes a, edits b.v
    right = {"srv": [{"id": "a", "v": 9}, {"id": "b", "v": 12}]}
    merged, conflicts = merge_resolved(
        base, left, right, {"/srv": "id"},
        {"/srv/0": "left", "/srv/1/v": "right"},
    )
    assert merged == {"srv": [{"id": "b", "v": 12}]}
    assert [c[0] for c in conflicts] == ["/srv/0", "/srv/1/v"]


def test_resolution_uses_final_pointer_after_wildcard_resolution():
    base = {
        "servers": [
            {"id": "s1", "disks": [{"name": "d1", "size": 10}]}
        ]
    }
    left = {
        "servers": [
            {"id": "s1", "disks": [{"name": "d1", "size": 100}]}
        ]
    }
    right = {
        "servers": [
            {"id": "s1", "disks": [{"name": "d1", "size": 200}]}
        ]
    }
    identity = {"/servers": "id", "/servers/*/disks": "name"}
    merged, conflicts = merge_resolved(
        base, left, right, identity,
        {"/servers/0/disks/0/size": "right"},
    )
    assert merged == {
        "servers": [
            {"id": "s1", "disks": [{"name": "d1", "size": 200}]}
        ]
    }
    assert conflicts == [("/servers/0/disks/0/size", "both_modified", 10, 100, 200)]


def test_resolution_both_added_same_new_identity():
    base = {"srv": []}
    left = {"srv": [{"id": "n", "v": 1}]}
    right = {"srv": [{"id": "n", "v": 2}]}

    merged, conflicts = merge_resolved(
        base, left, right, {"/srv": "id"}, {"/srv": "right"}
    )
    assert merged == {"srv": [{"id": "n", "v": 2}]}
    assert conflicts == [
        ("/srv", "both_added", None, {"id": "n", "v": 1}, {"id": "n", "v": 2})
    ]

    # "base": the new item is absent from base, so nothing is appended.
    merged, _ = merge_resolved(base, left, right, {"/srv": "id"}, {"/srv": "base"})
    assert merged == {"srv": []}


def test_resolution_on_root_conflict_uses_empty_pointer():
    merged, conflicts = merge_resolved(1, 2, 3, resolutions={"": "right"})
    assert merged == 3
    assert conflicts == [("", "both_modified", 1, 2, 3)]


def test_resolutions_do_not_change_reported_conflicts():
    base = {"srv": [{"id": "a", "v": 1}, {"id": "b", "v": 2}], "x": 1}
    left = {"srv": [{"id": "b", "v": 20}], "x": 10}
    right = {"srv": [{"id": "a", "v": 11}, {"id": "b", "v": 22}], "x": 99}
    identity = {"/srv": "id"}
    _, plain = Merger(identity).merge(base, left, right)
    _, resolved = Merger(identity).merge(
        base, left, right,
        {"/srv/0": "right", "/srv/1/v": "left", "/x": "left"},
    )
    assert resolved == plain


def test_resolution_for_unknown_pointer_is_rejected():
    with pytest.raises(ValueError, match="does not exist"):
        merge_resolved({"a": 1}, {"a": 2}, {"a": 3}, resolutions={"/nope": "left"})


def test_resolution_for_non_conflict_location_is_rejected():
    # /a exists in the merged document but merged cleanly (left-only edit).
    with pytest.raises(ValueError, match="does not correspond"):
        merge_resolved({"a": 1}, {"a": 2}, {"a": 1}, resolutions={"/a": "left"})


def test_resolution_with_unknown_side_is_rejected():
    with pytest.raises(ValueError, match="invalid resolution"):
        merge_resolved({"a": 1}, {"a": 2}, {"a": 3}, resolutions={"/a": "sideways"})


def test_resolution_with_malformed_pointer_is_rejected():
    with pytest.raises(ValueError):
        merge_resolved({"a": 1}, {"a": 2}, {"a": 3}, resolutions={"a": "left"})


# ----------------------------------------------------------- protectedPaths


def merge_protected(base, left, right, protected, identity=None, resolutions=None):
    merged, conflicts = Merger(identity, protected).merge(base, left, right, resolutions)
    return merged, [
        (c.path, c.kind, c.base, c.left, c.right) for c in conflicts
    ]


def test_protected_one_sided_change_keeps_base():
    merged, conflicts = merge_protected(
        {"a": 1, "b": 2}, {"a": 9, "b": 2}, {"a": 1, "b": 2}, ["/a"]
    )
    assert merged == {"a": 1, "b": 2}
    assert conflicts == [("/a", "protected_change", 1, 9, 1)]


def test_protected_identical_change_on_both_sides_keeps_base():
    # The same change made on both sides is normally merged once; under
    # protection it still must not be written automatically.
    merged, conflicts = merge_protected({"a": 1}, {"a": 9}, {"a": 9}, ["/a"])
    assert merged == {"a": 1}
    assert conflicts == [("/a", "protected_change", 1, 9, 9)]


def test_protected_path_without_any_change_is_silent():
    merged, conflicts = merge_protected(
        {"a": 1, "b": 2}, {"a": 1, "b": 3}, {"a": 1, "b": 4}, ["/a"]
    )
    # /a untouched: no protected conflict there; /b diverges normally.
    assert merged == {"a": 1, "b": 2}
    assert conflicts == [("/b", "both_modified", 2, 3, 4)]


def test_protected_coexists_with_regular_conflicts():
    merged, conflicts = merge_protected(
        {"a": 1, "b": 1}, {"a": 2, "b": 2}, {"a": 3, "b": 3}, ["/a"]
    )
    assert merged == {"a": 1, "b": 1}
    assert conflicts == [
        ("/a", "protected_change", 1, 2, 3),
        ("/b", "both_modified", 1, 2, 3),
    ]


def test_protected_wildcard_matches_single_segment():
    merged, conflicts = merge_protected(
        {"o": {"x": 1, "y": 1}}, {"o": {"x": 9, "y": 8}}, {"o": {"x": 1, "y": 1}},
        ["/o/*"],
    )
    assert merged == {"o": {"x": 1, "y": 1}}
    assert conflicts == [
        ("/o/x", "protected_change", 1, 9, 1),
        ("/o/y", "protected_change", 1, 8, 1),
    ]


def test_protected_wildcard_does_not_cross_segments():
    # /o/* matches the item /o/x itself when the item changes; but a
    # grandchild pointer /o/*/z needs two segments and is not hit by /o/*.
    merged, conflicts = merge_protected(
        {"o": {"x": {"z": 1}, "k": 1}},
        {"o": {"x": {"z": 2}, "k": 2}},
        {"o": {"x": {"z": 1}, "k": 1}},
        ["/o/*/z"],
    )
    assert merged == {"o": {"x": {"z": 1}, "k": 2}}
    assert conflicts == [("/o/x/z", "protected_change", 1, 2, 1)]

    # A literal scalar change at /o/k with only the grandchild pattern
    # configured is auto-accepted.
    merged, conflicts = merge_protected(
        {"o": {"k": 1}}, {"o": {"k": 2}}, {"o": {"k": 1}}, ["/o/*/z"]
    )
    assert merged == {"o": {"k": 2}}
    assert conflicts == []


def test_protected_object_field_lets_siblings_merge():
    base = {"o": {"x": 1, "y": 1}}
    left = {"o": {"x": 9, "y": 1}}
    right = {"o": {"x": 1, "y": 8}}
    merged, conflicts = merge_protected(base, left, right, ["/o/x"])
    assert merged == {"o": {"x": 1, "y": 8}}
    assert conflicts == [("/o/x", "protected_change", 1, 9, 1)]


def test_protected_object_blocks_one_sided_deletion_of_subtree():
    merged, conflicts = merge_protected(
        {"o": {"x": 1}}, {}, {"o": {"x": 1}}, ["/o"]
    )
    assert merged == {"o": {"x": 1}}
    assert conflicts == [("/o", "protected_change", {"x": 1}, None, {"x": 1})]


def test_protected_field_under_one_sided_object_addition_is_withheld():
    merged, conflicts = merge_protected(
        {}, {"o": {"x": 1, "y": 2}}, {}, ["/o/x"]
    )
    assert merged == {"o": {"y": 2}}
    assert conflicts == [("/o/x", "protected_change", None, 1, None)]


def test_protected_field_under_one_sided_object_deletion_is_kept():
    merged, conflicts = merge_protected(
        {"o": {"x": 1, "y": 1}}, {}, {"o": {"x": 1, "y": 1}}, ["/o/x"]
    )
    assert merged == {"o": {"x": 1}}
    assert conflicts == [("/o/x", "protected_change", 1, None, 1)]


def test_protected_root_protects_the_whole_document():
    merged, conflicts = merge_protected(1, 2, 1, [""])
    assert merged == 1
    assert conflicts == [("", "protected_change", 1, 2, 1)]

    merged, conflicts = merge_protected(
        {"a": 1}, {"a": 2}, {"a": 1}, [""]
    )
    assert merged == {"a": 1}
    assert conflicts == [("", "protected_change", {"a": 1}, {"a": 2}, {"a": 1})]


def test_protected_identity_array_item_one_sided_edit_uses_final_pointer():
    base = {"s": [{"id": "a", "v": 1}, {"id": "b", "v": 2}]}
    left = {"s": [{"id": "a", "v": 9}, {"id": "b", "v": 2}]}
    right = base
    merged, conflicts = merge_protected(base, left, right, ["/s/0"], {"/s": "id"})
    assert merged == base
    assert conflicts == [
        ("/s/0", "protected_change",
         {"id": "a", "v": 1}, {"id": "a", "v": 9}, {"id": "a", "v": 1})
    ]


def test_protected_identity_array_item_field_uses_final_pointer():
    # Item a is deleted by both revisions (auto-accepted); b survives and
    # lands at final index 0. Its field pointer must be /s/0/v.
    base = {"s": [{"id": "a"}, {"id": "b", "v": 1}]}
    left = {"s": [{"id": "b", "v": 9}]}  # deletes a, edits b.v
    right = base
    merged, conflicts = merge_protected(
        base, left, right, ["/s/0/v"], {"/s": "id"}
    )
    assert merged == {"s": [{"id": "b", "v": 1}]}
    assert conflicts == [("/s/0/v", "protected_change", 1, 9, 1)]

    # The original (base) index /s/1/v does not match the reported pointer,
    # so the one-sided edit is then auto-accepted.
    merged, conflicts = merge_protected(
        base, left, right, ["/s/1/v"], {"/s": "id"}
    )
    assert merged == {"s": [{"id": "b", "v": 9}]}
    assert conflicts == []

    # With a surviving earlier item, b keeps its final index 1.
    base = {"s": [{"id": "a"}, {"id": "b", "v": 1}, {"id": "c"}]}
    left = {"s": [{"id": "a"}, {"id": "b", "v": 9}]}  # only c deleted
    right = base
    merged, conflicts = merge_protected(
        base, left, right, ["/s/1/v"], {"/s": "id"}
    )
    assert merged == {"s": [{"id": "a"}, {"id": "b", "v": 1}]}
    assert conflicts == [("/s/1/v", "protected_change", 1, 9, 1)]


def test_protected_identity_array_item_deletion_uses_original_path():
    # Left deletes item a while right leaves it untouched. The protection
    # matches the original base index even though the item would leave the
    # merged array.
    base = {"s": [{"id": "a", "v": 1}, {"id": "b", "v": 2}]}
    left = {"s": [{"id": "b", "v": 2}]}
    right = base
    merged, conflicts = merge_protected(base, left, right, ["/s/0"], {"/s": "id"})
    assert merged == base
    assert conflicts == [
        ("/s/0", "protected_change",
         {"id": "a", "v": 1}, None, {"id": "a", "v": 1})
    ]

    # ... and a both-side deletion likewise keeps the base item.
    merged, conflicts = merge_protected(
        base, {"s": [{"id": "b", "v": 2}]}, {"s": [{"id": "b", "v": 2}]},
        ["/s/0"], {"/s": "id"},
    )
    assert merged == base
    assert conflicts == [
        ("/s/0", "protected_change", {"id": "a", "v": 1}, None, None)
    ]


def test_protected_identity_array_wildcard_item_and_field():
    base = {"s": [{"id": "a", "v": 1}, {"id": "b", "v": 2}]}
    left = {"s": [{"id": "a", "v": 9}, {"id": "b", "v": 2}]}
    right = base
    merged, conflicts = merge_protected(base, left, right, ["/s/*"], {"/s": "id"})
    assert merged == base
    assert [c[0] for c in conflicts] == ["/s/0"]
    assert conflicts[0][1] == "protected_change"

    merged, conflicts = merge_protected(base, left, right, ["/s/*/v"], {"/s": "id"})
    assert merged == base
    assert conflicts == [("/s/0/v", "protected_change", 1, 9, 1)]


def test_protected_new_identity_item_is_not_appended():
    base = {"s": []}
    left = {"s": [{"id": "n", "v": 1}]}
    right = {"s": []}
    merged, conflicts = merge_protected(base, left, right, ["/s/0"], {"/s": "id"})
    assert merged == {"s": []}
    assert conflicts == [
        ("/s/0", "protected_change", None, {"id": "n", "v": 1}, None)
    ]


def test_protected_field_of_new_identity_item_withholds_only_field():
    base = {"s": []}
    left = {"s": [{"id": "n", "v": 1, "w": 2}]}
    right = {"s": []}
    merged, conflicts = merge_protected(
        base, left, right, ["/s/0/v"], {"/s": "id"}
    )
    # The item is still appended; only the protected field is withheld.
    assert merged == {"s": [{"id": "n", "w": 2}]}
    assert conflicts == [("/s/0/v", "protected_change", None, 1, None)]


def test_protected_field_of_divergent_new_identity_item():
    base = {"s": []}
    left = {"s": [{"id": "n", "v": 1, "w": 1}]}
    right = {"s": [{"id": "n", "v": 2, "w": 1}]}
    merged, conflicts = merge_protected(
        base, left, right, ["/s/0/v"], {"/s": "id"}
    )
    assert merged == {"s": [{"id": "n", "w": 1}]}
    assert conflicts == [("/s/0/v", "protected_change", None, 1, 2)]
    # Without protection the divergent addition keeps the legacy
    # whole-item both_added conflict on the parent array.
    merged, conflicts = merge_protected(base, left, right, [], {"/s": "id"})
    assert merged == {"s": [{"id": "n", "v": 1, "w": 1}]}
    assert conflicts == [
        ("/s", "both_added", None,
         {"id": "n", "v": 1, "w": 1}, {"id": "n", "v": 2, "w": 1})
    ]


def test_protected_nested_identity_array_field_final_pointer():
    base = {
        "servers": [
            {"id": "s1", "disks": [{"name": "d1", "size": 10}]}
        ]
    }
    left = {
        "servers": [
            {"id": "s1", "disks": [{"name": "d1", "size": 100}]}
        ]
    }
    right = base
    identity = {"/servers": "id", "/servers/*/disks": "name"}
    merged, conflicts = merge_protected(
        base, left, right, ["/servers/0/disks/0/size"], identity
    )
    assert merged == base
    assert conflicts == [
        ("/servers/0/disks/0/size", "protected_change", 10, 100, 10)
    ]


def test_protected_plain_array_is_atomic():
    # Arrays without an identity field never diff element by element: an
    # element pointer cannot protect a sub-value.
    merged, conflicts = merge_protected(
        {"a": [1, 2]}, {"a": [1, 9]}, {"a": [1, 2]}, ["/a/1"]
    )
    assert merged == {"a": [1, 9]}
    assert conflicts == []

    # The array itself, however, can be protected.
    merged, conflicts = merge_protected(
        {"a": [1, 2]}, {"a": [1, 9]}, {"a": [1, 2]}, ["/a"]
    )
    assert merged == {"a": [1, 2]}
    assert conflicts == [("/a", "protected_change", [1, 2], [1, 9], [1, 2])]


def test_resolution_overrides_protected_change():
    base = {"a": 1}
    left = {"a": 9}
    right = {"a": 1}
    merged, conflicts = merge_protected(
        base, left, right, ["/a"], resolutions={"/a": "left"}
    )
    assert merged == {"a": 9}
    # The reported conflict still records the three-way values.
    assert conflicts == [("/a", "protected_change", 1, 9, 1)]

    # "base" is an explicit adjudication too: same as leaving it unresolved.
    merged, _ = merge_protected(
        base, left, right, ["/a"], resolutions={"/a": "base"}
    )
    assert merged == {"a": 1}


def test_resolution_overrides_protected_deletion():
    base = {"s": [{"id": "a"}, {"id": "b"}]}
    left = {"s": [{"id": "b"}]}  # deletes a
    right = base
    merged, conflicts = merge_protected(
        base, left, right, ["/s/0"], {"/s": "id"},
        resolutions={"/s/0": "left"},
    )
    assert merged == {"s": [{"id": "b"}]}
    assert conflicts == [
        ("/s/0", "protected_change", {"id": "a"}, None, {"id": "a"})
    ]


def test_unresolved_protected_conflict_keeps_base_alongside_resolutions():
    base = {"a": 1, "b": 1}
    left = {"a": 2, "b": 2}
    right = {"a": 3, "b": 3}
    # Only the regular conflict is adjudicated; the protected one keeps base.
    merged, conflicts = merge_protected(
        base, left, right, ["/a"], resolutions={"/b": "right"}
    )
    assert merged == {"a": 1, "b": 3}
    assert [c[0] for c in conflicts] == ["/a", "/b"]


def test_duplicate_or_invalid_protected_pointer_is_rejected():
    with pytest.raises(ValueError, match="duplicate protectedPaths"):
        Merger(None, ["/a", "/a"])
    # Escaped spellings of the same pointer collide after decoding.
    with pytest.raises(ValueError, match="duplicate protectedPaths"):
        Merger(None, ["/a~0b", "/a~0b"])
    with pytest.raises(ValueError):
        Merger(None, ["a"])
