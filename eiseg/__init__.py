import sys
import os.path as osp

pjpath = osp.dirname(osp.realpath(__file__))
sys.path.insert(1, pjpath)

__VERSION__ = "0.3.0.4"
__APPNAME__ = f"EISeg {__VERSION__}"


import os
import cv2

for k, v in os.environ.items():
    if k.startswith("QT_") and "cv2" in v:
        del os.environ[k]
