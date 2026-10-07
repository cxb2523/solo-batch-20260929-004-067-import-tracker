"""
This main entrypoint allows import_tracker to run as an independent script to
track the imports for a given module.

Example Usage:

# Track a single module
python -m import_tracker --name my_library

# Track a module and all of the sub-modules it contains
python -m import_tracker --name my_library --recursive --num_jobs 2

# Track a module with relative import syntax
python -m import_tracker --name .my_sub_module --package my_library

# Export the dependency tree (defaults to this package when no name is given)
# with depth truncation and a standalone HTML report
python -m import_tracker --tree --depth 2 --html /tmp/report.html
"""

# Standard
import argparse
import json
import logging
import os

# Local
from . import constants
from .import_tracker import get_last_tree, track_module

## Main ########################################################################


def main():
    """Main entrypoint as a function"""

    # Set up the args
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--name",
        "-n",
        default=None,
        help="Module name to track (defaults to this package in --tree mode)",
    )
    parser.add_argument(
        "--package",
        "-p",
        help="Package for relative imports",
        default=None,
    )
    parser.add_argument(
        "--indent",
        "-i",
        type=int,
        help="Indent for json printing",
        default=None,
    )
    parser.add_argument(
        "--submodules",
        "-s",
        nargs="*",
        default=None,
        help="List of subodules to include (all if no value given)",
    )
    parser.add_argument(
        "--track_import_stack",
        "-t",
        action="store_true",
        default=False,
        help="Store the stack trace of imports belonging to the tracked module",
    )
    parser.add_argument(
        "--full_depth",
        "-f",
        action="store_true",
        default=False,
        help="Include transitive third-party deps brought in by direct third-party deps",
    )
    parser.add_argument(
        "--detect_transitive",
        "-d",
        action="store_true",
        default=False,
        help="Detect whether each dependency is 'direct' or 'transitive'",
    )
    parser.add_argument(
        "--show_optional",
        "-o",
        action="store_true",
        default=False,
        help="Show whether each dependency is optional or required",
    )
    parser.add_argument(
        "--tree",
        action="store_true",
        default=False,
        help="Export the recorded dependency tree instead of the flattened "
        "dependency mapping",
    )
    parser.add_argument(
        "--depth",
        type=int,
        default=constants.MAX_DEPTH,
        help="Maximum depth for the exported dependency tree. Nodes beyond "
        "this depth are kept and marked as truncated.",
    )
    parser.add_argument(
        "--html",
        default=None,
        help="Write a standalone HTML report of the dependency tree to this "
        "path (implies --tree)",
    )
    parser.add_argument(
        "--include_stdlib",
        action="store_true",
        default=False,
        help="Include standard library modules in the recorded dependency "
        "tree (overrides constants.IGNORE_STDLIB)",
    )
    parser.add_argument(
        "--log_level",
        "-l",
        default=os.environ.get("LOG_LEVEL", "warning"),
        help="Default log level",
    )
    args = parser.parse_args()

    # The --html flag implies tree mode
    tree_mode = args.tree or args.html is not None

    # A module name is required unless we're exporting a tree, in which case
    # we default to tracking this package itself
    if not args.name and not tree_mode:
        parser.error("the following arguments are required: --name")
    module_name = args.name or constants.THIS_PACKAGE

    # Determine the submodules argument value
    submodules = (
        False
        if args.submodules is None
        else (args.submodules if args.submodules else True)
    )

    # Set the level on the shared logger
    log_level = getattr(logging, args.log_level.upper(), None)
    if log_level is None:
        log_level = int(args.log_level)
    logging.basicConfig(level=log_level)

    # Perform the tracking
    track_result = track_module(
        module_name=module_name,
        package_name=args.package,
        submodules=submodules,
        track_import_stack=args.track_import_stack,
        full_depth=args.full_depth,
        detect_transitive=args.detect_transitive,
        show_optional=args.show_optional,
        ignore_stdlib=False if args.include_stdlib else None,
    )

    # In tree mode, export the recorded dependency tree (depth truncation
    # happens here, at export time) and optionally write the HTML report
    if tree_mode:
        tree = get_last_tree()
        print(json.dumps(tree.to_dict(max_depth=args.depth), indent=args.indent))
        if args.html:
            tree.write_html(args.html, max_depth=args.depth)
        return

    # Otherwise, print the flattened dependency mapping
    print(json.dumps(track_result, indent=args.indent))


if __name__ == "__main__":  # pragma: no cover
    main()
