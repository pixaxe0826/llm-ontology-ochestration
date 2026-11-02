"""Validate an entire human action before any item or plan mutation."""
import json
from .plan_updates import prepare_plan,plan_key
from .security import contains_key
from .item_states import supersede_requested


def attach_original(text,original):
    if not isinstance(text,str) or not text.strip() or len(text)>8000 or contains_key(text):raise ValueError('invalid_item_direction')
    if original in text:return text
    suffix='\n\nHUMAN ORIGINAL (binding):\n'+original
    return text[:8000-len(suffix)]+suffix


def prepare_action(manager,goal,row,action,prohibited):
    if not isinstance(action,dict) or action.get('status') not in ('applied','partial','rejected'):raise ValueError('missing_ack')
    if contains_key(json.dumps(action,ensure_ascii=False)):raise ValueError('protected_action_content')
    items=action.get('items',[]);directions=action.get('instructions',[]);unmet=action.get('unapplied',[]);actions=action.get('item_actions',[])
    if not isinstance(unmet,list) or any(not isinstance(x,str) or len(x)>1000 or contains_key(x) for x in unmet):raise ValueError('invalid_unapplied')
    if not isinstance(directions,list) or len(directions)>40:raise ValueError('invalid_instructions')
    if not isinstance(actions,list) or len(actions)>40:raise ValueError('invalid_item_actions')
    if row['kind']=='note' and (items or directions or actions):raise ValueError('note_is_reference_only')
    if action['status']=='rejected' and (items or directions or actions):raise ValueError('rejected_action_has_changes')
    fields={'key','id','operation','title','acceptance','acceptance_rules','role','parent','depends_on','blocked_by_decision','priority','provisional_scope','files_to_read','files_to_write','commands_to_run','status','superseded_reason','superseded_by'}
    if not isinstance(items,list) or any(not isinstance(i,dict) or set(i)-fields for i in items):raise ValueError('plan_update_fields_forbidden')
    if any('status' in i and i['status']!='superseded' for i in items):raise ValueError('plan_update_fields_forbidden')
    patch=prepare_plan(manager.items(goal['id']),items,max_plan_items=goal.get('max_plan_items',40),allow_unreplaced_supersession=supersede_requested(row['text']));manager.validate_decision_refs(goal,patch)
    active={manager.store.get('task',t).get('plan_item_id') for t in list(manager.builders.active) if manager.store.get('task',t).get('goal_id')==goal['id']}
    lookup={ref:item for item in manager.items(goal['id']) for ref in (item['id'],plan_key(item))}
    def target(spec):
        if not isinstance(spec,dict) or not isinstance(spec.get('item_id'),str) or spec['item_id'] not in lookup:raise ValueError('invalid_item_action_target')
        return lookup[spec['item_id']]
    parsed_actions=[];seen=set()
    for spec in actions:
        item=target(spec);text=attach_original(spec.get('instruction',row['text']),row['text'])
        if item['id'] in seen or prohibited(text):raise ValueError('invalid_item_action_target')
        reason=spec.get('reason',spec.get('superseded_reason'));replacement=spec.get('superseded_by')
        if spec.get('resolution')=='superseded' and not supersede_requested(row['text']):raise ValueError('Human closure request required for superseded.')
        manager.validate_item_action(goal,item,spec.get('resolution'),text,reason,replacement);seen.add(item['id']);parsed_actions.append((item['id'],spec['resolution'],text,reason,replacement))
    parsed=[]
    for direction in directions:
        item=target(direction);text=attach_original(direction.get('text'),row['text'])
        if prohibited(text):raise ValueError('invalid_item_direction')
        parsed.append((item['id'],text))
    touched={r['id'] for r in patch['updates']}|{r[0] for r in parsed}|seen
    if touched & active:raise ValueError('active_item_update_deferred')
    if any(r.get('status')=='superseded' for r in patch['updates']) and not supersede_requested(row['text']):raise ValueError('Human closure request required for superseded.')
    if row['kind']!='note' and action['status']=='applied' and supersede_requested(row['text']) and not any(a[1]=='superseded' for a in parsed_actions) and not any(r.get('status')=='superseded' for r in patch['updates']):
        raise ValueError('closure_not_applied: return a superseded item action, or mark partial/rejected and explain why closure was not applied.')
    if action['status']=='partial' and not unmet:raise ValueError('partial_ack_requires_reason')
    return {'patch':patch,'actions':parsed_actions,'directions':parsed,'unmet':unmet,'state':'partial' if unmet and action['status']=='applied' else action['status']}


def readable_error(message,korean=True):
    known={
        'missing_ack':('지시 ID와 처리 상태를 포함한 ACK가 빠졌습니다.','ACK is missing its directive ID or valid processing status.'),
        'note_is_reference_only':('참고(note) 메시지로는 항목을 재작업으로 열거나 계획을 바꿀 수 없습니다. 지시 또는 중단으로 보내야 합니다.','A reference note cannot reopen items or change the plan. Send an instruction or interrupt.'),
        'invalid_item_actions':('재작업 동작(item_actions)은 항목별 객체 배열이어야 합니다.','item_actions must be an array of per-item action objects.'),
        'invalid_item_action_target':('재작업 대상 항목의 ID·키가 없거나 중복됐습니다.','A target item ID/key is missing, unknown, duplicated, or prohibited.'),
        'plan_update_fields_forbidden':('계획 변경에 허용되지 않는 필드가 있습니다. 상태 변경은 item_actions의 resolution으로 보내야 합니다.','Plan updates contain forbidden fields. Use item_actions.resolution for state changes.'),
        'active_item_update_deferred':('대상 Builder가 아직 실행 중이어서 항목을 변경하지 않았습니다.','The target Builder is still active; no item changes were applied.'),
        'invalid_instructions':('항목 지시(instructions)의 배열 형식이 잘못됐습니다.','instructions must be a bounded array of item directions.'),
        'invalid_item_direction':('항목 지시의 텍스트·길이 또는 보안 검사를 통과하지 못했습니다.','An item direction failed text, length, or security validation.'),
        'partial_ack_requires_reason':('일부 반영 ACK에 반영하지 못한 이유가 없습니다.','A partial ACK must state what was not applied.'),
        'protected_action_content':('Planner 응답에 보호 대상 내용이 있어 적용하지 않았습니다.','Protected content was detected in the Planner action.'),
        'rejected_action_has_changes':('거부 ACK에 실행할 변경이 함께 들어 있습니다.','A rejected ACK cannot contain changes.'),
    }
    if message in known:return known[message][0 if korean else 1]
    if 'Resolution must' in message:return '항목 동작은 rework, review, blocked, superseded 중 하나여야 합니다.' if korean else 'Resolution must be rework, review, blocked, or superseded.'
    if 'cyclic' in message or 'dependency' in message.lower():return '계획의 의존성 참조 또는 순환 검증이 실패했습니다.' if korean else 'Dependency references or cycle validation failed.'
    if 'acceptance' in message.lower() or 'criterion' in message.lower():return '수용 기준의 분류 또는 교체 형식 검증이 실패했습니다.' if korean else 'Acceptance criteria classification/replacement validation failed.'
    if message.startswith('GoalLimit'):return '예산·마감 또는 실행 상태 제한으로 지시 보정 요청을 진행하지 못했습니다.' if korean else 'Budget, deadline, or execution state prevented the correction request.'
    return ('Planner 응답 검증 실패: ' if korean else 'Planner response validation failed: ')+message[:180]
