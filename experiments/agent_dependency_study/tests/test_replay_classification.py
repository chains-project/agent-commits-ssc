"""Scope witnesses follow the repaired episode, including unaffected imports."""
import sys
import unittest
from pathlib import Path
BASE=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(BASE),str(BASE/'src')]
from replay_classification import applicable_auxiliary


class Migration(unittest.TestCase):
    def setUp(self):
        self.episode=dict(ecosystem='PyPI',package_name='runpy',query_kind='name_only',query_value='')
        self.aux=dict(stdlib_only=True,stdlib_source_row={'stdlib_roots_json':'["runpy"]'})
        self.event=dict(event_id='e',event_type='import',raw_target='runpy')

    def test_unchanged_import_survives_unrelated_commit_change(self):
        self.assertTrue(applicable_auxiliary(self.aux,self.episode,self.episode,[self.event])['stdlib_only'])

    def test_new_manifest_invalidates_import_only_scope(self):
        events=[self.event,dict(event_id='m',event_type='manifest_addition',raw_target='runpy')]
        self.assertNotIn('stdlib_only',applicable_auxiliary(self.aux,self.episode,self.episode,events))

    def test_changed_query_does_not_inherit_auxiliary(self):
        self.assertEqual(applicable_auxiliary(self.aux,self.episode,{**self.episode,'query_value':'^2'},[self.event]),{})

    def test_new_identity_has_no_old_auxiliary(self):
        self.assertEqual(applicable_auxiliary(self.aux,None,self.episode,[self.event]),{})

    def test_local_manifest_witness_must_match_event_set(self):
        manifest=dict(event_id='m',event_type='manifest_addition',path='Cargo.toml',version_spec='1')
        aux=dict(local_only=True,manifest_witnesses=[dict(event=manifest)])
        self.assertTrue(applicable_auxiliary(aux,self.episode,self.episode,[manifest])['local_only'])
        self.assertNotIn('local_only',applicable_auxiliary(aux,self.episode,self.episode,[{**manifest,'event_id':'new'}]))
