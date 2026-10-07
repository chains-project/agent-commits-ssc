"""All queries share pinned semantics; no candidate-only matching overrides."""
import json
import subprocess
import re
from functools import lru_cache
from pathlib import Path
import sys

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / 'vendor'))
from packaging.specifiers import SpecifierSet, InvalidSpecifier
from packaging.version import Version, InvalidVersion
from stage3_version_query import build_range_matcher, exact_equal


class Matcher:
    def __init__(self, node='node'):
        self.process = subprocess.Popen([node, str(BASE/'src/stage3_semver.js')],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding='utf-8')

    def close(self):
        self.process.stdin.close()
        if self.process.wait(timeout=15) != 0:
            raise RuntimeError('semver process failed')

    @lru_cache(maxsize=32768)
    def match(self, eco, kind, query, versions):
        if kind == 'name_only':
            return True, tuple(range(len(versions))), ()
        if eco == 'npm':
            return self.npm(kind, query, versions)
        if eco == 'Cargo':
            normalized=cargo_range(query) if kind=='range' else query
            if normalized is None:
                return False, (), ()
            return self.npm(kind, normalized, versions)
        return other_match(eco, kind, query, versions)

    def npm(self, kind, query, versions):
        self.process.stdin.write(json.dumps(dict(kind=kind, query=query, versions=versions))+'\n')
        self.process.stdin.flush()
        line = self.process.stdout.readline()
        if not line:
            raise RuntimeError('Pinned semver runtime stopped; no parser fallback')
        result = json.loads(line)
        return result['valid'], tuple(result['matches']), tuple(result['unknown'])


def cargo_range(query):
    """Cargo comma-intersected comparators; bare numeric requests imply caret."""
    clauses=[]
    token=r'(\^|~|>=|<=|>|<|=)?\s*([0-9xX*]+(?:\.[0-9xX*]+){0,2}(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?)'
    for raw in query.split(','):
        match=re.fullmatch(token,raw.strip())
        if match is None:
            return None
        operator,version=match.groups()
        core=re.split('[-+]',version,maxsplit=1)[0]
        if not operator and not any(c in core for c in '*xX'):
            operator='^'
        clauses.append((operator or '')+version)
    return ' '.join(clauses)


def other_match(eco, kind, query, versions):
    if kind == 'exact':
        if not query.strip():
            return False, (), ()
        if eco == 'PyPI':
            try:
                Version(query)
            except InvalidVersion:
                return False, (), ()
        test = lambda value: exact_value(eco, value, query)
    else:
        test = build_range_matcher(eco, query)
        if test is None:
            return False, (), ()
    outcomes = [test(v) for v in versions]
    return True, tuple(i for i, x in enumerate(outcomes) if x is True), tuple(i for i, x in enumerate(outcomes) if x is None)


def exact_value(eco, value, query):
    if eco == 'PyPI':
        try:
            return Version(value) == Version(query)
        except InvalidVersion:
            return False
    if eco == 'Maven':
        return value.strip() == query.strip()
    return exact_equal(value, query)
