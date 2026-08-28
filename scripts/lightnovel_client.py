#!/usr/bin/env python3
"""Pure-Python SignalR (LongPolling) client for lightnovel.app.

Replaces the Dart WebSocket bridge. The server's network layer drops WebSocket
connections whose TLS fingerprint isn't a real browser/Dart stack, but the
SignalR LongPolling transport is plain HTTP(S), so curl_cffi (which impersonates
a browser TLS fingerprint) works end-to-end: refresh -> negotiate -> handshake
-> invoke -> poll.

Flow validated against api.lightnovel.life:
  POST {base}/api/user/refresh_token {token}             -> Response = session token
  POST {base}/hub/api/negotiate?negotiateVersion=0 {}    -> connectionId
  POST {base}/hub/api?id=..&access_token=..              -> handshake text frame (RS-terminated)
  POST {base}/hub/api?id=..&access_token=..              -> MessagePack invocation (octet-stream)
  GET  {base}/hub/api?id=..&access_token=..              -> length-prefixed MessagePack frames
Response wrapper is {Success, Response, Status, Msg}; Response is gzip+JSON.
"""

import json
import gzip
import re
import time
from typing import Any, Dict, Optional

RS = b"\x1e"  # SignalR record separator (text/handshake records)


class SignalRError(RuntimeError):
    """Lift the server's {Status, Msg} into an exception."""

    def __init__(self, msg: str, status: Optional[int] = None):
        super().__init__(msg or f"server error ({status})")
        self.status = status


def _msgpack_encode(value):
    """Minimal MessagePack encoder (mirrors the Dart bridge exactly)."""
    out = bytearray()

    def enc(v):
        if v is None:
            out.append(0xC0)
        elif v is True:
            out.append(0xC3)
        elif v is False:
            out.append(0xC2)
        elif isinstance(v, int):
            if 0 <= v <= 0x7F:
                out.append(v)
            else:
                out.append(0xD1)
                out.extend((v & 0xFFFF).to_bytes(2, "big"))
        elif isinstance(v, str):
            b = v.encode("utf-8")
            n = len(b)
            if n < 32:
                out.append(0xA0 | n)
            elif n < 256:
                out.append(0xD9)
                out.append(n)
            else:
                out.append(0xDA)
                out.extend(n.to_bytes(2, "big"))
            out.extend(b)
        elif isinstance(v, (list, tuple)):
            n = len(v)
            if n < 16:
                out.append(0x90 | n)
            else:
                out.append(0xDC)
                out.extend(n.to_bytes(2, "big"))
            for x in v:
                enc(x)
        elif isinstance(v, dict):
            n = len(v)
            if n < 16:
                out.append(0x80 | n)
            else:
                out.append(0xDE)
                out.extend(n.to_bytes(2, "big"))
            for k, val in v.items():
                enc(k)
                enc(val)
        else:
            enc(str(v))

    enc(value)
    return bytes(out)


def _varint(n):
    b = bytearray()
    while n > 127:
        b.append((n & 0x7F) | 0x80)
        n >>= 7
    b.append(n)
    return bytes(b)


def _frame(method, args):
    payload = _msgpack_encode([1, {}, "1", method, args, []])
    return _varint(len(payload)) + payload


