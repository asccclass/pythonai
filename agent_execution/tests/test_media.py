from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import os
import io
import zipfile
import json
import base64
import wave
import asyncio
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from agent_execution.policy import execution_scope
from agent_execution.media.browser import browse
from agent_execution.media.browser_worker import allowed_url
from agent_execution.media.documents import parse_document
from agent_execution.media.audio import AudioTurn,voice_call


class BrowserTests(unittest.TestCase):
    def test_network_gate_rejects_local_and_unsafe_schemes(self):
        for url in ['file:///etc/passwd','http://user:secret@localhost/','http://127.0.0.1/']:
            with self.assertRaises((ValueError,PermissionError)):allowed_url(url,[])
        allowed_url('http://127.0.0.1/', ['127.0.0.1'])
    @unittest.skipUnless(os.environ.get('PYTHONAI_BROWSER_TEST')=='1','actual Chromium opt-in')
    def test_actual_profile_resume_isolation_assertions_and_download(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                self.send_response(200)
                if self.path=='/login':self.send_header('Set-Cookie','auth=verified; Path=/')
                if self.path=='/download':
                    self.send_header('Content-Disposition','attachment; filename=note.txt');self.end_headers();self.wfile.write(b'verified download');return
                self.send_header('Content-Type','text/html; charset=utf-8');self.end_headers()
                authenticated='auth=verified' in self.headers.get('Cookie','')
                self.wfile.write(('<body><p id="status">'+('verified' if authenticated else 'anonymous')+'</p><a id="download" href="/download">Download</a></body>').encode())
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'AGENT_BROWSER_ROOT':str(Path(directory)/'profiles'),
            'AGENT_WORKSPACE_ROOT':directory,'AGENT_BROWSER_ALLOWED_HOSTS':'127.0.0.1'}):
            url=f'http://127.0.0.1:{server.server_port}'
            browse(url+'/login',[])
            result=browse(url+'/protected',[{'action':'assert_text','selector':'#status','text':'verified'}, {'action':'download','selector':'#download'}])
            self.assertEqual(Path(result['artifacts'][0]).read_bytes(),b'verified download')
            with execution_scope(identity='another-user'):
                other=browse(url+'/protected',[{'action':'assert_text','selector':'#status','text':'anonymous'}])
                self.assertIn('anonymous',other['text'])
            with self.assertRaises(RuntimeError):browse(url+'/protected',[{'action':'assert_text','selector':'#status','text':'wrong'}])


class DocumentTests(unittest.TestCase):
    def archive(self,files):
        output=io.BytesIO()
        with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as archive:
            for name,data in files.items():archive.writestr(name,data)
        return output.getvalue()
    def test_native_docx_pptx_and_xlsx_source_attribution(self):
        data=self.archive({'word/document.xml':'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>中文來源</w:t></w:r></w:p></w:body></w:document>'})
        result=parse_document(data,'Chinese.docx');self.assertTrue(result['success']);self.assertEqual(result['units'][0]['source'],'paragraph:1');self.assertEqual(result['units'][0]['text'],'中文來源')
        data=self.archive({'ppt/slides/slide2.xml':'<a xmlns:t="text"><t:t>Slide text</t:t></a>'})
        self.assertEqual(parse_document(data,'deck.pptx')['units'][0]['source'],'slide:2')
        from openpyxl import Workbook
        workbook=Workbook();workbook.active.title='中文';workbook.active.append(['原始資料',42]);output=io.BytesIO();workbook.save(output);workbook.close()
        row=parse_document(output.getvalue(),'table.xlsx')['units'][0];self.assertEqual(row['source'],'sheet:中文/row:1');self.assertIn('原始資料',row['text'])
    def test_archive_bombs_entities_invalid_documents_and_binary_fallback(self):
        self.assertFalse(parse_document(self.archive({'word/document.xml':'x'*(9*1024*1024)}),'bomb.docx')['success'])
        self.assertFalse(parse_document(self.archive({'word/document.xml':'<!DOCTYPE x [<!ENTITY a SYSTEM "file:///private">]><x>&a;</x>'}),'entity.docx')['success'])
        self.assertFalse(parse_document(b'not a pdf','bad.pdf')['success'])
        from agent_runtime import attachment_content_parts
        from types import SimpleNamespace
        result=attachment_content_parts(SimpleNamespace(filename='unknown.bin',content_type='application/octet-stream',data=b'private binary'))
        self.assertNotIn('Base64',result[0]['text']);self.assertNotIn('cHJpdmF0',result[0]['text'])
    def test_real_scanned_pdf_ocr_has_page_source(self):
        import shutil
        if not shutil.which('tesseract'):self.skipTest('Tesseract not installed')
        from PIL import Image,ImageDraw,ImageFont
        image=Image.new('RGB',(900,300),'white');font=ImageFont.truetype('arial.ttf',64)
        ImageDraw.Draw(image).text((40,80),'HELLO 123',fill='black',font=font)
        output=io.BytesIO();image.save(output,format='PDF')
        result=parse_document(output.getvalue(),'scan.pdf');self.assertTrue(result['success']);self.assertEqual(result['units'][0]['source'],'page:1')
        self.assertTrue(result['units'][0]['ocr']);self.assertIn('HELLO',result['units'][0]['text'])


