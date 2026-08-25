"""The POSIX and Windows lock backends must expose the same observable contract."""
from __future__ import annotations

import errno
import os
import sqlite3
import threading
from pathlib import Path

import pytest

from src.core import portability


def test_platform_report_states_enforced_guarantees():
    report = portability.platform_report()
    assert report["platform"] == ("win32" if portability.WINDOWS else report["platform"])
    assert report["shared_locks_supported"] is True
    assert report["directory_fsync_supported"] is (not portability.WINDOWS)
    assert report["advisory_file_locking"] in {"fcntl.flock", "LockFileEx"}


def test_exclusive_lock_excludes_a_second_holder(tmp_path):
    target = tmp_path / "ledger.jsonl"
    target.write_text("seed\n", encoding="utf-8")
    with open(target, "a", encoding="utf-8") as first:
        with portability.exclusive_lock(first):
            with open(target, "a", encoding="utf-8") as second:
                with pytest.raises(BlockingIOError):
                    portability.lock_exclusive(second, blocking=False)


def test_shared_locks_coexist(tmp_path):
    target = tmp_path / "ledger.jsonl"
    target.write_text("seed\n", encoding="utf-8")
    with open(target, encoding="utf-8") as first, open(target, encoding="utf-8") as second:
        with portability.shared_lock(first):
            portability.lock_shared(second, blocking=False)
            portability.unlock(second)


def test_shared_lock_blocks_an_exclusive_acquire(tmp_path):
    target = tmp_path / "ledger.jsonl"
    target.write_text("seed\n", encoding="utf-8")
    with open(target, encoding="utf-8") as reader:
        with portability.shared_lock(reader):
            with open(target, "a", encoding="utf-8") as writer:
                with pytest.raises(BlockingIOError):
                    portability.lock_exclusive(writer, blocking=False)


def test_release_lets_the_next_holder_in(tmp_path):
    target = tmp_path / "ledger.jsonl"
    target.write_text("seed\n", encoding="utf-8")
    with open(target, "a", encoding="utf-8") as first:
        with portability.exclusive_lock(first):
            pass
        with open(target, "a", encoding="utf-8") as second:
            portability.lock_exclusive(second, blocking=False)
            portability.unlock(second)


def test_blocking_acquire_waits_for_the_holder(tmp_path):
    target = tmp_path / "ledger.jsonl"
    target.write_text("seed\n", encoding="utf-8")
    order: list[str] = []
    released = threading.Event()

    def hold() -> None:
        with open(target, "a", encoding="utf-8") as handle:
            with portability.exclusive_lock(handle):
                order.append("held")
                released.wait(5)
        order.append("released")

    holder = threading.Thread(target=hold)
    holder.start()
    while "held" not in order:
        pass
    released.set()
    holder.join(10)
    with open(target, "a", encoding="utf-8") as handle:
        with portability.exclusive_lock(handle):
            order.append("acquired")
    assert order == ["held", "released", "acquired"]


def test_fsync_directory_accepts_a_directory(tmp_path):
    portability.fsync_directory(tmp_path)


def test_fsync_directory_rejects_a_file(tmp_path):
    """A file here means the caller thinks it took a rename barrier that was never taken.

    Both platforms have to refuse it the same way, or the durability contract depends on where
    the code runs: POSIX lets the kernel refuse during the open, Windows checks the type, and a
    caller reading ``errno`` sees ``ENOTDIR`` either way.
    """
    target = tmp_path / "not-a-directory"
    target.write_text("x", encoding="utf-8")
    with pytest.raises(NotADirectoryError) as caught:
        portability.fsync_directory(target)
    assert caught.value.errno == errno.ENOTDIR


def test_private_append_round_trips_without_newline_translation(tmp_path):
    target = tmp_path / "trace.jsonl"
    portability.create_private_file(target)
    descriptor = portability.open_append_nofollow(target)
    try:
        os.write(descriptor, b'{"a":1}\n{"b":2}\n')
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    assert target.read_bytes() == b'{"a":1}\n{"b":2}\n'


def test_create_private_file_is_idempotent(tmp_path):
    target = tmp_path / "trace.jsonl"
    portability.create_private_file(target)
    target.write_bytes(b"kept\n")
    portability.create_private_file(target)
    assert target.read_bytes() == b"kept\n"


def test_create_private_file_refuses_a_symlink(tmp_path):
    real = tmp_path / "real.jsonl"
    real.write_text("x", encoding="utf-8")
    link = tmp_path / "link.jsonl"
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are unavailable to this account")
    with pytest.raises(ValueError):
        portability.create_private_file(link)


def test_open_append_refuses_a_symlink(tmp_path):
    real = tmp_path / "real.jsonl"
    real.write_text("x", encoding="utf-8")
    link = tmp_path / "link.jsonl"
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are unavailable to this account")
    with pytest.raises(OSError):
        descriptor = portability.open_append_nofollow(link)
        os.close(descriptor)


