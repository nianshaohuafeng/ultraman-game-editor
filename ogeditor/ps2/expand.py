"""Layout changes used by the FE3 native skill table writer."""

import struct
from dataclasses import replace
from shutil import copyfileobj

from ogeditor.ps2.formats import align, require, word
from ogeditor.ps2.native import native_offset
from scan_assets import iso_directory


PAGE = 2048
LOAD_PREFIX = 0x1FF000
AREA = 0x1000
HEADER = 0x80
MAGIC = b'OGFE3SK1'
HEADER_WORDS = (0x1000, 0x200000, 0x200000)


def prefix_records(elf):
    if elf[HEADER:HEADER + len(MAGIC)] != MAGIC:
        return None
    cursor, end = struct.unpack_from('<HH', elf, HEADER + len(MAGIC))
    require(HEADER + 12 <= cursor <= end <= AREA and struct.unpack_from('<III',elf,56) == (0,LOAD_PREFIX,LOAD_PREFIX),
            '新增必杀的 ELF 扩展头无效。')
    changes = {}
    while cursor < end:
        require(cursor + 8 <= end, '新增必杀的 ELF 改动表无效。')
        offset, length = struct.unpack_from('<II', elf, cursor)
        cursor += 8
        require(AREA <= offset <= len(elf) - length and 0 < length <= end - cursor and offset not in changes,
                '新增必杀的 ELF 改动位置无效。')
        changes[offset] = elf[cursor:cursor + length]
        cursor += length
    return struct.unpack_from('<H',elf,HEADER + len(MAGIC))[0], changes


def stock_elf(elf, records):
    if records is None:
        return elf
    original = bytearray(elf)
    ph_end = word(elf,28) + 32*struct.unpack_from('<H',elf,44)[0]
    original[ph_end:AREA] = bytes(AREA-ph_end)
    packed, allocated = struct.unpack_from('<II',elf,68)
    struct.pack_into('<5I',original,56,*HEADER_WORDS,packed - AREA,allocated - AREA)
    for offset, raw in records[1].items():
        original[offset:offset + len(raw)] = raw
    return bytes(original)


class ElfSpace:
    def __init__(self, elf, records):
        self.elf = bytearray(elf)
        if records is None:
            width,count = struct.unpack_from('<HH',elf,42)
            require(word(elf,28) == 52 and width == 32 and count in (1,2)
                    and struct.unpack_from('<III',elf,56) == HEADER_WORDS and not any(elf[52+32*count:AREA]),
                    '此 ELF 的装载前缀布局与已知版本不同。')
            packed, allocated = struct.unpack_from('<II',elf,68)
            struct.pack_into('<5I',self.elf,56,0,LOAD_PREFIX,LOAD_PREFIX,packed + AREA,allocated + AREA)
            self.cursor, self.changes = HEADER + 12, {}
        else:
            self.cursor, self.changes = records[0], records[1].copy()

    def allocate(self, data):
        at = align(self.cursor, 4)
        end = at + len(data)
        require(end <= AREA, '此镜像的新增必杀装载区已满。')
        self.elf[at:end] = data
        self.cursor = end
        return LOAD_PREFIX + at

    def patch_file(self, offset, data):
        if self.elf[offset:offset + len(data)] == data:
            return
        if offset >= AREA:
            before = self.changes.setdefault(offset, bytes(self.elf[offset:offset + len(data)]))
            require(len(before) == len(data), '新增必杀的 ELF 记录跨度不一致。')
        self.elf[offset:offset + len(data)] = data

    def patch(self, address, data):
        self.patch_file(native_offset(self.elf,address,len(data)),data)

    def patch_word(self,address,value):
        self.patch(address,struct.pack('<I',value & 0xFFFFFFFF))

    def finish(self):
        log = b''.join(struct.pack('<II',at,len(old)) + old for at,old in sorted(self.changes.items()))
        start = self.cursor
        end = start + len(log)
        require(end <= AREA, '此镜像的新增必杀装载区已满。')
        self.elf[HEADER:HEADER + 12] = MAGIC + struct.pack('<HH', start, end)
        self.elf[start:AREA] = log.ljust(AREA - start,b'\0')
        return bytes(self.elf)


