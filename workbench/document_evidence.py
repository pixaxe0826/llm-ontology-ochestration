"""Read-only document checks grounded in current same-Goal observations, never new runs."""
from decimal import Decimal,InvalidOperation
import csv,io,json,re,tomllib
from pathlib import Path
from .acceptance_rules import rule_for,has_documents
from .review_evidence import observations,EXECUTION_KINDS
from .structured_files import load_yaml,json_value
from .workspace import Workspace
from .document_roles import role_for,document_path,recognize_license,LICENSES
from .spec_citations import SPEC,add_spec_sources
from .document_numbers import compare

SUCCESS=re.compile(r'\b(?:PASS(?:ED)?|SUCCESS(?:FUL(?:LY)?)?|SUCCEEDED|COMPLETED?|DONE|VERIFIED|ACCEPTED)\b|성공|완료|통과|검증됨',re.I)
NEGATED=re.compile(r'\b(?:not|never|no)\s+(?:passed|successful|success|succeeded|complete|completed|done|verified)\b|미완료|미검증|성공하지\s*않|통과하지\s*않|(?:success|completed|verified)\s*[:=]\s*false',re.I)
PARENS=re.compile(r'\(([^()\n]{1,1800})\)')
TOKEN=re.compile(r'(?:observation|ledger):[A-Za-z0-9_-]+|[A-Za-z0-9_-]+:\d+(?:/output:[^\s,;()]+)?')
NUMBER=r'\d[\d,]*(?:\.\d+)?'
DATA_FORMATS={'.json':'json','.yaml':'yaml','.yml':'yaml','.toml':'toml','.csv':'csv'}
REFERENCE_KINDS=('reference_search','fetch_reference_page','fetch_reference_image','analyze_images')
UNIT_ALIASES={'inches':'in','inch':'in','metre':'m','metres':'m','meter':'m','meters':'m','degree':'deg','degrees':'deg'}
UNIT_FAMILIES={**dict.fromkeys(('mm','cm','m','in','ft'),'length'),'kg':'mass','deg':'angle','fpm':'speed','psi':'pressure','%':'ratio'}
RESULT_CLAIM=re.compile(r'\b(?:measured|measurement|achieved|benchmark|accuracy|precision|recall|latency|throughput|performance|actual|observed test|exit[_ ]?code)\b|실측|측정|성능|정확도|실행 결과|테스트.*통과',re.I)


class DocumentEvidenceError(ValueError):
    def __init__(self,defects):
        self.defects=list(dict.fromkeys(defects));super().__init__('Document evidence requires rework: '+'; '.join(self.defects[:8]))


def goal_sources(manager,goal,item):
    refs=observations(manager,goal,item,owners=manager.items(goal['id']))
    for row in manager.ledger.rows(goal['id']):
        if row.get('goal_id')!=goal['id']:continue
        identifier='ledger:'+row['id'];settled=row.get('state')=='settled'
        refs[identifier]={'id':identifier,'kind':'ledger_fact','source_plan_item_id':None,'stale':not settled,
            'stale_reason':'Settled accounting record (estimated cost, not invoice verification).' if settled else 'Accounting is not settled.',
            'supports_acceptance':settled,'ledger_id':row['id'],'_ledger':row}
    return refs


def citations(text,known):
    ids=[]
    for group in PARENS.findall(text):
        ids.extend(m.group() for m in SPEC.finditer(group))
        matches=[identifier for identifier in known if identifier in group]
        spans=[]
        for identifier in sorted(matches,key=len,reverse=True):
            start=group.find(identifier);end=start+len(identifier)
            if not any(a<=start and end<=b for a,b in spans):ids.append(identifier);spans.append((start,end))
        for match in TOKEN.finditer(group):
            if any(a<=match.start() and match.end()<=b for a,b in spans):continue
            token=match.group();prefix=token.split(':')[0]
            if prefix.isdecimal() and len(prefix)<8:continue  # A time such as (10:11) is not an evidence ID.
            ids.append(token)
    return list(dict.fromkeys(ids))


