"""DesignAround local engine.

Everything in this package runs on the user's machine (CLI, desktop app) or on a
server CPU worker. Nothing here needs a GPU: CAD programs are executed with
CadQuery/OpenCascade, patent views come from hidden-line removal, renders and
silhouettes come from a CPU rasteriser, and shape comparison is pure NumPy.
"""

__version__ = "0.1.4"
