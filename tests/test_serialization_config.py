"""Tests for ``behavior_tree_widget.serialization`` and ``behavior_tree_widget.config``.

Covers:

* the typed value codec (:func:`encode_value` / :func:`decode_value` / :func:`is_encodable`),
* atomic JSON writing (:func:`write_json_atomic`),
* tree file validation (:func:`read_tree_file` / :class:`TreeFileError`),
* :class:`TreeConfig` (defaults, ``to_dict`` / ``from_dict`` / ``copy``),
* :class:`ConfigureDialog` (pre-filling and ``config()``) and its use by the widget.

The codec contract (serialization module docstring): Python containers survive a
save/load round trip unchanged; ``None``/``bool``/``int``/``float``/``str`` are written
natively, lists as arrays, str-key dicts as objects, and tuples, sets/frozensets and
dicts with non-str keys (or keys colliding with a tag) as single-key tagged objects.
Anything else raises :class:`TypeError`.
"""

from __future__ import annotations

import copy
import decimal
import json
import math
import os
import stat
import sys

import pytest

import behavior_tree_widget
from behavior_tree_widget import serialization
from behavior_tree_widget.config import ConfigureDialog, TreeConfig
from behavior_tree_widget.serialization import (
    FORMAT_NAME,
    FORMAT_VERSION,
    TreeFileError,
    decode_value,
    encode_value,
    is_encodable,
    read_tree_file,
    write_json_atomic,
)


# ============================================================================ helpers
def json_round_trip(value):
    """encode -> JSON text -> parse -> decode, the path a value takes through a tree file."""
    return decode_value(json.loads(json.dumps(encode_value(value))))


def typed(value):
    """Type-aware canonical form, so that e.g. ``(1,) != [1]``, ``True != 1`` and ``set != frozenset``.

    NaN is mapped to a marker so it compares equal to itself. Dict order is kept.
    """
    if isinstance(value, float) and math.isnan(value):
        return ("float", "nan")
    if isinstance(value, (list, tuple)):
        return (type(value).__name__, [typed(item) for item in value])
    if isinstance(value, (set, frozenset)):
        return (type(value).__name__, sorted((typed(item) for item in value), key=repr))
    if isinstance(value, dict):
        return (type(value).__name__, [(typed(key), typed(item)) for key, item in value.items()])
    return (type(value).__name__, value)


def temp_files(directory) -> list[str]:
    """Leftover temporary files of write_json_atomic in ``directory``."""
    return [name for name in os.listdir(directory) if name.endswith(".tmp") or name.startswith(".bt_")]


