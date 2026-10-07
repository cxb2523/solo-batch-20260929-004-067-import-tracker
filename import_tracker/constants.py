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

## Dependency Tree #############################################################

# The default maximum depth used when exporting a dependency tree. When None,
# the tree is exported without any depth truncation. Note that this only
# applies at export time; recording always keeps the raw edges.
MAX_DEPTH = None

# Switch controlling whether standard library modules are ignored when
# recording dependency tree edges
IGNORE_STDLIB = True
