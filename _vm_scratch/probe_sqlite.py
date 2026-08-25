import os, sqlite3, tempfile, urllib.parse
from pathlib import Path

B = chr(92)
PREFIX = B + B + "?" + B
root = Path(tempfile.mkdtemp(prefix="vm-sql-"))
pad = root / ("p" * 70) / ("q" * 70) / ("r" * 40)
pad.mkdir(parents=True)

plain_db = pad / ("d" * 40) / "operations.db"
print("db path length:", len(str(plain_db)))

def attempt(label, fn):
    try:
        fn()
        print(label, "-> OK")
    except Exception as exc:
        print(label, "->", type(exc).__name__, str(exc)[:90])

ext_dir = Path(PREFIX + os.path.abspath(plain_db.parent))
ext_dir.mkdir(parents=True, exist_ok=True)
ext_db = ext_dir / "operations.db"

attempt("1 plain long path, plain connect", lambda: sqlite3.connect(str(plain_db)).execute("CREATE TABLE IF NOT EXISTS t(x)"))
attempt("2 ext long path, plain connect", lambda: sqlite3.connect(str(ext_db)).execute("CREATE TABLE IF NOT EXISTS t(x)"))
print("   db created:", ext_db.exists(), "size", ext_db.stat().st_size if ext_db.exists() else None)

attempt("3 ext path, raw URI (naive f-string)", lambda: sqlite3.connect(f"file:{ext_db}?mode=ro", uri=True).execute("SELECT 1"))
quoted = urllib.parse.quote(str(ext_db).replace(B, "/"), safe="/:")
attempt("4 ext path, quoted URI " + quoted[:24], lambda: sqlite3.connect("file:" + quoted + "?mode=ro", uri=True).execute("SELECT 1"))
quoted_bs = urllib.parse.quote(str(ext_db), safe=":")
attempt("5 ext path, quoted backslashes", lambda: sqlite3.connect("file:" + quoted_bs + "?mode=ro", uri=True).execute("SELECT 1"))
attempt("6 ext path, ATTACH as plain string", lambda: sqlite3.connect(":memory:").execute("ATTACH DATABASE ? AS a", (str(ext_db),)))
attempt("7 WAL on ext path", lambda: sqlite3.connect(str(ext_db)).execute("PRAGMA journal_mode=WAL").fetchone())