def write_text(path, text: str) -> str:
    with open(path, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(text)
    return str(path)


def write_tree(path, **overrides) -> str:
    """Write a minimal valid tree file (plus/minus ``overrides``; value None deletes a key)."""
    data = {
        "format": FORMAT_NAME,
        "version": FORMAT_VERSION,
        "config": TreeConfig().to_dict(),
        "nodes": [],
        "connections": [],
        "blackboard": [],
    }
    for key, value in overrides.items():
        if value is _DELETE:
            data.pop(key, None)
        else:
            data[key] = value
    return write_text(path, json.dumps(data))


_DELETE = object()


class Unsupported:
    """A user class the codec cannot save."""


# ============================================================================ encode/decode round trips
ROUND_TRIP_VALUES = [
    pytest.param(None, id="none"),
    pytest.param(True, id="true"),
    pytest.param(False, id="false"),
    pytest.param(0, id="zero"),
    pytest.param(-17, id="negative-int"),
    pytest.param(2**70, id="big-int"),
    pytest.param(1.5, id="float"),
    pytest.param(-0.25, id="negative-float"),
    pytest.param(1e300, id="huge-float"),
    pytest.param("", id="empty-str"),
    pytest.param("héllo wörld ✓ \n\t\"quoted\"", id="unicode-str"),
    pytest.param([], id="empty-list"),
    pytest.param([1, "a", None, True, 2.5], id="flat-list"),
    pytest.param([[1, [2, [3, []]]], ["x"]], id="nested-lists"),
    pytest.param({}, id="empty-dict"),
    pytest.param({"a": 1, "b": [1, 2], "c": {"d": None}}, id="str-key-dict"),
    pytest.param((), id="empty-tuple"),
    pytest.param((1,), id="one-tuple"),
    pytest.param((1, (2, (3, ()))), id="nested-tuples"),
    pytest.param((1, [2, 3], {"a": (4,)}), id="tuple-with-containers"),
    pytest.param([(1, 2), (3, 4)], id="list-of-tuples"),
    pytest.param(set(), id="empty-set"),
    pytest.param({3, 1, 2}, id="int-set"),
    pytest.param({"b", "a", "c"}, id="str-set"),
    pytest.param({1, "a", None, (1, 2), 2.5, True}, id="mixed-set"),
    pytest.param({(1, (2, 3)), ("x",)}, id="set-of-nested-tuples"),
    pytest.param([{1, 2}, ({3},)], id="sets-inside-containers"),
    pytest.param({1: "a", 2: "b"}, id="int-key-dict"),
    pytest.param({None: 1, 1.5: 2, False: 3}, id="none-float-bool-keys"),
    pytest.param({(1, 2): "t", (3, (4,)): "u"}, id="tuple-key-dict"),
    pytest.param({"a": 1, 2: "b"}, id="mixed-key-dict"),
    pytest.param({"outer": {1: {2: (3, {4})}}}, id="non-str-key-dict-nested-in-str-dict"),
    pytest.param({1: {"inner": [1, {2: 3}]}}, id="str-dict-nested-in-non-str-dict"),
    pytest.param({"__set__": [1, 2]}, id="tag-collision-set"),
    pytest.param({"__tuple__": [1]}, id="tag-collision-tuple"),
    pytest.param({"__dict__": [["a", 1]]}, id="tag-collision-dict"),
    pytest.param({"__set__": 5}, id="tag-collision-non-list-payload"),
    pytest.param({"__tuple__": {"__set__": [1]}}, id="tag-collision-nested"),
    pytest.param({"__set__": 1, "other": 2}, id="tag-key-among-others"),
    pytest.param([{"__dict__": []}, ({"__tuple__": ()},)], id="tag-collisions-in-containers"),
    pytest.param(
        {"list": [1, (2, {3: {4, 5}})], "set": {(1, 2)}, "tuple": ({"x": [None]},), 7: set()},
        id="everything",
    ),
]


@pytest.mark.parametrize("value", ROUND_TRIP_VALUES)
def test_round_trip_preserves_value_and_types(value):
    original = copy.deepcopy(value)
    decoded = decode_value(encode_value(value))
    assert typed(decoded) == typed(original)
    # encoding must not modify the input value
    assert typed(value) == typed(original)


@pytest.mark.parametrize("value", ROUND_TRIP_VALUES)
def test_round_trip_through_json_text(value):
    """The encoded form is JSON serialisable and survives JSON text unchanged."""
    text = json.dumps(encode_value(value), allow_nan=False)
    decoded = decode_value(json.loads(text))
    assert typed(decoded) == typed(value)


@pytest.mark.parametrize("value", [None, True, False, 0, 5, -3, 2.5, "text", ""])
def test_primitives_are_written_natively(value):
    encoded = encode_value(value)
    assert type(encoded) is type(value)
    assert encoded == value


def test_documented_encoded_shapes():
    assert encode_value([1, (2,)]) == [1, {"__tuple__": [2]}]
    assert encode_value((1, 2)) == {"__tuple__": [1, 2]}
    assert encode_value({3, 1, 2}) == {"__set__": [1, 2, 3]}
    assert encode_value(frozenset({"a"})) == {"__frozenset__": ["a"]}
    assert encode_value({1: "a"}) == {"__dict__": [[1, "a"]]}
    assert encode_value({(1, 2): {3}}) == {"__dict__": [[{"__tuple__": [1, 2]}, {"__set__": [3]}]]}
    assert encode_value({"a": (1,)}) == {"a": {"__tuple__": [1]}}


def test_str_key_dict_is_written_as_plain_object():
    value = {"b": 1, "a": [2]}
    encoded = encode_value(value)
    assert encoded == {"b": 1, "a": [2]}
    assert list(encoded) == ["b", "a"]


@pytest.mark.parametrize("tag", ["__tuple__", "__set__", "__dict__"])
def test_str_key_dict_whose_single_key_is_a_tag_is_escaped(tag):
    value = {tag: [1, 2]}
    encoded = encode_value(value)
    assert encoded == {"__dict__": [[tag, [1, 2]]]}
    decoded = json_round_trip(value)
    assert typed(decoded) == typed(value)


def test_set_encoding_is_deterministic():
    first = encode_value({"b", "a", "c", 10, 2})
    second = encode_value({2, 10, "c", "a", "b"})
    assert first == second
    assert encode_value(set(range(30))) == encode_value(set(reversed(range(30))))


def test_dict_order_is_preserved():
    value = {3: "c", 1: "a", 2: "b"}
    assert list(json_round_trip(value)) == [3, 1, 2]
    value = {"z": 1, "a": 2, "m": 3}
    assert list(json_round_trip(value)) == ["z", "a", "m"]


def test_frozenset_round_trips_to_equal_set():
    decoded = json_round_trip(frozenset({1, 2, (3, 4)}))
    assert isinstance(decoded, (set, frozenset))
    assert decoded == frozenset({1, 2, (3, 4)})


@pytest.mark.parametrize(
    "value",
    [
        pytest.param({frozenset({1}), frozenset({2, 3})}, id="set-of-frozensets"),
        pytest.param({frozenset({1}): "a", frozenset(): "b"}, id="frozenset-dict-keys"),
        pytest.param({(1, frozenset({2})): "x"}, id="tuple-key-holding-frozenset"),
    ],
)
def test_frozensets_in_hashable_positions_round_trip(value):
    """encode_value accepts these values (is_encodable is True), so they must also load back."""
    assert is_encodable(value)
    decoded = json_round_trip(value)
    assert decoded == value


@pytest.mark.parametrize(
    "value",
    [float("nan"), float("inf"), float("-inf")],
    ids=["nan", "inf", "-inf"],
)
def test_non_finite_floats_round_trip(value):
    encoded = encode_value(value)
    assert isinstance(encoded, float)
    for decoded in (decode_value(encoded), json_round_trip(value)):
        assert isinstance(decoded, float)
        if math.isnan(value):
            assert math.isnan(decoded)
        else:
            assert decoded == value


def test_non_finite_floats_inside_containers_round_trip():
    value = {"a": [float("nan"), float("inf")], 1: (float("-inf"),), "s": {float("inf")}}
    assert typed(json_round_trip(value)) == typed(value)


def test_decode_does_not_modify_input():
    data = {"a": {"__tuple__": [1, {"__set__": [2]}]}, "b": [{"__dict__": [[1, 2]]}]}
    snapshot = copy.deepcopy(data)
    decode_value(data)
    assert data == snapshot


@pytest.mark.parametrize(
    "data",
    [
        pytest.param({"a": [1]}, id="single-non-tag-key"),
        pytest.param({"x": 1, "y": {"z": [None]}}, id="nested-plain"),
        pytest.param([1, "two", None, 3.0, False], id="list"),
    ],
)
def test_decode_plain_json_is_identity(data):
    assert typed(decode_value(data)) == typed(data)


# ============================================================================ unsupported / malformed
UNSUPPORTED_VALUES = [
    pytest.param(object(), id="object"),
    pytest.param(Unsupported(), id="custom-class"),
    pytest.param(b"bytes", id="bytes"),
    pytest.param(bytearray(b"x"), id="bytearray"),
    pytest.param(1 + 2j, id="complex"),
    pytest.param(decimal.Decimal("1.5"), id="decimal"),
    pytest.param(range(3), id="range"),
    pytest.param(len, id="builtin-function"),
    pytest.param(lambda: None, id="lambda"),
    pytest.param(Unsupported, id="class"),
    pytest.param([1, object()], id="nested-in-list"),
    pytest.param({"a": b"x"}, id="nested-in-dict-value"),
    pytest.param({b"key": 1}, id="bytes-dict-key"),
    pytest.param((1, (2, 3j)), id="nested-in-tuple"),
    pytest.param({1, b"x"}, id="nested-in-set"),
    pytest.param({1: [Unsupported()]}, id="nested-in-non-str-key-dict"),
]


@pytest.mark.parametrize("value", UNSUPPORTED_VALUES)
def test_unsupported_types_raise_type_error(value):
    with pytest.raises(TypeError):
        encode_value(value)


def test_type_error_names_the_offending_type():
    with pytest.raises(TypeError, match="bytes"):
        encode_value({"a": [b"x"]})
    with pytest.raises(TypeError, match="Unsupported"):
        encode_value(Unsupported())


@pytest.mark.parametrize("value", ROUND_TRIP_VALUES[:20])
def test_is_encodable_true_for_supported_values(value):
    assert is_encodable(value) is True


@pytest.mark.parametrize("value", UNSUPPORTED_VALUES)
def test_is_encodable_false_for_unsupported_values(value):
    assert is_encodable(value) is False


def test_is_encodable_false_for_self_referencing_container():
    """A cyclic list can never be written; the predicate must answer False instead of raising."""
    cyclic: list = [1]
    cyclic.append(cyclic)
    assert is_encodable(cyclic) is False


MALFORMED_PAYLOADS = [
    pytest.param({"__dict__": [[1]]}, id="dict-pair-too-short"),
    pytest.param({"__dict__": [[1, 2, 3]]}, id="dict-pair-too-long"),
    pytest.param({"__dict__": [[]]}, id="dict-pair-empty"),
    pytest.param({"__dict__": [1]}, id="dict-entry-int"),
    pytest.param({"__dict__": [None]}, id="dict-entry-null"),
    pytest.param({"__dict__": [[[1], "x"]]}, id="dict-list-key"),
    pytest.param({"__dict__": [[{"a": 1}, "x"]]}, id="dict-object-key"),
    pytest.param({"__set__": [[1]]}, id="set-list-element"),
    pytest.param({"__set__": [{"a": 1}]}, id="set-object-element"),
    pytest.param({"__set__": [{"__tuple__": [[1]]}]}, id="set-unhashable-tuple-element"),
    pytest.param([0, {"__set__": [[1]]}], id="nested-in-list"),
    pytest.param({"k": {"__dict__": [[1]]}}, id="nested-in-object"),
    pytest.param({"__tuple__": [{"__set__": [[1]]}]}, id="nested-in-tuple"),
    pytest.param({"__dict__": ["ab"]}, id="dict-entry-two-char-string"),
    pytest.param({"__dict__": [{"a": 1, "b": 2}]}, id="dict-entry-object"),
]


@pytest.mark.parametrize("data", MALFORMED_PAYLOADS)
def test_malformed_tagged_payload_raises_tree_file_error(data):
    with pytest.raises(TreeFileError):
        decode_value(data)


def test_malformed_payload_error_names_the_tag():
    with pytest.raises(TreeFileError, match="__dict__"):
        decode_value({"__dict__": [[1]]})
    with pytest.raises(TreeFileError, match="__set__"):
        decode_value({"__set__": [[1]]})


def test_tree_file_error_is_a_public_value_error():
    assert issubclass(TreeFileError, ValueError)
    assert behavior_tree_widget.TreeFileError is TreeFileError
    assert behavior_tree_widget.TreeConfig is TreeConfig


# ============================================================================ write_json_atomic
SAMPLE_DATA = {
    "format": FORMAT_NAME,
    "version": FORMAT_VERSION,
    "config": {"tick_interval_ms": 100, "repeat": False},
    "nodes": [{"id": "a", "title": "Wörld ✓", "x": 1.5}],
    "connections": [],
    "blackboard": [{"name": "n", "type": "Integer", "value": 3}],
}


def test_write_json_atomic_writes_pretty_utf8_json(tmp_path):
    path = tmp_path / "tree.json"
    write_json_atomic(str(path), SAMPLE_DATA)
    raw = path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), "no UTF-8 BOM expected"
    text = raw.decode("utf-8")
    assert json.loads(text) == SAMPLE_DATA
    assert text == json.dumps(SAMPLE_DATA, indent=2, ensure_ascii=False) + "\n"
    assert "Wörld ✓" in text  # non-ASCII kept readable, not \u-escaped
    assert '\n  "format"' in text  # indented
    assert b"\r\n" not in raw


