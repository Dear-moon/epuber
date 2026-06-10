/// Lightnovel.app SignalR bridge — Dart native WebSocket.
/// Usage: dart run lightnovel_bridge.dart --token <refresh_token> --bid <book_id> --chapter <sortnum>
///
/// Python calls this via subprocess; JSON chapter data is printed to stdout.

import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

const _apiBase = 'https://api.lightnovel.life';
const _hubUrl = '$_apiBase/hub/api';
const _userAgent = 'Novella/1.8.0';

// ============================================================
//  MessagePack encoder (minimal)
// ============================================================
Uint8List _msgPackEncode(dynamic value) {
  final buf = <int>[];
  void enc(dynamic v) {
    if (v == null) { buf.add(0xc0); return; }
    if (v is bool) { buf.add(v ? 0xc3 : 0xc2); return; }
    if (v is int) {
      if (v >= 0 && v <= 0x7f) { buf.add(v); return; }
      buf.add(0xd1);
      buf.addAll([(v >> 8) & 0xff, v & 0xff]);
      return;
    }
    if (v is String) {
      final bytes = utf8.encode(v);
      final len = bytes.length;
      if (len < 32) { buf.add(0xa0 | len); }
      else if (len < 256) { buf.addAll([0xd9, len]); }
      else { buf.addAll([0xda, (len >> 8) & 0xff, len & 0xff]); }
      buf.addAll(bytes);
      return;
    }
    if (v is List) {
      final len = v.length;
      if (len < 16) { buf.add(0x90 | len); }
      else { buf.addAll([0xdc, (len >> 8) & 0xff, len & 0xff]); }
      for (final e in v) enc(e);
      return;
    }
    if (v is Map) {
      final len = v.length;
      if (len < 16) { buf.add(0x80 | len); }
      else { buf.addAll([0xde, (len >> 8) & 0xff, len & 0xff]); }
      v.forEach((k, val) { enc(k); enc(val); });
      return;
    }
    // Fallback
    final s = v.toString();
    final bytes = utf8.encode(s);
    buf.add(0xa0 | bytes.length.clamp(0, 31));
    buf.addAll(bytes);
  }
  enc(value);
  return Uint8List.fromList(buf);
}

Uint8List _varintEncode(int n) {
  final buf = <int>[];
  while (n > 127) { buf.add((n & 0x7F) | 0x80); n >>= 7; }
  buf.add(n);
  return Uint8List.fromList(buf);
}

Uint8List _buildFrame(String method, List<dynamic> args) {
  final payload = _msgPackEncode([1, {}, "1", method, args, []]);
  final varint = _varintEncode(payload.length);
  return Uint8List.fromList([...varint, ...payload]);
}

// ============================================================
//  MessagePack decoder
// ============================================================
class _MsgPackReader {
  final Uint8List _data;
  int _pos = 0;
  _MsgPackReader(this._data);

  dynamic read() {
    if (_pos >= _data.length) return null;
    final b = _data[_pos++];
    if (b <= 0x7f) return b;
    if (b >= 0xe0) return b - 0x100;
    if (b >= 0xa0 && b <= 0xbf) return _str(b & 0x1f);
    if (b >= 0x90 && b <= 0x9f) return _arr(b & 0xf);
    if (b >= 0x80 && b <= 0x8f) return _map(b & 0xf);
    switch (b) {
      case 0xc0: return null;
      case 0xc2: return false;
      case 0xc3: return true;
      case 0xc4: return _bin(_u8());
      case 0xc5: return _bin(_u16());
      case 0xcc: return _u8();
      case 0xcd: return _u16();
      case 0xce: return _u32();
      case 0xd0: return (_u8() << 24) >> 24;
      case 0xd1: return (_u16() << 16) >> 16;
      case 0xd2: return _u32();
      case 0xd9: return _str(_u8());
      case 0xda: return _str(_u16());
      case 0xdb: return _str(_u32());
      case 0xdc: return _arr(_u16());
      case 0xdd: return _arr(_u32());
      case 0xde: return _map(_u16());
      default: return null;
    }
  }

