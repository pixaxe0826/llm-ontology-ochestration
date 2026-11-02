"""Conservative document roles and offline recognition of complete known license bodies."""
import hashlib,json,re
from pathlib import Path

ROLES=('results','reference','design','static')
LICENSES=json.loads(Path(__file__).with_name('license_fingerprints.json').read_text(encoding='utf-8'))


def role_for(path,rule):
    if rule.get('doc_role') in ROLES:return rule['doc_role'],'explicit acceptance_rules.doc_role'
    name=Path(path).name.upper();stem=Path(path).stem.upper()
    if stem in ('LICENSE','LICENCE','COPYING','NOTICE','ATTRIBUTION'):return 'static','inferred from static/legal filename'
    if 'REFERENCE' in stem:return 'reference','inferred from reference filename'
    if any(word in stem for word in ('STORYBOARD','DESIGN','PLAN','PROPOSAL')):return 'design','inferred from planning/design filename'
    if Path(path).suffix.lower() in ('.yaml','.yml','.json','.toml','.csv'):return 'design','inferred from structured data format'
    return 'results','inferred strict results fallback (including README/RESULTS/report)'


def document_path(path):
    return Path(path).suffix.lower() in ('.md','.markdown','.txt','.rst','.json','.yaml','.yml','.toml','.csv','.html') or Path(path).name.upper() in ('LICENSE','LICENCE','COPYING','NOTICE')


def recognize_license(content):
    normalized=re.sub(r'[^a-z0-9]','',content.casefold())
    for identifier,template in LICENSES.items():
        for variant in [template,*template.get('variants',[])]:
            start=re.sub(r'[^a-z0-9]','',variant['start'].casefold());end=re.sub(r'[^a-z0-9]','',variant['end'].casefold())
            a=normalized.find(start);b=normalized.rfind(end)
            if a<0 or b<=a:continue
            if hashlib.sha256(normalized[a:b+len(end)].encode()).hexdigest()==variant['sha256']:
                return {'spdx_id':identifier,'method':'normalized official SPDX '+variant.get('type','complete_core')+' SHA-256',
                    'match_type':variant.get('type','complete_core'),'source':variant['source']}
    return None
