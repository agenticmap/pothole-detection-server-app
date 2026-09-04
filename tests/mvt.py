"""A minimal Mapbox Vector Tile reader, for tests only.

MVT is protobuf, and the parts these tests care about — layer name, feature
count, attribute keys, extent — are shallow enough that a ~60-line varint reader
beats adding `mapbox-vector-tile` (which pulls in protobuf and shapely) just to
assert on tile contents. Asserting only on byte length would not catch a tile
that encodes the wrong layer or drops its attributes.

Vector tile schema (v2), fields used here:
    Tile.layers        = 3  (repeated Layer)
    Layer.name         = 1  (string)
    Layer.features     = 2  (repeated Feature)
    Layer.keys         = 3  (repeated string)
    Layer.values       = 4  (repeated Value)
    Layer.extent       = 5  (uint32)
    Layer.version      = 15 (uint32)

`values` is a per-layer deduplicated pool, not a per-feature mapping — features
reference it by index. That is enough to assert a tile actually CARRIES a value
(rather than merely declaring the key), which is what separates "the column is in
the SELECT" from "the column arrived with the right contents".
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

_WIRE_VARINT = 0
_WIRE_64BIT = 1
_WIRE_LENGTH_DELIMITED = 2
_WIRE_32BIT = 5


@dataclass
class Layer:
    name: str = ""
    extent: int = 0
    version: int = 0
    feature_count: int = 0
    keys: list[str] = field(default_factory=list)
    values: list[object] = field(default_factory=list)


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7


def _iter_fields(buf: bytes):
    """Yield (field_number, payload) for one protobuf message."""
    pos = 0
    while pos < len(buf):
        tag, pos = _read_varint(buf, pos)
        field_number, wire_type = tag >> 3, tag & 0x07
        if wire_type == _WIRE_VARINT:
            value, pos = _read_varint(buf, pos)
            yield field_number, value
        elif wire_type == _WIRE_LENGTH_DELIMITED:
            length, pos = _read_varint(buf, pos)
            yield field_number, buf[pos : pos + length]
            pos += length
        elif wire_type == _WIRE_64BIT:
            yield field_number, buf[pos : pos + 8]
            pos += 8
        elif wire_type == _WIRE_32BIT:
            yield field_number, buf[pos : pos + 4]
            pos += 4
        else:
            raise ValueError(f"Unsupported protobuf wire type {wire_type}")


def _parse_value(buf: bytes) -> object:
    """One Layer.values entry.

    Value is a union: exactly one field is set. Only the types PostGIS actually
    emits through ST_AsMVT are handled — strings, doubles, ints and bools — and an
    unrecognised one returns None rather than raising, so a new column type cannot
    break every tile assertion in the suite at once.
    """
    for field_number, payload in _iter_fields(buf):
        if field_number == 1:
            return payload.decode("utf-8")
        if field_number == 3:
            return struct.unpack("<d", payload)[0]
        if field_number in (4, 5):
            return payload
        if field_number == 6:
            # zigzag
            return (payload >> 1) ^ -(payload & 1)
        if field_number == 7:
            return bool(payload)
    return None


def _parse_layer(buf: bytes) -> Layer:
    layer = Layer()
    for field_number, payload in _iter_fields(buf):
        if field_number == 1:
            layer.name = payload.decode("utf-8")
        elif field_number == 2:
            layer.feature_count += 1
        elif field_number == 3:
            layer.keys.append(payload.decode("utf-8"))
        elif field_number == 4:
            layer.values.append(_parse_value(payload))
        elif field_number == 5:
            layer.extent = payload
        elif field_number == 15:
            layer.version = payload
    return layer


def decode(tile: bytes) -> list[Layer]:
    """Decode MVT bytes into layers. An empty tile decodes to []."""
    if not tile:
        return []
    return [_parse_layer(payload) for num, payload in _iter_fields(tile) if num == 3]


def layer_named(tile: bytes, name: str) -> Layer | None:
    for layer in decode(tile):
        if layer.name == name:
            return layer
    return None