def test_write_json_atomic_creates_new_file(tmp_path):
    path = tmp_path / "new.json"
    assert not path.exists()
    write_json_atomic(str(path), {"a": 1})
    assert json.loads(path.read_text(encoding="utf-8")) == {"a": 1}
    assert os.listdir(tmp_path) == ["new.json"]


@pytest.mark.parametrize("previous", ["x" * 5000, "", "short"], ids=["longer", "empty", "shorter"])
def test_write_json_atomic_overwrites_existing_file(tmp_path, previous):
    path = tmp_path / "tree.json"
    path.write_text(previous, encoding="utf-8")
    write_json_atomic(str(path), {"replaced": True})
    assert json.loads(path.read_text(encoding="utf-8")) == {"replaced": True}
    assert "x" not in path.read_text(encoding="utf-8")


def test_write_json_atomic_leaves_no_temp_files(tmp_path):
    path = tmp_path / "tree.json"
    for index in range(5):
        write_json_atomic(str(path), {"i": index})
    assert os.listdir(tmp_path) == ["tree.json"]
    assert json.loads(path.read_text(encoding="utf-8")) == {"i": 4}


def test_write_json_atomic_accepts_relative_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_json_atomic("relative.json", {"a": [1, 2]})
    assert json.loads((tmp_path / "relative.json").read_text(encoding="utf-8")) == {"a": [1, 2]}
    assert temp_files(tmp_path) == []


