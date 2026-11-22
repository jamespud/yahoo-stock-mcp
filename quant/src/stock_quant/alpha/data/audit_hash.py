"""Public logical-row encoding, separate from persisted dataset identities."""
import hashlib
import json
import struct

HASH_VERSION = "logical-rows-f64-v1"


def _encode(value):
    if type(value) in (int, float):
        # An unquoted token cannot collide with a JSON string. Encode the number
        # seen by a JS consumer, treating positive and negative zero alike.
        return "~" + struct.pack(">d", float(value) or 0.0).hex()
    if isinstance(value, list):
        return "[" + ",".join(map(_encode, value)) + "]"
    if isinstance(value, dict):
        keys = sorted(value, key=lambda key: key.encode("utf-16-be", "surrogatepass"))
        return "{" + ",".join(_encode(key) + ":" + _encode(value[key]) for key in keys) + "}"
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def logical_rows_hash(rows):
    return "sha256:" + hashlib.sha256(_encode(rows).encode()).hexdigest()
