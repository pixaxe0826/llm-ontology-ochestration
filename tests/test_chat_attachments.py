"""Offline chat uploads: real image/PDF decoding, fake model transport, no cloud calls."""
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from PIL import Image
from test_reference_pages import pdf_fixture
from test_goals import response
from workbench.chat_attachments import ChatAttachments, MIB
from workbench.agent import Agents
from workbench.memory import Memory
from workbench.providers import Providers, BASETEN_MODEL
from workbench.security import register_secret
from workbench.server import Application, LocalHTTPServer, make_handler
from workbench.store import Store


def picture(kind='PNG'):
    out=io.BytesIO();Image.new('RGB',(24,24),'red').save(out,format=kind);return out.getvalue()


class AttachmentTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=Store(Path(self.temp.name)/'state')
        self.attach=ChatAttachments(self.store);self.ident=self.attach.conversation()['conversation_id']
    def tearDown(self):
        self.store.db.close();self.temp.cleanup()
    def upload(self,name='red.png',data=None):
        return self.attach.upload(self.ident,name,picture() if data is None else data)
    def test_original_hash_and_metadata_only_and_inflate_without_mutation(self):
        item=self.upload();descriptors=self.attach.prepare(self.ident,[item],'meta')
        self.assertEqual(set(descriptors[0]),{'name','size','sha256'})
        self.assertEqual(self.attach.read_hash(self.ident,item['sha256']),picture())
        messages=[{'role':'user','content':'Describe.','attachments':descriptors}];before=copy.deepcopy(messages)
        actual,counts=self.attach.inflate(self.ident,messages)
        self.assertEqual(messages,before);self.assertEqual(counts,{'image_count':1,'attachment_count':1})
        self.assertNotIn('attachments',actual[0]);self.assertTrue(actual[0]['content'][-1]['image_url']['url'].startswith('data:image/png;base64,'))
    def test_supported_images_and_gif_first_frame(self):
        for extension,kind in [('png','PNG'),('jpg','JPEG'),('webp','WEBP'),('gif','GIF')]:
            with self.subTest(extension=extension):
                item=self.upload('test.'+extension,picture(kind));self.assertEqual(item['image_count'],1)
                if kind=='GIF':self.assertTrue(item['gif_first_frame'])
    def test_invalid_signature_extension_corruption_and_pixel_cap(self):
        for name,data in [('x.png',b'not an image'),('x.jpg',picture()),('x.png',b'\x89PNG\r\n\x1a\nbroken')]:
            with self.subTest(name=name,data=data[:10]):
                with self.assertRaises(ValueError):self.upload(name,data)
    def test_size_limits_image_text_pdf(self):
        for name,data in [('big.png',b'\x89PNG\r\n\x1a\n'+b'x'*(5*MIB)),('big.txt',b'x'*200001),('big.pdf',b'%PDF-'+b'x'*(15*MIB))]:
            with self.subTest(name=name):
                with self.assertRaises(ValueError):self.upload(name,data)
    def test_text_utf8_contents_and_name_are_ephemeral(self):
        item=self.upload('코드.py','print("첨부 전용 표식")\n'.encode())
        descriptors=self.attach.prepare(self.ident,[item],'meta')
        actual,counts=self.attach.inflate(self.ident,[{'role':'user','content':'Read','attachments':descriptors}])
        self.assertIn('File: 코드.py\nprint("첨부 전용 표식")',actual[0]['content'][1]['text'])
        self.assertEqual(counts['image_count'],0)
        self.assertNotIn('첨부 전용 표식',json.dumps(self.store.items('attachment'),ensure_ascii=False))
    def test_utf8_only_binary_and_secret_file_rejected_before_persistence(self):
        secret='attachment-synthetic-secret-231755';register_secret(secret)
        for name,data in [('.env',b'example'),('x.key',b'sample'),('x.txt',secret.encode()),('bad.txt',b'\xff\xfe'),('bin.txt',b'abc\x00def'),('no.exe',b'x')]:
            with self.subTest(name=name):
                with self.assertRaises(ValueError):self.upload(name,data)
        self.assertFalse(self.attach.folder(self.ident).exists())
    def test_pdf_text_pages_and_first_four_images(self):
        item=self.upload('reference.pdf',pdf_fixture(5));self.assertEqual(item['image_count'],4);self.assertEqual(item['total_pages'],5)
        messages,counts=self.attach.inflate(self.ident,[{'role':'user','content':'read','attachments':self.attach.prepare(self.ident,[item],'meta')}])
        self.assertIn('[Page 1]',messages[0]['content'][1]['text']);self.assertEqual(counts['image_count'],4)
    def test_pdf_extracted_secret_rejected_before_original_is_saved(self):
        secret='Conveyor reference';register_secret(secret)
        try:
            with self.assertRaisesRegex(ValueError,'비밀'):self.upload('private.pdf',pdf_fixture())
            self.assertFalse(self.attach.folder(self.ident).exists())
        finally:
            from workbench import security
            with security._secret_lock:security._ephemeral.discard(secret)
    def test_combined_message_image_page_limit_and_ten_files(self):
        pdf=self.upload('four.pdf',pdf_fixture(4));image=self.upload()
        with self.assertRaisesRegex(ValueError,'4장'):self.attach.prepare(self.ident,[pdf,image],'meta')
        with self.assertRaisesRegex(ValueError,'10개'):self.attach.prepare(self.ident,[image]*11,'meta')
    def test_scope_metadata_tamper_and_content_tamper(self):
        item=self.upload();other=self.attach.conversation()['conversation_id']
        with self.assertRaises(ValueError):self.attach.prepare(other,[item],'meta')
        with self.assertRaises(ValueError):self.attach.prepare(self.ident,[{**item,'name':'other.png'}],'meta')
        (self.attach.folder(self.ident)/item['sha256']).write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'해시'):self.attach.prepare(self.ident,[item],'meta')
    def test_path_traversal_and_malformed_ident_are_rejected(self):
        for name in ('../x.txt','x/y.txt','x\\y.txt','x\r\n.txt'):
            with self.assertRaises(ValueError):self.upload(name,b'text')
        with self.assertRaises(ValueError):self.attach.upload('../outside','x.txt',b'x')
    def test_delete_conversation_removes_originals_and_keeps_unrelated_files(self):
        self.upload();other=self.store.root/'preserve.txt';other.write_text('keep')
        self.attach.delete(self.ident)
        self.assertFalse((self.store.root/'conversations'/self.ident).exists());self.assertTrue(other.is_file())
        self.assertEqual(self.store.items('attachment'),[])
    def test_pdf_timeout_is_bounded_and_original_not_persisted(self):
        import subprocess
        with patch('workbench.chat_attachments.subprocess.run',side_effect=subprocess.TimeoutExpired('pdf',45)):
            with self.assertRaises(subprocess.TimeoutExpired):self.upload('timeout.pdf',pdf_fixture())
        self.assertFalse(self.attach.folder(self.ident).exists())
    def test_baseten_explicit_unsupported_blocks_images_but_allows_text(self):
        provider=Providers(self.store)
        class Unsupported(Exception):status_code=400
        self.assertIsNone(provider.image_error('baseten',Unsupported('Invalid model'),True))
        self.assertEqual(provider.image_error('baseten',Unsupported('Images are not supported by this model'),True),'이 공급자는 이미지 미지원')
        self.assertFalse(provider.settings()['baseten']['vision_supported'])
        with self.assertRaisesRegex(ValueError,'이미지 미지원'):self.attach.prepare(self.ident,[self.upload()],'baseten')
        self.attach.prepare(self.ident,[self.upload('ok.txt',b'hello')],'baseten')
    def test_attachment_context_does_not_enter_shared_memory(self):
        memory=Memory(self.store)
        record={'id':self.ident,'status':'succeeded','provider':'baseten','title':'Private attachment','share_memory':True,'attachment_context_start':2,
            'events':[{'kind':'user','text':'ordinary earlier'},{'kind':'assistant','text':'earlier public answer'},
                {'kind':'user','text':'inspect','attachments':[self.upload('secret.txt',b'unique-original-marker')]},
                {'kind':'assistant','text':'unique-original-marker quoted'}, {'kind':'change','change':{'path':'x.py','diff':'unique-original-marker'}}]}
        self.store.save('task',record);memory.sync('task',record)
        self.assertEqual(memory.search('unique-original-marker'),[])
        self.assertTrue(memory.search('earlier public answer'))
        source=next(s for s in memory.graph()['sources'] if s.get('attachment_related'))
        self.assertFalse(source['shared'])
        with self.assertRaisesRegex(ValueError,'첨부 내용'):memory.share(source['id'],True)
        memory.share(source['id'],True,attachment_confirmed=True)
        self.assertTrue(memory.search('unique-original-marker'))
        self.assertTrue(self.store.get('task',self.ident)['events'][3]['shared'])
        memory.sync('task',self.store.get('task',self.ident))
        self.assertTrue(memory.source(source['id'])['shared'])


class AttachmentHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.folder=Path(self.temp.name)/'workspace';self.folder.mkdir()
        self.app=Application(Path(self.temp.name)/'state')
        self.server=LocalHTTPServer(('127.0.0.1',0),make_handler(self.app));self.url='http://127.0.0.1:'+str(self.server.server_address[1])
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.app.stop();self.app.store.db.close();self.temp.cleanup()
    def post(self,path,data,**headers):
        binary=isinstance(data,bytes)
        request=Request(self.url+path,data=data if binary else json.dumps(data).encode(),headers={'Content-Type':'application/octet-stream' if binary else 'application/json','X-Local-Token':self.app.token,**headers})
        with urlopen(request) as reply:return json.load(reply)
    def test_upload_requires_session_and_origin(self):
        for headers in ({'X-Local-Token':'wrong'},{'Origin':'https://outside.example'}):
            with self.assertRaises(HTTPError) as caught:self.post('/api/attachments/upload',picture(),**headers)
            self.assertEqual(caught.exception.code,403);caught.exception.close()
    def test_meta_work_and_baseten_stream_keep_history_metadata_and_record_image_usage(self):
        settings=self.app.providers.settings()
        for p in settings.values():p['ready']=True
        captured=[]
        def complete(provider,messages,tools,model):
            captured.append((provider,copy.deepcopy(messages),model));return response('red and UTF8 attachment')
        def stream(provider,messages,model):
            captured.append((provider,copy.deepcopy(messages),model))
            yield {'delta':'red and UTF8 attachment','finish_reason':'stop','usage':{'prompt_tokens':500,'completion_tokens':20,'total_tokens':520}}
        with patch.object(self.app.providers,'settings',return_value=settings),patch.object(self.app.providers,'completion',side_effect=complete),patch.object(self.app.providers,'stream_chat',side_effect=stream):
            for provider,mode in [('meta','work'),('baseten','chat')]:
                ident=self.post('/api/attachments/conversation',{})['conversation_id']
                image=self.post('/api/attachments/upload',picture(),**{'X-Conversation-Id':ident,'X-File-Name':'red.png'})
                text=self.post('/api/attachments/upload',b'unique-input-only-773',**{'X-Conversation-Id':ident,'X-File-Name':'notes.txt'})
                task=self.post('/api/task',{'root':str(self.folder),'prompt':'Describe attachments.','mode':mode,'provider':provider,'model':BASETEN_MODEL if provider=='baseten' else 'muse-spark-1.3','conversation_id':ident,'attachments':[image,text]})
                for _ in range(100):
                    if ident not in self.app.agents.active:break
                    time.sleep(.02)
                saved=self.app.store.get('task',ident);self.assertEqual(task['id'],ident);self.assertEqual(saved['status'],'succeeded',saved.get('error'))
                self.assertNotIn('unique-input-only-773',json.dumps(saved));self.assertNotIn('data:image',json.dumps(saved))
                self.assertEqual(set(saved['messages'][1]['attachments'][0]),{'name','size','sha256'})
                ledger=[r for r in self.app.store.items('ledger') if r.get('task_id')==ident];self.assertEqual(ledger[0]['image_count'],1)
        self.assertEqual(len(captured),2);self.assertEqual(captured[1][2],BASETEN_MODEL)
        for _,messages,_ in captured:
            self.assertTrue(any(part.get('type')=='image_url' for m in messages if isinstance(m['content'],list) for part in m['content']))
    def test_duplicate_conversation_cannot_replace_existing_task(self):
        ident=self.post('/api/attachments/conversation',{})['conversation_id']
        value=self.post('/api/attachments/upload',b'hello',**{'X-Conversation-Id':ident,'X-File-Name':'hello.txt'})
        self.app.store.save('task',{'id':ident,'status':'succeeded','title':'existing','messages':[],'events':[]})
        settings=self.app.providers.settings();settings['baseten']['ready']=True
        with patch.object(self.app.providers,'settings',return_value=settings):
            with self.assertRaises(HTTPError) as caught:self.post('/api/task',{'root':'','prompt':'hello','mode':'chat','provider':'baseten','conversation_id':ident,'attachments':[value]})
            self.assertEqual(caught.exception.code,400);caught.exception.close()
        self.assertEqual(self.app.store.get('task',ident)['title'],'existing')
    def test_explicit_baseten_vision_rejection_disables_later_sends(self):
        class Unsupported(Exception):status_code=400
        ident=self.post('/api/attachments/conversation',{})['conversation_id']
        image=self.post('/api/attachments/upload',picture(),**{'X-Conversation-Id':ident,'X-File-Name':'red.png'})
        original=self.app.providers.settings
        def settings():
            values=original();values['baseten']['ready']=True;return values
        def stream(*args):
            raise Unsupported('image input is not supported')
            yield
        with patch.object(self.app.providers,'settings',side_effect=settings),patch.object(self.app.providers,'stream_chat',side_effect=stream) as calls:
            self.post('/api/task',{'root':'','prompt':'Describe.','mode':'chat','provider':'baseten','conversation_id':ident,'attachments':[image]})
            for _ in range(100):
                if ident not in self.app.agents.active:break
                time.sleep(.02)
            task=self.app.store.get('task',ident);self.assertEqual(task['error'],'이 공급자는 이미지 미지원')
            self.assertFalse(settings()['baseten']['vision_supported']);self.assertEqual(calls.call_count,1)
        with self.assertRaises(ValueError):self.app.attachments.prepare(ident,[image],'baseten')


if __name__=='__main__':unittest.main()
