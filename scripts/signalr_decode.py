#!/usr/bin/env python3
"""Decode SignalR MessagePack frames from CDP Network capture."""
import base64, gzip, struct
import msgpack

def read_varint(data, offset):
    """Read a variable-length integer used as SignalR MessagePack length prefix."""
    result = 0
    shift = 0
    while offset < len(data):
        byte = data[offset]
        offset += 1
        result |= (byte & 0x7F) << shift
        shift += 7
        if not (byte & 0x80):
            break
    return result, offset

def decode_signalr_frame(b64_data):
    """Decode a base64-encoded SignalR MessagePack WebSocket frame.
    Returns list of decoded message dicts with decompressed Response fields.
    """
    data = base64.b64decode(b64_data)
    messages = []
    offset = 0

    while offset < len(data) - 1:
        try:
            length, offset = read_varint(data, offset)
            if length == 0 or offset + length > len(data):
                break
            payload = data[offset:offset + length]
            offset += length

            obj = msgpack.unpackb(payload, raw=True, strict_map_key=False,
                                   max_map_len=100000, max_array_len=100000,
                                   max_str_len=10000000, max_bin_len=10000000)

            # Decode dict keys and gzip response fields
            decoded = _decode_msg(obj)
            messages.append(decoded)
        except Exception:
            break

    return messages

def _decode_msg(obj):
    """Recursively decode msgpack objects, decompressing gzip Response fields."""
    if isinstance(obj, list):
        return [_decode_msg(item) for item in obj]
    if isinstance(obj, dict):
        result = {}
        for k, v in obj.items():
            key = k.decode('utf-8', errors='replace') if isinstance(k, bytes) else str(k)
            if key == 'Response' and isinstance(v, bytes) and len(v) > 20:
                try:
                    decompressed = gzip.decompress(v)
                    result[key] = decompressed.decode('utf-8', errors='replace')
                except:
                    result[key] = f"[{len(v)} bytes binary]"
            elif isinstance(v, bytes):
                try:
                    result[key] = v.decode('utf-8')
                except:
                    result[key] = f"[{len(v)} bytes binary]"
            elif isinstance(v, (dict, list)):
                result[key] = _decode_msg(v)
            else:
                result[key] = v
        return result
    if isinstance(obj, bytes):
        try:
            return obj.decode('utf-8', errors='replace')
        except:
            return obj.hex()
    return obj

if __name__ == '__main__':
    import sys, json
    for b64 in sys.argv[1:]:
        msgs = decode_signalr_frame(b64)
        for m in msgs:
            print(json.dumps(m, ensure_ascii=False, indent=2)[:3000])
            print('---')
