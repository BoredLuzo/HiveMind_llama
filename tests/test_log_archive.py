# -*- coding: utf-8 -*-
"""SLOT-LOG-CLEANUP archives before deleting (Bench-Phase-A rule).

2026-10-10 the cleanup unlinked old slot logs outright - the 100k-run log
analysis phase made "no deletion without an archive copy" a hard rule.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.manager_load import _archive_old_slot_logs


def _mk(log_dir, name):
    p = log_dir / name
    p.write_text("x" * 32, encoding="utf-8")
    return p


def test_archive_moves_beyond_keep(tmp=None):
    log_dir = Path(__file__).parent / "_tmp_log_archive"
    if log_dir.exists():
        import shutil
        shutil.rmtree(log_dir)
    log_dir.mkdir()
    try:
        # 12 dated logs for port 8102 + logs of other ports (must not move)
        names = [f"llama_server_8102_2026102{i}-120000.log" for i in range(0, 6)] + \
                [f"llama_server_8102_20261027-{h}0000.log" for h in ("14", "15", "16", "17", "18", "19")]
        for n in names:
            _mk(log_dir, n)
        _mk(log_dir, "llama_server_8101_20261020-120000.log")
        _mk(log_dir, "llama_server_8102.log")  # no timestamp suffix - not matched

        _archive_old_slot_logs(log_dir, 8102, keep=10)

        main = sorted(p.name for p in log_dir.glob("llama_server_8102_*.log"))
        arch = sorted(p.name for p in (log_dir / "archive").glob("*.log"))
        assert len(main) == 10, f"expected 10 in main, got {len(main)}: {main}"
        assert len(arch) == 2, f"expected 2 archived, got {len(arch)}: {arch}"
        # oldest two are the archived ones
        assert all("2026102" in n and ("-120000" in n) for n in arch), arch
        # nothing was deleted: 12 port-8102 logs still exist somewhere
        total = len(main) + len(arch)
        assert total == 12, f"12 expected, {total} survived"
        # other ports untouched, fixed-name log untouched
        assert (log_dir / "llama_server_8101_20261020-120000.log").exists()
        assert (log_dir / "llama_server_8102.log").exists()
        # newest stayed in main (not archived)
        assert any("20261027-190000" in n for n in main), main
        print("PASS archive keeps 10, moves 2 oldest, deletes nothing, others untouched")
    finally:
        import shutil
        shutil.rmtree(log_dir, ignore_errors=True)


def test_archive_rejects_nothing_when_under_keep():
    log_dir = Path(__file__).parent / "_tmp_log_archive2"
    if log_dir.exists():
        import shutil
        shutil.rmtree(log_dir)
    log_dir.mkdir()
    try:
        _mk(log_dir, "llama_server_8102_20261027-140000.log")
        _archive_old_slot_logs(log_dir, 8102, keep=10)
        assert len(list(log_dir.glob("llama_server_8102_*.log"))) == 1
        assert not (log_dir / "archive").exists() or not list((log_dir / "archive").glob("*")), \
            "archive must stay empty when nothing exceeded keep"
        print("PASS under-keep is a no-op")
    finally:
        import shutil
        shutil.rmtree(log_dir, ignore_errors=True)


def test_archive_same_name_gets_suffix():
    """P10 (2026-10-29): an archive entry is NEVER overwritten - the
    incoming log gets a numeric suffix, both copies survive."""
    log_dir = Path(__file__).parent / "_tmp_log_archive3"
    if log_dir.exists():
        import shutil
        shutil.rmtree(log_dir)
    log_dir.mkdir()
    try:
        name = "llama_server_8102_20261027-140000.log"
        _mk(log_dir, name)
        arch = log_dir / "archive"
        arch.mkdir()
        (arch / name).write_text("old", encoding="utf-8")
        _archive_old_slot_logs(log_dir, 8102, keep=0)
        assert not list(log_dir.glob("llama_server_8102_*.log"))
        # old entry untouched
        assert (arch / name).read_text(encoding="utf-8") == "old"
        # new entry got a suffix, not the same name
        moved = sorted(p.name for p in arch.glob("llama_server_8102_*"))
        assert moved == [name, "llama_server_8102_20261027-140000_1.log"], moved
        print("PASS archive collision gets a suffix, old entry survives")
    finally:
        import shutil
        shutil.rmtree(log_dir, ignore_errors=True)


if __name__ == "__main__":
    test_archive_moves_beyond_keep()
    test_archive_rejects_nothing_when_under_keep()
    test_archive_same_name_gets_suffix()
    print("ALL PASS")