def numeric(value):
    try:return Decimal(str(value).replace(',',''))
    except (InvalidOperation,ValueError):return None


def metrics(text):
    result=[]
    def add(kind,raw,label=''):
        value=numeric(raw)
        if value is not None:result.append({'kind':kind,'value':str(value),'raw':str(value),'precision':max(0,-value.as_tuple().exponent),'label':label})
    for match in re.finditer(r'('+NUMBER+r')\s+(?:tests?\s+)?(passed|failed|skipped|errors?)\b',text,re.I):add('tests_'+match[2].lower().rstrip('s'),match[1])
    for match in re.finditer(r'\b(?:tests?[_ ]*)?(passed|failed|skipped|total|run)["\']?\s*[:=]\s*('+NUMBER+r')',text,re.I):add('tests_'+('total' if match[1].lower()=='run' else match[1].lower()),match[2])
    for match in re.finditer(r'\btests?["\']?\s*[:=]\s*('+NUMBER+r')',text,re.I):add('tests',match[1])
    for match in re.finditer(r'(?:\bRan\s+|\btotal\s*[:=]?\s*)('+NUMBER+r')\s+tests?\b',text,re.I):add('tests_total',match[1])
    for match in re.finditer(r'('+NUMBER+r')\s*(?:tests?\b|개(?:의)?\s*테스트|건(?:의)?\s*테스트)',text,re.I):
        if not re.search(r'\b(?:passed|failed|skipped|errors?)\b',text[match.end():match.end()+14],re.I):add('tests',match[1])
    for match in re.finditer(r'테스트\s*('+NUMBER+r')\s*(?:개|건)?',text):add('tests',match[1])
    for match in re.finditer(r'(?:\$|\bUSD\s*)('+NUMBER+r')|('+NUMBER+r')\s*USD\b',text,re.I):add('cost',match[1] or match[2])
    for match in re.finditer(r'\b(?:estimated_usd|cost_usd|total_cost_usd_estimated)["\']?\s*[:=]\s*('+NUMBER+r')',text,re.I):add('cost',match[1])
    for match in re.finditer(r'\b(duration_s|wall_seconds|sim_wall_seconds|render_wall_seconds|simulated_seconds)["\']?\s*[:=]\s*('+NUMBER+r')',text,re.I):add('simulated_seconds' if match[1].lower()=='simulated_seconds' else 'wall_seconds',match[2])
    for match in re.finditer(r'('+NUMBER+r')\s*(milliseconds?|ms|seconds?|secs?|s|minutes?|mins?|hours?|초|분|시간)(?![A-Za-z])',text,re.I):
        unit=match[2].lower();value=numeric(match[1]);factor=Decimal('.001') if unit in ('ms','millisecond','milliseconds') else Decimal(60) if unit in ('min','mins','minute','minutes','분') else Decimal(3600) if unit in ('hour','hours','시간') else Decimal(1)
        context=text[max(0,match.start()-45):match.end()+20].lower()
        kind='simulated_seconds' if re.search(r'simulated|시뮬레이션\s*시간',context) else 'wall_seconds' if re.search(r'wall|runtime|actual|실제|측정|실행\s*시간',context) else 'seconds'
        result.append({'kind':kind,'value':str(value*factor),'raw':str(value),'precision':max(0,-value.as_tuple().exponent),'scale':str(factor),'label':match[0]})
    for match in re.finditer(r'\b([a-fA-F0-9]{64})\b|\b([a-fA-F0-9]{8,63})(?:…|\.{3})',text):
        result.append({'kind':'hash','value':(match[1] or match[2]).lower(),'prefix':bool(match[2])})
    unit_metrics=[]
    for match in re.finditer(r'('+NUMBER+r')\s*(mm|cm|m|metres?|meters?|inches?|inch|in|ft|kg|degrees?|deg|fpm|psi|%)(?![A-Za-z])',text,re.I):
        add('number',match[1]);metric=result[-1];metric['unit']=UNIT_ALIASES.get(match[2].lower(),match[2].lower());metric['label']=match[0]
        unit_metrics.append((match,metric))
    for (left,a),(right,b) in zip(unit_metrics,unit_metrics[1:]):
        # An explicitly paired alternate value may corroborate the same physical dimension.
        if a['unit']!=b['unit'] and UNIT_FAMILIES[a['unit']]==UNIT_FAMILIES[b['unit']] and re.fullmatch(r'\s*(?:\(|/|=|≈)\s*',text[left.end():right.start()]):
            a['alternate_group']=b['alternate_group']=left.start()
    # Untyped factual numbers still need citations; typed fields above get stronger comparisons.
    generic=' '.join(token for token in text.split() if '/' not in token and '\\' not in token)
    generic=re.sub(r'^\s*(?:[-*]\s*)?\d+[.)]\s+','',generic)
    generic=re.sub(r'\b[a-fA-F0-9]{64}\b|\b[a-fA-F0-9]{8,63}(?:…|\.{3})','',generic)
    known={numeric(c.get('raw',c['value'])) for c in result if c['kind']!='hash'}
    if not re.match(r'^\s*#{1,6}\s',text):
        for match in re.finditer(r'(?<![\w/_.-])('+NUMBER+r')(?![\w/_.-])',generic):
            if numeric(match[1]) not in known:add('number',match[1]);known.add(numeric(match[1]))
    return result


