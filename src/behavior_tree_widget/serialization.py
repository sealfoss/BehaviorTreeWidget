"""JSON persistence helpers for behavior tree files.

Values (leaf node fields and blackboard entries) are written with a small typed
encoding so that Python containers survive a save/load round trip unchanged:

* ``None``, ``bool``, ``int``, ``float`` and ``str`` are written natively.
* ``list`` is written as a JSON array.
* ``dict`` with only string keys is written as a JSON object.
* ``tuple``, ``set``, ``frozenset`` and dicts with non-string keys are written as
  single-key tagged objects (``{"__tuple__": [...]}``, ``{"__set__": [...]}``,
  ``{"__frozenset__": [...]}``, ``{"__dict__": [[key, value], ...]}``).

Anything else raises :class:`TypeError` from :func:`encode_value`, and so do values
nested more than :data:`MAX_NESTING` containers deep and integers too long to be
written as text (see :func:`sys.get_int_max_str_digits`): what is written can always
be read back.
"""

from __future__ import annotations

import errno
import json
import math
import os
import secrets
import stat
import time
from typing import Any

FORMAT_NAME = "behavior_tree_widget"
FORMAT_VERSION = 1

_TAG_TUPLE = "__tuple__"
_TAG_SET = "__set__"
_TAG_FROZENSET = "__frozenset__"
_TAG_DICT = "__dict__"
_TAGS = frozenset((_TAG_TUPLE, _TAG_SET, _TAG_FROZENSET, _TAG_DICT))

#: Containers inside containers, counted the same way when writing and reading.
MAX_NESTING = 256


class TreeFileError(ValueError):
    """Raised when a behavior tree file cannot be read or is malformed."""


def encode_value(value: Any) -> Any:
    """Convert ``value`` into a JSON-compatible structure (see module docs).

    Raises:
        TypeError: ``value`` contains an unsupported type or a circular reference, is
            nested too deeply or contains an integer too long to be written.
    """
    return _encode(value, set(), 0)


def _encode(value: Any, active: set[int], depth: int) -> Any:
    if isinstance(value, int) and not isinstance(value, bool):
        try:
            int.__repr__(value)  # how json writes ints; limited by sys.get_int_max_str_digits()
        except ValueError as error:
            raise TypeError("integers this large cannot be saved to JSON") from error
        return value
    if value is None or isinstance(value, (bool, float, str)):
        return value
    if not isinstance(value, (list, tuple, set, frozenset, dict)):
        raise TypeError(f"values of type {type(value).__name__!r} cannot be saved to JSON")
    if depth >= MAX_NESTING:
        raise TypeError("the value is nested too deeply to be saved to JSON")
    marker = id(value)
    if marker in active:
        raise TypeError("values containing a circular reference cannot be saved to JSON")
    active.add(marker)
    depth += 1
    try:
        if isinstance(value, list):
            return [_encode(item, active, depth) for item in value]
        if isinstance(value, tuple):
            return {_TAG_TUPLE: [_encode(item, active, depth) for item in value]}
        if isinstance(value, (set, frozenset)):
            # Sorted by their canonical JSON text: identical data always gives identical files.
            items = [_encode(item, active, depth) for item in value]
            items.sort(key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=True))
            tag = _TAG_FROZENSET if isinstance(value, frozenset) else _TAG_SET
            return {tag: items}
        if all(isinstance(key, str) for key in value) and not (_TAGS & value.keys()):
            return {key: _encode(item, active, depth) for key, item in value.items()}
        return {
            _TAG_DICT: [[_encode(key, active, depth), _encode(item, active, depth)] for key, item in value.items()]
        }
    except RecursionError as error:
        raise TypeError("the value is nested too deeply to be saved to JSON") from error
    finally:
        active.discard(marker)


def _hashable(value: Any) -> Any:
    """Make a decoded value usable as a set element / dict key (sets become frozensets)."""
    if isinstance(value, set):
        return frozenset([_hashable(item) for item in value])
    if isinstance(value, tuple):
        return tuple([_hashable(item) for item in value])
    return value


def decode_value(data: Any) -> Any:
    """Inverse of :func:`encode_value`.

    Raises:
        TreeFileError: a tagged value is malformed.
    """
    try:
        return _decode(data, 0)
    except RecursionError as error:
        raise TreeFileError("value is nested too deeply") from error


def _decode(data: Any, depth: int) -> Any:
    if not isinstance(data, (list, dict)):
        return data
    if depth >= MAX_NESTING:
        raise TreeFileError("value is nested too deeply")
    depth += 1
    if isinstance(data, list):
        return [_decode(item, depth) for item in data]
    if len(data) == 1:
        ((tag, payload),) = data.items()
        if tag in _TAGS and isinstance(payload, list):
            try:
                if tag == _TAG_TUPLE:
                    return tuple([_decode(item, depth) for item in payload])
                if tag == _TAG_SET:
                    return {_hashable(_decode(item, depth)) for item in payload}
                if tag == _TAG_FROZENSET:
                    return frozenset([_hashable(_decode(item, depth)) for item in payload])
                result = {}
                for entry in payload:  # the [key, value] pairs are not a nesting level
                    if not (isinstance(entry, list) and len(entry) == 2):
                        raise TreeFileError(f"malformed {tag} value: expected [key, value] pairs, got {entry!r}")
                    result[_hashable(_decode(entry[0], depth))] = _decode(entry[1], depth)
                return result
            except TypeError as error:
                raise TreeFileError(f"malformed {tag} value: {error}") from error
    return {key: _decode(item, depth) for key, item in data.items()}


def is_encodable(value: Any) -> bool:
    """Return True if ``value`` can be written by :func:`encode_value`."""
    try:
        encode_value(value)
    except TypeError:
        return False
    return True


