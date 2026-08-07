"""Version constants.

``__version__`` identifies the software. ``FORMAT_VERSION`` identifies the on-disk
survey format and changes only when the layout of a ``.smap`` directory changes in a
way that an older reader could not handle. Both are stamped into every survey and
into every derived product so that any artifact can be traced back to the code that
produced it.
"""

__version__ = "0.1.0.dev0"

# Bump only on breaking changes to the .smap container layout.
FORMAT_VERSION = "0.1"

__all__ = ["__version__", "FORMAT_VERSION"]
