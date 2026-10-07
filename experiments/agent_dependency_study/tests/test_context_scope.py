"""Resolved context must refine the actual selected declaration."""
import sys
import unittest
from pathlib import Path
BASE=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(BASE/'scripts/hallucination'),str(BASE/'vendor')]
from stage2_episode_linker import _build_commit,_materialize


def records(path='Black-Trucks-Co/package.json',package='@prisma/client',version='5.22.0'):
    common=dict(commit_id='c',ecosystem='npm',reason_codes='')
    imported=dict(common,event_id='i',event_type='import',path='Lupin-Project/lib/prisma.ts',
        raw_target='@prisma/client',package_candidate='@prisma/client',version_spec='',event_status='external_candidate')
    manifest=dict(common,event_id='m',event_type='manifest_addition',path='Lupin-Project/package.json',
        raw_target='@prisma/client',package_candidate='@prisma/client',version_spec='^6.19.3',event_status='direct_manifest')
    context=dict(ecosystem='npm',source_path=imported['path'],raw_target='@prisma/client',
        package_name=package,dependency_path=path,resolved_version=version,
        alignment_status='matched_manifest_and_lock',reason_codes='')
    return imported,manifest,context


def episodes(path,package='@prisma/client',version='5.22.0'):
    imported,manifest,context=records(path,package,version)
    drafts={};_build_commit([imported,manifest],drafts,[context])
    return _materialize(drafts)


class Scope(unittest.TestCase):
    def test_sibling_context_cannot_consume_new_declaration(self):
        rows,links=episodes('Black-Trucks-Co/package.json')
        self.assertEqual([(r['query_kind'],r['query_value']) for r in rows],[('range','^6.19.3')])
        self.assertEqual({r['event_id'] for r in links},{'i','m'})

    def test_same_declaration_can_use_bound_lock(self):
        rows,_=episodes('Lupin-Project/package.json',version='6.19.3')
        self.assertEqual([(r['query_kind'],r['query_value']) for r in rows],[('exact','6.19.3')])

    def test_package_mismatch_cannot_refine_manifest(self):
        rows,_=episodes('Lupin-Project/package.json',package='other')
        self.assertEqual(rows[0]['package_name'],'@prisma/client')
        self.assertEqual(rows[0]['query_value'],'^6.19.3')
