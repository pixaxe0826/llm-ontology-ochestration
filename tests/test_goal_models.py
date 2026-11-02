"""Goal-only model catalog, non-empty selection, and saved-setting preservation."""
import copy
import json
import os
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request,urlopen

from test_goals import GoalHTTPTests
from workbench.goal_models import BUILDER_MODELS,DEFAULT_BUILDER_MODEL,builder_catalog,validate_builder_model
from workbench.goals import GoalManager


class GoalModelTests(GoalHTTPTests):
    def post(self,path,data,token=True):
        return Request(self.url+path,data=json.dumps(data).encode(),headers={'Content-Type':'application/json',**({'X-Local-Token':self.app.token} if token else {})})

    def test_empty_and_unsupported_models_reject_create_without_side_effects(self):
        before=self.app.store.items('goal',None);defaults=self.app.store.get('settings','goal_defaults')
        for value in ('',' ',None,False,123,'muse-image-1.0','sam-3.1','muse-voice-transcribe-1.0','muse-spark-open','muse-spark-invalid'):
            with self.subTest(value=value),self.assertRaisesRegex(ValueError,'Builder 모델'):
                self.app.goals.create({'title':'Invalid model','objective':'Must not start','workspace':str(self.folder),'builder_model':value},start=False)
        self.assertEqual(self.app.store.items('goal',None),before);self.assertEqual(self.app.store.get('settings','goal_defaults'),defaults)

    def test_all_supported_models_and_default_are_valid(self):
        self.assertEqual(self.goal['builder_model'],DEFAULT_BUILDER_MODEL)
        for model in BUILDER_MODELS:self.assertEqual(validate_builder_model(model),model)
        self.assertEqual(builder_catalog([*BUILDER_MODELS,*BUILDER_MODELS,'muse-image-1.0','sam-3.1',None]),{'models':list(BUILDER_MODELS),'default':DEFAULT_BUILDER_MODEL})

    def test_empty_model_http_create_and_save_rejected_with_visible_reason(self):
        self.app.store.update('goal',self.goal['id'],status='paused')
        for path,payload in (('/api/goals',{'title':'Invalid','objective':'Must not run','workspace':str(self.folder),'builder_model':''}),
                             ('/api/goals/settings',{'goal_id':self.goal['id'],'builder_model':''})):
            with self.assertRaises(HTTPError) as caught:urlopen(self.post(path,payload))
            self.assertEqual(caught.exception.code,400);self.assertIn('Builder 모델',json.load(caught.exception)['error']);caught.exception.close()
        self.assertEqual(self.app.store.get('goal',self.goal['id'])['builder_model'],DEFAULT_BUILDER_MODEL)
        self.assertFalse(self.app.goals.settings_history(self.goal['id']))

    def test_catalog_filters_non_work_models_and_requires_session_authentication(self):
        ids=['muse-image-1.0','muse-spark-1.3-contributor','muse-spark-1.3','sam-3.1','muse-voice-transcribe-1.0','meta-models/muse-spark-open-1026']
        with patch.object(self.app.providers,'models',return_value=ids) as models:
            with self.assertRaises(HTTPError) as caught:urlopen(self.post('/api/goals/models',{},token=False))
            self.assertEqual(caught.exception.code,403);caught.exception.close();models.assert_not_called()
            with urlopen(self.post('/api/goals/models',{})) as result:body=json.load(result)
        self.assertEqual(body['models'],['muse-spark-1.3','muse-spark-1.3-contributor']);models.assert_called_once_with('meta')

    def test_catalog_failure_never_changes_saved_model_or_reasoning_and_never_leaks_keys(self):
        before=copy.deepcopy(self.app.store.items('goal',None));secret='synthetic-goal-model-catalog-secret-73491'
        with patch.dict(os.environ,{'MODEL_API_KEY':secret}),patch.object(self.app.providers,'models',side_effect=RuntimeError(secret)):
            with self.assertRaises(HTTPError) as caught:urlopen(self.post('/api/goals/models',{}))
            self.assertEqual(caught.exception.code,400);self.assertNotIn(secret,caught.exception.read().decode());caught.exception.close()
        self.assertEqual(self.app.store.items('goal',None),before)

    def test_saved_model_and_both_reasoning_values_survive_noop_save_and_manager_restart(self):
        self.app.store.update('goal',self.goal['id'],status='paused',builder_model='muse-spark-1.3-contributor',reasoning={'Planner':'xhigh','Builder':'medium'})
        before=self.app.goals.detail(self.goal['id'])
        result=self.app.goals.update(self.goal['id'],{'builder_model':before['goal']['builder_model'],'reasoning':before['goal']['reasoning']})
        self.assertEqual(result,before)
        restarted=GoalManager(self.app.store,self.app.goals.providers,self.app.gpu,self.app.memory)
        self.assertEqual(restarted.detail(self.goal['id']),before)

    def test_config_exposes_shared_validation_catalog_without_model_call(self):
        with patch.object(self.app.providers,'models') as models:
            config=self.app.handle('GET','/api/goals/config',{},{});models.assert_not_called()
        self.assertEqual(config['builder_models'],list(BUILDER_MODELS));self.assertEqual(config['builder_default'],DEFAULT_BUILDER_MODEL)
        self.assertEqual(self.app.goals.providers.calls,[])


for name in dir(GoalHTTPTests):
    if name.startswith('test_') and name not in GoalModelTests.__dict__:setattr(GoalModelTests,name,None)
del GoalHTTPTests,name
if __name__=='__main__':unittest.main()
