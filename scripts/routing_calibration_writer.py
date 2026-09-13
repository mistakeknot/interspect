#!/usr/bin/env python3
"""Strict serialized merge/write support for routing-calibration.json."""

from __future__ import annotations

import argparse
import errno
import json
import math
import os
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SUPPORTED_SCHEMA_VERSIONS = frozenset({1, 2, 3})
WRITER_KEYS = {
    "agent": frozenset(
        {
            "calibrated_at",
            "min_sessions",
            "min_non_bootstrap_sessions",
            "source_weights",
            "agents",
        }
    ),
    "skill": frozenset(
        {
            "skills",
            "signal_info_weights",
            "skills_calibrated_at",
            "skills_calibration",
        }
    ),
}
WRITER_SCHEMA_VERSION = {"agent": 2, "skill": 3}
DEFAULT_LOCK_TIMEOUT_SECONDS = 10.0


class CalibrationWriteError(RuntimeError):
    """The calibration artifact could not be safely committed."""


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite number {value!r}")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"non-finite number {value!r}")
    return number


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key {key!r}")
        result[key] = value
    return result


def strict_json_loads(raw: str, *, label: str) -> object:
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
        )
    except (json.JSONDecodeError, UnicodeError, ValueError) as exc:
        raise CalibrationWriteError(f"invalid {label}: {exc}") from exc
    return value


