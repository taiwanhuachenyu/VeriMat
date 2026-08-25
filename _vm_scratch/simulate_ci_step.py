"""Run the workflow's inline step exactly as the runner would.

`shell: python` writes the block to a file under RUNNER_TEMP and executes it from there, so the
only way to reproduce the import failure -- and to prove the fix -- is to run it as a script file
from outside the repository with the same environment the step declares.
"""
import pathlib
import re
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
STEP = "Prove the path-length limit is real before the suite clears it"

source = WORKFLOW.read_text(encoding="utf-8")
start = source.index(f"- name: {STEP}")
body_start = source.index("run: |", start) + len("run: |")
body_end = source.index("- name: ", body_start)
block = source[body_start:body_end]

indent = min(len(line) - len(line.lstrip()) for line in block.splitlines() if line.strip())
script = "\n".join(line[indent:] if line.strip() else "" for line in block.splitlines()).strip()

declared = re.search(r"env:\n((?:\s+#.*\n|\s+\w+: .*\n)+)", source[start:body_start])
env_keys = dict(re.findall(r"^\s+(\w+): (.+)$", declared.group(1), re.M)) if declared else {}
print(f"step env declared in the workflow: {env_keys}")

work = pathlib.Path(tempfile.mkdtemp())
step_file = work / "step.py"
step_file.write_text(script + "\n", encoding="utf-8")
summary = work / "summary.md"
summary.touch()

environment = {
    "SystemRoot": r"C:\Windows", "TEMP": str(work), "TMP": str(work),
    "RUNNER_OS": "Windows", "GITHUB_STEP_SUMMARY": str(summary),
    "RUNNER_TEMP": str(work), **env_keys,
}
result = subprocess.run(
    [sys.executable, str(step_file)], cwd=ROOT, env=environment,
    capture_output=True, text=True,
)
print(f"exit={result.returncode}")
print("stdout:", result.stdout.strip() or "(empty)")
if result.stderr.strip():
    print("stderr:", result.stderr.strip()[-600:])
print("step summary written:", summary.read_text(encoding="utf-8").strip() or "(empty)")
raise SystemExit(result.returncode)
