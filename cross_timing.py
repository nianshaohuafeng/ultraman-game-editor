"""记录帧事件在原动作中的位置，多次调整动作时长也能按原帧换算。"""
from collections import defaultdict, deque
import json
import struct
import zlib

from ogeditor.ps2.formats import require
from formats import s16, u32

# Both native loaders resolve the outer directory relative to the BOM base
# (EE 0x204670, PSP 0x088889C0). No script or shape points into this gap.
TAG = b'OGBOMT1\0'
MISSING = 0xffffffff
DURATION_OPS = ({0,1,2,3,5,10}, {0,1,2,3,4,5,12,24})


def directory(data):
    scripts, shapes = struct.unpack_from('<2I',data,4)
    total = scripts + shapes
    end = 12 + 4*total
    require(data[:4] == b'BOM\0' and end <= len(data), 'BOM 资源目录无效。')
    offsets = struct.unpack_from(f'<{total}I',data,12)
    start = min((pos for pos in offsets if pos != MISSING),default=len(data))
    require(end <= start <= len(data),'BOM 数据起点无效。')
    return end, start, offsets


class BomTiming:
    def __init__(self, data):
        end, start, _ = directory(data)
        self.padding = data[end:start]
        self.templates = set()
        self.anchors = {}
        if not self.padding.startswith(TAG):
            return
        try:
            size = u32(self.padding,len(TAG))
            used = (len(TAG)+4+size+15) & ~15
            if used > len(self.padding):
                raise ValueError
            stream = zlib.decompressobj()
            blob = stream.decompress(self.padding[len(TAG)+4:len(TAG)+4+size],max(4096,128*len(data)))
            if not stream.eof or stream.unused_data:
                raise ValueError
            indices, rows = json.loads(blob)
            if any(type(i) is not int or not 0 <= i < u32(data,8) for i in indices):
                raise ValueError
            self.templates.update(indices)
            for script, shape, values in rows:
                if not (type(script) is int and 0 <= script < u32(data,4) and type(shape) is int and 0 <= shape < 32):
                    raise ValueError
                anchors = []
                for ordinal,lo,hi,hex_row in values:
                    raw = bytes.fromhex(hex_row)
                    if not (type(ordinal) is int and ordinal >= 0 and type(lo) is int and type(hi) is int
                            and 0 <= lo <= hi <= 32767 and len(raw) >= 4):
                        raise ValueError
                    anchors.append((ordinal,lo,hi,raw))
                self.anchors[script,shape] = anchors
            self.padding = self.padding[used:]
        except (IndexError, KeyError, TypeError, ValueError, zlib.error, struct.error) as exc:
            raise ValueError('BOM 中的移植时间记录无效。') from exc

    def write(self, data):
        end,start,offsets = directory(data)
        gap = self.padding
        if self.templates or self.anchors:
            rows = [[script,shape,[[ordinal,lo,hi,raw.hex()] for ordinal,lo,hi,raw in values]]
                    for (script,shape),values in sorted(self.anchors.items())]
            compressed = zlib.compress(json.dumps([sorted(self.templates),rows],separators=(',',':')).encode())
            header = TAG + struct.pack('<I',len(compressed)) + compressed
            gap = header + bytes(-len(header)%16) + gap
        if data[end:start] == gap:
            return data
        delta = end + len(gap) - start
        shifted = [off if off == MISSING else off+delta for off in offsets]
        return data[:12] + struct.pack(f'<{len(offsets)}I',*shifted) + gap + data[start:]

    def remap(self, key, records, old, new, ps2):
        # Match the current row as well as its occurrence. Edits made by the
        # player become the new reference when they no longer match our output.
        matched, by_order = {}, {}
        for ordinal,lo,hi,ref in self.anchors.get(key,()):
            projected,_ = project_row(ref,(lo,hi),old,ps2)
            entry = lo,hi,ref,projected
            if ordinal < len(records) and records[ordinal] == projected:
                matched[ordinal] = entry
            else:
                by_order[ordinal] = entry
        remaining = defaultdict(deque)
        for index,raw in enumerate(records):
            if index not in matched:
                remaining[raw].append(index)
        for ordinal,entry in list(by_order.items()):
            matches = remaining.get(entry[3])
            if matches:
                matched[matches.popleft()] = entry
                del by_order[ordinal]
        references, shifted = [], []
        ambiguous = set()
        for index,raw in enumerate(records):
            previous = matched.get(index) or by_order.get(index)
            lo,hi = old
            ref = raw
            if previous:
                a,b,source,projected = previous
                duration = raw[0] in DURATION_OPS[ps2] and len(raw) >= 8
                if (raw[:4] == projected[:4] and len(raw) == len(projected)
                        and (not duration or raw[6:8] == projected[6:8])):
                    # A changed input, damage, node, or sound does not reset the frame reference.
                    lo,hi = a,b
                    ref = raw[:2]+source[2:4]+raw[4:]
                    if duration:
                        ref = ref[:6]+source[6:8]+ref[8:]
            value,loss = project_row(ref,(lo,hi),new,ps2)
            shifted.append(value)
            references.append((index,lo,hi,ref))
            if loss:
                ambiguous.add(value)
        # An exactly projected row may also match the bytes of a rounded row.
        # Keep both occurrences so a later directory merge preserves their individual times.
        self.set_anchors(key,[entry for entry,value in zip(references,shifted) if value in ambiguous])
        return shifted

    def set_anchors(self, key, values):
        if values:
            self.anchors[key] = values
        else:
            self.anchors.pop(key,None)

    def retain(self, key, rows, span, ps2):
        remaining = defaultdict(deque)
        kept = []
        for _,lo,hi,raw in self.anchors[key]:
            projected,_ = project_row(raw,(lo,hi),span,ps2)
            remaining[projected].append((lo,hi,raw))
        for ordinal,row in enumerate(rows):
            if remaining.get(row):
                kept.append((ordinal,*remaining[row].popleft()))
        self.set_anchors(key,kept)


def project_row(raw, old, new, ps2):
    lo,hi = old
    first,last = new
    if old == new:
        return raw,False
    source_span = max(1,hi-lo)
    new_span = last-first

    def tick(value):
        return max(0,min(32767,first + round((value-lo)*new_span/source_span)))

    start = s16(raw,2)
    at = tick(start)
    out = bytearray(raw)
    struct.pack_into('<h',out,2,at)
    loss = new_span == 0 and start != lo or (at-first)*source_span != (start-lo)*new_span
    if raw[0] in DURATION_OPS[ps2] and len(raw) >= 8:
        duration = s16(raw,6)
        if duration > 0:
            if duration > hi-lo and start+duration > hi:
                # Native 999-frame combo windows normally outlive the clip.
                length = min(32767,max(duration,last-at+1))
                loss |= length != duration or length <= new_span
            else:
                length = min(32767,max(1,tick(start+duration)-at))
                loss |= length*source_span != duration*new_span
            struct.pack_into('<h',out,6,length)
    return bytes(out),loss