def is_finite_number(value: Any) -> bool:
    """True for ints/floats (not bools) with a finite value."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except OverflowError:
        return False


def _new_file_mode() -> int:
    umask = os.umask(0)
    os.umask(umask)
    return 0o666 & ~umask


_REPLACE_ATTEMPTS = 5
_WINDOWS_KEPT_ATTRIBUTES = 0x2 | 0x4  # FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM


def _windows_attributes(path: str) -> int:
    if os.name != "nt":
        return 0
    try:
        return int(getattr(os.stat(path), "st_file_attributes", 0)) & _WINDOWS_KEPT_ATTRIBUTES
    except OSError:
        return 0


def _set_windows_attributes(path: str, attributes: int) -> None:
    if os.name != "nt" or not attributes:
        return
    try:
        import ctypes

        current = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        if current != -1 and current != 0xFFFFFFFF:
            ctypes.windll.kernel32.SetFileAttributesW(str(path), (current & ~0x80) | attributes)
    except Exception:  # noqa: BLE001 - cosmetic; never fail a save because of attributes
        pass


def _create_temp_file(path: str, directory: str) -> tuple[int, str]:
    """Create a new temporary file next to ``path``; return ``(fd, temp_path)``.

    ``tempfile.mkstemp`` is not used: on Windows it retries for a very long time when
    the folder does not allow creating files (it mistakes the PermissionError for a
    name clash), which would freeze the application.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0)
    for _ in range(100):
        temp_path = os.path.join(directory, f".bt_{secrets.token_hex(6)}.json.tmp")
        try:
            return os.open(temp_path, flags, 0o600), temp_path
        except FileExistsError:
            continue
        except PermissionError as error:
            raise PermissionError(
                error.errno, f"could not write '{path}': no permission to create files in '{directory}'"
            ) from error
    raise FileExistsError(errno.EEXIST, f"could not create a temporary file in '{directory}'")


def write_json_atomic(path: str, data: dict) -> None:
    """Write ``data`` as pretty JSON to ``path`` without leaving a partial file on failure.

    Non-ASCII text is written as UTF-8; if the data holds strings that cannot be
    encoded (e.g. lone surrogates) the file is written with ASCII escapes instead.
    The permissions (and on Windows the hidden/system attributes) of an existing file
    are kept, and symbolic links are followed. When another program briefly keeps the
    file open so that it cannot be replaced (common on Windows), the replacement is
    retried; the existing file is never modified unless the new content is complete.
    """
    try:
        text = json.dumps(data, indent=2, ensure_ascii=False)
        text.encode("utf-8")
    except UnicodeEncodeError:
        text = json.dumps(data, indent=2, ensure_ascii=True)
    text += "\n"
    path = os.path.realpath(path)
    directory = os.path.dirname(path)
    if os.path.isdir(path):
        raise IsADirectoryError(errno.EISDIR, f"could not write '{path}': it is a folder")
    if not os.path.isdir(directory):
        raise FileNotFoundError(errno.ENOENT, f"could not write '{path}': the folder '{directory}' does not exist")
    try:
        existing = os.stat(path)
    except FileNotFoundError:
        existing = None
    mode = stat.S_IMODE(existing.st_mode) if existing is not None else _new_file_mode()
    attributes = _windows_attributes(path) if existing is not None else 0
    handle, temp_path = _create_temp_file(path, directory)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        try:
            os.chmod(temp_path, mode)
        except OSError:
            pass
        for attempt in range(_REPLACE_ATTEMPTS):
            try:
                os.replace(temp_path, path)
                break
            except PermissionError as error:
                read_only = existing is not None and not (existing.st_mode & stat.S_IWRITE)
                if read_only:
                    raise PermissionError(error.errno, f"'{path}' is read-only") from error
                if attempt < _REPLACE_ATTEMPTS - 1:
                    time.sleep(0.05 * (attempt + 1))
                    continue
                raise PermissionError(
                    error.errno,
                    f"could not write '{path}': {error.strerror} (the file may be open in another "
                    "program, or you may not have permission to replace it)",
                ) from error
        _set_windows_attributes(path, attributes)
    except BaseException:
        if os.path.exists(temp_path):
            try:
                os.chmod(temp_path, stat.S_IREAD | stat.S_IWRITE)  # a read-only copy cannot be removed on Windows
            except OSError:
                pass
            try:
                os.remove(temp_path)
            except OSError:
                pass
        raise


def read_tree_file(path: str) -> dict:
    """Read and minimally validate a behavior tree file, returning the parsed dict."""
    try:
        with open(path, "r", encoding="utf-8-sig") as stream:
            data = json.load(stream)
    except OSError as error:
        raise TreeFileError(f"Could not read '{path}': {error.strerror or error}") from error
    except (ValueError, RecursionError, MemoryError) as error:  # JSONDecodeError / UnicodeDecodeError are ValueErrors
        raise TreeFileError(f"'{os.path.basename(path)}' is not a valid JSON file: {error}") from error
    if not isinstance(data, dict) or data.get("format") != FORMAT_NAME:
        raise TreeFileError(f"'{os.path.basename(path)}' is not a behavior tree file.")
    version = data.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise TreeFileError(f"'{os.path.basename(path)}' has an invalid format version: {version!r}")
    if version > FORMAT_VERSION:
        raise TreeFileError(
            f"'{os.path.basename(path)}' was written by a newer version of behavior_tree_widget "
            f"(format {version}; this version reads up to {FORMAT_VERSION})."
        )
    for key, kind in (("nodes", list), ("connections", list), ("blackboard", list), ("config", dict)):
        if key in data and not isinstance(data[key], kind):
            raise TreeFileError(f"'{os.path.basename(path)}' is malformed: '{key}' must be a {kind.__name__}.")
    return data
