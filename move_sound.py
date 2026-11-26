"""Resolve native BOM audio cues and copy PSX ADPCM tones into a fighter's PSP bank."""
import base64
import struct
import wave
import numpy as np

from audio_preview import find_sample, vag_wave
from fe3_formats import require
from formats import bom_parts, event_records, s16

EE_CUES = 0x3B4340
EE_COUNT = 787
PSP_TABLE = 0x089DCD50
PSP_CUES = (0x089DD324 - PSP_TABLE) // 4
VOICE = 0x80000000

# Native exclusive-voice branches at EE 0x203678 and PSP 0x08887A58.
# Each row is (bank and program, first tone, exclusive upper bound).
PSP_VOICES = ((0x701,0,6),(0x800,8,9),(0x801,0,6),(0x901,0,5),(0xA01,0,6),
              (0xB01,0,3),(0xC01,0,5),(0xD01,0,3),(0xE01,0,6),(0xF01,0,7),
              (0x1001,0,4),(0x1101,0,2),(0x1200,0,4),(0x1301,0,5),(0x1400,0,4),
              (0x1501,0,6),(0x1601,0,8),(0x1701,0,2))
EE_VOICES = ((0x301,0,6),(0xA01,0,6),(0xB01,0,5),(0x1301,0,6),(0x1B01,0,3),
             (0x1C01,0,1),(0x2001,0,5),(0x2801,0,3),(0x2901,0,4),(0x2A01,0,1),
             (0x2B01,0,5),(0x2C01,0,1),(0x2D01,0,2),(0x2E01,2,4),(0x2F01,0,3),
             (0x3001,0,2),(0x3101,0,3),(0x3200,0,3),(0x3300,0,6),(0x3C00,0,6),
             (0x4300,0,6),(0x4900,0,6),(0x4B00,0,6),(0x5200,0,6),(0x5500,0,6),
             (0x5600,0,5),(0x5601,0,6),(0x5700,0,9),(0x5701,1,4),(0x5800,0,8),
             (0x5900,0,8),(0x5A00,0,11),(0x5B00,0,4),(0x5C00,0,4),(0x5D00,0,5),
             (0x5E01,0,6),(0x5F01,0,5),(0x6000,0,5),(0x6101,0,5),(0x6200,0,7))


def voice_code(code, ee):
    return any(code >> 8 == group and first <= (code & 255) < end
               for group,first,end in (EE_VOICES if ee else PSP_VOICES))


