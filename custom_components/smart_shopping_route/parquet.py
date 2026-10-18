"""Minimal, dependency-free Parquet reader for the grocer.nz files.

grocer.nz publishes its data as Parquet, which normally needs a compiled reader
(duckdb, pyarrow). Those have no wheels for Home Assistant OS, which is Alpine
(musl): duckdb publishes no musllinux wheels at all, so pip falls back to a
source build and fails, and a failed requirement stops Home Assistant loading the
integration entirely. Making one optional feature able to break the whole
integration is not acceptable.

So this module reads exactly the shapes grocer.nz publishes, using only the
standard library. Python 3.14 ships Zstandard (PEP 784), which is what the
history files use; the per-store files use gzip.

Deliberately supported: flat schemas, PLAIN and RLE_DICTIONARY encodings, gzip
and Zstandard compression, and the INT32/INT64/DOUBLE/BYTE_ARRAY physical types.
BYTE_ARRAY values come back as ``bytes``. Anything else raises ``ParquetError``
rather than guessing, and callers fall back to no history: both grocer.nz file
types are covered, and a format change degrades to "no history" rather than
breaking the integration.
"""

from __future__ import annotations

import gzip
import struct
from dataclasses import dataclass
from typing import Any

try:  # Python 3.14+
    from compression import zstd as _zstd
except ImportError:  # pragma: no cover - only on older interpreters
    _zstd = None

PARQUET_MAGIC = b"PAR1"
# Parquet physical types. Named _P_* so they cannot collide with the thrift
# type ids below, which reuse the same names for a different meaning.
_P_INT32, _P_INT64, _P_DOUBLE, _P_BYTE_ARRAY = 1, 2, 5, 6
# Encodings.
_ENC_PLAIN, _ENC_PLAIN_DICT, _ENC_RLE, _ENC_RLE_DICT = 0, 2, 3, 8
# Page types.
_PAGE_DATA, _PAGE_INDEX, _PAGE_DICT = 0, 1, 2
# Compression codecs.
_C_UNCOMPRESSED, _C_GZIP, _C_ZSTD = 0, 2, 6


class ParquetError(Exception):
    """Raised when a file is not in a shape this reader supports."""


# --- Thrift compact protocol -------------------------------------------------
# Parquet metadata is Thrift-encoded. Only the wire format is needed, not the
# schema, so this walks the stream generically.

_T_STOP, _T_TRUE, _T_FALSE = 0, 1, 2
_T_BYTE, _T_I16, _T_I32, _T_I64, _T_DOUBLE, _T_BINARY = 3, 4, 5, 6, 7, 8
_T_LIST, _T_SET, _T_MAP, _T_STRUCT = 9, 10, 11, 12


class _Compact:
    """A cursor over a Thrift compact-protocol buffer."""

    def __init__(self, data: bytes, pos: int = 0) -> None:
        self.data = data
        self.pos = pos

    def _byte(self) -> int:
        if self.pos >= len(self.data):
            raise ParquetError("truncated thrift data")
        value = self.data[self.pos]
        self.pos += 1
        return value

    def _varint(self) -> int:
        result = 0
        shift = 0
        while True:
            byte = self._byte()
            result |= (byte & 0x7F) << shift
            if not byte & 0x80:
                return result
            shift += 7
            if shift > 70:
                raise ParquetError("varint too long")

    def _zigzag(self) -> int:
        raw = self._varint()
        return (raw >> 1) ^ -(raw & 1)

    def _binary(self) -> bytes:
        length = self._varint()
        end = self.pos + length
        if end > len(self.data):
            raise ParquetError("truncated thrift binary")
        value = self.data[self.pos : end]
        self.pos = end
        return value

    def read_struct(self) -> dict[int, Any]:
        """Read one struct as ``{field_id: value}``, skipping unknown fields."""

        fields: dict[int, Any] = {}
        last_id = 0
        while True:
            header = self._byte()
            field_type = header & 0x0F
            if field_type == _T_STOP:
                return fields
            delta = header >> 4
            field_id = last_id + delta if delta else self._zigzag()
            last_id = field_id
            fields[field_id] = self._read_value(field_type)

    def _read_value(self, field_type: int) -> Any:
        if field_type == _T_TRUE:
            return True
        if field_type == _T_FALSE:
            return False
        if field_type == _T_BYTE:
            return self._byte()
        if field_type in (_T_I16, _T_I32, _T_I64):
            return self._zigzag()
        if field_type == _T_DOUBLE:
            value = struct.unpack_from("<d", self.data, self.pos)[0]
            self.pos += 8
            return value
        if field_type == _T_BINARY:
            return self._binary()
        if field_type in (_T_LIST, _T_SET):
            header = self._byte()
            size = header >> 4
            element_type = header & 0x0F
            if size == 0x0F:
                size = self._varint()
            return [self._read_value(element_type) for _ in range(size)]
        if field_type == _T_MAP:
            size = self._varint()
            if size == 0:
                return {}
            types = self._byte()
            key_type, value_type = types >> 4, types & 0x0F
            return {
                self._read_value(key_type): self._read_value(value_type)
                for _ in range(size)
            }
        if field_type == _T_STRUCT:
            return self.read_struct()
        raise ParquetError(f"unsupported thrift type {field_type}")


