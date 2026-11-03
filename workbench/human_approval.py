"""A human approval proves only the explicitly approved, unchanged file bytes."""
import hashlib,json
from .workspace import Workspace
from .file_hashes import file_hashes


class HumanApprovalError(ValueError):
    def __init__(self,defects):self.defects=defects;super().__init__('; '.join(defects))


def capture(manager,goal,data):
    if 'approval_paths' not in data:return {}
    hashes=file_hashes(Workspace(goal['workspace'],manager.store.root),data['approval_paths'])['files']
    option=data.get('approval_option','Approve')
    if not isinstance(option,str) or option not in data.get('options',[]):raise ValueError('File approval requires an explicit approval_option in options (for example Approve).')
    return {'approval_files':{r['path']:r['sha256'] for r in hashes},'approval_option':option,'file_approval':True}


def validate_answer(manager,goal,decision,option,free,withdraw):
    if not decision.get('file_approval'):return {}
    approved=not withdraw and (option==decision['approval_option'] or option is None and free==decision['approval_option'])
    if approved:
        current={r['path']:r['sha256'] for r in file_hashes(Workspace(goal['workspace'],manager.store.root),list(decision['approval_files']))['files']}
        changed=[path for path,sha in decision['approval_files'].items() if current.get(path)!=sha]
        if changed:raise ValueError('Approval files changed since this request: '+', '.join(changed)+'. Request a new approval with current hashes.')
    return {'files_approved':approved,'answered_by':'Human'}


def observations(manager,goal,owner_ids):
    result={};ws=Workspace(goal['workspace'],manager.store.root)
    for decision in manager.decisions(goal['id']):
        if not decision.get('file_approval') or not set(decision.get('affected_items',[])) & owner_ids:continue
        reasons=[];files=decision.get('approval_files') or {}
        if not files:reasons.append('No request-time file hashes were recorded.')
        if decision['state']!='answered' or not decision.get('files_approved') or decision.get('answered_by')!='Human' or decision.get('automatic'):reasons.append('No explicit human approval was recorded.')
        try:
            current={r['path']:r['sha256'] for r in file_hashes(ws,list(files))['files']}
            reasons.extend('Approved file changed: '+p for p,sha in files.items() if current.get(p)!=sha)
        except (ValueError,OSError):reasons.append('Approved file is missing, protected or unreadable.')
        identifier='approval:'+decision['id']
        result[identifier]={'id':identifier,'kind':'human_approval','decision_id':decision['id'],'files':files,
            'sha256':hashlib.sha256(json.dumps(files,sort_keys=True).encode()).hexdigest(),'observed_at':decision.get('answered_at') or decision['created'],
            'source_plan_item_id':next((i for i in decision['affected_items'] if i in owner_ids),None),'stale':bool(reasons),'stale_reasons':reasons,
            'stale_reason':'; '.join(reasons) if reasons else 'Human approved these exact file hashes; they remain current.','supports_acceptance':not reasons}
    return result


def validate_criterion(rule,refs,valid):
    approved={path for identifier in refs for path in valid[identifier].get('files',{}) if valid[identifier].get('kind')=='human_approval' and valid[identifier].get('supports_acceptance')}
    missing=set(rule['approval_paths'])-approved
    if missing:raise HumanApprovalError(['Human approval requires an answered approving decision with current hashes for: '+', '.join(sorted(missing))])
