"""
Tests for the dependency-tree recorder (idempotent edges, cycles, depth
truncation and nested tracking contexts).
"""

# Third Party
import pytest

# Local
from import_tracker import constants, track_imports
from import_tracker.dependency_tree import DependencyTree, active_tree
from import_tracker.import_tracker import track_module

## DependencyTree unit tests ##################################################


def test_duplicate_edges_are_idempotent():
    """Repeated add_edge calls for the same pair must not duplicate edges"""
    tree = DependencyTree()
    for _ in range(5):
        tree.add_edge("a", "b")
    exported = tree.export("a")
    assert exported[constants.TREE_EDGES] == [
        {
            constants.TREE_EDGE_SRC: "a",
            constants.TREE_EDGE_DST: "b",
            constants.TREE_EDGE_KIND: constants.TREE_EDGE_FORWARD,
            constants.TREE_EDGE_OPTIONAL: False,
        }
    ]
    assert {node["name"] for node in exported[constants.TREE_NODES]} == {"a", "b"}


def test_self_reference_is_a_single_back_edge():
    """A module importing itself yields one back edge, not a duplicate or loop"""
    tree = DependencyTree()
    tree.add_edge("a", "a")
    tree.add_edge("a", "a")
    exported = tree.export("a")
    edges = exported[constants.TREE_EDGES]
    assert len(edges) == 1
    assert edges[0][constants.TREE_EDGE_KIND] == constants.TREE_EDGE_BACK


def test_a_b_a_cycle_edges_dedup_and_marked():
    """An A->B->A cycle produces two distinct edges with the cycle edge marked"""
    tree = DependencyTree()
    tree.add_edge("a", "b")
    tree.add_edge("b", "a")
    tree.add_edge("a", "b")
    tree.add_edge("b", "a")
    exported = tree.export("a")
    kinds = {
        (e[constants.TREE_EDGE_SRC], e[constants.TREE_EDGE_DST]): e[
            constants.TREE_EDGE_KIND
        ]
        for e in exported[constants.TREE_EDGES]
    }
    assert set(kinds.keys()) == {("a", "b"), ("b", "a")}
    assert kinds[("a", "b")] == constants.TREE_EDGE_FORWARD
    assert kinds[("b", "a")] == constants.TREE_EDGE_BACK


def test_forward_shortcut_edge_is_not_marked_as_cycle():
    """A layered diamond's shortcut edge is forward even within a connected SCC
    span (it does not point back to an ancestor)"""
    tree = DependencyTree()
    tree.add_edge("a", "b")
    tree.add_edge("b", "c")
    tree.add_edge("a", "c")
    exported = tree.export("a")
    kinds = {
        (e[constants.TREE_EDGE_SRC], e[constants.TREE_EDGE_DST]): e[
            constants.TREE_EDGE_KIND
        ]
        for e in exported[constants.TREE_EDGES]
    }
    assert all(kind == constants.TREE_EDGE_FORWARD for kind in kinds.values())


## Depth truncation ###########################################################


def _chain_tree():
    tree = DependencyTree()
    tree.add_edge("r", "a")
    tree.add_edge("a", "b")
    tree.add_edge("b", "c")
    tree.add_edge("c", "d")
    return tree


def test_depth_zero_export_keeps_only_root_marked_truncated():
    """Boundary: depth 0 keeps the root and marks it truncated, with no edges"""
    exported = _chain_tree().export("r", max_depth=0)
    nodes = {n["name"]: n for n in exported[constants.TREE_NODES]}
    assert set(nodes.keys()) == {"r"}
    assert nodes["r"][constants.TREE_NODE_TRUNCATED] is True
    assert nodes["r"][constants.TREE_NODE_DEPTH] == 0
    assert exported[constants.TREE_EDGES] == []


def test_depth_beyond_actual_depth_exports_everything_untruncated():
    """Boundary: a depth larger than the real tree has no truncation"""
    tree = _chain_tree()
    exported = tree.export("r", max_depth=99)
    nodes = {n["name"]: n for n in exported[constants.TREE_NODES]}
    assert set(nodes.keys()) == {"r", "a", "b", "c", "d"}
    assert all(not n[constants.TREE_NODE_TRUNCATED] for n in nodes.values())
    assert len(exported[constants.TREE_EDGES]) == 4


def test_raw_edges_are_shared_across_multiple_depths():
    """Truncation is export-only: one tree serves many depth reports without
    them interfering"""
    tree = _chain_tree()
    depth1 = tree.export("r", max_depth=1)
    depth2 = tree.export("r", max_depth=2)
    full = tree.export("r", max_depth=None)

    assert len(depth1[constants.TREE_EDGES]) == 1
    assert len(depth2[constants.TREE_EDGES]) == 2
    assert len(full[constants.TREE_EDGES]) == 4

    # The frontier node is retained and flagged truncated at each cut
    d1_nodes = {n["name"]: n for n in depth1[constants.TREE_NODES]}
    assert d1_nodes["a"][constants.TREE_NODE_TRUNCATED] is True
    d2_nodes = {n["name"]: n for n in depth2[constants.TREE_NODES]}
    assert d2_nodes["b"][constants.TREE_NODE_TRUNCATED] is True
    assert d2_nodes["a"][constants.TREE_NODE_TRUNCATED] is False


