"""Three-way JSON merge driven by JSON Pointer paths.

The engine merges ``left`` and ``right`` revisions against their common
``base`` document:

* a change made on only one side is accepted automatically;
* when both sides make the same change it is merged once;
* when one side deletes a value and the other modifies it, or both sides
  change the same value to different results, the base value is kept and a
  conflict is reported (kind ``delete_vs_modify`` / ``both_modified``);
* when a key exists on neither side of the base and the two sides add
  different values, the left value is kept and a ``both_added`` conflict is
  reported.

Callers may mark *protected paths* (RFC 6901 pointers, with ``*`` matching a
single token): any left/right change at a protected location -- even a
one-sided or identical change -- is never written automatically. The base
value is kept and a ``protected_change`` conflict carrying the three-way
values is reported instead. Fields of objects and of identity-array items
match by their final reported pointer; deletions match by the original path.
When a one-sided add/delete of an object, or a one-sided
addition/removal of an identity-array item, carries a hit protected
descendant, that whole structural unit is atomic -- it is never assembled
field by field. Unresolved, the unit stays exactly as in base (the whole
block kept, or wholly absent from ``merged`` when base carried it nowhere);
an explicit resolution for any hit protected path writes the chosen side's
whole unit. As with other conflicts, an explicit resolution overrides the
protection; unresolved protected conflicts keep the base value.

Object arrays listed in the identity mapping are merged per item keyed by an
identity field. Arrays without an identity field are treated as atomic
values (replaced wholesale, never diffed element by element).

Callers may adjudicate conflicts by passing *resolutions* to
:meth:`Merger.merge`: a mapping from a conflict pointer (RFC 6901, exactly
as reported in the returned conflicts) to the side whose value wins at that
location (``"base"``, ``"left"`` or ``"right"``). Conflicts not listed keep
the base value. The reported conflicts are unaffected by resolutions, so an
auditor always sees the original three-way values.

The merge result is deterministic for identical inputs.
"""

from __future__ import annotations

import json
from collections import OrderedDict
from typing import Any, NamedTuple, Optional

# A document value may be any JSON value; the response model mirrors this.
JsonValue = Optional[Any]

# Sentinel meaning "the key / item is absent on this side".  It is distinct
# from JSON ``null`` during the merge but rendered as ``null`` in responses.
MISSING: Any = object()


class IdMarker(NamedTuple):
    """Pointer step marking an item inside an identity-merged array.

    During merging items are addressed by their identity value instead of a
    positional index, because indices shift as earlier items are deleted.
    Markers are resolved to concrete indices once the final array order is
    known (see :meth:`Merger._resolve_paths`).
    """

    id_key: str
    id_value: Any


class RawConflict(NamedTuple):
    kind: str
    path: tuple
    base: Any
    left: Any
    right: Any


