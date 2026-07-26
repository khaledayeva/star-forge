"""Descriptor-relative filesystem operations confined beneath one trusted root."""
from __future__ import annotations
import errno
import hashlib
import os
import secrets
import stat
from pathlib import Path
from typing import Mapping, Sequence
class SafeIOError(OSError):
    """A confined filesystem operation could not be completed safely."""
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_DIR_FLAGS = os.O_RDONLY | _CLOEXEC | _DIRECTORY | _NOFOLLOW
_FILE_FLAGS = _CLOEXEC | _NOFOLLOW
def _require_support() -> None:
    supported = (_NOFOLLOW, _DIRECTORY, os.open in os.supports_dir_fd,
                 os.mkdir in os.supports_dir_fd, os.stat in os.supports_dir_fd,
                 os.unlink in os.supports_dir_fd, os.listdir in os.supports_fd)
    if not all(supported):
        raise SafeIOError("descriptor-relative no-follow filesystem APIs are unavailable")
def infer_root(path: str | Path) -> Path:
    """Infer a compatibility boundary, preferring known project-owned roots."""
    absolute = Path(os.path.abspath(os.fspath(path)))
    for marker in (".starforge", ".codex"):
        if marker in absolute.parts:
            return Path(*absolute.parts[:absolute.parts.index(marker)])
    candidate = absolute.parent
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate
def _relative_parts(root: str | Path, path: str | Path) -> tuple[Path, tuple[str, ...]]:
    lexical = Path(os.path.abspath(os.fspath(root)))
    physical = Path(os.path.realpath(lexical))
    candidate = Path(path)
    absolute = Path(os.path.abspath(os.fspath(
        candidate if candidate.is_absolute() else lexical / candidate)))
    relative = next((absolute.relative_to(boundary)
                     for boundary in (lexical, physical)
                     if absolute == boundary or absolute.is_relative_to(boundary)), None)
    if relative is None:
        raise SafeIOError(f"path escapes confined root: {path}")
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise SafeIOError(f"path is not a normalized confined child: {path}")
    return physical, relative.parts
def _open_root(root: Path) -> int:
    _require_support()
    try:
        descriptor = os.open(root, _DIR_FLAGS)
    except OSError as exc:
        raise SafeIOError(f"cannot open trusted filesystem root {root}: {exc}") from exc
    if stat.S_ISDIR(os.fstat(descriptor).st_mode):
        return descriptor
    os.close(descriptor)
    raise SafeIOError(f"trusted filesystem root is not a directory: {root}")
def _open_dir(parent: int, name: str, create: bool) -> int:
    try:
        return os.open(name, _DIR_FLAGS, dir_fd=parent)
    except FileNotFoundError:
        if not create:
            raise
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
        except FileExistsError:
            pass
        return os.open(name, _DIR_FLAGS, dir_fd=parent)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise SafeIOError(f"refusing symlink or non-directory component: {name}") from exc
        raise
def _directory_fd(root: str | Path, parts: Sequence[str], create: bool) -> int:
    current = _open_root(Path(os.path.realpath(os.path.abspath(os.fspath(root)))))
    try:
        for part in parts:
            child = _open_dir(current, part, create)
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise
def _parent_fd(root: str | Path, path: str | Path, create: bool) -> tuple[int, str]:
    physical, parts = _relative_parts(root, path)
    return _directory_fd(physical, parts[:-1], create), parts[-1]
def _entry(parent: int, name: str) -> os.stat_result | None:
    try:
        result = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(result.st_mode):
        raise SafeIOError(f"refusing symlinked confined file: {name}")
    return result
def _write_all(descriptor: int, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise SafeIOError("confined write made no progress")
        remaining = remaining[written:]
def make_directory(root: str | Path, path: str | Path) -> None:
    physical, parts = _relative_parts(root, path)
    descriptor = _directory_fd(physical, parts, True)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
def atomic_write_bytes(root: str | Path, path: str | Path, data: bytes) -> None:
    parent, name = _parent_fd(root, path, True)
    temporary = f".{name}.{secrets.token_hex(12)}.tmp"
    descriptor = -1
    try:
        existing = _entry(parent, name)
        if existing is not None and not stat.S_ISREG(existing.st_mode):
            raise SafeIOError(f"confined destination is not a regular file: {name}")
        mode = stat.S_IMODE(existing.st_mode) if existing else 0o600
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _FILE_FLAGS,
                             mode, dir_fd=parent)
        _write_all(descriptor, data)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        try:
            os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
        except TypeError as exc:
            raise SafeIOError("descriptor-relative atomic replacement is unavailable") from exc
        os.fsync(parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass
        os.close(parent)
def atomic_write_text(root: str | Path, path: str | Path, text: str) -> None:
    atomic_write_bytes(root, path, text.encode("utf-8"))
def atomic_create_bundle(root: str | Path, path: str | Path, *,
                         directories: Sequence[str], files: Mapping[str, str]) -> None:
    parent, name = _parent_fd(root, path, True)
    temporary = f".{name}.{secrets.token_hex(12)}.tmp"
    bundle = -1
    created_files: list[str] = []
    created_dirs: list[str] = []
    committed = False
    try:
        if _entry(parent, name) is not None:
            raise SafeIOError(f"confined bundle already exists: {name}")
        os.mkdir(temporary, 0o700, dir_fd=parent)
        bundle = _open_dir(parent, temporary, False)
        for directory in directories:
            if Path(directory).name != directory:
                raise SafeIOError(f"unsafe bundle directory: {directory}")
            os.mkdir(directory, 0o700, dir_fd=bundle)
            created_dirs.append(directory)
        for filename, text in files.items():
            if Path(filename).name != filename:
                raise SafeIOError(f"unsafe bundle file: {filename}")
            descriptor = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _FILE_FLAGS,
                                 0o600, dir_fd=bundle)
            created_files.append(filename)
            try:
                _write_all(descriptor, text.encode("utf-8"))
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        os.fsync(bundle)
        os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
        committed = True
        os.fsync(parent)
    finally:
        if bundle >= 0 and not committed:
            for filename in reversed(created_files):
                try:
                    os.unlink(filename, dir_fd=bundle)
                except OSError:
                    pass
            for directory in reversed(created_dirs):
                try:
                    os.rmdir(directory, dir_fd=bundle)
                except OSError:
                    pass
        if bundle >= 0:
            os.close(bundle)
        try:
            os.rmdir(temporary, dir_fd=parent)
        except OSError:
            pass
        os.close(parent)
