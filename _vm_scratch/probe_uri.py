import os, sqlite3, sys, tempfile, urllib.parse
from pathlib import Path

B = chr(92)
PREFIX = B + B + "?" + B

def ext(p):
    t = os.path.abspath(str(p))
    return Path(PREFIX + t) if sys.platform == "win32" else Path(t)

def uri(p):
    return "file:" + urllib.parse.quote(str(p), safe=":/") + "?mode=ro"

root = Path(tempfile.mkdtemp(prefix="vm-uri-"))

cases = {}
short = root / "short.db"
sqlite3.connect(str(short)).execute("CREATE TABLE t(x)")
cases["short plain"] = short

odd = root / "has space #hash %pct" / "odd.db"
odd.parent.mkdir(parents=True, exist_ok=True)
sqlite3.connect(str(odd)).execute("CREATE TABLE t(x)")
cases["odd characters"] = odd

deep_dir = ext(root / ("p" * 70) / ("q" * 70) / ("r" * 70))
deep_dir.mkdir(parents=True, exist_ok=True)
deep = deep_dir / "deep.db"
sqlite3.connect(str(deep)).execute("CREATE TABLE t(x)")
cases["deep extended (" + str(len(str(deep))) + ")"] = deep

for label, path in cases.items():
    built = uri(ext(path))
    try:
        sqlite3.connect(built, uri=True).execute("SELECT count(*) FROM t").fetchone()
        print("QUOTED   ", label, "-> OK")
    except Exception as exc:
        print("QUOTED   ", label, "->", type(exc).__name__, str(exc)[:60], "|", built[:70])
    try:
        sqlite3.connect("file:" + str(ext(path)) + "?mode=ro", uri=True).execute("SELECT count(*) FROM t").fetchone()
        print("UNQUOTED ", label, "-> OK")
    except Exception as exc:
        print("UNQUOTED ", label, "->", type(exc).__name__, str(exc)[:60])
