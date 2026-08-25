import os, sqlite3, sys, tempfile, urllib.parse
from pathlib import Path

B = chr(92)
PREFIX = B + B + "?" + B
DEVICE = B + B + "." + B
UNC_PREFIX = PREFIX + "UNC" + B

def extended(p):
    text = str(p)
    if sys.platform != "win32":
        return Path(os.path.abspath(text))
    if text.startswith(PREFIX) or text.startswith(DEVICE):
        return Path(text)
    absolute = os.path.abspath(text)
    if absolute.startswith(B + B):
        return Path(UNC_PREFIX + absolute[2:])
    return Path(PREFIX + absolute)

def uri(p, mode="ro"):
    return "file:" + urllib.parse.quote(str(p), safe=":/") + "?mode=" + mode

root = Path(tempfile.mkdtemp(prefix="vm-uri2-"))
cases = {}

short = extended(root / "short.db")
sqlite3.connect(str(short)).execute("CREATE TABLE t(x)")
cases["short"] = short

odd = extended(root / "has space #hash %pct +plus" / "odd.db")
odd.parent.mkdir(parents=True, exist_ok=True)
sqlite3.connect(str(odd)).execute("CREATE TABLE t(x)")
cases["odd characters"] = odd

deep = extended(root / ("p" * 70) / ("q" * 70) / ("r" * 70) / "deep.db")
deep.parent.mkdir(parents=True, exist_ok=True)
sqlite3.connect(str(deep)).execute("CREATE TABLE t(x)")
cases["deep(" + str(len(str(deep))) + ")"] = deep

print("idempotent:", str(extended(extended(deep))) == str(extended(deep)))
print("no double prefix:", str(deep).count("?" + B) == 1)

for label, path in cases.items():
    try:
        sqlite3.connect(uri(path), uri=True).execute("SELECT count(*) FROM t").fetchone()
        print("ro  OK   ", label)
    except Exception as exc:
        print("ro  FAIL ", label, type(exc).__name__, str(exc)[:50], "|", uri(path)[:64])

# ATTACH must accept the same string, and a relative-path caller must still work.
main = extended(root / "main.db")
conn = sqlite3.connect(str(main), isolation_level=None)
try:
    conn.execute("ATTACH DATABASE ? AS deep", (str(deep),))
    print("attach OK")
except Exception as exc:
    print("attach FAIL", type(exc).__name__, str(exc)[:60])

rel = Path("_vm_scratch") / "rel.db"
print("relative absolutised:", str(extended(rel))[:12], "len", len(str(extended(rel))))

# rglob/relative_to must still yield clean logical paths from an extended root.
found = sorted(p.relative_to(extended(root)).as_posix() for p in extended(root).rglob("*.db"))
print("logical names:", found)
print("prefix leaked into logical names:", any("?" in n for n in found))
