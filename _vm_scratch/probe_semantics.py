import hashlib, os, shutil, tempfile
from pathlib import Path

B = chr(92)
PREFIX = B + B + "?" + B

def ext(p):
    return Path(PREFIX + os.path.abspath(p))

root = Path(tempfile.mkdtemp(prefix="vm-sem-"))
pad = root / ("p" * 70) / ("q" * 70)
pad.mkdir(parents=True)
digest = hashlib.sha256(b"x").hexdigest()
rel = Path("tenants") / hashlib.sha256(b"t").hexdigest() / "blobs" / digest[:2] / digest

base = ext(pad)
deep = base / rel
deep.parent.mkdir(parents=True, exist_ok=True)
deep.write_bytes(b"payload")
print("deep length:", len(str(deep)))

print("1 resolve keeps prefix:", str(deep.resolve()).startswith(PREFIX), "| len", len(str(deep.resolve())))
print("2 is_relative_to(ext base):", deep.resolve().is_relative_to(base.resolve()))
print("3 abspath idempotent:", os.path.abspath(str(deep)) == str(deep))
print("4 double-prefix guarded:", str(ext(deep)) == str(deep))

found = sorted(p.relative_to(base).as_posix() for p in base.rglob("*") if p.is_file())
print("5 rglob+relative_to clean:", found == [rel.as_posix()], found[:1])

with deep.open("rb") as h:
    print("6 open('rb') ok:", h.read() == b"payload")

fd = os.open(str(deep.parent / "audit.jsonl"), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as h:
    h.write("{}\n")
print("7 os.open O_CREAT ok:", (deep.parent / "audit.jsonl").stat().st_size == 3)

tmp = deep.parent / (digest + ".tmp")
tmp.write_bytes(b"replaced")
os.replace(str(tmp), str(deep))
print("8 os.replace ok:", deep.read_bytes() == b"replaced")

print("9 is_symlink ok:", deep.is_symlink() is False)
print("10 name/parts sane:", deep.name == digest, deep.parts[0] == PREFIX + "C:" + B)

dst = ext(pad) / "copy"
shutil.copytree(str(base / "tenants"), str(dst), symlinks=False)
print("11 copytree ok:", len(list(dst.rglob("*"))) > 0)

plain_deep = pad / rel
print("12 MIXING FORMS -- ext.relative_to(plain):", end=" ")
try:
    deep.relative_to(pad)
    print("ok (unexpected)")
except ValueError as exc:
    print("ValueError ->", str(exc)[:60])
print("13 plain long path stat:", end=" ")
try:
    print(plain_deep.stat().st_size)
except OSError as exc:
    print(type(exc).__name__, getattr(exc, "winerror", None))
