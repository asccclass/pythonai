"""Bounded encoded audio turns; the provider runs in a cancellable child process."""
from dataclasses import dataclass,field
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import wave
from ..process import run_process


def features():
    return {'stt':bool(os.environ.get('AGENT_STT_MODEL')),'tts':bool(os.environ.get('AGENT_TTS_MODEL')),
            'tts_voices':os.environ.get('AGENT_TTS_VOICES','alloy').split(',') if os.environ.get('AGENT_TTS_MODEL') else []}


@dataclass
class AudioTurn:
    stream_id:int
    format:str
    data:bytearray=field(default_factory=bytearray)
    sequence:int=0
    started:float=field(default_factory=time.monotonic)
    cancelled:threading.Event=field(default_factory=threading.Event)
    closed:bool=False
    def __post_init__(self):
        if type(self.stream_id) is not int or not 1<=self.stream_id<2**32 or self.format not in {'wav','mp3','webm'}:raise ValueError('Unsupported audio stream/format')
    def append(self,frame):
        if self.closed or len(frame)<9 or len(frame)>65545 or frame[0]!=2 or int.from_bytes(frame[1:5],'big')!=self.stream_id or int.from_bytes(frame[5:9],'big')!=self.sequence:
            self.cancelled.set();raise ValueError('Audio stream or sequence mismatch')
        if len(self.data)+len(frame)-9>2*1024*1024 or self.sequence>=256 or time.monotonic()-self.started>60:
            self.cancelled.set();raise ValueError('Audio capacity/deadline exceeded')
        self.data.extend(frame[9:]);self.sequence+=1
    def finish(self):
        self.closed=True
        if self.cancelled.is_set() or not self.data:raise ValueError('Audio stream is cancelled/empty')
        data=bytes(self.data)
        if self.format=='wav':
            with wave.open(io.BytesIO(data)) as audio:
                if audio.getnframes()/audio.getframerate()>60 or audio.getnchannels()>2:raise ValueError('WAV duration/channels exceeded')
        elif self.format=='webm' and not data.startswith(b'\x1aE\xdf\xa3'):raise ValueError('Invalid WebM encoding')
        elif self.format=='mp3' and not (data.startswith(b'ID3') or len(data)>1 and data[0]==255 and data[1]&224==224):raise ValueError('Invalid MP3 encoding')
        return data


def voice_call(kind,data=None,format='wav',text='',voice='alloy',cancelled=None):
    config=features()
    if kind not in {'stt','tts'} or not config[kind]:raise ValueError('Audio capability is not configured')
    if kind=='stt' and (not isinstance(data,bytes) or not 0<len(data)<=2*1024*1024 or format not in {'wav','mp3','webm'}):raise ValueError('Invalid audio input')
    if kind=='tts' and (not isinstance(text,str) or not 1<=len(text)<=4096 or voice not in config['tts_voices']):raise ValueError('Invalid speech text/voice')
    with tempfile.TemporaryDirectory(prefix='pythonai-audio-') as directory:
        request=Path(directory)/'request.json';result=Path(directory)/'result.json';audio=Path(directory)/('input.'+format)
        if data:audio.write_bytes(data)
        request.write_text(json.dumps({'kind':kind,'source':str(audio),'text':text,'voice':voice}),encoding='utf-8')
        completed=run_process([sys.executable,str(Path(__file__).with_name('audio_worker.py')),str(request),str(result)],timeout=30,max_output_bytes=65536,check_cancelled=cancelled)
        if completed.returncode or not result.exists() or result.stat().st_size>3*1024*1024:raise RuntimeError('Audio worker stopped: '+str(completed.returncode))
        value=json.loads(result.read_text(encoding='utf-8'))
        if not value.get('success'):raise RuntimeError(value.get('error','Audio request failed'))
        return value
