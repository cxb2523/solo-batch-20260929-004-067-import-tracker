"""
Sample library that contains an intentional import cycle used to verify that
the dependency tree records cycle back edges exactly once and never recurses
out of control.
"""

# Local
from . import submod_a
