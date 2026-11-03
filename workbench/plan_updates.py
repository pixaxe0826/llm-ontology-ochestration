"""Validate plan edits before mutation; distinguish existing updates from new rows."""
import json
import re
import uuid
from .security import contains_key
from .acceptance_rules import normalize_criteria
from .plan_capacity import plan_limit,PlanCapacityError,DEFAULT,active_count
from .item_states import dependency_target

KEY=re.compile(r'[A-Za-z0-9_-]{1,60}')
HINT_FIELDS=('files_to_read','files_to_write','commands_to_run')


def implementation_hints(raw,target=None):
    result={}
    for field in HINT_FIELDS:
        if field not in raw and (not target or field not in target):continue
        value=raw.get(field,target.get(field) if target else None)
        maximum=5 if field=='files_to_read' else 20
        if not isinstance(value,list) or len(value)>maximum:raise ValueError(field+' must be a bounded list (maximum '+str(maximum)+').')
        if field=='commands_to_run':
            if any(not isinstance(argv,list) or not 1<=len(argv)<=40 or any(not isinstance(arg,str) or not arg or len(arg)>1000 for arg in argv) for argv in value):raise ValueError('commands_to_run must contain argv arrays, never shell command strings.')
        elif any(not isinstance(path,str) or not path or len(path)>500 or PathLikeUnsafe(path) for path in value):
            raise ValueError(field+' must contain safe workspace-relative paths, without globbing or traversal.')
        result[field]=value
    return result


def PathLikeUnsafe(value):
    from pathlib import PureWindowsPath
    from .security import protected
    pieces=value.replace('\\','/').split('/')
    return PureWindowsPath(value).is_absolute() or bool(PureWindowsPath(value).drive) or value.startswith(('/','\\')) or any(part in ('..','') or protected(part) or any(c in part for c in '*?\x00\r\n') for part in pieces)

def plan_key(item):
    return item.get('key') or item['id']