class Merger:
    def __init__(
        self,
        identity_map: Optional[dict[str, str]] = None,
        protected_paths: Optional[list[str]] = None,
    ) -> None:
        # Exact-pointer config, keyed by token tuple.
        self._exact: dict[tuple[str, ...], str] = {}
        # Wildcard config: "*" tokens match a single concrete index token.
        self._wild: list[tuple[tuple[str, ...], str]] = []
        for pointer, id_key in (identity_map or {}).items():
            if not isinstance(id_key, str) or not id_key:
                raise ValueError(
                    f"identity field for pointer {pointer!r} must be a non-empty string"
                )
            tokens = parse_pointer(pointer)
            if any(t == "*" for t in tokens):
                self._wild.append((tokens, id_key))
            else:
                self._exact[tokens] = id_key
        # More specific (more literal tokens) patterns first; the rest is a
        # total ordering so lookup is deterministic.
        self._wild.sort(key=lambda p: (-sum(t != "*" for t in p[0]), -len(p[0]), p[0]))

        # Protected paths: exact token tuples and single-segment "*" patterns.
        self._protected_exact: set[tuple[str, ...]] = set()
        self._protected_wild: list[tuple[str, ...]] = []
        seen: set[tuple[str, ...]] = set()
        for pointer in protected_paths or []:
            tokens = parse_pointer(pointer)
            if tokens in seen:
                raise ValueError(
                    f"duplicate protectedPaths entry for pointer {pointer!r}"
                )
            seen.add(tokens)
            if any(t == "*" for t in tokens):
                self._protected_wild.append(tokens)
            else:
                self._protected_exact.add(tokens)
        self._protected_wild.sort(key=lambda t: (-sum(x != "*" for x in t), -len(t), t))

    # ------------------------------------------------------------------ API

    #: Values accepted in a *resolutions* mapping.
    RESOLUTION_SIDES = ("base", "left", "right")

    def merge(
        self,
        base: Any,
        left: Any,
        right: Any,
        resolutions: Optional[dict[str, str]] = None,
    ) -> tuple[Any, list[RawConflict]]:
        """Merge *left* and *right* against *base*.

        *resolutions* optionally maps a conflict pointer (RFC 6901, exactly
        as it appears in the returned conflicts) to the side whose value is
        placed at the conflict location: ``"base"``, ``"left"`` or
        ``"right"``. Choosing a side on which the location is deleted
        removes it from the merged document. Conflicts not listed keep the
        base value.

        Every key must denote a reported conflict; unknown pointers and
        unknown sides raise :class:`ValueError`. The returned conflicts
        (pointers and three-way values) are identical to a run without
        resolutions.
        """
        # Pass 1: plain merge. Its output locates where every change ends
        # up, and a parallel walk enumerates every changed logical path so
        # we can tell which configured protectedPaths actually hit a
        # left/right change (shortcuts like one-sided edits would otherwise
        # skip subtree traversal).
        merged, raw = self._merge_pass(base, left, right, resolutions=None)
        changed = self._enumerate_changes(base, left, right, ())

        protected: set[tuple] = set()
        for path in changed:
            # Logical matching (object keys and identity markers, "*" for
            # identity-array items) covers wildcard patterns directly.
            tokens = tuple(
                "*" if isinstance(step, IdMarker) else str(step)
                for step in path
            )
            if self._is_protected(tokens):
                protected.add(path)
                continue
            # Fields of objects and of identity-array items match by their
            # final reported pointer: match the rendered pointer too so a
            # concrete index lines up with the configured pattern.
            pointer = _render_path(merged, base, base, path)
            if self._is_protected(parse_pointer(pointer)):
                protected.add(path)

        # Pass 2: canonical merge with protection applied and no
        # resolutions. Its outcome and conflicts are resolution
        # independent: conflicts always report the original three-way
        # values.
        merged_kept, raw = self._merge_pass(
            base, left, right, resolutions=None, protected=frozenset(protected),
        )
        resolved = self._resolve_paths(
            merged_kept, merged, base, [c.path for c in raw]
        )

        if resolutions:
            # Pass 3: re-merge with both protection and resolutions,
            # substituting the chosen side's value at each resolved
            # conflict. Resolutions are matched by internal path
            # (identity-array items by identity, not index), so accepted
            # deletions cannot misalign the remaining resolutions.
            by_tokens: dict[tuple[str, ...], tuple] = {}
            for conflict, pointer in zip(raw, resolved):
                by_tokens[parse_pointer(pointer)] = conflict.path
            logical: dict[tuple, str] = {}
            for pointer, side in resolutions.items():
                if side not in self.RESOLUTION_SIDES:
                    raise ValueError(
                        f"invalid resolution {side!r} for pointer {pointer!r}: "
                        f"must be one of {', '.join(self.RESOLUTION_SIDES)}"
                    )
                tokens = parse_pointer(pointer)
                conflict_path = by_tokens.get(tokens)
                if conflict_path is None:
                    raise ValueError(
                        _unknown_resolution_message(pointer, tokens, merged_kept)
                    )
                logical[conflict_path] = side
            merged, _ = self._merge_pass(
                base, left, right,
                resolutions=logical, protected=frozenset(protected),
            )
        else:
            merged = merged_kept

        ordered = sorted(
            zip(resolved, raw),
            key=lambda pair: (pair[0], pair[1].kind),
        )
        final_conflicts = [
            RawConflict(
                kind=conflict.kind,
                path=path,
                base=_public(conflict.base),
                left=_public(conflict.left),
                right=_public(conflict.right),
            )
            for path, conflict in ordered
        ]
        return merged, final_conflicts

    def _merge_pass(
        self,
        base: Any,
        left: Any,
        right: Any,
        resolutions: Optional[dict[tuple, str]],
        protected: frozenset[tuple] = frozenset(),
    ) -> tuple[Any, list[RawConflict]]:
        """One merge run.

        *resolutions* is keyed by internal path (with IdMarker steps), as
        recorded on the conflicts of the first pass. *protected* holds
        internal paths whose changes must never be written automatically.
        """
        conflicts: list[RawConflict] = []
        merged = self._merge_value(
            base, left, right, (), conflicts, resolutions or {}, protected
        )
        return merged, conflicts

    # --------------------------------------------------------- identity cfg

    def _identity_key(self, logical_path: tuple) -> Optional[str]:
        """Return the configured identity field for the array at *path*.

        Identity arrays are addressed positionally from the document root;
        an item inside an identity array contributes a wildcard ``"*"`` step
        so that ``/servers/*/disks`` matches any parent item.
        """
        # Merge-time logical paths only contain string object keys and
        # IdMarker steps for items of identity arrays; plain arrays never
        # recurse, so positional integer steps never occur here.
        tokens = tuple(
            "*" if isinstance(step, IdMarker) else str(step)
            for step in logical_path
        )
        if tokens in self._exact:
            return self._exact[tokens]
        for pattern, id_key in self._wild:
            if len(pattern) != len(tokens):
                continue
            if all(p == "*" or p == t for p, t in zip(pattern, tokens)):
                return id_key
        return None

    def _is_protected(self, tokens: tuple[str, ...]) -> bool:
        """Whether *tokens* (concrete path, ``"*"`` may stand for an
        identity-array item step) matches a configured protected path."""
        if tokens in self._protected_exact:
            return True
        for pattern in self._protected_wild:
            if len(pattern) != len(tokens):
                continue
            if all(p == "*" or p == t for p, t in zip(pattern, tokens)):
                return True
        return False

    # ------------------------------------------------------------ merging

    def _merge_value(
        self,
        base: Any,
        left: Any,
        right: Any,
        path: tuple,
        conflicts: list[RawConflict],
        resolutions: dict[tuple, str],
        protected: frozenset[tuple] = frozenset(),
    ) -> Any:
        # A protected location with any left/right change is never written
        # automatically: keep base and record a protected_change conflict,
        # even when only one side changed or both sides changed it the same
        # way. An explicit resolution still overrides the protection.
        if path in protected and (
            not json_equal(left, base) or not json_equal(right, base)
        ):
            conflicts.append(
                RawConflict("protected_change", path, base, left, right)
            )
            return _resolve_outcome(path, resolutions, base, base, left, right)

        # Both absent: the location simply does not exist.
        if left is MISSING and right is MISSING:
            return MISSING

        # Structured values recurse first so independent nested edits are
        # accepted individually and only truly conflicting leaves are
        # reported.
        if isinstance(base, dict) and isinstance(left, dict) and isinstance(right, dict):
            return self._merge_object(
                left, right, base, path, conflicts, resolutions, protected
            )
        # An object added on both sides where base carried none is normally
        # atomic, but descends field by field when a protected path lives
        # below (identical fields merge once; divergent or protected fields
        # are reported at their own pointers).
        if (
            base is MISSING
            and isinstance(left, dict)
            and isinstance(right, dict)
            and self._protected_below(protected, path)
        ):
            return self._merge_object(
                left, right, base, path, conflicts, resolutions, protected
            )
        # A one-sided object add/delete is one atomic structural unit. When
        # it carries a hit protected descendant it must never be assembled
        # field by field: unresolved it stays exactly as in base (the whole
        # block kept, or wholly absent when base had none); an explicit
        # resolution for a hit protected path writes the chosen side's whole
        # unit. Without a hit it follows the ordinary atomic shortcut.
        if (left is MISSING or right is MISSING) and _revisions_as_dicts(
            base, left, right
        ):
            return self._merge_one_sided_object(
                base, left, right, path, conflicts, resolutions, protected
            )

        if (
            isinstance(base, list)
            and isinstance(left, list)
            and isinstance(right, list)
            and (id_key := self._identity_key(path)) is not None
            and _identity_eligible(base, left, right, id_key)
        ):
            return self._merge_identity_array(
                base, left, right, path, id_key,
                conflicts, resolutions, protected,
            )
        # One-sided delete/add of an identity array, or a one-sided array
        # addition at an identity-configured path where base carried none:
        # otherwise atomic, but merged per item when protection can reach
        # inside -- deletions match by original path, and one-sided
        # added/removed items carrying hit protected descendants are
        # enforced as atomic units.
        base_list = base if isinstance(base, list) else (
            [] if base is MISSING else None
        )
        left_list = left if isinstance(left, list) else ([] if left is MISSING else None)
        right_list = right if isinstance(right, list) else (
            [] if right is MISSING else None
        )
        if (
            base_list is not None
            and left_list is not None
            and right_list is not None
            and (left is not MISSING or right is not MISSING)
            and (
                (base is not MISSING and self._protected_below(protected, path))
                or (
                    base is MISSING
                    and (left is MISSING or right is MISSING)
                    and self._protected_pattern_under(path)
                )
            )
            and (id_key := self._identity_key(path)) is not None
            and _identity_eligible(base_list, left_list, right_list, id_key)
        ):
            return self._merge_identity_array(
                base_list, left_list, right_list, path, id_key,
                conflicts, resolutions, protected,
            )

        # One side absent, the other present (scalar / non-identity array).
        if left is MISSING:
            if base is MISSING:
                return right  # added on the right only
            if json_equal(right, base):
                return MISSING  # right untouched -> accept left's deletion
            conflicts.append(
                RawConflict("delete_vs_modify", path, base, MISSING, right)
            )
            return _resolve_outcome(path, resolutions, base, base, MISSING, right)
        if right is MISSING:
            if base is MISSING:
                return left  # added on the left only
            if json_equal(left, base):
                return MISSING  # left untouched -> accept right's deletion
            conflicts.append(
                RawConflict("delete_vs_modify", path, base, left, MISSING)
            )
            return _resolve_outcome(path, resolutions, base, base, left, MISSING)

        # Both sides present.
        if json_equal(left, right):
            return left  # both made the same change (or neither changed)

        # Atomic values (and arrays without identity configuration).
        if base is MISSING:
            conflicts.append(
                RawConflict("both_added", path, MISSING, left, right)
            )
            return _resolve_outcome(path, resolutions, left, MISSING, left, right)
        if json_equal(base, right):
            return left  # changed only on the left
        if json_equal(base, left):
            return right  # changed only on the right

        conflicts.append(RawConflict("both_modified", path, base, left, right))
        return _resolve_outcome(path, resolutions, base, base, left, right)

    def _merge_object(
        self,
        left: Any,
        right: Any,
        base: Any,
        path: tuple,
        conflicts: list[RawConflict],
        resolutions: dict[tuple, str],
        protected: frozenset[tuple] = frozenset(),
    ) -> Any:
        left_obj: dict = {} if left is MISSING else left
        right_obj: dict = {} if right is MISSING else right
        base_obj: dict = {} if base is MISSING else base
        result: dict[str, Any] = {}
        for key in sorted(set(base_obj) | set(left_obj) | set(right_obj)):
            b = base_obj.get(key, MISSING)
            l = left_obj.get(key, MISSING)
            r = right_obj.get(key, MISSING)
            merged = self._merge_value(
                b, l, r, path + (key,), conflicts, resolutions, protected
            )
            if merged is not MISSING:
                result[key] = merged
        return result

    def _protected_below(self, protected: frozenset[tuple], path: tuple) -> bool:
        """Whether *path* itself or any path beneath it is protected."""
        return any(p == path or p[: len(path)] == path for p in protected)

    def _protected_pattern_under(self, path: tuple) -> bool:
        """Whether a configured protected pattern can match *path* or a
        descendant of it (a concrete identity-array item step renders as
        ``"*"``)."""
        prefix = tuple(
            "*" if isinstance(step, IdMarker) else str(step) for step in path
        )

        def hit(tokens: tuple[str, ...]) -> bool:
            return tokens == prefix or tokens[: len(prefix)] == prefix

        return any(hit(t) for t in self._protected_exact) or any(
            hit(t) for t in self._protected_wild
        )

    def _merge_one_sided_object(
        self,
        base: Any,
        left: Any,
        right: Any,
        path: tuple,
        conflicts: list[RawConflict],
        resolutions: dict[tuple, str],
        protected: frozenset[tuple],
    ) -> Any:
        """Merge an object added or removed on only one side.

        This is one atomic structural unit: if a hit protected descendant
        lives inside it, the unit is never assembled field by field. Every
        hit protected path is reported (``protected_change``, missing sides
        as ``null``); the unit is kept wholesale as base when unresolved, or
        written wholesale from the chosen side when any hit carries an
        explicit resolution (the first hit in pointer order decides).
        Without a protected hit the ordinary atomic shortcut applies.
        """
        present = left if left is not MISSING else right
        hits = sorted(
            (p for p in protected if p == path or p[: len(path)] == path),
            key=_path_sort_key,
        )
        if hits:
            for hit_path in hits:
                b, l, r = _descend_at(base, left, right, hit_path[len(path):])
                conflicts.append(
                    RawConflict("protected_change", hit_path, b, l, r)
                )
            side = self._hit_resolution(hits, resolutions)
            if side == "left":
                return left
            if side == "right":
                return right
            # "base" or unresolved: the unit stays exactly as in base --
            # the whole block kept, or wholly absent when base had none.
            return base
        if base is MISSING:
            return present  # added on one side only
        # Deleted on one side; accept the deletion while the surviving side
        # leaves the object untouched, otherwise keep base and report the
        # unit-level delete-versus-modify conflict.
        survivor = left if left is not MISSING else right
        if json_equal(survivor, base):
            return MISSING
        conflicts.append(
            RawConflict("delete_vs_modify", path, base, left, right)
        )
        return _resolve_outcome(
            path, resolutions, base, base, left, right
        )

    @staticmethod
    def _hit_resolution(
        hits: list[tuple], resolutions: dict[tuple, str]
    ) -> Optional[str]:
        """Explicit resolution governing an atomic unit with protected hits.

        Hit paths are visited in pointer order; the first adjudicated one
        decides the whole unit.
        """
        for hit_path in sorted(hits, key=_path_sort_key):
            side = resolutions.get(hit_path)
            if side is not None:
                return side
        return None

    # ----------------------------------------------------- change scanning

    def _enumerate_changes(
        self, base: Any, left: Any, right: Any, path: tuple
    ) -> set[tuple]:
        """Every internal path at which either revision differs from base.

        Mirrors the merge structure but ignores shortcuts, so a one-sided or
        identical change still yields the paths of the fields it touches.
        """
        changed: set[tuple] = set()
        if json_equal(left, base) and json_equal(right, base):
            return changed
        changed.add(path)

        if isinstance(base, dict) and isinstance(left, dict) and isinstance(right, dict):
            dicts = (base, left, right)
        elif _revisions_as_dicts(base, left, right):
            # One-sided object add/delete or two-sided object addition.
            dicts = (
                base if isinstance(base, dict) else {},
                left if isinstance(left, dict) else {},
                right if isinstance(right, dict) else {},
            )
        else:
            dicts = None
        if dicts is not None:
            base_obj, left_obj, right_obj = dicts
            for key in set(base_obj) | set(left_obj) | set(right_obj):
                changed |= self._enumerate_changes(
                    base_obj.get(key, MISSING),
                    left_obj.get(key, MISSING),
                    right_obj.get(key, MISSING),
                    path + (key,),
                )
            return changed

        if (
            (base is MISSING or isinstance(base, list))
            and (left is MISSING or isinstance(left, list))
            and (right is MISSING or isinstance(right, list))
            and (left is not MISSING or right is not MISSING)
            and (id_key := self._identity_key(path)) is not None
            and (
                # Existing arrays are always enumerated; an array added on
                # one side only needs enumeration when a protected pattern
                # could reach an item below it.
                base is not MISSING
                or (
                    (left is MISSING or right is MISSING)
                    and self._protected_pattern_under(path)
                )
            )
        ):
            base_list = base if isinstance(base, list) else []
            left_list = left if isinstance(left, list) else []
            right_list = right if isinstance(right, list) else []
            if _identity_eligible(base_list, left_list, right_list, id_key):
                changed |= self._enumerate_identity_changes(
                    base_list, left_list, right_list, path, id_key
                )
        return changed

    def _enumerate_identity_changes(
        self,
        base: list,
        left: list,
        right: list,
        path: tuple,
        id_key: str,
    ) -> set[tuple]:
        """Changed item and field paths inside an identity-merged array."""
        changed: set[tuple] = set()
        base_map = _index_items(base, id_key)
        left_map = _index_items(left, id_key)
        right_map = _index_items(right, id_key)

        for canon_id, base_item in base_map.items():
            left_item = left_map.get(canon_id, MISSING)
            right_item = right_map.get(canon_id, MISSING)
            item_path = path + (IdMarker(id_key, base_item[id_key]),)
            if json_equal(left_item, base_item) and json_equal(right_item, base_item):
                continue
            changed.add(item_path)
            if left_item is MISSING and right_item is MISSING:
                # Deleted on both sides: the whole item goes away; it is not
                # part of one-sided structural changes.
                continue
            if left_item is not MISSING and right_item is not MISSING:
                # Fields merge individually while both revisions carry the
                # item, including one-sided untouched-but-present edits.
                for key in set(base_item) | set(left_item) | set(right_item):
                    changed |= self._enumerate_changes(
                        base_item.get(key, MISSING),
                        left_item.get(key, MISSING),
                        right_item.get(key, MISSING),
                        item_path + (key,),
                    )
            else:
                # Removed on one side: enumerate the surviving side against
                # base so a hit protected descendant of the deleted item is
                # detected and the item treated as one atomic unit.
                survivor = left_item if left_item is not MISSING else right_item
                for key in set(base_item) | set(survivor):
                    changed |= self._enumerate_changes(
                        base_item.get(key, MISSING),
                        (left_item if left_item is not MISSING else {}).get(
                            key, MISSING
                        ),
                        (right_item if right_item is not MISSING else {}).get(
                            key, MISSING
                        ),
                        item_path + (key,),
                    )

        new_ids = (set(left_map) | set(right_map)) - set(base_map)
        for canon_id in new_ids:
            left_item = left_map.get(canon_id, MISSING)
            right_item = right_map.get(canon_id, MISSING)
            present = left_item if left_item is not MISSING else right_item
            item_path = path + (IdMarker(id_key, present[id_key]),)
            changed.add(item_path)
            left_obj = left_item if left_item is not MISSING else {}
            right_obj = right_item if right_item is not MISSING else {}
            for key in set(left_obj) | set(right_obj):
                changed |= self._enumerate_changes(
                    MISSING,
                    left_obj.get(key, MISSING),
                    right_obj.get(key, MISSING),
                    item_path + (key,),
                )
        return changed

    def _merge_identity_array(
        self,
        base: list,
        left: list,
        right: list,
        path: tuple,
        id_key: str,
        conflicts: list[RawConflict],
        resolutions: dict[tuple, str],
        protected: frozenset[tuple] = frozenset(),
    ) -> list:
        base_map = _index_items(base, id_key)
        left_map = _index_items(left, id_key)
        right_map = _index_items(right, id_key)

        result: list[Any] = []
        for canon_id, base_item in base_map.items():
            left_item = left_map.get(canon_id, MISSING)
            right_item = right_map.get(canon_id, MISSING)
            marker = IdMarker(id_key, base_item[id_key])
            item_path = path + (marker,)

            if left_item is MISSING and right_item is MISSING:
                # Deletions are matched at their original path.
                if item_path in protected:
                    conflicts.append(
                        RawConflict(
                            "protected_change", item_path,
                            base_item, MISSING, MISSING,
                        )
                    )
                    result.append(base_item)
                continue  # deleted on both sides
            if left_item is MISSING:
                guarded = self._guarded_item_unit(
                    item_path, base_item, MISSING, right_item,
                    conflicts, resolutions, protected,
                )
                if guarded is not _UNGUARDED:
                    if guarded is not MISSING:
                        result.append(guarded)
                    continue
                if json_equal(right_item, base_item):
                    continue  # right untouched: accept the deletion
                # Deleted on the left, modified on the right: keep base.
                conflicts.append(
                    RawConflict(
                        "delete_vs_modify",
                        item_path,
                        base_item,
                        MISSING,
                        right_item,
                    )
                )
                chosen = _resolve_outcome(
                    item_path, resolutions,
                    base_item, base_item, MISSING, right_item,
                )
                if chosen is not MISSING:
                    result.append(chosen)
                continue
            if right_item is MISSING:
                guarded = self._guarded_item_unit(
                    item_path, base_item, left_item, MISSING,
                    conflicts, resolutions, protected,
                )
                if guarded is not _UNGUARDED:
                    if guarded is not MISSING:
                        result.append(guarded)
                    continue
                if json_equal(left_item, base_item):
                    continue  # left untouched: accept the deletion
                # Modified on the left, deleted on the right: keep base.
                conflicts.append(
                    RawConflict(
                        "delete_vs_modify",
                        item_path,
                        base_item,
                        left_item,
                        MISSING,
                    )
                )
                chosen = _resolve_outcome(
                    item_path, resolutions,
                    base_item, base_item, left_item, MISSING,
                )
                if chosen is not MISSING:
                    result.append(chosen)
                continue

            # Present on both sides. A protected item is frozen wholesale;
            # otherwise three-way merge the objects so independent field
            # edits are accepted and protected/conflicting fields reported
            # individually. Field-wise merging of the shortcut cases
            # produces the same item the old whole-item rules chose.
            guarded = self._guarded_item_unit(
                item_path, base_item, left_item, right_item,
                conflicts, resolutions, protected,
                descendants_atomic=False,
            )
            if guarded is not _UNGUARDED:
                if guarded is not MISSING:
                    result.append(guarded)
                continue
            merged_item = self._merge_object(
                left_item,
                right_item,
                base_item,
                item_path,
                conflicts,
                resolutions,
                protected,
            )
            result.append(merged_item)

        # Items present in base order were emitted first. Append items that
        # are new on one side (or both), sorted by identity value.
        new_ids = (set(left_map) | set(right_map)) - set(base_map)
        new_items: list[tuple[Any, Any]] = []
        for canon_id in new_ids:
            left_item = left_map.get(canon_id, MISSING)
            right_item = right_map.get(canon_id, MISSING)
            present = left_item if left_item is not MISSING else right_item
            marker = IdMarker(id_key, present[id_key])
            item_path = path + (marker,)

            # An item-level protected path freezes the whole addition.
            if item_path in protected:
                conflicts.append(
                    RawConflict(
                        "protected_change", item_path,
                        MISSING, left_item, right_item,
                    )
                )
                chosen = _resolve_outcome(
                    item_path, resolutions,
                    MISSING, MISSING, left_item, right_item,
                )
                if chosen is not MISSING:
                    new_items.append((chosen[id_key], chosen))
                continue
            descendant_hits = [
                p for p in protected if p[: len(item_path)] == item_path
            ]
            if descendant_hits and (left_item is MISSING or right_item is MISSING):
                # One-sided addition carrying a hit protected descendant:
                # the item is one atomic structural unit, never assembled
                # field by field. Unresolved it is withheld wholesale; an
                # explicit resolution for a hit protected path appends the
                # chosen side's whole item.
                guarded = self._guarded_item_unit(
                    item_path, MISSING, left_item, right_item,
                    conflicts, resolutions, protected,
                )
                if guarded is not _UNGUARDED and guarded is not MISSING:
                    new_items.append((guarded[id_key], guarded))
                continue
            if descendant_hits:
                # Both sides add the same new identity: independent fields
                # merge while the protected/divergent fields are reported
                # individually.
                merged_item = self._merge_new_item_fields(
                    left_item, right_item, item_path, id_key,
                    conflicts, resolutions, protected,
                )
                if merged_item is not MISSING:
                    new_items.append((merged_item[id_key], merged_item))
                continue

            if left_item is MISSING:
                new_items.append((right_item[id_key], right_item))
            elif right_item is MISSING:
                new_items.append((left_item[id_key], left_item))
            elif json_equal(left_item, right_item):
                new_items.append((left_item[id_key], left_item))
            else:
                # Both sides add the same new identity with different content.
                # The merged document contains the left value; the conflict
                # is reported on the parent array (the item has no stable
                # positional pointer before sorting). A resolution picks the
                # item to append ("base" appends nothing: the item is absent
                # from base).
                conflicts.append(
                    RawConflict("both_added", path, MISSING, left_item, right_item)
                )
                chosen = _resolve_outcome(
                    path, resolutions, left_item, MISSING, left_item, right_item
                )
                if chosen is not MISSING:
                    new_items.append((chosen[id_key], chosen))
        new_items.sort(key=lambda pair: _sort_key(pair[0]))
        result.extend(item for _, item in new_items)
        return result

    def _guarded_item_unit(
        self,
        item_path: tuple,
        base_item: Any,
        left_item: Any,
        right_item: Any,
        conflicts: list[RawConflict],
        resolutions: dict[tuple, str],
        protected: frozenset[tuple],
        descendants_atomic: bool = True,
    ) -> Any:
        """Apply protection to an identity-array item as one atomic unit.

        A hit at the item itself always protects the whole item. A hit at a
        descendant also makes the whole item atomic for one-sided
        add/remove shortcuts (*descendants_atomic*); for an item present on
        both sides only the item path itself freezes wholesale --
        descendant hits merge field by field instead.

        Every hit protected path is reported and the item is kept wholesale
        as base (or withheld wholesale when base had no such item), unless
        an explicit resolution for a hit protected path writes the chosen
        side's whole item. Returns ``_UNGUARDED`` when no hit protected
        path changed on either side.
        """
        hits = [
            p
            for p in protected
            if p == item_path
            or (descendants_atomic and p[: len(item_path)] == item_path)
        ]
        if not hits:
            return _UNGUARDED
        changed = False
        for hit_path in sorted(hits, key=_path_sort_key):
            b, l, r = _descend_at(
                base_item, left_item, right_item, hit_path[len(item_path):]
            )
            if not json_equal(l, b) or not json_equal(r, b):
                conflicts.append(
                    RawConflict("protected_change", hit_path, b, l, r)
                )
                changed = True
        if not changed:
            return _UNGUARDED
        side = self._hit_resolution(hits, resolutions)
        if side == "left":
            return left_item
        if side == "right":
            return right_item
        # "base" or unresolved: keep the base item wholesale (or withhold a
        # base-less addition wholesale).
        return base_item

    def _merge_new_item_fields(
        self,
        left_item: Any,
        right_item: Any,
        item_path: tuple,
        id_key: str,
        conflicts: list[RawConflict],
        resolutions: dict[tuple, str],
        protected: frozenset[tuple],
    ) -> Any:
        """Field-wise merge of an identified item absent from base.

        Only reached when a path below the item (but not the item itself)
        is protected: unprotected fields are added normally, protected
        fields are withheld and reported. An item without its identity
        field is not appended.
        """
        left_obj = left_item if left_item is not MISSING else {}
        right_obj = right_item if right_item is not MISSING else {}
        merged_item: dict[str, Any] = {}
        for key in sorted(set(left_obj) | set(right_obj)):
            chosen = self._merge_value(
                MISSING,
                left_obj.get(key, MISSING),
                right_obj.get(key, MISSING),
                item_path + (key,),
                conflicts,
                resolutions,
                protected,
            )
            if chosen is not MISSING:
                merged_item[key] = chosen
        if id_key not in merged_item:
            return MISSING
        return merged_item

    # ---------------------------------------------------- path resolution

    def _resolve_paths(
        self, kept: Any, plain: Any, base: Any, paths: list[tuple]
    ) -> list[str]:
        """Translate internal paths (with IdMarkers) into RFC 6901 pointers.

        Each path is first walked through *kept* (the protected merge); a
        marker missing there is sought in *plain* (the unprotected merge,
        e.g. a withheld new item) and finally in *base* (the original
        index of a deleted item: deletions are reported by original path).
        """
        return [_render_path(kept, plain, base, path) for path in paths]