# --- file structures --------------------------------------------------------


@dataclass(slots=True)
class ColumnChunk:
    """Where one column's pages live, and how they are encoded."""

    name: str
    physical_type: int
    codec: int
    data_offset: int
    dictionary_offset: int | None
    num_values: int
    max_definition_level: int = 0


@dataclass(slots=True)
class _SchemaElement:
    name: str
    physical_type: int | None
    converted_type: int | None
    logical_type: dict | None
    repetition_type: int | None


def _decompress(codec: int, payload: bytes) -> bytes:
    if codec == _C_UNCOMPRESSED:
        return payload
    if codec == _C_GZIP:
        return gzip.decompress(payload)
    if codec == _C_ZSTD:
        if _zstd is None:
            raise ParquetError("zstd support requires Python 3.14 or newer")
        return _zstd.decompress(payload)
    raise ParquetError(
        f"unsupported compression codec {codec}"
        " (grocer.nz uses gzip and zstd; callers fall back to no history)"
    )


def _decode_plain(payload: bytes, count: int, physical_type: int) -> list[Any]:
    """Decode PLAIN-encoded values."""

    if physical_type == _P_INT32:
        return list(struct.unpack_from(f"<{count}i", payload, 0))
    if physical_type == _P_INT64:
        return list(struct.unpack_from(f"<{count}q", payload, 0))
    if physical_type == _P_DOUBLE:
        return list(struct.unpack_from(f"<{count}d", payload, 0))
    if physical_type == _P_BYTE_ARRAY:
        values: list[bytes] = []
        pos = 0
        for _ in range(count):
            length = struct.unpack_from("<i", payload, pos)[0]
            pos += 4
            values.append(payload[pos : pos + length])
            pos += length
        return values
    raise ParquetError(f"unsupported physical type {physical_type}")


def _decode_rle_bitpacked(payload: bytes, bit_width: int, count: int) -> list[int]:
    """Decode the RLE/bit-packing hybrid used for dictionary indices."""

    values: list[int] = []
    pos = 0
    byte_width = (bit_width + 7) // 8
    while len(values) < count and pos < len(payload):
        header = 0
        shift = 0
        while True:
            byte = payload[pos]
            pos += 1
            header |= (byte & 0x7F) << shift
            if not byte & 0x80:
                break
            shift += 7
        if header & 1:
            groups = header >> 1
            total = groups * 8
            chunk = payload[pos : pos + groups * bit_width]
            pos += groups * bit_width
            bits = int.from_bytes(chunk, "little")
            for index in range(total):
                values.append((bits >> (index * bit_width)) & ((1 << bit_width) - 1))
        else:
            run = header >> 1
            value = int.from_bytes(payload[pos : pos + byte_width], "little")
            pos += byte_width
            values.extend([value] * run)
    if len(values) < count:
        raise ParquetError("RLE data ended early")
    return values[:count]


def _definition_levels(
    payload: bytes, count: int, max_level: int
) -> tuple[list[int], int]:
    """Decode a definition-level section, returning ``(levels, bytes_used)``.

    Data page V1 stores levels as a 4-byte length followed by RLE/bit-packed
    data whose bit width comes from the maximum level.
    """

    if len(payload) < 4:
        raise ParquetError("definition levels missing")
    length = struct.unpack_from("<I", payload, 0)[0]
    section = payload[4 : 4 + length]
    bit_width = max(1, max_level.bit_length())
    levels = _decode_rle_bitpacked(section, bit_width, count)
    return levels, 4 + length


