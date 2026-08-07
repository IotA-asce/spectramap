"""SpectraMap: reconstruct the radio-frequency environment as a spatial dataset.

The central object of this project is not ``Spectrum(frequency)`` but::

    RF(latitude, longitude, altitude, frequency, time)

Everything in the package exists to acquire, preserve, or explain that object.
"""

from spectramap.version import FORMAT_VERSION, __version__

__all__ = ["__version__", "FORMAT_VERSION"]
