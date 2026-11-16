"""Physical CSV ingestion for real flight legs.

Every physical line is kept verbatim so a correction can always be traced back
to the bytes that were imported.  Header detection is explicit; embedded
newlines inside quoted fields are not supported, because the ledger's unit of
evidence is the physical line and its 1-based ``line_no``.
"""

import csv
import io

DEFAULT_COLUMNS = (
    "flight_no",
    "flight_date",
    "origin",
    "destination",
    "departure_local",
    "arrival_local",
    "departure_fold",
    "arrival_fold",
)

class CsvError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

def decode(payload: bytes) -> str:
    if not payload:
        raise CsvError("EMPTY_INPUT", "导入内容为空")
    try:
        return payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CsvError("BAD_ENCODING", f"无法按 UTF-8 解码: {exc}") from exc

def split_fields(line: str) -> list[str]:
    try:
        return next(csv.reader([line]))
    except csv.Error as exc:
        raise CsvError("BAD_FORMAT", f"CSV 字段无法解析: {exc}") from exc

def parse_csv(payload: bytes) -> dict:
    """Return raw records without judging their business content.

    ``truncated_tail`` is true when the file does not end with a newline, which
    is how TRAP-03 (half a leg at the tail) shows up in practice.  The last
    record is still returned so its raw line can be archived and quarantined.
    """
    text = decode(payload)
    truncated_tail = not text.endswith("\n")
    lines = text.splitlines(keepends=True)
    if not lines:
        raise CsvError("EMPTY_INPUT", "导入内容为空")

    records: list[dict] = []
    columns: tuple[str, ...] | None = None
    for line_no, physical in enumerate(lines, 1):
        raw_line = physical.rstrip("\r\n")
        if not raw_line.strip():
            continue
        is_last = line_no == len(lines)
        truncated = truncated_tail and is_last
        try:
            fields = split_fields(raw_line)
        except CsvError as exc:
            records.append({"line_no": line_no, "raw_line": raw_line, "values": {}, "parse_error": exc.code, "truncated": truncated})
            continue
        if columns is None:
            normalized = [field.strip().lower() for field in fields]
            if "flight_no" in normalized:
                columns = tuple(normalized)
                continue
            columns = DEFAULT_COLUMNS
        values: dict[str, str] = {}
        error = None
        if len(fields) > len(columns):
            error = "BAD_FORMAT"
        for name, value in zip(columns, fields):
            values[name] = value.strip()
        if truncated:
            error = "TRUNCATED_INPUT"
        records.append({"line_no": line_no, "raw_line": raw_line, "values": values, "parse_error": error, "truncated": truncated})

    if not records:
        raise CsvError("EMPTY_INPUT", "导入内容没有数据行")
    return {"filename": None, "columns": list(columns or DEFAULT_COLUMNS), "records": records, "truncated_tail": truncated_tail}
