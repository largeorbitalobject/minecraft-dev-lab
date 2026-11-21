import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
REAL = os.path.join(HERE, "fixtures", "real")


def real(name):
    with open(os.path.join(REAL, name), "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()