def append_text(root: str | Path, path: str | Path, text: str) -> None:
    parent, name = _parent_fd(root, path, True)
    descriptor = -1
    try:
        existing = _entry(parent, name)
        if existing is not None and not stat.S_ISREG(existing.st_mode):
            raise SafeIOError(f"confined append destination is not regular: {name}")
        descriptor = os.open(name, os.O_WRONLY | os.O_APPEND | os.O_CREAT | _FILE_FLAGS,
                             0o600, dir_fd=parent)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise SafeIOError(f"confined append destination is not regular: {name}")
        _write_all(descriptor, text.encode("utf-8"))
        os.fsync(descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent)
def _open_regular(root: str | Path, path: str | Path) -> int:
    parent, name = _parent_fd(root, path, False)
    try:
        try:
            descriptor = os.open(name, os.O_RDONLY | _FILE_FLAGS, dir_fd=parent)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise SafeIOError(f"refusing symlinked confined file: {name}") from exc
            raise
    finally:
        os.close(parent)
    if stat.S_ISREG(os.fstat(descriptor).st_mode):
        return descriptor
    os.close(descriptor)
    raise SafeIOError(f"confined source is not a regular file: {name}")
def _read(root: str | Path, path: str | Path, *, limit: int | None,
          capture_limit: int | None) -> tuple[bytes, str, int]:
    descriptor = _open_regular(root, path)
    digest = hashlib.sha256()
    chunks: list[bytes] = []
    size = 0
    captured = 0
    try:
        while limit is None or size < limit:
            amount = min(1024 * 1024, limit - size) if limit is not None else 1024 * 1024
            chunk = os.read(descriptor, amount)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
            wanted = len(chunk) if capture_limit is None else max(0, capture_limit - captured)
            if wanted:
                piece = chunk[:wanted]
                chunks.append(piece)
                captured += len(piece)
    finally:
        os.close(descriptor)
    return b"".join(chunks), digest.hexdigest(), size
def read_bytes(root: str | Path, path: str | Path, *, limit: int | None = None) -> bytes:
    if limit is not None and limit < 0:
        raise ValueError("limit must be non-negative")
    return _read(root, path, limit=limit, capture_limit=None)[0]
def read_text(root: str | Path, path: str | Path) -> str:
    return read_bytes(root, path).decode("utf-8")
def digest_size(root: str | Path, path: str | Path) -> tuple[str, int]:
    _content, digest, size = _read(root, path, limit=None, capture_limit=0)
    return digest, size
def snapshot(root: str | Path, path: str | Path, *,
             prefix_limit: int = 0) -> tuple[bytes, str, int]:
    if prefix_limit < 0:
        raise ValueError("prefix_limit must be non-negative")
    return _read(root, path, limit=None, capture_limit=prefix_limit)
def directory_exists(root: str | Path, path: str | Path) -> bool:
    physical, parts = _relative_parts(root, path)
    try:
        descriptor = _directory_fd(physical, parts, False)
    except FileNotFoundError:
        return False
    os.close(descriptor)
    return True
def _scan_tree(descriptor: int, display: str) -> None:
    for name in os.listdir(descriptor):
        result = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        child_display = f"{display}/{name}"
        if stat.S_ISLNK(result.st_mode):
            raise SafeIOError(f"refusing unsafe confined symlink: {child_display}")
        if stat.S_ISDIR(result.st_mode):
            child = _open_dir(descriptor, name, False)
            try:
                _scan_tree(child, child_display)
            finally:
                os.close(child)
def assert_tree_no_symlinks(root: str | Path, path: str | Path) -> None:
    physical, parts = _relative_parts(root, path)
    if not physical.exists():
        return
    try:
        descriptor = _directory_fd(physical, parts, False)
    except FileNotFoundError:
        return
    try:
        _scan_tree(descriptor, os.fspath(Path(*parts)))
    finally:
        os.close(descriptor)
