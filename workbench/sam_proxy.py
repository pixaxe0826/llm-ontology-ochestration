"""Goal product SAM 3.1 proxy: one bounded image, fixed provider, per-image accounting."""
import base64,binascii,hashlib,io,json,math,re,time,uuid,warnings
from PIL import Image
from .goal_costs import number
from .product_images import MAX_IMAGE_BYTES,IMAGE_TYPES,image_bytes,image_type,data_image_size
from .response_data import field
from .security import clean_data,contains_key
from .workspace import Workspace

SAM_MODEL='sam-3.1'
SAM_REQUEST_BYTES=8*1024*1024
MAX_PIXELS=64*1024*1024
MAX_MASKS=256
MAX_OUTPUT_BYTES=8*1024*1024

def sam_image(goal,state,value):
    if not isinstance(value,str) or not value:raise ValueError('image must be one data URI or workspace-relative image path.')
    if contains_key(value):raise ValueError('SAM image input contains protected content.')
    path=None
    if value.startswith('data:'):
        data_image_size(value)
        header,encoded=value.split(',',1)
        try:data=base64.b64decode(encoded,validate=True)
        except (ValueError,binascii.Error):raise ValueError('Invalid SAM image data URI.') from None
        mime=image_type(data)
        if header.lower()!='data:'+mime+';base64':raise ValueError('SAM image MIME type does not match its signature.')
        source='data_uri'
    else:
        ws=Workspace(goal['workspace'],state);file=ws.path(value,True)
        if not file.is_file() or file.stat().st_size>MAX_IMAGE_BYTES:raise ValueError('SAM image exceeds 5 MiB or is not a file.')
        with file.open('rb') as image:data=image.read(MAX_IMAGE_BYTES+1)
        image_bytes(data);mime=image_type(data);source='workspace_path';path=file.relative_to(ws.root).as_posix()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error',Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                width,height=image.size
                if not 0<width*height<=MAX_PIXELS:raise ValueError('SAM decoded image exceeds the 64-megapixel safety limit.')
                if getattr(image,'is_animated',False):raise ValueError('SAM accepts one still image, not animated media.')
                if image.getexif().get(274,1) in (5,6,7,8):width,height=height,width
            with Image.open(io.BytesIO(data)) as verified:verified.verify()
    except ValueError:raise
    except Exception:raise ValueError('SAM image is invalid or could not be decoded safely.') from None
    metadata={'source':source,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest(),'width':width,'height':height,'content_type':mime}
    if path:metadata['path']=path
    return 'data:'+mime+';base64,'+base64.b64encode(data).decode('ascii'),metadata

def sam_text(response):
    if field(response,'status')!='completed':raise ValueError('SAM response did not complete; partial masks are not returned.')
    value=field(response,'output_text')
    if not isinstance(value,str):
        parts=[]
        for item in field(response,'output') or []:
            if field(item,'type')!='message':continue
            for block in field(item,'content') or []:
                if field(block,'type')=='refusal':raise ValueError('SAM provider refused the request.')
                if field(block,'type')=='output_text':parts.append(field(block,'text') or '')
        value=''.join(parts)
    if len(value.encode('utf-8'))>MAX_OUTPUT_BYTES:raise ValueError('SAM output exceeds the response size safety limit.')
    if contains_key(value):raise ValueError('SAM output contains protected content.')
    return value