  int _u8() => _data[_pos++];
  int _u16() { final v = (_data[_pos] << 8) | _data[_pos + 1]; _pos += 2; return v; }
  int _u32() { final v = (_data[_pos] << 24) | (_data[_pos + 1] << 16) | (_data[_pos + 2] << 8) | _data[_pos + 3]; _pos += 4; return v; }
  String _str(int len) { final s = utf8.decode(_data.sublist(_pos, _pos + len)); _pos += len; return s; }
  Uint8List _bin(int len) { final b = Uint8List.fromList(_data.sublist(_pos, _pos + len)); _pos += len; return b; }
  List<dynamic> _arr(int len) => List.generate(len, (_) => read());
  Map<dynamic, dynamic> _map(int len) { final m = <dynamic, dynamic>{}; for (var i = 0; i < len; i++) { m[read()] = read(); } return m; }
}

// ============================================================
//  HTTP helpers
// ============================================================

Future<Map<String, dynamic>> _httpPost(HttpClient client, String url, {Map<String, dynamic>? body, Map<String, String>? headers}) async {
  final req = await client.postUrl(Uri.parse(url));
  req.headers.set('Content-Type', 'application/json');
  req.headers.set('User-Agent', _userAgent);
  headers?.forEach((k, v) => req.headers.set(k, v));
  req.write(jsonEncode(body ?? {}));
  final resp = await req.close();
  final bodyStr = await resp.transform(utf8.decoder).join();
  if (resp.statusCode != 200) {
    throw Exception('HTTP ${resp.statusCode}: ${bodyStr.substring(0, bodyStr.length.clamp(0, 300))}');
  }
  return jsonDecode(bodyStr) as Map<String, dynamic>;
}

// ============================================================
//  SignalR message parsing from binary stream
// ============================================================

List<dynamic> _parseMessages(Uint8List data) {
  final messages = <dynamic>[];
  int offset = 0;
  while (offset < data.length) {
    int length = 0, bytesRead = 0, shift = 0;
    while (offset + bytesRead < data.length) {
      final b = data[offset + bytesRead];
      length |= (b & 0x7F) << shift;
      bytesRead++;
      if ((b & 0x80) == 0) break;
      shift += 7;
      if (shift >= 35) break;
    }
    if (length == 0 || length > 10000000) { offset += bytesRead; continue; }
    final payloadStart = offset + bytesRead;
    final payloadEnd = payloadStart + length;
    if (payloadEnd > data.length) break;
    try {
      messages.add(_MsgPackReader(data.sublist(payloadStart, payloadEnd)).read());
    } catch (_) {}
    offset = payloadEnd;
  }
  return messages;
}

// ============================================================
//  Main
// ============================================================

