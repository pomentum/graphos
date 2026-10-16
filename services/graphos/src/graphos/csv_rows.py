"""CSV files as graphos rows: the reading and mapping behind ``nodes:csv`` /
``edges:csv`` (``graphos.api.csv_import``), with no database in it.

- **Reading** (RFC 4180, Python's :mod:`csv`): quoted cells with ``""`` for a
  quote and line breaks inside, CRLF or LF, a UTF-8 byte-order mark.  The
  separator is a comma, a semicolon or a tab: the one that splits the first
  lines most evenly.  The first row is the header; a blank line is no row.
  Rows are numbered from the first one under the header.
- **Mapping**: the id columns (a node's, a relationship's two ends), then
  every other column as a property: named after its header unless the
  mapping names it (or leaves it out), of a type: ``auto``, ``text``,
  ``number`` or ``boolean``.  A column's type defaults to the one its
  property's definition declares, else ``auto``.
- **Values**: an empty cell leaves the property out.  ``auto`` reads
  ``true`` / ``false`` (any case) and plain numbers (``42``, ``-1.5``; not
  ``007``, ``1e3`` or whole numbers past 2^53, which stay text), else text.
  ``number`` takes plain decimals; ``boolean`` also ``yes`` / ``no`` /
  ``1`` / ``0``.  A cell that does not read as its type is left out and
  reported.  A leading ``'`` before ``= + - @`` (graphos's own CSV export
  guards formulas so) is taken off.
- **Merging**: rows with the same id (a node) or the same two ends (a
  relationship) become one record, in the file's order: a later non-empty
  cell wins, an empty or unreadable one keeps the earlier value.  So what is
  written does not depend on how graphos batches the writes, and the
  property definitions check the record graphos receives.
"""

from __future__ import annotations

import csv
import io
import re
import unicodedata
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from graphos import reserved

ColumnType = Literal["auto", "text", "number", "boolean"]
COLUMN_TYPES: tuple[ColumnType, ...] = ("auto", "text", "number", "boolean")
DELIMITERS = {"comma": ",", "semicolon": ";", "tab": "\t"}

MAX_ID_LENGTH = 512
# Examples kept per kind of issue.
EXAMPLES = 3
_SAMPLE_ROWS = 20
_SAMPLE_CHARS = 64 * 1024
_PROPERTY_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
_DECIMAL = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$")
_AUTO_NUMBER = re.compile(r"^-?(?:0|[1-9]\d*)(?:\.\d+)?$")
_WHOLE = re.compile(r"^[+-]?\d+$")
_FORMULA_GUARD = re.compile(r"^'[=+\-@\t\r]")
_SAFE_INTEGER = 2**53 - 1
_ID_NAMES = ("id", "key", "identifier")
_SOURCE_NAMES = ("src", "srcid", "source", "sourceid", "from", "fromid", "start", "startid")
_TARGET_NAMES = ("dst", "dstid", "target", "targetid", "to", "toid", "end", "endid")

_UNREADABLE = object()
# A separator splits a line when it makes at least this many cells.
_MIN_CELLS = 2


class CsvError(ValueError):
    """A file or a mapping that cannot be read: answered 400 or 422."""


@dataclass(frozen=True)
class Column:
    """One property column: where it is, the property it fills, its type."""

    index: int
    header: str
    property: str
    type: ColumnType


@dataclass
class Issue:
    """One kind of problem in the file, counted, with a few examples."""

    code: str
    refused: bool
    count: int = 0
    examples: list[dict[str, Any]] = field(default_factory=list)


class Issues:
    """The issues of one file, in the order they were first met."""

    def __init__(self) -> None:
        self._by_code: dict[str, Issue] = {}

    def add(self, code: str, *, refused: bool, **example: Any) -> None:
        issue = self._by_code.setdefault(code, Issue(code, refused))
        issue.count += 1
        if len(issue.examples) < EXAMPLES:
            issue.examples.append(example)

    def listed(self) -> list[Issue]:
        """Refusals first, then what is written anyway."""
        return sorted(self._by_code.values(), key=lambda issue: not issue.refused)


@dataclass
class Record:
    """Rows with one key, merged: the first row's number, the ids, the properties."""

    row: int
    ids: tuple[str, ...]
    properties: dict[str, Any]


@dataclass
class Table:
    delimiter: str
    header: list[str]
    rows: list[list[str]]


