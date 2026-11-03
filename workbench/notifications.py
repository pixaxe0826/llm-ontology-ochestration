"""Persistent local attention events. Delivery failures never enter the Goal control path."""
import copy
import hashlib
import queue
import re
import threading
import time
from urllib.parse import urlencode

from .security import redact
from .windows_alerts import WindowsAlerts
from .human_briefs import ko

LEVELS=('urgent','warning','info')
DEFAULTS={'enabled':{level:True for level in LEVELS},'channels':{'app':True,'browser':True,'windows':True},
          'routes':{'urgent':{'app':True,'browser':True,'windows':True},'warning':{'app':True,'browser':True,'windows':True},
                    'info':{'app':True,'browser':False,'windows':False}},'sound':True,'repeat_minutes':15}
EVENTS={
    'directive_partial':('warning','사람 지시 일부 반영','사람 지시의 일부 변경을 적용하지 못했습니다.'),
    'directive_rejected':('warning','사람 지시 미반영','사람 지시를 적용하지 못했습니다.'),
    'directive_applied':('info','사람 지시 반영','사람 지시가 반영됐습니다.'),
    'no_runnable':('urgent','진행 중단','미완료 항목이 있지만 실행할 수 있는 항목이 없습니다.'),
    'watchdog':('warning','Goal 감시 경고','Goal 상태와 대기 기록을 확인해 주세요.'),
    'unattended_resume_failed':('warning','무인 재개 실패','재시작 후 한 번의 자동 재개가 실패했습니다. 직접 확인해 주세요.'),
    'waiting_decision':('urgent','판단 대기','진행 가능한 작업을 마쳤습니다. 남은 판단과 막힌 항목을 확인해 주세요.'),
    'command_approval':('urgent','명령 승인','로컬 명령의 승인이 필요합니다.'),
    'gpu_approval':('urgent','GPU 승인','GPU 작업의 승인이 필요합니다.'),
    'question':('urgent','판단 요청','사용자의 답변이 필요합니다.'),
    'escalate':('urgent','판단 필요','에이전트가 사용자의 판단을 요청했습니다.'),
    'human_checkpoint':('urgent','사람 확인 시점','관측 결과와 다음 진행을 확인해 주세요.'),
    'coordinator_error':('urgent','Coordinator 오류','플랫폼 오류로 Goal이 멈췄습니다.'),
    'budget_80':('warning','예산 80%','설정한 예산의 80% 이상이 사용 또는 예약됐습니다.'),
    'budget_pause':('warning','예산 일시정지','비용 상한으로 Goal이 일시정지됐습니다.'),
    'deadline_pause':('warning','마감 일시정지','마감 시각에 도달해 Goal이 일시정지됐습니다.'),
    'no_progress_pause':('warning','진행 없음','새 근거 없는 반복으로 Goal이 일시정지됐습니다.'),
    'goal_blocked':('warning','Goal 정지','진행 가능한 항목이 없어 판단이 필요합니다.'),
    'interrupted':('warning','실행 중단','서버 재시작으로 Goal 실행이 중단됐습니다.'),
    'item_blocked':('warning','항목 blocked','진행이 막힌 계획 항목을 확인해 주세요.'),
    'goal_done':('info','Goal 완료','모든 수용 기준의 완료 판정이 기록됐습니다.'),
    'milestone_done':('info','마일스톤 완료','계획에 있는 마일스톤 그룹이 완료됐습니다.')}