class _Reader:
    """Tiny MessagePack reader (enough for server frames)."""

    def __init__(self, data):
        self.d = data
        self.p = 0

    def u8(self):
        v = self.d[self.p]
        self.p += 1
        return v

    def u16(self):
        v = (self.d[self.p] << 8) | self.d[self.p + 1]
        self.p += 2
        return v

    def u32(self):
        v = ((self.d[self.p] << 24) | (self.d[self.p + 1] << 16)
             | (self.d[self.p + 2] << 8) | self.d[self.p + 3])
        self.p += 4
        return v

    def read(self):
        if self.p >= len(self.d):
            return None
        b = self.u8()
        if b <= 0x7F:
            return b
        if b >= 0xE0:
            return b - 0x100
        if 0xA0 <= b <= 0xBF:
            n = b & 0x1F
            s = self.d[self.p:self.p + n].decode("utf-8")
            self.p += n
            return s
        if 0x90 <= b <= 0x9F:
            return [self.read() for _ in range(b & 0xF)]
        if 0x80 <= b <= 0x8F:
            m = {}
            for _ in range(b & 0xF):
                k = self.read()
                m[k] = self.read()
            return m
        if b == 0xC0:
            return None
        if b == 0xC2:
            return False
        if b == 0xC3:
            return True
        if b == 0xC4:
            n = self.u8()
            v = self.d[self.p:self.p + n]
            self.p += n
            return v
        if b == 0xC5:
            n = self.u16()
            v = self.d[self.p:self.p + n]
            self.p += n
            return v
        if b == 0xCC:
            return self.u8()
        if b == 0xCD:
            return self.u16()
        if b == 0xCE:
            return self.u32()
        if b == 0xD0:
            return (self.u8() << 24) >> 24
        if b == 0xD1:
            return (self.u16() << 16) >> 16
        if b == 0xD2:
            return self.u32()
        if b == 0xD9:
            n = self.u8()
            s = self.d[self.p:self.p + n].decode("utf-8")
            self.p += n
            return s
        if b == 0xDA:
            n = self.u16()
            s = self.d[self.p:self.p + n].decode("utf-8")
            self.p += n
            return s
        if b == 0xDB:
            n = self.u32()
            s = self.d[self.p:self.p + n].decode("utf-8")
            self.p += n
            return s
        if b == 0xDC:
            return [self.read() for _ in range(self.u16())]
        if b == 0xDD:
            return [self.read() for _ in range(self.u32())]
        if b == 0xDE:
            m = {}
            for _ in range(self.u16()):
                k = self.read()
                m[k] = self.read()
            return m
        return None


def _split_records(data):
    """Split a LongPoll body into (kind, record): RS-terminated text or length-prefixed binary."""
    records = []
    off = 0
    n = len(data)
    while off < n:
        if data[off:off + 1] == b"{":
            end = data.find(RS, off)
            if end == -1:
                records.append(("text", data[off:]))
                break
            records.append(("text", data[off:end + 1]))
            off = end + 1
            continue
        length = 0
        br = 0
        shift = 0
        while off + br < n:
            q = data[off + br]
            length |= (q & 0x7F) << shift
            br += 1
            if (q & 0x80) == 0:
                break
            shift += 7
            if shift >= 35:
                break
        if length == 0 or length > 10000000:
            off += br
            continue
        start = off + br
        end = start + length
        if end > n:
            break
        try:
            records.append(("bin", _Reader(data[start:end]).read()))
        except Exception:
            pass
        off = end
    return records


