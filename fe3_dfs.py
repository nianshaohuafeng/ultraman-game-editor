"""Read full FE3 DFS streams and the short prefixes used by the ISO index."""

WINDOW = 4096
LOOKAHEAD = 18
HEADER = 12


def _expand(data, size):
    # A zero-filled prefix is the initial dictionary. The previous 4096 output
    # bytes then represent the same history as the game's circular buffer.
    out = bytearray(WINDOW)
    end = WINDOW + size
    cursor = HEADER
    while len(out) < end:
        if cursor >= len(data):
            raise ValueError('compressed stream ended before output was complete')
        flags = data[cursor]
        cursor += 1
        if flags == 255:
            count = min(8, end - len(out))
            if cursor + count > len(data):
                raise ValueError('literal token is truncated')
            out.extend(data[cursor:cursor + count])
            cursor += count
            continue
        for _ in range(8):
            if flags & 1:
                if cursor >= len(data):
                    raise ValueError('literal token is truncated')
                out.append(data[cursor])
                cursor += 1
            else:
                if cursor + 2 > len(data):
                    raise ValueError('reference token is truncated')
                high = data[cursor + 1]
                slot = data[cursor] | ((high & 0xF0) << 4)
                cursor += 2
                current = len(out)
                count = min((high & 15) + 3, end - current)
                distance = ((current - LOOKAHEAD - slot) & (WINDOW - 1)) or WINDOW
                start = current - distance
                if count <= distance:
                    out.extend(out[start:start + count])
                else:
                    # Overlapping references repeat the bytes preceding the cursor.
                    pattern = out[start:current]
                    out.extend((pattern * ((count + distance - 1) // distance))[:count])
            if len(out) == end:
                break
            flags >>= 1
    return bytes(memoryview(out)[WINDOW:])


def decode_dfs_enc(data):
    if len(data) < HEADER:
        raise ValueError('file is too small to be a DFS .ENC file')
    if data[:4] != b'DFS\x01':
        raise ValueError('unsupported header: expected DFS version 1')
    stored = int.from_bytes(data[8:12], 'little')
    if stored != len(data):
        raise ValueError(f'size field mismatch: header says {stored}, file is {len(data)}')
    return _expand(data, int.from_bytes(data[4:8], 'little'))


def unpacked_tag(header):
    if header[:4] != b'DFS\x01':
        return header[:4]
    if len(header) < HEADER + 1:
        raise ValueError('DFS 压缩头不完整。')
    size = min(int.from_bytes(header[4:8], 'little'), 4)
    literals = (1 << size) - 1
    if header[HEADER] & literals == literals and len(header) >= HEADER + 1 + size:
        return header[HEADER + 1:HEADER + 1 + size]
    return _expand(header, size)