EN_EVENTS={
    'directive_partial':('Human direction partially applied','Some requested changes were not applied.'),
    'directive_rejected':('Human direction not applied','The human direction was not applied.'),
    'directive_applied':('Human direction applied','The human direction was applied.'),
    'no_runnable':('Progress stopped','Unfinished items remain but none can run.'),
    'watchdog':('Goal watchdog','Check the Goal state and waiting records.'),
    'unattended_resume_failed':('Unattended resume failed','The single restart resume failed; manual inspection is needed.'),
    'waiting_decision':('Waiting for decisions','No independent work remains. Review pending decisions and their blocked items.'),
    'command_approval':('Command approval','A local command needs approval.'),'gpu_approval':('GPU approval','A GPU job needs approval.'),
    'question':('Decision requested','A human answer is needed.'),'escalate':('Decision needed','An agent requests a human decision.'),
    'human_checkpoint':('Human checkpoint','Review observed results before continuing.'),'coordinator_error':('Coordinator error','A platform error stopped the Goal.'),
    'budget_80':('Budget 80%','Spent or reserved usage has reached 80% of the configured budget.'),
    'budget_pause':('Budget pause','A spending limit paused the Goal.'),'deadline_pause':('Deadline pause','The Goal deadline was reached.'),
    'no_progress_pause':('No progress','Repeated cycles without new evidence paused the Goal.'),
    'goal_blocked':('Goal blocked','No runnable item remains; inspection is needed.'),'interrupted':('Interrupted','Server restart interrupted the Goal.'),
    'item_blocked':('Item blocked','Inspect the blocked plan item.'),'goal_done':('Goal complete','All acceptance criteria have recorded completion.'),
    'milestone_done':('Milestone complete','A plan milestone has completed.')}


def safe_text(value,limit=120):
    text=redact(str(value or ''))
    text=re.sub(r'```[\s\S]*?```|`[^`]*`','[코드 생략]',text)
    text=re.sub(r'https?://\S+','[링크 생략]',text,flags=re.I)
    text=re.sub(r'(?:[A-Za-z]:[\\/]|\\\\)[^\r\n\t<>"|]*','[경로 생략]',text)
    text=re.sub(r'(?<![\w])/[^\s]+','[경로 생략]',text)
    text=re.sub(r'[\r\n\t\x00-\x1f]+',' ',text)
    return re.sub(r'\s+',' ',text).strip()[:limit] or 'Muse Platform'


