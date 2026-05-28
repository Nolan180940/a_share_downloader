"""Simple wrapper to run incremental update (for scheduling)."""
import subprocess
import sys
import os

here = os.path.dirname(__file__)
cfg = os.path.join(here, "config.yaml")
py = sys.executable

cmd = [py, os.path.join(here, "downloader.py"), "--mode", "update", "--config", cfg]
print("Running:", " ".join(cmd))
res = subprocess.run(cmd)
sys.exit(res.returncode)