def payload_for(manager,ref):
    if ref.get('_ledger') is not None:return ref['_ledger']
    if ref.get('observation_id'):return manager.store.get('goal_evidence',ref['observation_id']).get('result',{})
    task=manager.store.get('task',ref['task_id']);event=task['events'][ref['event_index']]
    if ref['kind'] in ('submit_gpu_job','get_gpu_job') and ref.get('gpu_job_id'):return manager.store.get('gpu',ref['gpu_job_id'])
    return event.get('result',event.get('change',{}))


def source_facts(manager,ref):
    facts={}
    def add(kind,value):
        if kind=='hash':
            if isinstance(value,str):facts.setdefault(kind,set()).add(value.lower())
        elif (number:=numeric(value)) is not None and number.is_finite():facts.setdefault(kind,set()).add(number)
    add('hash',ref.get('file_sha256'))
    for value in ref.get('files',{}).values():add('hash',value)
    for value in ref.get('gpu_artifacts',[]):add('hash',value.get('sha256'))
    if ref['kind']=='spec_value':
        value=ref.get('spec_value');values=value if isinstance(value,list) else [value]
        for v in values:
            if not isinstance(v,bool):add('number',v)
        return facts
    payload=payload_for(manager,ref)
    if ref['kind']=='ledger_fact':
        add('cost',payload.get('estimated_usd'));add('wall_seconds',payload.get('runtime_s'))
        for key in ('estimated_usd','runtime_s','input_tokens','output_tokens','cached_input_tokens','reasoning_tokens'):add('number',payload.get(key))
        return facts
    if ref['kind'] not in EXECUTION_KINDS:
        texts=[]
        def collect(value,depth=0):
            if depth>8:return
            if isinstance(value,dict):
                for key,entry in value.items():
                    if key in ('content','text','description','title','alt') and isinstance(entry,str):texts.append(entry)
                    elif isinstance(entry,(dict,list)):collect(entry,depth+1)
            elif isinstance(value,list):
                for entry in value[:100]:collect(entry,depth+1)
        collect(payload)
        for metric in metrics('\n'.join(texts)[:200000]):
            if metric['kind']!='hash':
                add(metric['kind'],metric['value']);add('number',metric['value'])
                if metric.get('unit'):add('unit:'+metric['unit'],metric['value'])
        return facts
    add('wall_seconds',payload.get('duration_s'))
    inner=payload.get('result') or {};summary=inner.get('summary') or {};add('wall_seconds',summary.get('duration_s'))
    text=str(payload.get('output') or inner.get('logs') or '')
    if len(text)>200000:text=text[:100000]+'\n'+text[-100000:]
    for metric in metrics(text):
        if metric['kind']!='hash':
            add(metric['kind'],metric['value']);add('number',metric['value'])
            if metric.get('unit'):add('unit:'+metric['unit'],metric['value'])
    # Common machine-readable result fields, excluding requested timeouts/inputs.
    aliases={'tests':'tests_total','tests_run':'tests_total','tests_passed':'tests_passed','passed':'tests_passed',
        'tests_failed':'tests_failed','failed':'tests_failed','tests_skipped':'tests_skipped',
        'duration_s':'wall_seconds','wall_seconds':'wall_seconds','simulated_seconds':'simulated_seconds','sim_wall_seconds':'wall_seconds',
        'render_wall_seconds':'wall_seconds','estimated_usd':'cost','cost_usd':'cost','total_cost_usd_estimated':'cost'}
    def visit(value,depth=0):
        if depth>12:return
        if isinstance(value,dict):
            for key,v in value.items():
                if key in aliases:add(aliases[key],v)
                if isinstance(v,(int,float)) and not isinstance(v,bool):add('number',v)
                if isinstance(v,(dict,list)):visit(v,depth+1)
        elif isinstance(value,list):
            for v in value[:100]:visit(v,depth+1)
    visit(inner)
    try:visit(json.loads(text))
    except (ValueError,RecursionError):pass
    for match in re.finditer(r'["\'](wall_seconds|simulated_seconds|sim_wall_seconds|render_wall_seconds|tests_passed|tests_run)["\']\s*:\s*('+NUMBER+r')',text):add(aliases[match[1]],match[2])
    return facts