class Notifications:
    def __init__(self,store,ledger=None,native=None,clock=time.time):
        self.store,self.ledger,self.native,self.clock=store,ledger,native or WindowsAlerts(),clock
        self.base_url='http://127.0.0.1:8765'
        self.events=queue.Queue();self.native_events=queue.Queue()
        self.wake=threading.Event();self.stopping=threading.Event();self.lock=threading.RLock()
        self.thread=None;self.native_thread=None
        self.watchdog=None
        self.previous={}
        self.cursors={r['id']:r for r in store.items('notification_state',None)}
        for kind in ('goal','plan_item','task'):
            for row in store.items(kind,None):self.previous[(kind,row['id'])]=self.compact(kind,row)
        # Catch up current actionable states, without replaying historical messages/done events.
        for kind in ('goal','plan_item','goal_approval','task','decision'):
            for row in store.items(kind,None):
                active=(kind=='goal' and row['status'] in ('blocked','interrupted','paused','waiting_decision') or
                        kind=='plan_item' and row['status']=='blocked' or
                        kind=='goal_approval' and row['state']=='pending' or
                        kind=='task' and row['status']=='waiting_approval' or kind=='decision' and row['state']=='pending')
                if active:self.observe(kind,row,catchup=True)

    @staticmethod
    def compact(kind,row):
        keys={'goal':('id','title','status','reason','generation','updated','no_progress','budget_warnings','platform_error','scheduling_blocked'),
              'plan_item':('id','goal_id','title','status','updated','parent'),
              'goal_approval':('id','goal_id','task_id','plan_item_id','kind','state','updated'),
              'task':('id','goal_id','title','status','updated'),
              'message':('id','goal_id','plan_item_id','type','from','to','created','event','evidence_refs','ack_status','directive_id'),
              'ledger':('id','goal_id','provider','state','updated'),
              'decision':('id','goal_id','state','source','approval_id','affected_items','created','updated')}.get(kind,())
        data={key:copy.deepcopy(row[key]) for key in keys if key in row}
        if kind=='task' and row.get('approval'):
            data['approval']={k:row['approval'][k] for k in ('id','kind') if k in row['approval']}
        if kind=='message' and row.get('type')=='ESCALATE':
            data['checkpoint']=bool(re.search(r'human checkpoint|사람 확인|사용자 확인',str(row.get('body','')),re.I))
            data['coordinator']=str(row.get('body','')).startswith('Coordinator platform exception:')
        return data

    def observe(self,kind,row,catchup=False):
        if kind not in ('goal','plan_item','goal_approval','task','message','ledger','decision'):return
        try:
            self.events.put((kind,self.compact(kind,row),catchup));self.wake.set()
        except Exception:pass

    def config(self):
        result=copy.deepcopy(DEFAULTS)
        try:saved=self.store.get('settings','notifications')
        except ValueError:return result
        for key in ('enabled','channels'):result[key].update(saved.get(key,{}))
        for level in LEVELS:result['routes'][level].update(saved.get('routes',{}).get(level,{}))
        for key in ('sound','repeat_minutes'):result[key]=saved.get(key,result[key])
        return result

    def configure(self,data):
        if not isinstance(data,dict) or set(data)-set(DEFAULTS):raise ValueError('알림 설정 필드를 확인하세요.')
        result=self.config()
        for key in ('enabled','channels'):
            if key not in data:continue
            if not isinstance(data[key],dict) or set(data[key])-set(result[key]) or any(type(v) is not bool for v in data[key].values()):raise ValueError('알림 설정은 켜기/끄기로 지정하세요.')
            result[key].update(data[key])
        if 'routes' in data:
            if not isinstance(data['routes'],dict) or set(data['routes'])-set(LEVELS):raise ValueError('알림 종류를 확인하세요.')
            for level,values in data['routes'].items():
                if not isinstance(values,dict) or set(values)-{'app','browser','windows'} or any(type(v) is not bool for v in values.values()):raise ValueError('알림 방식을 확인하세요.')
                result['routes'][level].update(values)
        if 'sound' in data:
            if type(data['sound']) is not bool:raise ValueError('소리 설정을 확인하세요.')
            result['sound']=data['sound']
        if 'repeat_minutes' in data:
            value=data['repeat_minutes']
            if type(value) is not int or not 0<=value<=1440:raise ValueError('재알림은 0~1440분 정수로 지정하세요. 0은 해제입니다.')
            result['repeat_minutes']=value
        self.store.save('settings',{'id':'notifications',**result})
        return result

    @staticmethod
    def enabled(config,level,channel):
        return config['enabled'][level] and config['channels'][channel] and config['routes'][level][channel]

    def emit(self,event,key,*,goal_id=None,task_id=None,item_id=None,source=None,summary=None):
        if goal_id:
            try:self.store.get('deleted_goal',goal_id);return None
            except ValueError:pass
        identifier=hashlib.sha256(key.encode()).hexdigest()[:24]
        try:return self.store.get('notification',identifier)
        except ValueError:pass
        level,label,default_summary=EVENTS[event];summary=summary or default_summary;title='Muse Platform';generation=None
        if goal_id:
            try:
                goal=self.store.get('goal',goal_id);title=goal.get('title') or 'Goal';generation=goal.get('generation')
            except ValueError:pass
        elif task_id:
            try:title=self.store.get('task',task_id).get('title','Meta Work')
            except ValueError:pass
        config=self.config()
        record={'id':identifier,'event':event,'severity':level,'event_label':label,
            'title':safe_text(title),'summary':safe_text(summary,160),'goal_id':goal_id,'task_id':task_id,'plan_item_id':item_id,
            'source':source or {},'generation':generation,'created':self.clock(),'unread':self.enabled(config,level,'app'),
            'resolved':level=='info','delivery_seq':0,'last_attempt':None,'browser_claimed_seq':0,'sound_claimed_seq':0,
            'native':{'status':'not_requested','toast_submitted':False,'sound_requested':False}}
        return self.store.save('notification',{**record,**self.display_fields(record)})

    def display_fields(self,row):
        if not row.get('goal_id'):return {} # Existing Meta Work notifications are unchanged.
        try:goal=self.store.get('goal',row['goal_id'])
        except ValueError:return {}
        korean=ko(goal);event=row['event'];label,summary=EVENTS[event][1:] if korean else EN_EVENTS[event]
        source=row.get('source',{});decision=None
        if event=='waiting_decision':
            count=sum(d['goal_id']==goal['id'] and d['state']=='pending' for d in self.store.items('decision',None))
            label=f'진행 중단: 판단 {count}건 대기' if korean else f'Progress stopped: {count} pending decisions'
            summary='실행 가능한 항목이 없습니다. 판단에 답하거나 의존성·대기 이유를 확인해 주세요.' if korean else 'No runnable work remains. Answer decisions or inspect dependency/wait reasons.'
        if event.startswith('directive_'):
            try:
                message=self.store.get('message',source['id']);brief=message.get('human_brief') or {}
                if brief.get('language')==('Korean' if korean else 'English'):
                    summary=brief.get('summary','')+' '+ ' '.join(brief.get('unapplied',[])[:1])
            except ValueError:pass
        if event=='no_runnable':summary=goal.get('scheduling_human_summary') or summary
        if event=='watchdog' and source.get('kind')=='goal_watchdog':
            try:
                if self.store.get('goal_watchdog',source['id']).get('condition','').startswith('inactive_10m:'):
                    summary='사용자 일시정지가 아닌 중단 상태가 10분 이상 지속됐습니다. 재개 가능 여부를 확인해 주세요.' if korean else 'The Goal has been automatically paused/interrupted for at least 10 minutes. Inspect before resuming.'
            except ValueError:pass
        try:
            if source.get('kind')=='decision':decision=self.store.get('decision',source['id'])
            elif source.get('kind')=='goal_approval':decision=next((d for d in self.store.items('decision',None) if d.get('approval_id')==source['id']),None)
            elif source.get('kind')=='message':
                refs=self.store.get('message',source['id']).get('evidence_refs',[])
                decision=next((d for d in self.store.items('decision',None) if d['id'] in refs),None)
        except ValueError:pass
        brief=(decision or {}).get('human_brief')
        if brief and brief.get('language')==('Korean' if korean else 'English'):
            summary=('Builder 실행이 실패했습니다. 앱에서 실패 단계와 오류를 확인해 주세요.' if korean else 'Builder execution failed. Inspect the stage and error in the app.') if brief.get('failure') else brief['summary']+' '+brief['answer_helps']['reason']
        return {'event_label':safe_text(label),'summary':safe_text(summary,160),'title':safe_text(goal.get('title') or 'Goal'),
                'human_display_language':'Korean' if korean else 'English'}

    def process(self,kind,row,catchup=False):
        goal_id=row['id'] if kind=='goal' else row.get('goal_id')
        if goal_id:
            try:self.store.get('deleted_goal',goal_id);return
            except ValueError:pass
            try:
                if self.store.get('goal',goal_id).get('archived'):return
            except ValueError:pass
        if kind in ('plan_item','goal_approval'):
            try:
                if self.store.get('goal',row['goal_id'])['status']=='stopped':return
            except ValueError:return
        old=self.previous.get((kind,row['id']),{})
        if kind in ('goal','plan_item','task'):self.previous[(kind,row['id'])]=row
        transition=row.get('updated')
        if kind in ('goal','plan_item'):
            cursor_id=kind+':'+row['id'];cursor=self.cursors.get(cursor_id)
            if not cursor or cursor['status']!=row['status'] or (kind=='goal' and cursor.get('generation')!=row.get('generation')):
                cursor=self.store.save('notification_state',{'id':cursor_id,'status':row['status'],'since':transition,'generation':row.get('generation')})
                self.cursors[cursor_id]=cursor
            transition=cursor['since']
        if kind=='decision' and row['state']=='pending':
            event='human_checkpoint' if row.get('source','').startswith('checkpoint:') else 'question'
            key='decision:'+row['id']
            if row.get('approval_id'):
                request=self.store.get('goal_approval',row['approval_id'])
                event={'command':'command_approval','gpu':'gpu_approval'}.get(request['kind'],'question');key='approval:'+request['id']
            self.emit(event,key,goal_id=row['goal_id'],source={'kind':'decision','id':row['id']})
        elif kind=='goal_approval' and row['state']=='pending':
            if any(d.get('approval_id')==row['id'] for d in self.store.items('decision',None)):return
            event={'command':'command_approval','gpu':'gpu_approval'}.get(row['kind'],'question')
            self.emit(event,'approval:'+row['id'],goal_id=row['goal_id'],task_id=row['task_id'],item_id=row['plan_item_id'],source={'kind':'goal_approval','id':row['id']})
        elif kind=='task' and not row.get('goal_id') and row['status']=='waiting_approval' and row.get('approval'):
            approval=row['approval'];event='gpu_approval' if approval['kind']=='gpu' else 'command_approval'
            self.emit(event,'work-approval:'+approval['id'],task_id=row['id'],source={'kind':'task_approval','id':row['id'],'approval_id':approval['id']})
        elif kind=='message':
            if row.get('type')=='ACK' and row.get('ack_status') in ('partial','rejected','applied'):
                event={'partial':'directive_partial','rejected':'directive_rejected','applied':'directive_applied'}[row['ack_status']]
                self.emit(event,'directive-ack:'+row['id'],goal_id=row['goal_id'],item_id=row.get('plan_item_id'),source={'kind':'message','id':row['id'],'directive_id':row.get('directive_id')})
            elif row.get('type')=='ESCALATE':
                for ref in row.get('evidence_refs',[]):
                    if not isinstance(ref,str):continue
                    try:self.store.get('goal_approval',ref);return
                    except (ValueError,TypeError):pass
                    try:self.store.get('decision',ref);return
                    except (ValueError,TypeError):pass
                if row.get('coordinator'):
                    try:
                        if self.store.get('goal',row['goal_id']).get('platform_error'):return
                    except ValueError:pass
                event='human_checkpoint' if row.get('checkpoint') else 'escalate'
                key='checkpoint:'+row['goal_id']+':'+str(row.get('plan_item_id') or row['id']) if event=='human_checkpoint' else 'message:'+row['id']
                self.emit(event,key,goal_id=row['goal_id'],item_id=row.get('plan_item_id'),source={'kind':'message','id':row['id'],'created':row.get('created',0)})
            elif row.get('event') in ('human_checkpoint','milestone_complete'):
                event='human_checkpoint' if row['event']=='human_checkpoint' else 'milestone_done'
                self.emit(event,row['event']+':'+row['id'],goal_id=row['goal_id'],item_id=row.get('plan_item_id'),source={'kind':'message','id':row['id']})
        elif kind=='goal':
            if catchup or row['status']!=old.get('status'):
                event=None;reason=str(row.get('reason',''))
                if row['status']=='blocked':event='no_runnable' if row.get('scheduling_blocked') else 'coordinator_error' if row.get('platform_error') else 'goal_blocked'
                elif row['status']=='interrupted':event='interrupted'
                elif row['status']=='done' and not catchup:event='goal_done'
                elif row['status']=='waiting_decision':event='waiting_decision'
                elif row['status']=='paused':
                    if re.search(r'budget|allowance|상한|예산|비용',reason,re.I):event='budget_pause'
                    elif re.search(r'deadline|마감',reason,re.I):event='deadline_pause'
                    elif row.get('no_progress',0)>=3 or re.search(r'without new|진행 없음',reason,re.I):event='no_progress_pause'
                if event:self.emit(event,f"goal:{row['id']}:{event}:{transition}",goal_id=row['id'],source={'kind':'goal','id':row['id']})
            if not self.ledger:
                for warning in set(row.get('budget_warnings',[]))-set(old.get('budget_warnings',[])):
                    provider=next((p for p in ('baseten','meta','modal') if p in warning.lower()),'provider')
                    self.emit('budget_80','budget:'+row['id']+':'+provider,goal_id=row['id'])
            if row['status']!='stopped':self.milestones(row['id'])
        elif kind=='plan_item':
            if row['status']=='blocked' and (catchup or old.get('status')!='blocked'):
                self.emit('item_blocked',f"item:{row['id']}:blocked:{transition}",goal_id=row['goal_id'],item_id=row['id'],source={'kind':'plan_item','id':row['id']})
            if row['status']=='done' and old.get('status')!='done':
                if re.search(r'human checkpoint|사람 확인|사용자 확인',row['title'],re.I):
                    if not any(d.get('source')=='checkpoint:'+row['id'] for d in self.store.items('decision',None)):
                        self.emit('human_checkpoint','checkpoint:'+row['goal_id']+':'+row['id'],goal_id=row['goal_id'],item_id=row['id'],source={'kind':'checkpoint','id':row['id']})
                self.milestones(row['goal_id'])
        elif kind=='ledger' and self.ledger:self.budgets(row)

    def milestones(self,goal_id):
        groups={}
        items=[item for item in self.store.items('plan_item',None) if item['goal_id']==goal_id]
        by_id={item['id']:item for item in items}
        for item in items:
            match=re.match(r'^(M\d+(?:\.\d+)?)(?:\s|:)',item['title'],re.I)
            if match:groups.setdefault(match[1].upper(),[]).append(item)
        for name,members in groups.items():
            if any(i['status']=='done' for i in members) and all(i['status'] in ('done','superseded') for i in members):self.emit('milestone_done','milestone:'+goal_id+':'+name,goal_id=goal_id,source={'kind':'milestone','id':name})
        for parent in items:
            children=[i for i in items if i.get('parent')==parent['id']]
            if not children or re.match(r'^M\d+(?:\.\d+)?(?:\s|:)',parent['title'],re.I):continue
            descendants=set();pending=[i['id'] for i in children]
            while pending:
                child=pending.pop()
                if child in descendants:continue
                descendants.add(child);pending.extend(i['id'] for i in items if i.get('parent')==child)
            if parent['status']=='done' and all(by_id[i]['status'] in ('done','superseded') for i in descendants):
                self.emit('milestone_done','milestone:'+goal_id+':node:'+parent['id'],goal_id=goal_id,source={'kind':'milestone','id':parent['id']})

    def budgets(self,row):
        provider=row['provider']
        if provider not in self.ledger.prices['limits']:return
        records=[r for r in self.ledger.rows() if r['provider']==provider and r['state']!='released']
        scopes=[('platform',None,self.ledger.prices['limits'][provider])]
        if row.get('goal_id'):
            try:goal=self.store.get('goal',row['goal_id']);scopes.append((goal['id'],goal['id'],goal['budgets'][provider]))
            except ValueError:pass
        for scope,gid,limits in scopes:
            selected=records if gid is None else [r for r in records if r.get('goal_id')==gid]
            caps=[('total',limits['total'],sum(self.ledger.amount(r) for r in selected),'전체')]
            if provider=='baseten':
                for product in (False,True):
                    cap=limits.get('reserve',0) if product else max(0,limits['total']-limits.get('reserve',0))
                    caps.append(('product' if product else 'development',cap,sum(self.ledger.amount(r) for r in selected if (r.get('purpose')=='product')==product),'제품 예비분' if product else '개발'))
            if provider=='modal':
                cap=limits.get('llm',30)
                if scope=='platform':
                    try:cap=self.store.get('settings','muse_open').get('modal_llm_cap',cap)
                    except ValueError:pass
                caps.append(('llm',cap,sum(self.ledger.amount(r) for r in selected if r.get('workload')=='llm'),'LLM'))
            for label,cap,used,description in caps:
                if cap and used>=cap*.8:self.emit('budget_80',f'budget:{scope}:{provider}:{label}:{cap}',goal_id=gid or row.get('goal_id'),source={'kind':'budget','provider':provider,'scope':'platform' if gid is None else 'goal'},summary=f'{"플랫폼" if gid is None else "Goal"} {provider} {description} 예산의 80% 이상이 사용 또는 예약됐습니다.')
            if limits.get('daily'):
                zone=self.ledger.prices.get('timezone','America/New_York') if gid is None else goal['timezone']
                start=self.ledger.day_start(zone)
                if sum(self.ledger.amount(r) for r in selected if r['created']>=start)>=limits['daily']*.8:
                    self.emit('budget_80',f'budget:{scope}:{provider}:day:{start}:{limits["daily"]}',goal_id=gid or row.get('goal_id'),source={'kind':'budget','provider':provider,'scope':'platform' if gid is None else 'goal'})

    def resolved_by_source(self,row):
        source=row['source'];kind=source.get('kind')
        try:
            if kind=='goal_watchdog':return not self.store.get(kind,source['id']).get('active',False)
            if kind=='budget' and source.get('scope')=='platform':return False
            if row.get('goal_id') and self.store.get('goal',row['goal_id'])['status']=='stopped':return True
            if kind=='goal_approval':return self.store.get(kind,source['id'])['state']!='pending'
            if kind=='decision':return self.store.get(kind,source['id'])['state']!='pending'
            if kind=='task_approval':
                task=self.store.get('task',source['id'])
                return task['status']!='waiting_approval' or (task.get('approval') or {}).get('id')!=source['approval_id']
            if kind=='plan_item':return self.store.get(kind,source['id'])['status']!='blocked' or self.store.get('goal',row['goal_id'])['status']=='done'
            if row.get('goal_id'):
                goal=self.store.get('goal',row['goal_id'])
                if row['event'].startswith('directive_'):
                    directive=self.store.get('goal_directive',source['directive_id'])
                    resolved=directive.get('resolved_by')
                    return bool(resolved and self.store.get('goal_directive',resolved)['state']=='applied')
                if row['event']=='no_runnable':return goal.get('generation')!=row.get('generation') or goal['status'] not in ('blocked','waiting_decision')
                if row['event']=='waiting_decision':return goal['status']!='waiting_decision'
                if row['event']=='human_checkpoint':
                    if goal['status']=='stopped':return True
                    return any(m['goal_id']==row['goal_id'] and m.get('from')=='Human' and m['type']=='ANSWER' and m.get('created',0)>row['created'] and m.get('plan_item_id')==row.get('plan_item_id') for m in self.store.items('message',None))
                if goal['status'] in ('done','stopped') or goal.get('generation')!=row.get('generation'):return True
                if kind=='goal':return goal['status'] in ('running','planning')
                if kind=='message':
                    return any(m['goal_id']==row['goal_id'] and m.get('from')=='Human' and m['type']=='ANSWER' and m.get('created',0)>source.get('created',row['created']) and m.get('plan_item_id')==row.get('plan_item_id') for m in self.store.items('message',None))
        except ValueError:return True
        return False

    def tick(self):
        if self.watchdog:
            try:self.watchdog.tick()
            except Exception:pass
        with self.lock:
            while True:
                try:kind,row,catchup=self.events.get_nowait()
                except queue.Empty:break
                try:self.process(kind,row,catchup)
                except Exception:pass
            config=self.config();now=self.clock()
            for row in self.store.items('notification',None):
                if row.get('goal_id'):
                    try:
                        if self.store.get('goal',row['goal_id']).get('archived'):continue
                    except ValueError:pass
                if not row['resolved'] and self.resolved_by_source(row):
                    self.store.update('notification',row['id'],resolved=True,unread=False,resolved_at=now);continue
                if row['resolved'] and row['severity']!='info':continue
                interval=config['repeat_minutes']*60
                due=row['delivery_seq']==0 or (row['severity']=='urgent' and not row['resolved'] and interval and now-row['last_attempt']>=interval)
                if not due:continue
                level=row['severity'];seq=row['delivery_seq']+1
                self.store.update('notification',row['id'],delivery_seq=seq,last_attempt=now,native={'status':'queued','toast_submitted':False,'sound_requested':False})
                toast=self.enabled(config,level,'windows');sound=level=='urgent' and config['enabled'][level] and config['sound']
                if toast or sound:
                    work=(row['id'],seq,toast,sound)
                    if self.thread and not self.stopping.is_set():self.native_events.put(work)
                    else:self.deliver_native(*work)
                else:self.store.update('notification',row['id'],native={'status':'disabled','toast_submitted':False,'sound_requested':False})

    def url(self,row):
        query={}
        if row.get('goal_id'):query['goal']=row['goal_id']
        elif row.get('task_id'):query['task']=row['task_id']
        query['notification']=row['id']
        return self.base_url+'/?'+urlencode(query)

    def deliver_native(self,identifier,seq,toast,sound):
        try:
            row=self.store.get('notification',identifier)
            if row['delivery_seq']!=seq or (row['resolved'] and row['severity']!='info'):return
            config=self.config();toast=toast and self.enabled(config,row['severity'],'windows');sound=sound and config['sound'] and config['enabled']['urgent']
            if not toast and not sound:
                self.store.update('notification',identifier,native={'status':'disabled','toast_submitted':False,'sound_requested':False});return
            shown={**row,**self.display_fields(row)}
            result=self.native.send({'title':shown['title'],'body':shown['event_label']+' · '+shown['summary'],'url':self.url(row),'tag':identifier},toast=toast,sound=sound)
        except Exception:result={'status':'failed','toast_submitted':False,'sound_requested':False}
        if self.stopping.is_set():return
        with self.lock:
            try:row=self.store.get('notification',identifier)
            except ValueError:return
            if row['delivery_seq']==seq:self.store.update('notification',identifier,native=result)

    def public(self,row):
        return {**{k:copy.deepcopy(row[k]) for k in ('id','event','severity','event_label','title','summary','goal_id','task_id','plan_item_id',
                                                 'created','unread','resolved','delivery_seq','native')},
                **self.display_fields(row),'decision_id':row['source'].get('id') if row['source'].get('kind')=='decision' else None}

    def inbox(self):
        config=self.config();rows=sorted(self.store.items('notification',None),key=lambda r:r['created'],reverse=True)
        archived={r['id'] for r in self.store.items('goal',None) if r.get('archived')}
        rows=[row for row in rows if row.get('goal_id') not in archived]
        visible=[self.public(r) for r in rows if self.enabled(config,r['severity'],'app')]
        browser=[self.public(r) for r in rows if r['delivery_seq']>r['browser_claimed_seq'] and self.enabled(config,r['severity'],'browser') and (not r['resolved'] or r['severity']=='info')]
        sounds=[self.public(r) for r in rows if r['severity']=='urgent' and not r['resolved'] and r['delivery_seq']>r['sound_claimed_seq'] and config['sound'] and config['enabled']['urgent'] and r['native']['status']!='queued' and not r['native'].get('sound_requested')]
        return {'items':visible[:200],'unread_count':sum(r['unread'] for r in visible),
                'urgent':[r for r in visible if r['severity']=='urgent' and not r['resolved']],
                'browser':browser[:100],'sounds':sounds[:100],'settings':config}

    def read(self,identifier=None):
        with self.lock:
            rows=[self.store.get('notification',identifier)] if identifier else self.store.items('notification',None)
            for row in rows:
                if row['unread']:self.store.update('notification',row['id'],unread=False)
        return {'ok':True}

    def resolve(self,identifier):
        with self.lock:
            row=self.store.get('notification',identifier)
            if row['source'].get('kind') in ('goal_approval','task_approval','decision') and not self.resolved_by_source(row):raise ValueError('대기 중인 승인·판단은 해당 화면에서 답변 또는 처리하세요.')
            if row['event']=='waiting_decision' and not self.resolved_by_source(row):raise ValueError('남은 판단을 처리하면 대기 배너가 해제됩니다.')
            self.store.update('notification',identifier,unread=False,resolved=True,resolved_at=self.clock())
        return {'ok':True}

    def claim(self,identifier,seq,channel):
        if channel not in ('browser','sound') or type(seq) is not int:raise ValueError('알림 전달 요청을 확인하세요.')
        with self.lock:
            row=self.store.get('notification',identifier);field=channel+'_claimed_seq';config=self.config()
            allowed=self.enabled(config,row['severity'],'browser') if channel=='browser' else row['severity']=='urgent' and config['sound'] and config['enabled']['urgent'] and not row['native'].get('sound_requested') and row['native']['status']!='queued'
            if seq!=row['delivery_seq'] or seq<=row[field] or not allowed or (row['resolved'] and row['severity']!='info'):return {'claimed':False}
            self.store.update('notification',identifier,**{field:seq});return {'claimed':True}

    def start(self,base_url):
        if self.thread:return
        self.base_url=base_url
        self.thread=threading.Thread(target=self.run,daemon=True,name='muse-notifications')
        self.native_thread=threading.Thread(target=self.run_native,daemon=True,name='muse-windows-notifications')
        self.thread.start();self.native_thread.start()

    def run(self):
        while not self.stopping.is_set():
            self.wake.clear()
            try:self.tick()
            except Exception:pass
            self.wake.wait(1)

    def run_native(self):
        while not self.stopping.is_set():
            try:work=self.native_events.get(timeout=.5)
            except queue.Empty:continue
            try:self.deliver_native(*work)
            except Exception:pass

    def stop(self):
        self.stopping.set();self.wake.set()
        for thread in (self.thread,self.native_thread):
            if thread and thread is not threading.current_thread():thread.join(timeout=1)
