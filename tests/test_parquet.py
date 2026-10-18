"""The built-in Parquet reader.

Validated against pyarrow where it is available, and against hand-built byte
streams where it is not, because this reader is what makes grocer.nz history
work without a compiled dependency.
"""

import gzip
import struct

import pytest

from smart_shopping_route.parquet import (
    PARQUET_MAGIC,
    ParquetError,
    _Compact,
    _decode_plain,
    _decode_rle_bitpacked,
    _decompress,
    read_columns,
)

# --- thrift compact protocol ------------------------------------------------


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def test_thrift_reads_a_simple_struct() -> None:
    # field 1 (i32) = 42 as a struct with a short field-id delta, then STOP.
    payload = bytes([(1 << 4) | 0x05]) + _varint(42 << 1) + bytes([0x00])

    assert _Compact(payload).read_struct() == {1: 42}


def test_thrift_reads_binary_and_lists() -> None:
    text = b"hello"
    # field 1 binary "hello"
    payload = bytes([(1 << 4) | 0x08]) + _varint(len(text)) + text
    # field 2 list of 3 i32
    payload += bytes([(1 << 4) | 0x09, (3 << 4) | 0x05])
    payload += b"".join(_varint(v << 1) for v in (1, 2, 3))
    payload += bytes([0x00])

    result = _Compact(payload).read_struct()

    assert result[1] == b"hello"
    assert result[2] == [1, 2, 3]


def test_thrift_skips_unknown_types_by_raising() -> None:
    """An unsupported type must fail loudly rather than misparse."""

    payload = bytes([(1 << 4) | 0x0F])

    with pytest.raises(ParquetError):
        _Compact(payload).read_struct()


def test_thrift_rejects_truncated_data() -> None:
    with pytest.raises(ParquetError):
        _Compact(b"").read_struct()


# --- value decoders ---------------------------------------------------------


def test_plain_decoding() -> None:
    assert _decode_plain(struct.pack("<3i", 1, 2, 3), 3, 1) == [1, 2, 3]
    assert _decode_plain(struct.pack("<2q", 10, 20), 2, 2) == [10, 20]
    assert _decode_plain(struct.pack("<2d", 1.5, 2.5), 2, 5) == [1.5, 2.5]
    raw = struct.pack("<i", 3) + b"abc" + struct.pack("<i", 2) + b"de"
    assert _decode_plain(raw, 2, 6) == [b"abc", b"de"]


def test_plain_rejects_an_unknown_physical_type() -> None:
    with pytest.raises(ParquetError):
        _decode_plain(b"", 0, 99)


def test_rle_run_decoding() -> None:
    # header: not bit-packed, run length 4 -> value 7 as one byte at width 8
    payload = _varint(4 << 1) + bytes([7])

    assert _decode_rle_bitpacked(payload, 8, 4) == [7, 7, 7, 7]


def test_rle_bit_packed_decoding() -> None:
    # header: bit-packed with 1 group of 8 values, width 1 -> 0b10101010
    payload = _varint((1 << 1) | 1) + bytes([0b10101010])

    assert _decode_rle_bitpacked(payload, 1, 8) == [0, 1, 0, 1, 0, 1, 0, 1]


def test_rle_reports_truncation() -> None:
    with pytest.raises(ParquetError):
        _decode_rle_bitpacked(_varint(2 << 1) + bytes([1]), 8, 10)


# --- compression ------------------------------------------------------------


def test_gzip_and_uncompressed_paths() -> None:
    assert _decompress(0, b"plain") == b"plain"
    assert _decompress(2, gzip.compress(b"hello")) == b"hello"


def test_zstd_uses_the_standard_library() -> None:
    zstd = pytest.importorskip("compression.zstd")
    payload = zstd.compress(b"hello zstd")

    assert _decompress(6, payload) == b"hello zstd"


def test_unknown_codec_is_rejected() -> None:
    """Snappy and others are not used by grocer.nz; degrade rather than guess."""

    with pytest.raises(ParquetError, match="unsupported compression codec"):
        _decompress(1, b"")
    with pytest.raises(ParquetError):
        _decompress(99, b"")


