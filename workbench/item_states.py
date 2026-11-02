"""Terminal retirement is distinct from acceptance and replacement readiness."""
import re

TERMINAL=frozenset(('done','superseded'))

def terminal(item):return item.get('status') in TERMINAL

def supersede_requested(text):
    for line in str(text or '').splitlines():
        if re.search(r"(?:do not|don't|never).{0,20}(?:supersed|close|cancel)|(?:대체|폐기|취소).{0,8}(?:하지|금지)",line,re.I):continue
        if re.search(r'\bsupersed|\b(?:close|cancel|retire)\s+(?:it\b|this\b|that\b|the\s+(?:item|task)|item\b|task\b|[TP]\d)|^\s*(?:close|cancel|retire)[.!]?\s*$|대체|폐기|항목.{0,20}(?:닫|종료|취소)',line,re.I):return True
    return False

def dependency_target(identifier,items):
    seen=set()
    while identifier in items and items[identifier].get('status')=='superseded':
        if identifier in seen:return identifier,False
        seen.add(identifier);replacement=items[identifier].get('superseded_by')
        if not replacement:return identifier,False
        identifier=replacement
    return identifier,identifier in items and items[identifier].get('status')=='done'


class ItemSupersession:
    def validate_supersession(self,goal,item,reason,replacement=None,additional_items=()):
        from .security import contains_key
        if not isinstance(reason,str) or not reason.strip() or len(reason)>2000 or contains_key(reason):raise ValueError('Superseded requires a non-secret reason of 1–2000 characters.')
        rows=self.items(goal['id'])+list(additional_items);lookup={ref:r for r in rows for ref in (r['id'],r.get('key',r['id']))}
        if replacement is not None:
            if not isinstance(replacement,str) or replacement not in lookup:raise ValueError('superseded_by must identify an item in this Goal.')
            replacement=lookup[replacement]['id'];by_id={r['id']:r for r in rows};seen={item['id']};current=replacement
            while current:
                if current in seen:raise ValueError('Cyclic superseded_by chain.')
                seen.add(current);current=by_id.get(current,{}).get('superseded_by')
        if item.get('task_id') in self.builders.active or self.long_commands.active(goal['id'],item['id']) or self.live_gpu_jobs(goal,item):
            raise ValueError('Wait for active Builder/GPU/long-command steps before superseding an item.')
        return reason.strip(),replacement

    def supersede_item(self,goal,item,reason,replacement=None,*,actor='Planner',source=None,before_snapshot=None):
        import time
        reason,replacement=self.validate_supersession(goal,item,reason,replacement)
        current=self.store.get('plan_item',item['id'])
        if current['status']=='superseded' and current.get('superseded_by')==replacement and current.get('superseded_reason')==reason:return current
        now=time.time();audit=self.store.save('goal_item_supersession',{'goal_id':goal['id'],'plan_item_id':item['id'],'before':before_snapshot or current,
            'reason':reason,'superseded_by':replacement,'actor':actor,'source':source,'created':now,**self.times(goal,now)})
        current=self.store.update('plan_item',item['id'],status='superseded',superseded_reason=reason,superseded_by=replacement,
            superseded_actor=actor,superseded_at=now,superseded_utc=self.times(goal,now)['time_utc'],superseded_local=self.times(goal,now)['time_local'],
            supersession_audit_id=audit['id'],continue_pending=False,wait_reason=None,waiting_resume=None,needs_decision_replan=False,review_trigger=None,stall_replan_pending=False)
        by_id={r['id']:r for r in self.items(goal['id'])};withdrawn=[]
        for decision in self.decisions(goal['id']):
            affected=decision.get('affected_items') or []
            if decision['state']=='pending' and affected and all(by_id.get(i,{}).get('status')=='superseded' for i in affected):
                self.store.update('decision',decision['id'],state='withdrawn',answered_at=now,withdraw_reason='Associated items superseded.',supersession_audit_id=audit['id']);withdrawn.append(decision['id'])
        self.store.update('goal_item_supersession',audit['id'],state='applied',withdrawn_decisions=withdrawn)
        return current
