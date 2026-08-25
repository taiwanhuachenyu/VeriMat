"""Compile the report reproducibly."""
from __future__ import annotations
import os, shutil, subprocess
from pathlib import Path
SOURCE_DATE_EPOCH = "1755000000"
root = Path(__file__).resolve().parent
env = dict(os.environ)
env["SOURCE_DATE_EPOCH"] = SOURCE_DATE_EPOCH
env["FORCE_SOURCE_DATE"] = "1"
latex, bibtex = shutil.which("pdflatex"), shutil.which("bibtex")
if latex is None or bibtex is None:
    raise SystemExit("pdflatex and bibtex are required")
for command in ([latex, "-interaction=nonstopmode", "-halt-on-error", "survey"], [bibtex, "survey"], [latex, "-interaction=nonstopmode", "-halt-on-error", "survey"], [latex, "-interaction=nonstopmode", "-halt-on-error", "survey"]):
    subprocess.run(command, cwd=root, env=env, check=True)
