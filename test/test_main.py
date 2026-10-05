"""
Tests for the main entrypoint
"""

# Standard
from contextlib import contextmanager
import json
import logging
import os
import sys
import tempfile

# Third Party
import pytest

# Local
from import_tracker import constants
from import_tracker.__main__ import main

## Helpers #####################################################################


@contextmanager
def cli_args(*args):
    """Wrapper to set the sys.argv for the enclosed context"""
    prev_argv = sys.argv
    sys.argv = ["dummy_script"] + list(args)
    yield
    sys.argv = prev_argv


## Tests #######################################################################


def test_without_package(capsys):
    """Run the main function against the sample lib and check the output"""
    with cli_args("--name", "sample_lib.submod1"):
        main()
    captured = capsys.readouterr()
    assert captured.out
    parsed_out = json.loads(captured.out)
    assert list(parsed_out.keys()) == ["sample_lib.submod1"]
    assert (set(parsed_out["sample_lib.submod1"])) == {"conditional_deps"}


def test_with_package(capsys):
    """Run the main function with a package argument"""
    with cli_args("--name", ".submod1", "--package", "sample_lib"):
        main()
    captured = capsys.readouterr()
    assert captured.out
    parsed_out = json.loads(captured.out)
    assert list(parsed_out.keys()) == ["sample_lib.submod1"]
    assert (set(parsed_out["sample_lib.submod1"])) == {"conditional_deps"}


def test_file_without_parent_path(capsys):
    """Check that the corner case of __file__ being unset is caught"""
    with cli_args("--name", "google.protobuf"):
        main()
    captured = capsys.readouterr()
    assert captured.out
    parsed_out = json.loads(captured.out)

    # Just check the keys. The values are funky because of this being run from
    # within a test
    assert list(parsed_out.keys()) == ["google.protobuf"]


def test_with_logging(capsys):
    """Run the main function with logging turned up and make sure the output is
    not changed
    """
    with cli_args(
        "--name", "sample_lib.submod1", "--log_level", str(logging.DEBUG - 3)
    ):
        main()
    captured = capsys.readouterr()
    assert captured.err
    assert captured.out
    parsed_out = json.loads(captured.out)
    assert list(parsed_out.keys()) == ["sample_lib.submod1"]
    assert (set(parsed_out["sample_lib.submod1"])) == {"conditional_deps"}


def test_import_time_error(capsys):
    """Check that an exception from the imported module is forwarded"""
    with cli_args("--name", "bad_lib"):
        with pytest.raises(RuntimeError):
            main()


def test_submodule_error(capsys):
    """Check that an exception from a submodule is forwarded"""
    with cli_args("--name", "bad_lib"):
        with pytest.raises(RuntimeError):
            main()


def test_sibling_import(capsys):
    """Make sure that a library with a submodule that imports a sibling
    submodule properly tracks dependencies through the sibling
    """
    with cli_args("--name", "inter_mod_deps", "--submodules"):
        main()
    captured = capsys.readouterr()
    assert captured.out
    parsed_out = json.loads(captured.out)
    assert (set(parsed_out["inter_mod_deps.submod1"])) == {"alog"}
    assert (set(parsed_out["inter_mod_deps.submod2"])) == {
        "alog",
        "yaml",
    }
    assert (set(parsed_out["inter_mod_deps.submod2.foo"])) == {
        "yaml",
    }
    assert (set(parsed_out["inter_mod_deps.submod2.bar"])) == {
        "yaml",
    }
    assert (set(parsed_out["inter_mod_deps.submod3"])) == {
        "alog",
        "yaml",
    }
    assert (set(parsed_out["inter_mod_deps.submod4"])) == {
        "yaml",
    }
    assert (set(parsed_out["inter_mod_deps.submod5"])) == {
        "yaml",
    }