def _read_pages(data: bytes, chunk: ColumnChunk) -> tuple[list[Any] | None, list[Any]]:
    """Return ``(dictionary, values)`` for one column chunk.

    ``values`` has one entry per row; entries that were null in the file become
    ``None``.
    """

    dictionary: list[Any] | None = None
    values: list[Any] = []
    start = (
        chunk.dictionary_offset
        if chunk.dictionary_offset is not None
        else chunk.data_offset
    )
    pos = start
    while len(values) < chunk.num_values and pos < len(data):
        page_header = _Compact(data, pos)
        header = page_header.read_struct()
        pos = page_header.pos
        # PageHeader: 1 type, 2 uncompressed size, 3 compressed size, 5 data page
        # header, 7 dictionary page header.
        page_type = header.get(1)
        compressed_size = header.get(3)
        if compressed_size is None:
            raise ParquetError("page header without a size")
        payload = _decompress(chunk.codec, data[pos : pos + compressed_size])
        pos += compressed_size

        if page_type == _PAGE_DICT:
            dict_header = header.get(7) or {}
            count = dict_header.get(1, 0)
            dictionary = _decode_plain(payload, count, chunk.physical_type)
            continue
        if page_type != _PAGE_DATA:
            # Index pages carry no values.
            continue
        data_header = header.get(5) or {}
        num_values = data_header.get(1, 0)
        encoding = data_header.get(2, _ENC_PLAIN)

        body = payload
        levels: list[int] | None = None
        if chunk.max_definition_level > 0:
            levels, used = _definition_levels(
                payload, num_values, chunk.max_definition_level
            )
            body = payload[used:]
        present = (
            num_values
            if levels is None
            else sum(1 for level in levels if level == chunk.max_definition_level)
        )

        if encoding == _ENC_PLAIN:
            decoded = _decode_plain(body, present, chunk.physical_type)
        elif encoding == _ENC_RLE_DICT or encoding == _ENC_PLAIN_DICT:
            if dictionary is None:
                raise ParquetError("dictionary page missing")
            bit_width = body[0]
            indices = _decode_rle_bitpacked(body[1:], bit_width, present)
            decoded = [dictionary[index] for index in indices]
        elif encoding == _ENC_RLE:
            # Only level streams use plain RLE in these files.
            continue
        else:
            raise ParquetError(f"unsupported value encoding {encoding}")

        if levels is None:
            values.extend(decoded)
        else:
            position = 0
            for level in levels:
                if level == chunk.max_definition_level:
                    values.append(decoded[position])
                    position += 1
                else:
                    values.append(None)
    return dictionary, values


def _footer_metadata(data: bytes) -> dict[int, Any]:
    if len(data) < 12 or data[:4] != PARQUET_MAGIC or data[-4:] != PARQUET_MAGIC:
        raise ParquetError("not a parquet file")
    metadata_len = struct.unpack_from("<I", data, len(data) - 8)[0]
    start = len(data) - 8 - metadata_len
    if start < 4:
        raise ParquetError("parquet metadata length out of range")
    return _Compact(data, start).read_struct()


def _column_chunks(metadata: dict[int, Any]) -> list[ColumnChunk]:
    """Flatten the row groups into one list of column chunks.

    Field ids follow parquet.thrift: FileMetaData schema is 2 and row_groups is
    4; SchemaElement is ``{1 type, 4 name, 5 num_children}``; RowGroup columns is
    1; ColumnChunk meta_data is 3; ColumnMetaData is ``{1 type, 3 path_in_schema,
    4 codec, 5 num_values, 9 data_page_offset, 11 dictionary_page_offset}``.
    """

    schema_elements = metadata.get(2) or []
    # The first element describes the root group; leaves follow in order.
    leaves: list[_SchemaElement] = []
    for element in schema_elements[1:]:
        if element.get(5):  # num_children means a group, not a leaf
            continue
        leaves.append(
            _SchemaElement(
                name=(element.get(4) or b"").decode("utf-8", "replace"),
                physical_type=element.get(1),
                converted_type=element.get(6),
                logical_type=element.get(10),
                repetition_type=element.get(3),
            )
        )

    chunks: list[ColumnChunk] = []
    for row_group in metadata.get(4) or []:
        columns = row_group.get(1) or []
        for index, column in enumerate(columns):
            meta = column.get(3) or {}
            if index >= len(leaves):
                raise ParquetError("column count does not match the schema")
            leaf = leaves[index]
            # path_in_schema names the column directly; it is authoritative.
            path = meta.get(3) or []
            name = path[0].decode("utf-8", "replace") if path else leaf.name
            chunks.append(
                ColumnChunk(
                    name=name,
                    physical_type=meta.get(1) or leaf.physical_type or 0,
                    codec=meta.get(4, _C_UNCOMPRESSED),
                    data_offset=meta.get(9, 0),
                    dictionary_offset=meta.get(11),
                    num_values=meta.get(5, 0),
                    # A flat optional column needs one definition level; a
                    # required one needs none. These files are flat, so this is
                    # the whole tree depth.
                    max_definition_level=(
                        1 if leaf.repetition_type in (None, 1) else 0
                    ),
                )
            )
    return chunks


def read_columns(path: str, wanted: list[str]) -> dict[str, list[Any]]:
    """Read the named columns from a flat parquet file.

    Only the columns asked for are decoded, which matters because the history
    files are large.
    """

    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError as err:
        raise ParquetError("parquet file could not be read") from err

    metadata = _footer_metadata(data)

    result: dict[str, list[Any]] = {}
    for chunk in _column_chunks(metadata):
        if chunk.name not in wanted:
            continue
        _dictionary, values = _read_pages(data, chunk)
        result[chunk.name] = values
    missing = [name for name in wanted if name not in result]
    if missing:
        raise ParquetError(f"columns not found: {', '.join(missing)}")
    return result