# Sentinel: an identity-array item path carried no protection this pass.
_UNGUARDED: Any = object()


def _render_path(kept: Any, plain: Any, base: Any, path: tuple) -> str:
    """Render an internal path against the merge documents.

    Marker steps are located by identity, in order, in *kept* (the
    protected merge), *plain* (the unprotected merge, for withheld new
    items whose reported pointer is their would-be final position) and
    *base* (for deleted base items: deletions report their original path).
    The documents are traversed with independent cursors; once a document
    fails to resolve a step, later steps stay on the next fallback.
    """
    cursors: list[Any] = [kept, plain, base]
    steps: list[str] = []
    for step in path:
        if isinstance(step, (IdMarker, int)):
            # Locate the first document that still resolves this step.
            chosen: Optional[tuple[int, int, Any]] = None
            for i, node in enumerate(cursors):
                if not isinstance(node, list):
                    continue
                if isinstance(step, int):
                    idx = step if 0 <= step < len(node) else None
                else:
                    idx = _find_identity_or_none(node, step)
                if idx is not None:
                    chosen = (i, idx, node[idx])
                    break
            if chosen is None:
                raise KeyError(  # unreachable: markers always resolve somewhere
                    f"path step {step!r} not found in any merge document"
                )
            doc_index, idx, _ = chosen
            for i in range(len(cursors)):
                node = cursors[i]
                same = False
                if isinstance(node, list):
                    if isinstance(step, int):
                        same = 0 <= step < len(node)
                    else:
                        same = _find_identity_or_none(node, step) == idx
                cursors[i] = node[idx] if same else None
            steps.append(escape_token(str(idx)))
        else:
            token = str(step)
            for i, node in enumerate(cursors):
                cursors[i] = node[token] if isinstance(node, dict) and token in node else None
            steps.append(escape_token(token))
    return "" if not steps else "/" + "/".join(steps)