def test_write_json_atomic_writes_non_finite_floats_readably(tmp_path):
    path = tmp_path / "tree.json"
    data = {"format": FORMAT_NAME, "version": FORMAT_VERSION, "values": [float("nan"), float("inf"), float("-inf")]}
    write_json_atomic(str(path), data)
    values = read_tree_file(str(path))["values"]
    assert math.isnan(values[0]) and values[1] == math.inf and values[2] == -math.inf


@pytest.mark.parametrize("existing", [True, False], ids=["existing-file", "no-file"])
def test_write_json_atomic_unserialisable_data_keeps_original(tmp_path, existing):
    path = tmp_path / "tree.json"
    if existing:
        path.write_text('{"original": true}', encoding="utf-8")
    with pytest.raises(TypeError):
        write_json_atomic(str(path), {"bad": object()})
    if existing:
        assert path.read_text(encoding="utf-8") == '{"original": true}'
        assert os.listdir(tmp_path) == ["tree.json"]
    else:
        assert os.listdir(tmp_path) == []


def test_write_json_atomic_replace_failure_keeps_original_and_removes_temp(tmp_path, monkeypatch):
    path = tmp_path / "tree.json"
    path.write_text('{"original": true}', encoding="utf-8")
    calls = []

    def failing_replace(src, dst):
        calls.append((src, dst))
        assert os.path.exists(src), "temp file should exist when os.replace is called"
        with open(src, encoding="utf-8") as stream:
            assert json.load(stream) == {"new": 1}, "temp file should hold the complete new content"
        raise OSError(28, "No space left on device")

    with monkeypatch.context() as patch:
        patch.setattr(serialization.os, "replace", failing_replace)
        with pytest.raises(OSError):
            write_json_atomic(str(path), {"new": 1})

    assert len(calls) == 1
    src, dst = calls[0]
    # the temp file lives in the target's directory so os.replace is atomic (same file system)
    assert os.path.samefile(os.path.dirname(src), tmp_path)
    assert os.path.abspath(dst) == os.path.abspath(str(path))
    assert not os.path.exists(src)
    assert path.read_text(encoding="utf-8") == '{"original": true}'
    assert os.listdir(tmp_path) == ["tree.json"]


def test_write_json_atomic_base_exception_removes_temp(tmp_path, monkeypatch):
    class Abort(BaseException):
        pass

    path = tmp_path / "tree.json"
    path.write_text("keep", encoding="utf-8")

    def aborting_replace(src, dst):
        raise Abort()

    with monkeypatch.context() as patch:
        patch.setattr(serialization.os, "replace", aborting_replace)
        with pytest.raises(Abort):
            write_json_atomic(str(path), {"new": 1})
    assert path.read_text(encoding="utf-8") == "keep"
    assert os.listdir(tmp_path) == ["tree.json"]


def test_write_json_atomic_write_failure_removes_temp(tmp_path, monkeypatch):
    path = tmp_path / "tree.json"
    path.write_text("keep", encoding="utf-8")
    real_fdopen = os.fdopen

    class FullDisk:
        def __init__(self, stream):
            self._stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._stream.close()
            return False

        def write(self, text):
            raise OSError(28, "No space left on device")

    with monkeypatch.context() as patch:
        patch.setattr(serialization.os, "fdopen", lambda *a, **k: FullDisk(real_fdopen(*a, **k)))
        with pytest.raises(OSError):
            write_json_atomic(str(path), {"new": 1})
    assert path.read_text(encoding="utf-8") == "keep"
    assert os.listdir(tmp_path) == ["tree.json"]


def test_write_json_atomic_missing_directory_raises_os_error(tmp_path):
    path = tmp_path / "missing" / "tree.json"
    with pytest.raises(OSError):
        write_json_atomic(str(path), {"a": 1})
    assert os.listdir(tmp_path) == []


def test_write_json_atomic_onto_directory_raises_and_cleans_up(tmp_path):
    target = tmp_path / "folder"
    target.mkdir()
    with pytest.raises(OSError):
        write_json_atomic(str(target), {"a": 1})
    assert target.is_dir()
    assert os.listdir(tmp_path) == ["folder"]


@pytest.mark.skipif(sys.platform != "win32", reason="read-only files block os.replace only on Windows")
def test_write_json_atomic_read_only_target_keeps_original(tmp_path):
    path = tmp_path / "tree.json"
    path.write_text('{"original": true}', encoding="utf-8")
    os.chmod(path, stat.S_IREAD)
    try:
        with pytest.raises(OSError):
            write_json_atomic(str(path), {"new": 1})
        assert path.read_text(encoding="utf-8") == '{"original": true}'
        assert os.listdir(tmp_path) == ["tree.json"]
    finally:
        os.chmod(path, stat.S_IREAD | stat.S_IWRITE)


