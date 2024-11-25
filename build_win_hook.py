import os
import sys
import logging

logging.info(f"Set IOLIBS and CAMLIBS to: #{sys._MEIPASS}")
os.environ["IOLIBS"] = sys._MEIPASS
os.environ["CAMLIBS"] = sys._MEIPASS