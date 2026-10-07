"""
This module implements utilities that enable tracking of third party deps
through import statements
"""
# Standard
from collections import deque
from contextlib import contextmanager
from types import ModuleType
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple, Union
import builtins
import dis
import html as html_lib
import importlib
import inspect
import os
import re
import sys

# Local
from . import constants
from .log import log

## Public ######################################################################


def track_module(
    module_name: str,
    package_name: Optional[str] = None,
    submodules: Union[List[str], bool] = False,
    track_import_stack: bool = False,
    full_depth: bool = False,
    detect_transitive: bool = False,
    show_optional: bool = False,
    ignore_stdlib: Optional[bool] = None,
) -> Union[Dict[str, List[str]], Dict[str, Dict[str, Any]]]:
    """Track the dependencies of a single python module

    Args:
        module_name:  str
            The name of the module to track (may be relative if package_name
            provided)
        package_name:  Optional[str]
            The parent package name of the module if the module name is relative
        submodules:  Union[List[str], bool]
            If True, all submodules of the given module will also be tracked. If
            given as a list of strings, only those submodules will be tracked.
            If False, only the named module will be tracked.
        track_import_stack:  bool
            Store the stacks of modules causing each dependency of each tracked
            module for debugging purposes.
        full_depth:  bool
            Include transitive dependencies of the third party dependencies that
            are direct dependencies of modules within the target module's parent
            library.
        detect_transitive:  bool
            Detect whether each dependency is 'direct' or 'transitive'
        show_optional:  bool
            Show whether each requirement is optional (behind a try/except) or
            not
        ignore_stdlib:  Optional[bool]
            Whether standard library modules are ignored when recording
            dependency tree edges. Defaults to constants.IGNORE_STDLIB. This
            only affects the recorded dependency tree (see get_last_tree); the
            returned dependency mapping always excludes standard libs.

    Returns:
        import_mapping:  Union[Dict[str, List[str]], Dict[str, Dict[str, Any]]]
            The mapping from fully-qualified module name to the set of imports
            needed by the given module. If tracking import stacks or detecting
            direct vs transitive dependencies, the output schema is
            Dict[str, Dict[str, Any]] where the nested dicts hold "stack" and/or
            "type" keys respectively. If neither feature is enabled, the schema
            is Dict[str, List[str]].
    """
    if ignore_stdlib is None:
        ignore_stdlib = constants.IGNORE_STDLIB

    # Import the target module
    log.debug("Importing %s.%s", package_name, module_name)
    imported = importlib.import_module(module_name, package=package_name)
    full_module_name = imported.__name__

    # Start a fresh dependency tree rooted at the target module
    tree = ImportTree()
    tree.add_root(full_module_name)

    # Recursively build the mapping
    module_deps_map = dict()
    modules_to_check = {imported}
    checked_modules = set()
    tracked_module_root_pkg = full_module_name.partition(".")[0]
    while modules_to_check:
        next_modules_to_check = set()
        for module_to_check in modules_to_check:

            # Figure out all direct imports from this module
            req_imports, opt_imports = _get_imports(module_to_check)
            opt_dep_names = {mod.__name__ for mod in opt_imports}
            all_imports = req_imports.union(opt_imports)
            module_import_names = {mod.__name__ for mod in all_imports}
            log.debug3(
                "Full import names for [%s]: %s",
                module_to_check.__name__,
                module_import_names,
            )

            # Record the raw dependency tree edges for this module. This is
            # idempotent: repeated imports, self references and cycles only
            # ever produce a single edge between the same two nodes.
            for imported_mod in all_imports:
                if ignore_stdlib and _is_stdlib_module(imported_mod.__name__):
                    continue
                tree.add_import(module_to_check.__name__, imported_mod.__name__)

            # Trim to just non-standard modules
            non_std_module_names = _get_non_std_modules(module_import_names)
            log.debug3("Non std module names: %s", non_std_module_names)
            non_std_module_imports = [
                mod for mod in all_imports if mod.__name__ in non_std_module_names
            ]

            # Set the deps for this module as a mapping from each dep to its
            # optional status
            module_deps_map[module_to_check.__name__] = {
                mod: mod in opt_dep_names for mod in non_std_module_names
            }
            log.debug2(
                "Deps for [%s] -> %s",
                module_to_check.__name__,
                non_std_module_names,
            )

            # Add each of these modules to the next round of modules to check if
            # it has not yet been checked
            next_modules_to_check = next_modules_to_check.union(
                {
                    mod
                    for mod in non_std_module_imports
                    if (
                        mod not in checked_modules
                        and (
                            full_depth
                            or mod.__name__.partition(".")[0] == tracked_module_root_pkg
                        )
                    )
                }
            )

            # Always traverse modules within the tracked root package so that
            # the dependency tree captures the internal structure even when
            # the tracked library is this package itself
            next_modules_to_check = next_modules_to_check.union(
                {
                    mod
                    for mod in all_imports
                    if (
                        mod not in checked_modules
                        and mod.__name__.partition(".")[0] == tracked_module_root_pkg
                    )
                }
            )

            # Also check modules with intermediate names
            parent_mods = set()
            for mod in next_modules_to_check:
                mod_name_parts = mod.__name__.split(".")
                for parent_mod_name in [
                    ".".join(mod_name_parts[: i + 1])
                    for i in range(len(mod_name_parts))
                ]:
                    parent_mod = sys.modules.get(parent_mod_name)
                    if parent_mod is None:
                        log.warning(
                            "Could not find parent module %s of %s",
                            parent_mod_name,
                            mod.__name__,
                        )
                        continue
                    if parent_mod not in checked_modules:
                        parent_mods.add(parent_mod)
            next_modules_to_check = next_modules_to_check.union(parent_mods)

            # Mark this module as checked
            checked_modules.add(module_to_check)

        # Set the next iteration
        log.debug3("Next modules to check: %s", next_modules_to_check)
        modules_to_check = next_modules_to_check

    log.debug3("Full module dep mapping: %s", module_deps_map)

    # Expose the recorded dependency tree for export
    global _last_tree
    _last_tree = tree

    # Determine all the modules we want the final answer for
    output_mods = {full_module_name}
    if submodules:
        output_mods = output_mods.union(
            {
                mod
                for mod in module_deps_map
                if (
                    (submodules is True and mod.startswith(full_module_name))
                    or (submodules is not True and mod in submodules)
                )
            }
        )
    log.debug2("Output modules: %s", output_mods)

    # Add parent direct deps to the module deps map
    parent_direct_deps = _find_parent_direct_deps(module_deps_map)

    # Flatten each of the output mods' dependency lists
    flattened_deps = {
        mod: _flatten_deps(mod, module_deps_map, parent_direct_deps)
        for mod in output_mods
    }
    log.debug("Raw output deps map: %s", flattened_deps)

    # If not displaying any of the extra info, the values are simple lists of
    # dependency names
    if not any([detect_transitive, track_import_stack, show_optional]):
        deps_out = {
            mod: list(sorted(deps.keys())) for mod, (deps, _) in flattened_deps.items()
        }

    # Otherwise, the values will be dicts with some combination of "type" and
    # "stack" populated
    else:
        deps_out = {mod: {} for mod in flattened_deps.keys()}

    # If detecting transitive deps, look through the stacks and mark each dep as
    # transitive or direct
    if detect_transitive:
        for mod, (deps, _) in flattened_deps.items():
            for dep_name, dep_stacks in deps.items():
                deps_out.setdefault(mod, {}).setdefault(dep_name, {})[
                    constants.INFO_TYPE
                ] = (
                    constants.TYPE_DIRECT
                    if any(len(dep_stack) == 1 for dep_stack in dep_stacks)
                    else constants.TYPE_TRANSITIVE
                )

    # If tracking import stacks, move them to the "stack" key in the output
    if track_import_stack:
        for mod, (deps, _) in flattened_deps.items():
            for dep_name, dep_stacks in deps.items():
                deps_out.setdefault(mod, {}).setdefault(dep_name, {})[
                    constants.INFO_STACK
                ] = dep_stacks

    # If showing optional, add the optional status of each dependency
    if show_optional:
        for mod, (deps, optional_mapping) in flattened_deps.items():
            for dep_name, dep_stacks in deps.items():
                deps_out.setdefault(mod, {}).setdefault(dep_name, {})[
                    constants.INFO_OPTIONAL
                ] = optional_mapping.get(dep_name, False)

    log.debug("Final output: %s", deps_out)
    return deps_out


