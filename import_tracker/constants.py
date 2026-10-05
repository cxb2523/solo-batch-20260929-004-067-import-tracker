"""
Shared constants across the various parts of the library
"""

# Standard
import sys

# The name of this package (import_tracker)
THIS_PACKAGE = sys.modules[__name__].__package__.partition(".")[0]

# Labels for direct vs transitive dependencies
TYPE_DIRECT = "direct"
TYPE_TRANSITIVE = "transitive"

# Info section headers
INFO_TYPE = "type"
INFO_STACK = "stack"
INFO_OPTIONAL = "optional"

# Default maximum relative depth used when exporting a dependency tree. A
# value of None means "no limit". Depth truncation is applied only at export
# time so that a single recorded tree can produce reports at multiple depths.
DEFAULT_MAX_DEPTH = None

# Whether standard-library modules should be included as tree nodes/edges.
# This only affects the recorded dependency tree, not the existing flattened
# dependency output (which always trims stdlib modules).
IGNORE_STDLIB = True

# Dependency-tree section headers
TREE_ROOT = "root"
TREE_NODES = "nodes"
TREE_EDGES = "edges"
TREE_EDGE_SRC = "src"
TREE_EDGE_DST = "dst"
TREE_EDGE_KIND = "kind"
TREE_EDGE_OPTIONAL = "optional"
TREE_EDGE_FORWARD = "forward"
TREE_EDGE_BACK = "back"
TREE_NODE_TRUNCATED = "truncated"
TREE_NODE_DEPTH = "depth"
