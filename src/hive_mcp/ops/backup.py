"""Backup and restore of everything that makes up a deployment.

An archive (.tar.gz) holds: the database (SQLite snapshot, or a pg_dump of Postgres), the blob
store (session logs, content-addressed), and the small state files (tokens, users, cookie signing
key), plus a manifest with the SHA-256 of every member. Restore verifies the manifest first and
refuses anything that is not exactly what a backup produced.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from hive_mcp import __version__
from hive_mcp.config import Profile
from hive_mcp.domain.errors import ValidationError
from hive_mcp.ops.pgclient import check_client

if TYPE_CHECKING:
    from hive_mcp.config import Settings

MANIFEST = "manifest.json"
FORMAT = 1
STATE_FILES = ("tokens", "users", "secret")


@dataclass(frozen=True, slots=True)
class Summary:
    path: Path
    profile: str
    files: int
    bytes: int


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _is_blob(path: Path) -> bool:
    """False for the temporary file of a blob write in progress (or one a crash left behind): the
    blob store writes through `tempfile.mkstemp`, whose names start with "tmp" (a SHA-256 in hex
    never does). Such a file is not data, and it changes while it is being hashed."""
    return path.is_file() and not path.name.endswith(".tmp") and not path.name.startswith("tmp")


def _state_paths(settings: Settings) -> dict[str, Path]:
    return {
        "tokens": settings.tokens_path,
        "users": settings.users_path,
        "secret": settings.data_dir / "secret.key",
    }


def _snapshot_sqlite(source: Path, dest: Path) -> None:
    """A consistent copy while the server may be writing (SQLite's online backup API)."""
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        out = sqlite3.connect(dest)
        try:
            src.backup(out)
        finally:
            out.close()
    finally:
        src.close()


def _tool(name: str) -> str:
    found = shutil.which(name)
    if not found:
        raise ValidationError(f"{name} is not installed; it is needed for Postgres backups")
    return found


def _pg_env(dsn: str) -> dict[str, str]:
    """libpq environment for a DSN, so the password never appears in the process list."""
    from psycopg.conninfo import conninfo_to_dict  # noqa: PLC0415 - postgres extra

    names = {"host": "PGHOST", "port": "PGPORT", "user": "PGUSER", "password": "PGPASSWORD",
             "dbname": "PGDATABASE", "sslmode": "PGSSLMODE"}  # fmt: skip
    parts = conninfo_to_dict(dsn)
    return {**os.environ, **{env: str(parts[key]) for key, env in names.items() if key in parts}}


def _run(cmd: list[str], env: dict[str, str] | None = None) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True, check=False, env=env)  # noqa: S603
    if result.returncode:
        raise ValidationError(f"{Path(cmd[0]).name} failed: {result.stderr.strip()[:300]}")


def create_backup(settings: Settings, dest: Path) -> Summary:
    """Write a backup archive to `dest` (a file path ending in .tar.gz, or a directory)."""
    if dest.is_dir():
        dest = dest / f"hive-backup-{datetime.now(UTC):%Y%m%d-%H%M%S}.tar.gz"
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="hive-backup-") as tmp:
        stage = Path(tmp)
        members: dict[str, Path] = {}
        # Database first: blobs are only ever added, so copying them afterwards gives a superset
        # of what the snapshot refers to.
        if settings.profile is Profile.EMBEDDED:
            db = settings.data_dir / "hive.db"
            if not db.exists():
                raise ValidationError(f"no database at {db}")
            _snapshot_sqlite(db, stage / "hive.db")
            members["db/hive.db"] = stage / "hive.db"
        else:
            if not settings.database_url:
                raise ValidationError("HIVE_DATABASE_URL is required for the external profile")
            tool = _tool("pg_dump")
            check_client(tool, settings.database_url)
            dump = [tool, "--format=custom", f"--file={stage / 'hive.pgdump'}"]
            _run(dump, _pg_env(settings.database_url))
            members["db/hive.pgdump"] = stage / "hive.pgdump"
        blobs = settings.data_dir / "blobs"
        if blobs.is_dir():
            for f in sorted(p for p in blobs.rglob("*") if _is_blob(p)):
                members[f"blobs/{f.relative_to(blobs).as_posix()}"] = f
        for name, path in _state_paths(settings).items():
            if path.is_file():  # rewritten while the server runs: archive one fixed copy
                shutil.copy2(path, stage / f"state-{name}")
                members[f"state/{name}"] = stage / f"state-{name}"
        manifest = {
            "format": FORMAT,
            "created_at": datetime.now(UTC).isoformat(),
            "software": __version__,
            "profile": settings.profile.value,
            "files": {
                name: {"sha256": sha256_file(p), "size": p.stat().st_size}
                for name, p in members.items()
            },
        }
        (stage / MANIFEST).write_text(json.dumps(manifest, indent=1))
        partial = dest.with_name(dest.name + ".part")
        with tarfile.open(partial, "w:gz") as tar:
            tar.add(stage / MANIFEST, arcname=MANIFEST)
            for name, path in members.items():
                tar.add(path, arcname=name, recursive=False)
        partial.replace(dest)  # a half-written archive never has the final name
    total = sum(f["size"] for f in manifest["files"].values())  # type: ignore[attr-defined]
    return Summary(dest, settings.profile.value, len(members), total)


