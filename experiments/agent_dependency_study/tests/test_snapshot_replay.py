"""Snapshot restoration must bind changed lines and preserve request semantics."""
import sys
import unittest
from pathlib import Path
BASE=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(BASE/'src'),str(BASE/'scripts/hallucination'),str(BASE/'vendor')]
from stage1_manifest_diff import FilePatch,PatchLine
from stage1_snapshot import replay_manifest
from stage2_alignment import classify_version,plan_manifest
from stage2_version_repairs import poetry_constraint
from stage2_episode_linker import _add_manifest_only
from stage1_replay_adapter import refresh_commit


def patch(path,text,added,visible=None):
    lines=text.splitlines()
    numbers=visible or range(1,len(lines)+1)
    return FilePatch(path,tuple(PatchLine(i,1,lines[i-1],i in added) for i in numbers))


class SnapshotReplay(unittest.TestCase):
    def test_maven_comment_is_not_dependency(self):
        text='<project>\n<!--\n<dependency>\n<groupId>example</groupId>\n<artifactId>fake</artifactId>\n</dependency>\n-->\n<dependency>\n<groupId>example</groupId>\n<artifactId>real</artifactId>\n</dependency>\n</project>'
        rows,_=replay_manifest(patch('pom.xml',text,{5,10},[5,10]),text)
        self.assertEqual([r.package_candidate for r in rows],['example:real'])

    def test_snapshot_constraint_wins_over_old_context(self):
        event=dict(commit_id='c',event_id='e',event_type='manifest_addition',ecosystem='PyPI',
            path='requirements.txt',raw_target='requests',package_candidate='requests',
            version_spec='>=2,<3',reason_codes='child_snapshot_structure')
        context=dict(package_name='requests',version_spec='>=2,',alignment_status='matched_manifest')
        drafts={};_add_manifest_only(event,drafts,context)
        self.assertEqual(next(iter(drafts.values())).query_value,'>=2,<3')
        context['resolved_version']='2.5'
        drafts={};_add_manifest_only(event,drafts,context)
        self.assertEqual(next(iter(drafts.values())).query_value,'2.5')

    def test_bound_maven_property_context_is_preserved(self):
        event=dict(commit_id='c',event_id='e',event_type='manifest_addition',ecosystem='Maven',
            path='pom.xml',raw_target='org.mapstruct:mapstruct',package_candidate='org.mapstruct:mapstruct',
            version_spec='${mapstruct.version}',reason_codes='child_snapshot_structure')
        context=dict(package_name='org.mapstruct:mapstruct',version_spec='1.5.5.Final',alignment_status='matched_manifest')
        drafts={};_add_manifest_only(event,drafts,context)
        self.assertEqual(next(iter(drafts.values())).query_kind,'exact')
        self.assertEqual(next(iter(drafts.values())).query_value,'1.5.5.Final')

    def test_commit_metadata_reflects_repaired_events(self):
        commit=dict(commit_id='c',repo='a/b',sha='s',agent='Claude',manifest_event_count='0')
        events=[dict(event_type='manifest_addition'),dict(event_type='import',actual_language='Python')]
        row=refresh_commit(commit,events)
        self.assertEqual(row['manifest_event_count'],'1')
        self.assertEqual(row['dependency_quadrant'],'I+M+')
        self.assertEqual(row['agent'],'Claude')

    def test_missing_section_and_unchanged_rows(self):
        text='{\n "dependencies": {\n  "axios": "^1.7.0",\n  "react": "18.0.0"\n }\n}'
        rows,status=replay_manifest(patch('package.json',text,{3},[3,4]),text)
        self.assertEqual(status,'snapshot_applied')
        self.assertEqual([(r.package_candidate,r.new_line_number) for r in rows],[('axios',3)])

    def test_mismatch_refuses_fulltext(self):
        old='\n"axios": "1"'
        rows,status=replay_manifest(patch('package.json',old,{2},[2]),'{}')
        self.assertIsNone(rows);self.assertEqual(status,'snapshot_line_mismatch')

    def test_nested_non_dependency_is_not_event(self):
        text='{\n "other": {"dependencies": {"wrong":"1"}},\n "dependencies": {"right":"2"}\n}'
        rows,_=replay_manifest(patch('package.json',text,{2,3}),text)
        self.assertEqual([r.package_candidate for r in rows],['right'])

    def test_multiple_inline_dependencies_stable_ordinals(self):
        text='{"dependencies":{"axios":"1","react":"2"}}'
        rows,_=replay_manifest(patch('package.json',text,{1}),text)
        self.assertEqual([r.event_ordinal for r in rows],[0,1])

    def test_go_context_across_hunks(self):
        text='module example.com/x\nrequire (\n example.com/a v1.0.0\n example.com/b v2.0.0\n)'
        rows,_=replay_manifest(patch('go.mod',text,{4},[4,5]),text)
        self.assertEqual([r.package_candidate for r in rows],['example.com/b'])

    def test_requirements_continuation_preserves_constraints(self):
        text='requests>=2, \\\n <3'
        rows,_=replay_manifest(patch('requirements.txt',text,{1,2}),text)
        self.assertEqual(rows[0].version_spec,'>=2, <3')
        self.assertEqual(rows[0].new_line_number,1)

    def test_wildcard_is_range(self):
        self.assertEqual(classify_version('PyPI','==2.*')[:2],('range','==2.*'))

    def test_vendor_manifest_stays_excluded(self):
        text='{"dependencies":{"axios":"1"}}'
        rows,status=replay_manifest(patch('vendor/package.json',text,{1}),text)
        self.assertIsNone(rows);self.assertEqual(status,'non_research_path')

    def test_added_hash_only_does_not_add_requirement(self):
        text='requests==2.0 \\\n --hash=sha256:abc'
        rows,_=replay_manifest(patch('requirements.txt',text,{2}),text)
        self.assertEqual(rows,[])

    def test_poetry_constraints(self):
        self.assertEqual(poetry_constraint('^0.2.3'),'>=0.2.3,<0.3.0')
        self.assertEqual(poetry_constraint('~1.2'),'>=1.2.0,<1.3.0')
        event=dict(ecosystem='PyPI',package_candidate='requests',version_spec='^2.0',path='pyproject.toml')
        self.assertEqual(plan_manifest(event).query_value,'>=2.0.0,<3.0.0')

    def test_maven_literal_and_gradle_dynamic(self):
        for value in ['1.5.5.Final','20231013']:
            self.assertEqual(classify_version('Maven',value)[0],'exact')
        for value in ['1.+','$kotlin_version','latest.release']:
            self.assertEqual(classify_version('Maven',value)[0],'range')


if __name__=='__main__':unittest.main()