# ============================================================================ read_tree_file
def test_read_tree_file_returns_parsed_data(tmp_path):
    path = tmp_path / "tree.json"
    write_json_atomic(str(path), SAMPLE_DATA)
    assert read_tree_file(str(path)) == SAMPLE_DATA


def test_read_tree_file_accepts_minimal_file_and_extra_keys(tmp_path):
    path = write_text(tmp_path / "min.json", json.dumps({"format": FORMAT_NAME, "version": 1, "extra": {"x": 1}}))
    assert read_tree_file(path) == {"format": FORMAT_NAME, "version": 1, "extra": {"x": 1}}


def test_read_tree_file_round_trips_encoded_values(tmp_path):
    values = {"t": (1, 2), "s": {"a", "b"}, "d": {1: [None]}, "c": {"__set__": 1}}
    data = {"format": FORMAT_NAME, "version": FORMAT_VERSION, "blackboard": [], "values": encode_value(values)}
    path = tmp_path / "tree.json"
    write_json_atomic(str(path), data)
    loaded = read_tree_file(str(path))
    assert typed(decode_value(loaded["values"])) == typed(values)


def test_read_tree_file_missing_file(tmp_path):
    path = str(tmp_path / "does_not_exist.json")
    with pytest.raises(TreeFileError) as info:
        read_tree_file(path)
    message = str(info.value)
    assert "does_not_exist.json" in message
    assert "Could not read" in message
    assert isinstance(info.value.__cause__, FileNotFoundError)


def test_read_tree_file_directory(tmp_path):
    folder = tmp_path / "folder.json"
    folder.mkdir()
    with pytest.raises(TreeFileError, match="folder.json"):
        read_tree_file(str(folder))


@pytest.mark.parametrize(
    "text",
    ["", "{", "not json", "{'format': 'x'}", '{"format": "behavior_tree_widget",}', "[1, 2", '{"a": 1} trailing'],
    ids=["empty", "truncated", "garbage", "single-quotes", "trailing-comma", "truncated-array", "trailing-data"],
)
def test_read_tree_file_invalid_json(tmp_path, text):
    path = write_text(tmp_path / "broken.json", text)
    with pytest.raises(TreeFileError) as info:
        read_tree_file(path)
    assert "broken.json" in str(info.value)
    assert "not a valid JSON file" in str(info.value)
    assert isinstance(info.value.__cause__, json.JSONDecodeError)


def test_read_tree_file_invalid_utf8(tmp_path):
    path = tmp_path / "binary.json"
    path.write_bytes(b'{"format": "\xff\xfe\xfa"}')
    with pytest.raises(TreeFileError, match="binary.json"):
        read_tree_file(str(path))


@pytest.mark.parametrize("payload", [[], [1, 2], "text", 42, None, True], ids=repr)
def test_read_tree_file_non_dict_top_level(tmp_path, payload):
    path = write_text(tmp_path / "other.json", json.dumps(payload))
    with pytest.raises(TreeFileError) as info:
        read_tree_file(path)
    assert "other.json" in str(info.value)
    assert "not a behavior tree file" in str(info.value)


@pytest.mark.parametrize(
    "fmt", [_DELETE, "something_else", "", None, 1, ["behavior_tree_widget"], "Behavior_Tree_Widget"],
    ids=["missing", "other", "empty", "null", "int", "list", "wrong-case"],
)
def test_read_tree_file_wrong_format(tmp_path, fmt):
    path = write_tree(tmp_path / "fmt.json", format=fmt)
    with pytest.raises(TreeFileError) as info:
        read_tree_file(path)
    assert "fmt.json" in str(info.value)
    assert "not a behavior tree file" in str(info.value)


@pytest.mark.parametrize(
    "version",
    [_DELETE, None, True, False, "1", 0, -1, 1.0, 1.5, [1], {"v": 1}],
    ids=["missing", "null", "true", "false", "str", "zero", "negative", "float-1.0", "float-1.5", "list", "object"],
)
def test_read_tree_file_invalid_version(tmp_path, version):
    path = write_tree(tmp_path / "ver.json", version=version)
    with pytest.raises(TreeFileError) as info:
        read_tree_file(path)
    message = str(info.value)
    assert "ver.json" in message
    assert "version" in message
    if version is not _DELETE:
        assert repr(version) in message


@pytest.mark.parametrize("version", [FORMAT_VERSION + 1, 99])
def test_read_tree_file_newer_version(tmp_path, version):
    path = write_tree(tmp_path / "future.json", version=version)
    with pytest.raises(TreeFileError) as info:
        read_tree_file(path)
    message = str(info.value)
    assert "future.json" in message
    assert "newer" in message
    assert str(version) in message


def test_read_tree_file_accepts_current_version(tmp_path):
    path = write_tree(tmp_path / "ok.json", version=FORMAT_VERSION)
    assert read_tree_file(path)["version"] == FORMAT_VERSION


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        ("nodes", {}, "list"),
        ("nodes", "abc", "list"),
        ("nodes", 3, "list"),
        ("connections", {}, "list"),
        ("connections", "x", "list"),
        ("blackboard", {"name": "a"}, "list"),
        ("blackboard", 1.5, "list"),
        ("config", [], "dict"),
        ("config", "fast", "dict"),
        ("config", 100, "dict"),
    ],
)
def test_read_tree_file_wrong_section_types(tmp_path, key, value, expected):
    path = write_tree(tmp_path / "sections.json", **{key: value})
    with pytest.raises(TreeFileError) as info:
        read_tree_file(path)
    message = str(info.value)
    assert "sections.json" in message
    assert f"'{key}'" in message
    assert expected in message


