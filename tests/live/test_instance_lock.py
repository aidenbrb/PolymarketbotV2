import os

import pytest

from polymarket_bot.live import instance_lock as lock_module
from polymarket_bot.live.instance_lock import AlreadyRunningError, InstanceLock


def test_acquire_when_unlocked_succeeds(tmp_path):
    with InstanceLock(lock_path=tmp_path / "live_bot.lock"):
        pass  # must not raise


def test_second_acquire_while_first_held_raises_already_running(tmp_path):
    lock_path = tmp_path / "live_bot.lock"
    lock1 = InstanceLock(lock_path=lock_path)
    lock1.__enter__()
    try:
        with pytest.raises(AlreadyRunningError):
            with InstanceLock(lock_path=lock_path):
                pass
    finally:
        lock1.__exit__(None, None, None)


def test_lock_released_after_exit_allows_reacquire(tmp_path):
    lock_path = tmp_path / "live_bot.lock"
    with InstanceLock(lock_path=lock_path):
        pass
    with InstanceLock(lock_path=lock_path):  # must not raise -- first lock fully released
        pass


def test_lock_released_after_exception_inside_with_block(tmp_path):
    lock_path = tmp_path / "live_bot.lock"
    with pytest.raises(RuntimeError):
        with InstanceLock(lock_path=lock_path):
            raise RuntimeError("boom")
    with InstanceLock(lock_path=lock_path):  # must not raise -- __exit__ still ran
        pass


def test_lock_file_contains_pid_after_acquiring(tmp_path):
    # Read AFTER the lock is released, not while held -- Windows file
    # locking is mandatory, so a second handle can't even read the locked
    # byte range while another handle holds it (confirmed via Microsoft's
    # own docs during this feature's design review).
    lock_path = tmp_path / "live_bot.lock"
    with InstanceLock(lock_path=lock_path):
        pass
    content = lock_path.read_bytes().decode("utf-8")
    assert str(os.getpid()) in content


def test_already_running_error_message_mentions_lock_path(tmp_path):
    lock_path = tmp_path / "live_bot.lock"
    lock1 = InstanceLock(lock_path=lock_path)
    lock1.__enter__()
    try:
        with pytest.raises(AlreadyRunningError, match=r"live_bot\.lock"):
            with InstanceLock(lock_path=lock_path):
                pass
    finally:
        lock1.__exit__(None, None, None)


def test_creates_parent_directory_if_missing(tmp_path):
    nested = tmp_path / "nested" / "dir" / "live_bot.lock"
    with InstanceLock(lock_path=nested):
        pass
    assert nested.exists()


def test_no_lock_path_uses_module_level_lock_file(tmp_path, monkeypatch):
    monkeypatch.setattr(lock_module, "LOCK_FILE", tmp_path / "live_bot.lock")
    with InstanceLock():
        pass
    assert (tmp_path / "live_bot.lock").exists()