def format_check(content,path,rule):
    suffix=Path(path).suffix.lower()
    fmt=DATA_FORMATS.get(suffix) or ({'.md':'markdown','.markdown':'markdown','.txt':'text','.rst':'text'}.get(suffix)) or rule.get('format') or 'text'
    required=list(dict.fromkeys([*rule.get('required_sections',[]),*rule.get('required_keys',[])]));errors=[]
    if not content.strip():return fmt,['Document is empty.']
    if fmt in DATA_FORMATS.values():
        try:
            if fmt=='json':data=json_value(json.loads(content))
            elif fmt=='yaml':data=json_value(load_yaml(content))
            elif fmt=='toml':data=tomllib.loads(content)
            else:
                rows=list(csv.reader(io.StringIO(content),strict=True));headers=rows[0] if rows else []
                if not headers or any(not h.strip() for h in headers) or len(set(headers))!=len(headers) or any(len(row)!=len(headers) for row in rows[1:]):raise ValueError('Invalid CSV rows/headers.')
                data={h:[row[n] for row in rows[1:]] for n,h in enumerate(headers)}
        except (ValueError,RecursionError,csv.Error):return fmt,['Invalid '+fmt+' document syntax.']
        for section in required:
            value=data
            for part in section.split('.'):
                if isinstance(value,dict) and part in value:value=value[part]
                elif isinstance(value,list) and part.isdecimal() and int(part)<len(value):value=value[int(part)]
                else:errors.append('Missing required section/key: '+section);break
        return fmt,errors
    lines=content.splitlines();headings=set();fence=None
    for index,line in enumerate(lines):
        match=re.match(r'^\s{0,3}(`{3,}|~{3,})',line)
        if match:
            marker=match[1][0]
            if fence is None:fence=marker
            elif fence==marker:fence=None
            continue
        if fence is not None:continue
        heading=re.match(r'^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$',line)
        if heading:headings.add(heading[1].strip().casefold())
        elif index+1<len(lines) and re.fullmatch(r'\s{0,3}(?:=+|-+)\s*',lines[index+1]) and line.strip():headings.add(line.strip().casefold())
        elif fmt=='text' and line.strip():headings.add(line.strip().rstrip(':').casefold())
    if fence is not None:errors.append('Unclosed Markdown code fence.')
    license=recognize_license(content) if Path(path).stem.upper() in ('LICENSE','LICENCE','COPYING') else None
    if license:headings.add(license['spdx_id'].casefold())
    for section in required:
        if Path(path).stem.upper() in ('LICENSE','LICENCE','COPYING') and section in LICENSES and (license or {}).get('spdx_id')!=section:
            errors.append('Expected recognized SPDX license body '+section+'; an identifier/title alone is insufficient.');continue
        if section.strip().lstrip('#').strip().casefold() not in headings:errors.append('Missing required section: '+section)
    return fmt,errors