def test_posix_backend_is_absent_on_windows():
    if portability.WINDOWS:
        assert "fcntl" not in dir(portability)
    else:
        assert not hasattr(portability, "msvcrt")


# --------------------------------------------------------------------------- long paths

BACKSLASH = chr(92)
LONG_PATH_PREFIX = BACKSLASH * 2 + "?" + BACKSLASH


def _past_the_limit(root: Path) -> Path:
    """A path comfortably longer than the 260 characters Windows allows by default."""
    deep = portability.extended_path(root).joinpath(*("segment" + "x" * 60,) * 4)
    assert len(str(deep)) > 260
    return deep


def test_a_relative_root_is_absolutised_on_every_platform():
    converted = portability.extended_path(Path("relative") / "root")
    assert converted.is_absolute()
    assert converted.name == "root"


def test_conversion_is_idempotent_because_a_doubled_prefix_opens_nothing(tmp_path):
    once = portability.extended_path(tmp_path)
    assert portability.extended_path(once) == once
    assert str(once).count("?" + BACKSLASH) == (1 if portability.WINDOWS else 0)


def test_only_windows_carries_a_prefix(tmp_path):
    converted = str(portability.extended_path(tmp_path))
    assert converted.startswith(LONG_PATH_PREFIX) is portability.WINDOWS


def test_a_network_share_takes_the_unc_form_of_the_prefix():
    if not portability.WINDOWS:
        pytest.skip("UNC paths are a Windows concept")
    share = BACKSLASH * 2 + "server" + BACKSLASH + "share" + BACKSLASH + "run"
    assert str(portability.extended_path(share)) == LONG_PATH_PREFIX + "UNC" + BACKSLASH + (
        "server" + BACKSLASH + "share" + BACKSLASH + "run"
    )


def test_a_path_past_the_limit_supports_the_operations_the_stores_need(tmp_path):
    deep = _past_the_limit(tmp_path)
    deep.mkdir(parents=True)
    blob = deep / ("d" * 64)
    blob.write_bytes(b"content")
    assert blob.read_bytes() == b"content"
    assert blob.exists() and blob.stat().st_size == 7
    assert not blob.is_symlink()
    assert blob.resolve().is_relative_to(portability.extended_path(tmp_path))


def test_the_prefix_never_reaches_a_recorded_relative_name(tmp_path):
    """Backup manifests record ``relative_to(root)``, which reviewers read; it must stay clean."""
    deep = _past_the_limit(tmp_path)
    deep.mkdir(parents=True)
    (deep / "artifact.bin").write_bytes(b"x")
    root = portability.extended_path(tmp_path)
    recorded = [item.relative_to(root).as_posix() for item in root.rglob("artifact.bin")]
    assert recorded and all("?" not in name and BACKSLASH not in name for name in recorded)


def test_a_database_past_the_limit_can_be_opened_read_only(tmp_path):
    deep = _past_the_limit(tmp_path)
    deep.mkdir(parents=True)
    database = deep / "artifacts.db"
    with sqlite3.connect(str(database), isolation_level=None) as writer:
        writer.execute("CREATE TABLE artifact(digest TEXT)")
        writer.execute("INSERT INTO artifact VALUES ('abc')")
    reader = sqlite3.connect(portability.sqlite_readonly_uri(database), uri=True)
    try:
        assert reader.execute("SELECT digest FROM artifact").fetchone()[0] == "abc"
        with pytest.raises(sqlite3.OperationalError):
            reader.execute("INSERT INTO artifact VALUES ('nope')")
    finally:
        reader.close()


def test_a_read_only_uri_survives_characters_the_uri_parser_would_claim(tmp_path):
    directory = tmp_path / "has space #hash %pct"
    directory.mkdir()
    database = directory / "jobs.db"
    with sqlite3.connect(str(database), isolation_level=None) as writer:
        writer.execute("CREATE TABLE job(job_id TEXT)")
    reader = sqlite3.connect(portability.sqlite_readonly_uri(database), uri=True)
    try:
        assert reader.execute("SELECT count(*) FROM job").fetchone()[0] == 0
    finally:
        reader.close()


def test_a_posix_read_only_uri_is_unchanged_by_the_encoding():
    if portability.WINDOWS:
        pytest.skip("the plain interpolation is only preserved where no prefix is added")
    assert portability.sqlite_readonly_uri("/var/lib/verimat/jobs.db") == (
        "file:/var/lib/verimat/jobs.db?mode=ro"
    )


def test_platform_report_states_the_path_length_limit():
    report = portability.platform_report()
    assert report["path_length_limit"] == (260 if portability.WINDOWS else None)
    assert report["path_length_limit_bypassed"] == (
        "extended-length prefix" if portability.WINDOWS else "not applicable"
    )