def relocated_halfwords(address):
    return ((address + 0x8000) >> 16) & 0xFFFF, address & 0xFFFF


def erx_append(program, ordinal, added, *, action_callbacks=None):
    """Append native scene code, scripts and the updated action and input arrays."""
    data = program.data
    prog_at, old_sections_at = struct.unpack_from('<II',data,28)
    require(struct.unpack_from('<HH',data,42) == (32,2), '角色 ERX 的装载表不匹配。')
    mod = struct.unpack_from('<8I',data,prog_at)
    kind, seg, va, physical, file_size, memory_size, flags, alignment = struct.unpack_from('<8I',data,prog_at + 32)
    require((kind,va,physical,flags,mod[0]) == (1,0,0,7,0x70000090)
            and file_size <= memory_size and seg + file_size <= len(data), '角色 ERX 的装载段不匹配。')
    count = len(program.actions())
    ext_start = align(memory_size,16)
    extension = bytearray()
    relocations = []
    prior_relocations = program.sections.get(b'.rel.moves')
    if prior_relocations:
        offset,length = prior_relocations[4:6]
        relocations.extend(struct.iter_unpack('<II',data[offset:offset+length]))

    def allocate(raw, boundary=4):
        address = align(ext_start + len(extension),boundary)
        extension.extend(bytes(address - ext_start - len(extension)))
        extension.extend(raw)
        return address

    action_at = program.file_offset(program.action_table,24 * count)
    command_at = program.file_offset(program.command_table,4 * count)
    actions = bytearray(data[action_at:action_at + 24 * count])
    commands = data[command_at:command_at + 4 * count]
    if action_callbacks is not None:
        for state, callbacks in action_callbacks(allocate, relocations).items():
            require(0 <= state < count, '角色回调的状态编号越界。')
            struct.pack_into('<3I', actions, 24 * state, *callbacks)
    for state, (template, anm, scene) in enumerate(added,count):
        current, begin, end, next_action, _, flags_word = program.at(program.action_table + 24 * template,'<6I')
        script_address = allocate(scene,32)
        head = list(program.at(begin,'<7I',b'.text'))
        require((head[1] >> 16, head[3] >> 16, head[5] >> 26) == (0x3C05,0x24A5,2),
                '角色没有可复用的原生必杀进入回调。')
        hi, lo = relocated_halfwords(script_address)
        head[1] = (head[1] & 0xFFFF0000) | hi
        head[3] = (head[3] & 0xFFFF0000) | lo
        thunk = allocate(struct.pack('<7I',*head))
        relocations.extend(((thunk + 4,5),(thunk + 12,6),(thunk + 20,4)))
        actions.extend(struct.pack('<6I',current,thunk,end,next_action,anm,flags_word))
    action_table = allocate(actions,16)
    allocate(bytes(24))
    command_table = allocate(commands + bytes(4 * len(added)),16)
    relocations.extend((action_table + 24 * i + field,2) for i in range(count + len(added)) for field in (0,4,8))
    relocations.extend((command_table + 4*i, 2) for i in range(count)
                       if program.command_table + 4*i in program.relocations)

    size, sections, names = struct.unpack_from('<3H',data,46)
    require(size == 40 and 0 < names < sections, '角色 ERX 的节目录无效。')
    headers = [list(struct.unpack_from('<10I', data,old_sections_at + 40*i)) for i in range(sections)]
    names_at, names_size = headers[names][4:6]
    strings = data[names_at:names_at + names_size]
    end_of_load = seg + file_size
    inserted = bytes(ext_start - file_size) + bytes(extension)
    result = bytearray(data[:end_of_load] + inserted + data[end_of_load:])
    new_filesize = ext_start + len(extension)
    # The original BSS bytes between the old image and extension are included
    # as zero in the PT_LOAD. The EE loader allocates p_memsz and reads p_filesz.
    struct.pack_into('<II',result,prog_at + 32 + 16,new_filesize,new_filesize)
    text_size = word(data,mod[1] + 12)
    require(word(data,mod[1]+16) + text_size == file_size and word(data,mod[1]+20) + file_size == memory_size,
            '角色 ERX 的 EEMOD 文件与内存长度不匹配。')
    struct.pack_into('<II',result,mod[1]+16,new_filesize - text_size,0)
    for row in headers[1:]:
        if row[4] >= end_of_load:
            row[4] += len(inserted)
    if not prior_relocations:
        string_name = len(strings)
        strings += b'.moves\0.rel.moves\0'
        headers[names][4:6] = len(result),len(strings)
        result.extend(strings)
    result.extend(bytes(-len(result) % 4))
    reloc_at = len(result)
    result.extend(b''.join(struct.pack('<II',*pair) for pair in sorted(relocations)))
    if prior_relocations:
        by_name = {strings[row[0]:strings.index(b'\0',row[0])]:row for row in headers if row[0]}
        moves = by_name[b'.moves']
        moves[5] = new_filesize-moves[3]
        by_name[b'.rel.moves'][4:6] = reloc_at,8*len(relocations)
    else:
        headers.extend((
            [string_name,1,7,ext_start,seg+ext_start,len(extension),0,0,16,0],
            [string_name+7,9,0,0,reloc_at,8*len(relocations),
             next(i for i,row in enumerate(headers) if row[1] == 2),sections,4,8],
        ))
    struct.pack_into('<I',result,32,len(result))
    struct.pack_into('<H',result,48,len(headers))
    result.extend(b''.join(struct.pack('<10I',*row) for row in headers))
    for query, offsets, address in ((5,(4,12),action_table),(ordinal,(0,8),command_table)):
        for delta, half in zip(offsets,relocated_halfwords(address)):
            at = program.file_offset(program.export[query] + delta,4,b'.text')
            struct.pack_into('<H',result,at,half)
    return bytes(result)


