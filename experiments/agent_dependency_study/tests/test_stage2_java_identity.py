"""Documented Java mapping, module isolation and episode-link regression tests."""
import sys
import unittest
from pathlib import Path
BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE/'scripts/hallucination'), str(BASE/'vendor')]
from stage2_java_identity import resolve_import, rule_for
from stage2_episode_linker import _build_commit, _materialize


def imp(target='lombok.Data', path='app/src/main/java/demo/User.java', eid='i1'):
    return dict(event_id=eid, commit_id='c1', event_type='import', path=path,
        ecosystem='Maven', raw_target=target, package_candidate='', version_spec='',
        event_status='mapping_uncertain', reason_codes='mapping_uncertain')


def manifest(version='1.18.30', path='app/pom.xml', eid='m1', package='org.projectlombok:lombok'):
    return dict(event_id=eid, commit_id='c1', event_type='manifest_addition', path=path,
        ecosystem='Maven', raw_target=package, package_candidate=package, version_spec=version,
        event_status='direct_manifest', reason_codes='')


class JavaIdentity(unittest.TestCase):
    def test_catalog_families(self):
        for target, package in [('lombok.Data','org.projectlombok:lombok'),
            ('org.slf4j.Logger','org.slf4j:slf4j-api'),
            ('org.junit.jupiter.api.Test','org.junit.jupiter:junit-jupiter-api')]:
            self.assertEqual(rule_for(imp(target))['coordinate'], package)

    def test_high_frequency_module_owners(self):
        targets = {
            'org.springframework.stereotype.Service':'org.springframework:spring-context',
            'org.springframework.transaction.annotation.Transactional':'org.springframework:spring-tx',
            'org.springframework.data.repository.query.Param':'org.springframework.data:spring-data-commons',
            'org.mockito.junit.jupiter.MockitoExtension':'org.mockito:mockito-junit-jupiter',
            'org.mockito.Mockito.when':'org.mockito:mockito-core',
            'com.fasterxml.jackson.databind.ObjectMapper':'com.fasterxml.jackson.core:jackson-databind',
            'jakarta.persistence.Entity':'jakarta.persistence:jakarta.persistence-api',
            'org.springframework.security.crypto.password.PasswordEncoder':'org.springframework.security:spring-security-crypto'}
        for target,coordinate in targets.items():
            self.assertEqual(rule_for(imp(target))['coordinate'],coordinate)

    def test_listed_package_wildcard_is_not_a_prefix_rule(self):
        self.assertEqual(rule_for(imp('jakarta.persistence.*'))['coordinate'],'jakarta.persistence:jakarta.persistence-api')
        self.assertIsNone(rule_for(imp('jakarta.persistence.Imaginary')))

    def test_exact_targets_and_members(self):
        for value in ['lombok.Unknown', 'lombok.DataExtra', 'org.junit.jupiter.api.Assertions.unknown', 'org.junit.jupiter.api.*']:
            self.assertIsNone(rule_for(imp(value)))
        self.assertIsNotNone(rule_for(imp('org.junit.jupiter.api.Assertions.assertEquals')))
        self.assertIsNotNone(rule_for(imp('org.junit.jupiter.api.Assertions.*')))

    def test_local_context_priority(self):
        for status in ['first_party_excluded','private_or_local_excluded']:
            self.assertIsNone(resolve_import(imp(), [], [], dict(alignment_status=status)))

    def test_local_context_priority_through_linker(self):
        for status in ['first_party_excluded','private_or_local_excluded']:
            drafts = {}
            context = dict(source_path=imp()['path'], raw_target='lombok.Data', ecosystem='Maven',
                package_name='local:lombok', alignment_status=status, reason_codes='local_witness')
            _build_commit([imp(), manifest()], drafts, [context])
            episodes, links = _materialize(drafts)
            import_id = next(r['episode_id'] for r in links if r['event_id']=='i1')
            self.assertEqual(next(r for r in episodes if r['episode_id']==import_id)['query_kind'],'excluded')

    def test_witness_order_stable(self):
        rows = [manifest('1.18.20'), manifest('1.18.30', eid='m2')]
        self.assertEqual(resolve_import(imp(), rows, []), resolve_import(imp(), rows[::-1], []))

    def test_mapped_package_without_registry_record(self):
        sys.path.insert(0,str(BASE/'src'))
        from stage3_classify import classify
        drafts = {}
        _build_commit([imp()], drafts, [])
        episode = _materialize(drafts)[0][0]
        decision = classify(episode, [], '2026-01-01T00:00:00Z', None)
        self.assertEqual(decision['label'],'insufficient_evidence')

    def test_nearest_ancestor(self):
        rows = [manifest('1.18.20', 'pom.xml'), manifest('1.18.30', eid='m2')]
        self.assertEqual(resolve_import(imp(), rows, [])['context']['version_spec'], '1.18.30')

    def test_sibling_and_missing_path(self):
        for path in ['other/pom.xml', '']:
            result = resolve_import(imp(), [manifest(path=path)], [])
            self.assertEqual(result['context']['version_spec'], '')
            self.assertEqual(result['manifests'], [])

    def test_conflicting_versions(self):
        rows = [manifest('1.18.20'), manifest('1.18.30', eid='m2')]
        result = resolve_import(imp(), rows, [])
        self.assertEqual(result['context']['version_spec'], '')
        self.assertIn('version_conflict', result['context']['reason_codes'])
        self.assertEqual(result['manifests'], [])

    def test_other_coordinate_never_supplies_version(self):
        result = resolve_import(imp(), [manifest('99.0.0', package='org.postgresql:postgresql')], [])
        self.assertEqual(result['context']['package_name'], 'org.projectlombok:lombok')
        self.assertEqual(result['context']['version_spec'], '')

    def test_episode_merge_and_event_closure(self):
        events = [imp(), imp('lombok.RequiredArgsConstructor', eid='i2'), manifest()]
        drafts = {}
        _build_commit(events, drafts, [])
        episodes, links = _materialize(drafts)
        self.assertEqual(len(episodes), 1)
        self.assertEqual({r['event_id'] for r in links}, {'i1','i2','m1'})
        self.assertIn('lombok_core_annotations_v1', episodes[0]['reason_codes'])

    def test_conflict_retains_manifests_and_determinism(self):
        events = [imp(), manifest('1.18.20'), manifest('1.18.30', eid='m2')]
        a, b = {}, {}
        _build_commit(events, a, [])
        _build_commit(list(reversed(events)), b, [])
        self.assertEqual(_materialize(a), _materialize(b))
        self.assertEqual(len(_materialize(a)[0]), 3)

    def test_modules_keep_distinct_versions(self):
        events = [imp(), imp(path='other/src/User.java', eid='i2'), manifest(),
                  manifest('1.18.20', path='other/pom.xml', eid='m2')]
        drafts = {}
        _build_commit(events, drafts, [])
        self.assertEqual({e['query_value'] for e in _materialize(drafts)[0]}, {'1.18.20','1.18.30'})


if __name__ == '__main__':
    unittest.main()
