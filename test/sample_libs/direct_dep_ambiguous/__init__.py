"""
This sample lib is carefully crafted such that the order of the imports makes
the allocation of alog ambiguous. As such, we very intentionally have alog after
the local imports and need to ignore this file in isort.
"""

# First Party
# Import alog here so that is a direct dependency of the top-level
import alog

# Local
# Import the two submodules
from . import bar, foo
