"""Branch and evidence-contract tests independent of old C/P/I outcomes."""
import sys
import unittest
from pathlib import Path
BASE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BASE/'src'))
from stage3_classify import classify,collect_facts
from stage3_matching import Matcher


def episode(kind='exact',query='1.0.0',eco='npm'):
    return dict(query_kind=kind,query_value=query,ecosystem=eco,package_name='demo',dependency_quadrant='I-M+')


def obs(versions=(),status='ok',scope='enumeration',**kwargs):
    return dict(lookup_status=status,scope=scope,time_type='npm_version_time',
                versions=[dict(version=v,published_at=t) for v,t in versions],**kwargs)


class Classification(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.matcher=Matcher()

    @classmethod
    def tearDownClass(cls):
        cls.matcher.close()

    def run_case(self, records,ep=None,**kwargs):
        return classify(ep or episode(),list(enumerate(records)),
                        '2026-01-01T00:00:00Z',self.matcher,**kwargs)

    def test_before_and_postdate(self):
        for t,label in [('2025-01-01T00:00:00Z','present_at_commit'),('2026-01-02T00:00:00Z','hallucinated_dependency')]:
            self.assertEqual(self.run_case([obs([('1.0.0',t)])])['label'],label)

    def test_exact_range_parity(self):
        evidence=[obs([('0.5.0','2025-01-01T00:00:00Z')])]
        for ep in [episode(),episode('range','>=1.0.0')]:
            self.assertEqual(self.run_case(evidence,ep)['subtype'],'version_no_match')

    def test_404_empty_error_distinct(self):
        self.assertEqual(self.run_case([obs(status='not_found')])['subtype'],'package_no_match')
        for evidence in [obs(),obs(status='error')]:
            self.assertEqual(self.run_case([evidence])['label'],'insufficient_evidence')

    def test_legacy_omission_is_unknown(self):
        for versions in [[],[('0.5.0','2025-01-01T00:00:00Z')]]:
            self.assertEqual(self.run_case([obs(versions,scope='observed_subset')])['label'],'insufficient_evidence')

    def test_presence_beats_negative(self):
        records=[obs(status='not_found'),obs([('1.0.0','2025-01-01T00:00:00Z')])]
        self.assertEqual(self.run_case(records)['label'],'present_at_commit')
        self.assertEqual(self.run_case(records[::-1])['label'],'present_at_commit')

    def test_match_without_time_blocks_negative(self):
        records=[obs(status='not_found'),obs([('1.0.0','')])]
        self.assertEqual(self.run_case(records)['reason'],'matching_release_time_missing')

    def test_existing_package_blocks_name_absence(self):
        records=[obs(status='not_found'),obs([('0.1.0','')],scope='observed_subset')]
        self.assertEqual(self.run_case(records)['label'],'insufficient_evidence')
        records.append(obs([('0.1.0','')]))
        self.assertEqual(self.run_case(records)['subtype'],'version_no_match')

    def test_exact_endpoint_404_does_not_prove_package_exists(self):
        record=obs(status='not_found',negative_kind='version')
        self.assertEqual(self.run_case([record])['label'],'insufficient_evidence')

    def test_merge_time_for_same_version(self):
        records=[obs([('1.0.0','')]),obs([('1.0.0','2026-01-02T00:00:00Z')])]
        self.assertEqual(self.run_case(records)['subtype'],'postdate')

    def test_unknown_candidate_prevents_false_no_match(self):
        records=[obs([('not-a-version','')]),obs([('0.1.0','')])]
        self.assertEqual(self.run_case(records,episode('range','>=1.0.0'))['label'],'insufficient_evidence')
        self.assertEqual(self.run_case(records)['subtype'],'version_no_match')

    def test_npm_standard_prerelease_and_invalid(self):
        records=[obs([('1.1.0-beta.1','2025-01-01T00:00:00Z')])]
        self.assertEqual(self.run_case(records,episode('range','^1.0.0'))['subtype'],'version_no_match')
        self.assertEqual(self.run_case(records,episode('range','banana'))['label'],'insufficient_evidence')

    def test_parent_and_old_label_ignored(self):
        ep=episode()
        ep.update(parent_missing=True,old_label='confirmed_hallucination')
        records=[obs([('1.0.0','2025-01-01T00:00:00Z')])]
        self.assertEqual(self.run_case(records,ep),self.run_case(records))

    def test_mixed_scope_not_excluded(self):
        result=self.run_case([obs(status='not_found')],aux=dict(local_only=False,mixed_scope=True))
        self.assertEqual(result['label'],'hallucinated_dependency')
        self.assertEqual(self.run_case([],aux=dict(local_only=True))['label'],'out_of_scope')

    def test_proxy_sensitivity(self):
        record=obs([('1.0.0','2026-01-02T00:00:00Z')])
        record['time_type']='http_last_modified'
        self.assertEqual(self.run_case([record])['subtype'],'postdate')
        self.assertEqual(self.run_case([record],exclude_proxies=True)['label'],'insufficient_evidence')

    def test_partial_range_not_earliest(self):
        records=[obs([('1.0.0','2026-01-02T00:00:00Z')],scope='observed_subset')]
        self.assertEqual(self.run_case(records,episode('range','>=1'))['label'],'insufficient_evidence')
        records.append(obs([('1.0.0','2026-01-02T00:00:00Z')]))
        self.assertEqual(self.run_case(records,episode('range','>=1'))['subtype'],'postdate')

    def test_no_boundary_does_not_block_valid_absence(self):
        r=classify(episode(),[(1,obs(status='not_found'))],'',self.matcher)
        self.assertEqual(r['subtype'],'package_no_match')

    def test_multiecosystem_range_and_exact(self):
        cases=[('PyPI','>=1.0,<2.0','1.2'),('Cargo','^1.0','1.2.0'),
               ('Maven','[1.0,2.0)','1.2')]
        for eco,query,version in cases:
            with self.subTest(ecosystem=eco):
                records=[obs([(version,'2025-01-01T00:00:00Z')])]
                self.assertEqual(self.run_case(records,episode('range',query,eco))['label'],'present_at_commit')
                self.assertEqual(self.run_case(records,episode('exact','8.0.0',eco))['subtype'],'version_no_match')

    def test_pypi_normalization_and_go_pseudoversion(self):
        records=[obs([('1.0.0','2025-01-01T00:00:00Z')])]
        self.assertEqual(self.run_case(records,episode('exact','1.0','PyPI'))['label'],'present_at_commit')
        version='v0.0.0-20251201090000-abcdef123456'
        records=[obs([(version,'2025-12-01T09:00:00Z')])]
        self.assertEqual(self.run_case(records,episode('exact',version,'Go'))['label'],'present_at_commit')

    def test_maven_missing_metadata_time_filled_by_proxy(self):
        record=obs([('9.0.0','2026-02-01T00:00:00Z')],scope='exact',exact_query='9.0.0')
        record['time_type']='http_last_modified'
        records=[obs([('9.0.0','')]),record]
        ep=episode('exact','9.0.0','Maven')
        self.assertEqual(self.run_case(records,ep)['subtype'],'postdate')
        self.assertEqual(self.run_case(records,ep,exclude_proxies=True)['reason'],'matching_release_time_missing')

    def test_conflict_flag_requires_contradictory_observations(self):
        records=[obs([('0.5.0','2025-01-01T00:00:00Z')])]
        self.assertFalse(self.run_case(records)['conflict_observed'])
        records.append(obs(status='not_found'))
        self.assertTrue(self.run_case(records)['conflict_observed'])

    def test_unexpanded_maven_property_is_not_absence(self):
        ep=episode('name_only','','Maven')
        ep['package_name']='${project.groupId}:common'
        records=[obs(status='not_found')]
        self.assertEqual(self.run_case(records,ep)['reason'],'unresolved_property')
        self.assertEqual(self.run_case(records,ep,aux=dict(effective_package='org.example:common'))['subtype'],'package_no_match')

    def test_cached_facts_are_equivalent_and_not_mutated(self):
        records=[(1,obs([('1.0.0','2026-02-01T00:00:00Z')]))]
        facts=collect_facts(episode(),records,self.matcher,False)
        for boundary in ['2026-01-01T00:00:00Z','2026-03-01T00:00:00Z']:
            expected=classify(episode(),records,boundary,self.matcher)
            cached=classify(episode(),records,boundary,self.matcher,prepared_facts=facts)
            self.assertEqual(cached,expected)

    def test_rfc3339_fractional_precision(self):
        for fraction in ['1','12','123','1234','123456','123456789']:
            records=[obs([('1.0.0','2025-01-01T00:00:00.'+fraction+'Z')])]
            self.assertEqual(self.run_case(records)['label'],'present_at_commit')

    def test_cargo_partial_zero_caret_boundaries(self):
        for query,inside,outside in [('0','0.9.0','1.0.0'),('^0','0.9.0','1.0.0'),
                                     ('0.0','0.0.9','0.1.0'),('^0.0','0.0.9','0.1.0')]:
            ep=episode('range',query,'Cargo')
            self.assertEqual(self.run_case([obs([(inside,'2025-01-01T00:00:00Z')])],ep)['label'],'present_at_commit')
            self.assertEqual(self.run_case([obs([(outside,'')])],ep)['subtype'],'version_no_match')

    def test_cargo_comparators_prereleases_and_unsupported(self):
        records=[obs([('1.5.0','2025-01-01T00:00:00Z')])]
        self.assertEqual(self.run_case(records,episode('range','>= 1.0, < 2','Cargo'))['label'],'present_at_commit')
        self.assertEqual(self.run_case(records,episode('range','1 || 2','Cargo'))['label'],'insufficient_evidence')
        records=[obs([('1.5.0-beta.1','2025-01-01T00:00:00Z')])]
        self.assertEqual(self.run_case(records,episode('range','1','Cargo'))['subtype'],'version_no_match')
        self.assertEqual(self.run_case(records,episode('range','=1.5.0-beta.1','Cargo'))['label'],'present_at_commit')
        records=[obs([('1.5.1','2025-01-01T00:00:00Z')])]
        self.assertEqual(self.run_case(records,episode('range','1.5.0-fox','Cargo'))['label'],'present_at_commit')


if __name__=='__main__':
    unittest.main()
