"""PostgreSQL WAL segment archiver, base backup runner, and isolated PITR drill."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import time
from typing import Any

import psycopg

from evolvmem.pg_test_guard import PINNED_PGVECTOR_IMAGE, assert_safe_test_pg_target


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def archive_wal_segment(src_path: Path, archive_dir: Path, segment_name: str) -> dict[str, Any]:
    """Atomically archive one WAL segment and verify SHA-256 without silent overwrite."""
    if not segment_name or "/" in segment_name or "\\" in segment_name or ".." in segment_name:
        raise ValueError(f"invalid_wal_segment_name:{segment_name!r}")
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_dir.chmod(0o700)
    src = Path(src_path)
    src_hash = _sha256_file(src)
    dst = archive_dir / segment_name
    digest_file = archive_dir / f"{segment_name}.sha256"

    def _fsync_dir(dirpath: Path) -> None:
        dfd = os.open(dirpath, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)

    def _write_0600(target: Path, text: str) -> None:
        dtmp = target.parent / f".{target.name}.{os.getpid()}.tmp"
        dfd = os.open(dtmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(dfd, text.encode("utf-8"))
            os.fsync(dfd)
        finally:
            os.close(dfd)
        os.replace(dtmp, target)
        _fsync_dir(target.parent)

    if dst.exists():
        existing_hash = _sha256_file(dst)
        if existing_hash != src_hash:
            raise ValueError(f"wal_checksum_conflict:{segment_name}")
        if not digest_file.exists():
            _write_0600(digest_file, f"{src_hash}  {segment_name}\n")
        return {"status": "already_archived_verified", "segment": segment_name, "sha256": src_hash}

    tmp = archive_dir / f".{segment_name}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with src.open("rb") as src_fh:
            while True:
                chunk = src_fh.read(65536)
                if not chunk:
                    break
                view = memoryview(chunk)
                while view:
                    n = os.write(fd, view)
                    view = view[n:]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, dst)
    _fsync_dir(archive_dir)
    _write_0600(digest_file, f"{src_hash}  {segment_name}\n")
    return {"status": "archived", "segment": segment_name, "sha256": src_hash, "bytes": dst.stat().st_size}


def take_base_backup(
    container_name: str,
    backup_dir: Path,
    *,
    port: int,
    password_file: Path,
    allow_prod: bool = False,
) -> dict[str, Any]:
    """Create a physical pg_basebackup from a PG container into a 0700 host directory."""
    if not allow_prod:
        assert_safe_test_pg_target(port=port, container_name=container_name, volumes=[str(backup_dir)])
    backup_dir = backup_dir.expanduser().resolve()
    if backup_dir.exists():
        shutil.rmtree(backup_dir, ignore_errors=True)
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_dir.chmod(0o700)

    pw = password_file.read_text(encoding="utf-8").strip()
    if "\n" in pw or "\r" in pw:
        raise ValueError("invalid_multiline_password_file")
    in_container_tmp = f"/tmp/em_basebak_{os.getpid()}_{secrets.token_hex(4)}"
    try:
        cmd = [
            "docker", "exec", "-i", container_name,
            "sh", "-c",
            (
                "IFS= read -r PGPASSWORD; export PGPASSWORD; "
                'exec pg_basebackup -h 127.0.0.1 -p 5432 -U postgres -D "$1" -Ft -z -Xs --checkpoint=fast'
            ),
            "sh", in_container_tmp,
        ]
        res = subprocess.run(cmd, input=f"{pw}\n", capture_output=True, text=True, check=False)
        if res.returncode != 0:
            raise RuntimeError(f"pg_basebackup_failed:{res.stderr.strip()}")
        cp = subprocess.run(
            ["docker", "cp", f"{container_name}:{in_container_tmp}/.", str(backup_dir)],
            capture_output=True, text=True, check=False,
        )
        if cp.returncode != 0:
            raise RuntimeError(f"docker_cp_backup_failed:{cp.stderr.strip()}")
    finally:
        subprocess.run(["docker", "exec", container_name, "rm", "-rf", in_container_tmp], capture_output=True, check=False)

    files = {}
    for item in sorted(backup_dir.iterdir()):
        if item.is_file():
            item.chmod(0o600)
            files[item.name] = {"bytes": item.stat().st_size, "sha256": _sha256_file(item)}
    manifest = {
        "ok": "base.tar.gz" in files or "base.tar" in files,
        "container": container_name,
        "backup_path": str(backup_dir),
        "files": files,
    }
    (backup_dir / "backup-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (backup_dir / "backup-manifest.json").chmod(0o600)
    return manifest


def _extract_tar(src_tar: Path, dest_dir: Path) -> None:
    cmd = ["tar", "-xzf" if src_tar.name.endswith(".gz") else "-xf", str(src_tar), "-C", str(dest_dir)]
    res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if res.returncode != 0:
        raise RuntimeError(f"extract_backup_failed:{src_tar.name}:{res.stderr.strip()}")


def run_pitr_restore_drill(
    *,
    base_backup_dir: Path,
    wal_archive_dir: Path,
    target_time_iso: str,
    restore_container_name: str,
    restore_port: int,
    password_file: Path,
    verify_db: str,
    pre_target_sql: str,
    post_target_sql: str,
) -> dict[str, Any]:
    """Restore base backup + WAL segments up to target_time_iso in an isolated PG container."""
    assert_safe_test_pg_target(
        port=restore_port,
        container_name=restore_container_name,
        volumes=[str(base_backup_dir), str(wal_archive_dir)],
    )
    wal_segments = sorted(
        p.name for p in wal_archive_dir.iterdir()
        if p.is_file() and len(p.name) == 24 and not p.name.endswith(".sha256")
    )
    if not wal_segments:
        raise RuntimeError("no_wal_segments_found_for_pitr")

    restore_root = wal_archive_dir.parent / f".pitr_restore_{restore_port}"
    shutil.rmtree(restore_root, ignore_errors=True)
    restore_root.mkdir(parents=True, exist_ok=True)
    restore_root.chmod(0o700)
    subprocess.run(["docker", "rm", "-f", restore_container_name], capture_output=True, check=False)

    pw = password_file.read_text(encoding="utf-8").strip()
    try:
        base_tar = next((p for p in (base_backup_dir / "base.tar.gz", base_backup_dir / "base.tar") if p.exists()), None)
        if base_tar is None:
            raise FileNotFoundError(f"missing_base_tar_in:{base_backup_dir}")
        _extract_tar(base_tar, restore_root)
        pg_wal_dir = restore_root / "pg_wal"
        pg_wal_dir.mkdir(parents=True, exist_ok=True)
        wal_tar = next((p for p in (base_backup_dir / "pg_wal.tar.gz", base_backup_dir / "pg_wal.tar") if p.exists()), None)
        if wal_tar is not None:
            _extract_tar(wal_tar, pg_wal_dir)

        (restore_root / "recovery.signal").write_text("", encoding="utf-8")
        auto_conf = restore_root / "postgresql.auto.conf"
        existing_conf = auto_conf.read_text(encoding="utf-8") if auto_conf.exists() else ""
        filtered = "\n".join(
            ln for ln in existing_conf.splitlines()
            if not ln.startswith(("restore_command", "recovery_target", "archive_mode", "archive_command"))
        )
        pitr_conf = (
            f"{filtered}\n"
            "restore_command = 'cp /wal_archive/%f %p'\n"
            f"recovery_target_time = '{target_time_iso}'\n"
            "recovery_target_inclusive = true\n"
            "recovery_target_action = 'promote'\n"
            "archive_mode = off\n"
        ).lstrip()
        auto_conf.write_text(pitr_conf, encoding="utf-8")
        restore_root.chmod(0o700)

        cmd = [
            "docker", "run", "-d", "--name", restore_container_name,
            "--user", f"{os.getuid()}:{os.getgid()}",
            "-v", "/etc/passwd:/etc/passwd:ro",
            "-v", "/etc/group:/etc/group:ro",
            "-p", f"127.0.0.1:{restore_port}:5432",
            "-v", f"{restore_root}:/pgdata",
            "-v", f"{wal_archive_dir}:/wal_archive:ro",
            "--label", "evolvmem.role=isolated-pitr-drill",
            "--entrypoint", "postgres",
            PINNED_PGVECTOR_IMAGE,
            "-D", "/pgdata",
            "-c", "shared_preload_libraries=pg_stat_statements",
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if res.returncode != 0:
            raise RuntimeError(f"pitr_container_start_failed:{res.stderr.strip()}")

        deadline = time.monotonic() + 30.0
        promoted = False
        last_err = ""
        while time.monotonic() < deadline:
            try:
                with psycopg.connect(
                    host="127.0.0.1", port=restore_port, dbname=verify_db,
                    user="postgres", password=pw, sslmode="disable",
                ) as conn:
                    in_rec = conn.execute("SELECT pg_is_in_recovery()").fetchone()[0]
                    if not in_rec:
                        promoted = True
                        pre_row = conn.execute(pre_target_sql).fetchone()
                        post_row = conn.execute(post_target_sql).fetchone()
                        reached = bool(promoted and pre_row is not None and post_row is None)
                        return {
                            "ok": reached,
                            "pitr_target_reached": reached,
                            "promoted": promoted,
                            "target_time_iso": target_time_iso,
                            "restore_port": restore_port,
                            "wal_segment_count": len(wal_segments),
                            "pre_target_present": pre_row is not None,
                            "post_target_absent": post_row is None,
                        }
            except Exception as exc:
                last_err = str(exc)
            time.sleep(0.5)
        raise RuntimeError(f"pitr_drill_timeout:promoted={promoted}:last_err={last_err}")
    finally:
        subprocess.run(["docker", "rm", "-f", restore_container_name], capture_output=True, check=False)
        shutil.rmtree(restore_root, ignore_errors=True)


def generate_prod_wal_config_patch() -> dict[str, Any]:
    """Emit ready-to-apply WAL archiving config for Stage 5B without touching production."""
    archive_dir = "/var/lib/evolvmem_wal_archive"
    archive_cmd = (
        f"umask 077 && if [ -f {archive_dir}/%f ]; then cmp -s %p {archive_dir}/%f; "
        f"else cp %p {archive_dir}/.%f.$$.tmp && chmod 600 {archive_dir}/.%f.$$.tmp && "
        f"sync {archive_dir}/.%f.$$.tmp && mv -n {archive_dir}/.%f.$$.tmp {archive_dir}/%f && "
        f"cmp -s %p {archive_dir}/%f || {{ rm -f {archive_dir}/.%f.$$.tmp; exit 1; }}; fi"
    )
    return {
        "requires_prod_restart": True,
        "target_service": "evolvmem-pg-prod (port 55432, scheduled for Stage 5B)",
        "postgres_flags": [
            "-c", "wal_level=replica",
            "-c", "archive_mode=on",
            "-c", "archive_timeout=300",
            "-c", f"archive_command={archive_cmd}",
        ],
        "host_wal_dir": "/home/jiangli/evolvmem-pg/wal_archive",
        "mount_spec": "/home/jiangli/evolvmem-pg/wal_archive:/var/lib/evolvmem_wal_archive",
    }