void main(List<String> args) async {
  String? token;
  int? bid;
  int? chapter;
  String mode = 'chapter';
  for (var i = 0; i < args.length; i++) {
    if (args[i] == '--token' && i + 1 < args.length) token = args[++i];
    if (args[i] == '--bid' && i + 1 < args.length) bid = int.tryParse(args[++i]);
    if (args[i] == '--chapter' && i + 1 < args.length) chapter = int.tryParse(args[++i]);
    if (args[i] == '--mode' && i + 1 < args.length) mode = args[++i];
  }
  if (token == null || bid == null) {
    stderr.writeln('Usage: lightnovel_bridge --token <refresh_token> --bid <book_id> [--chapter <sortnum>] [--mode chapter|book-info]');
    exit(1);
  }
  if (mode == 'chapter' && chapter == null) {
    stderr.writeln('ERROR: --chapter required for chapter mode');
    exit(1);
  }

  WebSocket.userAgent = null;
  final client = HttpClient();
  client.userAgent = _userAgent;
  client.connectionTimeout = const Duration(seconds: 15);

  try {
    // 1. Refresh session token
    stderr.writeln('[bridge] Refreshing session token...');
    final refreshData = await _httpPost(client, '$_apiBase/api/user/refresh_token',
        body: {'token': token});
    final sessionToken = (refreshData['Response'] ?? refreshData['Token'] ?? refreshData['token']) as String;
    stderr.writeln('[bridge] Session token obtained.');

    // 2. Negotiate SignalR (without negotiateVersion=1 for simpler response)
    stderr.writeln('[bridge] Negotiating SignalR...');
    final negData = await _httpPost(client, '$_hubUrl/negotiate?negotiateVersion=0');
    final connectionId = negData['connectionId'] as String;
    stderr.writeln('[bridge] Connection ID: $connectionId');

    // 3. Connect WebSocket with explicit :443 port
    final wsUrl = 'wss://api.lightnovel.life:443/hub/api?id=${Uri.encodeQueryComponent(connectionId)}'
        '&access_token=${Uri.encodeQueryComponent(sessionToken)}';
    stderr.writeln('[bridge] Connecting WebSocket...');
    final ws = await WebSocket.connect(wsUrl, headers: {
      'Origin': 'https://www.lightnovel.app',
      'User-Agent': _userAgent,
    });
    stderr.writeln('[bridge] Connected.');

    // 4. MessagePack handshake
    ws.add(utf8.encode('{"protocol":"messagepack","version":1}\x1e'));
    stderr.writeln('[bridge] Handshake sent.');

    // 5. Prepare response collector
    final completer = Completer<Map<String, dynamic>?>();
    Map<String, dynamic>? result;

    // 6. Send invocation based on mode
    Uint8List frame;
    if (mode == 'book-info') {
      final methods = ['GetBookDetail', 'GetBook', 'GetNovelInfo', 'GetNovel'];
      for (final method in methods) {
        frame = _buildFrame(method, [{'BookId': bid}]);
        ws.add(frame);
        stderr.writeln('[bridge] Trying $method...');
        await Future.delayed(Duration(milliseconds: 2000));
        if (completer.isCompleted) break;
      }
      if (!completer.isCompleted) {
        completer.complete(null);
      }
    } else {
      frame = _buildFrame('GetNovelContent', [
        {'Bid': bid, 'SortNum': chapter},
        {'UseGzip': true},
      ]);
      ws.add(frame);
      stderr.writeln('[bridge] Invocation sent (${frame.length} bytes).');
    }

    ws.listen(
      (message) {
        if (completer.isCompleted) return;

        if (message is List<int>) {
          final data = Uint8List.fromList(message);
          for (final msg in _parseMessages(data)) {
            if (msg is! List || msg.isEmpty) continue;
            final typeId = msg[0] as int?;

            if (typeId == 2 && msg.length > 3) {
              // StreamItem: [2, headers, invocationId, item]
              _extractResult(msg[3], completer);
            } else if (typeId == 3 && msg.length > 4) {
              // Completion: [3, headers, invocationId, resultKind, result]
              final resultKind = msg[3] as int?;
              if (resultKind == 3) {
                _extractResult(msg[4], completer);
              }
              if (!completer.isCompleted) {
                completer.complete(null); // No result
              }
            } else if (typeId == 7) {
              // Close
              if (!completer.isCompleted) completer.complete(null);
            }
          }
        }
      },
      onDone: () {
        if (!completer.isCompleted) completer.complete(null);
      },
      onError: (e) {
        if (!completer.isCompleted) completer.complete(null);
      },
      cancelOnError: true,
    );

    result = await completer.future.timeout(const Duration(seconds: 30));
    await ws.close();
    client.close();

    if (result == null) {
      stderr.writeln('[bridge] ERROR: No chapter data received.');
      exit(1);
    }

    // 7. Output
    final ch = result['Chapter'];
    if (ch is Map) {
      stderr.writeln('[bridge] Chapter: ${ch['Title'] ?? "?"}');
      stderr.writeln('[bridge] Content: ${(ch['Content'] as String?)?.length ?? 0} chars');
      stderr.writeln('[bridge] Font: ${ch['Font'] ?? "none"}');
    }
    stdout.writeln(jsonEncode(result));
  } catch (e, st) {
    stderr.writeln('[bridge] ERROR: $e');
    stderr.writeln(st.toString());
    exit(1);
  }
}

void _extractResult(dynamic item, Completer<Map<String, dynamic>?> completer) {
  if (completer.isCompleted) return;
  if (item is! Map) return;

  // Try gzip-compressed Response field
  final respBytes = item['Response'];
  if (respBytes is Uint8List && respBytes.length > 10) {
    try {
      final decompressed = gzip.decode(respBytes);
      final parsed = jsonDecode(utf8.decode(decompressed));
      if (parsed is Map<String, dynamic>) {
        completer.complete(parsed);
        return;
      }
    } catch (_) {}
  }

  // Try direct chapter data
  if (item.containsKey('Chapter')) {
    final result = <String, dynamic>{};
    item.forEach((k, v) {
      if (v is Uint8List) {
        try { result[k.toString()] = utf8.decode(v); } catch (_) { result[k.toString()] = v; }
      } else {
        result[k.toString()] = v;
      }
    });
    completer.complete(result);
  }
}
