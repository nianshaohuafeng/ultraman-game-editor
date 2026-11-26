"""Extend only the v7 BOM sound lookup, above installed move and AI layers."""
import struct
from ai_eboot import finish_layer
from move_eboot import HI, LO, Eboot, Z, A0, A1, A2, branch, compiled_layout, iword, lw, payload_limit, rword, sll
from move_sound import PSP_CUES, PSP_TABLE, VOICE
from formats import u32

MARKER = b'KGFSND\x01'
LOAD = 0x08887A98
CLASSIFY = 0x08887AAC
S0, S1 = 16,17


def read_codes(rows):
    if not isinstance(rows,list) or len(rows) > 32768-PSP_CUES:
        raise ValueError('扩展声音索引数量无效。')
    result = {}
    for i,value in enumerate(rows,PSP_CUES):
        if type(value) != int or not 0 <= value <= 0xffffffff:
            raise ValueError('扩展声音编码无效。')
        code = value & ~VOICE
        if not (7 <= code >> 16 < 24 and (code >> 8 & 255) < 128 and (code & 255) < 16):
            raise ValueError('扩展声音引用了无效角色音库或音色。')
        result[i] = value
    return result


def install_sound(raw, layout, canonical, codes, members=None):
    limit = payload_limit(canonical,layout)
    builder = Eboot(raw,compiled_layout(raw,layout),limit)
    rows = [codes[i] for i in range(PSP_CUES,PSP_CUES+len(codes))]
    read_codes(rows)
    at = builder.locate(PSP_TABLE)[2]
    table = builder.allocate(raw[at:at+4*PSP_CUES]+struct.pack(f'<{len(rows)}I',*rows))
    segment, relative, _ = builder.locate(table)
    for site,kind,operand,signature in ((LOAD,HI,(relative+32768)>>16,0x3C0A),(LOAD+4,LO,relative,0x2549)):
        pos = builder.locate(site)[2]
        original = u32(raw,pos)
        relocs = builder.relocs[site]
        if original >> 16 != signature or len(relocs)!=1 or relocs[0][1] & 15 != kind:
            raise ValueError('v7 声音索引的原生装载指令已变化。')
        where,info = relocs[0]
        builder.write_raw(where+4,struct.pack('<I',(info & 0xff00ffff) | segment<<16))
        builder.word(site,original & 0xffff0000 | (operand & 65535))
    if struct.unpack_from('<2I',raw,builder.locate(CLASSIFY)[2]) != (0x3C06000F,0x34C40106):
        raise ValueError('v7 的声音分派入口已变化。')
    # Private voices use the actor's own interruptible handle; stock and one-shot effects use the native switch.
    hook = builder.code([branch(1,S0,1,5),iword(15,Z,A2,15),sll(S0,S0,1),rword(Z,S0,S0,2,1),
                         0,lw(A1,0xF0,S1),0,iword(13,A2,A0,0x106)])
    builder.redirect(hook+16,0x08887B94)
    builder.redirect(hook+24,CLASSIFY+8)
    builder.redirect(CLASSIFY,hook)
    builder.word(CLASSIFY+4,0)
    fields = {'sound_members':sorted(members.items())} if members else {}
    return finish_layer(builder,raw,canonical,limit,MARKER,rows,**fields)