def wav_bytes():
    output=io.BytesIO()
    with wave.open(output,'wb') as sound:
        sound.setnchannels(1);sound.setsampwidth(2);sound.setframerate(16000);sound.writeframes(b'\0\0'*1600)
    return output.getvalue()


class AudioTests(unittest.TestCase):
    def test_stream_identity_sequence_capacity_and_encoded_wav(self):
        turn=AudioTurn(3,'wav');data=wav_bytes();turn.append(bytes([2])+int(3).to_bytes(4,'big')+int(0).to_bytes(4,'big')+data)
        self.assertEqual(turn.finish(),data)
        turn=AudioTurn(3,'wav')
        with self.assertRaises(ValueError):turn.append(bytes([2])+int(4).to_bytes(4,'big')+int(0).to_bytes(4,'big')+data)
        self.assertTrue(turn.cancelled.is_set())
    def test_real_sdk_http_transcription_speech_and_cancelled_worker(self):
        seen=[];audio=wav_bytes()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                body=self.rfile.read(int(self.headers['Content-Length']));seen.append((self.path,body))
                self.send_response(200)
                if self.path.endswith('/transcriptions'):
                    result=json.dumps({'text':'辨識文字'},ensure_ascii=False).encode();self.send_header('Content-Type','application/json')
                else:result=audio;self.send_header('Content-Type','audio/wav')
                self.send_header('Content-Length',str(len(result)));self.end_headers();self.wfile.write(result)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        with patch.dict(os.environ,{'AGENT_AUDIO_BASE_URL':f'http://127.0.0.1:{server.server_port}/v1','AGENT_AUDIO_API_KEY':'fixture',
            'AGENT_STT_MODEL':'fixture-stt','AGENT_TTS_MODEL':'fixture-tts','AGENT_TTS_VOICES':'alloy'}):
            self.assertEqual(voice_call('stt',data=audio)['text'],'辨識文字')
            result=voice_call('tts',text='測試');self.assertEqual(result['mime'],'audio/wav');self.assertEqual(base64.b64decode(result['audio_base64']),audio)
            with self.assertRaises(RuntimeError):voice_call('stt',data=audio,cancelled=lambda:True)
        self.assertEqual(len(seen),2);self.assertIn(b'fixture-stt',seen[0][1])
    def test_websocket_cancel_suppresses_old_turn_result(self):
        import websocket_server as module
        async def scenario():
            session=module.Session('device');turn=AudioTurn(1,'wav');session.audio_turn=turn
            def fake(*args,**kwargs):turn.cancelled.set();return {'success':True,'text':'stale'}
            with patch('agent_execution.media.audio.voice_call',side_effect=fake):await module.audio_result(session,turn,'stt','ref',data=wav_bytes())
            self.assertEqual(len(session.outbox),0)
        asyncio.run(scenario())