def _safe_extract(archive: Path, target: Path) -> dict[str, dict[str, object]]:
    """Unpack only regular files with plain relative names, then check every hash."""
    try:
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar.getmembers():
                parts = Path(member.name).parts
                if not member.isfile() or member.name.startswith("/") or ".." in parts:
                    raise ValidationError(f"unsafe or unexpected entry in archive: {member.name!r}")
            tar.extractall(target, filter="data")
    except (tarfile.TarError, EOFError, zlib.error, OSError) as exc:  # truncated or not a backup
        raise ValidationError(f"{archive.name} is not a readable backup archive") from exc
    try:
        manifest = json.loads((target / MANIFEST).read_text())
        files: dict[str, dict[str, object]] = manifest["files"]
        fmt, profile = manifest["format"], manifest["profile"]
    except (OSError, ValueError, KeyError) as exc:
        raise ValidationError("not a hive backup (missing or damaged manifest)") from exc
    if fmt != FORMAT:
        raise ValidationError(f"unsupported backup format {fmt}")
    on_disk = {p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()} - {
        MANIFEST
    }
    if on_disk != set(files):
        raise ValidationError("archive contents do not match its manifest")
    for name, info in files.items():
        if sha256_file(target / name) != info["sha256"]:
            raise ValidationError(f"{name} is corrupt (checksum mismatch)")
    files["__profile__"] = {"value": profile}
    return files


def verify_backup(archive: Path) -> Summary:
    with tempfile.TemporaryDirectory(prefix="hive-verify-") as tmp:
        files = _safe_extract(archive, Path(tmp))
        profile = str(files.pop("__profile__")["value"])
        if "db/hive.db" in files:
            conn = sqlite3.connect(Path(tmp) / "db" / "hive.db")
            try:
                result = conn.execute("PRAGMA integrity_check").fetchone()[0]
            except sqlite3.Error as exc:  # damage can make even the check itself fail
                result = str(exc)
            finally:
                conn.close()
            if result != "ok":
                raise ValidationError(f"database in the backup is damaged: {result}")
        total = sum(int(i["size"]) for i in files.values())  # type: ignore[call-overload]
        return Summary(archive, profile, len(files), total)


def restore_backup(settings: Settings, archive: Path, *, force: bool = False) -> Summary:
    """Restore into the configured deployment. Stop the server first."""
    with tempfile.TemporaryDirectory(prefix="hive-restore-") as tmp:
        stage = Path(tmp)
        files = _safe_extract(archive, stage)
        profile = str(files.pop("__profile__")["value"])
        if profile != settings.profile.value:
            raise ValidationError(
                f"backup is for the {profile} profile, this server runs {settings.profile.value}"
            )
        data = settings.data_dir
        if profile == Profile.EMBEDDED.value:
            if (data / "hive.db").exists() and not force:
                raise ValidationError(
                    f"{data / 'hive.db'} already exists; pass --force to replace it"
                )
            if not (stage / "db" / "hive.db").is_file():
                raise ValidationError("the backup holds no database; nothing was changed")
            data.mkdir(parents=True, exist_ok=True)
            # copy first, swap after: a full disk or a crash must not leave no database at all
            partial = data / "hive.db.restoring"
            shutil.copy2(stage / "db" / "hive.db", partial)
            for suffix in ("-wal", "-shm"):
                (data / f"hive.db{suffix}").unlink(missing_ok=True)
            partial.replace(data / "hive.db")
        else:
            if not settings.database_url:
                raise ValidationError("HIVE_DATABASE_URL is required for the external profile")
            if not force:
                raise ValidationError(
                    "restoring replaces the database contents; pass --force to confirm"
                )
            tool = _tool("pg_restore")
            check_client(tool, settings.database_url)
            restore = [tool, "--clean", "--if-exists", "--no-owner"]
            env = _pg_env(settings.database_url)
            restore += ["--dbname=" + env.get("PGDATABASE", "postgres")]
            _run([*restore, str(stage / "db" / "hive.pgdump")], env)
        blobs = data / "blobs"
        if (stage / "blobs").is_dir():
            shutil.copytree(stage / "blobs", blobs, dirs_exist_ok=True)
        for name, path in _state_paths(settings).items():
            if (stage / "state" / name).is_file():
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(stage / "state" / name, path)
                path.chmod(0o600)
        total = sum(int(i["size"]) for i in files.values())  # type: ignore[call-overload]
        return Summary(archive, profile, len(files), total)