## Dependency Tree #############################################################

# The most recently recorded dependency tree (from track_module or
# track_imports). Recording always keeps the raw edges; depth truncation only
# happens when the tree is exported.
_last_tree = None


class ImportTree:
    """A dependency tree recording the raw import edges between modules.

    Each tracked module is a node that maintains its in-edges and out-edges.
    Edge creation is idempotent: repeated imports, module self references and
    circular imports (A -> B -> A) never produce duplicate edges, and all
    traversals are iterative with visited tracking so cycles cannot cause
    runaway recursion.
    """

    def __init__(self):
        # Raw edges as (importer, imported) pairs
        self.edges = set()
        # Adjacency maps from module name to the set of connected names
        self.out_edges = {}
        self.in_edges = {}
        # Ordered root module names (relative depth 0)
        self.roots = []

    @property
    def nodes(self) -> Set[str]:
        """All tracked module names"""
        return set(self.out_edges.keys())

    def _ensure_node(self, name: str):
        self.out_edges.setdefault(name, set())
        self.in_edges.setdefault(name, set())

    def add_root(self, name: str):
        """Register a root node (relative depth 0)"""
        self._ensure_node(name)
        if name not in self.roots:
            self.roots.append(name)

    def add_edge(self, source: str, target: str) -> bool:
        """Idempotently add a single edge from source to target. Self edges
        are recorded once like any other edge.
        """
        self._ensure_node(source)
        self._ensure_node(target)
        if (source, target) in self.edges:
            return False
        self.edges.add((source, target))
        self.out_edges[source].add(target)
        self.in_edges[target].add(source)
        return True

    def add_import(self, importer: str, imported: str):
        """Record that the importer imported the given dotted module name.

        Importing a dotted name also imports each of its parent packages, so
        edges are recorded for the full name and every parent prefix that is
        present in sys.modules.
        """
        name_parts = imported.split(".")
        for idx in range(len(name_parts)):
            prefix = ".".join(name_parts[: idx + 1])
            if prefix in sys.modules:
                self.add_edge(importer, prefix)

    def depths(self) -> Dict[str, Optional[int]]:
        """Compute the relative depth of each node from the roots using an
        iterative breadth-first traversal (cycle safe).
        """
        # Roots plus any node with no incoming edges start at depth 0
        starts = list(self.roots) + sorted(
            node
            for node in self.nodes
            if node not in self.roots and not self.in_edges.get(node)
        )
        depth_map = {}
        queue = deque()
        for start in starts:
            if start not in depth_map:
                depth_map[start] = 0
                queue.append(start)
        while queue:
            node = queue.popleft()
            for child in sorted(self.out_edges.get(node, ())):
                if child not in depth_map:
                    depth_map[child] = depth_map[node] + 1
                    queue.append(child)
        # Any node still unreachable (e.g. only reachable through cycles with
        # no entry point) gets no relative depth
        for node in self.nodes:
            depth_map.setdefault(node, None)
        return depth_map

    def cycle_edges(self) -> Set[Tuple[str, str]]:
        """Find the edges that close a cycle (including self edges) using an
        iterative depth-first traversal.
        """
        back_edges = set()
        done = set()
        starts = list(self.roots) + sorted(self.nodes)
        for start in starts:
            if start in done:
                continue
            in_path = {start}
            stack = [(start, sorted(self.out_edges.get(start, ())))]
            while stack:
                node, children = stack[-1]
                descended = False
                while children:
                    child = children.pop()
                    if child in in_path:
                        back_edges.add((node, child))
                    elif child not in done:
                        in_path.add(child)
                        stack.append((child, sorted(self.out_edges.get(child, ()))))
                        descended = True
                        break
                if not descended:
                    done.add(node)
                    in_path.discard(node)
                    stack.pop()
        return back_edges

    def to_dict(self, max_depth: Optional[int] = None) -> Dict[str, Any]:
        """Export the tree as a JSON-serializable mapping.

        Depth truncation happens only here, at export time: the raw edges are
        always exported in full and nodes beyond max_depth are kept and marked
        as truncated, so the same recording can produce reports at multiple
        depth settings.
        """
        if max_depth is None:
            max_depth = constants.MAX_DEPTH
        depth_map = self.depths()
        nodes = {}
        for name in sorted(self.nodes):
            depth = depth_map.get(name)
            nodes[name] = {
                "depth": depth,
                "in": sorted(self.in_edges.get(name, ())),
                "out": sorted(self.out_edges.get(name, ())),
                "truncated": bool(
                    max_depth is not None and depth is not None and depth > max_depth
                ),
            }
        return {
            "roots": list(self.roots),
            "max_depth": max_depth,
            "nodes": nodes,
            "edges": sorted([list(edge) for edge in self.edges]),
            "cycles": sorted([list(edge) for edge in self.cycle_edges()]),
        }

    def render_html(self, max_depth: Optional[int] = None) -> str:
        """Render the tree as a standalone, directly openable HTML report"""
        report = self.to_dict(max_depth=max_depth)
        max_depth = report["max_depth"]
        nodes = report["nodes"]
        cycle_edges = {tuple(edge) for edge in report["cycles"]}
        depth_map = {name: info["depth"] for name, info in nodes.items()}

        def render_node(name: str, path: Tuple[str, ...]) -> List[str]:
            depth = depth_map.get(name)
            depth_label = "?" if depth is None else str(depth)
            lines = [
                "<li><code>{}</code> "
                '<span class="depth">[depth {}]</span>'.format(
                    html_lib.escape(name), depth_label
                )
            ]
            child_lines = []
            for child in sorted(self.out_edges.get(name, ())):
                escaped_child = html_lib.escape(child)
                if (name, child) in cycle_edges or child in path:
                    child_lines.append(
                        "<li><code>{}</code> "
                        '<span class="cycle">&#8617; cycle</span></li>'.format(
                            escaped_child
                        )
                    )
                elif nodes.get(child, {}).get("truncated"):
                    child_lines.append(
                        "<li><code>{}</code> "
                        '<span class="truncated">&#8230; truncated '
                        "(depth &gt; {})</span></li>".format(escaped_child, max_depth)
                    )
                else:
                    child_lines.extend(render_node(child, path + (name,)))
            if child_lines:
                lines.append("<ul>")
                lines.extend(child_lines)
                lines.append("</ul>")
            lines.append("</li>")
            return lines

        body_lines = []
        roots = report["roots"] or sorted(
            name for name, info in nodes.items() if not info["in"]
        )
        body_lines.append("<ul>")
        for root in roots:
            body_lines.extend(render_node(root, ()))
        body_lines.append("</ul>")

        cycle_lines = ""
        if report["cycles"]:
            cycle_items = "\n".join(
                "<li><code>{}</code> &#8594; <code>{}</code></li>".format(
                    html_lib.escape(src), html_lib.escape(dst)
                )
                for src, dst in report["cycles"]
            )
            cycle_lines = "<h2>Cycle Edges</h2>\n<ul>\n{}\n</ul>".format(cycle_items)

        return _TREE_HTML_TEMPLATE.format(
            node_count=len(nodes),
            edge_count=len(report["edges"]),
            max_depth="unlimited" if max_depth is None else max_depth,
            tree="\n".join(body_lines),
            cycles=cycle_lines,
        )

    def write_html(self, path: str, max_depth: Optional[int] = None):
        """Write the standalone HTML report to the given file path"""
        parent_dir = os.path.dirname(os.path.abspath(path))
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(self.render_html(max_depth=max_depth))


