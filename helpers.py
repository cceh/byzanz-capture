from os import path
import sys

def get_ui_path(file: str):
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        bundle_dir = sys._MEIPASS
    else:    
        bundle_dir = path.abspath(path.dirname("__FILE__"))
    return path.join(bundle_dir, file)