def test_cycle_remains_visible_at_shallow_depth():
    """A cycle back edge between visible nodes survives depth truncation"""
    tree = DependencyTree()
    tree.add_edge("r", "a")
    tree.add_edge("a", "b")
    tree.add_edge("b", "a")
    exported = tree.export("r", max_depth=2)
    kinds = {
        (e[constants.TREE_EDGE_SRC], e[constants.TREE_EDGE_DST]): e[
            constants.TREE_EDGE_KIND
        ]
        for e in exported[constants.TREE_EDGES]
    }
    assert kinds[("b", "a")] == constants.TREE_EDGE_BACK


## track_module integration ###################################################


def test_track_module_cyclic_sample_lib_tree():
    """The cyclic_deps sample lib records exactly one back edge and no dupes"""
    tree = track_module("cyclic_deps", track_tree=True)
    pairs = {
        (e[constants.TREE_EDGE_SRC], e[constants.TREE_EDGE_DST])
        for e in tree[constants.TREE_EDGES]
    }
    assert pairs == {
        ("cyclic_deps", "cyclic_deps.submod_a"),
        ("cyclic_deps.submod_a", "cyclic_deps.submod_b"),
        ("cyclic_deps.submod_b", "cyclic_deps.submod_a"),
    }
    kinds = {
        (e[constants.TREE_EDGE_SRC], e[constants.TREE_EDGE_DST]): e[
            constants.TREE_EDGE_KIND
        ]
        for e in tree[constants.TREE_EDGES]
    }
    assert kinds[("cyclic_deps.submod_b", "cyclic_deps.submod_a")] == "back"
    assert kinds[("cyclic_deps", "cyclic_deps.submod_a")] == "forward"


def test_track_module_duplicate_imports_sample_lib():
    """submod_b imports submod_a twice but only one edge is recorded"""
    tree = track_module("cyclic_deps", track_tree=True)
    b_to_a = [
        edge
        for edge in tree[constants.TREE_EDGES]
        if edge[constants.TREE_EDGE_SRC] == "cyclic_deps.submod_b"
        and edge[constants.TREE_EDGE_DST] == "cyclic_deps.submod_a"
    ]
    assert len(b_to_a) == 1


def test_ignore_stdlib_switch():
    """include/exclude standard-library modules via ignore_stdlib"""
    without_stdlib = track_module(
        "sample_lib.nested.submod3", track_tree=True, ignore_stdlib=True
    )
    destinations = {
        edge[constants.TREE_EDGE_DST] for edge in without_stdlib[constants.TREE_EDGES]
    }
    assert "json" not in destinations

    with_stdlib = track_module(
        "sample_lib.nested.submod3", track_tree=True, ignore_stdlib=False
    )
    destinations = {
        edge[constants.TREE_EDGE_DST] for edge in with_stdlib[constants.TREE_EDGES]
    }
    assert "json" in destinations


## Nested contexts ############################################################


def test_track_imports_records_edges_in_context():
    """A track_imports context captures the tree for imports made inside"""
    with track_imports() as tree:
        track_module("sample_lib")
        assert active_tree() is tree
    pairs = {
        (e[constants.TREE_EDGE_SRC], e[constants.TREE_EDGE_DST])
        for e in tree.export("sample_lib")[constants.TREE_EDGES]
    }
    assert ("sample_lib", "sample_lib.submod1") in pairs
    assert active_tree() is None


def test_nested_context_inner_exception_restores_outer():
    """When the inner context raises, the outer context is restored and exits
    cleanly without itself raising"""
    with track_imports("outer") as outer:
        track_module("sample_lib")
        with pytest.raises(ValueError):
            with track_imports("inner") as inner:
                assert active_tree() is inner
                raise ValueError("boom")
        assert active_tree() is outer
    assert active_tree() is None
    assert len(outer.edges) > 0


def test_nested_contexts_keep_independent_trees():
    """Inner tracking records into the inner tree; the outer tree is untouched
    while the inner context is active, then resumes afterwards"""
    with track_imports("outer") as outer:
        with track_imports("inner") as inner:
            track_module("sample_lib")
        assert active_tree() is outer
        assert len(inner.edges) > 0
        # The outer context was paused while the inner one was active
        assert len(outer.edges) == 0
        track_module("sample_lib")
    assert len(outer.edges) > 0