def test_read_tree_file_optional_sections_may_be_missing(tmp_path):
    path = write_tree(tmp_path / "bare.json", nodes=_DELETE, connections=_DELETE, blackboard=_DELETE, config=_DELETE)
    assert read_tree_file(path) == {"format": FORMAT_NAME, "version": FORMAT_VERSION}


# ============================================================================ TreeConfig
def test_tree_config_defaults():
    config = TreeConfig()
    assert config.tick_interval_ms == 100
    assert config.repeat is False
    assert config.restore_blackboard is False
    assert config.default_memory is True
    assert TreeConfig.MIN_TICK_MS == 1
    assert TreeConfig.MAX_TICK_MS == 60_000


def test_tree_config_to_dict():
    config = TreeConfig(tick_interval_ms=250, repeat=True, restore_blackboard=True, default_memory=False)
    data = config.to_dict()
    assert data == {"tick_interval_ms": 250, "repeat": True, "restore_blackboard": True, "default_memory": False}
    assert json.loads(json.dumps(data)) == data
    data["tick_interval_ms"] = 1  # the returned dict is independent of the config
    assert config.tick_interval_ms == 250


@pytest.mark.parametrize(
    "config",
    [
        TreeConfig(),
        TreeConfig(tick_interval_ms=1, repeat=True, restore_blackboard=True, default_memory=False),
        TreeConfig(tick_interval_ms=60_000, repeat=False, restore_blackboard=True, default_memory=True),
        TreeConfig(tick_interval_ms=5, repeat=True, restore_blackboard=False, default_memory=False),
    ],
    ids=["defaults", "min", "max", "mixed"],
)
def test_tree_config_dict_round_trip(config):
    assert TreeConfig.from_dict(config.to_dict()) == config
    assert TreeConfig.from_dict(json.loads(json.dumps(config.to_dict()))) == config


@pytest.mark.parametrize("data", [None, [], "config", 5, ("tick_interval_ms", 5)], ids=repr)
def test_tree_config_from_non_dict_gives_defaults(data):
    assert TreeConfig.from_dict(data) == TreeConfig()


def test_tree_config_from_dict_ignores_unknown_keys():
    config = TreeConfig.from_dict({"tick_interval_ms": 42, "colour": "red", "MIN_TICK_MS": 99, "repeat": True})
    assert config == TreeConfig(tick_interval_ms=42, repeat=True)
    assert not hasattr(config, "colour")
    assert TreeConfig.MIN_TICK_MS == 1


def test_tree_config_from_partial_dict_keeps_other_defaults():
    assert TreeConfig.from_dict({"restore_blackboard": True}) == TreeConfig(restore_blackboard=True)
    assert TreeConfig.from_dict({}) == TreeConfig()


@pytest.mark.parametrize(
    "interval",
    ["250", 250.0, 12.5, None, [250], {"ms": 250}, True, False],
    ids=["str", "float-integral", "float", "null", "list", "object", "true", "false"],
)
def test_tree_config_from_dict_ignores_invalid_interval(interval):
    assert TreeConfig.from_dict({"tick_interval_ms": interval}).tick_interval_ms == 100


@pytest.mark.parametrize("name", ["repeat", "restore_blackboard", "default_memory"])
@pytest.mark.parametrize("value", [1, 0, "true", "false", None, 1.0, []], ids=repr)
def test_tree_config_from_dict_ignores_non_bool_flags(name, value):
    assert getattr(TreeConfig.from_dict({name: value}), name) == getattr(TreeConfig(), name)


@pytest.mark.parametrize("name", ["repeat", "restore_blackboard", "default_memory"])
@pytest.mark.parametrize("value", [True, False])
def test_tree_config_from_dict_accepts_bool_flags(name, value):
    assert getattr(TreeConfig.from_dict({name: value}), name) is value


@pytest.mark.parametrize(
    ("interval", "expected"),
    [(0, 1), (-5, 1), (-(10**12), 1), (1, 1), (2, 2), (59_999, 59_999), (60_000, 60_000), (60_001, 60_000), (10**12, 60_000)],
)
def test_tree_config_from_dict_clamps_interval(interval, expected):
    assert TreeConfig.from_dict({"tick_interval_ms": interval}).tick_interval_ms == expected


def test_tree_config_from_dict_does_not_modify_input():
    data = {"tick_interval_ms": 0, "repeat": "yes", "junk": [1]}
    snapshot = copy.deepcopy(data)
    TreeConfig.from_dict(data)
    assert data == snapshot


def test_tree_config_copy_is_equal_and_independent():
    original = TreeConfig(tick_interval_ms=7, repeat=True, restore_blackboard=False, default_memory=False)
    duplicate = original.copy()
    assert duplicate == original
    assert duplicate is not original
    assert type(duplicate) is TreeConfig
    duplicate.tick_interval_ms = 999
    duplicate.repeat = False
    duplicate.default_memory = True
    assert original == TreeConfig(tick_interval_ms=7, repeat=True, restore_blackboard=False, default_memory=False)


# ============================================================================ ConfigureDialog
@pytest.fixture
def make_dialog(qtbot):
    def make(config: TreeConfig) -> ConfigureDialog:
        dialog = ConfigureDialog(config)
        qtbot.addWidget(dialog)
        return dialog

    return make