_TREE_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Import Dependency Tree</title>
<style>
body {{ font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
       margin: 2em; color: #24292f; }}
code {{ background: #f6f8fa; padding: 0.1em 0.4em; border-radius: 4px; }}
ul {{ list-style: none; border-left: 1px solid #d0d7de; padding-left: 1.5em; }}
li {{ margin: 0.25em 0; }}
.depth {{ color: #57606a; font-size: 0.85em; }}
.cycle {{ color: #cf222e; font-weight: bold; }}
.truncated {{ color: #6e7781; font-style: italic; }}
.summary {{ color: #57606a; }}
</style>
</head>
<body>
<h1>Import Dependency Tree</h1>
<p class="summary">Nodes: {node_count} | Edges: {edge_count} | Max depth: {max_depth}</p>
{tree}
{cycles}
</body>
</html>
"""


def get_last_tree() -> Optional[ImportTree]:
    """Get the most recently recorded dependency tree"""
    return _last_tree


@contextmanager
def track_imports(
    roots: Optional[List[str]] = None,
    ignore_stdlib: Optional[bool] = None,
) -> ImportTree:
    """Context manager that records a dependency tree of the imports that
    happen inside its body.

    Args:
        roots:  Optional[List[str]]
            The module names to use as tree roots (relative depth 0). If not
            given, the module that entered the context is used.
        ignore_stdlib:  Optional[bool]
            Whether standard library modules are ignored when recording edges.
            Defaults to constants.IGNORE_STDLIB.

    Yields:
        tree:  ImportTree
            The tree being recorded. Raw edges only; depth truncation happens
            at export time.

    Notes:
        Contexts may be nested. Each exit restores the __import__ hook that
        was in place when it was entered, so an exception raised inside an
        inner context still leaves the outer context's state intact.
    """
    if ignore_stdlib is None:
        ignore_stdlib = constants.IGNORE_STDLIB
    tree = ImportTree()
    root_names = list(roots) if roots else []
    if not root_names:
        caller_name = _external_caller_name()
        if caller_name:
            root_names.append(caller_name)
    for root_name in root_names:
        tree.add_root(root_name)

    # Save the current import hook so it can be restored on exit, no matter
    # how the body terminates
    prev_import = builtins.__import__

    def _tracking_import(name, globals=None, locals=None, fromlist=(), level=0):
        module = prev_import(name, globals, locals, fromlist, level)

        # Figure out who is doing the importing
        importer = (globals or {}).get("__name__") or _external_caller_name()

        # Resolve relative imports against the caller's package
        abs_name = name
        if level:
            package = (globals or {}).get("__package__") or ""
            package_parts = package.split(".") if package else []
            if level > 1:
                keep = len(package_parts) - (level - 1)
                package_parts = package_parts[: max(keep, 0)]
            abs_name = ".".join(package_parts + ([name] if name else []))

        # Record the import itself plus any fromlist entries that resolve to
        # imported submodules
        targets = [abs_name] if abs_name else []
        if abs_name:
            for from_item in fromlist or ():
                candidate = f"{abs_name}.{from_item}"
                if candidate in sys.modules:
                    targets.append(candidate)
        for target in targets:
            if target.partition(".")[0] not in sys.modules:
                continue
            if ignore_stdlib and _is_stdlib_module(target):
                continue
            if importer:
                tree.add_import(importer, target)
        return module

    builtins.__import__ = _tracking_import
    global _last_tree
    _last_tree = tree
    try:
        yield tree
    finally:
        builtins.__import__ = prev_import


## Private #####################################################################


def _is_stdlib_module(mod_name: str) -> bool:
    """Detect whether the given module name belongs to the standard library"""
    if mod_name in sys.builtin_module_names:
        return True
    if mod_name.partition(".")[0] in _known_std_pkgs:
        return True
    mod = sys.modules.get(mod_name)
    if mod is None:
        return False
    mod_file = getattr(mod, "__file__", None)
    if mod_file is None:
        # Builtins and frozen modules have no __file__
        return True
    real_file = os.path.normcase(os.path.realpath(mod_file))
    # Anything under a site/dist-packages directory is third party, even
    # though such directories live under the standard library dir
    path_parts = set(os.path.normpath(real_file).split(os.sep))
    if "site-packages" in path_parts or "dist-packages" in path_parts:
        return False
    # The module is standard if its file lives under the stdlib directories
    return any(
        real_file.startswith(os.path.normcase(std_dir) + os.sep)
        for std_dir in [_std_lib_dir, _std_dylib_dir]
    )


def _external_caller_name() -> Optional[str]:
    """Walk up the stack to find the name of the first module outside of this
    module and the contextlib machinery.
    """
    frame = inspect.currentframe()
    try:
        while frame is not None:
            mod_name = frame.f_globals.get("__name__", "")
            if mod_name and mod_name not in (__name__, "contextlib"):
                return mod_name
            frame = frame.f_back
        return None
    finally:
        del frame


def _get_dylib_dir():
    """Different versions/builds of python manage different builtin libraries as
    "builtins" versus extensions. As such, we need some heuristics to try to
    find the base directory that holds shared objects from the standard library.
    """
    is_dylib = lambda x: x is not None and (x.endswith(".so") or x.endswith(".dylib"))
    all_mod_paths = list(
        filter(is_dylib, (getattr(mod, "__file__", "") for mod in sys.modules.values()))
    )
    # If there's any dylib found, return the parent directory
    sample_dylib = None
    if all_mod_paths:
        sample_dylib = all_mod_paths[0]
    else:  # pragma: no cover
        # If not found with the above, look through libraries that are known to
        # sometimes be packaged as compiled extensions
        #
        # NOTE: This code may be unnecessary, but it is intended to catch future
        #   cases where the above does not yield results
        #
        # More names can be added here as needed
        for lib_name in ["cmath"]:
            lib = importlib.import_module(lib_name)
            fname = getattr(lib, "__file__", None)
            if is_dylib(fname):
                sample_dylib = fname
                break

    # If all else fails, we'll just return a sentinel string. This will fail to
    # match in the below check for builtin modules
    return (
        os.path.realpath(os.path.dirname(sample_dylib))
        if sample_dylib is not None
        else "BADPATH"
    )


# The path where global modules are found
_std_lib_dir = os.path.realpath(os.path.dirname(os.__file__))
_std_dylib_dir = _get_dylib_dir()
_known_std_pkgs = [
    "collections",
]


# Regex for matching lines in the exception table
_exception_table_expr = re.compile(r"  ([0-9]+) to ([0-9]+) -> [0-9]+ \[([0-9]+)\].*")


def _mod_defined_in_init_file(mod: ModuleType) -> bool:
    """Determine if the given module is defined in an __init__.py[c]"""
    mod_file = getattr(mod, "__file__", None)
    if mod_file is None:
        return False
    return os.path.splitext(os.path.basename(mod_file))[0] == "__init__"


def _get_import_parent_path(mod_name: str) -> str:
    """Get the parent directory of the given module"""
    mod = sys.modules[mod_name]  # NOTE: Intentionally unsafe to raise if not there!

    # Some standard libs have no __file__ attribute
    file_path = getattr(mod, "__file__", None)
    if file_path is None:
        return _std_lib_dir

    # If the module comes from an __init__, we need to pop two levels off
    if _mod_defined_in_init_file(mod):
        file_path = os.path.dirname(file_path)
    parent_path = os.path.dirname(file_path)
    return parent_path


def _is_third_party(mod_name: str) -> bool:
    """Detect whether the given module is a third party (non-standard and not
    import_tracker)"""
    mod_pkg = mod_name.partition(".")[0]
    return (
        not mod_name.startswith("_")
        and (
            mod_name not in sys.modules
            or _get_import_parent_path(mod_name) not in [_std_lib_dir, _std_dylib_dir]
        )
        and mod_pkg != constants.THIS_PACKAGE
        and mod_pkg not in _known_std_pkgs
    )


def _get_non_std_modules(mod_names: Iterable[str]) -> Set[str]:
    """Take a snapshot of the non-standard modules currently imported"""
    # Determine the names from the list that are non-standard
    return {mod_name for mod_name in mod_names if _is_third_party(mod_name)}


def _get_value_col(dis_line: str) -> str:
    """Parse the string value from a `dis` output line"""
    loc = dis_line.find("(")
    if loc >= 0:
        return dis_line[loc + 1 : -1]
    return ""


def _get_op_number(dis_line: str) -> Optional[int]:
    """Get the opcode number out of the line of `dis` output"""
    line_parts = dis_line.split()
    valid_line_part_idxs = [i for i, val in enumerate(line_parts) if val.isupper()]
    if not valid_line_part_idxs:
        return None
    opcode_idx = min(valid_line_part_idxs)
    assert opcode_idx > 0, f"Opcode found at the beginning of line! [{dis_line}]"
    return int(line_parts[opcode_idx - 1])


def _get_try_end_number(
    dis_line: str,
    op_num: Optional[int],
    exception_table: Dict[int, int],
) -> Optional[int]:
    """If the line contains a known indicator for a try block, get the
    corresponding end number

    NOTE: This contains compatibility code for changes between 3.10 and 3.11
    """
    return exception_table.get(op_num or -1) or (
        int(_get_value_col(dis_line).split()[-1])
        if any(op in dis_line for op in ["SETUP_FINALLY", "SETUP_EXCEPT"])
        else None
    )


def _get_exception_table(dis_lines: List[str]) -> Dict[int, int]:
    """For 3.11+ exception handling, parse the Exception Table"""
    table_start = [i for i, line in enumerate(dis_lines) if line == "ExceptionTable:"]
    assert len(table_start) <= 1, "Found multiple exception tables!"
    return (
        {
            int(m.group(1)): int(m.group(2))
            for m in [
                _exception_table_expr.match(line)
                for line in dis_lines[table_start[0] + 1 :]
            ]
            if m and int(m.group(3)) == 0 and m.group(1) != m.group(2)
        }
        if table_start
        else {}
    )


def _figure_out_import(
    mod: ModuleType,
    dots: Optional[int],
    import_name: Optional[str],
    import_from: Optional[str],
) -> ModuleType:
    """This function takes the set of information about an individual import
    statement parsed out of the `dis` output and attempts to find the in-memory
    module object it refers to.
    """
    log.debug2("Figuring out import [%s/%s/%s]", dots, import_name, import_from)

    # If there are no dots, look for candidate absolute imports
    if not dots:
        if import_name in sys.modules:
            if import_from is not None:
                candidate = f"{import_name}.{import_from}"
                if candidate in sys.modules:
                    log.debug3("Found [%s] in sys.modules", candidate)
                    return sys.modules[candidate]
            log.debug3("Found [%s] in sys.modules", import_name)
            return sys.modules[import_name]

    # Try simulating a relative import from a non-relative local
    dots = dots or 1

    # If there are dots, figure out the parent
    parent_mod_name_parts = mod.__name__.split(".")
    defined_in_init = _mod_defined_in_init_file(mod)
    if dots > 1:
        parent_dots = dots - 1 if defined_in_init else dots
        root_mod_name = ".".join(parent_mod_name_parts[:-parent_dots])
    elif defined_in_init:
        root_mod_name = mod.__name__
    else:
        root_mod_name = ".".join(parent_mod_name_parts[:-1])
    log.debug3("Parent mod name parts: %s", parent_mod_name_parts)
    log.debug3("Num Dots: %d", dots)
    log.debug3("Root mod name: %s", root_mod_name)
    log.debug3("Module file: %s", getattr(mod, "__file__", None))
    if not import_name:
        import_name = root_mod_name
    elif root_mod_name:
        import_name = f"{root_mod_name}.{import_name}"

    # Try with the import_from attached. This might be a module name or a
    # non-module attribute, so this might not work
    full_import_candidate = f"{import_name}.{import_from}"
    log.debug3("Looking for [%s] in sys.modules", full_import_candidate)
    if full_import_candidate in sys.modules:
        return sys.modules[full_import_candidate]

    # If that didn't work, the from is an attribute, so just get the import name
    return sys.modules.get(import_name)


def _get_imports(mod: ModuleType) -> Tuple[Set[ModuleType], Set[ModuleType]]:
    """Get the sets of required and optional imports for the given module by
    parsing its bytecode
    """
    log.debug2("Getting imports for %s", mod.__name__)
    req_imports = set()
    opt_imports = set()

    # Attempt to disassemble the byte code for this module. If the module has no
    # code, we ignore it since it's most likely a c extension
    try:
        loader = mod.__loader__ or mod.__spec__.loader
        mod_code = loader.get_code(mod.__name__)
    except (AttributeError, ImportError):
        log.warning("Couldn't find a loader for %s!", mod.__name__)
        return req_imports, opt_imports
    if mod_code is None:
        log.debug2("No code object found for %s", mod.__name__)
        return req_imports, opt_imports
    bcode = dis.Bytecode(mod_code)

    # Parse all bytecode lines
    current_dots = None
    current_import_name = None
    current_import_from = None
    open_import = False
    open_tries = set()
    log.debug4("Byte Code:")
    dis_lines = bcode.dis().split("\n")

    # Look for and parse an Exception Table (3.11+)
    exception_table = _get_exception_table(dis_lines)
    log.debug4("Exception Table: %s", exception_table)

    for line in dis_lines:
        log.debug4(line)
        line_val = _get_value_col(line)

        # If this is the beginning of a try block, add the end to the known open
        # try set
        op_num = _get_op_number(line)
        try_end = _get_try_end_number(line, op_num, exception_table)
        if try_end:
            open_tries.add(try_end)
            log.debug3("Open tries: %s", open_tries)

        # Parse the individual ops
        if "LOAD_CONST" in line:
            if line_val.isnumeric():
                current_dots = int(line_val)
        elif "IMPORT_NAME" in line:
            open_import = True
            current_import_name = line_val
        elif "IMPORT_FROM" in line:
            open_import = True
            current_import_from = line_val
        else:
            # This closes an import, so figure out what the module is that is
            # being imported!
            if open_import:
                import_mod = _figure_out_import(
                    mod, current_dots, current_import_name, current_import_from
                )
                if import_mod is not None:
                    log.debug2("Adding import module [%s]", import_mod.__name__)
                    if open_tries:
                        log.debug(
                            "Found optional dependency of [%s]: %s",
                            mod.__name__,
                            import_mod.__name__,
                        )
                        opt_imports.add(import_mod)
                    else:
                        req_imports.add(import_mod)

            # If this is a STORE_NAME, subsequent "from" statements may use the
            # same dots and name
            if "STORE_NAME" not in line:
                current_dots = None
                current_import_name = None
            open_import = False
            current_import_from = None

        # Close the open try if this ends one
        if op_num in open_tries:
            open_tries.remove(op_num)
            log.debug3("Closed try %d. Remaining open tries: %s", op_num, open_tries)

    # To the best of my knowledge, all bytecode will end with something other
    # than an import, even if an import is the last line in the file (e.g.
    # STORE_NAME). If this somehow proves to be untrue, please file a bug!
    assert not open_import, "Found an unclosed import in {}! {}/{}/{}".format(
        mod.__name__,
        current_dots,
        current_import_name,
        current_import_from,
    )

    return req_imports, opt_imports


def _find_parent_direct_deps(
    module_deps_map: Dict[str, List[str]]
) -> Dict[str, Dict[str, List[str]]]:
    """Construct a mapping for each module (e.g. foo.bar.baz) to a mapping of
    parent modules (e.g. [foo, foo.bar]) and the sets of imports that are
    directly imported in those modules. This mapping is used to augment the sets
    of required imports for each target module in the final flattening.
    """

    parent_direct_deps = {}
    for mod_name, mod_deps in module_deps_map.items():

        # Look through all parent modules of module_name and aggregate all
        # third-party deps that are directly used by those modules
        mod_base_name = mod_name.partition(".")[0]
        mod_name_parts = mod_name.split(".")
        for i in range(1, len(mod_name_parts)):
            parent_mod_name = ".".join(mod_name_parts[:i])
            parent_deps = module_deps_map.get(parent_mod_name, {})
            for dep, parent_dep_opt in parent_deps.items():
                currently_optional = mod_deps.get(dep, True)
                if not dep.startswith(mod_base_name) and currently_optional:
                    log.debug3(
                        "Adding direct-dependency of parent mod [%s] to [%s]: %s",
                        parent_mod_name,
                        mod_name,
                        dep,
                    )
                    mod_deps[dep] = currently_optional and parent_dep_opt
                    parent_direct_deps.setdefault(mod_name, {}).setdefault(
                        parent_mod_name, set()
                    ).add(dep)
    log.debug3("Parent direct dep map: %s", parent_direct_deps)
    return parent_direct_deps


def _flatten_deps(
    module_name: str,
    module_deps_map: Dict[str, List[str]],
    parent_direct_deps: Dict[str, Dict[str, List[str]]],
) -> Tuple[Dict[str, List[str]], Dict[str, bool]]:
    """Flatten the names of all modules that the target module depends on"""

    # Look through all modules that are directly required by this target module.
    # This only looks at the leaves, so if the module depends on foo.bar.baz,
    # only the deps for foo.bar.baz will be incluced and not foo.bar.buz or
    # foo.biz.
    all_deps = {}
    mods_to_check = {module_name: []}
    while mods_to_check:
        next_mods_to_check = {}
        for mod_to_check, parent_path in mods_to_check.items():
            log.debug4("Checking mod %s", mod_to_check)
            mod_parents_direct_deps = parent_direct_deps.get(mod_to_check, {})
            mod_path = parent_path + [mod_to_check]
            mod_deps = set(module_deps_map.get(mod_to_check, []))
            log.debug4(
                "Mod deps for %s at path %s: %s", mod_to_check, mod_path, mod_deps
            )
            new_mods = mod_deps - set(all_deps.keys())
            next_mods_to_check.update({new_mod: mod_path for new_mod in new_mods})
            for mod_dep in mod_deps:
                # If this is a parent direct dep, and the stack for this parent
                # is not already present in the dep stacks for this dependency,
                # add the parent to the path
                mod_dep_direct_parents = {}
                for (
                    mod_parent,
                    mod_parent_direct_deps,
                ) in mod_parents_direct_deps.items():
                    if mod_dep in mod_parent_direct_deps:
                        log.debug4(
                            "Found direct parent dep for [%s] from parent [%s] and dep [%s]",
                            mod_to_check,
                            mod_parent,
                            mod_dep,
                        )
                        mod_dep_direct_parents[mod_parent] = [
                            mod_parent
                        ] in all_deps.get(mod_dep, [])
                if mod_dep_direct_parents:
                    for (
                        mod_dep_direct_parent,
                        already_present,
                    ) in mod_dep_direct_parents.items():
                        if not already_present:
                            all_deps.setdefault(mod_dep, []).append(
                                [mod_dep_direct_parent] + mod_path
                            )
                else:
                    all_deps.setdefault(mod_dep, []).append(mod_path)
        log.debug3("Next mods to check: %s", next_mods_to_check)
        mods_to_check = next_mods_to_check
    log.debug4("All deps: %s", all_deps)

    # Create the flattened dependencies with the source lists for each
    mod_base_name = module_name.partition(".")[0]
    flat_base_deps = {}
    optional_deps_map = {}
    for dep, dep_sources in all_deps.items():
        if not dep.startswith(mod_base_name):
            # Truncate the dep_sources entries and trim to avoid duplicates
            dep_root_mod_name = dep.partition(".")[0]
            flat_dep_sources = flat_base_deps.setdefault(dep_root_mod_name, [])
            opt_dep_values = optional_deps_map.setdefault(dep_root_mod_name, [])
            for dep_source in dep_sources:
                log.debug4("Considering dep source list for %s: %s", dep, dep_source)

                # If any link in the dep_source is optional, the whole
                # dep_source should be considered optional
                is_optional = False
                for parent_idx, dep_mod in enumerate(dep_source[1:] + [dep]):
                    dep_parent = dep_source[parent_idx]
                    log.debug4(
                        "Checking whether [%s -> %s] is optional (dep=%s)",
                        dep_parent,
                        dep_mod,
                        dep_root_mod_name,
                    )
                    if module_deps_map.get(dep_parent, {}).get(dep_mod, False):
                        log.debug4("Found optional link %s -> %s", dep_parent, dep_mod)
                        is_optional = True
                        break
                opt_dep_values.append(
                    [
                        is_optional,
                        dep_source,
                    ]
                )

                flat_dep_source = dep_source
                if dep_root_mod_name in dep_source:
                    flat_dep_source = dep_source[: dep_source.index(dep_root_mod_name)]
                if flat_dep_source not in flat_dep_sources:
                    flat_dep_sources.append(flat_dep_source)
    log.debug3("Optional deps map for [%s]: %s", module_name, optional_deps_map)
    optional_deps_map = {
        mod: all([opt_val[0] for opt_val in opt_vals])
        for mod, opt_vals in optional_deps_map.items()
    }
    return flat_base_deps, optional_deps_map