def test_lib_with_lazy_imports(capsys):
    """Make sure that a library which uses import_tracker's lazy import errors
    and has "traditional" conditional dependencies does not blow up when tracked
    """
    with cli_args("--name", "lazy_import_errors"):
        main()
    captured = capsys.readouterr()
    assert captured.out
    parsed_out = json.loads(captured.out)
    assert "lazy_import_errors" in parsed_out


def test_tree_output(capsys):
    """--tree emits a structured dependency tree rather than the flat map"""
    with cli_args("--name", "cyclic_deps", "--tree"):
        main()
    captured = capsys.readouterr()
    parsed_out = json.loads(captured.out)
    assert parsed_out["root"] == "cyclic_deps"
    edge_pairs = {(edge["src"], edge["dst"]) for edge in parsed_out["edges"]}
    assert edge_pairs == {
        ("cyclic_deps", "cyclic_deps.submod_a"),
        ("cyclic_deps.submod_a", "cyclic_deps.submod_b"),
        ("cyclic_deps.submod_b", "cyclic_deps.submod_a"),
    }
    back_edges = [edge for edge in parsed_out["edges"] if edge["kind"] == "back"]
    assert len(back_edges) == 1
    assert (back_edges[0]["src"], back_edges[0]["dst"]) == (
        "cyclic_deps.submod_b",
        "cyclic_deps.submod_a",
    )


def test_tree_depth_zero(capsys):
    """--depth 0 keeps only the root and marks it truncated"""
    with cli_args("--name", "cyclic_deps", "--tree", "--depth", "0"):
        main()
    parsed_out = json.loads(capsys.readouterr().out)
    node_names = {node["name"] for node in parsed_out["nodes"]}
    assert node_names == {"cyclic_deps"}
    assert parsed_out["nodes"][0]["truncated"] is True
    assert parsed_out["edges"] == []


def test_tree_depth_beyond_actual(capsys):
    """A depth larger than the real tree exports everything untruncated"""
    with cli_args("--name", "cyclic_deps", "--tree", "--depth", "99"):
        main()
    parsed_out = json.loads(capsys.readouterr().out)
    assert len(parsed_out["nodes"]) == 3
    assert len(parsed_out["edges"]) == 3
    assert all(not node["truncated"] for node in parsed_out["nodes"])


def test_tree_depth_two_shows_cycle_and_truncation(capsys):
    """At depth 2 the cycle back edge stays visible and the frontier node is
    marked truncated"""
    with cli_args("--name", "cyclic_deps", "--tree", "--depth", "2"):
        main()
    parsed_out = json.loads(capsys.readouterr().out)
    truncated = {node["name"] for node in parsed_out["nodes"] if node["truncated"]}
    assert truncated == {"cyclic_deps.submod_b"}
    kinds = {(edge["src"], edge["dst"]): edge["kind"] for edge in parsed_out["edges"]}
    assert kinds[("cyclic_deps.submod_b", "cyclic_deps.submod_a")] == "back"


def test_html_report_written(capsys):
    """--html writes an openable report and still prints the JSON tree"""
    with tempfile.TemporaryDirectory() as workdir:
        html_path = os.path.join(workdir, "report.html")
        with cli_args(
            "--name",
            "cyclic_deps",
            "--tree",
            "--depth",
            "2",
            "--html",
            html_path,
        ):
            main()
        captured = capsys.readouterr()
        assert json.loads(captured.out)
        assert os.path.exists(html_path)
        with open(html_path, "r", encoding="utf-8") as handle:
            content = handle.read()
        assert content.lstrip().startswith("<!DOCTYPE html>")
        assert "cyclic_deps.submod_a" in content
        assert "cyclic_deps.submod_b" in content
        assert "back (cycle)" in content
        # The edge table lists every exported edge exactly once (one data row
        # each plus the header row)
        assert content.count("<tr") == 4


def test_html_requires_tree():
    """--html without --tree is a usage error"""
    with cli_args("--name", "cyclic_deps", "--html", "report.html"):
        with pytest.raises(ValueError):
            main()