def _validate_existing(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise CalibrationWriteError("invalid existing calibration: top level must be an object")

    if "schema_version" not in value:
        raise CalibrationWriteError("invalid existing calibration: schema_version is required")
    if "schema_version" in value:
        version = value["schema_version"]
        if type(version) is not int:
            raise CalibrationWriteError(
                "invalid existing calibration: schema_version must be an integer"
            )
        if version not in SUPPORTED_SCHEMA_VERSIONS:
            raise CalibrationWriteError(
                f"unsupported existing calibration schema_version {version}"
            )

    for key in ("agents", "skills"):
        if key in value and not isinstance(value[key], dict):
            raise CalibrationWriteError(
                f"invalid existing calibration: {key} must be an object"
            )
    return value


def _validate_update(writer: str, value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise CalibrationWriteError("invalid writer update: top level must be an object")
    expected = WRITER_KEYS[writer]
    actual = frozenset(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise CalibrationWriteError(
            f"invalid {writer} update keys: missing={missing}, extra={extra}"
        )
    object_keys = (
        ("source_weights", "agents")
        if writer == "agent"
        else ("skills", "signal_info_weights", "skills_calibration")
    )
    for key in object_keys:
        if not isinstance(value[key], dict):
            raise CalibrationWriteError(f"invalid {writer} update: {key} must be an object")
    return value


def _locking_module():
    try:
        import fcntl  # type: ignore[import-not-found]
    except ImportError as exc:
        raise CalibrationWriteError(
            "advisory file locking is unsupported on this platform"
        ) from exc
    if not hasattr(fcntl, "flock"):
        raise CalibrationWriteError(
            "advisory file locking is unsupported on this platform"
        )
    return fcntl


@contextmanager
def _exclusive_lock(lock_path: Path, timeout_seconds: float) -> Iterator[None]:
    if not math.isfinite(timeout_seconds) or timeout_seconds < 0:
        raise CalibrationWriteError("lock timeout must be a finite non-negative number")

    fcntl = _locking_module()
    try:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        raise CalibrationWriteError(f"cannot open calibration lock: {exc}") from exc

    deadline = time.monotonic() + timeout_seconds
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise CalibrationWriteError(
                        f"cannot acquire calibration lock: {exc}"
                    ) from exc
                if time.monotonic() >= deadline:
                    raise CalibrationWriteError(
                        f"timed out after {timeout_seconds:g}s waiting for calibration lock"
                    )
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        yield
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _read_existing(calibration_path: Path) -> dict[str, object]:
    if not calibration_path.exists():
        return {}
    try:
        raw = calibration_path.read_bytes().decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise CalibrationWriteError(f"cannot read existing calibration: {exc}") from exc
    return _validate_existing(strict_json_loads(raw, label="existing calibration"))


def _merged_document(
    existing: dict[str, object], writer: str, update: dict[str, object]
) -> dict[str, object]:
    merged = dict(existing)
    merged.update(update)

    previous_version = merged.get("schema_version", 1)
    if type(previous_version) is not int:
        raise CalibrationWriteError(
            "invalid existing calibration: schema_version must be an integer"
        )
    version = max(previous_version, WRITER_SCHEMA_VERSION[writer])
    if "skills" in merged:
        version = max(version, 3)
    merged["schema_version"] = version
    return merged


def _encode_and_validate(document: dict[str, object]) -> bytes:
    try:
        raw = (json.dumps(document, indent=2, sort_keys=True, allow_nan=False) + "\n").encode(
            "utf-8"
        )
    except (TypeError, ValueError) as exc:
        raise CalibrationWriteError(f"cannot serialize calibration: {exc}") from exc
    parsed = strict_json_loads(raw.decode("utf-8"), label="merged calibration")
    _validate_existing(parsed)
    return raw


def _fsync_directory(directory: Path) -> None:
    directory_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _atomic_replace(calibration_path: Path, committed_bytes: bytes) -> None:
    temp_path: Path | None = None
    try:
        fd, raw_temp_path = tempfile.mkstemp(
            prefix=f".{calibration_path.name}.tmp.", dir=calibration_path.parent
        )
        temp_path = Path(raw_temp_path)
        with os.fdopen(fd, "wb") as handle:
            handle.write(committed_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        # Re-read strictly before publishing the file that was actually flushed.
        strict_json_loads(temp_path.read_text(encoding="utf-8"), label="temporary calibration")
        os.replace(temp_path, calibration_path)
        temp_path = None
        _fsync_directory(calibration_path.parent)
    except (OSError, UnicodeError) as exc:
        raise CalibrationWriteError(f"cannot atomically commit calibration: {exc}") from exc
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass


def _snapshot_name(now_ns: int) -> str:
    seconds, nanoseconds = divmod(now_ns, 1_000_000_000)
    return time.strftime("%Y-%m-%dT%H-%M-%S", time.gmtime(seconds)) + (
        f".{nanoseconds:09d}Z.json"
    )


def _archive_best_effort(calibration_path: Path, committed_bytes: bytes) -> None:
    try:
        history_dir = calibration_path.parent / "calibration-history"
        history_dir.mkdir(parents=True, exist_ok=True)
        candidate_ns = time.time_ns()
        for offset in range(1000):
            snapshot_path = history_dir / _snapshot_name(candidate_ns + offset)
            try:
                fd = os.open(snapshot_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                continue
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(committed_bytes)
                    handle.flush()
                    os.fsync(handle.fileno())
            except BaseException:
                try:
                    snapshot_path.unlink()
                except OSError:
                    pass
                raise
            break
        else:
            return

        cutoff = time.time() - 365 * 86400
        for child in history_dir.iterdir():
            try:
                if child.name.endswith(".json") and child.is_file() and child.stat().st_mtime < cutoff:
                    child.unlink()
            except OSError:
                pass
    except (OSError, ValueError):
        # History remains observational. A failure must not undo a committed
        # calibration or turn a successful writer call into a hard failure.
        pass


def merge_calibration(
    calibration_path: Path,
    *,
    writer: str,
    update: object,
    lock_timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> bytes:
    """Serialize one writer's owned fields into the shared calibration envelope."""
    if writer not in WRITER_KEYS:
        raise CalibrationWriteError(f"unknown calibration writer {writer!r}")
    validated_update = _validate_update(writer, update)

    # Check support before even creating a parent directory or lock file.
    _locking_module()
    try:
        calibration_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise CalibrationWriteError(f"cannot create calibration directory: {exc}") from exc

    lock_path = Path(f"{calibration_path}.lock")
    with _exclusive_lock(lock_path, lock_timeout_seconds):
        existing = _read_existing(calibration_path)
        committed_bytes = _encode_and_validate(
            _merged_document(existing, writer, validated_update)
        )
        _atomic_replace(calibration_path, committed_bytes)
        _archive_best_effort(calibration_path, committed_bytes)
        return committed_bytes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calibration", required=True, type=Path)
    parser.add_argument("--writer", required=True, choices=sorted(WRITER_KEYS))
    parser.add_argument(
        "--lock-timeout",
        type=float,
        default=DEFAULT_LOCK_TIMEOUT_SECONDS,
        help="bounded advisory-lock wait in seconds (default: 10)",
    )
    args = parser.parse_args()
    try:
        update = strict_json_loads(sys.stdin.read(), label=f"{args.writer} update")
        merge_calibration(
            args.calibration,
            writer=args.writer,
            update=update,
            lock_timeout_seconds=args.lock_timeout,
        )
    except CalibrationWriteError as exc:
        print(f"routing-calibration-writer: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