def detect_delimiter(text: str) -> str:
    """The separator that splits the first lines most evenly: the most lines
    with as many cells as the first one (more than one), then the most cells;
    a comma when none splits them.  Quoted separators do not count."""
    sample = text[:_SAMPLE_CHARS]
    best = (",", 0, 1)
    for delimiter in DELIMITERS.values():
        rows = [row for row in _reader(sample, delimiter, lenient=True) if row][:_SAMPLE_ROWS]
        if not rows or len(rows[0]) < _MIN_CELLS:
            continue
        cells = len(rows[0])
        even = sum(1 for row in rows if len(row) == cells)
        if even > best[1] or (even == best[1] and cells > best[2]):
            best = (delimiter, even, cells)
    return best[0]


def _reader(text: str, delimiter: str, *, lenient: bool = False) -> Iterator[list[str]]:
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=False)
    try:
        yield from reader
    except csv.Error:
        if not lenient:
            raise


def _rows(text: str, delimiter: str) -> list[list[str]]:
    """The non-blank rows.  Read strictly first, which notices a quoted cell
    the file ends inside (the lenient reader would swallow the rest of the
    file into it); other slips, such as text after a closing quote, are then
    read leniently, kept as written."""
    rows: list[list[str]] = []
    strict = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)
    try:
        for row in strict:
            if row:
                rows.append(row)
    except csv.Error as exc:
        if "unexpected end of data" in str(exc):
            raise CsvError(
                f"a quoted cell is never closed: the file ends inside it (after row "
                f"{max(len(rows) - 1, 0)}, counted under the header)",
            ) from exc
        return [row for row in _reader(text, delimiter) if row]
    return rows


def read_table(data: bytes, delimiter: str | None = None) -> Table:
    """The file as a header and rows.

    Raises:
        CsvError: not UTF-8, no header, or a quoted cell never closed.
    """
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CsvError(f"the file is not UTF-8 text (byte {exc.start})") from exc
    chosen = delimiter or detect_delimiter(text)
    rows = _rows(text, chosen)
    if not rows:
        raise CsvError("the file is empty: it needs a header row")
    return Table(chosen, rows[0], rows[1:])


def property_name_of(header: str, index: int) -> str:
    """A header as a property name graphos accepts: ``First name`` →
    ``First_name``, ``2024`` → ``_2024``, nothing usable → ``column_<n>``."""
    trimmed = header.strip()
    if _PROPERTY_NAME.match(trimmed):
        return trimmed
    plain = unicodedata.normalize("NFD", trimmed)
    plain = "".join(c for c in plain if not unicodedata.combining(c))
    name = re.sub(r"[^A-Za-z0-9_]+", "_", plain).strip("_")
    if not name:
        return f"column_{index + 1}"
    return (f"_{name}" if name[0].isdigit() else name)[:63]


def is_reserved(name: str, *, nodes: bool) -> bool:
    """Names graphos keeps for itself; for nodes also ``id``, set from the id column."""
    return reserved.is_reserved(name) or (nodes and name == "id")


def find_column(
    header: Sequence[str], wanted: str | None, names: Sequence[str], fallback: int,
) -> int:
    """The column ``wanted`` names (exactly, else ignoring case and spaces);
    without one, the first whose header is one of ``names``, else ``fallback``.

    Raises:
        CsvError: ``wanted`` is not a header of the file.
    """
    def plain(title: str) -> str:
        return re.sub(r"[\s_-]+", "", title.strip().lower())

    if wanted is not None:
        if wanted in header:
            return list(header).index(wanted)
        for i, title in enumerate(header):
            if plain(title) == plain(wanted):
                return i
        raise CsvError(f"no column {wanted!r}; the header is {list(header)}")
    for i, title in enumerate(header):
        if plain(title) in names:
            return i
    return min(fallback, len(header) - 1)


def id_column(header: Sequence[str], wanted: str | None) -> int:
    return find_column(header, wanted, _ID_NAMES, 0)


def end_columns(header: Sequence[str], src: str | None, dst: str | None) -> tuple[int, int]:
    return (find_column(header, src, _SOURCE_NAMES, 0), find_column(header, dst, _TARGET_NAMES, 1))


def type_for(definition: Mapping[str, Any] | None) -> ColumnType:
    """The column type a declared property suggests; ``auto`` otherwise."""
    kind = (definition or {}).get("type")
    if kind == "string":
        return "text"
    if kind in ("integer", "number"):
        return "number"
    if kind == "boolean":
        return "boolean"
    return "auto"