class LightnovelClient:
    """One SignalR LongPolling connection to lightnovel.app."""

    def __init__(self, base: str, refresh_token: str, impersonate: str = "chrome124"):
        self.base = base.rstrip("/")
        self.hub = f"{self.base}/hub/api"
        self.token = refresh_token
        self.impersonate = impersonate
        self.session = None
        self.cid = None
        self._q = None
        try:
            from curl_cffi import requests as curl
            self._curl = curl
        except ImportError as e:
            raise RuntimeError("curl_cffi required (pip install curl_cffi)") from e

    def _post_json(self, url, payload):
        r = self._curl.post(url, json=payload, impersonate=self.impersonate,
                            verify=False, timeout=15)
        if r.status_code != 200:
            raise SignalRError(f"HTTP {r.status_code}: {r.text[:200]}", r.status_code)
        return r.json()

    def _hub(self, url, method="post", body=None, headers=None, timeout=15):
        h = dict(headers or {})
        if body is not None and not h.get("Content-Type"):
            h["Content-Type"] = "application/octet-stream"
        r = self._curl.request(method.upper(), url, headers=h, data=body,
                               impersonate=self.impersonate, verify=False, timeout=timeout)
        return r.status_code, r.content

    def connect(self):
        # 1. Exchange long-term token for a short-lived session token.
        j = self._post_json(f"{self.base}/api/user/refresh_token", {"token": self.token})
        self.session = j.get("Response") or j.get("Token") or j.get("token")
        if not self.session:
            raise SignalRError("refresh_token returned no session token")

        # 2. Negotiate SignalR (v0 -> connectionId).
        j = self._post_json(f"{self.hub}/negotiate?negotiateVersion=0", {})
        self.cid = j.get("connectionId")
        if not self.cid:
            raise SignalRError("negotiate returned no connectionId")
        self._q = f"?id={self.cid}&access_token={self.session}"

        # 3. Handshake (text frame, RS-terminated).
        self._hub(f"{self.hub}{self._q}", "post",
                  b'{"protocol":"messagepack","version":1}' + RS,
                  headers={"Content-Type": "text/plain;charset=UTF-8"})
        return self

    def _poll(self, timeout=15):
        r, body = self._hub(f"{self.hub}{self._q}", "get", timeout=timeout)
        if r == 204 or not body:
            return None
        return body

    def invoke(self, method: str, params: Dict[str, Any]):
        """Call a hub method and return its decoded Response (gzip handled)."""
        if not self._q:
            self.connect()
        self._hub(f"{self.hub}{self._q}", "post", _frame(method, [params, {"UseGzip": True}]))

        deadline = time.time() + 30
        while time.time() < deadline:
            body = self._poll()
            if not body:
                continue
            for kind, rec in _split_records(body):
                if kind == "text":
                    if b'"error"' in rec:
                        raise SignalRError(rec[:120].decode("utf-8", "replace"))
                    continue
                if not isinstance(rec, list) or not rec:
                    continue
                t = rec[0]
                if not isinstance(t, int):
                    continue
                if t == 2 and len(rec) > 3 and str(rec[2]) == "1":
                    item = rec[3]
                    if isinstance(item, dict):
                        return self._unwrap(item)
                elif t == 3 and len(rec) > 4 and str(rec[2]) == "1":
                    if rec[3] == 3:  # result
                        return self._unwrap(rec[4])
                    if rec[3] in (2, 4):  # error / void
                        raise SignalRError(_fmt_completion_error(rec))
        raise SignalRError(f"timeout waiting for {method}")

    @staticmethod
    def _unwrap(item):
        if isinstance(item, dict) and "Response" in item:
            rb = item["Response"]
            if isinstance(rb, (bytes, bytearray)) and len(rb) > 10:
                return json.loads(gzip.decompress(bytes(rb)).decode("utf-8"))
            return item["Response"]
        return item


def _fmt_completion_error(rec):
    # Completion [3, headers, id, resultKind(2=error), error{message,stack}]
    err = rec[4]
    if isinstance(err, dict):
        return f"{err.get('message') or err.get('Message') or err} (signalr error)"
    return f"{err} (signalr error)"


def download_book(base: str, refresh_token: str, bid: int,
                  bearer: Optional[str] = None) -> "tuple[bytes, Optional[str]]":
    """Download a whole book as EPUB via REST.

    Requires the account to have download permission (CanDownload / DownloadCost).
    Returns (epub_bytes, filename_or_None). Filename comes from Content-Disposition.
    """
    from curl_cffi import requests as curl
    base = base.rstrip("/")
    if not bearer:
        # Obtain a session token with the same stack.
        r = curl.post(f"{base}/api/user/refresh_token", json={"token": refresh_token},
                      impersonate="chrome124", verify=False, timeout=15)
        j = r.json()
        bearer = j.get("Response") or j.get("Token") or j.get("token")
    headers = {"Accept": "application/octet-stream, application/json",
               "Authorization": f"Bearer {bearer}"}
    r = curl.get(f"{base}/api/book/download?bid={bid}", headers=headers,
                 impersonate="chrome124", verify=False, timeout=120)
    if r.status_code == 200 and (r.headers.get("Content-Type", "").startswith(("application", "application/epub")) or r.content[:2] == b"PK"):
        return r.content, _parse_filename(r.headers.get("Content-Disposition"))
    # Failure returns the unified {Success, Status, Msg}.
    try:
        j = r.json()
        raise SignalRError(f"download failed: {j.get('Msg') or j.get('Message')} ({r.status_code})", j.get("Status"))
    except SignalRError:
        raise
    except Exception:
        raise SignalRError(f"download failed (HTTP {r.status_code}, non-JSON body)")


def _parse_filename(disposition):
    if not disposition:
        return None
    m = re.search(r"filename\*=UTF-8''([^;]+)", disposition, re.I)
    if m:
        try:
            import urllib.parse
            return urllib.parse.unquote(m.group(1))
        except Exception:
            pass
    m = re.search(r'filename="?([^";]+)"?', disposition, re.I)
    return m.group(1) if m else None
