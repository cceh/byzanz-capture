"""py2app entry point for CCeH Crocodile Capture (the papyri app).

Built in ALIAS mode (`python setup.py py2app -A`): nothing is frozen — the
.app runs this live source tree against the project venv, so code changes
are picked up on the next launch. Because py2app's bundle process *is* this
Python interpreter, the Dock shows a single "CCeH Crocodile Capture" tile.

`byzanz_camera.helpers.get_ui_path` resolves UI assets relative to the
current working directory, so chdir into the repo root before launching.
"""
import os

os.chdir(os.path.dirname(os.path.abspath(__file__)))

from papyri.main import main

main()
