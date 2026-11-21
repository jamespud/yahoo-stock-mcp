"""Bounded result pages shared by job and dataset readers."""
import base64
import json

MAX_FRAME_BYTES = 4 * 1024 * 1024


def _compact(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _numeric_slack(value):
    # JSON.stringify can expand Python's exponent notation to a decimal literal.
    if isinstance(value, dict):
        return sum(_numeric_slack(item) for item in value.values())
    if isinstance(value, list):
        return sum(_numeric_slack(item) for item in value)
    if type(value) is float and "e" in repr(value):
        return 24
    if type(value) is int and not -(2**53) < value < 2**53:
        return 24
    return 0


def _mcp_bytes(text):
    # Python escapes non-ASCII for the bridge, but Node preserves it in MCP.
    text = text.encode("utf-8", "backslashreplace").decode("utf-8")
    return len(_compact(text).encode()) - 2


def _envelope_sizes(result, uri, mcp_overhead_bytes=None):
    text = _compact(result)
    tool = {"content": [{"type": "text", "text": ""}]}
    resource = {"contents": [{"uri": uri, "mimeType": "application/json", "text": ""}]}
    # Reserve framing for the JSON-RPC version, result wrapper and request ID.
    outer = max(len(_compact(item).encode()) for item in (tool, resource)) + 128
    if mcp_overhead_bytes is not None:
        if type(mcp_overhead_bytes) is not int or not 0 <= mcp_overhead_bytes < MAX_FRAME_BYTES:
            raise ValueError("RESOURCE_LIMIT_EXCEEDED: invalid MCP frame overhead")
        outer = max(outer, mcp_overhead_bytes)
    return len(json.dumps({"result": result}, allow_nan=False).encode()) + 1, outer + _mcp_bytes(text) + _numeric_slack(result)


def fits_response(result, *, resource_uri="", mcp_overhead_bytes=None):
    return max(_envelope_sizes(result, resource_uri, mcp_overhead_bytes)) <= MAX_FRAME_BYTES


def result_page(identity, section, content_hash, count, limit, cursor, read_rows, *, resource_uri, byte_limit,
                metadata=None, mcp_overhead_bytes=None):
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("Invalid result limit")
    key = [identity, section, content_hash]
    offset = 0
    if cursor is not None:
        try:
            if not isinstance(cursor, str) or len(cursor) > 2048:
                raise ValueError()
            parts = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
            if (not isinstance(parts, list) or len(parts) != 4 or parts[:3] != key
                    or type(parts[3]) is not int or not 0 <= parts[3] <= count):
                raise ValueError()
            offset = parts[3]
        except (ValueError, TypeError, UnicodeError) as error:
            raise ValueError("Invalid result cursor") from error
    def token(next_offset):
        return (base64.urlsafe_b64encode(json.dumps([*key, next_offset]).encode()).decode()
                if next_offset < count else None)

    page = {**(metadata or {}), "rows": [], "total_count": count, "next_cursor": token(offset),
            "content_hash": content_hash, "resource_uri": resource_uri}
    # The last possible offset has the longest cursor. Reserve its space on all pages.
    bridge_base, mcp_base = _envelope_sizes(
        {**page, "next_cursor": token(count - 1) if count else None}, resource_uri, mcp_overhead_bytes)
    rows = page["rows"]
    byte_count = mcp_count = 0
    for item in read_rows(offset, limit):
        row_bytes = len(json.dumps(item, allow_nan=False).encode())
        mcp_row_bytes = _mcp_bytes(_compact(item)) + _numeric_slack(item)
        if (2 + byte_count + row_bytes + 2 * len(rows) > byte_limit
                or bridge_base + byte_count + row_bytes + 2 * len(rows) > MAX_FRAME_BYTES
                or mcp_base + mcp_count + mcp_row_bytes + len(rows) > MAX_FRAME_BYTES):
            if not rows:
                raise ValueError("RESOURCE_LIMIT_EXCEEDED: one result row exceeds the response budget")
            break
        rows.append(item)
        byte_count += row_bytes
        mcp_count += mcp_row_bytes
    page["next_cursor"] = token(offset + len(rows))
    if not fits_response(page, resource_uri=resource_uri, mcp_overhead_bytes=mcp_overhead_bytes):
        raise ValueError("RESOURCE_LIMIT_EXCEEDED: result page exceeds the response budget")
    return page


def fits_response_row(row, encoded_bytes=None):
    # Budget the largest accepted cursor and bounded page metadata as well as
    # the row. Cheap upper bound keeps ordinary small artifact rows on a fast path.
    if encoded_bytes is None:
        encoded_bytes = len(json.dumps(row, allow_nan=False).encode())
    if encoded_bytes <= 256_000:
        return True
    return fits_response({"rows": [row], "next_cursor": "x" * 2048, "metadata_reserve": "x" * 1024})
