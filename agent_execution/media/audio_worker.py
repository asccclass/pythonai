import base64
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit


def main(request,result):
    value=json.loads(Path(request).read_text(encoding='utf-8'))
    try:
        from openai import OpenAI
        url=os.environ.get('AGENT_AUDIO_BASE_URL','');parsed=urlsplit(url)
        if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.scheme!='https' and not (parsed.scheme=='http' and parsed.hostname in {'127.0.0.1','localhost','::1'}):raise ValueError('Audio endpoint requires HTTPS or loopback HTTP')
        client=OpenAI(base_url=url,api_key=os.environ.get('AGENT_AUDIO_API_KEY',''),timeout=10,max_retries=0)
        try:
            if value['kind']=='stt':
                with Path(value['source']).open('rb') as source:
                    response=client.audio.transcriptions.create(model=os.environ['AGENT_STT_MODEL'],file=source)
                if not isinstance(response.text,str) or len(response.text)>65536:raise ValueError('Transcription size exceeded')
                output={'success':True,'text':response.text}
            else:
                data=bytearray()
                with client.audio.speech.with_streaming_response.create(model=os.environ['AGENT_TTS_MODEL'],input=value['text'],voice=value['voice'],response_format='wav') as response:
                    for chunk in response.iter_bytes():
                        data.extend(chunk)
                        if len(data)>2*1024*1024:raise ValueError('Speech response size exceeded')
                import io,wave
                with wave.open(io.BytesIO(data)) as audio:
                    if audio.getnframes()/audio.getframerate()>60:raise ValueError('Speech duration exceeds 60 seconds')
                output={'success':True,'mime':'audio/wav','audio_base64':base64.b64encode(data).decode('ascii')}
        finally:client.close()
    except Exception as error:output={'success':False,'error':type(error).__name__}
    Path(result).write_text(json.dumps(output,ensure_ascii=False),encoding='utf-8')


if __name__=='__main__':main(sys.argv[1],sys.argv[2])