def check_claim(manager,text,line,refs,own_document_ids,fact_cache,doc_role='results',doc_path=''):
    ids=citations(text,refs)
    plain=PARENS.sub(lambda m:m.group() if re.fullmatch(r'\s*'+NUMBER+r'\s*(?:mm|cm|m|in|inch|inches|ft|kg|deg|fpm|psi|%)\s*',m[1],re.I) else '',text)
    claims=metrics(plain);success=bool(SUCCESS.search(NEGATED.sub('',plain)))
    result_claim=success or bool(RESULT_CLAIM.search(NEGATED.sub('',plain))) or any(c['kind'].startswith('tests') or c['kind']=='cost' for c in claims)
    if doc_role=='results' and claims:result_claim=True
    planned=doc_role=='design' and not result_claim and (Path(doc_path).stem.upper()=='STORYBOARD' or re.search(r'\bplanned|\btarget|\bproposed|\bschedule|계획|예정|목표',plain,re.I))
    if planned and not ids:return {'line':line,'text':text[:500],'evidence_ids':ids,'planned_intent':True,'result_claim':False,'comparisons':[],'defects':[],'passed':True}
    if not claims and re.fullmatch(r'\s*(?:#{1,6}\s*)?(?:Done|Completed|Results|Status|Success|Failures?|Verification|Tests?|완료|성공|검증)\s*:?\s*',plain,re.I):return None
    if not claims and not success and not ids:return None
    errors=[];usable=[]
    if not ids:errors.append('Missing parenthesized evidence ID for a success/numeric claim.')
    for identifier in ids:
        source=refs.get(identifier)
        if source is None:errors.append('Unknown evidence ID: '+identifier)
        elif identifier in own_document_ids:errors.append('A document cannot cite its own file observation to prove its factual claims: '+identifier)
        elif source.get('stale'):errors.append('Stale citation '+identifier+': '+source.get('stale_reason',''))
        elif not source.get('supports_acceptance'):errors.append('Citation is not successful evidence: '+identifier)
        else:usable.append(source)
    if result_claim and usable and not any(source['kind'] in EXECUTION_KINDS for source in usable):errors.append('Result/success/completion, performance, cost or test claim requires a successful execution citation, not reference/spec/file evidence alone.')
    if not result_claim:
        for source in usable:
            if source['kind'] not in (*EXECUTION_KINDS,*REFERENCE_KINDS,'spec_value'):
                errors.append('Reference/design facts require execution, a current reference observation, or an exact spec key citation: '+source['id'])
            if source['kind']=='spec_value' and isinstance(source.get('spec_value'),(str,bool)):
                value=str(source['spec_value']).strip().casefold()
                if value and value not in plain.casefold():errors.append('Spec value mismatch: cited key value is not stated in the claim: '+source['id'])
    # Reference/spec facts cannot supply a result metric even when an unrelated run is also cited.
    fact_sources=[s for s in usable if s['kind'] in EXECUTION_KINDS or s['kind']=='ledger_fact'] if result_claim else usable
    all_facts={};cost_values=[]
    for source in fact_sources:
        identifier=source['id']
        if identifier not in fact_cache:fact_cache[identifier]=source_facts(manager,source)
        for kind,values in fact_cache[identifier].items():all_facts.setdefault(kind,set()).update(values)
        if source['kind']=='ledger_fact':cost_values.extend(fact_cache[identifier].get('cost',[]))
    if len(cost_values)>1:all_facts.setdefault('cost',set()).add(sum(cost_values))
    comparisons=[]
    for claim in claims:
        kind=claim['kind']
        if kind=='tests':expected=set().union(*(v for k,v in all_facts.items() if k.startswith('tests')))
        elif kind=='seconds':expected=all_facts.get('seconds',set())|all_facts.get('wall_seconds',set())|all_facts.get('simulated_seconds',set())
        else:expected=all_facts.get(kind,set())
        if claim.get('unit'):
            unit_facts={k:v for k,v in all_facts.items() if k.startswith('unit:')}
            if unit_facts:expected=unit_facts.get('unit:'+claim['unit'],set())
        matched,method=compare(claim,expected)
        comparison={'kind':kind,'claimed':claim['value'],'unit':claim.get('unit'),'alternate_group':claim.get('alternate_group'),
            'expected':sorted(str(v) for v in expected),'matched':matched,'method':method};comparisons.append(comparison)
    for comparison in comparisons:
        group=comparison['alternate_group']
        if group is not None and any(c['matched'] and c['alternate_group']==group for c in comparisons):
            comparison['matched']=True;comparison['method']+='; explicitly paired alternative unit value matches'
        if usable and not comparison['matched']:
            errors.append(('Numeric/hash mismatch' if comparison['expected'] else 'Unverifiable numeric/hash claim')+f': {comparison["kind"]} claimed {comparison["claimed"]}; observed '+(', '.join(comparison['expected'][:12]) or 'no corresponding value')+'; comparison: '+comparison['method']+'.')
    return {'line':line,'text':text[:500],'evidence_ids':ids,'success_claim':success,'result_claim':result_claim,'comparisons':comparisons,'defects':errors,'passed':not errors}