# --- file handling ----------------------------------------------------------


def test_non_parquet_file_is_rejected(tmp_path) -> None:
    path = tmp_path / "not.parquet"
    path.write_bytes(b"definitely not parquet")

    with pytest.raises(ParquetError):
        read_columns(str(path), ["a"])


def test_missing_file_is_reported(tmp_path) -> None:
    with pytest.raises(ParquetError):
        read_columns(str(tmp_path / "nope.parquet"), ["a"])


def test_missing_column_is_reported(tmp_path) -> None:
    pa = pytest.importorskip("pyarrow")
    import pyarrow.parquet as pq

    path = tmp_path / "t.parquet"
    pq.write_table(pa.table({"a": [1, 2, 3]}), path)

    with pytest.raises(ParquetError):
        read_columns(str(path), ["b"])


# --- validation against pyarrow --------------------------------------------

CASES = [
    # (column definitions, rows, compression)
    ({"id": "int32", "price": "int64"}, [(1, 10), (2, 20), (3, 30)], "gzip"),
    ({"id": "int32", "price": "int64"}, [(i, i * 2) for i in range(1000)], "zstd"),
    ({"id": "int32", "price": "int64"}, [(i, i) for i in range(100)], "none"),
    ({"name": "string"}, [("alpha",), ("beta",), ("gamma",)], "gzip"),
]


@pytest.mark.parametrize(("fields", "rows", "compression"), CASES)
def test_reads_match_pyarrow(tmp_path, fields, rows, compression) -> None:
    """Byte-for-byte agreement with a reference implementation."""

    pa = pytest.importorskip("pyarrow")
    import pyarrow.parquet as pq

    path = tmp_path / "t.parquet"
    columns = {
        name: pa.array([row[index] for row in rows], type=pa.type_for_alias(kind))
        for index, (name, kind) in enumerate(fields.items())
    }
    pq.write_table(
        pa.table(columns),
        path,
        compression=None if compression == "none" else compression,
    )

    mine = read_columns(str(path), list(fields))

    for name in fields:
        expected = pq.read_table(path, columns=[name]).column(name).to_pylist()
        if fields[name] == "string":
            expected = [value.encode() for value in expected]
        assert mine[name] == expected, name


def test_nullable_columns_keep_nulls(tmp_path) -> None:
    """grocer.nz store files are all OPTIONAL, so nulls must survive."""

    pa = pytest.importorskip("pyarrow")
    import pyarrow.parquet as pq

    path = tmp_path / "t.parquet"
    pq.write_table(
        pa.table({
            "id": pa.array([1, 2, 3], type=pa.int32()),
            "sale": pa.array([None, 199, None], type=pa.int32()),
        }),
        path,
        compression="gzip",
    )

    mine = read_columns(str(path), ["id", "sale"])

    assert mine["id"] == [1, 2, 3]
    assert mine["sale"] == [None, 199, None]


def test_dictionary_encoded_strings(tmp_path) -> None:
    pa = pytest.importorskip("pyarrow")
    import pyarrow.parquet as pq

    path = tmp_path / "t.parquet"
    pq.write_table(
        pa.table({"name": ["repeat", "repeat", "other", "repeat"]}),
        path,
        use_dictionary=True,
        compression="gzip",
    )

    # BYTE_ARRAY is returned as bytes; decoding is the caller's business.
    assert read_columns(str(path), ["name"])["name"] == [
        b"repeat",
        b"repeat",
        b"other",
        b"repeat",
    ]


def test_only_requested_columns_are_decoded(tmp_path) -> None:
    """History files are large, so unrequested columns must be skipped."""

    pa = pytest.importorskip("pyarrow")
    import pyarrow.parquet as pq

    path = tmp_path / "t.parquet"
    pq.write_table(
        pa.table({"a": [1, 2], "b": ["x", "y"], "c": [1.5, 2.5]}),
        path,
        compression="gzip",
    )

    result = read_columns(str(path), ["a"])

    assert list(result) == ["a"]
    assert result["a"] == [1, 2]


