"""add-folder.sh: connect a folder as block storage by editing .env and recreating the indexer.

The script runs against a copy in a temp dir, with stand-ins for docker and macOS's `open`.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "add-folder.sh"

FAKE_DOCKER = """#!/usr/bin/env bash
echo "$*" >> "$DOCKER_LOG"
case "$1" in
  inspect) echo healthy ;;
  exec) case "$*" in *python*) echo "The indexer accepted it" ;; *) echo somefile ;; esac ;;
esac
"""

FAKE_OPEN = """#!/usr/bin/env bash
# smb://host/Share%20One -> $VOLUMES_DIR/Share One with a file in it
share="${1#*://}"; share="${share#*/}"; share="${share%%/*}"; share="${share//%20/ }"
mkdir -p "$VOLUMES_DIR/$share/Sub" && touch "$VOLUMES_DIR/$share/Sub/a.txt"
echo "$1" >> "$OPEN_LOG"
"""


@pytest.fixture
def env(tmp_path):
    work = tmp_path / "repo"
    work.mkdir()
    shutil.copy(SCRIPT, work / "add-folder.sh")
    for name, body in (("docker", FAKE_DOCKER), ("open", FAKE_OPEN)):
        f = tmp_path / name
        f.write_text(body)
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    vols = tmp_path / "Volumes"
    vols.mkdir()
    dotenv = work / ".env"
    dotenv.write_text("VAULT_KEY=secret\nBLOCK_SOURCE_PATH=/srv/one\nBLOCK_SOURCES=One=/block/source1\n")
    dotenv.chmod(0o600)

    def run(*args, check=True):
        e = dict(os.environ, DOCKER=str(tmp_path / "docker"), OPEN_CMD=str(tmp_path / "open"),
                 VOLUMES_DIR=str(vols), MOUNT_WAIT_SEC="3", DOCKER_LOG=str(tmp_path / "docker.log"),
                 OPEN_LOG=str(tmp_path / "open.log"))
        r = subprocess.run(["bash", str(work / "add-folder.sh"), *args], env=e, capture_output=True,
                           text=True, stdin=subprocess.DEVNULL)
        if check:
            assert r.returncode == 0, r.stdout + r.stderr
        return r

    def values():
        out = {}
        for line in dotenv.read_text().splitlines():
            k, _, v = line.partition("=")
            out[k] = v
        return out

    return type("Env", (), {"run": staticmethod(run), "values": staticmethod(values), "dotenv": dotenv,
                            "tmp": tmp_path, "vols": vols})


def test_adds_a_local_folder_to_the_first_free_slot(env):
    folder = env.tmp / "My Projects!"
    folder.mkdir()
    r = env.run(str(folder), "--yes")
    v = env.values()
    assert v["BLOCK_SOURCE_PATH_2"] == str(folder.resolve())
    assert v["BLOCK_SOURCES"] == "One=/block/source1;My-Projects=/block/source2"
    assert v["VAULT_KEY"] == "secret" and v["BLOCK_SOURCE_PATH"] == "/srv/one"
    assert stat.S_IMODE(env.dotenv.stat().st_mode) == 0o600
    backups = list(env.dotenv.parent.glob(".env.bak-*"))
    assert len(backups) == 1 and stat.S_IMODE(backups[0].stat().st_mode) == 0o600
    log = (env.tmp / "docker.log").read_text()
    assert "compose up -d lucidlink-indexer" in log and "exec" in log
    assert "accepted" in r.stdout


def test_mounts_an_smb_url_and_uses_the_subfolder(env):
    env.run("smb://nas.local/Share%20One/Sub", "--name", "NAS", "--yes")
    v = env.values()
    assert v["BLOCK_SOURCE_PATH_2"] == str((env.vols / "Share One" / "Sub").resolve())
    assert v["BLOCK_SOURCES"].endswith(";NAS=/block/source2")
    assert (env.tmp / "open.log").read_text().strip() == "smb://nas.local/Share%20One/Sub"


def test_skips_mounting_a_share_that_is_already_mounted(env):
    (env.vols / "Archive").mkdir()
    (env.vols / "Archive" / "x.txt").write_text("x")
    env.run("smb://nas.local/Archive", "--yes", "--no-restart")
    assert not (env.tmp / "open.log").exists()
    assert not (env.tmp / "docker.log").exists()
    assert env.values()["BLOCK_SOURCES"].endswith("Archive=/block/source2")


def test_refuses_duplicates_overlaps_and_bad_names(env):
    one = env.tmp / "one"
    (one / "inner").mkdir(parents=True)
    env.dotenv.write_text(f"BLOCK_SOURCE_PATH={one}\nBLOCK_SOURCES=One=/block/source1\n")
    before = env.dotenv.read_text()
    assert "already connected" in env.run(str(one), "--yes", check=False).stderr
    assert "inside slot 1" in env.run(str(one / "inner"), "--yes", check=False).stderr
    assert "is inside" in env.run(str(env.tmp), "--yes", check=False).stderr
    other = env.tmp / "other"
    other.mkdir()
    assert "already exists" in env.run(str(other), "--name", "One", "--yes", check=False).stderr
    assert "can't contain" in env.run(str(other), "--name", "a;b", "--yes", check=False).stderr
    assert "not a folder" in env.run(str(env.tmp / "missing"), "--yes", check=False).stderr
    assert env.dotenv.read_text() == before


def test_refuses_when_all_slots_are_full(env):
    lines = []
    for n in range(1, 7):
        d = env.tmp / f"s{n}"
        d.mkdir()
        lines.append(f"BLOCK_SOURCE_PATH{'' if n == 1 else f'_{n}'}={d}")
    env.dotenv.write_text("\n".join(lines) + "\n")
    extra = env.tmp / "extra"
    extra.mkdir()
    assert "all six slots" in env.run(str(extra), "--yes", check=False).stderr


def test_needs_yes_without_a_terminal(env):
    folder = env.tmp / "f"
    folder.mkdir()
    r = env.run(str(folder), check=False)
    assert r.returncode != 0 and "--yes" in r.stderr
    assert "BLOCK_SOURCE_PATH_2" not in env.dotenv.read_text()


def test_list_and_remove(env):
    folder = env.tmp / "Two"
    folder.mkdir()
    env.run(str(folder), "--yes", "--no-restart")
    out = env.run("--list").stdout
    assert "1  One  ->  /srv/one" in out and f"2  Two  ->  {folder.resolve()}" in out and "3  free" in out
    env.run("--remove", "One", "--yes", "--no-restart")
    v = env.values()
    assert v["BLOCK_SOURCES"] == "Two=/block/source2"
    assert v["BLOCK_SOURCE_PATH"] == ""
    assert "no source called" in env.run("--remove", "Nope", "--yes", check=False).stderr


def test_icloud_drive_gets_a_friendly_name(env):
    icloud = env.tmp / "Mobile Documents" / "com~apple~CloudDocs"
    icloud.mkdir(parents=True)
    env.run(str(icloud), "--yes", "--no-restart")
    assert env.values()["BLOCK_SOURCES"].endswith(";iCloud=/block/source2")
