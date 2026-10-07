"""
Tests for the dependency tree recording in track_module / track_imports and
the tree export (JSON dict + standalone HTML report)
"""

# Standard
import builtins
import json

# Third Party
import pytest

# Local
from import_tracker import constants
from import_tracker.import_tracker import (
    ImportTree,
    get_last_tree,
    track_imports,
    track_module,
)


def _edges_from(tree, importer):
    """Get the sorted out-edge targets recorded for the given importer"""
    return sorted(tree.out_edges.get(importer, ()))


## Recording: idempotency ######################################################


def test_track_imports_duplicate_imports_idempotent():
    """Importing the same module repeatedly inside a track_imports context
    must only ever produce a single edge
    """
    with track_imports() as tree:
        # Local
        import conditional_deps  # pylint: disable=unused-import

    assert _edges_from(tree, __name__).count("conditional_deps") == 1
    assert list(tree.edges).count((__name__, "conditional_deps")) == 1


def test_track_imports_self_reference_idempotent():
    """A module that imports itself must produce exactly one self edge and
    exporting the tree must terminate
    """
    with track_imports() as tree:
        # Third Party
        import self_ref  # noqa: F401

    assert _edges_from(tree, "self_ref").count("self_ref") == 1
    report = tree.to_dict()
    assert ["self_ref", "self_ref"] in report["cycles"]
    # The self edge shows up exactly once in the raw edges
    assert report["edges"].count(["self_ref", "self_ref"]) == 1


def test_track_imports_circular_dependency():
    """An A -> B -> A import cycle must record each edge once and must not
    cause runaway recursion when computing depths or exporting
    """
    with track_imports() as tree:
        # Third Party
        import cyclic_deps.a  # noqa: F401

    # Both directions of the cycle recorded exactly once
    assert list(tree.edges).count(("cyclic_deps.a", "cyclic_deps.b")) == 1
    assert list(tree.edges).count(("cyclic_deps.b", "cyclic_deps.a")) == 1

    # Depths are finite and relative to the root
    depth_map = tree.depths()
    assert depth_map["cyclic_deps.a"] == 1
    assert depth_map["cyclic_deps.b"] == 2

    # The cycle is detected and marked at export
    report = tree.to_dict()
    assert ["cyclic_deps.b", "cyclic_deps.a"] in report["cycles"]


def test_track_module_records_tree_with_cycle():
    """track_module records a dependency tree (available via get_last_tree)
    and handles circular imports without runaway recursion
    """
    track_module("cyclic_deps.a")
    tree = get_last_tree()
    assert isinstance(tree, ImportTree)
    assert tree.roots == ["cyclic_deps.a"]
    assert ("cyclic_deps.a", "cyclic_deps.b") in tree.edges
    assert ("cyclic_deps.b", "cyclic_deps.a") in tree.edges
    report = tree.to_dict()
    assert ["cyclic_deps.b", "cyclic_deps.a"] in report["cycles"]


def test_track_module_tree_ignores_stdlib_by_default():
    """Standard library imports are not recorded as tree edges unless the
    ignore-stdlib switch is flipped
    """
    track_module("sample_lib.nested.submod3")
    tree = get_last_tree()
    # submod3 imports the standard json module which must be ignored
    assert "json" not in tree.nodes
    assert all(dst != "json" for _, dst in tree.edges)

    # With the switch off, the stdlib edge is recorded
    track_module("sample_lib.nested.submod3", ignore_stdlib=False)
    tree = get_last_tree()
    assert ("sample_lib.nested.submod3", "json") in tree.edges


## Nesting #####################################################################


def test_track_imports_nested_contexts():
    """Nested track_imports contexts record independently and the outer
    context keeps recording after the inner one exits
    """
    original_import = builtins.__import__
    with track_imports() as outer_tree:
        # Local
        import conditional_deps  # noqa: F401

        with track_imports() as inner_tree:
            # Third Party
            import optional_deps  # noqa: F401

        # The inner context restored the outer hook, not the original one
        assert builtins.__import__ is not original_import
        # Third Party
        import single_extra  # noqa: F401

    # Exiting the outer context restored the original import hook
    assert builtins.__import__ is original_import

    # The inner tree saw the inner import (and the outer tree saw everything)
    assert any(dst == "optional_deps" for _, dst in inner_tree.edges)
    assert any(dst == "optional_deps" for _, dst in outer_tree.edges)
    assert any(dst == "conditional_deps" for _, dst in outer_tree.edges)
    assert any(dst == "single_extra" for _, dst in outer_tree.edges)
    assert not any(dst == "single_extra" for _, dst in inner_tree.edges)