class Vab:
    def __init__(self, data):
        require(len(data) >= 0xA20 and data[:4] == b'pBAV', '角色音库不是完整的 VAB。')
        self.data = data
        self.programs, self.tones, self.samples = struct.unpack_from('<3H',data,18)
        self.lengths = 0x820 + 512 * self.programs
        self.start = self.lengths + 512
        require(self.programs <= 128 and self.samples <= 255 and self.start <= len(data), 'VAB 音库目录越界。')
        units = struct.unpack_from(f'<{self.samples}H',data,self.lengths+2)
        self.ends = [self.start]
        for length in units:
            self.ends.append(self.ends[-1] + 8 * length)
        require(self.ends[-1] <= len(data), 'VAB 音色波形被截断。')

    def tone(self, program, tone):
        if not 0 <= program < self.programs or not 0 <= tone < min(16,self.data[32+16*program]):
            return None
        resolved = find_sample(self.data,program,tone)
        if not resolved:
            return None
        _,at,size,rate = resolved
        record = 0x820 + 512*program + 32*tone
        return (self.data[32+16*program:48+16*program], self.data[record:record+32],
                self.data[at:at+size], rate)

    def append(self, sample, owned):
        prog, attr, wave, _ = sample
        number = next((n for n in range(1,len(self.ends))
                       if self.data[self.ends[n-1]:self.ends[n]] == wave),None)
        add_sample = number is None
        if add_sample:
            number = self.samples + 1
            require(number <= 255 and 0 < len(wave) <= 65535*8 and len(wave)%16 == 0,
                    '目标角色的 VAB 已无空间容纳这个采样。')
        out = bytearray(self.data)
        index = next((n for n in sorted(owned) if n < self.programs and out[32+16*n] < 16
                      and out[33+16*n:48+16*n] == prog[1:]), self.programs)
        require(index < 128,'目标 VAB 已用满 128 个程序。')
        pos = 32+16*index
        if index == self.programs:
            out[pos:pos+16] = bytes([0])+prog[1:]
            out[self.lengths:self.lengths] = bytes(512)
            struct.pack_into('<H',out,18,index+1)
        slot = out[pos]
        out[pos] += 1
        offset = 0x820+512*index+32*slot
        out[offset:offset+32] = attr
        struct.pack_into('<2H',out,offset+20,index,number)
        struct.pack_into('<H',out,20,self.tones+1)
        if add_sample:
            extra = 512 if index == self.programs else 0
            struct.pack_into('<H',out,22,number)
            struct.pack_into('<H',out,self.lengths+extra+2*number,len(wave)//8)
            end = self.ends[-1]+extra
            out[end:end] = wave
        struct.pack_into('<I',out,12,len(out))
        return bytes(out), index, slot


def tone_key(sample):
    prog, attr, wave, _ = sample
    return prog[1:], attr[:20], attr[24:], wave


def wav_voice(path, template):
    """Encode a player's PCM recording at the selected native tone's actual pitch."""
    prog, attr, _, rate = template
    try:
        with wave.open(str(path),'rb') as file:
            channels, width, hz, count = file.getparams()[:4]
            require(channels in (1,2) and width in (1,2,3,4) and hz > 0 and count > 0,
                    '请选择单声道或双声道 PCM WAV。')
            samples = round(count*rate/hz)
            require(0 < samples <= (65535*8//16-2)*28, '这段录音超过游戏单个音色的长度上限。')
            data = file.readframes(count)
    except (wave.Error, EOFError) as error:
        raise ValueError('请选择完整的 PCM WAV 录音。') from error
    require(len(data) == channels*width*count, 'WAV 采样数据不完整。')
    if width == 3:
        b = np.frombuffer(data,np.uint8).reshape(-1,3).astype(np.int32)
        pcm = ((b[:,0] | b[:,1]<<8 | b[:,2]<<16) ^ 0x800000)-0x800000
        pcm = pcm / 256
    elif width == 1:
        pcm = (np.frombuffer(data,np.uint8).astype(float)-128)*256
    else:
        pcm = np.frombuffer(data,'<i2' if width==2 else '<i4').astype(float) / (1 if width==2 else 65536)
    pcm = pcm.reshape(-1,channels).mean(axis=1)
    pcm = np.interp(np.arange(samples)*hz/rate,np.arange(count),pcm).round().astype(int)
    pcm = np.pad(pcm,(0,-len(pcm)%28))
    result = bytearray(16)
    h1 = h2 = 0
    filters = ((0,0),(60,0),(115,-52),(98,-55),(122,-60))
    for i in range(0,len(pcm),28):
        block = pcm[i:i+28].tolist()
        older, prior = [h2,h1]+block[:-2], [h1]+block[:-1]
        candidates = [[n-((a*p+b*q+32)>>6) for n,p,q in zip(block,prior,older)] for a,b in filters]
        filt = min(range(5),key=lambda k:max(map(abs,candidates[k])))
        peak = max(map(abs,candidates[filt]))
        power = min(12,max(0,(max(1,(peak+6)//7)-1).bit_length()))
        step = 1 << power
        a,b = filters[filt]
        nibbles = []
        for n in block:
            prediction = (a*h1+b*h2+32)>>6
            nibble = min(7,max(-8,round((n-prediction)/step)))
            h2,h1 = h1,min(32767,max(-32768,prediction+nibble*step))
            nibbles.append(nibble & 15)
        result.extend(((filt<<4) | (12-power),1 if i+28 == len(pcm) else 0))
        result.extend(left | right<<4 for left,right in zip(nibbles[::2], nibbles[1::2]))
    result.extend(b'\0\x07'+bytes(14))
    return prog,attr,bytes(result),rate


def validate_extension(original, data):
    old, new = Vab(original), Vab(data)
    require(new.programs >= old.programs and new.samples >= old.samples
            and new.tones >= old.tones and struct.unpack_from('<I',data,12)[0] == len(data)
            and original[32:32+16*old.programs] == data[32:32+16*old.programs]
            and original[0x820:old.lengths] == data[0x820:old.lengths]
            and original[old.lengths:old.lengths+2+2*old.samples] == data[new.lengths:new.lengths+2+2*old.samples]
            and original[old.start:old.ends[-1]] == data[new.start:new.ends[old.samples]],
            '工程里的角色音库改动破坏了原有音色或波形。')
    for program in range(old.programs,new.programs):
        require(0 < data[32+16*program] <= 16 and all(new.tone(program,t) for t in range(data[32+16*program])),
                '工程中的新增 VAB 音色无效。')


class SoundLibrary:
    def __init__(self, game, role, config=0):
        self.game = game
        self.ee = hasattr(game,'workbench')
        self.role = role
        self.banks = {}
        if self.ee:
            tables = game.workbench.tables
            desc, sizes = tables.descriptor(role)
            self.actor_banks = tables.words(desc[18],sizes[11])
            self.codes = (*tables.words(EE_CUES,EE_COUNT),
                          *(word & ~VOICE for _,word in sorted(game.sound_extensions.items())))
        else:
            case = game.roles[role]['cases'][config]
            self.member = (case.get('voice_bank') or {}).get('id')
            self.actor_banks = (role+7,)
            self.codes = tuple(game.sound_code(i) for i in range(game.sound_count))

    def voice(self, cue):
        if cue >= (EE_COUNT if self.ee else PSP_CUES):
            return bool(self.game.sound_extensions[cue] & VOICE)
        return voice_code(self.codes[cue], self.ee)

    def bank(self, bank):
        if bank not in self.banks:
            if self.ee:
                pair = self.game.workbench.tables.at(0x42F5B8+32*bank,'<2i') if 0 <= bank < 140 else (-1,-1)
                if min(pair) < 0:
                    return None
                data = self.game.data((pair[0],-1)) + self.game.data((pair[1],-1))
            else:
                if bank in self.actor_banks:
                    member = self.member
                elif bank in (0,1):
                    member = self.game.yum['groups'][1][3] + bank
                elif bank == 6:
                    member = self.game.yum['groups'][2][3] + 1
                else:
                    return None
                if member is None:
                    return None
                data = self.game.read_member(member,b'pBAV')
            self.banks[bank] = Vab(data)
        return self.banks[bank]

    def sample(self, cue):
        require(type(cue) is int and 0 <= cue < len(self.codes),'声音编号超出原生索引。')
        if not self.ee and cue >= PSP_CUES and self.member not in self.game.sound_members.get(cue,()):
            return None
        code = self.codes[cue]
        bank = self.bank(code >> 16)
        return bank.tone(code >> 8 & 255,code & 255) if bank else None

    def voices(self):
        return [cue for cue,code in enumerate(self.codes) if code >> 16 in self.actor_banks
                and self.voice(cue) and self.sample(cue)]

    def recording_template(self):
        choices = self.voices()
        if choices:
            return self.sample(choices[0])
        for cue,code in enumerate(self.codes):
            if code >> 16 in self.actor_banks and (tone := self.sample(cue)):
                return tone
        return None

    def listen(self, cue):
        sample = self.sample(cue)
        require(sample is not None, '音库中找不到这个音色。')
        return 'data:audio/wav;base64,'+base64.b64encode(vag_wave(sample[2],sample[3])).decode('ascii')


def load_sound_members(game, rows, existing=None):
    """Resolve legacy PSP cue numbers to actual YUM banks before new tones can occupy those slots."""
    bindings = dict(existing or {})
    libraries = {}
    for cue,tagged in game.sound_extensions.items():
        role = ((tagged & ~VOICE) >> 16) - 7
        if role not in libraries:
            cases = game.roles[role]['cases']
            libraries[role] = {member:Vab(game.read_member(member,b'pBAV'))
                               for member in {(case.get('voice_bank') or {}).get('id') for case in cases} - {None}}
    for cue,members in rows:
        require(cue in game.sound_extensions and isinstance(members,(tuple,list))
                and all(type(i) is int for i in members),'工程中的声音所属音库无效。')
        code = game.sound_code(cue)
        allowed = libraries[(code >> 16)-7]
        require(set(bindings.get(cue,())) <= set(members) <= allowed.keys()
                and all(allowed[i].tone(code >> 8 & 255,code & 255) for i in members),
                '声音所属音库与当前出场配置不匹配。')
        bindings[cue] = tuple(sorted(set(members)))

    missing = game.sound_extensions.keys() - bindings.keys()
    if not missing:
        return bindings
    used = {cue:set() for cue in missing}
    for role,banks in libraries.items():
        wanted = {cue for cue in missing if game.sound_code(cue) >> 16 == role+7}
        if not wanted:
            continue
        for config,case in enumerate(game.roles[role]['cases']):
            member = (case.get('voice_bank') or {}).get('id')
            if member not in banks:
                continue
            masks = {}
            for shape in range(len(case['forms'])):
                bom = game.form_binding(role,config,shape)[2]['id']
                masks[bom] = masks.get(bom,0) | (1<<shape)
            for bom,mask in masks.items():
                count,_,parts = bom_parts(game.bom_content(bom))
                for script in parts[:count]:
                    if len(script) < 4 or struct.unpack_from('<I',script)[0] > (len(script)-4)//4:
                        continue
                    for record in event_records(script):
                        raw = record.raw
                        if len(raw) >= 6 and raw[0] in (14,15) and raw[1] & mask and s16(raw,4) in wanted:
                            used[s16(raw,4)].add(member)
        for cue in wanted:
            code = game.sound_code(cue)
            candidates = {i:tone_key(tone) for i,bank in banks.items()
                          if (tone := bank.tone(code >> 8 & 255,code & 255))}
            # Distinct waves in unused, identically numbered slots have no recoverable owner.
            members = used[cue].intersection(candidates) if used[cue] else (
                candidates.keys() if len(set(candidates.values())) == 1 else ())
            bindings[cue] = tuple(sorted(members))
    return bindings


class SoundTransfer:
    def __init__(self, origin, receiver):
        self.origin, self.receiver = origin,receiver
        self.extras = receiver.game.sound_extensions.copy()
        self.members = {} if receiver.ee else receiver.game.sound_members.copy()
        self.bank_edit = None
        self.lookup = {}
        self.samples = None

    def resolve(self, cue):
        if cue in self.lookup:
            return self.lookup[cue]
        src = self.origin
        sound = src.sample(cue)
        require(sound is not None,f'来源音效 {cue} 没有可读的原生采样。')
        self.lookup[cue] = self.store(sound,src.voice(cue))
        return self.lookup[cue]

    def store(self, sound, voice):
        dst = self.receiver
        if self.samples is None:
            self.samples = {}
            for i, code in enumerate(dst.codes):
                banks = (*dst.actor_banks,0) if dst.ee else (*dst.actor_banks,0,1,6)
                if code >> 16 in banks and (sample := dst.sample(i)):
                    self.samples.setdefault((dst.voice(i),tone_key(sample)),i)
        key = voice,tone_key(sound)
        if key not in self.samples:
            require(dst.actor_banks, '目标角色没有独立的战斗音库。')
            bank_id = dst.actor_banks[0]
            bank = dst.bank(bank_id)
            require(bank is not None,'目标角色形态没有可载入的语音库。')
            if dst.ee:
                head = dst.game.workbench.tables.pointer(0x42F5B8+32*bank_id,0)
                original = dst.game.base_data((head,-1))
            else:
                original = dst.game.read_source_member(dst.member,b'pBAV')
            native_programs, = struct.unpack_from('<H',original,18)
            owned = {(code >> 8) & 255 for cue,code in self.extras.items() if (code & ~VOICE) >> 16 == bank_id
                     and (dst.ee or dst.member in self.members.get(cue,()))
                     and ((code >> 8) & 255) >= native_programs}
            self.bank_edit,program,tone = bank.append(sound,owned)
            dst.banks[bank_id] = Vab(self.bank_edit)
            index = (EE_COUNT if dst.ee else PSP_CUES) + len(self.extras)
            require(index < 32768,'声音索引已达到原生上限。')
            self.extras[index] = (VOICE if voice else 0) | bank_id<<16 | program<<8 | tone
            if not dst.ee:
                self.members[index] = (dst.member,)
            self.samples[key] = index
        return self.samples[key]

