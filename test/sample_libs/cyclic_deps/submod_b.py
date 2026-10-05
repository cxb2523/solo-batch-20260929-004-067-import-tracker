"""
Second module in the cycle: submod_b imports submod_a back, closing the loop.

It also imports submod_a a second time to exercise edge idempotency.
"""

# Local
from . import submod_a  # noqa: F401 (dedup test)
from . import submod_a as _submod_a_duplicate

VALUE_B = "b"
