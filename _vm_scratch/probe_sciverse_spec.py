"""Ask the Sciverse deployment to describe itself.  Never prints the token."""
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())

TOKEN = os.environ["SCIVERSE_API_TOKEN"]
BASE = os.environ.get("SCIVERSE_BASE_URL", "https://api.sciverse.space").rstrip("/")
print(f"base_url = {BASE}")
print(f"token    = {TOKEN[:4]}...{TOKEN[-4:]} (len {len(TOKEN)})")
print()


def fetch(path, method="GET", body=None):
    url = f"{BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace")
    except Exception as error:  # noqa: BLE001 - a probe reports, it does not raise
        return None, f"{type(error).__name__}: {error}"


for path in ("/openapi.json", "/docs", "/redoc", "/", "/health", "/v1/openapi.json"):
    status, text = fetch(path)
    head = text[:400].replace("\n", " ")
    print(f"{status!s:>6}  {path:<20} {len(text):>7} bytes  {head}")
    if path.endswith("openapi.json") and status == 200:
        (ROOT / "_vm_scratch" / "openapi.json").write_text(text, encoding="utf-8")
        print("        -> saved to _vm_scratch/openapi.json")
        sys.exit(0)