# ---------------------------------------------------------------- helpers


def parse_pointer(pointer: str) -> tuple[str, ...]:
    """Parse a JSON Pointer (RFC 6901) into reference tokens.

    ``""`` denotes the whole document. A leading ``"#"`` (URI fragment form)
    and fragment percent-decoding are intentionally not supported.
    """
    if not isinstance(pointer, str):
        raise ValueError(f"JSON Pointer must be a string, got {type(pointer).__name__}")
    if pointer == "":
        return ()
    if not pointer.startswith("/"):
        raise ValueError(
            f"invalid JSON Pointer {pointer!r}: non-empty pointers must start with '/'"
        )
    tokens: list[str] = []
    for raw in pointer.split("/")[1:]:
        token = raw.replace("~1", "/").replace("~0", "~")
        tokens.append(token)
    return tuple(tokens)


def escape_token(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def json_equal(a: Any, b: Any) -> bool:
    """Structural equality with JSON semantics.

    Unlike Python's ``==``, booleans never compare equal to numbers, and the
    ``MISSING`` sentinel is only equal to itself.
    """
    if a is MISSING or b is MISSING:
        return a is b
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    if isinstance(a, str) or isinstance(b, str):
        return isinstance(a, str) and isinstance(b, str) and a == b
    if isinstance(a, dict) and isinstance(b, dict):
        if a.keys() != b.keys():
            return False
        return all(json_equal(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(json_equal(x, y) for x, y in zip(a, b))
    return a == b


def _identity_eligible(base: list, left: list, right: list, id_key: str) -> bool:
    """Identity merge requires every item (on every side) to be an object
    carrying the identity field, with no duplicated identity per side."""
    for side in (base, left, right):
        seen: set[str] = set()
        for item in side:
            if not isinstance(item, dict) or id_key not in item:
                return False
            canon = _canonical_id(item[id_key])
            if canon in seen:
                return False
            seen.add(canon)
    return True


def _canonical_id(value: Any) -> str:
    # sort_keys keeps {"a":1,"b":2} and {"b":2,"a":1} equivalent; such a
    # composite identity is unusual but the ordering must be total.
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _index_items(items: list, id_key: str) -> "OrderedDict[str, dict]":
    indexed: "OrderedDict[str, dict]" = OrderedDict()
    for item in items:
        indexed[_canonical_id(item[id_key])] = item
    return indexed


def _sort_key(value: Any):
    """Total ordering over JSON scalar identity values.

    Ordering: null < false < true < numbers < strings. Numbers sort by their
    numeric value. Strings sort lexicographically.
    """
    if value is None:
        return (0,)
    if isinstance(value, bool):
        return (1, int(value))
    if isinstance(value, (int, float)):
        return (2, float(value))
    if isinstance(value, str):
        return (3, value)
    # Composite (array/object) identity: fall back to canonical JSON text.
    return (4, _canonical_id(value))


def _public(value: Any) -> Any:
    """Render an internal value for the API response (MISSING -> null)."""
    return None if value is MISSING else value


def _resolve_outcome(
    path: tuple,
    resolutions: dict[tuple, str],
    default: Any,
    base: Any,
    left: Any,
    right: Any,
) -> Any:
    """Value placed at a conflict location.

    With a resolution for *path* the chosen side's value wins (MISSING means
    the location is deleted / stays absent); without one the conflict's
    default outcome (*default*) applies.
    """
    side = resolutions.get(path)
    if side == "base":
        return base
    if side == "left":
        return left
    if side == "right":
        return right
    return default


def _unknown_resolution_message(pointer: str, tokens: tuple[str, ...], merged: Any) -> str:
    """Explain why a resolution pointer matched no reported conflict."""
    if _pointer_exists(merged, tokens):
        return (
            f"conflict resolution pointer {pointer!r} does not correspond "
            "to a reported conflict"
        )
    return (
        f"conflict resolution pointer {pointer!r} does not exist "
        "in the merged document"
    )


def _pointer_exists(document: Any, tokens: tuple[str, ...]) -> bool:
    """Whether an RFC 6901 pointer addresses a location in *document*."""
    node = document
    for token in tokens:
        if isinstance(node, dict):
            if token not in node:
                return False
            node = node[token]
        elif isinstance(node, list):
            index = _array_index(token)
            if index is None or index >= len(node):
                return False
            node = node[index]
        else:
            return False
    return True


def _array_index(token: str) -> Optional[int]:
    """RFC 6901 array index: ``0`` or digits without a leading zero."""
    if token == "0":
        return 0
    if token and token[0] in "123456789" and token.isdigit():
        return int(token)
    return None


def _revisions_as_dicts(base: Any, left: Any, right: Any) -> bool:
    """Whether the three values should be merged field by field.

    True when every present side is an object: objects against an object
    base (ordinary object merge), objects added where base was absent
    (one- or two-sided additions, entered to visit protected fields
    underneath), or an object deleted on one side.
    """
    if not isinstance(base, dict) and base is not MISSING:
        return False
    return (left is MISSING or isinstance(left, dict)) and (
        right is MISSING or isinstance(right, dict)
    ) and (left is not MISSING or right is not MISSING)


def _descend_at(base: Any, left: Any, right: Any, rel: tuple) -> tuple:
    """Three-way values beneath a structural unit at relative path *rel*.

    Dicts are followed by key; identity markers locate their item inside a
    nested identity array. A missing key/item or a non-matching node is
    rendered as :data:`MISSING`. Used to report the three-way values of
    protected descendants without field-wise merging their unit.
    """

    def follow(node: Any) -> Any:
        for step in rel:
            if isinstance(step, IdMarker):
                idx = _find_identity_or_none(node, step)
                node = node[idx] if idx is not None else MISSING
            elif isinstance(node, dict):
                node = node.get(step, MISSING)
            else:
                return MISSING
        return node

    return follow(base), follow(left), follow(right)


def _path_sort_key(path: tuple):
    """Total, deterministic ordering over internal paths.

    Object keys are strings and identity markers carry JSON scalars; both
    are rendered to comparable text (markers as ``"*"``), so a tuple of
    mixed step types never raises during sorting.
    """
    return tuple(
        ("*",) if isinstance(step, IdMarker) else ("k", str(step))
        for step in path
    )


def _find_identity_or_none(items: Any, marker: IdMarker) -> Optional[int]:
    """Index of the item carrying *marker*'s identity, or ``None``."""
    if not isinstance(items, list):
        return None
    canon = _canonical_id(marker.id_value)
    for i, item in enumerate(items):
        if (
            isinstance(item, dict)
            and marker.id_key in item
            and _canonical_id(item[marker.id_key]) == canon
        ):
            return i
    return None
