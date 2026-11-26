"""Extend the SLPS_254.41 short-sound dispatch using the existing editor ELF prefix."""
import struct
from fe3_formats import require
from fe3_native import native_offset
from fe3_expand import AREA, LOAD_PREFIX, relocated_halfwords
from move_eboot import Z, branch, iword, rword, sll
from move_sound import EE_COUNT, VOICE

LOOKUP = 0x203758
MAGIC = b'OGSND3v1'
T5,T6,T7,S1 = 13,14,15,17


def jump(va):
    return 0x08000000 | (va >> 2)


def read_codes(elf):
    site = native_offset(elf,LOOKUP,8)
    op, delay = struct.unpack_from('<II',elf,site)
    if op == 0x3C0E003B and delay == 0x25CE4340:
        return {}, None
    hook = (op & 0x03ffffff) << 2
    require(op >> 26 == 2 and delay == 0 and LOAD_PREFIX+8 <= hook < LOAD_PREFIX+AREA-72,
            '奥3短音效读取入口无法识别。')
    pos = native_offset(elf,hook,72)
    require(elf[pos-8:pos] == MAGIC,'奥3扩展音效入口标记无效。')
    hi,lo = struct.unpack_from('<II',elf,pos+12)
    addr = ((hi & 65535)<<16) + ((lo & 32767)-(lo & 32768)) + 4*EE_COUNT
    start = native_offset(elf,addr-4,4)
    n, = struct.unpack_from('<I',elf,start)
    require(0 < n <= 32768-EE_COUNT and start+4+4*n <= AREA,'奥3扩展声音目录无效。')
    values = struct.unpack_from(f'<{n}I',elf,start+4)
    return validated(values), (hook,addr,n)


def validated(values):
    require(isinstance(values,(tuple,list)) and len(values) <= 32768-EE_COUNT,'奥3声音索引数无效。')
    for word in values:
        require(type(word) is int and 0<=word<=0xffffffff and (word & ~VOICE)>>16 < 140
                and (word>>8 & 255)<128 and (word&255)<16,'奥3声音编码无效。')
    return dict(enumerate(values,EE_COUNT))


def install(space, codes):
    old, info = read_codes(space.elf)
    require(old.items() <= codes.items(),'奥3基底声音索引与工程冲突。')
    if codes == old:
        return
    values = [codes[i] for i in range(EE_COUNT,EE_COUNT+len(codes))]
    validated(values)
    if info:
        hook,addr,n = info
    else:
        hook = space.allocate(MAGIC+bytes(72)) + len(MAGIC)
    if info and addr+4*n == LOAD_PREFIX+space.cursor:
        space.allocate(struct.pack(f'<{len(values)-n}I',*values[n:]))
        space.patch_word(addr-4,len(values))
    else:
        addr = space.allocate(struct.pack(f'<{1+len(values)}I',len(values),*values))+4
    hi,lo = relocated_halfwords(addr-4*EE_COUNT)
    # Stock enters the original lookup. Private samples take the same EE playback paths.
    words = (iword(11,T7,T5,EE_COUNT),branch(5,T5,Z,13),sll(T5,T7,2),
             iword(15,Z,T6,hi),iword(9,T6,T6,lo),rword(T6,T5,T5,0x21),iword(35,T5,S1,0),
             branch(1,S1,Z,3),0,jump(0x203860),0,
             sll(S1,S1,1),rword(Z,S1,S1,2,1),jump(0x203810),0,
             0x3C0E003B,jump(0x203760),0x25CE4340)
    space.patch(hook,struct.pack('<18I',*words))
    space.patch(LOOKUP,struct.pack('<2I',jump(hook),0))