def validate_document(manager,goal,item,text,rule,review_ids,refs=None):
    refs=goal_sources(manager,goal,item) if refs is None else refs;ws=Workspace(goal['workspace'],manager.store.root)
    files={};errors=[];checks=[];fact_cache={}
    for identifier in review_ids:
        ref=refs.get(identifier)
        if not ref or not ref.get('supports_acceptance') or not ref.get('path') or not ref.get('file_sha256') or ref.get('kind') not in ('change','read_file','read_structured','command_output','file_artifact'):continue
        files[Path(ref['path']).as_posix()]=ref
    requested=rule.get('document_paths')
    if requested:
        for path in requested:
            if path not in files:errors.append('Missing current document file read/write evidence: '+path)
        files={p:r for p,r in files.items() if p in requested}
    else:files={p:r for p,r in files.items() if document_path(p)}
    if not files:errors.append('Document acceptance requires a current-hash document file read/write observation.')
    for path,ref in files.items():
        own_ids={key for key,value in refs.items() if value.get('path') and Path(value['path']).as_posix()==path}
        try:
            raw=ws.read(path)
            if raw['sha256']!=ref['file_sha256']:raise ValueError('Document hash changed since observation.')
            fmt,format_errors=format_check(raw['content'],path,rule);errors.extend(path+': '+e for e in format_errors)
            doc_role,role_origin=role_for(path,rule)
            add_spec_sources(manager,goal,refs,[m.group() for m in SPEC.finditer(raw['content'])])
            claims=[];claim_lines=list(enumerate(raw['content'].splitlines(),1))
            # Configuration values are chosen inputs, not prose claims of observed results.
            # This never makes an execution criterion pass without successful execution evidence.
            if fmt in DATA_FORMATS.values() or doc_role=='static':claim_lines=[]
            for number,line in claim_lines:
                for sentence in re.split(r'(?<=[.!?])\s+(?!\()',line):
                    claim=check_claim(manager,sentence,number,refs,own_ids,fact_cache,doc_role,path)
                    if claim:claims.append(claim);errors.extend(f'{path}:{number}: '+e for e in claim['defects'])
            checks.append({'path':path,'sha256':raw['sha256'],'format':fmt,'format_defects':format_errors,'claims':claims,
                'doc_role':doc_role,'doc_role_origin':role_origin,'license':recognize_license(raw['content']) if Path(path).stem.upper() in ('LICENSE','LICENCE','COPYING') else None,
                'spec_sources':[{k:r[k] for k in ('id','path','file_sha256','spec_key_path','spec_value','stale','supports_acceptance') if k in r} for r in refs.values() if r['kind']=='spec_value']})
        except (ValueError,OSError):errors.append(path+': document unavailable, protected, or changed; read it again before review.')
    return {'criterion':text,'kind':'document','passed':not errors,'defects':list(dict.fromkeys(errors)),'files':checks,
        'cited_evidence_ids':list(dict.fromkeys(identifier for f in checks for c in f['claims'] for identifier in c['evidence_ids'])),
        'limits':'Automatic comparison covers recognized test counts, cost, seconds and SHA hashes. Semantic completeness/accuracy still requires Planner review; unknown numeric facts require grounded clarification, never a new run by default.'}