def test_real_parquet_magic_is_checked(tmp_path) -> None:
    pa = pytest.importorskip("pyarrow")
    import pyarrow.parquet as pq

    path = tmp_path / "t.parquet"
    pq.write_table(pa.table({"a": [1]}), path)

    assert path.read_bytes()[:4] == PARQUET_MAGIC


# --- a real file, embedded --------------------------------------------------
# CI does not install pyarrow, so the read path would otherwise be untested
# there. This is a real Parquet file written once by pyarrow: two INT32 columns,
# gzip, dictionary-encoded, with nulls. Embedding it keeps the test independent
# of pyarrow while still exercising the true read path.

REAL_PARQUET_B64 = """
UEFSMRUEFSAVPkwVCBUAEgAAH4sIAAAAAAACA2NkYGBgAmJmIGYBYgDv1AWvEAAAABUAFRQVPCwVCBUQFQYVBhwYBAQAAAAYBAEAAAAWACgEBAAAABgEAQAAABERAAAAH4sIAAAAAAACA2NiYGDgYGRifsIAAKxTEaQKAAAAFQQVEBU4TBUEFQASAAAfiwgAAAAAAAIDO87AwBDLyMAAAA8s5C0IAAAAFQAVEhU6LBUIFRAVBhUGHBgEXQEAABgExwAAABYEKARdAQAAGATHAAAAEREAAAAfiwgAAAAAAAIDY2JgYGBmZWRmAgAS7mB8CQAAABUEGTw1ABgGc2NoZW1hFQQAFQIlAhgCaWQAFQIlAhgFcHJpY2UAFggZHBksJgAcFQIZNQAGEBkYAmlkFQQWCBauARb0ASZiJggcGAQEAAAAGAQBAAAAFgAoBAQAAAAYBAEAAAAREQAZLBUEFQAVAgAVABUQFQIAPCkGGSYACAAAACYAHBUCGTUABhAZGAVwcmljZRUEFggWnAEW7AEm0AIm/AEcGARdAQAAGATHAAAAFgQoBF0BAAAYBMcAAAAREQAZLBUEFQAVAgAVABUQFQIAPCkGGSYEBAAAABbKAhYIJggW4AMAGRwYDEFSUk9XOnNjaGVtYRj4AS8vLy8vN0FBQUFBUUFBQUFBQUFLQUF3QUJnQUZBQWdBQ2dBQUFBQUJCQUFNQUFBQUNBQUlBQUFBQkFBSUFBQUFCQUFBQUFJQUFBQklBQUFBQkFBQUFORC8vLzhBQUFFQ0VBQUFBQmdBQUFBRUFBQUFBQUFBQUFVQUFBQndjbWxqWlFBQUFNVC8vLzhBQUFBQklBQUFBQkFBRkFBSUFBWUFCd0FNQUFBQUVBQVFBQUFBQUFBQkFoQUFBQUFjQUFBQUJBQUFBQUFBQUFBQ0FBQUFhV1FBQUFnQURBQUlBQWNBQ0FBQUFBQUFBQUVnQUFBQUFBQUFBQT09ABggcGFycXVldC1jcHAtYXJyb3cgdmVyc2lvbiAyNi4wLjAZLBwAABwAAAAdAgAAUEFSMQ==
"""


@pytest.fixture
def real_parquet(tmp_path):
    import base64

    path = tmp_path / "real.parquet"
    path.write_bytes(base64.b64decode(REAL_PARQUET_B64))
    return path


def test_reads_a_real_file_without_pyarrow(real_parquet) -> None:
    """Covers decoding, gzip, dictionary indices, and nulls in CI."""

    result = read_columns(str(real_parquet), ["id", "price"])

    assert result["id"] == [1, 2, 3, 4]
    assert result["price"] == [199, None, 349, None]


def test_real_file_reports_missing_columns(real_parquet) -> None:
    with pytest.raises(ParquetError):
        read_columns(str(real_parquet), ["other"])


def test_real_file_can_read_one_column(real_parquet) -> None:
    assert list(read_columns(str(real_parquet), ["id"])) == ["id"]
