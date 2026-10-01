"""PyInstaller runtime hook: point gphoto2 at the bundled drivers.

Runs before any application code, so the camlibs/iolibs shipped in the
bundle are in place when `import gphoto2` rewrites both variables (see
byzanz_camera/_gphoto2_paths.py, which restores them afterwards and logs
the resolved paths).

Deliberately silent: this runs BEFORE logging is configured, and the
module-level `logging.info()` configures the root logger as a side
effect — which left `logging_setup.install()` a no-op and the frozen app
without any log file.
"""
import os
import sys

os.environ["IOLIBS"] = sys._MEIPASS
os.environ["CAMLIBS"] = sys._MEIPASS