def test_configure_dialog_prefills_defaults(make_dialog):
    dialog = make_dialog(TreeConfig())
    assert dialog.tick_interval.value() == 100
    assert dialog.run_mode.currentIndex() == 0
    assert dialog.run_mode.currentText() == ConfigureDialog.RUN_ONCE_TEXT
    assert dialog.restore_blackboard.isChecked() is False
    assert dialog.default_memory.isChecked() is True


def test_configure_dialog_prefills_custom_config(make_dialog):
    dialog = make_dialog(TreeConfig(tick_interval_ms=250, repeat=True, restore_blackboard=True, default_memory=False))
    assert dialog.tick_interval.value() == 250
    assert dialog.run_mode.currentIndex() == 1
    assert dialog.run_mode.currentText() == ConfigureDialog.REPEAT_TEXT
    assert dialog.restore_blackboard.isChecked() is True
    assert dialog.default_memory.isChecked() is False


@pytest.mark.parametrize(
    "config",
    [
        TreeConfig(),
        TreeConfig(tick_interval_ms=1, repeat=True, restore_blackboard=True, default_memory=False),
        TreeConfig(tick_interval_ms=60_000, repeat=False, restore_blackboard=True, default_memory=True),
    ],
    ids=["defaults", "min", "max"],
)
def test_configure_dialog_unedited_config_equals_input(make_dialog, config):
    dialog = make_dialog(config)
    result = dialog.config()
    assert result == config
    assert isinstance(result, TreeConfig)
    assert result is not config


def test_configure_dialog_returns_edited_values(make_dialog):
    original = TreeConfig()
    dialog = make_dialog(original)
    dialog.tick_interval.setValue(42)
    dialog.run_mode.setCurrentIndex(1)
    dialog.restore_blackboard.setChecked(True)
    dialog.default_memory.setChecked(False)
    assert dialog.config() == TreeConfig(tick_interval_ms=42, repeat=True, restore_blackboard=True, default_memory=False)
    # the dialog edits a copy: the config it was created from is untouched
    assert original == TreeConfig()


def test_configure_dialog_switching_back_to_run_once(make_dialog):
    dialog = make_dialog(TreeConfig(repeat=True))
    dialog.run_mode.setCurrentText(ConfigureDialog.RUN_ONCE_TEXT)
    assert dialog.config().repeat is False
    dialog.run_mode.setCurrentText(ConfigureDialog.REPEAT_TEXT)
    assert dialog.config().repeat is True


def test_configure_dialog_run_mode_items(make_dialog):
    dialog = make_dialog(TreeConfig())
    items = [dialog.run_mode.itemText(index) for index in range(dialog.run_mode.count())]
    assert items == [ConfigureDialog.RUN_ONCE_TEXT, ConfigureDialog.REPEAT_TEXT]


def test_configure_dialog_interval_range_is_clamped(make_dialog):
    dialog = make_dialog(TreeConfig())
    assert dialog.tick_interval.minimum() == TreeConfig.MIN_TICK_MS
    assert dialog.tick_interval.maximum() == TreeConfig.MAX_TICK_MS
    dialog.tick_interval.setValue(0)
    assert dialog.config().tick_interval_ms == TreeConfig.MIN_TICK_MS
    dialog.tick_interval.setValue(10**7)
    assert dialog.config().tick_interval_ms == TreeConfig.MAX_TICK_MS


@pytest.mark.parametrize(("interval", "expected"), [(0, 1), (-3, 1), (123_456, 60_000)])
def test_configure_dialog_prefill_out_of_range_interval_is_clamped(make_dialog, interval, expected):
    dialog = make_dialog(TreeConfig(tick_interval_ms=interval))
    assert dialog.tick_interval.value() == expected
    assert dialog.config().tick_interval_ms == expected


def test_configure_dialog_widgets_have_object_names(make_dialog):
    from PySide6.QtWidgets import QCheckBox, QComboBox, QSpinBox

    dialog = make_dialog(TreeConfig())
    assert dialog.findChild(QSpinBox, "TickInterval") is dialog.tick_interval
    assert dialog.findChild(QComboBox, "RunMode") is dialog.run_mode
    assert dialog.findChild(QCheckBox, "RestoreBlackboard") is dialog.restore_blackboard
    assert dialog.findChild(QCheckBox, "DefaultMemory") is dialog.default_memory
    assert dialog.windowTitle() == "Configure Behavior Tree"
    assert dialog.tick_interval.suffix() == " ms"


def test_configure_dialog_config_is_fresh_each_call(make_dialog):
    dialog = make_dialog(TreeConfig())
    first = dialog.config()
    first.tick_interval_ms = 5
    assert dialog.config().tick_interval_ms == 100


# ============================================================================ widget integration
def test_widget_configure_accept_applies_values(bt, dialogs):
    seen = []

    def handler(dialog):
        seen.append(dialog)
        assert isinstance(dialog, ConfigureDialog)
        dialog.tick_interval.setValue(333)
        dialog.run_mode.setCurrentIndex(1)
        dialog.restore_blackboard.setChecked(True)
        dialog.default_memory.setChecked(False)
        return True

    dialogs.dialog_handler = handler
    assert not bt.IsModified()
    bt.Configure()
    assert len(seen) == 1
    assert bt.GetConfig() == TreeConfig(tick_interval_ms=333, repeat=True, restore_blackboard=True, default_memory=False)
    assert bt.IsModified()


def test_widget_configure_reject_keeps_config(bt, dialogs):
    def handler(dialog):
        dialog.tick_interval.setValue(999)
        dialog.run_mode.setCurrentIndex(1)
        return False

    dialogs.dialog_handler = handler
    before = bt.GetConfig()
    bt.Configure()
    assert bt.GetConfig() == before
    assert not bt.IsModified()