def document_bundle(manager,goal,item,review_ids=None,refs=None):
    if not has_documents(item):return None
    refs=goal_sources(manager,goal,item) if refs is None else refs
    if review_ids is None:
        own=manager.observed_evidence(goal,item)
        paths={p for c in item['acceptance'] for p in rule_for(item,c).get('document_paths',[])}
        candidates=[r for r in own.values() if r.get('supports_acceptance') and r.get('path') and r.get('file_sha256') and (not paths or Path(r['path']).as_posix() in paths)
            and document_path(r['path'])]
        if not paths and candidates:
            latest=max(candidates,key=lambda r:(r.get('observed_at',0),r['kind']=='change'));candidates=[r for r in candidates if r['path']==latest['path']]
        review_ids=[r['id'] for r in candidates]
    rows=[validate_document(manager,goal,item,c,rule_for(item,c),review_ids,refs) for c in item['acceptance'] if rule_for(item,c)['kind']=='document']
    return {'passed':all(row['passed'] for row in rows),'criteria':rows,'defects':list(dict.fromkeys(e for row in rows for e in row['defects']))}


def preferred_documents(bundle,refs):
    # Repeated reads of one unchanged file must not crowd its actual cited sources out.
    paths={f['path'] for row in bundle['criteria'] for f in row['files']};latest={}
    for identifier,ref in reversed(list(refs.items())):
        if ref.get('supports_acceptance') and ref.get('path') in paths and ref.get('file_sha256'):
            latest.setdefault((ref['path'],ref['file_sha256']),identifier)
    return list(dict.fromkeys([*latest.values(),*(identifier for row in bundle['criteria'] for identifier in row['cited_evidence_ids'])]))


def compact_bundle(bundle,limit=5500):
    if bundle is None:return None
    view={'passed':bundle['passed'],'criteria':[],'defects':bundle['defects'][:8],'omitted_claims':0}
    for row in bundle['criteria']:
        short={'criterion':row['criterion'],'passed':row['passed'],'files':[],'cited_evidence_ids':row['cited_evidence_ids'][:40]}
        for file in row['files']:
            short['files'].append({k:file[k] for k in ('path','sha256','format','format_defects','doc_role','doc_role_origin','license') if k in file})
            for claim in file['claims']:
                candidate={k:claim[k] for k in ('line','evidence_ids','comparisons','defects','passed')}
                short.setdefault('claim_checks',[]).append(candidate)
                if len(json.dumps(view,ensure_ascii=False))+len(json.dumps(short,ensure_ascii=False))>limit:
                    short['claim_checks'].pop();view['omitted_claims']+=1
        view['criteria'].append(short)
    # Keep bounded metadata too; full checks remain in goal_review_attempt.
    while len(json.dumps(view,ensure_ascii=False))>limit:
        if view['defects']:view['defects'].pop()
        elif any(row.get('cited_evidence_ids') for row in view['criteria']):next(row for row in reversed(view['criteria']) if row.get('cited_evidence_ids'))['cited_evidence_ids'].pop()
        elif len(view['criteria'])>1:view['criteria'].pop()
        else:return {'passed':bundle['passed'],'truncated':True,'guidance':'Full document checks are stored in goal_review_attempt.'}
    return view