def plan_columns(
    header: Sequence[str],
    keys: Sequence[int],
    choices: Mapping[str, Mapping[str, Any]],
    declared: Mapping[str, Mapping[str, Any]],
    *,
    nodes: bool,
) -> list[Column]:
    """Every column but the id ones, as the property it fills.

    ``choices`` maps a header to ``{"property": name | None, "type": ...}``
    (``None`` leaves the column out); without a choice a column is named
    after its header (left out when that name is reserved) and typed after
    its definition in ``declared`` (property → definition).

    Raises:
        CsvError: a choice names a header the file lacks, a type graphos does
            not know, a name it refuses, or two columns fill one property.
    """
    unknown = set(choices) - set(header)
    if unknown:
        raise CsvError(f"columns {sorted(unknown)} are not in the header {list(header)}")
    columns: list[Column] = []
    used: dict[str, str] = {}
    for index, title in enumerate(header):
        if index in keys:
            continue
        choice = choices.get(title, {})
        suggested = property_name_of(title, index)
        if "property" in choice:
            name = choice["property"]
            if name is None:
                continue
            if not isinstance(name, str) or not _PROPERTY_NAME.match(name):
                raise CsvError(f"column {title!r}: {name!r} is not a property name")
            if is_reserved(name, nodes=nodes):
                raise CsvError(f"column {title!r}: {reserved.refusal([name])}")
        elif is_reserved(suggested, nodes=nodes):
            continue
        else:
            name = suggested
        kind = choice.get("type") or type_for(declared.get(name))
        if kind not in COLUMN_TYPES:
            raise CsvError(f"column {title!r}: type {kind!r} is not one of {list(COLUMN_TYPES)}")
        if name in used:
            raise CsvError(f"columns {used[name]!r} and {title!r} both fill {name!r}")
        used[name] = title
        columns.append(Column(index, title, name, kind))
    return columns


def unguarded(text: str) -> str:
    """A cell as written before graphos's CSV export guarded it against formulas."""
    return text[1:] if _FORMULA_GUARD.match(text) else text


def _number(text: str, pattern: re.Pattern[str]) -> Any:
    if not pattern.match(text):
        return _UNREADABLE
    if _WHOLE.match(text):
        value = int(text)
        return value if abs(value) <= _SAFE_INTEGER else _UNREADABLE
    number = float(text)
    return number if number not in (float("inf"), float("-inf")) else _UNREADABLE


def cell_value(text: str, kind: ColumnType) -> Any:  # noqa: PLR0911 — one return per type
    """A cell as a value of its column's type: ``None`` when empty,
    ``_UNREADABLE`` when it does not read as the type."""
    if text == "":
        return None
    cell = unguarded(text)
    if kind == "text":
        return cell
    if kind == "auto":
        word = cell.lower()
        if word in ("true", "false"):
            return word == "true"
        value = _number(cell, _AUTO_NUMBER)
        return cell if value is _UNREADABLE else value
    if kind == "number":
        return _number(cell.strip(), _DECIMAL)
    word = cell.strip().lower()
    if word in ("true", "yes", "1"):
        return True
    if word in ("false", "no", "0"):
        return False
    return _UNREADABLE


def id_of(row: Sequence[str], index: int) -> str:
    """A row's id in a column, without the spaces around it."""
    return unguarded(row[index] if index < len(row) else "").strip()


def merge_rows(
    table: Table, keys: Sequence[int], columns: Sequence[Column], issues: Issues, *, nodes: bool,
) -> tuple[list[Record], list[Record]]:
    """The rows as records, merged by key; and the rows without a whole key
    (an empty id), each a record of its own, which graphos refuses.

    Issues noted: rows of another length than the header, empty and too long
    ids, repeated keys (merged), cells that do not read as their type.
    """
    merged: dict[tuple[str, ...], Record] = {}
    keyless: list[Record] = []
    width = len(table.header)
    for number, row in enumerate(table.rows, start=1):
        if len(row) != width:
            issues.add("ragged", refused=False, row=number, cells=len(row))
        ids = tuple(id_of(row, index) for index in keys)
        for index, value in zip(keys, ids, strict=True):
            if value == "":
                issues.add("empty_id", refused=True, row=number, column=table.header[index])
            elif len(value) > MAX_ID_LENGTH:
                issues.add("long_id", refused=True, row=number, column=table.header[index])
        properties: dict[str, Any] = {}
        for column in columns:
            text = row[column.index] if column.index < len(row) else ""
            value = cell_value(text, column.type)
            if value is _UNREADABLE:
                issues.add("bad_value", refused=False, row=number, column=column.header, value=text)
            elif value is not None:
                properties[column.property] = value
        if not all(ids):
            keyless.append(Record(number, ids, properties))
            continue
        record = merged.get(ids)
        if record is None:
            merged[ids] = Record(number, ids, properties)
        else:
            issues.add(
                "duplicate_id" if nodes else "duplicate_pair", refused=False,
                row=number, value=" → ".join(ids),
            )
            record.properties.update(properties)
    return list(merged.values()), keyless
