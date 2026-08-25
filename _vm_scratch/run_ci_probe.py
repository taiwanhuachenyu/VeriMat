"""Extract the CI step's Python body and run it, so the workflow is verified, not assumed."""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
lines = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8").splitlines()

start = next(i for i, line in enumerate(lines) if line.strip().startswith("- name: Prove"))
run_at = next(i for i in range(start, len(lines)) if lines[i].strip() == "run: |")
body = []
for line in lines[run_at + 1:]:
    if line.strip() and not line.startswith(" " * 10):
        break
    body.append(line[10:])
source = "\n".join(body).rstrip() + "\n"
print(source)
print("=" * 70)

with tempfile.TemporaryDirectory() as scratch:
    script = Path(scratch) / "step.py"
    script.write_text(source, encoding="utf-8")
    summary = Path(scratch) / "summary.md"
    summary.write_text("", encoding="utf-8")
    environment = dict(
        os.environ,
        RUNNER_OS="Windows" if sys.platform == "win32" else "Linux",
        GITHUB_STEP_SUMMARY=str(summary),
        PYTHONPATH=str(ROOT),
    )
    result = subprocess.run(
        [sys.executable, str(script)], cwd=ROOT, env=environment,
        capture_output=True, text=True,
    )
    print("exit:", result.returncode)
    print("stdout:", result.stdout.rstrip())
    if result.stderr.strip():
        print("stderr:", result.stderr.rstrip())
    print("step summary:", summary.read_text(encoding="utf-8").rstrip())
sys.exit(result.returncode)