def write_extent(handle, directory, offset, length):
    handle.seek(directory + 2)
    for value in (offset // PAGE,length):
        handle.write(struct.pack('<I',value))
        handle.write(struct.pack('>I',value))


def write_expanded_iso(source, destination, files, container, grown, writes):
    """Insert aligned resource bytes at the end of FILE3 and update ISO9660."""
    pack = files[container]
    boundary = pack.offset + pack.size
    require(boundary % PAGE == 0,'FILE3.BIN 没有按扇区结束。')
    growth = sum(map(len,grown))
    extents = {}
    with source.open('rb') as inp, destination.open('w+b') as out:
        records = {item['path'].lstrip('/').upper():item['record'] for item in iso_directory(inp)}
        require(len(records) == len(files) and all(p < boundary for p in records.values()),
                'ISO 目录布局与格斗进化3 扩展写入不匹配。')
        inp.seek(0)
        remaining = boundary
        while remaining:
            chunk = inp.read(min(8*1024*1024,remaining))
            require(chunk, 'ISO 在 FILE3.BIN 结束前截断。')
            out.write(chunk)
            remaining -= len(chunk)
        for blob in grown:
            out.write(blob)
        copyfileobj(inp,out,8*1024*1024)
        end = align(out.tell(),PAGE)
        for path, file in files.items():
            if path == container:
                extents[path] = replace(file,size=file.size + growth)
            elif growth and file.offset >= boundary:
                extents[path] = replace(file,offset=file.offset + growth)
        for path, data in writes.items():
            file = extents.get(path,files[path])
            if len(data) > align(file.size,PAGE):
                file = replace(file,offset=end)
                end += align(len(data),PAGE)
            out.seek(file.offset)
            out.write(data)
            extents[path] = replace(file,size=len(data))
        for path,file in extents.items():
            write_extent(out,records[path],file.offset,file.size)
        out.truncate(end)
        out.seek(16*PAGE + 80)
        out.write(struct.pack('<I',end//PAGE))
        out.write(struct.pack('>I',end//PAGE))

