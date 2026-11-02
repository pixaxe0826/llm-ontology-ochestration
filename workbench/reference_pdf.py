"""Isolated PDF text/render worker. No credentials, shell, JavaScript, or network tools."""
import io,json,math,sys
from contextlib import closing
from pathlib import Path

TEXT_LIMIT=20000
IMAGE_LIMIT=5*1024*1024

def convert(source,output,validate_all=False):
    import pypdfium2 as pdfium
    parts=[];pages=[];used=0;truncated=False
    with pdfium.PdfDocument(source) as document:
        total=len(document)
        if total<1:raise ValueError('Empty PDF.')
        validation=[]
        if validate_all:
            if total>200:raise ValueError('PDF attachments are limited to 200 pages.')
            scanned=0
            for index in range(total):
                with closing(document[index]) as page:
                    with closing(page.get_textpage()) as textpage:text=textpage.get_text_bounded()
                scanned+=len(text)
                if scanned>2_000_000:raise ValueError('PDF text validation limit exceeded.')
                validation.append(text)
        for index in range(min(total,200)):
            with closing(document[index]) as page:
                with closing(page.get_textpage()) as textpage:
                    # Full Unicode support; the parent terminates this process on timeout.
                    text=textpage.get_text_bounded().replace('\r\n','\n').replace('\r','\n')
            label='[Page '+str(index+1)+']\n';remaining=TEXT_LIMIT-used
            if remaining<=len(label):truncated=True;break
            shown=(label+text+'\n\n')[:remaining];parts.append(shown);used+=len(shown)
            pages.append({'page':index+1,'characters':len(text),'included_characters':max(0,len(shown)-len(label))})
            if len(shown)<len(label+text+'\n\n'):truncated=True;break
        if len(pages)<total:truncated=True
        rendered=[]
        for index in range(min(total,4)):
            with closing(document[index]) as page:
                width,height=page.get_size()
                if not all(math.isfinite(v) and v>0 for v in (width,height)):raise ValueError('Invalid PDF page dimensions.')
                scale=min(2,2000/max(width,height))
                for attempt in range(5):
                    with closing(page.render(scale=scale)) as bitmap:
                        with bitmap.to_pil() as image:
                            image=image.convert('RGB');buffer=io.BytesIO();image.save(buffer,format='PNG');data=buffer.getvalue();size=image.size;image.close()
                    if len(data)<=IMAGE_LIMIT:break
                    scale*=.7
                else:raise ValueError('Rendered PDF page exceeds image size limit.')
                name='page-'+str(index+1)+'.png';(output/name).write_bytes(data)
                rendered.append({'page':index+1,'file':name,'width':size[0],'height':size[1],'bytes':len(data)})
        return {'content':''.join(parts),'total_pages':total,'text_pages':pages,'text_truncated':truncated,
            'rendered_pages':rendered,'ocr_run':False,**({'validation_text':'\n'.join(validation)} if validate_all else {})}

def main():
    if sys.argv[1]=='--stdin':
        try:
            data=sys.stdin.buffer.read(15*1024*1024+1)
            if len(data)>15*1024*1024:raise ValueError('PDF limit exceeded.')
            value=convert(data,Path(sys.argv[2]),validate_all=True)
        except Exception as error:value={'error':'PDF processing failed.','error_type':type(error).__name__}
        sys.stdout.buffer.write(json.dumps(value,ensure_ascii=False).encode('utf-8'))
        return 1 if value.get('error') else 0
    source=Path(sys.argv[1]);output=Path(sys.argv[2])
    try:value=convert(source,output)
    except Exception as error:value={'error':'PDF is invalid, encrypted, or could not be processed.','error_type':type(error).__name__}
    (output/'result.json').write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8')
    return 1 if value.get('error') else 0

if __name__=='__main__':raise SystemExit(main())
