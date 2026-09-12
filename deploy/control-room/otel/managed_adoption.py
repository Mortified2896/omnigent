"""Reserve backup/scratch space for cooperating source adopters, not all writers."""

from __future__ import annotations

import contextlib
import hashlib
import os
import stat
from pathlib import Path

from managed_budget import load_policy
from managed_storage import StoragePaused, exclusive, measure

_MAX_IMAGE_BYTES = 8 * 1024**2
_MAX_IMAGES_BYTES = 64 * 1024**2
_CONTROL_RESERVE_BYTES = 1024**2
_MAX_CONTROL_FILE_BYTES = 64 * 1024


def _signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _image(path, optional=False):
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path:
        raise StoragePaused("unsafe_adoption_source")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        if optional:
            return None
        raise StoragePaused("missing_adoption_source") from None
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise StoragePaused("unsafe_adoption_source")
        if before.st_size > _MAX_IMAGE_BYTES:
            raise StoragePaused("oversized_adoption_source")
        data = stream.read(_MAX_IMAGE_BYTES + 1)
        after = os.fstat(stream.fileno())
    if (
        len(data) != before.st_size
        or _signature(before) != _signature(after)
        or _signature(after) != _signature(path.lstat())
    ):
        raise StoragePaused("adoption_source_changed")
    return data, before


class ReservedCopies:
    """Copy immutable admitted bytes; live source growth cannot enlarge a copy."""

    def __init__(self, images, allowed_roots, required_bytes):
        self.images = images
        self.allowed_roots = allowed_roots
        self.required_bytes = required_bytes
        self.written_bytes = 0

    def assert_unchanged(self):
        for path, image in self.images.items():
            try:
                info = path.lstat()
            except FileNotFoundError:
                if image is None:
                    continue
                raise StoragePaused("adoption_source_changed") from None
            if image is None or _signature(info) != _signature(image[1]):
                raise StoragePaused("adoption_source_changed")

    def assert_hashes(self, expected):
        for path, value in expected.items():
            image = self.images.get(path)
            if image is None or hashlib.sha256(image[0]).hexdigest() != value:
                raise StoragePaused("reviewed_source_changed")

    def digest(self, path):
        return hashlib.sha256(self.images[Path(path)][0]).hexdigest()

    def present(self, path):
        return self.images[Path(path)] is not None

    def different(self, source, target):
        old = self.images[Path(target)]
        return old is None or self.images[Path(source)][0] != old[0]

    def write(self, path, data, mode=0o600):
        path = Path(path)
        if path.resolve() != path or not any(root in path.parents for root in self.allowed_roots):
            raise StoragePaused("unsafe_adoption_destination")
        # Charge actual writes again, even if a caller mistakenly copies twice.
        charge = ((len(data) + 65535) // 65536) * 65536 + 65536
        if self.written_bytes + charge > self.required_bytes:
            raise StoragePaused("adoption_reservation_exhausted")
        self.written_bytes += charge
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), stat.S_IMODE(mode))

    def copy2(self, source, target):
        data, info = self.images[Path(source)]
        self.write(target, data, info.st_mode)
        os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns))

    def text(self, target, text, replace=False):
        data = text.encode()
        if len(data) > _MAX_CONTROL_FILE_BYTES:
            raise StoragePaused("oversized_adoption_metadata")
        path = Path(target)
        temporary = path.with_name(path.name + ".pending") if replace else path
        self.write(temporary, data)
        if replace:
            temporary.replace(path)


@contextlib.contextmanager
def reserved_copies(source_paths, existing_paths, state, destination_roots):
    """Use one lock for both adopters and refuse before any rollback copy.

    All state bytes are conservatively charged to the backup allocation. Other
    backup producers must join this lock/allocation before claiming global bounds.
    """
    state = Path(state)
    if not state.is_dir() or state.resolve() != state:
        raise StoragePaused("unsafe_adoption_state")
    roots = tuple(Path(root) for root in destination_roots)
    if any(not root.is_dir() or root.resolve() != root for root in roots):
        raise StoragePaused("unsafe_adoption_destination")
    with exclusive(state / ".provenance-adoption.lock"):
        images = {}
        total = 0
        for paths, optional in ((source_paths, False), (existing_paths, True)):
            for path in paths:
                path = Path(path)
                if path in images:
                    continue
                image = _image(path, optional)
                images[path] = image
                total += len(image[0]) if image else 0
                if total > _MAX_IMAGES_BYTES:
                    raise StoragePaused("oversized_adoption_batch")
        required = _CONTROL_RESERVE_BYTES + sum(
            2 * (((len(image[0]) + 65535) // 65536) * 65536 + 65536)
            for image in images.values()
            if image is not None
        )
        inventory = measure([{"path": str(state), "component": "telemetry_backups"}])
        if not inventory.get("complete"):
            raise StoragePaused("adoption_inventory_unknown")
        used = inventory["components"]["telemetry_backups"]["bytes"]
        limit = load_policy()["allocations"]["telemetry_backups"]
        if used + required > limit:
            raise StoragePaused("adoption_reserve_unavailable")
        copies = ReservedCopies(images, roots, required)
        copies.assert_unchanged()
        yield copies
