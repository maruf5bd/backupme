from backupme.core.skip import should_skip, should_skip_dir


def test_user_exclude_with_forward_slashes_matches_windows_paths():
    skip, why = should_skip(r"C:\data\subdir\large.csv", user_excludes=["subdir/*.csv"])
    assert skip, why


def test_user_dir_named_tmp_is_not_skipped():
    skip, why = should_skip(r"C:\projects\mytmp\keep.txt")
    assert not skip, why
    assert not should_skip_dir(r"C:\projects\mytmp")


def test_system_temp_still_skipped():
    skip, _ = should_skip(r"C:\Users\x\AppData\Local\Temp\junk.dat")
    assert skip


def test_own_database_files_skipped():
    for name in ("backupme.db", "backupme.db-wal", "backupme.db-shm"):
        skip, _ = should_skip(f"C:\\app\\{name}")
        assert skip, name


def test_regular_log_still_skipped():
    skip, _ = should_skip(r"C:\logs\app.log")
    assert skip


def test_absolute_path_skips_resolve(monkeypatch):
    from backupme.core import skip as skip_mod

    def boom(*a, **kw):
        raise AssertionError("resolve() called for absolute path")

    monkeypatch.setattr(skip_mod.Path, "resolve", boom)
    skip, why = skip_mod.should_skip(r"C:\logs\app.log")
    assert skip
