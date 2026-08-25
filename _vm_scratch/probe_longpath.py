import hashlib, os, tempfile
from pathlib import Path

B = chr(92)
PREFIX = B + B + "?" + B

root = Path(tempfile.mkdtemp(prefix="vm-lp-"))
pad = root / ("p" * 60) / ("q" * 60)
pad.mkdir(parents=True)

digest = hashlib.sha256(b"x").hexdigest()
tenant = hashlib.sha256(b"tenant").hexdigest()
deep_rel = Path("tenants") / tenant / "blobs" / digest[:2] / digest

plain = pad / deep_rel
print("plain length:", len(str(plain)))
try:
    plain.parent.mkdir(parents=True, exist_ok=True)
    plain.write_bytes(b"payload")
    print("PLAIN: unexpectedly ok")
except OSError as exc:
    print("PLAIN fails as expected:", type(exc).__name__, "winerror", getattr(exc, "winerror", None))

ext_base = Path(PREFIX + os.path.abspath(pad))
print("prefix survived:", str(ext_base).startswith(PREFIX))
print("ext drive:", repr(ext_base.drive), "anchor:", repr(ext_base.anchor))
ext = ext_base / deep_rel
print("ext length:", len(str(ext)))

ext.parent.mkdir(parents=True, exist_ok=True)
ext.write_bytes(b"payload")
print("EXT write ok, read back:", ext.read_bytes())
print("EXT exists:", ext.exists(), "size:", ext.stat().st_size)

fd, name = tempfile.mkstemp(prefix="." + digest + ".", dir=str(ext.parent))
os.close(fd)
print("mkstemp ok, len:", len(name), "keeps prefix:", name.startswith(PREFIX))
target = ext.parent / (digest[:8] + ".link")
os.link(name, str(target))
print("os.link ok:", target.stat().st_size == 0)
os.unlink(name)

d = os.open(str(ext.parent), os.O_RDONLY)
os.close(d)
print("os.open on ext dir ok")

found = sorted(p.relative_to(ext_base).as_posix() for p in ext_base.rglob("*") if p.is_file())
print("rglob relative:", found)
print("logical path clean:", deep_rel.as_posix() in found)
