"""Write a new ISO while keeping v7's group-relative story resources intact."""

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import struct
from tempfile import NamedTemporaryFile

from event_edits import Patch
from formats import u32
from editor_readers import iso_directory, read_yum

SECTOR = 2048


@contextmanager
def new_output(destination, extension, replace_existing=False):
    destination = Path(destination).resolve()
    if destination.suffix.lower() != extension:
        raise ValueError(f'请使用 {extension} 扩展名。')
    if destination.exists() and not replace_existing:
        raise ValueError('请选择尚不存在的文件名。')
    with NamedTemporaryFile(dir=destination.parent, prefix='.kgf-', suffix='.partial', delete=False) as created:
        temporary = Path(created.name)
    try:
        yield temporary
        try:
            if replace_existing:
                temporary.replace(destination)
            elif os.name == 'nt':
                temporary.rename(destination)  # Windows does not replace an existing target.
            else:
                os.link(temporary, destination)
        except FileExistsError as exc:
            raise ValueError('目标文件已经存在，请选择其他文件名。') from exc
    finally:
        temporary.unlink(missing_ok=True)


def both_endian(value):
    return struct.pack('<I', value) + struct.pack('>I', value)


def member_resize(game, member, length):
    size = game.yum['files'][member][1]
    prior = game._recovery.alignment.get(member, 0) if game.resumed else 0
    pad = (size + prior - length) % 16
    return prior, pad, length + pad - size - prior


def move_padding(game, replacements):
    previous = game._recovery.padding if game.resumed else {}
    result = {}
    for group, (_, _, count, first) in enumerate(game.yum['groups']):
        delta = sum(member_resize(game, m, len(replacements[m]))[2]
                    for m in range(first, first + count) if m in replacements)
        padding = (previous.get(group, 0) - delta) % SECTOR
        if padding:
            result[group] = padding
    return result


def plan_yum(game, replacements):
    entry = game.entry
    base = entry['offset']
    original_groups, original_members = game.yum['groups'], game.yum['files']
    payload = original_groups[0][0]
    with game.path.open('rb') as source:
        source.seek(base)
        old_directory = source.read(payload)
    if u32(old_directory, 20) != payload:
        raise ValueError('YUM 目录与首个资源组位置不一致。')

    directory = bytearray(old_directory)
    member_table = 32 + 16 * len(original_groups)
    patches = []
    groups, members = [], list(original_members)
    delta = 0
    padding = move_padding(game, replacements)
    previous = game._recovery.padding if game.resumed else {}
    for group, (start, length, count, first) in enumerate(original_groups):
        group_delta = 0
        for member in range(first, first + count):
            offset, size, flags = original_members[member]
            new_offset = offset + delta + group_delta
            if member in replacements:
                edited = replacements[member]
                old = game.read_image_member(member, edited[:4])
                # Keep the original 16-byte alignment for any following member.
                prior, pad, change = member_resize(game, member, len(edited))
                patches.append(Patch(base + offset, old + bytes(prior), edited + bytes(pad)))
                group_delta += change
                size = len(edited)
            record = new_offset, size, flags
            struct.pack_into('<3I', directory, member_table + 12 * member, *record)
            members[member] = record
        # Reuse only zero padding identified from an earlier move export.
        pad = padding.get(group, 0) - previous.get(group, 0)
        if pad > 0:
            patches.append(Patch(base + start + length, b'', bytes(pad)))
        elif pad < 0:
            patches.append(Patch(base + start + length + pad, bytes(-pad), b''))
        record = start + delta, length + group_delta + pad, count, first
        struct.pack_into('<4I', directory, 32 + 16 * group, *record)
        groups.append(record)
        delta += group_delta + pad

    patches.append(Patch(base, old_directory, bytes(directory)))
    if delta:
        end = base + entry['size']
        for item in game.iso_files.values():
            if item['path'] == entry['path']:
                continue
            if item['offset'] + item['size'] > base or item['record'] + 18 > base:
                raise ValueError('此镜像的 YUM 后方还有文件，无法扩缩资源区。')
        if end > game.stamp[0] or entry['record'] + 18 > base:
            raise ValueError('YUM 的 ISO 目录位置无效。')
        patches.extend((
            Patch(entry['record'] + 10, both_endian(entry['size']), both_endian(entry['size'] + delta)),
            Patch(16 * SECTOR + 80, both_endian(game.stamp[0] // SECTOR),
                  both_endian((game.stamp[0] + delta) // SECTOR)),
        ))
    return patches, {'groups': groups, 'files': members}


def save_iso(game, destination, patches, replacements=None):
    destination = Path(destination).resolve()
    if destination == game.path:
        raise ValueError('请选择新的 ISO 文件名。')
    game.ensure_open()
    size = game.stamp[0]
    digest = hashlib.sha256()
    layout = None
    if replacements:
        resource_patches, layout = plan_yum(game, replacements)
        patches = [*patches, *resource_patches]
    # Insert group padding before any record that starts at the same source offset.
    patches = sorted(patches, key=lambda patch: (patch.offset, bool(patch.original)))

    with new_output(destination, '.iso') as temporary:
        with game.path.open('rb') as source, temporary.open('wb') as output:
            def write(data):
                output.write(data)
                digest.update(data)

            def copy_until(end):
                while source.tell() < end:
                    data = source.read(min(2**20, end - source.tell()))
                    if not data:
                        raise ValueError('源 ISO 大小已变化，请重新打开。')
                    write(data)

            for patch in patches:
                length = len(patch.original)
                if not source.tell() <= patch.offset <= size - length:
                    raise ValueError('镜像写入范围有重叠或越界。')
                copy_until(patch.offset)
                if source.read(length) != patch.original:
                    raise ValueError(f'原镜像在 0x{patch.offset:X} 的字节已变化，请重新打开。')
                write(patch.edited)
            copy_until(size)
            if source.read(1):
                raise ValueError('源 ISO 大小已变化，请重新打开。')
            output.flush()
            os.fsync(output.fileno())
        with temporary.open('rb') as saved:
            if hashlib.file_digest(saved, 'sha256').digest() != digest.digest():
                raise ValueError('另存的 ISO 读回校验失败。')
            if layout is not None:
                try:
                    entry = next(e for e in iso_directory(saved) if e['path'] == game.entry['path'])
                    if read_yum(saved, entry) != layout:
                        raise ValueError('另存的 YUM 目录与资源写入位置不一致。')
                except (AssertionError, UnicodeDecodeError, StopIteration, struct.error):
                    raise ValueError('另存的 ISO 或 YUM 目录校验失败。') from None
        game.ensure_open()
    return {'name': destination.name, 'path': str(destination), 'sha256': digest.hexdigest()}
