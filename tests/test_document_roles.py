"""Offline document grounding and roles. Never call models, web or GPUs."""
import hashlib,json,unittest
from pathlib import Path
from unittest.mock import patch
import test_document_acceptance as docs
from workbench.document_evidence import DocumentEvidenceError,document_bundle,format_check
from workbench.document_roles import role_for,recognize_license
from workbench.acceptance_rules import normalize_criteria
from workbench.workspace import Workspace


class DocumentRoleTests(unittest.TestCase):
    setUp=docs.DocumentAcceptanceTests.setUp
    tearDown=docs.DocumentAcceptanceTests.tearDown
    create=docs.DocumentAcceptanceTests.create
    def fixture(self,*args,**kwargs):
        runner=self.folder/'runner.py'
        if runner.exists():runner.unlink()
        return docs.DocumentAcceptanceTests.fixture(self,*args,**kwargs)
    write_doc=docs.DocumentAcceptanceTests.write_doc
    verdict=docs.DocumentAcceptanceTests.verdict

    def reference(self,body='Length 2990 mm (OBS).',kind='fetch_reference_page',source='Manufacturer length 2990 mm, width 500 mm, height 800 mm.'):
        goal,item,_=self.fixture('placeholder',path='review/VISUAL_REFERENCE.md')
        result=self.manager.reference_observation(goal,item,kind,{'content':source})
        self.write_doc(goal,item,'# Summary\n'+body.replace('OBS',result['evidence_id'])+'\n## Evidence\nSource observations.\n','review/VISUAL_REFERENCE.md')
        return goal,self.store.get('plan_item',item['id']),result

    def test_current_reference_observations_ground_dimensions_without_runs(self):
        for kind in ('reference_search','fetch_reference_page','fetch_reference_image','analyze_images'):
            with self.subTest(kind=kind):
                goal,item,_=self.reference(kind=kind)
                with patch.object(self.manager.builders,'start',side_effect=AssertionError('No Builder')):
                    self.assertTrue(self.manager.validate_review(goal,item,self.verdict(item))[0])
                checks=document_bundle(self.manager,goal,item)['criteria'][0]['files'][0]
                self.assertEqual(checks['doc_role'],'reference');self.assertIn('inferred',checks['doc_role_origin'])
    def test_reference_value_mismatch_and_stale_image_are_rejected(self):
        goal,item,result=self.reference('Length 2991 mm (OBS).')
        with self.assertRaisesRegex(DocumentEvidenceError,'mismatch'):self.manager.validate_review(goal,item,self.verdict(item))
        ws=Workspace(self.folder,self.store.root);change=ws.write('references/source.txt','Observed width 500 mm')
        observation=self.store.get('goal_evidence',result['evidence_id'].split(':',1)[1]);self.store.update('goal_evidence',observation['id'],files={'references/source.txt':change['sha256']})
        # Existing reference points at the original source hash; a changed source invalidates it.
        current=self.store.get('plan_item',item['id']);refs=current['evidence'];next(r for r in refs if r.get('observation_id')==observation['id'])['files']={'references/source.txt':change['sha256']};self.store.update('plan_item',item['id'],evidence=refs)
        (self.folder/'references/source.txt').write_text('Changed observation',encoding='utf-8')
        self.write_doc(goal,item,'# Summary\nLength 2990 mm ('+result['evidence_id']+').\n## Evidence\nSources.','review/VISUAL_REFERENCE.md')
        with self.assertRaisesRegex(DocumentEvidenceError,'not current|Stale'):self.manager.validate_review(goal,self.store.get('plan_item',item['id']),self.verdict(item))
    def test_reference_cannot_prove_tests_or_measured_performance(self):
        goal,item,_=self.reference('9 tests passed (OBS).',source='9 tests passed; measured latency 3 seconds')
        with self.assertRaisesRegex(DocumentEvidenceError,'successful execution'):self.manager.validate_review(goal,item,self.verdict(item))
    def test_mixing_a_run_cannot_launder_a_reference_result_metric(self):
        goal,item,_=self.reference('10 tests passed (OBS, run-evidence:1).',source='10 tests passed.')
        with self.assertRaisesRegex(DocumentEvidenceError,'claimed 10'):self.manager.validate_review(goal,item,self.verdict(item))
    def test_spec_key_current_value_hash_and_missing_key(self):
        citation='spec:spec/irm1.yaml#conveyors.CV1_infeed.belt_width'
        goal,item,_=self.reference('Width 500 mm ('+citation+').')
        ws=Workspace(self.folder,self.store.root);ws.write('spec/irm1.yaml','conveyors:\n  CV1_infeed:\n    belt_width: 500\n')
        self.assertTrue(self.manager.validate_review(goal,item,self.verdict(item))[0])
        check=document_bundle(self.manager,goal,item)['criteria'][0]['files'][0]
        self.assertEqual(check['spec_sources'][0]['spec_value'],500);self.assertEqual(check['spec_sources'][0]['file_sha256'],ws.read('spec/irm1.yaml')['sha256'])
        (self.folder/'spec/irm1.yaml').write_text('conveyors:\n  CV1_infeed:\n    belt_width: 600\n',encoding='utf-8')
        with self.assertRaisesRegex(DocumentEvidenceError,'mismatch'):self.manager.validate_review(goal,item,self.verdict(item))
        (self.folder/'spec/irm1.yaml').write_text('conveyors: {}',encoding='utf-8')
        with self.assertRaisesRegex(DocumentEvidenceError,'Stale citation'):self.manager.validate_review(goal,item,self.verdict(item))
    def test_spec_traversal_protected_path_and_non_scalar_selection_rejected(self):
        goal,item,_=self.reference('Width 500 mm (spec:../outside.yaml#width).')
        for citation in ('spec:../outside.yaml#width','spec:.env#width','spec:spec/config.json#section'):
            if citation.endswith('#section'):Workspace(self.folder,self.store.root).write('spec/config.json','{"section":{"width":500}}')
            self.write_doc(goal,item,'# Summary\nWidth 500 mm ('+citation+').\n## Evidence\nSources.','review/VISUAL_REFERENCE.md')
            with self.subTest(citation=citation),self.assertRaises(DocumentEvidenceError):self.manager.validate_review(goal,self.store.get('plan_item',item['id']),self.verdict(item))
    def test_spec_string_and_json_list_keys(self):
        goal,item,_=self.reference('Color red (spec:spec/config.json#color). Width 500 mm (spec:spec/config.json#widths[0]).')
        Workspace(self.folder,self.store.root).write('spec/config.json','{"color":"red","widths":[500,800]}')
        self.assertTrue(self.manager.validate_review(goal,item,self.verdict(item))[0])
        self.write_doc(goal,item,'# Summary\nColor blue (spec:spec/config.json#color).\n## Evidence\nSource.','review/VISUAL_REFERENCE.md')
        with self.assertRaisesRegex(DocumentEvidenceError,'Spec value mismatch'):self.manager.validate_review(goal,self.store.get('plan_item',item['id']),self.verdict(item))
    def test_results_remain_execution_only_even_with_valid_spec(self):
        goal,item,_=self.reference('Accuracy 99 percent (spec:spec/config.json#accuracy).')
        Workspace(self.folder,self.store.root).write('spec/config.json','{"accuracy":99}')
        with self.assertRaisesRegex(DocumentEvidenceError,'successful execution'):self.manager.validate_review(goal,item,self.verdict(item))
    def test_static_attribution_and_design_storyboard_are_not_measured_results(self):
        for path,body,role in [('ATTRIBUTION.md','CC BY 4.0, copyright 2026 NVIDIA.','static'),('demo/STORYBOARD.md','Planned duration: 180 seconds.\nShot 1: 0–30 seconds.','design')]:
            goal,item,_=self.fixture(body,path=path,fmt='text',required=[])
            self.assertTrue(self.manager.validate_review(goal,item,self.verdict(item))[0]);check=document_bundle(self.manager,goal,item)['criteria'][0]['files'][0];self.assertEqual(check['doc_role'],role)
    def test_explicit_roles_validate_and_override_filename_inference(self):
        self.assertEqual(role_for('README.md',{'doc_role':'reference'})[0],'reference')
        for role in ('unknown',None):
            with self.assertRaises(ValueError):normalize_criteria([{'text':'Document','kind':'document','doc_role':role}])
        with self.assertRaises(ValueError):normalize_criteria([{'text':'Run','kind':'execution','doc_role':'static'}])
    def test_known_complete_spdx_bodies_and_wrapping_are_recognized(self):
        folder=Path(__file__).parent/'fixtures/licenses'
        for name in ('Apache-2.0','MIT','BSD-3-Clause'):
            text=(folder/(name+'.txt')).read_text(encoding='utf-8')
            self.assertEqual(recognize_license(text)['spdx_id'],name);self.assertEqual(recognize_license(text.replace('\n','\r\n').replace(' ','  '))['spdx_id'],name)
            self.assertIsNone(recognize_license('SPDX-License-Identifier: '+name+'\n'+name))
            self.assertFalse(format_check(text,'LICENSE',{'required_sections':[name]})[1])
        goal,item,_=self.fixture((folder/'Apache-2.0.txt').read_text(encoding='utf-8'),path='LICENSE',fmt='text',required=['Apache-2.0'])
        self.assertTrue(self.manager.validate_review(goal,item,self.verdict(item))[0]);self.assertEqual(document_bundle(self.manager,goal,item)['criteria'][0]['files'][0]['license']['spdx_id'],'Apache-2.0')
    def test_license_identifier_alone_and_missing_sections_still_fail(self):
        self.assertTrue(format_check('Apache-2.0\nSPDX-License-Identifier: Apache-2.0','LICENSE',{'required_sections':['Apache-2.0']})[1])
        goal,item,_=self.fixture('Attribution content.',path='ATTRIBUTION.md',required=['Missing section'])
        with self.assertRaisesRegex(DocumentEvidenceError,'Missing required section'):self.manager.validate_review(goal,item,self.verdict(item))
    def test_official_apache_standard_header_is_distinct_from_full_terms(self):
        text=(Path(__file__).parent/'fixtures/licenses/Apache-2.0-header.txt').read_text(encoding='utf-8')
        recognized=recognize_license('Apache License\nVersion 2.0\n'+text+'\nProject-specific attribution remains separate.')
        self.assertEqual(recognized['spdx_id'],'Apache-2.0');self.assertEqual(recognized['match_type'],'standard_header')
        self.assertIsNone(recognize_license(text.replace('Version 2.0','Version 3.0')))
