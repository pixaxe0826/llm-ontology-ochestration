"""Inactive Goal lifecycle. Ledger, workspaces, task/GPU results and memory survive."""
import json
import time

ARCHIVABLE = {'done', 'stopped', 'paused'}
OWNED_KINDS = ('goal_directive','goal_inbox_attempt','goal_item_action','plan_item','message','decision','goal_approval','goal_evidence','goal_setting_change',
               'goal_reference_page','goal_long_command','goal_commit','goal_acceptance_change','goal_plan_capacity',
               'goal_restart','goal_model_attempt','goal_watchdog','goal_watchdog_state',
               'goal_recovery','goal_review_attempt','goal_plan_attempt','goal_review_reset','goal_file_read',
               'goal_file_read_index','goal_chunk_summary','goal_chunk_state','goal_stall_replan','goal_dependency_change','goal_provisional_file')


def assert_unarchived(goal):
    if goal.get('archived'):
        raise ValueError('아카이브한 Goal은 먼저 복원하세요.')
    return goal


class GoalHistory:
    def __init__(self, app):self.app=app

    def related(self, goal_id):
        tasks=[r for r in self.app.store.items('task',None) if r.get('goal_id')==goal_id]
        task_ids={r['id'] for r in tasks}
        jobs=[r for r in self.app.store.items('gpu',None) if r.get('goal_id')==goal_id or r.get('task_id') in task_ids]
        return tasks,jobs

    def inactive(self, goal):
        if goal['status'] not in ARCHIVABLE:
            raise ValueError('완료·중지·일시정지 Goal만 아카이브할 수 있습니다. 먼저 Goal을 중지하세요.')
        tasks,jobs=self.related(goal['id']);app=self.app
        if app.goals.long_commands.active(goal['id']):raise ValueError('장시간 명령이 실행 중입니다. 완료를 기다리거나 Goal을 중지하세요.')
        if (any(r['id'] in app.agents.active or r['id'] in app.goals.builders.active or r['status'] in ('running','queued','waiting_approval') for r in tasks)
                or any(r['id'] in app.gpu.active or r['status'] in ('running','queued') for r in jobs)
                or any(r['state']=='reserved' for r in app.goals.ledger.rows(goal['id']))
                or (app.goals.threads.get(goal['id']) is not None and app.goals.threads[goal['id']].is_alive())):
            raise ValueError('이 Goal의 단계 또는 GPU 작업이 종료 중입니다. 실행이 끝난 뒤 다시 시도하세요.')
        return tasks,jobs

    def preview(self, goal_id):
        app=self.app;app.memory.pending.join()
        with app.goals.lock,app.memory.lock:
            goal=app.store.get('goal',goal_id);tasks,jobs=self.related(goal_id)
            reason=''
            try:self.inactive(goal)
            except ValueError as error:reason=str(error)
            return {'goal':goal,'can_archive':not goal.get('archived') and not reason,
                'can_delete':bool(goal.get('archived')) and not reason,'reason':reason,
                'memory_records':len(app.memory.goal_sources(goal_id,tasks,jobs)),
                'gpu_jobs':len(jobs),'gpu_results':sum(bool(r.get('result',{}).get('volume') or r.get('result',{}).get('retained_outputs')) for r in jobs if isinstance(r.get('result'),dict))}

    def manage(self, data):
        if not isinstance(data,dict) or set(data)-{'goal_id','action','confirmation_title','exclude_memory','confirmed'}:
            raise ValueError('Goal 관리 요청을 확인하세요.')
        action=data.get('action')
        if action not in ('archive','restore','delete'):raise ValueError('Goal 관리 동작을 확인하세요.')
        app=self.app;goal_id=data.get('goal_id');app.memory.pending.join()
        # Notification delivery cannot recreate rows during deletion. Memory uses memory -> store.
        with app.notifications.lock,app.goals.lock,app.agents.lock,app.goals.builders.lock,app.gpu.lock,app.memory.lock,app.store.lock:
            goal=app.store.get('goal',goal_id)
            tasks,jobs=self.inactive(goal)
            if action=='restore':
                if not goal.get('archived'):raise ValueError('아카이브한 Goal만 복원할 수 있습니다.')
                app.store.update('goal',goal_id,archived=False)
            elif action=='archive':
                if not goal.get('archived'):app.store.update('goal',goal_id,archived=True,archived_at=time.time())
                app.goals.proxy_tokens.pop(goal_id,None)
            else:
                if not goal.get('archived'):raise ValueError('아카이브한 Goal만 삭제할 수 있습니다.')
                if data.get('confirmed') is not True or not isinstance(data.get('confirmation_title'),str) or data['confirmation_title']!=goal['title']:
                    raise ValueError('삭제하려면 Goal 제목을 정확히 다시 입력하세요.')
                if type(data.get('exclude_memory',False)) is not bool:raise ValueError('메모리 공유 제외는 켜기/끄기로 지정하세요.')
                excluded=app.memory.exclude_goal(goal_id,tasks,jobs) if data.get('exclude_memory',False) else 0
                rows=[('goal',goal_id)]
                for kind in OWNED_KINDS:
                    rows.extend((kind,row['id']) for row in app.store.items(kind,None) if row.get('goal_id')==goal_id)
                item_ids={identifier for kind,identifier in rows if kind=='plan_item'}
                rows.extend(('notification',row['id']) for row in app.store.items('notification',None) if row.get('goal_id')==goal_id)
                cursor_ids={'goal:'+goal_id,*('plan_item:'+identifier for identifier in item_ids)}
                rows.extend(('notification_state',identifier) for identifier in cursor_ids)
                # A minimal tombstone rejects stale queued notification events after deletion/restart.
                with app.store.db:
                    app.store.db.execute('INSERT OR REPLACE INTO records VALUES(?,?,?,?)',
                        ('deleted_goal',goal_id,json.dumps({'id':goal_id,'deleted_at':time.time()}),time.time()))
                    app.store.db.executemany('DELETE FROM records WHERE kind=? AND id=?',rows)
                for key in list(app.notifications.previous):
                    if key==('goal',goal_id) or key[0]=='plan_item' and key[1] in item_ids:app.notifications.previous.pop(key,None)
                for identifier in cursor_ids:app.notifications.cursors.pop(identifier,None)
                for cache in (app.goals.threads,app.goals.signals,app.goals.decision_events,app.goals.proxy_tokens):cache.pop(goal_id,None)
                return {'ok':True,'action':action,'memory_excluded':excluded,'ledger_preserved':True,'workspace_preserved':True,'gpu_results_preserved':True}
        return {'ok':True,'action':action}