def prepare_plan(existing,rows,require_nonempty=False,max_plan_items=DEFAULT,allow_unreplaced_supersession=False):
    limit=plan_limit(max_plan_items)
    if not isinstance(rows,list) or require_nonempty and not rows:
        raise ValueError('Plan items must be an array, nonempty for an initial plan.')
    if len(rows)>len(existing)+200:raise ValueError('Plan update exceeds the bounded update size.')
    if contains_key(json.dumps(rows,ensure_ascii=False)):raise ValueError('Plan output contains protected content.')
    by_key={plan_key(i):i for i in existing};by_id={i['id']:i for i in existing}
    if len(by_key)!=len(existing):raise ValueError('Existing plan keys are ambiguous; inspect the plan records.')
    reserved={r.get('key') for r in rows if isinstance(r,dict) and isinstance(r.get('key'),str)}
    used=set(by_key);updates={};creates=[];aliases={}
    for index,raw in enumerate(rows):
        if not isinstance(raw,dict):raise ValueError(f'Plan entry {index+1} must be an object.')
        key=raw.get('key')
        if not isinstance(key,str) or not KEY.fullmatch(key):raise ValueError(f'Invalid plan key at entry {index+1}; use 1–60 letters, digits, _ or -.')
        operation=raw.get('operation')
        if operation not in (None,'create','update'):raise ValueError(f'Invalid operation for key {key}.')
        identified=by_id.get(raw.get('id')) if raw.get('id') is not None else None
        if raw.get('id') is not None and identified is None:raise ValueError(f'Unknown existing item id for key {key}.')
        target=identified or (by_key.get(key) if operation!='create' else None)
        if identified and operation=='create':raise ValueError(f'Key {key} cannot create and identify an existing item simultaneously.')
        if identified and key in by_key and by_key[key]['id']!=identified['id']:raise ValueError(f'Ambiguous item id and existing key {key}.')
        if operation=='update' and target is None:raise ValueError(f'Cannot update unknown key {key}.')
        title=raw.get('title',target.get('title') if target else None)
        criteria=raw.get('acceptance',target.get('acceptance') if target else None)
        if not isinstance(title,str) or not 1<=len(title)<=200:raise ValueError(f'Invalid title for key {key}.')
        criteria,rules=normalize_criteria(criteria,raw.get('acceptance_rules'),target.get('acceptance_rules') if target else None)
        row={'key':plan_key(target) if target else key,'title':title,'acceptance':criteria,'role':'Builder'}
        row.update(implementation_hints(raw,target))
        if 'builder_route' in raw:
            route=raw['builder_route']
            if route is not None and (not isinstance(route,dict) or set(route)-{'provider','model','reasoning_effort'} or not all(isinstance(route.get(k),str) and route[k] for k in ('provider','model'))):raise ValueError('builder_route requires registered provider/model.')
            row['builder_route']=route
        elif target and 'builder_route' in target:row['builder_route']=target['builder_route']
        if raw.get('status')=='superseded':
            if target is None:raise ValueError('Superseded must update an existing item.')
            reason=raw.get('superseded_reason',raw.get('reason'))
            if not isinstance(reason,str) or not reason.strip() or len(reason)>2000:raise ValueError('Superseded requires a reason.')
            if not raw.get('superseded_by') and not allow_unreplaced_supersession:raise ValueError('Planner supersession requires a clear superseded_by replacement.')
            row.update(status='superseded',superseded_reason=reason,superseded_by_ref=raw.get('superseded_by'))
        if rules:row['acceptance_rules']=rules
        # Missing fields on historical rows stay unknown; never infer independence.
        for field in ('depends_on','blocked_by_decision'):
            if field in raw:
                refs=raw[field]
                if not isinstance(refs,list) or len(refs)>200 or any(not isinstance(v,str) or not v for v in refs) or len(set(refs))!=len(refs):
                    raise ValueError(f'Invalid {field} for key {key}.')
                row[field]=refs
            elif target and field in target:row[field]=target[field]
            elif not target:row[field]=[]
        for field,default in (('priority',100),('provisional_scope','none')):
            row[field]=raw.get(field,target.get(field,default) if target else default)
        if type(row['priority']) is not int or not 0<=row['priority']<=10000:raise ValueError('Priority must be 0–10000.')
        if row['provisional_scope'] not in ('none','local_files_and_tests'):raise ValueError('Invalid provisional scope.')
        if 'parent' in raw:row['parent_ref']=raw['parent']
        elif target:row['parent_id']=target.get('parent')
        else:row['parent_ref']=None
        if target:
            row['id']=target['id']
            old=updates.get(target['id'])
            if old and old!=row:raise ValueError(f'Conflicting updates for existing key {row["key"]}; provide one unambiguous update.')
            updates[target['id']]=row
        else:
            # An identical repeated new row is the same item; distinct new rows get unique keys.
            same=next((r for r in creates if r.get('requested_key')==key and all(r.get(k)==v for k,v in row.items() if k!='key')),None)
            if same:
                aliases.setdefault(key,set()).add(same['key']);continue
            suffix=2;unique=key
            while unique in used or unique!=key and unique in reserved:
                tail='-'+str(suffix);unique=key[:60-len(tail)]+tail;suffix+=1
            row.update(key=unique,id=uuid.uuid4().hex[:16],requested_key=key);creates.append(row);used.add(unique)
        aliases.setdefault(key,set()).add(row['key'])
    id_for={plan_key(i):i['id'] for i in existing};id_for.update({r['key']:r['id'] for r in creates})
    parents={i['id']:i.get('parent') for i in existing}
    projected={i['id']:dict(i) for i in existing}
    for row in [*updates.values(),*creates]:
        if row.get('status')=='superseded':
            reference=row.pop('superseded_by_ref')
            replacement=(reference if reference in by_id else id_for.get(reference)) if isinstance(reference,str) else None
            if reference is not None and replacement is None:raise ValueError('Unknown superseded_by item.')
            if replacement==row['id']:raise ValueError('An item cannot supersede itself.')
            row['superseded_by']=replacement
        previous=projected.get(row['id'],{})
        projected[row['id']]={**previous,**row}
        if previous.get('status')=='done' and row.get('status')!='superseded' and (previous['acceptance']!=row['acceptance'] or previous.get('acceptance_rules',{})!=row.get('acceptance_rules',{})):
            projected[row['id']]['status']='review'
    count=active_count(projected.values())
    # Metadata-only edits and retirement remain possible when a legacy plan is over capacity.
    if count>limit and count>active_count(existing):raise PlanCapacityError(limit,count)
    for identifier in projected:
        seen=set();current=identifier
        while current and projected.get(current,{}).get('status')=='superseded':
            if current in seen:raise ValueError('Cyclic superseded_by chain.')
            seen.add(current);current=projected[current].get('superseded_by')
    for row in [*updates.values(),*creates]:
        if 'parent_ref' in row:
            ref=row.pop('parent_ref')
            if ref is None:parent=None
            elif not isinstance(ref,str):raise ValueError(f'Invalid parent for key {row["key"]}.')
            elif ref in by_id:parent=ref
            else:
                choices=aliases.get(ref,{ref})
                # An explicit create colliding with an existing key makes that parent reference ambiguous.
                if ref in by_key:choices=choices|{ref}
                if len(choices)!=1:raise ValueError(f'Ambiguous parent key {ref}; use a unique key or existing item id.')
                parent=id_for.get(next(iter(choices)))
                if parent is None:raise ValueError(f'Unknown parent key {ref}.')
            row['parent_id']=parent
        parents[row['id']]=row['parent_id']
    for identifier,parent in parents.items():
        seen={identifier}
        while parent is not None:
            if parent not in parents or parent in seen:raise ValueError('Invalid parent or cyclic plan.')
            seen.add(parent);parent=parents[parent]
    dependency_ids={i['id']:i.get('depends_on',[]) for i in existing}
    for row in [*updates.values(),*creates]:
        if 'depends_on' not in row:continue
        refs=[]
        for ref in row['depends_on']:
            if ref in by_id:identifier=ref
            else:
                choices=aliases.get(ref,{ref})
                if len(choices)!=1:raise ValueError(f'Ambiguous dependency {ref}.')
                identifier=id_for.get(next(iter(choices)))
            if identifier is None:raise ValueError(f"Unknown dependency {ref!r} for item {row['key']!r}; use an exact existing item ID or key.")
            if identifier==row['id']:raise ValueError(f"Self dependency {ref!r} for item {row['key']!r} is forbidden.")
            refs.append(identifier)
        row['depends_on']=list(dict.fromkeys(refs));dependency_ids[row['id']]=refs
    def visit(identifier,trail):
        if identifier in trail:raise ValueError('Cyclic plan dependency.')
        for ref in dependency_ids.get(identifier,[]):
            if ref not in dependency_ids:raise ValueError('Unknown dependency.')
            visit(ref,trail|{identifier})
    # Retired nodes do not execute; dependencies follow their replacement chain.
    for identifier in dependency_ids:
        if projected[identifier].get('status')=='superseded':dependency_ids[identifier]=[]
        else:dependency_ids[identifier]=list(dict.fromkeys(dependency_target(ref,projected)[0] for ref in dependency_ids[identifier]))
    for identifier in dependency_ids:visit(identifier,set())
    return {'updates':list(updates.values()),'creates':creates}


def initial_items(rows,max_plan_items=DEFAULT):
    patch=prepare_plan([],rows,True,max_plan_items);keys={r['id']:r['key'] for r in patch['creates']}
    return [{k:r[k] for k in ('key','title','acceptance','role','blocked_by_decision','priority','provisional_scope','acceptance_rules','builder_route',*HINT_FIELDS) if k in r}|
            {'parent':keys.get(r['parent_id']),'depends_on':[keys[v] for v in r['depends_on']]} for r in patch['creates']]
