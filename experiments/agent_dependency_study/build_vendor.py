"""Copy existing local dependencies/source; no downloading or package installation."""
import argparse
import shutil
import json
import re
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--python-lib', type=Path, required=True)
    parser.add_argument('--semver', type=Path, required=True)
    args = parser.parse_args()
    base = Path(__file__).resolve().parent
    vendor = base/'vendor'
    vendor.mkdir(exist_ok=True)
    ignore = shutil.ignore_patterns('__pycache__', '*.pyc', '.env', 'node_modules')
    shutil.copytree(args.python_lib/'packaging', vendor/'packaging', ignore=ignore)
    for source in args.python_lib.glob('packaging-*.dist-info/LICENSE*'):
        shutil.copy2(source, vendor/('packaging-'+source.name))
    shutil.copytree(args.python_lib/'pyparsing', vendor/'pyparsing', ignore=ignore)
    for source in args.python_lib.glob('pyparsing-*.dist-info/LICENSE*'):
        shutil.copy2(source, vendor/('pyparsing-'+source.name))
    shutil.copytree(args.semver, vendor/'semver', ignore=ignore)
    src = args.project/'scripts/hallucination'
    names=json.loads((base/'code_organization.json').read_text(encoding='utf-8'))['module_renames']
    copy_source(src/'rq2_stage3_version_query.py',base/'src/stage3_version_query.py',names)
    copy_source(src/'rq2_python_stdlib.py',base/'src/python_stdlib.py',names)
    target = base/'scripts'
    for folder in ('collection', 'processing', 'metadata', 'hallucination', 'rq2/adjudication'):
        for path in (args.project/'scripts'/folder).rglob('*'):
            if path.suffix not in {'.py', '.ps1', '.md', '.js'} or '__pycache__' in path.parts:
                continue
            dest = target/path.relative_to(args.project/'scripts')
            dest=dest.with_name(names.get(path.stem,path.stem)+path.suffix)
            dest.parent.mkdir(parents=True, exist_ok=True)
            copy_source(path,dest,names)
    print('Local source/dependency copy complete; no collectors executed.')


def copy_source(source,target,names):
    text=source.read_text(encoding='utf-8-sig')
    for old,new in names.items():
        text=re.sub(r'\b'+re.escape(old)+r'\b',new,text)
    target.write_text(text,encoding='utf-8')


if __name__ == '__main__':
    main()
