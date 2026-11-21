"""Strict parser and in-memory applier for git-format unified diffs.

Everything works on bytes and splits on b"\\n" only. Anything malformed,
ambiguous or unsupported raises PatchError, so the caller can fall back to
running every test. apply() never searches for an offset and never fuzzes: a
hunk applies at its stated position or not at all. The rules mirror what
`git apply` (default options) accepts, so whenever apply() succeeds its output
is byte-identical to what `git apply` writes.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator

__all__ = [
    "PatchError",
    "Hunk",
    "FilePatch",
    "parse",
    "apply",
    "apply_all",
    "git_blob_id",
    "index_matches",
]


class PatchError(ValueError):
    """The patch is malformed, unsupported, or does not apply exactly."""


_DIFF_GIT = b"diff --git "
_HUNK_RE = re.compile(rb"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)", re.S)
_INDEX_RE = re.compile(rb"([0-9a-f]{4,64})\.\.([0-9a-f]{4,64})(?: ([0-7]{6}))?")
_MODE_RE = re.compile(rb"[0-7]{6}")
_PERCENT_RE = re.compile(rb"(\d{1,3})%")
_BINARY_DATA_RE = re.compile(rb"(?:(?:literal|delta) \d+|[A-Za-z][0-9A-Za-z!#$%&()*+\-;<=>?@^_`{|}~]+|)")
_SUPPORTED_MODES = (0o100644, 0o100755)
# Preamble lines (before the first `diff --git`) that git might read as patch data.
_SUSPICIOUS_PREAMBLE = (
    b"diff ", b"--- ", b"+++ ", b"@@", b"*** ", b"Index:", b"index ", b"Binary files ",
    b"GIT binary patch", b"old mode ", b"new mode ", b"new file mode ", b"deleted file mode ",
    b"rename ", b"copy ", b"similarity index ", b"dissimilarity index ",
)
_EXT_HEADERS = (
    (b"old mode ", "old mode"),
    (b"new mode ", "new mode"),
    (b"deleted file mode ", "deleted file mode"),
    (b"new file mode ", "new file mode"),
    (b"similarity index ", "similarity index"),
    (b"dissimilarity index ", "dissimilarity index"),
    (b"rename from ", "rename from"),
    (b"rename to ", "rename to"),
    (b"copy from ", "copy from"),
    (b"copy to ", "copy to"),
    (b"index ", "index"),
)
_C_ESCAPES = {
    ord("a"): 7, ord("b"): 8, ord("t"): 9, ord("n"): 10, ord("v"): 11, ord("f"): 12,
    ord("r"): 13, ord('"'): 34, ord("\\"): 92,
}


class _Record:
    """Slotted record with repr and equality (avoids importing dataclasses)."""

    __slots__ = ()

    def __repr__(self) -> str:
        fields = ", ".join(f"{name}={getattr(self, name)!r}" for name in self.__slots__)
        return f"{type(self).__name__}({fields})"

    def __eq__(self, other: object) -> bool:
        return type(other) is type(self) and all(
            getattr(self, name) == getattr(other, name) for name in self.__slots__
        )

    __hash__ = None  # type: ignore[assignment]


class Hunk(_Record):
    """One `@@ -old_start,old_len +new_start,new_len @@` block.

    lines holds (tag, data) with tag in " ", "-", "+"; data is the line's bytes
    including its b"\\n", which is absent only when the line was followed by a
    `\\ No newline at end of file` marker. trailing counts the context lines
    after the last change (0 means the hunk must end at EOF, as git requires).
    """

    __slots__ = ("old_start", "old_len", "new_start", "new_len", "lines", "heading", "trailing")

    def __init__(
        self, old_start: int, old_len: int, new_start: int, new_len: int,
        lines: list[tuple[str, bytes]], heading: bytes = b"", trailing: int = 0,
    ) -> None:
        self.old_start, self.old_len, self.new_start, self.new_len = old_start, old_len, new_start, new_len
        self.lines, self.heading, self.trailing = lines, heading, trailing

    def old_side(self) -> list[bytes]:
        """Context and removed lines, as they must appear in the old file."""
        return [data for tag, data in self.lines if tag != "+"]

    def new_side(self) -> list[bytes]:
        """Context and added lines, as they appear in the new file."""
        return [data for tag, data in self.lines if tag != "-"]

    @property
    def old_pos(self) -> int:
        """0-based index in the old file of the first line this hunk covers."""
        return self.old_start - 1 if self.old_len else self.old_start


class FilePatch(_Record):
    """The changes to one file. Paths are repository-relative str; None is /dev/null."""

    __slots__ = (
        "old_path", "new_path", "is_new", "is_deleted", "is_rename", "is_copy", "is_binary",
        "old_mode", "new_mode", "similarity", "old_index", "new_index", "hunks",
    )

    def __init__(
        self, old_path: str | None, new_path: str | None, *, is_new: bool = False,
        is_deleted: bool = False, is_rename: bool = False, is_copy: bool = False,
        is_binary: bool = False, old_mode: int | None = None, new_mode: int | None = None,
        similarity: int | None = None, old_index: str | None = None, new_index: str | None = None,
        hunks: list[Hunk] | None = None,
    ) -> None:
        self.old_path, self.new_path = old_path, new_path
        self.is_new, self.is_deleted, self.is_rename, self.is_copy = is_new, is_deleted, is_rename, is_copy
        self.is_binary, self.old_mode, self.new_mode, self.similarity = is_binary, old_mode, new_mode, similarity
        self.old_index, self.new_index = old_index, new_index
        self.hunks: list[Hunk] = [] if hunks is None else hunks

    @property
    def path(self) -> str:
        """The path after the patch, or the deleted path."""
        return self.new_path if self.new_path is not None else self.old_path  # type: ignore[return-value]

    @property
    def mode_changed(self) -> bool:
        return (
            not self.is_new and not self.is_deleted
            and self.old_mode is not None and self.new_mode is not None
            and self.old_mode != self.new_mode
        )

    def _walk(self) -> Iterator[tuple[Hunk, int, str, int, int]]:
        """Yield (hunk, index, tag, old line number, new line number), 1-based."""
        for hunk in self.hunks:
            old_no = hunk.old_start if hunk.old_len else hunk.old_start + 1
            new_no = hunk.new_start if hunk.new_len else hunk.new_start + 1
            for index, (tag, _data) in enumerate(hunk.lines):
                yield hunk, index, tag, old_no, new_no
                if tag != "+":
                    old_no += 1
                if tag != "-":
                    new_no += 1

    def removed_lines(self) -> list[int]:
        """1-based old-file numbers of removed lines."""
        return [old for _h, _i, tag, old, _new in self._walk() if tag == "-"]

    def added_lines(self) -> list[int]:
        """1-based new-file numbers of added lines."""
        return [new for _h, _i, tag, _old, new in self._walk() if tag == "+"]

    def insertion_points(self) -> list[tuple[int, int | None]]:
        """For each run of added lines with no removed line, (prev, next) old line numbers.

        prev is the old line just before the insertion (0 = start of file); next is
        the old line just after it, or None when the insertion ends the file.
        """
        points: list[tuple[int, int | None]] = []
        run_start: tuple[Hunk, int, int] | None = None
        has_removal = False
        for hunk, index, tag, old, _new in self._walk():
            if run_start is not None and (tag == " " or run_start[0] is not hunk):
                if not has_removal:
                    points.append(self._insertion(run_start))
                run_start = None
            if tag != " " and run_start is None:
                run_start, has_removal = (hunk, index, old), False
            if tag == "-":
                has_removal = True
        if run_start is not None and not has_removal:
            points.append(self._insertion(run_start))
        return points

    @staticmethod
    def _insertion(run_start: tuple[Hunk, int, int]) -> tuple[int, int | None]:
        hunk, index, old = run_start
        follows = any(tag != "+" for tag, _data in hunk.lines[index:])
        return old - 1, (old if follows else None)

    def touched_old_lines(self) -> list[int]:
        """Sorted old line numbers: removed lines plus lines adjacent to pure insertions."""
        touched = set(self.removed_lines())
        for prev, nxt in self.insertion_points():
            if prev >= 1:
                touched.add(prev)
            if nxt is not None:
                touched.add(nxt)
        return sorted(touched)


# ----------------------------------------------------------------------------- parsing


def parse(patch: bytes) -> list[FilePatch]:
    """Parse a git-format patch into FilePatch objects (raises PatchError)."""
    if not isinstance(patch, (bytes, bytearray, memoryview)):
        raise TypeError("patch must be bytes")
    data = bytes(patch)
    if not data:
        raise PatchError("empty patch")
    if not data.endswith(b"\n"):
        raise PatchError("patch does not end with a newline")
    lines = data.split(b"\n")
    lines.pop()
    count = len(lines)
    i = 0
    while i < count and not lines[i].startswith(_DIFF_GIT):
        if lines[i].startswith(_SUSPICIOUS_PREAMBLE):
            raise PatchError(f"line {i + 1}: patch data before the first 'diff --git'")
        i += 1
    if i == count:
        raise PatchError("no 'diff --git' header")
    files: list[FilePatch] = []
    while i < count:
        line = lines[i]
        if line.startswith(_DIFF_GIT):
            file_patch, i = _parse_file(lines, i)
            files.append(file_patch)
        elif line == b"-- ":
            _check_signature(lines, i)
            break
        else:
            raise PatchError(f"line {i + 1}: unexpected line between file patches")
    return files


def _check_signature(lines: list[bytes], i: int) -> None:
    """Accept a format-patch trailer: '-- ', one version line, then blank lines."""
    rest = [line for line in lines[i + 1:] if line]
    if len(rest) > 1 or (rest and rest[0].startswith(_SUSPICIOUS_PREAMBLE + (b" ", b"-", b"+", b"\\"))):
        raise PatchError(f"line {i + 1}: unexpected data after signature")
    if rest and lines[i + 1] != rest[0]:
        raise PatchError(f"line {i + 2}: malformed signature")


def _parse_file(lines: list[bytes], i: int) -> tuple[FilePatch, int]:
    count = len(lines)
    start_line = i + 1
    a_name, b_name = _split_git_names(lines[i][len(_DIFF_GIT):])
    headers: dict[str, bytes] = {}
    minus_name: bytes | None = None
    plus_name: bytes | None = None
    has_names = False
    is_binary = False
    hunks: list[Hunk] = []
    i += 1
    while i < count:
        line = lines[i]
        if line.startswith(_DIFF_GIT) or line == b"-- ":
            break
        if line.startswith(b"--- "):
            if i + 1 >= count or not lines[i + 1].startswith(b"+++ "):
                raise PatchError(f"line {i + 1}: '---' not followed by '+++'")
            minus_name = _parse_name_line(lines[i][4:], b"a/")
            plus_name = _parse_name_line(lines[i + 1][4:], b"b/")
            has_names = True
            hunks, i = _parse_hunks(lines, i + 2)
            if not hunks:
                raise PatchError(f"line {i + 1}: file header without hunks")
            break
        if (line.startswith(b"Binary files ") or line.startswith(b"Files ")) and line.endswith(b" differ"):
            is_binary = True
            i += 1
            break
        if line == b"GIT binary patch":
            is_binary = True
            i = _skip_binary(lines, i + 1)
            break
        for prefix, key in _EXT_HEADERS:
            if line.startswith(prefix):
                if key in headers:
                    raise PatchError(f"line {i + 1}: repeated '{key}' header")
                headers[key] = line[len(prefix):]
                break
        else:
            raise PatchError(f"line {i + 1}: unknown header line")
        i += 1
    fp = _build_file_patch(a_name, b_name, headers, start_line)
    fp.is_binary = is_binary
    fp.hunks = hunks
    _check_names(fp, has_names, minus_name, plus_name, start_line)
    _check_hunks(fp, start_line)
    return fp, i


def _skip_binary(lines: list[bytes], i: int) -> int:
    blocks = 0
    while i < len(lines) and not lines[i].startswith(_DIFF_GIT) and lines[i] != b"-- ":
        if not _BINARY_DATA_RE.fullmatch(lines[i]):
            raise PatchError(f"line {i + 1}: malformed binary patch data")
        if lines[i].startswith((b"literal ", b"delta ")):
            blocks += 1
        i += 1
    if not blocks:
        raise PatchError(f"line {i}: binary patch without data")
    return i


def _parse_mode(value: bytes, start_line: int) -> int:
    if not _MODE_RE.fullmatch(value):
        raise PatchError(f"file patch at line {start_line}: malformed mode")
    mode = int(value, 8)
    if mode not in _SUPPORTED_MODES:
        raise PatchError(f"file patch at line {start_line}: unsupported mode {value.decode()}")
    return mode


def _build_file_patch(a_name: bytes, b_name: bytes, headers: dict[str, bytes], start_line: int) -> FilePatch:
    where = f"file patch at line {start_line}"
    is_new = "new file mode" in headers
    is_deleted = "deleted file mode" in headers
    is_rename = "rename from" in headers or "rename to" in headers
    is_copy = "copy from" in headers or "copy to" in headers
    if is_rename and not ("rename from" in headers and "rename to" in headers):
        raise PatchError(f"{where}: incomplete rename")
    if is_copy and not ("copy from" in headers and "copy to" in headers):
        raise PatchError(f"{where}: incomplete copy")
    if sum((is_new, is_deleted, is_rename, is_copy)) > 1:
        raise PatchError(f"{where}: contradictory headers")
    has_mode_pair = "old mode" in headers or "new mode" in headers
    if has_mode_pair and not ("old mode" in headers and "new mode" in headers):
        raise PatchError(f"{where}: incomplete mode change")
    if has_mode_pair and (is_new or is_deleted):
        raise PatchError(f"{where}: mode change on a created or deleted file")
    if ("similarity index" in headers) != (is_rename or is_copy):
        raise PatchError(f"{where}: similarity index without rename or copy")
    if "dissimilarity index" in headers and (is_rename or is_copy or is_new or is_deleted):
        raise PatchError(f"{where}: unexpected dissimilarity index")

    old_name, new_name = a_name, b_name
    if is_rename:
        old_name, new_name = _unquote_full(headers["rename from"]), _unquote_full(headers["rename to"])
    elif is_copy:
        old_name, new_name = _unquote_full(headers["copy from"]), _unquote_full(headers["copy to"])
    if (old_name, new_name) != (a_name, b_name):
        raise PatchError(f"{where}: names disagree with the 'diff --git' line")
    if not (is_rename or is_copy) and a_name != b_name:
        raise PatchError(f"{where}: different names without rename or copy")
    if (is_rename or is_copy) and a_name == b_name:
        raise PatchError(f"{where}: rename or copy onto itself")

    fp = FilePatch(
        old_path=None if is_new else _decode_path(old_name, where),
        new_path=None if is_deleted else _decode_path(new_name, where),
        is_new=is_new, is_deleted=is_deleted, is_rename=is_rename, is_copy=is_copy,
    )
    if is_new:
        fp.new_mode = _parse_mode(headers["new file mode"], start_line)
    if is_deleted:
        fp.old_mode = _parse_mode(headers["deleted file mode"], start_line)
    if has_mode_pair:
        fp.old_mode = _parse_mode(headers["old mode"], start_line)
        fp.new_mode = _parse_mode(headers["new mode"], start_line)
    for key in ("similarity index", "dissimilarity index"):
        if key in headers:
            match = _PERCENT_RE.fullmatch(headers[key])
            if not match or int(match[1]) > 100:
                raise PatchError(f"{where}: malformed {key}")
            if key == "similarity index":
                fp.similarity = int(match[1])
    if "index" in headers:
        match = _INDEX_RE.fullmatch(headers["index"])
        if not match:
            raise PatchError(f"{where}: malformed index line")
        fp.old_index, fp.new_index = match[1].decode(), match[2].decode()
        if is_new != (set(fp.old_index) == {"0"}) or is_deleted != (set(fp.new_index) == {"0"}):
            raise PatchError(f"{where}: index line disagrees with creation or deletion")
        if match[3] is not None:
            if has_mode_pair or is_new or is_deleted:
                raise PatchError(f"{where}: unexpected mode on index line")
            fp.old_mode = fp.new_mode = _parse_mode(match[3], start_line)
    return fp


def _check_names(fp: FilePatch, has_names: bool, minus: bytes | None, plus: bytes | None, start_line: int) -> None:
    where = f"file patch at line {start_line}"
    if has_names:
        minus_path = None if minus is None else _decode_path(minus, where)
        plus_path = None if plus is None else _decode_path(plus, where)
        if minus_path != fp.old_path or plus_path != fp.new_path:
            raise PatchError(f"{where}: '---'/'+++' names disagree with the header")
    if not fp.hunks and not fp.is_binary and not (
        fp.is_new or fp.is_deleted or fp.is_rename or fp.is_copy or fp.mode_changed
    ):
        raise PatchError(f"{where}: file patch without changes")


def _check_hunks(fp: FilePatch, start_line: int) -> None:
    """Order, position and side checks that git applies (or that make git misplace hunks)."""
    where = f"file patch at line {start_line}"
    previous_end: int | None = None
    delta = 0
    for number, hunk in enumerate(fp.hunks):
        old_pos = hunk.old_start - 1 if hunk.old_len else hunk.old_start
        new_pos = hunk.new_start - 1 if hunk.new_len else hunk.new_start
        if old_pos < 0 or new_pos < 0:
            raise PatchError(f"{where}: hunk {number + 1} starts at line 0")
        if hunk.old_start <= 1 and old_pos != 0:
            raise PatchError(f"{where}: hunk {number + 1} is ambiguous at the start of the file")
        if hunk.old_len == 0 and (hunk.old_start != 0 or len(fp.hunks) != 1):
            raise PatchError(f"{where}: context-free insertion git cannot place")
        if previous_end is not None and (old_pos < previous_end or old_pos == 0):
            raise PatchError(f"{where}: hunk {number + 1} overlaps or is out of order")
        if new_pos != old_pos + delta:
            raise PatchError(f"{where}: hunk {number + 1} has inconsistent new start")
        delta += hunk.new_len - hunk.old_len
        previous_end = old_pos + hunk.old_len
        if fp.is_new and hunk.old_len:
            raise PatchError(f"{where}: new file depends on old contents")
        if fp.is_deleted and hunk.new_len:
            raise PatchError(f"{where}: deleted file still has contents")
        no_newline_new = any(not data.endswith(b"\n") for tag, data in hunk.lines if tag != "-")
        if no_newline_new and number != len(fp.hunks) - 1:
            raise PatchError(f"{where}: missing newline before the last hunk")


def _parse_hunks(lines: list[bytes], i: int) -> tuple[list[Hunk], int]:
    count = len(lines)
    hunks: list[Hunk] = []
    while i < count and lines[i].startswith(b"@@ "):
        match = _HUNK_RE.fullmatch(lines[i])
        if not match:
            raise PatchError(f"line {i + 1}: malformed hunk header")
        header_line = i + 1
        old_start = int(match[1])
        old_len = int(match[2]) if match[2] is not None else 1
        new_start = int(match[3])
        new_len = int(match[4]) if match[4] is not None else 1
        old_left, new_left = old_len, new_len
        body: list[list] = []  # [tag, data]
        old_closed = new_closed = False  # a side ended without newline
        changed = False
        trailing = 0
        i += 1
        while old_left or new_left:
            if i >= count:
                raise PatchError(f"hunk at line {header_line}: truncated")
            line = lines[i]
            first = line[:1]
            if first == b"\\":
                _apply_marker(body, line, i)
                i += 1
                continue
            if first in (b" ", b""):
                tag, uses_old, uses_new = " ", True, True
            elif first == b"-":
                tag, uses_old, uses_new = "-", True, False
            elif first == b"+":
                tag, uses_old, uses_new = "+", False, True
            else:
                raise PatchError(f"line {i + 1}: invalid hunk line")
            if (uses_old and not old_left) or (uses_new and not new_left):
                raise PatchError(f"hunk at line {header_line}: more lines than its header counts")
            if body and not body[-1][1].endswith(b"\n"):
                previous_tag = body[-1][0]
                old_closed = old_closed or previous_tag != "+"
                new_closed = new_closed or previous_tag != "-"
            if (uses_old and old_closed) or (uses_new and new_closed):
                raise PatchError(f"line {i + 1}: line after a missing newline at end of file")
            if uses_old:
                old_left -= 1
            if uses_new:
                new_left -= 1
            if tag == " ":
                trailing += 1
            else:
                changed = True
                trailing = 0
            body.append([tag, line[1:] + b"\n"])
            i += 1
        if i < count and lines[i][:1] == b"\\":
            _apply_marker(body, lines[i], i)
            i += 1
        if not changed:
            raise PatchError(f"hunk at line {header_line}: no changes")
        hunks.append(Hunk(
            old_start, old_len, new_start, new_len,
            [(tag, data) for tag, data in body], match[5], trailing,
        ))
    return hunks, i


def _apply_marker(body: list[list], line: bytes, i: int) -> None:
    """`\\ No newline at end of file`: strip the newline of the preceding line."""
    if not line.startswith(b"\\ ") or len(line) + 1 < 12:
        raise PatchError(f"line {i + 1}: malformed '\\' line")
    if not body or not body[-1][1].endswith(b"\n"):
        raise PatchError(f"line {i + 1}: misplaced '\\ No newline' marker")
    body[-1][1] = body[-1][1][:-1]


def _split_git_names(rest: bytes) -> tuple[bytes, bytes]:
    """Split the `a/X b/Y` part of a `diff --git` line."""
    if rest.startswith(b'"'):
        a_name, tail = _unquote(rest)
        if not tail.startswith(b" "):
            raise PatchError("malformed 'diff --git' line")
        b_name = _unquote_full(tail[1:])
    elif b'"' in rest:
        split = rest.find(b' "')
        if split < 0:
            raise PatchError("malformed 'diff --git' line")
        a_name, b_name = rest[:split], _unquote_full(rest[split + 1:])
        _check_unquoted(a_name)
    else:
        a_name = b_name = None
        if len(rest) % 2 == 1:
            half = len(rest) // 2
            if rest[half:half + 3] == b" b/" and rest[2:half] == rest[half + 3:]:
                a_name, b_name = rest[:half], rest[half + 1:]
        if a_name is None:
            if rest.count(b" b/") != 1:
                raise PatchError("ambiguous 'diff --git' line")
            split = rest.find(b" b/")
            a_name, b_name = rest[:split], rest[split + 1:]
        _check_unquoted(a_name)
        _check_unquoted(b_name)
    if not a_name.startswith(b"a/") or not b_name.startswith(b"b/"):
        raise PatchError("'diff --git' names lack a/ and b/ prefixes")
    return a_name[2:], b_name[2:]


def _parse_name_line(rest: bytes, prefix: bytes) -> bytes | None:
    """Name on a '---'/'+++' line: None for /dev/null, else the name without prefix."""
    if rest.endswith(b"\t"):
        rest = rest[:-1]
    if rest == b"/dev/null":
        return None
    if rest.startswith(b'"'):
        name = _unquote_full(rest)
    else:
        _check_unquoted(rest)
        name = rest
    if not name.startswith(prefix):
        raise PatchError(f"name on '---'/'+++' line lacks {prefix.decode()} prefix")
    return name[len(prefix):]


def _check_unquoted(name: bytes) -> None:
    """git quotes names with control characters, quotes or backslashes."""
    if any(byte < 0x20 or byte in (0x22, 0x5C, 0x7F) for byte in name):
        raise PatchError("unquoted name with special characters")


def _unquote_full(text: bytes) -> bytes:
    if not text.startswith(b'"'):
        _check_unquoted(text)
        return text
    name, tail = _unquote(text)
    if tail:
        raise PatchError("data after quoted name")
    return name


def _unquote(text: bytes) -> tuple[bytes, bytes]:
    """Decode a C-quoted name at the start of text; return (name, remainder)."""
    out = bytearray()
    i = 1
    while i < len(text):
        byte = text[i]
        if byte == 0x22:
            return bytes(out), text[i + 1:]
        if byte == 0x5C:
            if i + 1 >= len(text):
                break
            code = text[i + 1]
            if code in _C_ESCAPES:
                out.append(_C_ESCAPES[code])
                i += 2
                continue
            octal = text[i + 1:i + 4]
            if len(octal) == 3 and all(0x30 <= c <= 0x37 for c in octal) and octal[0] <= 0x33:
                out.append(int(octal, 8))
                i += 4
                continue
            raise PatchError("bad escape in quoted name")
        out.append(byte)
        i += 1
    raise PatchError("unterminated quoted name")


def _decode_path(name: bytes, where: str) -> str:
    try:
        path = name.decode("utf-8")
    except UnicodeDecodeError:
        raise PatchError(f"{where}: path is not UTF-8") from None
    parts = path.split("/")
    if not path or path.startswith("/") or any(part in ("", ".", "..") for part in parts) or "\0" in path:
        raise PatchError(f"{where}: unsafe path {path!r}")
    return path


# ----------------------------------------------------------------------------- applying


def _split_keep(data: bytes) -> list[bytes]:
    """Lines with their b"\\n"; only the last one may lack it."""
    if not data:
        return []
    pieces = data.split(b"\n")
    lines = [piece + b"\n" for piece in pieces[:-1]]
    if pieces[-1]:
        lines.append(pieces[-1])
    return lines


def apply(fp: FilePatch, old_bytes: bytes | None) -> bytes | None:
    """Apply one FilePatch to the old content exactly; None for a deleted file."""
    if fp.is_binary:
        raise PatchError(f"{fp.path}: binary patch cannot be applied")
    if fp.is_new:
        if old_bytes:
            raise PatchError(f"{fp.path}: file to create already has content")
        old_bytes = b""
    elif old_bytes is None:
        raise PatchError(f"{fp.path}: file to patch does not exist")
    source = _split_keep(bytes(old_bytes))
    total = len(source)
    out: list[bytes] = []
    position = 0
    for number, hunk in enumerate(fp.hunks):
        start = hunk.old_pos
        end = start + hunk.old_len
        if start < position or end > total:
            raise PatchError(f"{fp.path}: hunk {number + 1} is outside the file")
        if hunk.trailing == 0 and end != total:
            raise PatchError(f"{fp.path}: hunk {number + 1} without trailing context is not at EOF")
        new_side = hunk.new_side()
        if new_side and not new_side[-1].endswith(b"\n") and end != total:
            raise PatchError(f"{fp.path}: hunk {number + 1} drops a newline before EOF")
        if source[start:end] != hunk.old_side():
            raise PatchError(f"{fp.path}: hunk {number + 1} does not match the file")
        out.extend(source[position:start])
        out.extend(new_side)
        position = end
    out.extend(source[position:])
    result = b"".join(out)
    if fp.is_deleted:
        if result:
            raise PatchError(f"{fp.path}: deletion leaves content behind")
        return None
    return result


def apply_all(
    patch_bytes: bytes, read_old: Callable[[str], bytes | None]
) -> dict[str, tuple[bytes | None, bytes | None]]:
    """Apply a whole patch; map every touched path to (before, after), None = absent.

    read_old(path) returns the file's content at the pinned commit, or None if it
    does not exist. A rename shows up as old path (content, None) and new path
    (None, content). Binary patches raise PatchError.
    """
    files = parse(patch_bytes)
    cache: dict[str, bytes | None] = {}

    def before(path: str) -> bytes | None:
        if path not in cache:
            content = read_old(path)
            cache[path] = None if content is None else bytes(content)
        return cache[path]

    vacated: set[str] = set()  # old paths whose content this patch consumes
    targets: set[str] = set()
    for fp in files:
        if fp.is_binary:
            raise PatchError(f"{fp.path}: binary patch cannot be applied")
        if fp.old_path is not None and not fp.is_copy:
            if fp.old_path in vacated:
                raise PatchError(f"{fp.old_path}: patched twice")
            vacated.add(fp.old_path)
        if fp.new_path is not None:
            if fp.new_path in targets:
                raise PatchError(f"{fp.new_path}: written twice")
            targets.add(fp.new_path)

    after: dict[str, bytes | None] = {}
    for fp in files:
        if fp.old_path is not None and fp.old_path != fp.new_path and not fp.is_copy:
            after[fp.old_path] = None
    for fp in files:
        old = None if fp.old_path is None else before(fp.old_path)
        if fp.old_path is not None and old is None:
            raise PatchError(f"{fp.old_path}: file to patch does not exist")
        new = apply(fp, old)
        if fp.new_path is None:
            continue
        if fp.new_path != fp.old_path and before(fp.new_path) is not None and fp.new_path not in vacated:
            raise PatchError(f"{fp.new_path}: already exists")
        after[fp.new_path] = new
    return {path: (before(path), content) for path, content in after.items()}


# ----------------------------------------------------------------------------- index check


def git_blob_id(data: bytes) -> str:
    """The git object id (SHA-1) of a blob with this content."""
    import hashlib  # lazy: only callers of the index check pay for the import

    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def index_matches(fp: FilePatch, old_bytes: bytes | None, new_bytes: bytes | None) -> bool | None:
    """Whether the contents agree with the patch's `index` line (None: no index line)."""
    if fp.old_index is None or fp.new_index is None:
        return None
    old_id = "0" * 40 if old_bytes is None or fp.is_new else git_blob_id(old_bytes)
    new_id = "0" * 40 if new_bytes is None or fp.is_deleted else git_blob_id(new_bytes)
    return old_id.startswith(fp.old_index) and new_id.startswith(fp.new_index)
