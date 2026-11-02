"""Local temporary Git repositories; no remotes, provider calls or user Git changes."""
import hashlib,json,os,subprocess,time,unittest
from pathlib import Path
from unittest.mock import patch
import test_goals as fixtures
from workbench.git_checkpoint import Git,IGNORE,enabled
from workbench.security import child_environment,register_secret

class CommitTests(unittest.TestCase):
    setUp=fixtures.GoalTests.setUp
    tearDown=fixtures.GoalTests.tearDown
    create=fixtures.GoalTests.create

    def git(self,*args,okay=(0,)):
        hooks=self.base/'no-hooks';hooks.mkdir(exist_ok=True)
        r=subprocess.run(['git','-c','core.fsmonitor=false','-c','commit.gpgsign=false','-c','core.hooksPath='+str(hooks),*args],cwd=self.folder,env=child_environment(),capture_output=True,timeout=10)
        self.assertIn(r.returncode,okay,'Git fixture failed: '+args[0]);return r.stdout

    def prepare(self,initial=True):
        self.git('init','-b','main');self.git('config','user.name','Local Test Author');self.git('config','user.email','checkpoint@example.invalid')
        self.goal=self.create(start=False)
        self.manager.add_items(self.goal,[{'key':'P1','title':'Implement episode evaluation','acceptance':['Observed result exists.'],'depends_on':[],'blocked_by_decision':[]}])
        self.item=self.manager.items(self.goal['id'])[0]
        if initial:
            (self.folder/'baseline.txt').write_text('baseline\n',encoding='utf-8');self.git('add','baseline.txt');self.git('commit','-m','Initial local fixture')
        self.store.update('plan_item',self.item['id'],status='done',accepted_criteria={'Observed result exists.':['fixture']})

    def change(self,path,text):
        file=self.folder/path;file.parent.mkdir(parents=True,exist_ok=True);file.write_text(text,encoding='utf-8')
        item=self.store.get('plan_item',self.item['id']);refs=item['evidence']
        refs.append({'id':'fixture:'+str(len(refs)),'kind':'change','path':path,'file_sha256':hashlib.sha256(file.read_bytes()).hexdigest(),
            'sha256':hashlib.sha256((path+text).encode()).hexdigest(),'files':{}})
        self.store.update('plan_item',self.item['id'],evidence=refs)

    def commit(self,acceptance='review-1'):
        return self.manager.auto_commit_item(self.goal['id'],self.item['id'],acceptance)

    def test_accepted_item_commits_with_user_author_exact_message_hash_and_evidence(self):
        self.prepare();self.change('evaluate.py','print("episode")\n');row=self.commit()
        self.assertEqual(row['state'],'committed',row);self.assertEqual(self.git('log','-1','--format=%s').decode().strip(),'[goal] Implement episode evaluation accepted (P1)')
        self.assertEqual(self.git('log','-1','--format=%an').decode().strip(),'Local Test Author')
        self.assertEqual(self.git('rev-parse','HEAD').decode().strip(),row['commit_hash'])
        item=self.store.get('plan_item',self.item['id']);self.assertEqual(item['commit_hash'],row['commit_hash'])
        ref=next(e for e in item['evidence'] if e['kind']=='git_commit');self.assertEqual(ref['commit_hash'],row['commit_hash'])
        observed=self.manager.observed_evidence(self.goal,item);self.assertFalse(observed[ref['id']]['supports_acceptance'])
        self.assertEqual(self.store.get('goal_evidence',ref['observation_id'])['result']['commit_hash'],row['commit_hash'])
        self.assertFalse((self.folder/'.git/index.lock').exists());self.assertEqual(self.git('status','--porcelain'),b'')

    def test_existing_unrelated_user_staging_and_worktree_are_preserved(self):
        self.prepare();(self.folder/'user.txt').write_text('staged user work\n');self.git('add','user.txt')
        (self.folder/'user.txt').write_text('later user work\n');(self.folder/'unrelated.txt').write_text('untracked user work\n')
        before=self.git('diff','--cached','--binary');self.change('episode.py','print(3)\n');row=self.commit()
        self.assertEqual(row['state'],'committed',row);self.assertEqual(before,self.git('diff','--cached','--binary'))
        self.assertEqual((self.folder/'user.txt').read_text(),'later user work\n')
        names=self.git('ls-tree','-r','--name-only','HEAD').decode().splitlines()
        self.assertNotIn('user.txt',names);self.assertNotIn('unrelated.txt',names);self.assertIn('episode.py',names)

    def test_private_exclusions_are_added_and_files_are_never_committed(self):
        self.prepare();self.change('result.txt','safe\n')
        for path in ('references/photo.png','.env','.env.local','private.key','.venv/local.txt','.workbench/state.txt'):
            file=self.folder/path;file.parent.mkdir(exist_ok=True,parents=True);file.write_bytes(b'synthetic private fixture')
        row=self.commit();self.assertEqual(row['state'],'committed',row);self.assertTrue(row['ignore_added'])
        names=self.git('ls-tree','-r','--name-only','HEAD').decode().splitlines();self.assertEqual(set(names),{'baseline.txt','result.txt','.gitignore'})
        self.assertTrue((self.folder/'references/photo.png').exists());self.assertIn('*.key',(self.folder/'.gitignore').read_text())
        count=len((self.folder/'.gitignore').read_text().splitlines());self.change('result.txt','safe v2\n');self.commit('review-2')
        self.assertEqual(len((self.folder/'.gitignore').read_text().splitlines()),count)

    def test_ignore_negation_is_overridden_by_final_private_rules(self):
        self.prepare();(self.folder/'.gitignore').write_text('.env*\n!.env.local\n/references/\n')
        self.change('safe.txt','safe\n');row=self.commit();self.assertEqual(row['state'],'committed',row)
        self.assertIn('.env*',row['ignore_added']);self.assertIn('references/',row['ignore_added'])
        self.assertTrue(self.git('check-ignore','.env.local'));self.assertTrue(self.git('check-ignore','nested/references/a.png'))

    def test_missing_author_warns_and_does_not_change_author_or_goal_status(self):
        self.prepare();self.git('config','user.email','');head=self.git('rev-parse','HEAD');self.change('safe.txt','safe\n')
        before=self.store.get('goal',self.goal['id']);row=self.commit()
        self.assertEqual(row['state'],'warning');self.assertIn('user.name and user.email',row['warning']);self.assertEqual(self.git('rev-parse','HEAD'),head)
        self.assertEqual(self.store.get('goal',self.goal['id']),before);self.assertEqual(self.git('config','--get','user.email'),b'\n')
        self.assertEqual(self.store.get('plan_item',self.item['id'])['status'],'done')

    def test_absent_repo_warns_without_initializing_or_stopping_goal(self):
        self.goal=self.create(start=False);self.manager.add_items(self.goal,[{'key':'P1','title':'Task','acceptance':['Verified.']}]);self.item=self.manager.items(self.goal['id'])[0]
        self.store.update('plan_item',self.item['id'],status='done');row=self.commit()
        self.assertEqual(row['state'],'warning');self.assertFalse((self.folder/'.git').exists());self.assertEqual(self.store.get('plan_item',self.item['id'])['status'],'done')

    def test_preexisting_staged_item_is_not_committed_or_replaced(self):
        self.prepare();self.change('evaluate.py','print(1)\n');self.git('add','evaluate.py');before=self.git('diff','--cached','--binary');head=self.git('rev-parse','HEAD')
        self.change('evaluate.py','print(2)\n');row=self.commit();self.assertEqual(row['state'],'warning')
        self.assertIn('staged changes',row['warning']);self.assertEqual(self.git('diff','--cached','--binary'),before);self.assertEqual(self.git('rev-parse','HEAD'),head)

    def test_tracked_private_file_blocks_commit_without_reading_or_untracking_it(self):
        self.prepare();(self.folder/'.env').write_text('synthetic fixture, not a key');self.git('add','.env');self.git('commit','-m','Private tracking fixture')
        self.change('safe.txt','safe');head=self.git('rev-parse','HEAD');row=self.commit()
        self.assertEqual(row['state'],'warning');self.assertIn('already tracked',row['warning']);self.assertEqual(self.git('rev-parse','HEAD'),head);self.assertTrue((self.folder/'.env').exists())

    def test_registered_synthetic_secret_is_rejected_before_git_object_write(self):
        self.prepare();secret='synthetic-checkpoint-secret-not-a-real-key-9318';register_secret(secret);self.change('candidate.txt',secret)
        head=self.git('rev-parse','HEAD');writes=[];original=Git.run
        def checked(git,*args,**kwargs):
            if args[0]=='hash-object':writes.append(kwargs.get('data',b''))
            return original(git,*args,**kwargs)
        with patch.object(Git,'run',checked):row=self.commit()
        self.assertEqual(row['state'],'warning');self.assertNotIn(secret,json.dumps(row));self.assertFalse(any(secret.encode() in data for data in writes));self.assertEqual(head,self.git('rev-parse','HEAD'))

    def test_same_acceptance_is_idempotent_and_empty_followup_does_not_create_commit(self):
        self.prepare();self.change('safe.txt','safe\n');first=self.commit();count=self.git('rev-list','--count','HEAD')
        self.assertEqual(self.commit()['id'],first['id']);self.assertEqual(self.git('rev-list','--count','HEAD'),count)
        second=self.commit('review-2');self.assertEqual(second['state'],'no_changes');self.assertEqual(self.git('rev-list','--count','HEAD'),count)

    def test_initial_commit_in_an_empty_repository_preserves_unrelated_staging(self):
        self.prepare(initial=False);(self.folder/'user.txt').write_text('user');self.git('add','user.txt');self.change('safe.txt','safe\n')
        row=self.commit();self.assertEqual(row['state'],'committed',row)
        self.assertNotIn('user.txt',self.git('ls-tree','-r','--name-only','HEAD').decode().splitlines())
        self.assertIn('user.txt',self.git('diff','--cached','--name-only').decode())

    def test_external_git_filter_and_hooks_never_execute(self):
        self.prepare();marker=self.folder/'unwanted-marker.txt';hook=self.folder/'.git/hooks/reference-transaction'
        hook.write_text('#!/bin/sh\nprintf bad > unwanted-marker.txt\n');hook.chmod(0o755)
        self.change('safe.txt','safe\n');self.assertEqual(self.commit()['state'],'committed');self.assertFalse(marker.exists())
        (self.folder/'.gitattributes').write_text('*.txt filter=unsafe\n');self.git('config','filter.unsafe.clean','echo forbidden')
        self.change('safe.txt','new safe\n');self.assertEqual(self.commit('review-2')['state'],'warning');self.assertFalse(marker.exists())

    def test_external_head_change_fails_cas_and_preserves_index(self):
        self.prepare();self.change('safe.txt','safe\n');head=self.git('rev-parse','HEAD');index=(self.folder/'.git/index').read_bytes();original=Git.run
        def fail(git,*args,**kwargs):
            if args[0]=='update-ref':raise OSError('synthetic reference failure')
            return original(git,*args,**kwargs)
        with patch.object(Git,'run',fail):row=self.commit()
        self.assertEqual(row['state'],'warning');self.assertEqual(self.git('rev-parse','HEAD'),head);self.assertEqual((self.folder/'.git/index').read_bytes(),index)
        self.assertFalse((self.folder/'.git/index.lock').exists());self.assertNotIn('commit_hash',row)

    def test_locked_repository_and_active_builder_are_warning_only(self):
        self.prepare();self.change('safe.txt','safe\n');lock=self.folder/'.git/index.lock';lock.write_bytes(b'user owned lock')
        self.assertEqual(self.commit()['state'],'warning');self.assertEqual(lock.read_bytes(),b'user owned lock');lock.unlink()
        self.manager.builder_gate.acquire()
        try:self.assertEqual(self.commit('review-2')['state'],'warning')
        finally:self.manager.builder_gate.release()

    def test_setting_default_off_validation_legacy_and_preservation(self):
        self.prepare();self.assertTrue(self.goal['auto_commit']);self.change('safe.txt','safe');self.manager.update(self.goal['id'],{'auto_commit':False})
        self.assertIsNone(self.commit());self.assertFalse(self.store.items('goal_commit',None))
        for value in ('true',1,None):
            with self.assertRaises(ValueError):self.manager.update(self.goal['id'],{'auto_commit':value})
        before=self.manager.detail(self.goal['id']);after=self.manager.update(self.goal['id'],{'auto_commit':True})
        for key in ('items','messages','ledger'):self.assertEqual(before[key],after[key])
        self.assertEqual(after['goal']['objective'],before['goal']['objective']);self.assertTrue(enabled())
        goal=self.store.get('goal',self.goal['id']);goal.pop('auto_commit');self.store.save('goal',goal)
        self.assertEqual(self.commit()['state'],'committed')

    def test_real_accept_review_invokes_checkpoint_without_extra_model_calls(self):
        self.prepare();file=self.folder/'safe.txt';file.write_text('safe\n');item=self.store.get('plan_item',self.item['id'])
        task=self.store.save('task',{'goal_id':self.goal['id'],'plan_item_id':item['id'],'workspace':str(self.folder),'status':'succeeded',
            'events':[{'kind':'change','time':time.time(),'change':{'path':'safe.txt','sha256':hashlib.sha256(file.read_bytes()).hexdigest(),'diff':'Added safe file.'}}]})
        self.store.update('plan_item',item['id'],status='review',task_ids=[task['id']]);refs,_=self.manager.collect_evidence(item,task)
        value={'decision':'accept','criteria':[{'criterion':'Observed result exists.','evidence_ids':[refs[0]['id']]}],'defects':[],'summary':'Accepted observed work.','next_items':[],'goal_complete':True}
        with patch.object(self.manager,'phase',return_value=json.dumps(value)):
            self.assertTrue(self.manager.review(self.goal,self.store.get('plan_item',item['id'])))
        self.assertEqual(self.store.get('plan_item',item['id'])['last_commit_state'],'committed')

    def test_snapshot_commit_preserves_a_later_worktree_edit(self):
        self.prepare();self.change('safe.txt','reviewed\n');original=Git.run
        def mutate(git,*args,**kwargs):
            if args[0]=='hash-object' and args[-1]=='--path=safe.txt':(self.folder/'safe.txt').write_text('later user edit\n')
            return original(git,*args,**kwargs)
        with patch.object(Git,'run',mutate):row=self.commit()
        self.assertEqual(row['state'],'committed',row);self.assertEqual(self.git('show','HEAD:safe.txt'),b'reviewed\n')
        self.assertEqual((self.folder/'safe.txt').read_text(),'later user edit\n')

    def test_interrupted_checkpoint_warns_on_restart_without_a_git_retry(self):
        from workbench.goals import GoalManager
        self.prepare();row=self.store.save('goal_commit',{'goal_id':self.goal['id'],'plan_item_id':self.item['id'],'acceptance_id':'pending','state':'running','created':time.time()})
        with patch.object(Git,'run',side_effect=AssertionError('No Git retry')):
            other=GoalManager(self.store,self.providers,self.manager.gpu,self.memory)
        try:
            self.assertEqual(self.store.get('goal_commit',row['id'])['state'],'warning');self.assertIn('restarted',self.store.get('plan_item',self.item['id'])['commit_warning'])
        finally:other.shutdown()

    def test_committed_hash_survives_index_replacement_failure_with_recovery_lock(self):
        self.prepare();self.change('safe.txt','safe\n');original=os.replace
        def fail(source,target):
            if Path(source).name=='index.lock' and Path(target).name=='index':raise PermissionError('synthetic index replacement denial')
            return original(source,target)
        with patch('workbench.git_checkpoint.os.replace',fail):row=self.commit()
        self.assertEqual(row['state'],'committed_with_warning');self.assertEqual(row['commit_hash'],self.git('rev-parse','HEAD').decode().strip())
        self.assertTrue((self.folder/'.git/index.lock').is_file());self.assertEqual(self.store.get('plan_item',self.item['id'])['status'],'done')

if __name__=='__main__':unittest.main()