def test_widget_configure_dialog_is_prefilled_with_current_config(bt, dialogs):
    bt.SetConfig(TreeConfig(tick_interval_ms=77, repeat=True, restore_blackboard=True, default_memory=False))
    captured = []
    dialogs.dialog_handler = lambda dialog: (captured.append(dialog.config()), False)[-1]
    bt.Configure()
    assert captured == [TreeConfig(tick_interval_ms=77, repeat=True, restore_blackboard=True, default_memory=False)]


def test_widget_get_config_returns_copy(bt):
    config = bt.GetConfig()
    config.tick_interval_ms = 5
    assert bt.GetConfig().tick_interval_ms == 100


def test_saved_tree_file_passes_validation_and_contains_config(bt, tmp_path):
    config = TreeConfig(tick_interval_ms=250, repeat=True, restore_blackboard=True, default_memory=False)
    bt.SetConfig(config)
    path = tmp_path / "saved.json"
    assert bt.SaveTree(str(path))
    data = read_tree_file(str(path))
    assert data["format"] == FORMAT_NAME
    assert data["version"] == FORMAT_VERSION
    assert data["config"] == config.to_dict()
    assert isinstance(data["nodes"], list) and isinstance(data["connections"], list)
    assert isinstance(data["blackboard"], list)
    assert temp_files(tmp_path) == []


def test_config_survives_save_and_load(make_widget, tmp_path):
    config = TreeConfig(tick_interval_ms=1234, repeat=True, restore_blackboard=True, default_memory=False)
    first = make_widget()
    assert first.NewTree(str(tmp_path / "a.json"))
    first.SetConfig(config)
    assert first.SaveTree(str(tmp_path / "a.json"))

    second = make_widget()
    assert second.LoadTree(str(tmp_path / "a.json"))
    assert second.GetConfig() == config


@pytest.mark.parametrize(
    ("saved", "expected"),
    [
        ({"tick_interval_ms": 0}, TreeConfig(tick_interval_ms=1)),
        ({"tick_interval_ms": 10**9, "repeat": True}, TreeConfig(tick_interval_ms=60_000, repeat=True)),
        ({"tick_interval_ms": True, "repeat": 1, "junk": 5}, TreeConfig()),
    ],
    ids=["too-small", "too-large", "invalid-types"],
)
def test_loaded_config_is_sanitised(make_widget, tmp_path, saved, expected):
    path = write_tree(tmp_path / "cfg.json", config=saved)
    widget = make_widget()
    assert widget.LoadTree(path)
    assert widget.GetConfig() == expected


def test_load_rejects_invalid_file_with_tree_file_error(make_widget, tmp_path):
    path = write_tree(tmp_path / "bad.json", version=FORMAT_VERSION + 1)
    widget = make_widget()
    with pytest.raises(TreeFileError, match="newer"):
        widget.LoadTree(path)
    assert not widget.IsTreeLoaded()


def test_blackboard_containers_survive_file_round_trip(make_widget, tmp_path):
    path = str(tmp_path / "bb.json")
    first = make_widget()
    assert first.NewTree(path)
    first.SetEntry({1: (2, 3), "k": {"a", "b"}, (4, 5): None, "__set__": [1]}, "mapping")
    first.SetEntry({(1, 2), "x", 3}, "things")
    assert first.SaveTree(path)

    second = make_widget()
    assert second.LoadTree(path)
    assert typed(second.GetEntry("mapping")) == typed({1: (2, 3), "k": {"a", "b"}, (4, 5): None, "__set__": [1]})
    assert typed(second.GetEntry("things")) == typed({(1, 2), "x", 3})


def test_blackboard_set_of_frozensets_survives_file_round_trip(make_widget, tmp_path):
    """A Set entry holding frozensets is saved (encodable) and must load back."""
    path = str(tmp_path / "fs.json")
    first = make_widget()
    assert first.NewTree(path)
    value = {frozenset({1, 2}), frozenset({3})}
    first.SetEntry(value, "groups")
    assert first.SaveTree(path)

    second = make_widget()
    assert second.LoadTree(path)
    assert second.HasEntry("groups")
    assert second.GetEntry("groups") == value


def test_set_in_hashable_position_decodes_as_frozenset():
    """Files written before the __frozenset__ tag stored frozensets as __set__."""
    assert decode_value({"__dict__": [[{"__set__": [1]}, "x"]]}) == {frozenset({1}): "x"}
    assert decode_value({"__set__": [{"__set__": [1, 2]}]}) == {frozenset({1, 2})}



def test_write_json_atomic_retries_then_fails_cleanly_when_target_is_held_open(tmp_path, monkeypatch):
    """On Windows another program keeping the file open makes os.replace fail with PermissionError:
    the replacement is retried; if it keeps failing the original is left intact and no temp file remains."""
    path = tmp_path / "tree.json"
    path.write_text('{"original": true}', encoding="utf-8")
    calls = []

    def locked_replace(src, dst):
        calls.append(src)
        raise PermissionError(13, "The process cannot access the file")

    monkeypatch.setattr(serialization.time, "sleep", lambda _s: None)
    with monkeypatch.context() as patch:
        patch.setattr(serialization.os, "replace", locked_replace)
        with pytest.raises(PermissionError) as info:
            write_json_atomic(str(path), {"new": 1})
    assert "tree.json" in str(info.value)
    assert len(calls) == serialization._REPLACE_ATTEMPTS
    assert json.loads(path.read_text(encoding="utf-8")) == {"original": True}
    assert os.listdir(tmp_path) == ["tree.json"]