def segmentation(response,image):
    from meta_sam_parser import image_segmentation_format,CompletedOutcome,decode_mask_to_rle
    text=sam_text(response)
    # The official parser decodes masks strictly. Bound dimensions BEFORE it allocates.
    tokens=re.findall(r'<\|mask;([^|]*)\|>',text);area=0
    if len(tokens)>MAX_MASKS:raise ValueError('SAM output exceeds the mask count safety limit.')
    for token in tokens:
        match=re.search(r'(?:^|;)\s*data\s*=\s*(\d+)\s*,\s*(\d+)\s*,',token)
        if not match or any(len(n)>10 for n in match.groups()):raise ValueError('SAM mask dimensions are invalid.')
        height,width=map(int,match.groups());area+=width*height
        if not 0<width<=image['width'] or not 0<height<=image['height'] or area>MAX_PIXELS:
            raise ValueError('SAM output exceeds the mask dimension/area safety limit.')
    parser=image_segmentation_format().create_parser();parser.push(text)
    result=parser.finish(CompletedOutcome()).result
    if any(d.severity=='error' for d in result.diagnostics):raise ValueError('SAM segmentation output failed format validation.')
    if any(record.kind=='text' and getattr(record,'text','').strip() for record in result.records):raise ValueError('SAM returned unexpected non-segmentation text.')
    boxes={record.object_id:record for record in result.records if record.kind=='box'};segments=[]
    for record in result.records:
        if record.kind!='mask':continue
        box=boxes.get(record.object_id)
        if box is None:raise ValueError('SAM returned a mask without a box.')
        bounds=[box.left,box.top,box.right,box.bottom]
        if not all(isinstance(n,(int,float)) and math.isfinite(n) for n in bounds) or not (0<=box.left<box.right<=image['width'] and 0<=box.top<box.bottom<=image['height']):raise ValueError('SAM box is outside the input image.')
        rle=decode_mask_to_rle(record.mask);score=record.confidence if record.confidence is not None else box.confidence
        segments.append({'id':record.object_id,'box':bounds,'score':score,
            'mask':{'encoding':'coco_rle','size':list(rle.size),'counts':rle.counts,'bounds':bounds}})
    value={'segments':segments,'box_format':'xyxy_half_open','mask_coordinates':'COCO column-major RLE of the cropped mask; place/resize it inside mask.bounds in source-image pixels.'}
    if len(json.dumps(value).encode())>MAX_OUTPUT_BYTES:raise ValueError('SAM decoded masks exceed the response size safety limit.')
    if contains_key(json.dumps(value)):raise ValueError('SAM output contains protected content.')
    return value

class SAMProxy:
    def proxy_sam(self,token,data):
        goal=self.proxy_goal(token)
        if not isinstance(data,dict) or set(data)-{'image','prompt'}:raise ValueError('SAM accepts only image and prompt; model and provider are fixed.')
        prompt=data.get('prompt')
        if not isinstance(prompt,str) or not 1<=len(prompt.strip())<=512 or contains_key(prompt):raise ValueError('SAM prompt must contain 1–512 characters without secrets; use one short noun phrase.')
        uri,image=sam_image(goal,self.store.root,data.get('image'))
        rate=number(self.ledger.prices.get('sam',{}).get('usd_per_thousand_images',2.50),'SAM image price')/1000
        def attempt(index,call_id):
            self.product_guard(goal)
            reservation=self.reserve(goal,'meta','product',rate,model=SAM_MODEL,role='Product',route='meta',workload='segmentation',
                proxy_operation='sam_segment',image_count=1,image_info=image,retry_index=index,model_call_id=call_id,
                image_unit_estimated_usd=rate,cost_estimated=True)
            try:response=self.providers.sam_segment(uri,prompt.strip(),min(90,max(.1,goal['deadline_epoch']-time.time())))
            except Exception:
                self.ledger.settle(reservation,error='SAM provider request failed; image usage unconfirmed, reserved estimate retained.')
                raise
            reported=field(field(response,'usage'),'images_processed')
            reported=reported if type(reported) is int and reported>=0 else None
            row=self.ledger.settle(reservation,actual_cost=rate,images_processed=1,provider_usage={'images_processed':reported},
                usage_source='provider' if reported==1 else 'one_input_image',sam_response_validated=False)
            self.product_guard(goal)
            return response,row
        response,row=self.model_retry(goal,'meta',attempt,role='Product',purpose='product')
        try:parsed=segmentation(response,image)
        except Exception:
            self.store.update('ledger',row['id'],sam_response_validated=False,response_error='SAM response validation failed; raw output omitted.')
            raise ValueError('SAM response validation failed; partial or malformed segmentation was not returned.') from None
        self.store.update('ledger',row['id'],sam_response_validated=True,segment_count=len(parsed['segments']))
        return clean_data({'id':'muse-sam-'+uuid.uuid4().hex,'object':'sam.segmentation','model':SAM_MODEL,'created':int(time.time()),
            'image':{key:image[key] for key in ('width','height','sha256')},**parsed,
            'usage':{'images_processed':1},'cost':{'provider':'meta','purpose':'product','estimated_usd':row['estimated_usd'],'ledger_id':row['id']}})