def test_track_imports_inner_exception_restores_state():
    """When the inner context raises, exiting the outer context must still
    restore the original import hook
    """
    original_import = builtins.__import__
    with track_imports() as outer_tree:
        with pytest.raises(RuntimeError, match="boom"):
            with track_imports():
                # Local
                import conditional_deps  # noqa: F401

                raise RuntimeError("boom")
        # The failed inner context restored the outer hook
        assert builtins.__import__ is not original_import
        # Third Party
        import single_extra  # noqa: F401

    assert builtins.__import__ is original_import
    assert any(dst == "single_extra" for _, dst in outer_tree.edges)


## Export: depth truncation ####################################################


def test_tree_export_depth_zero():
    """With max_depth=0, only the roots are fully expanded and every node
    beyond the roots is kept but marked truncated
    """
    with track_imports(roots=["cyclic_deps.a"]) as tree:
        # Third Party
        import cyclic_deps.a  # noqa: F401

    report = tree.to_dict(max_depth=0)
    assert report["max_depth"] == 0
    assert report["nodes"]["cyclic_deps.a"]["depth"] == 0
    assert report["nodes"]["cyclic_deps.a"]["truncated"] is False
    assert report["nodes"]["cyclic_deps.b"]["truncated"] is True
    assert report["nodes"]["cyclic_deps"]["truncated"] is True

    # The raw edges are still exported in full (truncation is export-time
    # marking only, the recording is untouched)
    assert ["cyclic_deps.a", "cyclic_deps.b"] in report["edges"]
    assert ["cyclic_deps.b", "cyclic_deps.a"] in report["edges"]


def test_tree_export_depth_exceeds_actual():
    """With a max_depth beyond the actual tree depth, nothing is truncated"""
    with track_imports(roots=["cyclic_deps.a"]) as tree:
        # Third Party
        import cyclic_deps.a  # noqa: F401

    report = tree.to_dict(max_depth=100)
    assert report["max_depth"] == 100
    assert not any(info["truncated"] for info in report["nodes"].values())


def test_tree_export_same_data_multiple_depths():
    """The same recording can be exported at multiple depth settings without
    the exports interfering with each other
    """
    with track_imports(roots=["cyclic_deps.a"]) as tree:
        # Third Party
        import cyclic_deps.a  # noqa: F401

    shallow = tree.to_dict(max_depth=0)
    deep = tree.to_dict(max_depth=100)
    again = tree.to_dict(max_depth=0)

    # Raw edges identical across exports; only truncation flags differ
    assert shallow["edges"] == deep["edges"]
    assert shallow == again
    assert shallow["nodes"]["cyclic_deps.b"]["truncated"] is True
    assert deep["nodes"]["cyclic_deps.b"]["truncated"] is False


def test_tree_export_default_max_depth_from_constants():
    """The export defaults to constants.MAX_DEPTH"""
    tree = ImportTree()
    tree.add_root("root_mod")
    tree.add_edge("root_mod", "child_mod")
    assert tree.to_dict()["max_depth"] == constants.MAX_DEPTH


## Export: HTML report ##########################################################


def test_html_report_marks_cycles_and_truncation(tmp_path):
    """The standalone HTML report marks cycle edges and truncated nodes"""
    with track_imports(roots=["sample_lib"]) as tree:
        # Local
        import sample_lib  # noqa: F401

    html_path = tmp_path / "report.html"
    tree.write_html(str(html_path), max_depth=1)
    content = html_path.read_text(encoding="utf-8")

    # The tree is rendered with cycle and truncation markers
    assert "cycle" in content
    assert "truncated" in content
    assert "sample_lib.submod1" in content
    assert "conditional_deps" in content

    # The summary line carries the raw edge count for cross-checking
    assert f"Edges: {len(tree.edges)}" in content


def test_html_report_is_json_consistent(tmp_path):
    """The JSON export and the HTML export of the same tree agree on the
    node and edge counts
    """
    with track_imports(roots=["cyclic_deps.a"]) as tree:
        # Third Party
        import cyclic_deps.a  # noqa: F401

    report = tree.to_dict(max_depth=2)
    html_path = tmp_path / "tree.html"
    tree.write_html(str(html_path), max_depth=2)
    content = html_path.read_text(encoding="utf-8")
    assert f"Nodes: {len(report['nodes'])}" in content
    assert f"Edges: {len(report['edges'])}" in content
    # The JSON export round-trips
    assert json.loads(json.dumps(report)) == report
