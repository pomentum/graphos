"""How a CSV file becomes graphos rows (``graphos.csv_rows``): no database."""

from __future__ import annotations

import pytest

from graphos import csv_rows
from graphos.csv_rows import Column, CsvError, Issues, cell_value, merge_rows, read_table


def _issue(issues: Issues, code: str) -> csv_rows.Issue | None:
    return next((i for i in issues.listed() if i.code == code), None)


def test_reads_quotes_line_breaks_crlf_and_a_byte_order_mark() -> None:
    data = '﻿id,name,note\r\na,"Ann, the first","two\nlines"\r\n\r\nb,"say ""hi""",\r\n'
    table = read_table(data.encode())
    assert table.delimiter == ","
    assert table.header == ["id", "name", "note"]
    assert table.rows == [["a", "Ann, the first", "two\nlines"], ["b", 'say "hi"', ""]]


@pytest.mark.parametrize(
    ("text", "delimiter"),
    [
        ("id;name\na;b\nc;d\n", ";"),
        ("id\tname\na\tb\n", "\t"),
        ('id,name\n"a;b;c",x\n', ","),
        ("single\nrow\n", ","),
    ],
)
def test_detects_the_separator_quoted_ones_aside(text: str, delimiter: str) -> None:
    assert csv_rows.detect_delimiter(text) == delimiter


def test_refuses_a_file_it_cannot_read() -> None:
    with pytest.raises(CsvError, match="not UTF-8"):
        read_table(b"id\n\xff\xfe\n")
    with pytest.raises(CsvError, match="empty"):
        read_table(b"\n\n")
    with pytest.raises(CsvError, match="never closed"):
        read_table(b'id,name\na,"open\nb,c\n')


def test_keeps_text_after_a_closing_quote_as_written() -> None:
    assert read_table(b'id,name\na,"x"y\n').rows == [["a", "xy"]]


@pytest.mark.parametrize(
    ("header", "name"),
    [
        ("name", "name"),
        (" First name ", "First_name"),
        ("Prénom", "Prenom"),
        ("2024 total (€)", "_2024_total"),
        ("---", "column_3"),
        ("x" * 80, "x" * 63),
    ],
)
def test_turns_a_header_into_a_property_name(header: str, name: str) -> None:
    assert csv_rows.property_name_of(header, 2) == name


def test_finds_the_id_columns_by_name_else_by_position() -> None:
    assert csv_rows.id_column(["name", "ID"], None) == 1
    assert csv_rows.id_column(["name", "other"], None) == 0
    assert csv_rows.id_column(["name", "Person Id"], "person_id") == 1
    assert csv_rows.end_columns(["Source ID", "x", "target_id"], None, None) == (0, 2)
    with pytest.raises(CsvError, match="no column"):
        csv_rows.id_column(["a"], "b")


def test_plans_columns_from_the_header_the_choices_and_the_definitions() -> None:
    header = ["id", "name", "age", "graphos_rank", "notes", "Name"]
    declared = {"age": {"type": "integer"}, "name": {"type": "string"}}
    plan = csv_rows.plan_columns(
        header, [0], {"notes": {"property": None}, "Name": {"property": "alias", "type": "text"}},
        declared, nodes=True,
    )
    assert plan == [
        Column(1, "name", "name", "text"),
        Column(2, "age", "age", "number"),
        Column(5, "Name", "alias", "text"),
    ]


@pytest.mark.parametrize(
    ("choices", "message"),
    [
        ({"nope": {"property": "x"}}, "not in the header"),
        ({"a": {"property": "graphos_x"}}, "reserved"),
        ({"a": {"property": "id"}}, "reserved"),
        ({"a": {"property": "bad name"}}, "not a property name"),
        ({"a": {"type": "date"}}, "not one of"),
        ({"a": {"property": "b"}}, "both fill"),
    ],
)
def test_refuses_a_mapping_graphos_cannot_write(choices: dict, message: str) -> None:
    with pytest.raises(CsvError, match=message):
        csv_rows.plan_columns(["id", "a", "b"], [0], choices, {}, nodes=True)


def test_an_edge_may_carry_an_id_property() -> None:
    plan = csv_rows.plan_columns(["src", "dst", "id"], [0, 1], {}, {}, nodes=False)
    assert plan == [Column(2, "id", "id", "auto")]


@pytest.mark.parametrize(
    ("text", "kind", "value"),
    [
        ("", "auto", None),
        ("42", "auto", 42),
        ("-1.5", "auto", -1.5),
        ("TRUE", "auto", True),
        ("007", "auto", "007"),
        ("1e3", "auto", "1e3"),
        ("9007199254740993", "auto", "9007199254740993"),
        ("'=1+2", "auto", "=1+2"),
        ("'plain", "text", "'plain"),
        (" 12 ", "number", 12),
        ("1e3", "number", 1000.0),
        ("0x10", "number", csv_rows._UNREADABLE),
        ("9007199254740993", "number", csv_rows._UNREADABLE),
        ("yes", "boolean", True),
        ("0", "boolean", False),
        ("maybe", "boolean", csv_rows._UNREADABLE),
    ],
)
def test_reads_a_cell_as_its_columns_type(
    text: str, kind: csv_rows.ColumnType, value: object,
) -> None:
    assert cell_value(text, kind) == value


def test_merges_rows_of_one_id_a_later_non_empty_cell_winning() -> None:
    table = read_table(
        b"id,name,age\n a ,Ann,30\nb,Bob,x\na,,31\n,Nobody,1\na,Annie,old\nb,Bob\n",
    )
    columns = [Column(1, "name", "name", "text"), Column(2, "age", "age", "number")]
    issues = Issues()
    records, keyless = merge_rows(table, [0], columns, issues, nodes=True)
    assert [(r.row, r.ids, r.properties) for r in records] == [
        (1, ("a",), {"name": "Annie", "age": 31}),
        (2, ("b",), {"name": "Bob"}),
    ]
    assert [(r.row, r.properties) for r in keyless] == [(4, {"name": "Nobody", "age": 1})]
    assert _issue(issues, "empty_id").count == 1  # type: ignore[union-attr]
    assert _issue(issues, "empty_id").refused  # type: ignore[union-attr]
    assert _issue(issues, "duplicate_id").count == 3  # type: ignore[union-attr]
    assert _issue(issues, "bad_value").examples[0] == {  # type: ignore[union-attr]
        "row": 2, "column": "age", "value": "x",
    }
    assert _issue(issues, "ragged").examples == [{"row": 6, "cells": 2}]  # type: ignore[union-attr]
    assert [i.refused for i in issues.listed()][0] is True
