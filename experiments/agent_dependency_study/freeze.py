"""Record source, dependency, sample and result fingerprints for this local handoff."""
import json
import platform
import subprocess
import sys
from pathlib import Path
BASE=Path(__file__).resolve().parent
sys.path.insert(0,str(BASE/'src'))
from common import digest,write_json


def main():
    files=[]
    for path in sorted(BASE.rglob('*')):
        relative=path.relative_to(BASE)
        if not path.is_file() or '__pycache__' in relative.parts or path.suffix=='.pyc':
            continue
        if path.name.endswith(('.sqlite-wal','.sqlite-shm','.sqlite-journal')):
            continue
        if relative.parts[0] in {'demo_output'} or path.name=='package_manifest.json':
            continue
        files.append(dict(path=relative.as_posix(),bytes=path.stat().st_size,sha256=digest(path)))
    result=dict(python=platform.python_version(),node=subprocess.check_output(['node','--version'],text=True).strip(),
                files=files,total_listed_bytes=sum(row['bytes'] for row in files),
                raw_data='https://huggingface.co/datasets/ASSERT-KTH/agent-commits-raw',
                note='Local delivery snapshot of code, saved evidence, results and reproduction instructions.')
    write_json(BASE/'package_manifest.json',result)
    print(json.dumps({k:v for k,v in result.items() if k!='files'},indent=2))


if __name__=='__main__':
    main()
